"""Explicitly process fictional invitation simulations; no mail is sent."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from publishing.local_review_mail import simulate_invitation
from publishing.models import LocalReviewMailJob


class Command(BaseCommand):
    help = "Simulate pending fictional review invitations in memory; no external mail."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        if not settings.DEBUG or not getattr(settings, "IPS_LOCAL_REVIEW_MAIL_ENABLED", False):
            raise CommandError("The local fictional mail simulation is disabled.")
        limit = options["limit"]
        if not 1 <= limit <= 100:
            raise CommandError("Limit must be between 1 and 100.")
        ids = list(LocalReviewMailJob.objects.filter(status=LocalReviewMailJob.Status.PENDING)
                   .order_by("created_at", "pk").values_list("pk", flat=True)[:limit])
        results = [simulate_invitation(pk) for pk in ids]
        self.stdout.write(f"Simulated: {results.count('simulated')}; suppressed: {results.count('suppressed')}. No email was sent.")
