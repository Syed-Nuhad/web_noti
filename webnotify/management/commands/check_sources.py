# webnotify/management/commands/check_sources.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from webnotify.models import NotificationSource, Notification

User = get_user_model()


class Command(BaseCommand):
    help = "Create test notifications for a user or run the placeholder source checks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            "-e",
            help="Email of the user to create a test notification for (creates for all users if omitted).",
        )
        parser.add_argument(
            "--source-id",
            "-s",
            type=int,
            help="If provided, only create notifications for this source id (must belong to the user).",
        )
        parser.add_argument(
            "--message",
            "-m",
            help="Custom message for generated notification.",
        )

    def handle(self, *args, **options):
        email = options.get("email")
        source_id = options.get("source_id")
        message = options.get("message")

        if email:
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"No user with email={email}"))
                return
            sources = NotificationSource.objects.filter(user=user, enabled=True)
            if source_id:
                sources = sources.filter(pk=source_id)
        else:
            sources = NotificationSource.objects.filter(enabled=True)

        created = 0
        for src in sources:
            title = f"Test notification from {src.name}"
            msg = message or f"Management command generated at {timezone.now().isoformat()}"
            Notification.objects.create(user=src.user, source=src, title=title, message=msg)
            created += 1
            self.stdout.write(self.style.SUCCESS(f"Created test notification for source {src.pk} (user={src.user})"))

        if created == 0:
            # Fallback: if there are no sources, create a notification for the superuser or all users
            if email is None:
                # create one notification per superuser
                superusers = User.objects.filter(is_superuser=True)
                for su in superusers:
                    Notification.objects.create(user=su, title="Fallback test notification", message="Fallback message")
                    created += 1
                    self.stdout.write(self.style.SUCCESS(f"Created fallback notification for superuser {su.email}"))
        self.stdout.write(self.style.SUCCESS(f"Total created: {created}"))
