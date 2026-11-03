"""Opt-in, private image generation and human review for fictional sites."""
import hashlib
import hmac
import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import ai_views, image_ai_provider, portal_policy_views, portal_views, site_image_views, site_views
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since, portal_requests_since
from .image_ai_forms import ImageGenerationForm
from .models import SiteImageAsset, SiteImageGenerationRun
from .production_views import _enabled, _no_index
from .site_images import MAX_SITE_IMAGES, asset_is_intact


def _site(user, slug, kind, locked=False):
    if kind == 'journal':
        owner = site_views._managed_journal(user, slug, locked=locked)
        return owner, site_views._site(owner, locked=locked)
    owner = portal_views._organisation(user, slug, locked=locked)
    return owner, portal_views._site(owner, locked=locked)


def _policy(owner, kind, locked=False):
    return ai_views._policy(owner, locked=locked) if kind == 'journal' else portal_policy_views._policy(owner, locked=locked)


def _fingerprint(site, kind):
    return site_views._fingerprint(site) if kind == 'journal' else portal_views._fingerprint(site)


def _configured():
    key = os.environ.get('OPENAI_API_KEY', '')
    return bool(settings.IPS_OPENAI_IMAGE_ENABLED and key and '\r' not in key and '\n' not in key and
                re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', settings.IPS_OPENAI_IMAGE_MODEL or ''))


def _policy_snapshot(policy, kind):
    if not policy:
        return None
    enabled = policy.site_image_enabled if kind == 'journal' else policy.portal_image_enabled
    limit = policy.journal_daily_limit if kind == 'journal' else policy.portal_daily_limit
    return (policy.pk, policy.revision, enabled, limit)


def _active(policy, kind):
    snapshot = _policy_snapshot(policy, kind)
    return bool(snapshot and snapshot[2] and 1 <= snapshot[3] <= 100)


def _route(kind, suffix):
    return ('journal' if kind == 'journal' else 'portal') + '_image_ai_' + suffix


def _run(site, user, run_id, kind, locked=False):
    rows = SiteImageGenerationRun.objects.select_for_update() if locked else SiteImageGenerationRun.objects
    scope = {'journal_site':site} if kind == 'journal' else {'portal_site':site}
    return get_object_or_404(rows, pk=run_id, requested_by=user, **scope)


def _current(site, run, kind):
    return site.revision == run.site_revision and _fingerprint(site, kind) == run.site_fingerprint


def _intact(candidate):
    """Treat incomplete or damaged private image records as unavailable."""
    try:
        return bool(candidate and candidate.image_data and asset_is_intact(candidate))
    except (TypeError, ValueError, AttributeError):
        return False


def _event(site, actor, kind, action):
    site_image_views._event(site, actor, kind, action)


def _create_page(request, owner, site, kind, form=None, status=200):
    policy = _policy(owner, kind)
    return _no_index(render(request, 'publishing/image_ai_create.html', {
        'owner':owner, 'site':site, 'kind':kind, 'form':form if form is not None else ImageGenerationForm(),
        'request_id':uuid.uuid4(), 'fingerprint':_fingerprint(site, kind),
        'policy_revision':policy.revision if policy else 0, 'model_name':settings.IPS_OPENAI_IMAGE_MODEL,
        'available':_configured() and _active(policy, kind),
        'recent_runs':site.image_generation_runs.filter(requested_by=request.user).defer('image_data')[:10],
        'result_route':_route(kind, 'result'),
        'history_url':reverse('journal_design_history' if kind == 'journal' else 'publisher_design_history', args=[owner.slug]),
        'manage_url':reverse('manage_journal_site' if kind == 'journal' else 'manage_publisher_portal', args=[owner.slug]) + '#site-image-library',
    }, status=status))


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def create(request, slug, kind):
    _enabled()
    owner, site = _site(request.user, slug, kind)
    if request.method == 'GET':
        return _create_page(request, owner, site, kind)
    if request.POST.get('confirm') != 'yes' or request.POST.get('synthetic') != 'yes':
        return HttpResponse(_('Both confirmations are required.'), status=400)
    form = ImageGenerationForm(request.POST)
    if not form.is_valid():
        return _create_page(request, owner, site, kind, form, status=400)
    try:
        run_id = uuid.UUID(request.POST.get('request_id', ''))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_('Invalid request identifier.'), status=400)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        owner, site = _site(request.user, slug, kind, locked=True)
        previous = SiteImageGenerationRun.objects.filter(pk=run_id).first()
        if previous:
            scope = previous.journal_site_id if kind == 'journal' else previous.portal_site_id
            if previous.requested_by_id != request.user.pk or scope != site.pk:
                raise Http404
            return redirect(_route(kind, 'result'), slug=slug, run_id=previous.pk)
        policy = _policy(owner, kind, locked=True)
        if not _configured() or not _active(policy, kind):
            return HttpResponse(_('AI image generation is not enabled for this site and test environment.'), status=409)
        if (request.POST.get('policy_revision') != str(policy.revision) or
                request.POST.get('model') != settings.IPS_OPENAI_IMAGE_MODEL):
            return HttpResponse(_('The AI policy or model changed; reload the page.'), status=409)
        fingerprint = _fingerprint(site, kind)
        if request.POST.get('revision') != str(site.revision) or request.POST.get('fingerprint') != fingerprint:
            return HttpResponse(_('The site draft changed; reload the page.'), status=409)
        since = timezone.now() - timedelta(hours=24)
        used = journal_requests_since(owner, since) if kind == 'journal' else portal_requests_since(owner, since)
        limit = policy.journal_daily_limit if kind == 'journal' else policy.portal_daily_limit
        if account_requests_since(request.user, since) >= DAILY_REQUEST_LIMIT or used >= limit:
            return HttpResponse(_('The local test request limit has been reached.'), status=429)
        if site.images.count() >= MAX_SITE_IMAGES:
            return HttpResponse(_('This site has reached its local image-library limit.'), status=409)
        brief = form.cleaned_data['brief']
        scope = {'journal_site':site} if kind == 'journal' else {'portal_site':site}
        run = SiteImageGenerationRun.objects.create(id=run_id, requested_by=request.user, **scope,
            model_name=settings.IPS_OPENAI_IMAGE_MODEL, prompt_fingerprint=hashlib.sha256(brief.encode('utf-8')).hexdigest(),
            image_description=form.cleaned_data['description'], size=form.cleaned_data['size'],
            policy_revision=policy.revision, site_revision=site.revision, site_fingerprint=fingerprint)
        policy_snapshot = _policy_snapshot(policy, kind)
        _event(site, request.user, kind, 'image_ai_requested')

    # Only the manually entered brief, requested size, model and pseudonymous ID
    # are sent; the site, drafts, image library and description are not included.
    def still_allowed():
        try:
            actor = get_user_model().objects.get(pk=request.user.pk)
            fresh_owner, fresh_site = _site(actor, slug, kind)
            fresh_policy = _policy(fresh_owner, kind)
            return (_configured() and settings.IPS_OPENAI_IMAGE_MODEL == run.model_name and
                    _active(fresh_policy, kind) and _policy_snapshot(fresh_policy, kind) == policy_snapshot and
                    _current(fresh_site, run, kind))
        except (Http404, get_user_model().DoesNotExist):
            return False

    if not still_allowed():
        run.status, run.error_code, run.finished_at = run.Status.CANCELLED, 'authorization_changed', timezone.now()
        run.save(update_fields=['status','error_code','finished_at'])
        return redirect(_route(kind, 'result'), slug=slug, run_id=run.pk)
    identifier = hmac.new(settings.SECRET_KEY.encode('utf-8'), str(request.user.pk).encode('utf-8'), hashlib.sha256).hexdigest()
    try:
        image = image_ai_provider.generate_image(api_key=os.environ.get('OPENAI_API_KEY', ''),
            model=run.model_name, prompt=brief, size=run.size, safety_identifier=identifier)
    except Exception:
        run.status, run.error_code, run.finished_at = run.Status.FAILED, 'provider_unavailable', timezone.now()
        run.save(update_fields=['status','error_code','finished_at'])
    else:
        if not still_allowed():
            run.status, run.error_code = run.Status.CANCELLED, 'authorization_changed'
        else:
            run.status = run.Status.COMPLETED
            for key, value in image.items(): setattr(run, key, value)
        run.finished_at = timezone.now()
        run.save(update_fields=['status','error_code','finished_at','image_data','media_type','width','height','sha256'])
    return redirect(_route(kind, 'result'), slug=slug, run_id=run.pk)


