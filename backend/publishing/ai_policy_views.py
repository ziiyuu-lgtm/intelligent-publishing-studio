"""Journal-scoped configuration for fictional metadata and site design tasks."""
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .forms import JournalAIPolicyForm
from .models import Journal, JournalAIPolicy, JournalAIPolicyEvent, Membership
from .ai_usage import journal_requests_since
from .views import _has_role

POLICY_FIELDS = ("metadata_enabled", "allow_author", "allow_editor", "site_design_enabled", "site_modules_enabled", "site_pages_enabled", "site_page_blocks_enabled", "site_page_layout_enabled", "site_image_enabled", "revision_plan_enabled", "editor_revision_comparison_enabled", "review_synthesis_enabled", "journal_daily_limit")


def _managed_journal(user, slug, locked=False):
    if not settings.DEBUG:
        raise Http404
    qs = Journal.objects.select_for_update() if locked else Journal.objects.select_related("organisation")
    journal = get_object_or_404(qs, slug=slug, active=True, organisation__active=True)
    if not _has_role(user, journal, Membership.Role.AI_MANAGER):
        raise Http404
    return journal


def _context(journal, policy, form=None):
    since = timezone.now() - timedelta(hours=24)
    return {
        "journal": journal,
        "policy": policy,
        "form": form if form is not None else JournalAIPolicyForm(instance=policy or JournalAIPolicy(journal=journal)),
        "revision": policy.revision if policy else 0,
        "requests_24h": journal_requests_since(journal, since),
        "events": policy.events.select_related("actor")[:20] if policy else [],
    }


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def manage_ai_policy(request, slug):
    journal = _managed_journal(request.user, slug)
    policy = JournalAIPolicy.objects.filter(journal=journal).first()
    if request.method == "GET":
        return render(request, "publishing/manage_ai_policy.html", _context(journal, policy))
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    form = JournalAIPolicyForm(request.POST, instance=policy or JournalAIPolicy(journal=journal))
    if not form.is_valid():
        return render(request, "publishing/manage_ai_policy.html", _context(journal, JournalAIPolicy.objects.filter(journal=journal).first(), form), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        policy = JournalAIPolicy.objects.select_for_update().filter(journal=journal).first()
        if request.POST.get("revision") != str(policy.revision if policy else 0):
            return HttpResponse(_("The AI policy changed; reload the page."), status=409)
        values = {field: form.cleaned_data[field] for field in POLICY_FIELDS}
        if policy is None:
            policy = JournalAIPolicy.objects.create(journal=journal, **values)
            changed = True
        else:
            changed = any(getattr(policy, field) != values[field] for field in POLICY_FIELDS)
            if changed:
                for field, value in values.items():
                    setattr(policy, field, value)
                policy.revision += 1
                policy.save(update_fields=[*POLICY_FIELDS, "revision", "updated_at"])
        if changed:
            JournalAIPolicyEvent.objects.create(policy=policy, actor=request.user, revision=policy.revision, configuration=values)
    messages.success(request, _("Journal AI policy saved for local testing."))
    return redirect("manage_ai_policy", slug=slug)
