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


def _fingerprint_response(resp) -> Tuple[str, str, str]:
    """Return (etag, last_modified, sha256(body)). Empty strings if missing."""
    headers = getattr(resp, "headers", {}) or {}
    etag = headers.get("ETag", "") or ""
    last_mod = headers.get("Last-Modified", "") or ""
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


def _gmail_unread_count(soup: BeautifulSoup) -> Optional[int]:
    """
    Count Gmail unread rows (modern Gmail marks unread rows with class zA zE).
    This function tries several heuristics and returns an integer or None.
    """
    try:
        # modern Gmail (web UI): rows with classes 'zA zE' indicate unread
        nodes = soup.select("tr.zA.zE, .zA.zE, .zA.zE *")
        if nodes:
            # prefer counting unique row containers (some selectors return child nodes)
            rows = soup.select("tr.zA.zE")
            if rows:
                return len(rows)
            # fallback: count unique elements matching .zA.zE
            return len({elem for elem in nodes})

        # class 'unread' or 'bsu' variants
        nodes = soup.select(".unread, .unread-count, .bsu")
        if nodes:
            return len(nodes)

        # try title like "(3) Inbox"
        try:
            title = (soup.title.string or "").strip()
        except Exception:
            title = ""
        m = re.search(r"^\s*\((\d{1,4})\)\s*Inbox", title)
        if m:
            return int(m.group(1))

        # fallback: scan visible text for "Inbox (N)" or "Unread N"
        text = _visible_text(soup)
        m = re.search(r"Inbox\s*\(?(\d{1,4})\)?", text, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"Unread[:\s]*?(\d{1,4})", text, re.I)
        if m:
            return int(m.group(1))
    except Exception:
        # don't let Gmail-specific errors break whole task
        logger.exception("Error in _gmail_unread_count")
    return None


def _extract_count_from_title(soup: BeautifulSoup) -> Optional[int]:
    # e.g. "(3) Inbox - Example"
    try:
        title = (soup.title.string or "").strip()
    except Exception:
        title = ""
    m = re.search(r"\((\d{1,3})\)", title)
    return int(m.group(1)) if m else None