@login_required
@never_cache
@require_GET
def result(request, slug, run_id, kind):
    _enabled()
    owner, site = _site(request.user, slug, kind)
    run = _run(site, request.user, run_id, kind)
    return _no_index(render(request, 'publishing/image_ai_result.html', {
        'owner':owner, 'site':site, 'run':run, 'current':_current(site, run, kind),
        'image_url':reverse(_route(kind, 'image'), args=[slug, run.pk]),
        'accept_url':reverse(_route(kind, 'accept'), args=[slug, run.pk]),
        'discard_url':reverse(_route(kind, 'discard'), args=[slug, run.pk]),
        'create_url':reverse(_route(kind, 'create'), args=[slug]),
        'history_url':reverse('journal_design_history' if kind == 'journal' else 'publisher_design_history', args=[slug]),
        'manage_url':reverse('manage_journal_site' if kind == 'journal' else 'manage_publisher_portal', args=[slug]) + '#site-image-library',
    }))


@login_required
@never_cache
@require_GET
def image(request, slug, run_id, kind):
    _enabled()
    _, site = _site(request.user, slug, kind)
    run = _run(site, request.user, run_id, kind)
    candidate = run.accepted_asset if run.accepted_at else run
    if run.status != run.Status.COMPLETED or not _intact(candidate):
        raise Http404
    if run.accepted_at and not site.images.filter(pk=candidate.pk).exists():
        raise Http404
    return site_image_views._image_response(candidate)


