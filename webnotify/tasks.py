import hashlib
import logging
import re
import requests
from bs4 import BeautifulSoup
from django.utils import timezone
from celery import shared_task
from .models import NotificationSource, Notification

logger = logging.getLogger(__name__)

KEYWORDS = re.compile(r"\b(inbox|message|messages|notification|notifications|alert|alerts)\b", re.I)

def _extract_count_from_title(soup):
    # e.g. "(3) Inbox - Example"
    try:
        title = (soup.title.string or "").strip()
    except Exception:
        title = ""
    m = re.search(r"\((\d{1,3})\)", title)
    return int(m.group(1)) if m else None

def _extract_count_from_text(soup_text):
    # lines like "Inbox (4)" or "Notifications 7"
    best = None
    for line in soup_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not KEYWORDS.search(line):
            continue
        m = re.search(r"\b(\d{1,3})\b", line)
        if m:
            val = int(m.group(1))
            best = val if best is None else max(best, val)
    return best

def _extract_count_from_aria_or_badges(soup):
    # look for common badge/aria-label patterns
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

def _visible_text(soup):
    # simple visible text joiner
    for script in soup(["script", "style", "noscript", "template"]):
        script.decompose()
    return soup.get_text("\n", strip=True)

def _hash_text(txt):
    return hashlib.sha256(txt.encode("utf-8", "ignore")).hexdigest()

@shared_task
def check_source(source_id: int) -> bool:
    """
    Auto-detects unread counts or page changes. No CSS selector required.
    Persists last_count / last_hash in extra_config automatically.
    """
    try:
        source = NotificationSource.objects.get(pk=source_id, enabled=True)
    except NotificationSource.DoesNotExist:
        return False

    extra = source.extra_config or {}
    last_count = extra.get("last_count")
    last_hash  = extra.get("last_hash")
    timeout    = int(extra.get("timeout", 20))  # internal, user never sets this
    headers    = (extra.get("headers") or {})   # may be filled later by cookie import
    cookies    = (extra.get("cookies") or {})

    # Always have a UA
    base_headers = {"User-Agent": "WebNotify/1.0 (+desktop-client)"}
    base_headers.update(headers)

    # --- fetch
    try:
        session = requests.Session()
        session.headers.update(base_headers)
        if cookies:
            session.cookies.update(cookies)
        resp = session.get(source.check_url, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning("Fetch failed for %s: %s", source.check_url, e)
        source.last_checked = timezone.now()
        source.save(update_fields=["last_checked"])
        return False

    soup = BeautifulSoup(html, "html.parser")
    text = _visible_text(soup)

    # --- primary: counts
    count = (_extract_count_from_title(soup) or
             _extract_count_from_aria_or_badges(soup) or
             _extract_count_from_text(text))

    created = False

    if count is not None:
        # first time: just store it
        if last_count is None:
            extra["last_count"] = count
        else:
            # only notify if it increased
            if count > int(last_count):
                Notification.objects.create(
                    user=source.user,
                    source=source,
                    title=f"New messages on {source.name}",
                    message=f"Unread count: {count}",
                    detected_at=timezone.now(),
                    seen=False, played=False,
                    meta={"detector": "count", "prev": last_count, "now": count},
                )
                extra["last_count"] = count
                created = True
            else:
                extra["last_count"] = count
    else:
        # fallback: whole-text hash compare
        h = _hash_text(text)
        if last_hash is None:
            extra["last_hash"] = h
        elif h != last_hash:
            preview = text[:200] + ("…" if len(text) > 200 else "")
            Notification.objects.create(
                user=source.user,
                source=source,
                title=f"Activity on {source.name}",
                message=preview,
                detected_at=timezone.now(),
                seen=False, played=False,
                meta={"detector": "hash"},
            )
            extra["last_hash"] = h
            created = True

    source.extra_config = extra
    source.last_checked = timezone.now()
    source.save(update_fields=["extra_config", "last_checked"])

    return created