"""Print a new VAPID key pair for push notifications.

Put the two lines in the server's .env (never in the repository). Changing
the keys later means every device has to turn notifications on again.
"""

from django.core.management.base import BaseCommand

from comms.webpush import generate_vapid_keys


class Command(BaseCommand):
    help = "Print a new VAPID key pair (VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY) for .env."

    def handle(self, *args, **options):
        public, private = generate_vapid_keys()
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}\n")
        self.stderr.write("Keep VAPID_PRIVATE_KEY secret. Set VAPID_SUBJECT=mailto:<contact>.")