@login_required
@never_cache
@require_POST
def accept(request, slug, run_id, kind):
    _enabled()
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Confirm adding this image to the library.'), status=400)
    with transaction.atomic():
        owner, site = _site(request.user, slug, kind, locked=True)
        run = _run(site, request.user, run_id, kind, locked=True)
        if run.accepted_at:
            return redirect(_route(kind, 'result'), slug=slug, run_id=run.pk)
        if not _current(site, run, kind):
            return HttpResponse(_('The site draft changed; request a new image.'), status=409)
        if run.status != run.Status.COMPLETED or not _intact(run):
            return HttpResponse(_('The selected image is unavailable or damaged.'), status=409)
        if site.images.count() >= MAX_SITE_IMAGES:
            return HttpResponse(_('This site has reached its local image-library limit.'), status=409)
        scope = {'journal_site':site} if kind == 'journal' else {'portal_site':site}
        asset = SiteImageAsset.objects.create(**scope, uploaded_by=request.user, default_alt=run.image_description,
            image_data=bytes(run.image_data), media_type=run.media_type, width=run.width, height=run.height, sha256=run.sha256)
        run.accepted_asset = asset
        run.accepted_at, run.image_data = timezone.now(), None
        run.save(update_fields=['accepted_asset','accepted_at','image_data'])
        _event(site, request.user, kind, 'image_ai_saved_to_library')
    return redirect(_route(kind, 'result'), slug=slug, run_id=run.pk)


@login_required
@never_cache
@require_POST
def discard(request, slug, run_id, kind):
    _enabled()
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Confirm discarding this candidate.'), status=400)
    with transaction.atomic():
        owner, site = _site(request.user, slug, kind, locked=True)
        run = _run(site, request.user, run_id, kind, locked=True)
        if run.status == run.Status.DISCARDED:
            return redirect(_route(kind, 'result'), slug=slug, run_id=run.pk)
        if run.accepted_at or run.status != run.Status.COMPLETED:
            return HttpResponse(_('This candidate can no longer be discarded.'), status=409)
        run.image_data, run.status = None, run.Status.DISCARDED
        run.save(update_fields=['image_data','status'])
        _event(site, request.user, kind, 'image_ai_discarded')
    return redirect(_route(kind, 'result'), slug=slug, run_id=run.pk)
