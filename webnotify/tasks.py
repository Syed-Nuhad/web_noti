# webnotify/tasks.py
from contextlib import contextmanager
import hashlib
import logging
import re
from typing import Dict, Tuple, Optional
import os

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import requests
from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone
from celery import shared_task

from .models import NotificationSource, Notification

logger = logging.getLogger(__name__)

# Generic keywords that commonly appear near inbox/notification badges
KEYWORDS = re.compile(r"\b(inbox|message|messages|notification|notifications|alert|alerts)\b", re.I)

# A sane desktop UA helps some sites behave correctly
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
}

# ------------------------- helpers (state) -------------------------

def _get_extra(src: NotificationSource) -> Dict:
    return src.extra_config or {}

def _save_extra(src: NotificationSource, extra: Dict):
    src.extra_config = extra
    src.last_checked = timezone.now()
    src.save(update_fields=["extra_config", "last_checked"])

# --------------------- fingerprint-based change -------------------

def _fingerprint_response(resp: requests.Response) -> Tuple[str, str, str]:
    """Return (etag, last_modified, sha256(body)). Empty strings if missing."""
    etag = resp.headers.get("ETag", "") or ""
    last_mod = resp.headers.get("Last-Modified", "") or ""
    # resp may be our fake object in rendered mode
    body = getattr(resp, "content", b"") or b""
    body_hash = hashlib.sha256(body).hexdigest()
    return etag, last_mod, body_hash

def _load_previous_fingerprint(extra: Dict) -> Tuple[str, str, str]:
    fp = extra.get("fingerprint") or {}
    return (
        fp.get("etag", "") or "",
        fp.get("last_modified", "") or "",
        fp.get("body_hash", "") or "",
    )

def _store_fingerprint(extra: Dict, etag: str, last_mod: str, body_hash: str) -> Dict:
    extra = dict(extra or {})
    extra["fingerprint"] = {
        "etag": etag,
        "last_modified": last_mod,
        "body_hash": body_hash,
        "saved_at": timezone.now().isoformat(),
    }
    return extra

# --------------------- cookies & headers --------------------------

def _build_cookies(extra: Dict) -> Dict[str, str]:
    """
    Cookies are uploaded by desktop_client/link_source.py into extra_config["cookies"].
    Keep them as a flat dict {name: value}.
    """
    c = extra.get("cookies") or {}
    return {str(k): str(v) for k, v in c.items()}

def _build_headers(extra: Dict) -> Dict[str, str]:
    # Allow future overrides, keep simple for now
    headers = dict(DEFAULT_HEADERS)
    user_headers = extra.get("headers") or {}
    for k, v in user_headers.items():
        headers[str(k)] = str(v)
    return headers

# --------------------- content parsing (counts) -------------------

def _visible_text(soup: BeautifulSoup) -> str:
    for script in soup(["script", "style", "noscript", "template"]):
        script.decompose()
    return soup.get_text("\n", strip=True)

def _extract_count_from_title(soup: BeautifulSoup) -> Optional[int]:
    # e.g. "(3) Inbox - Example"
    try:
        title = (soup.title.string or "").strip()
    except Exception:
        title = ""
    m = re.search(r"\((\d{1,3})\)", title)
    return int(m.group(1)) if m else None

def _extract_count_from_aria_or_badges(soup: BeautifulSoup) -> Optional[int]:
    try:
        # aria-label with numbers
        for el in soup.find_all(attrs={"aria-label": True}):
            lab = str(el.get("aria-label") or "")
            m = re.search(r"\b(\d{1,3})\b", lab)
            if m and KEYWORDS.search(lab):
                return int(m.group(1))
        # class contains "badge"
        for el in soup.find_all(class_=True):
            cls = " ".join(el.get("class") or [])
            if "badge" in cls.lower():
                txt = el.get_text(" ", strip=True)
                m = re.search(r"\b(\d{1,3})\b", txt)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None

def _extract_count_from_text(soup_text: str) -> Optional[int]:
    # lines like "Inbox (4)" or "Notifications 7"
    best = None
    for line in soup_text.splitlines():
        line = line.strip()
        if not line or not KEYWORDS.search(line):
            continue
        m = re.search(r"\b(\d{1,3})\b", line)
        if m:
            val = int(m.group(1))
            best = val if best is None else max(best, val)
    return best

