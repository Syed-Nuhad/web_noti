from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from webnotify.models import NotificationSource
from webnotify.tasks import check_source

User = get_user_model()


class Command(BaseCommand):
    help = "Run live source checks (uses stored cookies, no test notifications)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email", "-e",
            help="Filter by user email. If omitted, checks all users."
        )
        parser.add_argument(
            "--name", "-n",
            help="Filter by source name (case-insensitive contains)."
        )
        parser.add_argument(
            "--source-id", "-sid", type=int,
            help="Only process this source id (must belong to the selected user if --email is used)."
        )

    def handle(self, *args, **opts):
        email     = opts.get("email")
        name      = opts.get("name")
        source_id = opts.get("source_id")

        # 1) Base queryset (by user if provided)
        if email:
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"No user with email={email}"))
                return
            qs = NotificationSource.objects.filter(user=user, enabled=True)
        else:
            qs = NotificationSource.objects.filter(enabled=True)

        # 2) Optional filters
        if name:
            qs = qs.filter(name__icontains=name)
        if source_id is not None:
            qs = qs.filter(pk=source_id)

        sources = list(qs.order_by("id"))
        if not sources:
            self.stdout.write(self.style.WARNING("No enabled sources found for the given filter(s)."))
            return

        created = 0
        checked = 0

        for src in sources:
            try:
                # True => new Notification created; False => no change/baseline
                res = check_source(src.id)
                checked += 1
                if res:
                    created += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"[changed] Notification created for source {src.pk} ({src.name})"
                    ))
            except KeyboardInterrupt:
                self.stderr.write(self.style.WARNING("\nInterrupted by user (Ctrl+C). Exiting cleanly…"))
                break
            except Exception as e:
                self.stderr.write(self.style.WARNING(
                    f"[skip] Source {src.pk} ({src.name}) failed: {e}"
                ))

        self.stdout.write(self.style.SUCCESS(f"Checked sources: {checked}, New notifications: {created}"))