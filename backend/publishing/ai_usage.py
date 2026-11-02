"""Shared local-test attempt counts across implemented AI tasks."""
from .models import AIAssistanceRun, JournalSiteAIDesignRun

DAILY_REQUEST_LIMIT = 3


def account_requests_since(user, since):
    return (AIAssistanceRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            JournalSiteAIDesignRun.objects.filter(requested_by=user, created_at__gte=since).count())


def journal_requests_since(journal, since):
    return (AIAssistanceRun.objects.filter(version__submission__journal=journal, created_at__gte=since).count() +
            JournalSiteAIDesignRun.objects.filter(site__journal=journal, created_at__gte=since).count())
