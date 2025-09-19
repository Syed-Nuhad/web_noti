# webnotify/management/commands/print_apikey.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from webnotify.models import UserSettings

User = get_user_model()

class Command(BaseCommand):
    help = "Ensure and print the API key for a user."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)

    def handle(self, *args, **opts):
        email = opts["email"]
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"No user with email={email}"))
            return
        us, _ = UserSettings.objects.get_or_create(user=user)
        us.ensure_api_key()
        self.stdout.write(self.style.SUCCESS(f"API key for {email}: {us.api_key}"))
