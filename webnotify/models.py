import uuid

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None   # disable username
    email = models.EmailField(unique=True)
    ring_count = models.IntegerField(default=1)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()   # <<< FIX: attach custom manager here

    def __str__(self):
        return self.email


class MonitoredURL(models.Model):
    url = models.TextField()  # Encrypted
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    css_selector = models.CharField(max_length=200, blank=True, null=True)


class NotificationSound(models.Model):
    sound = models.TextField()  # Encrypted
    user = models.ForeignKey(User, on_delete=models.CASCADE)



class NotificationSource(models.Model):
    """
    A source to check for notifications (e.g. Fiverr account, other services).
    We'll store per-user sources so each user can add multiple monitoring targets.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_sources")
    name = models.CharField(max_length=120)
    check_url = models.URLField(help_text="URL used to check for new notifications (or API endpoint).")
    enabled = models.BooleanField(default=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    # Store arbitrary provider-specific settings (cookies, selectors, headers)
    extra_config = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Notification Source"
        verbose_name_plural = "Notification Sources"

    def __str__(self):
        return f"{self.name} ({'on' if self.enabled else 'off'})"


class CustomRingtone(models.Model):
    """
    A ringtone uploaded by user. We'll store file and some metadata.
    Allowed types: mp3, wav (validate in forms/views).
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ringtones")
    name = models.CharField(max_length=140, blank=True)
    file = models.FileField(upload_to="ringtones/%Y/%m/%d/")
    size_bytes = models.PositiveIntegerField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)  # optional, we can fill later
    created_at = models.DateTimeField(auto_now_add=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.name or f"ringtone-{self.pk}"




class Notification(models.Model):
    """
    Stores one detected notification event.
    external_id: optional id from remote system to avoid duplicates.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    source = models.ForeignKey(NotificationSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications")
    title = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    link = models.URLField(blank=True, null=True)
    external_id = models.CharField(max_length=255, blank=True, null=True, help_text="ID from provider to dedupe (if available)")
    detected_at = models.DateTimeField(default=timezone.now)
    seen = models.BooleanField(default=False)      # user has seen it in UI
    played = models.BooleanField(default=False)    # browser/app already played alarm
    meta = models.JSONField(null=True, blank=True) # extra provider-specific data

    class Meta:
        ordering = ("-detected_at",)

    def __str__(self):
        return f"{self.title or 'Notification'} [{self.user}]"




class UserSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="settings")
    volume = models.PositiveIntegerField(default=80)
    play_loop = models.BooleanField(default=True)
    play_in_background = models.BooleanField(default=True)
    default_ringtone = models.ForeignKey("CustomRingtone", null=True, blank=True, on_delete=models.SET_NULL)
    last_updated = models.DateTimeField(auto_now=True)

    # NEW: simple API key for desktop client
    api_key = models.CharField(max_length=64, unique=True, blank=True, null=True)

    def ensure_api_key(self):
        if not self.api_key:
            # 32 hex is enough; you can double it if you want
            self.api_key = uuid.uuid4().hex
            self.save(update_fields=["api_key"])

    def __str__(self):
        return f"Settings({self.user})"