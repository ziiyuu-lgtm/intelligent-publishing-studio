"""Fail closed if the blocked candidate is accidentally asked to send mail."""
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend


class EmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if email_messages:
            raise ImproperlyConfigured("Email delivery is disabled in the blocked release candidate.")
        return 0
