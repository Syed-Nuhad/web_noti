# webnotify/views.py
import json
import os
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.contrib.auth import login as auth_login, logout as auth_logout, authenticate, get_user_model
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_http_methods, require_GET
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.utils import timezone
from django.conf import settings

from .models import Notification, NotificationSource, CustomRingtone, UserSettings, NotificationSound

User = get_user_model()


# ---------- Simple pages / auth (no templates required for migrations) ----------


@login_required
def dashboard(request):
    """
    Render dashboard with a couple of flags so the template can
    hide the upload form if a default ringtone already exists.
    """
    try:
        settings_obj, _ = UserSettings.objects.get_or_create(user=request.user)
    except Exception:
        settings_obj = None

    has_default = bool(getattr(settings_obj, "default_ringtone", None))
    default_name = getattr(getattr(settings_obj, "default_ringtone", None), "name", None)

    ctx = {
        "user": request.user,
        "has_default_ringtone": has_default,
        "default_ringtone_name": default_name or "",
    }
    try:
        return render(request, "webnotify/dashboard.html", ctx)
    except Exception:
        # fallback minimal response for environments without templates
        from .models import Notification
        unread_count = Notification.objects.filter(user=request.user, seen=False).count()
        html = f"<html><body><h1>Dashboard</h1><p>Hello, {request.user} — unread: {unread_count}</p></body></html>"
        return HttpResponse(html)


@require_http_methods(["GET", "POST"])
def RegisterView(request):
    """
    Simple registration view. Expects POST with 'email' and 'password'.
    If your custom user model requires other fields, adjust here.
    """
    if request.method == "GET":
        try:
            return render(request, "webnotify/register.html")
        except Exception:
            return HttpResponse("POST email & password to register.", status=200)

    # POST: create user
    email = request.POST.get("email") or request.POST.get("username")
    password = request.POST.get("password")
    if not email or not password:
        return HttpResponseBadRequest("Missing email or password.")

    # Create user - use create_user to respect custom User model
    try:
        user = User.objects.create_user(email=email, password=password)
    except TypeError:
        # fallback: try username field as well
        user = User.objects.create_user(username=email, email=email, password=password)
    # create default settings for the user
    UserSettings.objects.get_or_create(user=user)
    auth_login(request, user)
    return redirect("webnotify:dashboard")


@require_http_methods(["GET", "POST"])
def LoginView(request):
    """
    Simple login view. POST with 'email' and 'password' (or username/password).
    """
    if request.method == "GET":
        try:
            return render(request, "webnotify/login.html")
        except Exception:
            return HttpResponse("POST email & password to login.", status=200)

    username = request.POST.get("email") or request.POST.get("username")
    password = request.POST.get("password")
    if not username or not password:
        return HttpResponseBadRequest("Missing credentials.")

    # Try authenticate. Depending on custom user model, authenticate may accept 'email' or 'username'.
    user = authenticate(request, username=username, password=password)
    if user is None:
        # try using email explicitly (some setups use email backend)
        user = authenticate(request, email=username, password=password)

    if user is None:
        return HttpResponseForbidden("Invalid credentials.")
    auth_login(request, user)
    return redirect("webnotify:dashboard")


@login_required
def LogoutView(request):
    auth_logout(request)
    return redirect("webnotify:login")


# ---------- API endpoints (JSON) ----------

@login_required
def active_notification(request):
    """
    Return the latest *unplayed* notification for the current user as JSON.
    Once the client starts playing it, it should mark it as played to avoid repeats.
    """
    notif = (
        Notification.objects
        .filter(user=request.user, played=False)   # only unplayed
        .order_by("-detected_at")
        .first()
    )
    if not notif:
        return JsonResponse({"has": False})

    data = {
        "has": True,
        "id": notif.pk,
        "title": notif.title,
        "message": notif.message,
        "link": notif.link,
        "detected_at": notif.detected_at.isoformat(),
        "source": notif.source.name if notif.source else None,
    }
    return JsonResponse(data)


