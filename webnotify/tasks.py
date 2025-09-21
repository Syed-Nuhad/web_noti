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

def _fetch_rendered_html(
    url: str,
    cookies: dict,
    user_data_dir: str = None,
    wait_ms: int = 3000,
    click_selector: str = None,     # NEW
    wait_selector: str = None,      # NEW
    scroll_down: int = 0,           # NEW
    **_ignore,                      # swallow unexpected kwargs
) -> str:
    if not isinstance(url, str):
        try:
            url = str(url)
        except Exception:
            logger.warning("Rendered fetch got non-string URL; aborting.")
            return ""

    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except Exception as e:
        logger.warning("Playwright not available: %s", e)
        return ""

    if not user_data_dir:
        base = os.path.dirname(os.path.dirname(__file__))
        user_data_dir = os.path.join(base, "desktop_client", ".pw_profile")

    html = ""
    with sync_playwright() as pw:
        ctx = None
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

        # seed cookies (profile usually already has them)
        if cookies:
            try:
                ck = [{"name": k, "value": v, "path": "/", "httpOnly": False, "secure": True} for k, v in cookies.items()]
                if ck:
                    ctx.add_cookies(ck)
            except Exception:
                pass

        def open_and_render(target_url: str) -> str:
            page = ctx.new_page()
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(wait_ms)

                # optional: scroll to load lazy content
                for _ in range(int(scroll_down or 0)):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)

                # optional: click something (e.g., a bell)
                if click_selector:
                    try:
                        page.click(click_selector, timeout=8000)
                        page.wait_for_timeout(1500)
                    except PWError:
                        pass

                # optional: wait for a specific element to appear
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=12000, state="visible")
                        page.wait_for_timeout(800)
                    except PWError:
                        pass

                if page.is_closed():
                    return ""
                return page.content()
            except PWError as e:
                logger.warning("Rendered fetch failed for %s: %s", target_url, e)
                return ""
            finally:
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:
                    pass

        html = open_and_render(url)

        # YouTube mobile fallback (kept generic but harmless)
        if (not html or len(html) < 3000) and ("youtube.com" in url):
            alt = "https://m.youtube.com/?persist_app=1&app=m"
            alt_html = open_and_render(alt)
            if alt_html and len(alt_html) > len(html):
                html = alt_html

        try:
            ctx.close()
        except Exception:
            pass

    return html


