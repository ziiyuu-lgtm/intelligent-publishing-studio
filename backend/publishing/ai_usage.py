"""Shared local-test attempt counts across implemented AI tasks."""
from .models import AIAssistanceRun, EditorRevisionComparisonRun, JournalSiteAIDesignRun, OrganisationPortalAIDesignRun, RevisionPlanRun, ReviewSynthesisRun, SiteImageGenerationRun

DAILY_REQUEST_LIMIT = 3


def account_requests_since(user, since):
    return (AIAssistanceRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            JournalSiteAIDesignRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            OrganisationPortalAIDesignRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            SiteImageGenerationRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            RevisionPlanRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            EditorRevisionComparisonRun.objects.filter(requested_by=user, created_at__gte=since).count() +
            ReviewSynthesisRun.objects.filter(requested_by=user, created_at__gte=since).count())


def journal_requests_since(journal, since):
    return (AIAssistanceRun.objects.filter(version__submission__journal=journal, created_at__gte=since).count() +
            JournalSiteAIDesignRun.objects.filter(site__journal=journal, created_at__gte=since).count() +
            SiteImageGenerationRun.objects.filter(journal_site__journal=journal, created_at__gte=since).count() +
            RevisionPlanRun.objects.filter(submission__journal=journal, created_at__gte=since).count() +
            EditorRevisionComparisonRun.objects.filter(submission__journal=journal, created_at__gte=since).count() +
            ReviewSynthesisRun.objects.filter(submission__journal=journal, created_at__gte=since).count())


def portal_requests_since(organisation, since):
    return (OrganisationPortalAIDesignRun.objects.filter(site__organisation=organisation, created_at__gte=since).count() +
            SiteImageGenerationRun.objects.filter(portal_site__organisation=organisation, created_at__gte=since).count())