def _hash_text(txt: str) -> str:
    return hashlib.sha256(txt.encode("utf-8", "ignore")).hexdigest()

def _build_session(headers: Dict, cookies: Dict, timeout_connect=5, timeout_read=8) -> requests.Session:
    """
    Build a requests session with retries and separate connect/read timeouts.
    """
    sess = requests.Session()
    sess.trust_env = False  # ignore system proxies (can cause stalls)
    sess.headers.update(headers)
    if cookies:
        sess.cookies.update(cookies)

    retry = Retry(
        total=2, connect=2, read=2, status=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "OPTIONS"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess

# --------------------- optional Playwright rendered fetch ---------

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    _PW_AVAILABLE = True
except Exception:
    _PW_AVAILABLE = False

@contextmanager
def _playwright_ctx():
    if not _PW_AVAILABLE:
        yield None, None
        return
    from playwright.sync_api import sync_playwright as _sp  # local import
    pw = _sp().start()
    ctx = None
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir="/tmp/wn_pw_profile",
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-renderer-backgrounding",
            ],
        )
        yield pw, ctx
    finally:
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

def _fetch_rendered_html(url: str, cookies: dict, user_data_dir: str = None, wait_ms: int = 3000) -> str:
    """
    Generic dynamic-page fetcher using Playwright with a persistent profile.
    No site-specific logic. Returns HTML string or "" on failure.
    """
    if not _PW_AVAILABLE:
        logger.warning("Playwright not available for rendered fetch.")
        return ""

    # Persistent profile so logins persist across runs
    if not user_data_dir:
        base = os.path.dirname(os.path.dirname(__file__))  # project root-ish
        user_data_dir = os.path.join(base, "desktop_client", ".pw_profile")

    html = ""
    from playwright.sync_api import sync_playwright as _sp
    with _sp() as pw:
        ctx = None
        # Try Chrome → Edge → bundled Chromium
        for channel in ("chrome", "msedge", None):
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=True,
                    channel=channel,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-gpu",
                        "--disable-renderer-backgrounding",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--password-store=basic",
                    ],
                    viewport={"width": 1366, "height": 850},
                )
                break
            except Exception:
                ctx = None
        if ctx is None:
            logger.warning("Could not launch any Chromium channel for rendered fetch.")
            return ""

        page = ctx.new_page()

        # Optional: seed cookies (usually not needed if profile already has them)
        if cookies:
            try:
                cookie_list = []
                for k, v in cookies.items():
                    cookie_list.append({"name": k, "value": v, "path": "/", "httpOnly": False, "secure": True})
                if cookie_list:
                    ctx.add_cookies(cookie_list)
            except Exception:
                pass

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(wait_ms)  # allow client JS to render
            html = page.content()
        except Exception as e:
            logger.warning("Rendered fetch failed for %s: %s", url, e)
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    return html

# -------------------------- main task -----------------------------