@require_POST
@login_required
def mark_notifications_read(request):
    """
    Mark notification(s) as seen/played.
    Accepts JSON body like: {"ids": [1,2], "played": true}
    Or form-encoded 'ids' comma-separated.
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = {}

    ids = body.get("ids")
    if not ids:
        ids_raw = request.POST.get("ids", "")
        if ids_raw:
            ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]

    if not ids:
        return HttpResponseBadRequest("No ids provided.")

    played = body.get("played", True)
    Notification.objects.filter(user=request.user, pk__in=ids).update(seen=True, played=bool(played))
    return JsonResponse({"ok": True, "updated": len(ids)})

@login_required
@require_http_methods(["GET", "POST"])
def source_list_create(request):
    """
    GET: list sources for the current user.
    POST: create a source with ONLY:
          - name       (required)
          - check_url  (required)
          - selector   (optional CSS selector)
          - enabled    (optional bool: 1/true/on/yes, default true)

    No regex, no timeout, no cookies, no headers.
    """
    if request.method == "GET":
        sources = NotificationSource.objects.filter(user=request.user).order_by("-created_at")
        return JsonResponse({
            "sources": [{
                "id": s.pk,
                "name": s.name,
                "check_url": s.check_url,
                "enabled": s.enabled,
                "last_checked": s.last_checked.isoformat() if s.last_checked else None,
                "extra_config": s.extra_config,  # will be {} or {"css_selector": "..."}
            } for s in sources]
        })

    # POST
    # Try JSON body, fall back to form fields
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}

    def _get(k, default=None):
        return data.get(k, request.POST.get(k, default))

    def _coerce_bool(v, default=True):
        if v is None: return default
        if isinstance(v, bool): return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    name      = _get("name")
    check_url = _get("check_url")
    selector  = _get("selector")  # optional CSS selector
    enabled   = _coerce_bool(_get("enabled"), True)

    if not name or not check_url:
        return HttpResponseBadRequest("Missing name or check_url.")

    extra = {"css_selector": selector} if selector else {}

    src = NotificationSource.objects.create(
        user=request.user,
        name=name,
        check_url=check_url,
        enabled=enabled,
        extra_config=extra
    )
    return JsonResponse({"ok": True, "id": src.pk, "extra_config": src.extra_config})




@login_required
@require_http_methods(["POST"])
def upload_ringtone(request):
    """
    Upload a ringtone via multipart form (field 'file'), store on disk (MEDIA_ROOT),
    save DB record, and set as this user's *single* default ringtone.
    Returns a fully-qualified URL clients can load immediately.
    """
    uploaded = request.FILES.get("file")
    if not uploaded:
        return HttpResponseBadRequest("No file uploaded. Use field name 'file'.")

    allowed = {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp3"}
    if uploaded.content_type not in allowed:
        return HttpResponseBadRequest(f"Unsupported content type: {uploaded.content_type}")

    MAX_MB = int(os.environ.get("MAX_RINGTONE_MB", 8))
    if uploaded.size > MAX_MB * 1024 * 1024:
        return HttpResponseBadRequest(f"File too large. Max {MAX_MB} MB allowed.")

    # save file
    subpath = os.path.join("ringtones", timezone.now().strftime("%Y/%m/%d"))
    saved_relpath = default_storage.save(os.path.join(subpath, uploaded.name), ContentFile(uploaded.read()))

    # make this the ONLY default: unset previous defaults for this user
    CustomRingtone.objects.filter(user=request.user, is_default=True).update(is_default=False)

    rt = CustomRingtone.objects.create(
        user=request.user,
        name=uploaded.name,
        file=saved_relpath,
        size_bytes=uploaded.size,
        is_default=True,
    )

    # build URL: prefer storage.url(); if not available, build MEDIA_URL path
    try:
        rel_url = default_storage.url(saved_relpath)
    except Exception:
        rel_url = f"{settings.MEDIA_URL.rstrip('/')}/{saved_relpath}"

    # make absolute URL for convenience on the client
    abs_url = request.build_absolute_uri(rel_url)

    # ensure UserSettings points to this default
    settings_obj, _ = UserSettings.objects.get_or_create(user=request.user)
    settings_obj.default_ringtone = rt
    settings_obj.save(update_fields=["default_ringtone", "last_updated"])

    return JsonResponse({"ok": True, "id": rt.pk, "file": abs_url})

#
# @login_required
# @require_http_methods(["GET", "POST"])
# def user_settings_view(request):
#     settings_obj, _ = UserSettings.objects.get_or_create(user=request.user)
#
#     if request.method == "GET":
#         ringtone_url = None
#         if settings_obj.default_ringtone and settings_obj.default_ringtone.file:
#             try:
#                 rel_url = settings_obj.default_ringtone.file.url
#             except Exception:
#                 rel_url = f"{settings.MEDIA_URL.rstrip('/')}/{settings_obj.default_ringtone.file.name}"
#             ringtone_url = request.build_absolute_uri(rel_url)
#
#         data = {
#             "volume": settings_obj.volume,
#             "play_loop": settings_obj.play_loop,
#             "play_in_background": settings_obj.play_in_background,
#             "default_ringtone_id": settings_obj.default_ringtone.pk if settings_obj.default_ringtone else None,
#             "default_ringtone_url": ringtone_url,
#             "default_ringtone_name": settings_obj.default_ringtone.name if settings_obj.default_ringtone else None,
#         }
#         return JsonResponse({"settings": data})
#
#     # keep your existing POST logic for updating settings
#     # (volume, play_loop, play_in_background, default_ringtone_id)
#     # ...






@login_required
def settings_page(request):
    return render(request, "webnotify/settings.html")

@login_required
def sources_page(request):
    return render(request, "webnotify/sources.html")

@login_required
def notifications_page(request):
    return render(request, "webnotify/notifications.html")





def user_from_apikey(key: str):
    if not key:
        return None
    try:
        from .models import UserSettings
        us = UserSettings.objects.select_related("user").get(api_key=key)
        return us.user
    except Exception:
        return None

def json_bad_request(msg="bad request", code=400):
    return JsonResponse({"ok": False, "error": msg}, status=code)




@csrf_exempt
def settings_by_key(request):
    """
    GET /api/settings_key/?key=APIKEY
    Returns default_ringtone_url, volume, etc, for the user owning the key.
    """
    key = request.GET.get("key") or request.POST.get("key")
    user = user_from_apikey(key)
    if not user:
        return json_bad_request("invalid key", 401)

    from .models import UserSettings
    settings_obj, _ = UserSettings.objects.get_or_create(user=user)
    ringtone_url = None
    if settings_obj.default_ringtone and settings_obj.default_ringtone.file:
        try:
            rel_url = settings_obj.default_ringtone.file.url
        except Exception:
            rel_url = f"{settings.MEDIA_URL.rstrip('/')}/{settings_obj.default_ringtone.file.name}"
        ringtone_url = request.build_absolute_uri(rel_url)

    data = {
        "volume": settings_obj.volume,
        "play_loop": settings_obj.play_loop,
        "default_ringtone_url": ringtone_url,
        "default_ringtone_name": settings_obj.default_ringtone.name if settings_obj.default_ringtone else None,
    }
    return JsonResponse({"ok": True, "settings": data})


@csrf_exempt
def active_notification_by_key(request):
    """
    GET /api/notifications/active_key/?key=APIKEY
    Return latest UNPLAYED notification for that user.
    """
    key = request.GET.get("key") or request.POST.get("key")
    user = user_from_apikey(key)
    if not user:
        return json_bad_request("invalid key", 401)

    notif = (
        Notification.objects
        .filter(user=user, played=False)
        .order_by("-detected_at")
        .first()
    )
    if not notif:
        return JsonResponse({"ok": True, "has": False})

    data = {
        "ok": True,
        "has": True,
        "id": notif.pk,
        "title": notif.title,
        "message": notif.message,
        "link": notif.link,
        "detected_at": notif.detected_at.isoformat(),
        "source": notif.source.name if notif.source else None,
    }
    return JsonResponse(data)


@csrf_exempt
@require_POST
def mark_notifications_read_by_key(request):
    """
    POST /api/notifications/mark-read_key/
    body: { "key":"APIKEY", "ids":[1,2], "played": true }
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = {}
    key = body.get("key") or request.POST.get("key")
    user = user_from_apikey(key)
    if not user:
        return json_bad_request("invalid key", 401)

    ids = body.get("ids") or []
    if not ids:
        return json_bad_request("ids required")

    played = bool(body.get("played", True))
    updated = Notification.objects.filter(user=user, pk__in=ids).update(seen=True, played=played)
    return JsonResponse({"ok": True, "updated": updated})





