"""Opt-in, private image generation and human review for fictional sites."""
import hashlib
import hmac
import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import ai_views, image_ai_provider, portal_policy_views, portal_views, site_image_views, site_page_views, site_views
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since, portal_requests_since
from .image_ai_forms import ImageGenerationForm
from .models import HERO_FIT_CHOICES, HERO_POSITION_CHOICES, JournalSiteEvent, SiteImageAsset, SiteImageGenerationRun
from .module_images import sync_image_references
from .production_views import _enabled, _no_index
from .site_images import MAX_SITE_IMAGES, asset_is_intact, valid_display

IMAGE_PREVIEW_SALT = 'ips.image-ai.site-preview.v1'
IMAGE_PREVIEW_MAX_AGE = 3600
IMAGE_PAGE_PREVIEW_SALT = 'ips.image-ai.page-preview.v1'


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


def _candidate(site, run):
    candidate = run.accepted_asset if run.accepted_at else run
    if run.status != run.Status.COMPLETED or not _intact(candidate):
        return None
    if run.accepted_at and not site.images.filter(pk=candidate.pk).exists():
        return None
    return candidate


def _review_token(user, site, run, kind, candidate, fit, position):
    return signing.dumps({'user':str(user.pk), 'site':str(site.pk), 'run':str(run.pk), 'kind':kind,
        'revision':site.revision, 'fingerprint':_fingerprint(site, kind), 'sha256':candidate.sha256,
        'description':run.image_description, 'asset_id':str(run.accepted_asset_id or ''),
        'accepted':bool(run.accepted_at), 'fit':fit, 'position':position},
        salt=IMAGE_PREVIEW_SALT, compress=True)


def _read_review(raw):
    keys = {'user','site','run','kind','revision','fingerprint','sha256','description',
            'asset_id','accepted','fit','position'}
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError('invalid review')
    try:
        value = signing.loads(raw, salt=IMAGE_PREVIEW_SALT, max_age=IMAGE_PREVIEW_MAX_AGE)
    except signing.BadSignature as exc:
        raise ValueError('invalid review') from exc
    if (not isinstance(value, dict) or set(value) != keys or type(value['accepted']) is not bool or
            not valid_display(value['fit'], value['position']) or
            not isinstance(value['description'], str) or not value['description'].strip() or
            len(value['description']) > 200):
        raise ValueError('invalid review')
    return value


