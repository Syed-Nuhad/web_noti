# webnotify/tasks.py
import hashlib
import logging
import re
from typing import Dict, Tuple, Optional
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
    body_hash = hashlib.sha256(resp.content or b"").hexdigest()
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
def _build_session(headers: Dict, cookies: Dict, timeout_connect=8, timeout_read=15) -> requests.Session:
    """
    Build a requests session with sane retries and connect/read timeouts.
    """
    sess = requests.Session()
    sess.headers.update(headers)
    if cookies:
        sess.cookies.update(cookies)

    retry = Retry(
        total=2,                # two retries on transient failures
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "OPTIONS"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    # we’ll pass the (connect, read) timeout tuple to .get(...) where we call it
    sess._wn_timeout = (timeout_connect, timeout_read)  # attach for convenience
    return sess


# -------------------------- main task -----------------------------

@shared_task
def check_source(source_id: int) -> bool:
    """
    Real checker (no CSS selector needed):
      1) Fetch page with stored cookies.
      2) Baseline on first run (fingerprint + counts, no notify).
      3) On later runs, if unread-count increases → notify.
         Else if ETag/Last-Modified/body hash changed → generic notify.
    Returns:
      True  = a new Notification was created
      False = no new Notification
    """
    try:
        source = NotificationSource.objects.select_related("user").get(pk=source_id, enabled=True)
    except NotificationSource.DoesNotExist:
        return False

    extra = _get_extra(source)
    cookies = _build_cookies(extra)
    headers = _build_headers(extra)
    timeout = int(extra.get("timeout", 25))  # internal; not shown to users

    # ---- fetch
    # ---- fetch (with retries + separate connect/read timeouts)
    try:
        # allow per-source internal override (not exposed to users)
        tout = extra.get("timeout")
        if isinstance(tout, (int, float)):
            connect_t, read_t = max(2, int(tout) // 2), max(5, int(tout))
        else:
            connect_t, read_t = 8, 15

        sess = _build_session(headers, cookies, timeout_connect=connect_t, timeout_read=read_t)
        r = sess.get(
            source.check_url,
            timeout=sess._wn_timeout,  # (connect, read)
            allow_redirects=True,
        )
        r.raise_for_status()
        html_bytes = r.content or b""
        html_text = r.text

    except Exception as e:
        logger.warning("Fetch failed for %s: %s", source.check_url, e)
        source.last_checked = timezone.now()
        source.save(update_fields=["last_checked"])
        return False

    # ---- fingerprint (HTTP + body)
    etag, last_mod, body_hash = _fingerprint_response(r)
    prev_etag, prev_last, prev_hash = _load_previous_fingerprint(extra)

    # ---- parse counts (HTML)
    soup = BeautifulSoup(html_text, "html.parser")
    text = _visible_text(soup)
    prev_count = extra.get("last_count")
    parsed_count = (
        _extract_count_from_title(soup)
        or _extract_count_from_aria_or_badges(soup)
        or _extract_count_from_text(text)
    )
    text_hash = _hash_text(text)

    # ---- first run: baseline everything, no notify
    if not (prev_etag or prev_last or prev_hash or prev_count is not None):
        extra = _store_fingerprint(extra, etag, last_mod, body_hash)
        if parsed_count is not None:
            extra["last_count"] = int(parsed_count)
        else:
            extra["last_hash"] = text_hash
        _save_extra(source, extra)
        return False

    created = False
    reasons = []

    # ---- preferred: unread count increase
    if parsed_count is not None:
        if prev_count is None:
            # baseline count
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
                # update count anyway
                extra["last_count"] = int(parsed_count)

    # ---- fallback: content changed (only if count didn’t already create)
    if not created:
        changed = False
        if etag and etag != prev_etag:
            changed = True
            reasons.append("etag changed")
        if last_mod and last_mod != prev_last:
            changed = True
            reasons.append("last-modified changed")
        if body_hash != prev_hash:
            changed = True
            reasons.append("content changed")
        # also compare visible text hash when we don't have counts
        if parsed_count is None:
            prev_text_hash = extra.get("last_hash")
            if prev_text_hash is None:
                extra["last_hash"] = text_hash
            elif text_hash != prev_text_hash:
                changed = True
                reasons.append("visible text changed")
                extra["last_hash"] = text_hash

        if changed:
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
                    meta={"detector": "fingerprint", "reasons": reasons},
                )
            created = True

    # ---- persist new baseline
    extra = _store_fingerprint(extra, etag, last_mod, body_hash)
    _save_extra(source, extra)

    return created
