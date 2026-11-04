"""Validate email configuration format without contacting an email server."""
import os

from django.core.management.base import BaseCommand, CommandError

from config.smtp_policy import SMTPConfigurationError, SMTPPolicy


class Command(BaseCommand):
    help = "Validate Altavien SMTP configuration offline without enabling or sending mail."

    def handle(self, *args, **options):
        try:
            SMTPPolicy.from_mapping(os.environ)
        except SMTPConfigurationError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write("ALTAVIEN_SMTP_FORMAT_VALID")
        self.stdout.write("Offline check only: no connection was made and mail was not enabled.")
