# webnotify/urls.py
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from . import views          # your function-based pages & simple APIs
from . import views_api      # your DRF class-based APIs
from .views import active_notification, mark_notifications_read, user_sound

app_name = "webnotify"

urlpatterns = [
    # ------- Pages / Auth (function-based views) -------
    path("", views.dashboard, name="dashboard"),
    path("accounts/register/", views.RegisterView, name="register"),   # function-based per your current code
    path("accounts/login/", views.LoginView, name="login"),
    path("accounts/logout/", views.LogoutView, name="logout"),

    path("settings/", views.settings_page, name="settings_page"),
    path("sources/", views.sources_page, name="sources_page"),
    path("notifications/", views.notifications_page, name="notifications_page"),

    # ------- Legacy minimal JSON endpoints (function-based) -------
    # Keep ONLY those that do not overlap with the DRF versions.
    path("api/notifications/active_key/", views.active_notification_by_key, name="api_notifications_active_key"),
    path("api/notifications/mark-read_key/", views.mark_notifications_read_by_key, name="api_notifications_mark_read_key"),
    path("api/settings_key/", views.settings_by_key, name="api_settings_key"),
    path("api/ringtones/upload/", views.upload_ringtone, name="api_ringtones_upload"),  # keep if you still use this func

    # ------- DRF APIs (class-based) — single source of truth -------
    path("api/notifications/", views_api.NotificationListAPI.as_view(), name="api_notifications_list"),
    path("api/notifications/active/", views_api.NotificationActiveAPI.as_view(), name="api_notifications_active"),
    path("api/notifications/mark-read/", views_api.NotificationMarkReadAPI.as_view(), name="api_notifications_mark_read"),
    path("api/notifications/clear-all/", views_api.NotificationClearAllAPI.as_view(), name="api_notifications_clear_all"),
    path("api/notifications/delete-all/", views_api.NotificationDeleteAllAPI.as_view(), name="api_notifications_delete_all"),

    path("api/sources/", views_api.SourceListCreateAPI.as_view(), name="api_sources"),
    path("api/ringtones/upload/", views_api.UploadRingtoneAPI.as_view(), name="api_ringtone_upload"),

    # 👇 IMPORTANT: only this settings route remains; remove the legacy one.
    path("api/settings/", views_api.UserSettingsAPI.as_view(), name="api_settings"),
    path("api/active_notification/", active_notification, name="active_notification"),
    path("api/mark_notifications_read/", mark_notifications_read, name="mark_notifications_read"),
    path("api/sound/", user_sound, name="user_sound"),

    path("api/source/create_key/", views.source_create_by_key, name="source_create_by_key"),
    path("api/source/import_cookies_key/", views.source_import_cookies_by_key, name="source_import_cookies_by_key"),
    path("api/settings/update_key/", views.settings_update_by_key, name="settings_update_by_key"),

    path("api/settings/set_play_in_background/", views.set_play_in_background, name="set_play_in_background"),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
