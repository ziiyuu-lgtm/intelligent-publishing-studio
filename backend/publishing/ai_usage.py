"""Shared local-test attempt counts across implemented AI tasks."""
from .models import AIAssistanceRun, JournalSiteAIDesignRun, OrganisationPortalAIDesignRun, RevisionPlanRun

DAILY_REQUEST_LIMIT = 3


def account_requests_since(user, since):
    return (AIAssistanceRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            JournalSiteAIDesignRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            OrganisationPortalAIDesignRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            RevisionPlanRun.objects.filter(requested_by=user, created_at__gte=since).count())


def journal_requests_since(journal, since):
    return (AIAssistanceRun.objects.filter(version__submission__journal=journal, created_at__gte=since).count() +
            JournalSiteAIDesignRun.objects.filter(site__journal=journal, created_at__gte=since).count() +
            RevisionPlanRun.objects.filter(submission__journal=journal, created_at__gte=since).count())


def portal_requests_since(organisation, since):
    return OrganisationPortalAIDesignRun.objects.filter(site__organisation=organisation, created_at__gte=since).count()