def _extract_count_from_aria_or_badges(soup: BeautifulSoup) -> Optional[int]:
    """
    Aggressive heuristic: try to find unread-count badges / numbers near inbox links.
    Returns integer count or None.
    Logs candidate matches (logger.debug) to help tuning.
    """
    candidates = []

    try:
        # 1) Title like "(3) Inbox - " or "3 new"
        try:
            title = (soup.title.string or "").strip()
        except Exception:
            title = ""
        if title:
            m = re.search(r"^\s*\((\d{1,5})\)", title) or re.search(
                r"\b(\d{1,5})\s+(?:unread|new)\b", title, re.I
            )
            if m:
                val = int(m.group(1))
                logger.debug("count-candidate: title -> %s", val)
                return val
            if title:
                candidates.append(("title", title[:200]))

        # 2) scan attributes that often carry badges / tooltips
        attr_names = ("aria-label", "title", "data-tooltip", "alt", "data-count", "data-unread")
        for attr in attr_names:
            for el in soup.find_all(attrs={attr: True}):
                valstr = str(el.get(attr) or "").strip()
                if not valstr:
                    continue
                if any(k in valstr.lower() for k in ("inbox", "unread", "new", "notifications", "notification")):
                    m = re.search(r"\b(\d{1,5})\b", valstr)
                    if m:
                        num = int(m.group(1))
                        logger.debug("count-candidate: %s attr -> %s", attr, num)
                        return num
                    candidates.append((f"attr:{attr}", valstr[:200]))

        # 3) anchors / buttons linking to inbox/mail; look at their text and siblings
        anchors = list(soup.find_all(["a", "button"], href=True))
        anchors += [b for b in soup.find_all("button") if b not in anchors]
        for a in anchors:
            href = (a.get("href") or "").lower()
            text = a.get_text(" ", strip=True) or ""
            if any(x in href for x in ("#inbox", "/inbox", "/mail", "mail.google.com", "notifications", "/feed/notifications")) or "inbox" in text.lower() or "mail" in href:
                m = re.search(r"\b(\d{1,5})\b", text)
                if m:
                    num = int(m.group(1))
                    logger.debug("count-candidate: anchor text -> %s (href=%s)", num, href[:120])
                    return num
                try:
                    sib_texts = []
                    for ch in a.find_all(recursive=False):
                        sib_txt = ch.get_text(" ", strip=True)
                        if sib_txt:
                            sib_texts.append(sib_txt)
                    p = a.parent
                    if p:
                        for sib in p.find_all(recursive=False):
                            if sib is a:
                                continue
                            t = sib.get_text(" ", strip=True)
                            if t:
                                sib_texts.append(t)
                    for s in sib_texts:
                        m = re.search(r"\b(\d{1,5})\b", s)
                        if m:
                            num = int(m.group(1))
                            logger.debug("count-candidate: anchor sibling -> %s (href=%s)", num, href[:120])
                            return num
                except Exception:
                    pass
                if text:
                    candidates.append(("anchor", text[:200]))

        # 4) class-name heuristic: look for elements whose class contains "badge", "count", "unread", "bsU"
        for el in soup.find_all(class_=True):
            cls = " ".join(el.get("class") or [])
            low = cls.lower()
            if any(tok in low for tok in ("badge", "count", "unread", "unread-count", "bsu", "bsu-")):
                txt = el.get_text(" ", strip=True)
                if not txt:
                    for span in el.find_all(["span", "b"]):
                        t = span.get_text(" ", strip=True)
                        if t:
                            txt = t
                            break
                if txt:
                    m = re.search(r"\b(\d{1,5})\b", txt)
                    if m:
                        num = int(m.group(1))
                        logger.debug("count-candidate: class(%s) -> %s", cls, num)
                        return num
                    candidates.append((f"class:{cls[:100]}", txt[:200]))

        # 5) final pass: find lines in visible text that mention Inbox/Unread/Notifications near a number
        visible = _visible_text(soup)
        if visible:
            for line in visible.splitlines():
                L = line.strip()
                if not L:
                    continue
                low = L.lower()
                if any(k in low for k in ("inbox", "unread", "notification", "notifications", "new")):
                    m = re.search(r"\b(\d{1,5})\b", L)
                    if m:
                        num = int(m.group(1))
                        logger.debug("count-candidate: visible-line -> %s", num)
                        return num
                    candidates.append(("visible-line", L[:200]))

    except Exception as ex:
        logger.exception("Error in badge heuristic: %s", ex)

    if candidates:
        logger.debug("badge-candidates found: %s", candidates[:10])
    else:
        logger.debug("badge-candidates: none")

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


# Replace your existing _fetch_rendered_html with this function

def _fetch_rendered_html(
    url: str,
    cookies: dict,
    user_data_dir: str = None,
    wait_ms: int = 3000,
    click_selector: str = None,
    wait_selector: str = None,
    scroll_down: int = 0,
    headless: Optional[bool] = None,
    **_ignore,
) -> str:
    """
    Playwright-rendered HTML fetch.
    - headless=None => choose default, but caller can force headful/headless via extra_config.
    - wait_selector: CSS selector to wait for (useful for Gmail: 'tr.zA' or 'tr.zA.zE')
    - user_data_dir: persistent profile path (must match the profile used by link_source)
    """
    if not isinstance(url, str):
        try:
            url = str(url)
        except Exception:
            logger.warning("Rendered fetch got non-string URL; aborting.")
            return ""

    try:
        from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout
    except Exception as e:
        logger.warning("Playwright not available: %s", e)
        return ""

    # default profile path if not provided
    if not user_data_dir:
        base = os.path.dirname(os.path.dirname(__file__))
        user_data_dir = os.path.join(base, "desktop_client", ".pw_profile")

    # choose headless: if explicit arg provided, obey it; else default True except for gmail fallback
    auto_headless = True
    if headless is not None:
        auto_headless = bool(headless)
    else:
        # prefer headful for Gmail because Google often serves limited basic HTML to headless browsers
        if "mail.google.com" in url:
            auto_headless = False

    html = ""
    with sync_playwright() as pw:
        ctx = None
        for channel in ("chrome", "msedge", None):
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=auto_headless,
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
                    viewport={"width": 1366, "height": 900},
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
                # set a desktop UA so Gmail gives full UI
                try:
                    page.set_extra_http_headers({"User-Agent": DEFAULT_HEADERS["User-Agent"]})
                except Exception:
                    pass

                page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)

                # If a concrete selector is given, wait for it. This helps Gmail.
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=max(12000, wait_ms), state="visible")
                        # small additional wait to let JS finish populating rows
                        page.wait_for_timeout(800)
                    except PWTimeout:
                        # continue even if selector didn't appear
                        pass

                # wait the requested ms to give client JS time to render extra bits
                page.wait_for_timeout(wait_ms)

                # optional lazy scrolls
                for _ in range(int(scroll_down or 0)):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)

                # optional click
                if click_selector:
                    try:
                        page.click(click_selector, timeout=8000)
                        page.wait_for_timeout(1200)
                    except Exception:
                        pass

                if page.is_closed():
                    return ""
                return page.content()
            except Exception as e:
                logger.warning("Rendered fetch failed for %s: %s", target_url, e)
                return ""
            finally:
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:
                    pass

        # first try requested URL
        html = open_and_render(url)

        # gmail fallback: sometimes the root / redirects; try safe inbox/mobile variants
        if (not html or len(html) < 2000) and ("mail.google.com" in url):
            # prefer the basic inbox path (but still headful) or mobile
            for alt in ("https://mail.google.com/mail/u/0/#inbox", "https://mail.google.com/"):
                alt_html = open_and_render(alt)
                if alt_html and len(alt_html) > len(html):
                    html = alt_html

        try:
            ctx.close()
        except Exception:
            pass

    return html