def _page_image_target(value, page):
    """Resolve a visible block by its snapshot-bound index, or the main image."""
    if value == 'main':
        return None
    if not isinstance(value, str) or not re.fullmatch(r'block-(0|[1-9][0-9]?)', value):
        raise ValueError('invalid target')
    index = int(value[6:])
    blocks = page.get('blocks', [])
    if index >= len(blocks) or not blocks[index]['visible']:
        raise ValueError('unavailable target')
    return index


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
    current = _current(site, run, kind)
    return _no_index(render(request, 'publishing/image_ai_result.html', {
        'owner':owner, 'site':site, 'run':run, 'current':current,
        'image_url':reverse(_route(kind, 'image'), args=[slug, run.pk]),
        'site_preview_url':reverse(_route(kind, 'site_preview'), args=[slug, run.pk]) if _candidate(site, run) and (current or run.accepted_at) else None,
        'page_preview_url':reverse(_route(kind, 'page_preview'), args=[slug, run.pk]) if _candidate(site, run) and (current or run.accepted_at) else None,
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
    candidate = _candidate(site, run)
    if candidate is None:
        raise Http404
    return site_image_views._image_response(candidate)


@login_required
@never_cache
@require_GET
def site_preview(request, slug, run_id, kind):
    """Preview one private candidate on the saved site without editing it."""
    _enabled()
    owner, site = _site(request.user, slug, kind)
    run = _run(site, request.user, run_id, kind)
    candidate = _candidate(site, run)
    if candidate is None:
        raise Http404
    if not run.accepted_at and not _current(site, run, kind):
        return HttpResponse(_('The site draft changed; request a new image.'), status=409)
    fit, position = request.GET.get('fit', 'cover'), request.GET.get('position', 'center')
    if not valid_display(fit, position):
        return HttpResponse(_('Invalid image display settings.'), status=400)
    context = {'preview':True, 'image_candidate_preview':True,
               'candidate_image_url':reverse(_route(kind, 'image'), args=[slug, run.pk]),
               'candidate_back_url':reverse(_route(kind, 'result'), args=[slug, run.pk]),
               'candidate_alt':run.image_description, 'candidate_fit':fit, 'candidate_position':position,
               'candidate_fit_label':dict(HERO_FIT_CHOICES)[fit],
               'candidate_position_label':dict(HERO_POSITION_CHOICES)[position],
               'candidate_reviewed':_review_token(request.user, site, run, kind, candidate, fit, position),
               'candidate_apply_url':reverse(_route(kind, 'apply_preview'), args=[slug, run.pk]),
               'hero_fit_choices':HERO_FIT_CHOICES, 'hero_position_choices':HERO_POSITION_CHOICES}
    if kind == 'journal':
        return site_views._render_site(request, owner, site, **context)
    return portal_views._render(request, owner, site, portal_views._draft_journals(site), **context)


@login_required
@never_cache
@require_GET
def page_preview(request, slug, run_id, kind):
    """Show a candidate on one validated independent page without saving it."""
    _enabled()
    owner, site = _site(request.user, slug, kind)
    run = _run(site, request.user, run_id, kind)
    candidate = _candidate(site, run)
    if candidate is None:
        raise Http404
    if not run.accepted_at and not _current(site, run, kind):
        return HttpResponse(_('The site draft changed; request a new image.'), status=409)
    fit, position = request.GET.get('fit', 'cover'), request.GET.get('position', 'center')
    if not valid_display(fit, position):
        return HttpResponse(_('Invalid image display settings.'), status=400)
    pages = site_page_views._valid_design(site, site, kind, site)
    if pages is None:
        raise Http404
    back_url = reverse(_route(kind, 'result'), args=[slug, run.pk])
    if not pages:
        return _no_index(render(request, 'publishing/image_ai_no_pages.html', {
            'back_url':back_url, 'manage_pages_url':reverse(
                'manage_journal_pages' if kind == 'journal' else 'manage_publisher_pages', args=[slug]),
        }))
    page_slug = request.GET.get('page', pages[0]['slug'])
    page = next((page for page in pages if page['slug'] == page_slug), None)
    if page is None:
        raise Http404
    # The placement list belongs to the previously displayed page. Reset it
    # when a different page is selected, including without JavaScript.
    target = request.GET.get('target', 'main')
    if request.GET.get('from_page', page_slug) != page_slug:
        target = 'main'
    try:
        target_index = _page_image_target(target, page)
    except ValueError:
        return HttpResponse(_('The image placement is unavailable; update the preview.'), status=400)
    targets = [{'value':'main', 'label':_('Page main image')}]
    for index, block in enumerate(page.get('blocks', [])):
        if block['visible']:
            label = block['heading'] or _('Untitled block')
            targets.append({'value':f'block-{index}', 'label':_('Block %(number)s: %(heading)s') % {
                'number':index + 1, 'heading':label}})
    target_label = next(item['label'] for item in targets if item['value'] == target)
    reviewed = signing.dumps({'page':page_slug, 'target':target,
        'image':_review_token(request.user, site, run, kind, candidate, fit, position)},
        salt=IMAGE_PAGE_PREVIEW_SALT, compress=True)
    context = {'candidate_image_url':reverse(_route(kind, 'image'), args=[slug, run.pk]),
        'candidate_back_url':back_url, 'candidate_alt':run.image_description,
        'candidate_fit':fit, 'candidate_position':position,
        'candidate_fit_label':dict(HERO_FIT_CHOICES)[fit],
        'candidate_position_label':dict(HERO_POSITION_CHOICES)[position],
        'candidate_reviewed':reviewed,
        'candidate_apply_url':reverse(_route(kind, 'apply_page_preview'), args=[slug, run.pk]),
        'candidate_pages':pages, 'candidate_page_slug':page_slug,
        'candidate_targets':targets, 'candidate_target':target, 'candidate_target_index':target_index,
        'candidate_target_label':target_label,
        'hero_fit_choices':HERO_FIT_CHOICES, 'hero_position_choices':HERO_POSITION_CHOICES}
    return site_page_views._page(request, slug, page_slug, kind, True,
        expected=(site.revision, _fingerprint(site, kind)), image_candidate_page=context)


@login_required
@never_cache
@require_POST
def apply_page_preview(request, slug, run_id, kind):
    """Apply only the reviewed image, crop, focus and target page to the draft."""
    _enabled()
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Confirm applying the reviewed image to the draft.'), status=400)
    try:
        raw = request.POST.get('reviewed_image', '')
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError('invalid review')
        review = signing.loads(raw, salt=IMAGE_PAGE_PREVIEW_SALT, max_age=IMAGE_PREVIEW_MAX_AGE)
        if (not isinstance(review, dict) or set(review) not in ({'page', 'image'}, {'page', 'image', 'target'}) or
                not isinstance(review['page'], str) or len(review['page']) > 60):
            raise ValueError('invalid review')
        reviewed = _read_review(review['image'])
    except (ValueError, TypeError, signing.BadSignature):
        return HttpResponse(_('The image preview is invalid or expired.'), status=400)
    with transaction.atomic():
        owner, site = _site(request.user, slug, kind, locked=True)
        run = _run(site, request.user, run_id, kind, locked=True)
        if run.status != run.Status.COMPLETED:
            return HttpResponse(_('The image is unavailable; open the request again.'), status=409)
        candidate = (site.images.select_for_update().filter(pk=run.accepted_asset_id).first()
                     if run.accepted_at else run)
        if not _intact(candidate):
            return HttpResponse(_('The image is unavailable; open the request again.'), status=409)
        expected = (str(request.user.pk), str(site.pk), str(run.pk), kind, site.revision,
                    _fingerprint(site, kind), candidate.sha256, run.image_description,
                    str(run.accepted_asset_id or ''), bool(run.accepted_at))
        received = tuple(reviewed[key] for key in ('user', 'site', 'run', 'kind', 'revision',
                         'fingerprint', 'sha256', 'description', 'asset_id', 'accepted'))
        if received != expected or (not run.accepted_at and not _current(site, run, kind)):
            return HttpResponse(_('The image or site draft changed; preview it again.'), status=409)
        pages = site_page_views._valid_design(site, site, kind, site)
        page = next((item for item in pages if item['slug'] == review['page']), None) if pages is not None else None
        if page is None:
            return HttpResponse(_('The page or site draft changed; preview it again.'), status=409)
        try:
            # Previous one-hour tokens without a target referred to the main image.
            target_index = _page_image_target(review.get('target', 'main'), page)
        except ValueError:
            return HttpResponse(_('The image placement is unavailable; update the preview.'), status=409)
        if not run.accepted_at:
            if site.images.count() >= MAX_SITE_IMAGES:
                return HttpResponse(_('This site has reached its local image-library limit.'), status=409)
            scope = {'journal_site':site} if kind == 'journal' else {'portal_site':site}
            candidate = SiteImageAsset.objects.create(**scope, uploaded_by=request.user, default_alt=run.image_description,
                image_data=bytes(run.image_data), media_type=run.media_type, width=run.width, height=run.height, sha256=run.sha256)
            run.accepted_asset, run.accepted_at, run.image_data = candidate, timezone.now(), None
            run.save(update_fields=['accepted_asset', 'accepted_at', 'image_data'])
            _event(site, request.user, kind, 'image_ai_saved_to_library')
        image_fields = {'image_asset_id':str(candidate.pk), 'image_alt':reviewed['description'],
                        'image_fit':reviewed['fit'], 'image_position':reviewed['position']}
        if target_index is None:
            updated_page = {**page, **image_fields}
        else:
            updated_page = {**page, 'blocks':[
                {**block, **image_fields} if index == target_index else block
                for index, block in enumerate(page['blocks'])]}
        new_pages = [updated_page if item['slug'] == review['page'] else item for item in pages]
        if site.pages_config != new_pages:
            sync_image_references(site, site, site.sections_config, pages=new_pages)
            site.pages_config = new_pages
            site.revision += 1
            site.save(update_fields=['pages_config', 'revision'])
            if kind == 'journal':
                JournalSiteEvent.objects.create(site=site, actor=request.user, action='site_pages_updated', revision=site.revision)
            else:
                portal_views._event(site, request.user, 'portal_pages_updated')
    messages.success(request, _('The reviewed image was applied to the draft page. Publish separately after review.'))
    return redirect('manage_journal_pages' if kind == 'journal' else 'manage_publisher_pages', slug=slug)


@login_required
@never_cache
@require_POST
def apply_preview(request, slug, run_id, kind):
    """Apply exactly the reviewed image and display settings to the local draft."""
    _enabled()
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Confirm applying the reviewed image to the draft.'), status=400)
    try:
        reviewed = _read_review(request.POST.get('reviewed_image', ''))
    except (ValueError, TypeError):
        return HttpResponse(_('The image preview is invalid or expired.'), status=400)
    with transaction.atomic():
        owner, site = _site(request.user, slug, kind, locked=True)
        run = _run(site, request.user, run_id, kind, locked=True)
        if run.status != run.Status.COMPLETED:
            return HttpResponse(_('The image is unavailable; open the request again.'), status=409)
        if run.accepted_at:
            candidate = site.images.select_for_update().filter(pk=run.accepted_asset_id).first()
        else:
            candidate = run
        if not _intact(candidate):
            return HttpResponse(_('The image is unavailable; open the request again.'), status=409)
        expected = (str(request.user.pk), str(site.pk), str(run.pk), kind, site.revision,
                    _fingerprint(site, kind), candidate.sha256, run.image_description,
                    str(run.accepted_asset_id or ''), bool(run.accepted_at))
        received = tuple(reviewed[key] for key in ('user','site','run','kind','revision',
                         'fingerprint','sha256','description','asset_id','accepted'))
        if received != expected or (not run.accepted_at and not _current(site, run, kind)):
            return HttpResponse(_('The image or site draft changed; preview it again.'), status=409)
        if not run.accepted_at:
            if site.images.count() >= MAX_SITE_IMAGES:
                return HttpResponse(_('This site has reached its local image-library limit.'), status=409)
            scope = {'journal_site':site} if kind == 'journal' else {'portal_site':site}
            candidate = SiteImageAsset.objects.create(**scope, uploaded_by=request.user, default_alt=run.image_description,
                image_data=bytes(run.image_data), media_type=run.media_type, width=run.width, height=run.height, sha256=run.sha256)
            run.accepted_asset, run.accepted_at, run.image_data = candidate, timezone.now(), None
            run.save(update_fields=['accepted_asset','accepted_at','image_data'])
            _event(site, request.user, kind, 'image_ai_saved_to_library')
        if (site.hero_asset_id != candidate.pk or site.hero_alt != reviewed['description'] or
                site.hero_fit != reviewed['fit'] or site.hero_position != reviewed['position']):
            site.hero_asset, site.hero_alt = candidate, reviewed['description']
            site.hero_fit, site.hero_position = reviewed['fit'], reviewed['position']
            site.revision += 1
            site.save(update_fields=['hero_asset','hero_alt','hero_fit','hero_position','revision'])
            _event(site, request.user, kind, 'image_ai_hero_applied')
    messages.success(request, _('The reviewed image and display settings were applied to the draft. Publish separately after review.'))
    return redirect('manage_journal_site' if kind == 'journal' else 'manage_publisher_portal', slug=slug)


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
