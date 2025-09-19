from django.contrib import admin

from webnotify.models import NotificationSource, Notification, CustomRingtone, UserSettings


# Register your models here.
@admin.register(NotificationSource)
class NotificationSourceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "name", "enabled", "last_checked", "created_at")
    list_filter = ("enabled", "created_at")
    search_fields = ("name", "user__email", "user__username")

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "title", "source", "detected_at", "seen", "played")
    list_filter = ("seen", "played", "detected_at")
    search_fields = ("title", "message", "external_id")

@admin.register(CustomRingtone)
class CustomRingtoneAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "name", "is_default", "created_at")
    search_fields = ("name", "user__email")

@admin.register(UserSettings)
class UserSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "default_ringtone", "volume", "play_loop")