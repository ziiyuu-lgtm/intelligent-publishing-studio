"""Local-only journal site design, preview and versioned test publication."""
import hashlib
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .forms import JournalSiteForm
from .models import Journal, JournalSiteDraft, JournalSiteEvent, JournalSiteRelease, Membership
from .production_views import _enabled, _no_index
from .views import _has_role

SITE_FIELDS = ("brand_name", "headline", "tagline", "introduction", "theme", "layout")


def _managed_journal(user, slug, locked=False):
    qs = Journal.objects.select_for_update() if locked else Journal.objects.select_related("organisation")
    journal = get_object_or_404(qs, slug=slug, active=True, organisation__active=True)
    if not _has_role(user, journal, Membership.Role.SITE_MANAGER):
        raise Http404
    return journal


def _site(journal, locked=False):
    qs = JournalSiteDraft.objects.select_for_update() if locked else JournalSiteDraft.objects.select_related("current_release")
    return get_object_or_404(qs, journal=journal)


def _fingerprint(site):
    payload = {"site": site.pk, "revision": site.revision, **{key: getattr(site, key) for key in SITE_FIELDS}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _context(journal, site, form=None):
    return {"journal": journal, "site": site, "form": form if form is not None else JournalSiteForm(instance=site),
            "fingerprint": _fingerprint(site), "releases": site.releases.select_related("published_by")}


def _stale(request, site):
    return request.POST.get("revision") != str(site.revision)


@login_required
@never_cache
@require_GET
def manage_journal_site(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = JournalSiteDraft.objects.filter(journal=journal).select_related("current_release").first()
    return render(request, "publishing/manage_journal_site.html", _context(journal, site) if site else {"journal": journal})


@login_required
@never_cache
@require_POST
def start_journal_site(request, slug):
    _enabled()
    if request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Confirm that the materials are fictional."), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site, created = JournalSiteDraft.objects.get_or_create(journal=journal, defaults={"brand_name": journal.name, "headline": journal.name,
            "tagline": "Fictional journal site", "introduction": "A fictional journal for local testing.", "created_by": request.user})
        if created:
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_draft_created", revision=site.revision)
    messages.success(request, _("Journal site draft created.") if created else _("The journal site draft already exists."))
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_POST
def edit_journal_site(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    form = JournalSiteForm(request.POST, instance=site)
    if not form.is_valid():
        return render(request, "publishing/manage_journal_site.html", _context(journal, _site(journal), form), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        changed = any(getattr(site, key) != form.cleaned_data[key] for key in SITE_FIELDS)
        if changed:
            for key in SITE_FIELDS:
                setattr(site, key, form.cleaned_data[key])
            site.revision += 1
            site.save(update_fields=[*SITE_FIELDS, "revision"])
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_draft_updated", revision=site.revision)
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_GET
def preview_journal_site(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    return _no_index(render(request, "publishing/journal_site.html", {"journal": journal, "design": site, "preview": True}))


@login_required
@never_cache
@require_POST
def publish_journal_site(request, slug):
    _enabled()
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        if _stale(request, site) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if (site.theme not in JournalSiteDraft.Theme.values or site.layout not in JournalSiteDraft.Layout.values or
                not site.brand_name.strip() or not site.headline.strip() or not site.introduction.strip()):
            return HttpResponse(_("The site design is invalid."), status=409)
        if site.current_release_id and (site.current_release.site_id != site.pk or site.current_release.revision == site.revision):
            return HttpResponse(_("This version is already published or unavailable."), status=409)
        release = JournalSiteRelease.objects.create(site=site, revision=site.revision, published_by=request.user,
            **{key: getattr(site, key) for key in SITE_FIELDS})
        site.current_release = release
        site.save(update_fields=["current_release"])
        JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_test_published", revision=site.revision, source_release=release)
    messages.success(request, _("Journal site published in the local test environment."))
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_POST
def restore_journal_site(request, slug, release_id):
    _enabled()
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        release = get_object_or_404(JournalSiteRelease, pk=release_id, site=site)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if request.POST.get("confirm") != "yes":
            return HttpResponse(_("Confirm the draft restoration."), status=400)
        if release.theme not in JournalSiteDraft.Theme.values or release.layout not in JournalSiteDraft.Layout.values:
            return HttpResponse(_("The site design is invalid."), status=409)
        changed = any(getattr(site, key) != getattr(release, key) for key in SITE_FIELDS)
        if changed:
            for key in SITE_FIELDS:
                setattr(site, key, getattr(release, key))
            site.revision += 1
            site.save(update_fields=[*SITE_FIELDS, "revision"])
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_draft_restored", revision=site.revision, source_release=release)
    messages.success(request, _("Version restored to the draft; publish separately after review."))
    return redirect("manage_journal_site", slug=slug)


@require_GET
def public_journal_site(request, slug):
    _enabled()
    journal = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
    site = get_object_or_404(JournalSiteDraft.objects.select_related("current_release"), journal=journal, current_release__isnull=False)
    if (site.current_release.site_id != site.pk or site.current_release.theme not in JournalSiteDraft.Theme.values or
            site.current_release.layout not in JournalSiteDraft.Layout.values):
        raise Http404
    return _no_index(render(request, "publishing/journal_site.html", {"journal": journal, "design": site.current_release, "preview": False}))
