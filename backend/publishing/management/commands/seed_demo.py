import secrets
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from publishing.models import Journal, Membership, Organisation, OrganisationMembership


class Command(BaseCommand):
    help = "Create isolated, fictional journal and user data for local testing."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data may only be created in a development environment.")
        suffix = secrets.token_hex(4)
        credentials = []
        with transaction.atomic():
            organisation = Organisation.objects.create(name="SCIENTARA-inspired test organisation", slug=f"test-organisation-{suffix}")
            journal_a = Journal.objects.create(organisation=organisation, name="AIRES (fictional test)", slug=f"aires-test-{suffix}", description="Fictional test journal for the publishing workflow.")
            journal_b = Journal.objects.create(organisation=organisation, name="VERSIONICS (fictional test)", slug=f"versionics-test-{suffix}", description="A second fictional journal for access isolation checks.")
            for role, journal, member_role in [
                ("author", journal_a, Membership.Role.AUTHOR),
                ("editor", journal_a, Membership.Role.EDITOR),
                ("coordinator", journal_a, Membership.Role.COORDINATOR),
                ("producer", journal_a, Membership.Role.PRODUCER),
                ("site_manager", journal_a, Membership.Role.SITE_MANAGER),
                ("ai_manager", journal_a, Membership.Role.AI_MANAGER),
                ("reviewer", journal_a, Membership.Role.REVIEWER),
                ("other_editor", journal_b, Membership.Role.EDITOR),
            ]:
                username = f"test_{role}_{suffix}"
                password = secrets.token_urlsafe(24)
                user = get_user_model().objects.create_user(username=username, password=password)
                Membership.objects.create(user=user, journal=journal, role=member_role)
                if role == "author":
                    Membership.objects.create(user=user, journal=journal_b, role=Membership.Role.AUTHOR)
                credentials.append((role, username, password))
            username = f"test_portal_manager_{suffix}"
            password = secrets.token_urlsafe(24)
            user = get_user_model().objects.create_user(username=username, password=password)
            OrganisationMembership.objects.create(user=user, organisation=organisation, role=OrganisationMembership.Role.PORTAL_MANAGER)
            credentials.append(("portal_manager", username, password))
            username = f"test_publisher_ai_manager_{suffix}"
            password = secrets.token_urlsafe(24)
            user = get_user_model().objects.create_user(username=username, password=password)
            OrganisationMembership.objects.create(user=user, organisation=organisation, role=OrganisationMembership.Role.AI_MANAGER)
            credentials.append(("publisher_ai_manager", username, password))
        self.stdout.write(self.style.WARNING("Synthetic data only. These generated credentials are shown once; keep them private."))
        for role, username, password in credentials:
            self.stdout.write(f"{role}: {username}  password: {password}")
        self.stdout.write("Start the local server; after acceptance, the coordinator may assign the production editor in the local test workspace.")
