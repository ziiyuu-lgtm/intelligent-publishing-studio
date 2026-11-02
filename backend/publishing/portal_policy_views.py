"""Separately administered, default-off AI policy for fictional publisher portals."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .ai_usage import portal_requests_since
from .models import Organisation, OrganisationAIPolicy, OrganisationAIPolicyEvent, OrganisationMembership
from .portal_forms import OrganisationAIPolicyForm
from .production_views import _enabled

POLICY_FIELDS = ("portal_design_enabled", "portal_modules_enabled", "portal_daily_limit")


def _managed_organisation(user, slug, locked=False):
    qs = Organisation.objects.select_for_update() if locked else Organisation.objects.all()
    organisation = get_object_or_404(qs, slug=slug, active=True)
    if not user.is_active or not OrganisationMembership.objects.filter(organisation=organisation, user=user,
            role=OrganisationMembership.Role.AI_MANAGER, active=True).exists():
        raise Http404
    return organisation


def _policy(organisation, locked=False):
    qs = OrganisationAIPolicy.objects.select_for_update() if locked else OrganisationAIPolicy.objects
    return qs.filter(organisation=organisation).first()


def _context(organisation, policy, form=None):
    return {"organisation":organisation, "policy":policy,
        "form":form if form is not None else OrganisationAIPolicyForm(instance=policy or OrganisationAIPolicy(organisation=organisation)),
        "revision":policy.revision if policy else 0,
        "requests_24h":portal_requests_since(organisation, timezone.now() - timedelta(hours=24)),
        "events":policy.events.select_related("actor")[:20] if policy else []}


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def manage_policy(request, slug):
    _enabled()
    organisation = _managed_organisation(request.user, slug)
    policy = _policy(organisation)
    if request.method == "GET":
        return render(request, "publishing/manage_publisher_ai_policy.html", _context(organisation, policy))
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    form = OrganisationAIPolicyForm(request.POST, instance=policy or OrganisationAIPolicy(organisation=organisation))
    if not form.is_valid():
        return render(request, "publishing/manage_publisher_ai_policy.html",
                      _context(organisation, _policy(organisation), form), status=400)
    with transaction.atomic():
        organisation = _managed_organisation(request.user, slug, locked=True)
        policy = _policy(organisation, locked=True)
        if request.POST.get("revision") != str(policy.revision if policy else 0):
            return HttpResponse(_("The AI policy changed; reload the page."), status=409)
        values = {field:form.cleaned_data[field] for field in POLICY_FIELDS}
        if not policy:
            policy = OrganisationAIPolicy.objects.create(organisation=organisation, **values)
            changed = True
        else:
            changed = any(getattr(policy, field) != values[field] for field in POLICY_FIELDS)
            if changed:
                for field, value in values.items():
                    setattr(policy, field, value)
                policy.revision += 1
                policy.save(update_fields=[*POLICY_FIELDS, "revision", "updated_at"])
        if changed:
            OrganisationAIPolicyEvent.objects.create(policy=policy, actor=request.user, revision=policy.revision, configuration=values)
    messages.success(request, _("Publisher AI policy saved for local testing."))
    return redirect("manage_publisher_ai_policy", slug=slug)
