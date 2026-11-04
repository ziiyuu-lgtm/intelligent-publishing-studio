"""Remove expired, pseudonymous local-test login counters."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from publishing.models import LocalLoginThrottleBucket


class Command(BaseCommand):
    help = "Delete expired local-test login counters."

    def handle(self, *args, **options):
        count, _ = LocalLoginThrottleBucket.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(f"Expired local login counters removed: {count}")