def _extract_item_keys(soup: BeautifulSoup) -> list:
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
    Checker:
      1) Fetch page (requests by default; Playwright if extra_config.rendered == True).
      2) Baseline on first run OR whenever fetch mode changes (no notify).
      3) Notify only when:
           - unread COUNT increases, OR
           - (fallback) visible text containing inbox/message keywords changed.
      4) Uses conditional GET (If-None-Match / If-Modified-Since) for speed.
    Returns:
      True  = a new Notification was created
      False = no new Notification (or baseline/update only)
    """
    # ---------- load source ----------
    try:
        source = NotificationSource.objects.select_related("user").get(pk=source_id, enabled=True)
    except NotificationSource.DoesNotExist:
        return False

    extra = _get_extra(source)                # dict
    use_rendered = bool(extra.get("rendered", False))
    prev_mode = extra.get("mode") or "requests"   # previous fetch mode recorded in baseline
    cur_mode = "rendered" if use_rendered else "requests"

    cookies = _build_cookies(extra)
    headers = _build_headers(extra)

    # previously stored fingerprint (for conditional GET)
    prev_etag, prev_last, prev_hash = _load_previous_fingerprint(extra)

    # ---------- fetch HTML (requests first; rendered if flagged or failed) ----------
    html_text: Optional[str] = None
    resp_for_fp = None

    # Build conditional headers so unchanged pages come back as 304 quickly
    cond_headers = {}
    if prev_etag:
        cond_headers["If-None-Match"] = prev_etag
    if prev_last:
        cond_headers["If-Modified-Since"] = prev_last

    # Fast path (requests)
    try:
        tout = extra.get("timeout")
        if isinstance(tout, (int, float)):
            connect_t, read_t = max(2, int(tout) // 2), max(5, int(tout))
        else:
            connect_t, read_t = 5, 8

        # Nudge servers to close promptly; avoids stuck keep-alives
        req_headers = {**headers, **cond_headers, "Connection": "close"}

        sess = _build_session(headers=req_headers, cookies=cookies,
                              timeout_connect=connect_t, timeout_read=read_t)
        r = sess.get(
            source.check_url,
            timeout=(connect_t, read_t),
            allow_redirects=True,
        )

        # Short-circuit: 304 Not Modified => nothing changed
        if getattr(r, "status_code", None) == 304:
            source.last_checked = timezone.now()
            source.save(update_fields=["last_checked"])
            return False

        r.raise_for_status()
        html_text = r.text
        resp_for_fp = r
    except Exception as e:
        logger.warning("Fetch failed for %s: %s", source.check_url, e)

    # Rendered fallback (only if enabled or requests failed)
    # Rendered fallback (only if enabled or requests failed)
    if use_rendered or html_text is None:
        try:
            user_data_dir = extra.get("user_data_dir")  # optional override

            # Two-stage render: a short snapshot to catch transient badges,
            # and a longer snapshot for the 'stable' DOM.
            short_ms = int(extra.get("short_render_ms", 400))  # capture early transient badges
            long_ms = int(extra.get("render_timeout_ms", 3000))  # stable render

            short_html = _fetch_rendered_html(
                source.check_url,
                cookies=cookies,
                user_data_dir=user_data_dir,
                wait_ms=short_ms,
            ) or ""

            long_html = _fetch_rendered_html(
                source.check_url,
                cookies=cookies,
                user_data_dir=user_data_dir,
                wait_ms=long_ms,
            ) or ""

            # prefer long_html for the saved HTML_text (stable), but we will
            # parse both to pick the highest/unread count seen.
            html_text = long_html or short_html or html_text

            # Build a fake response for fingerprinting (rendered mode)
            if html_text:
                class _FakeResp:
                    headers = {}

                    def __init__(self, body: str): self.content = body.encode("utf-8", "ignore")

                resp_for_fp = _FakeResp(html_text)

            # Parse both snapshots and choose the highest parsed_count (to catch transient badge)
            parsed_count_candidates = []
            for h in (short_html, long_html):
                if not h:
                    continue
                tmp_soup = BeautifulSoup(h, "html.parser")
                tmp_text = _visible_text(tmp_soup)
                tmp_count = (
                        _extract_count_from_title(tmp_soup)
                        or _extract_count_from_aria_or_badges(tmp_soup)
                        or _extract_count_from_text(tmp_text)
                )
                if tmp_count is not None:
                    parsed_count_candidates.append(int(tmp_count))

            # if we found candidates, pick the max as parsed_count (otherwise leave parsed_count as None,
            # it will be computed later from html_text again)
            if parsed_count_candidates:
                # we intentionally choose the maximum to avoid missing a transient unread badge
                parsed_count = max(parsed_count_candidates)
        except Exception as e:
            logger.warning("Rendered fetch failed for %s: %s", source.check_url, e)

    if html_text is None or resp_for_fp is None:
        source.last_checked = timezone.now()
        source.save(update_fields=["last_checked"])
        return False

    # ---------- fingerprint + parse ----------
    etag, last_mod, body_hash = _fingerprint_response(resp_for_fp)
    soup = BeautifulSoup(html_text, "html.parser")
    text = _visible_text(soup)

    prev_count = extra.get("last_count")

    # First try Gmail-specific detector, then fallbacks
    parsed_count = (
        _gmail_unread_count(soup)
        or _extract_count_from_title(soup)
        or _extract_count_from_aria_or_badges(soup)
        or _extract_count_from_text(text)
    )
    if parsed_count is None and "mail.google.com" in (source.check_url or ""):
        try:
            unread_rows = soup.select("tr.zA.zE")
            if unread_rows:
                parsed_count = len(unread_rows)
            else:
                # try mobile/basic variants
                unread_spans = soup.select('[aria-label*="unread"], .zF, .yP')  # some Gmail label classes
                if unread_spans:
                    parsed_count = len(unread_spans)
        except Exception:
            logger.exception("Gmail parse error")

    text_hash = _hash_text(text)

    # DEBUG (optional)
    if bool(extra.get("debug", False)):
        logger.warning(
            "DEBUG user=%s src=%s name=%s mode=%s parsed_count=%s text_len=%s keywords=%s url=%s",
            getattr(source.user, "email", None),
            source.id, source.name, cur_mode, parsed_count, len(text),
            bool(KEYWORDS.search(text)), source.check_url
        )

    # ---------- baseline if first run OR fetch mode changed ----------
    first_baseline = (prev_etag, prev_last, prev_hash, prev_count) == ("", "", "", None)
    if first_baseline or (prev_mode != cur_mode):
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
                extra["last_count"] = int(parsed_count)

    # ---------- fallback: only notify if KEYWORD text changed ----------
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
                extra["last_hash"] = text_hash

    # ---------- persist updated baseline (fingerprint + mode) ----------
    extra = _store_fingerprint(extra, etag, last_mod, body_hash)
    extra["mode"] = cur_mode
    _save_extra(source, extra)

    return created