def _user_from_apikey(request):
    """
    Authentication: header 'Authorization: ApiKey <key>'.
    Returns user or None.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("ApiKey "):
        return None
    key = auth.split(None, 1)[1].strip()
    try:
        us = UserSettings.objects.select_related("user").get(api_key=key)
        return us.user
    except UserSettings.DoesNotExist:
        return None


@require_GET
def active_notification(request):
    """
    Return the next unseen notification for this user.
    JSON shape used by the desktop client.
    """
    user = _user_from_apikey(request)
    if not user:
        return HttpResponseForbidden("Invalid API key")

    note = (
        Notification.objects
        .filter(user=user, seen=False)
        .order_by("detected_at")
        .first()
    )
    if not note:
        return JsonResponse({"has": False})

    # ring_count lives on your custom User in your project; default to 1 if missing
    ring_count = getattr(user, "ring_count", 1)

    return JsonResponse({
        "has": True,
        "id": note.id,
        "source_name": getattr(note.source, "name", "App"),
        "title": note.title or "",
        "message": note.message or "",
        "ring_count": int(min(5, max(0, ring_count))),  # hard-cap 0..5
        "detected_at": note.detected_at.isoformat(),
    })

@csrf_exempt
@require_POST
def mark_notifications_read(request):
    """
    Mark all unseen notifications as seen/played for the API user.
    """
    user = _user_from_apikey(request)
    if not user:
        return HttpResponseForbidden("Invalid API key")

    updated = (
        Notification.objects
        .filter(user=user, seen=False)
        .update(seen=True, played=True, seen_at=timezone.now())
    )
    return JsonResponse({"ok": True, "updated": updated})


@require_GET
def user_sound(request):
    """
    Stream the user's current ringtone bytes.
    Tries UserSettings.default_ringtone (FileField) first.
    Falls back to latest NotificationSound for the user.
    Returns empty body if nothing set.
    """
    user = _user_from_apikey(request)
    if not user:
        return HttpResponseForbidden("Invalid API key")

    data = None
    content_type = "application/octet-stream"

    try:
        us = UserSettings.objects.get(user=user)
    except UserSettings.DoesNotExist:
        us = None

    # 1) Try FileField on settings, e.g., us.default_ringtone
    fobj = getattr(us, "default_ringtone", None) if us else None
    if hasattr(fobj, "open"):
        try:
            fobj.open("rb")
            data = fobj.read()
            fobj.close()
        except Exception:
            data = None

    # 2) Try a stored path, e.g., us.default_ringtone_path
    if data is None and us:
        path = getattr(us, "default_ringtone_path", None)
        if path:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except Exception:
                data = None

    # 3) Fallback to a NotificationSound record
    if data is None:
        try:
            latest = (
                NotificationSound.objects
                .filter(user=user)
                .order_by("-id")
                .first()
            )
            if latest and hasattr(latest, "sound") and hasattr(latest.sound, "open"):
                latest.sound.open("rb")
                data = latest.sound.read()
                latest.sound.close()
        except Exception:
            data = None

    if not data:
        return HttpResponse(b"", content_type=content_type)

    # pygame handles mp3/wav fine even with generic content-type
    return HttpResponse(data, content_type=content_type)



def _api_user(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("ApiKey "):
        key = auth.split(None, 1)[1].strip()
    else:
        key = request.GET.get("key") or request.POST.get("key")
    if not key:
        return None
    try:
        us = UserSettings.objects.select_related("user").get(api_key=key)
        return us.user
    except UserSettings.DoesNotExist:
        return None

@csrf_exempt
@require_POST
def source_create_by_key(request):
    """
    POST: /api/source/create_key/
      body: { "key": "...", "name": "Fiverr", "check_url": "https://example.com/inbox" }
    Returns: { ok: true, id: <source_id> }
    """
    user = _api_user(request)
    if not user:
        return HttpResponseForbidden("invalid key")

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = {}
    name = body.get("name") or request.POST.get("name")
    url  = body.get("check_url") or request.POST.get("check_url")
    if not name or not url:
        return HttpResponseBadRequest("name and check_url required")

    # Create with empty extra_config; checker fills last_count/hash automatically
    src = NotificationSource.objects.create(
        user=user, name=name, check_url=url, enabled=True, extra_config={}
    )
    return JsonResponse({"ok": True, "id": src.id})

@csrf_exempt
@require_POST
def source_import_cookies_by_key(request):
    """
    POST: /api/source/import_cookies_key/
      body: { "key": "...", "source_id": 123, "cookies": {"sessionid": "...", "...": "..."} }
    Stores cookies into extra_config for that source.
    """
    user = _api_user(request)
    if not user:
        return HttpResponseForbidden("invalid key")

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return HttpResponseBadRequest("bad json")

    sid = body.get("source_id")
    cookies = body.get("cookies") or {}
    if not sid or not isinstance(cookies, dict):
        return HttpResponseBadRequest("source_id and cookies required")

    try:
        src = NotificationSource.objects.get(pk=int(sid), user=user)
    except NotificationSource.DoesNotExist:
        return HttpResponseBadRequest("source not found")

    extra = src.extra_config or {}
    extra["cookies"] = cookies
    # best-effort UA
    headers = extra.get("headers") or {}
    headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    extra["headers"] = headers
    src.extra_config = extra
    src.save(update_fields=["extra_config"])

    return JsonResponse({"ok": True})




@csrf_exempt
@require_POST
def settings_update_by_key(request):
    """
    POST /api/settings/update_key/
      { "key":"...", "volume": 0..100, "play_loop": true|false }
    """
    user = _api_user(request)
    if not user:
        return HttpResponseForbidden("invalid key")

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = {}

    volume = body.get("volume")
    play_loop = body.get("play_loop")

    us, _ = UserSettings.objects.get_or_create(user=user)
    changed = []

    if volume is not None:
        try:
            v = int(volume)
            v = max(0, min(100, v))
            us.volume = v
            changed.append("volume")
        except Exception:
            pass

    if play_loop is not None:
        us.play_loop = bool(play_loop)
        changed.append("play_loop")

    if changed:
        us.save(update_fields=changed + ["last_updated"])

    return JsonResponse({"ok": True, "changed": changed})