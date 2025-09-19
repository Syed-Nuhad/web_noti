from rest_framework import serializers
from .models import Notification, NotificationSource, UserSettings, CustomRingtone

class NotificationSerializer(serializers.ModelSerializer):
    source_name = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "title", "message", "link", "detected_at", "seen", "played", "source_name"]

    def get_source_name(self, obj):
        return obj.source.name if obj.source else None


class NotificationSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationSource
        fields = ["id", "name", "check_url", "cooldown_seconds", "is_active", "snooze_until"]


class CustomRingtoneSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = CustomRingtone
        fields = ["id", "name", "url", "is_default", "uploaded_at", "size_bytes"]

    def get_url(self, obj):
        try:
            request = self.context.get("request")
            rel = obj.file.url
            return request.build_absolute_uri(rel) if request else rel
        except Exception:
            return None


class UserSettingsSerializer(serializers.ModelSerializer):
    default_ringtone = CustomRingtoneSerializer(read_only=True)

    class Meta:
        model = UserSettings
        fields = ["volume", "play_loop", "play_in_background", "default_ringtone"]