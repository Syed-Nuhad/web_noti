# webnotify/views_api.py
import json
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from rest_framework import generics, permissions, pagination, status, throttling

from django.utils import timezone
from django.templatetags.static import static
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions, status


from .models import Notification, NotificationSource, UserSettings, CustomRingtone
from .serializers import (
    NotificationSerializer,
    NotificationSourceSerializer,
)

# ---------- throttling (protect endpoints) ----------
class UserBurst(throttling.UserRateThrottle):
    scope = "user_burst"
class UserSustained(throttling.UserRateThrottle):
    scope = "user_sustained"

# ---------- notifications ----------
class NotificationListAPI(generics.ListAPIView):
    """
    GET /api/notifications/?unplayed=true (optional) & page=1
    Lists recent notifications for the logged-in user.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NotificationSerializer
    pagination_class = pagination.PageNumberPagination
    throttle_classes = [UserBurst, UserSustained]

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user).order_by("-detected_at")
        if self.request.query_params.get("unplayed") in ("1", "true", "True"):
            qs = qs.filter(played=False)
        return qs


class NotificationActiveAPI(APIView):
    """
    GET /api/notifications/active/
    Returns latest UNPLAYED notification (does not mark it).
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [UserBurst]

    def get(self, request):
        notif = (
            Notification.objects
            .filter(user=request.user, played=False)
            .order_by("-detected_at")
            .first()
        )
        if not notif:
            return Response({"has": False})
        return Response({
            "has": True,
            **NotificationSerializer(notif).data
        })


class NotificationMarkReadAPI(APIView):
    """
    POST /api/notifications/mark-read/
    body: {"ids":[1,2], "played": true}
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [UserBurst]

    def post(self, request):
        try:
            ids = request.data.get("ids", [])
            played = bool(request.data.get("played", True))
        except Exception:
            return Response({"ok": False, "error": "bad body"}, status=status.HTTP_400_BAD_REQUEST)
        if not ids:
            return Response({"ok": False, "error": "ids required"}, status=status.HTTP_400_BAD_REQUEST)
        updated = Notification.objects.filter(user=request.user, pk__in=ids).update(seen=True, played=played)
        return Response({"ok": True, "updated": updated})


class NotificationClearAllAPI(APIView):
    """
    POST /api/notifications/clear-all/
    Marks ALL as seen+played for this user (use carefully).
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [UserSustained]

    def post(self, request):
        cnt = Notification.objects.filter(user=request.user).update(seen=True, played=True)
        return Response({"ok": True, "updated": cnt})


# ---------- sources ----------
class SourceListCreateAPI(APIView):
    """
    GET /api/sources/
    POST /api/sources/
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [UserSustained]

    def get(self, request):
        qs = NotificationSource.objects.filter(user=request.user).order_by("id")
        return Response({"sources": NotificationSourceSerializer(qs, many=True).data})

    def post(self, request):
        data = request.data or {}
        src = NotificationSource.objects.create(
            user=request.user,
            name=data.get("name", "Unnamed"),
            check_url=data.get("check_url", ""),
            extra_config=self._parse_json(data.get("extra_config", "")) or {},
        )
        return Response({"ok": True, "source": NotificationSourceSerializer(src).data}, status=201)

    def _parse_json(self, s):
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            return None





class UploadRingtoneAPI(APIView):
    """
    POST multipart to /api/ringtones/upload/  (field: file)
    Saves file and makes it DEFAULT immediately.
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [UserBurst]

    def post(self, request):
        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response({"ok": False, "error": "no file"}, status=400)

        allowed = {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp3"}
        if uploaded.content_type not in allowed:
            return Response({"ok": False, "error": "unsupported type"}, status=400)

        max_mb = int(getattr(settings, "MAX_RINGTONE_MB", 8))
        if uploaded.size > max_mb * 1024 * 1024:
            return Response({"ok": False, "error": f"max {max_mb}MB"}, status=400)

        subpath = timezone.now().strftime("ringtones/%Y/%m/%d/")
        saved_rel = default_storage.save(subpath + uploaded.name, ContentFile(uploaded.read()))

        # single default
        CustomRingtone.objects.filter(user=request.user, is_default=True).update(is_default=False)
        rt = CustomRingtone.objects.create(
            user=request.user, name=uploaded.name, file=saved_rel, size_bytes=uploaded.size, is_default=True
        )

        us, _ = UserSettings.objects.get_or_create(user=request.user)
        us.default_ringtone = rt
        us.save(update_fields=["default_ringtone", "last_updated"])

        try:
            rel = rt.file.url
            file_url = request.build_absolute_uri(rel)
        except Exception:
            file_url = None

        return Response({"ok": True, "id": rt.pk, "file": file_url})

class NotificationDeleteAllAPI(APIView):
    """
    POST /api/notifications/delete-all/
    body (optional): {"older_than_days": 0}  # 0 or missing => delete ALL
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [UserSustained]

    def post(self, request):
        try:
            older_than_days = int(request.data.get("older_than_days", 0))
        except Exception:
            older_than_days = 0

        qs = Notification.objects.filter(user=request.user)
        if older_than_days > 0:
            cutoff = timezone.now() - timedelta(days=older_than_days)  # <-- fixed
            qs = qs.filter(detected_at__lt=cutoff)

        deleted_count, _ = qs.delete()
        return Response({"ok": True, "deleted": deleted_count}, status=status.HTTP_200_OK)






class UserSettingsAPI(APIView):
    """
    GET  /api/settings/
    POST /api/settings/   body: {"volume": 0..100, "play_loop": true|false}
    Saves volume/loop in session (no migrations), and returns current settings.
    Falls back to /static/audio/beep.mp3 when user has no custom ringtone.
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_default_ringtone_url_and_name(self, request):
        # Use your CustomRingtone model if available; otherwise fall back to static
        try:
            latest = (
                CustomRingtone.objects.filter(user=request.user)
                .order_by("-created_at")
                .first()
            )
            if latest and latest.file:
                # .url works with default/storage; name is just a nice label
                return latest.file.url, (latest.file.name.split("/")[-1] or "Custom")
        except Exception:
            pass
        return static("audio/beep.mp3"), "Default"

    def get(self, request):
        sess = request.session.get("notihub_settings", {})
        try:
            volume = int(sess.get("volume", 80))
        except Exception:
            volume = 80
        volume = max(0, min(100, volume))
        play_loop = bool(sess.get("play_loop", True))

        url, name = self._get_default_ringtone_url_and_name(request)
        return Response(
            {
                "ok": True,
                "settings": {
                    "volume": volume,
                    "play_loop": play_loop,
                    "default_ringtone_url": url,
                    "default_ringtone_name": name,
                },
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        data = request.data or {}
        try:
            volume = int(data.get("volume", 80))
        except Exception:
            volume = 80
        volume = max(0, min(100, volume))

        play_loop = data.get("play_loop", True)
        if isinstance(play_loop, str):
            play_loop = play_loop.lower() in ("1", "true", "yes", "on")
        else:
            play_loop = bool(play_loop)

        request.session["notihub_settings"] = {"volume": volume, "play_loop": play_loop}
        request.session.modified = True

        url, name = self._get_default_ringtone_url_and_name(request)
        return Response(
            {
                "ok": True,
                "settings": {
                    "volume": volume,
                    "play_loop": play_loop,
                    "default_ringtone_url": url,
                    "default_ringtone_name": name,
                },
            },
            status=status.HTTP_200_OK,
        )