def _extract_item_keys(soup: BeautifulSoup) -> list[str]:
    """
    Build stable 'keys' for items we consider notifications/messages.
    We prefer anchors inside list-like structures. Key = href or href+text.
    This stays generic across sites.
    """
    keys = []

    # Common list structures first (keeps noise down)
    containers = soup.select('[role="list"], [role="listbox"], ul, ol, .list, .notifications, .inbox, .menu')
    if not containers:
        containers = [soup]  # fall back to whole doc

    seen = set()
    for cont in containers:
        for a in cont.select('a[href]'):
            href = a.get('href', '').strip()
            if not href:
                continue
            # normalize absolute-ish key; YouTube/others often use long query strings
            text = a.get_text(" ", strip=True)[:120]
            key = href
            if len(text) >= 8:  # add text to reduce collisions when hrefs are generic
                key = f"{href} :: {text}"
            if key not in seen:
                seen.add(key)
                keys.append(key)

        # Also look for obvious “item” blocks with text (no href)
        for item in cont.select('[role="listitem"], li, .notification, .inbox-item, .message'):
            t = item.get_text(" ", strip=True)
            if t and len(t) >= 12:
                key = f"TXT::{t[:160]}"
                if key not in seen:
                    seen.add(key)
                    keys.append(key)

    return keys[:500]  # cap


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

    # Fast path (requests)
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
            # Optional profile override from extra_config
            user_data_dir = extra.get("user_data_dir")

            globals()["_wn_wait_selector_tmp"] = (extra.get("wait_selector") or None)
            globals()["_wn_scroll_down_tmp"] = int(extra.get("scroll_down", 0) or 0)

            rendered = _fetch_rendered_html(
                source.check_url,
                cookies=cookies,
                user_data_dir=extra.get("user_data_dir"),
                wait_ms=int(extra.get("render_timeout_ms", 15000)),
                click_selector=extra.get("click_selector"),  # NEW
                wait_selector=extra.get("wait_selector"),  # NEW
                scroll_down=int(extra.get("scroll_down", 0)),  # NEW
            )

            # clean up
            globals().pop("_wn_wait_selector_tmp", None)
            globals().pop("_wn_scroll_down_tmp", None)


            if rendered:
                html_text = rendered

                class _FakeResp:
                    headers = {}
                    def __init__(self, body: str):
                        self.content = body.encode("utf-8", "ignore")

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
    inner_text = globals().pop("_wn_last_inner_text", None)

    item_keys = _extract_item_keys(soup)
    prev_seen_keys = set(extra.get("seen_keys", []))

    new_keys = []
    if item_keys:
        if not prev_seen_keys:
            # first baseline: remember what we currently see, do NOT notify
            extra["seen_keys"] = item_keys
        else:
            # compare
            cur_set = set(item_keys)
            new_keys = [k for k in item_keys if k not in prev_seen_keys]


    if use_rendered and inner_text:
        text = inner_text
    else:
        text = _visible_text(soup)

    prev_count   = extra.get("last_count")
    parsed_count = (
        _extract_count_from_title(soup)
        or _extract_count_from_aria_or_badges(soup)
        or _extract_count_from_text(text)
    )
    text_hash = _hash_text(text)

    # --- optional debug logging + HTML dump
    debug = bool(extra.get("debug", False))
    if debug:
        # Log with user email
        logger.warning(
            "DEBUG user=%s src=%s name=%s mode=%s parsed_count=%s text_len=%s keywords=%s url=%s",
            getattr(source.user, "email", None),
            source.id,
            source.name,
            cur_mode,
            parsed_count,
            len(text),
            bool(KEYWORDS.search(text)),
            source.check_url,
        )
        # Dump HTML to MEDIA_ROOT/debug/source_<id>.html (or ./media/debug if MEDIA_ROOT unset)
        try:
            from django.conf import settings
            base_out = getattr(settings, "MEDIA_ROOT", None) or os.path.join(os.path.dirname(os.path.dirname(__file__)), "media")
            out_dir = os.path.join(base_out, "debug")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"source_{source.id}.html")
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(html_text)
            logger.warning("DEBUG wrote HTML dump to: %s", out_path)
        except Exception as _e:
            logger.warning("DEBUG failed to write HTML dump: %s", _e)

    # ---------- baseline if first run OR fetch mode changed ----------
    # Prevents false "new" alerts when switching to rendered mode or first baseline
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
    created = False

    # 1) PRIMARY: brand new items appeared
    if item_keys and new_keys:
        preview_lines = []
        for k in new_keys[:3]:
            # make a readable line (strip href if present)
            preview_lines.append(k.split(" :: ", 1)[-1][:100])
        preview = "\n".join(preview_lines)

        Notification.objects.create(
            user=source.user,
            source=source,
            title=f"New activity on {source.name}",
            message=preview or f"{len(new_keys)} new item(s)",
            link=source.check_url,
            detected_at=timezone.now(),
            seen=False, played=False,
            meta={"detector": "new-keys", "new_count": len(new_keys)},
        )
        created = True

        # roll forward the baseline (keep it bounded)
        extra["seen_keys"] = (list(prev_seen_keys | set(new_keys)))[:500]

    # 2) SECONDARY: unread count increased (only if keys didn’t already trigger)
    elif parsed_count is not None:
        prev_count = extra.get("last_count")
        if prev_count is None:
            extra["last_count"] = int(parsed_count)
        else:
            if int(parsed_count) > int(prev_count):
                Notification.objects.create(
                    user=source.user,
                    source=source,
                    title=f"New messages on {source.name}",
                    message=f"Unread count: {parsed_count}",
                    detected_at=timezone.now(),
                    seen=False, played=False,
                    meta={"detector": "count", "prev": int(prev_count), "now": int(parsed_count)},
                )
                created = True
            # always roll forward baseline
            extra["last_count"] = int(parsed_count)

    # 3) TERTIARY: keyworded visible-text change (kept as a quiet fallback)
    if not created and parsed_count is None:
        prev_text_hash = extra.get("last_hash")
        contains_keywords = bool(KEYWORDS.search(text))
        text_hash = _hash_text(text)
        if prev_text_hash is None:
            extra["last_hash"] = text_hash
        else:
            if contains_keywords and text_hash != prev_text_hash:
                preview = text[:200] + ("…" if len(text) > 200 else "")
                Notification.objects.create(
                    user=source.user,
                    source=source,
                    title=f"Activity on {source.name}",
                    message=preview if preview.strip() else "Page changed",
                    link=source.check_url,
                    detected_at=timezone.now(),
                    seen=False, played=False,
                    meta={"detector": "text-hash", "keywords": True},
                )
                extra["last_hash"] = text_hash
                created = True
            else:
                extra["last_hash"] = text_hash



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