@shared_task
def check_source(source_id: int) -> bool:
    """
    Real checker (no CSS selector needed):
      1) Fetch page (requests by default; Playwright if extra_config.rendered == True).
      2) Baseline on first run OR whenever fetch mode changes (no notify).
      3) Notify only when:
           - unread COUNT increases, OR
           - (fallback) visible text containing inbox/message keywords changed.
      4) Update baseline every run.

    Returns:
      True  = a new Notification was created
      False = no new Notification (or baseline/update only)
    """
    # ---------- load source ----------
    try:
        source = NotificationSource.objects.select_related("user").get(pk=source_id, enabled=True)
    except NotificationSource.DoesNotExist:
        return False

    extra        = _get_extra(source)                # dict
    use_rendered = bool(extra.get("rendered", False))
    prev_mode    = extra.get("mode") or "requests"   # previous fetch mode recorded in baseline
    cur_mode     = "rendered" if use_rendered else "requests"

    cookies = _build_cookies(extra)
    headers = _build_headers(extra)

    # ---------- fetch HTML (requests first; rendered if flagged or failed) ----------
    html_text: Optional[str] = None
    resp_for_fp = None

    # Fast path
    try:
        tout = extra.get("timeout")
        if isinstance(tout, (int, float)):
            connect_t, read_t = max(2, int(tout) // 2), max(5, int(tout))
        else:
            connect_t, read_t = 5, 8

        headers["Connection"] = "close"

        sess = _build_session(headers, cookies, timeout_connect=connect_t, timeout_read=read_t)
        r = sess.get(
            source.check_url,
            timeout=(connect_t, read_t),
            allow_redirects=True,
        )
        r.raise_for_status()
        html_text   = r.text
        resp_for_fp = r
    except Exception as e:
        logger.warning("Fetch failed for %s: %s", source.check_url, e)

    # Rendered fallback (only if enabled or requests failed)
    if use_rendered or html_text is None:
        try:
            rendered = _fetch_rendered_html(
                source.check_url,
                cookies=cookies,
                # fixed: pass wait_ms (helper doesn't accept timeout_ms)
                wait_ms=int(extra.get("render_timeout_ms", 15000)),
            )
            if rendered:
                html_text = rendered

                # Shim for fingerprint (no HTTP headers in rendered mode)
                class _FakeResp:
                    headers = {}
                    def __init__(self, body: str): self.content = body.encode("utf-8", "ignore")
                resp_for_fp = _FakeResp(rendered)
        except Exception as e:
            logger.warning("Rendered fetch failed for %s: %s", source.check_url, e)

    if html_text is None or resp_for_fp is None:
        source.last_checked = timezone.now()
        source.save(update_fields=["last_checked"])
        return False

    # ---------- fingerprint + parse ----------
    etag, last_mod, body_hash = _fingerprint_response(resp_for_fp)
    prev_etag, prev_last, prev_hash = _load_previous_fingerprint(extra)

    soup = BeautifulSoup(html_text, "html.parser")
    text = _visible_text(soup)

    prev_count   = extra.get("last_count")
    parsed_count = (
        _extract_count_from_title(soup)
        or _extract_count_from_aria_or_badges(soup)
        or _extract_count_from_text(text)
    )
    text_hash = _hash_text(text)

    # ---------- baseline if first run OR fetch mode changed ----------
    # This prevents false "new" alerts when you just switched to rendered mode
    if (prev_etag, prev_last, prev_hash, prev_count) == ("", "", "", None) or (prev_mode != cur_mode):
        extra = _store_fingerprint(extra, etag, last_mod, body_hash)
        if parsed_count is not None:
            extra["last_count"] = int(parsed_count)
            extra.pop("last_hash", None)
        else:
            extra["last_hash"] = text_hash
            extra.pop("last_count", None)
        extra["mode"] = cur_mode
        _save_extra(source, extra)
        return False

    created = False

    # ---------- preferred: unread-count increased ----------
    if parsed_count is not None:
        if prev_count is None:
            extra["last_count"] = int(parsed_count)
        else:
            if int(parsed_count) > int(prev_count):
                with transaction.atomic():
                    Notification.objects.create(
                        user=source.user,
                        source=source,
                        title=f"New messages on {source.name}",
                        message=f"Unread count: {parsed_count}",
                        detected_at=timezone.now(),
                        seen=False,
                        played=False,
                        meta={"detector": "count", "prev": int(prev_count), "now": int(parsed_count)},
                    )
                extra["last_count"] = int(parsed_count)
                created = True
            else:
                # Update baseline even if not increased (so future increases work correctly)
                extra["last_count"] = int(parsed_count)

    # ---------- fallback: only notify if KEYWORD text changed ----------
    # If count didn't trigger, require inbox/message keywords AND changed visible text.
    if not created and parsed_count is None:
        prev_text_hash = extra.get("last_hash")
        contains_keywords = bool(KEYWORDS.search(text))
        if prev_text_hash is None:
            extra["last_hash"] = text_hash  # baseline
        else:
            if contains_keywords and text_hash != prev_text_hash:
                preview = text[:200] + ("…" if len(text) > 200 else "")
                with transaction.atomic():
                    Notification.objects.create(
                        user=source.user,
                        source=source,
                        title=f"Activity on {source.name}",
                        message=preview if preview.strip() else "Page changed",
                        link=source.check_url,
                        detected_at=timezone.now(),
                        seen=False,
                        played=False,
                        meta={"detector": "text-hash", "keywords": True},
                    )
                extra["last_hash"] = text_hash
                created = True
            else:
                # quiet re-baseline (no keywords or no real change)
                extra["last_hash"] = text_hash

    # ---------- persist updated baseline (fingerprint + mode) ----------
    extra = _store_fingerprint(extra, etag, last_mod, body_hash)
    extra["mode"] = cur_mode
    _save_extra(source, extra)

    return created