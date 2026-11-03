"""Private, reviewed customisation of an AI appearance suggestion."""
import hashlib
import json
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import portal_ai_views, portal_views, site_ai_views, site_views
from .forms import JournalAppearanceForm
from .models import JournalSiteEvent
from .portal_forms import PortalAppearanceForm
from .production_views import _enabled, _no_index
from .site_ai_provider import LEGACY_PROMPT_VERSION
from .site_design import InvalidDesign, appearance_for, legacy_to_blueprint, validate_blueprint

SALT = 'ips.visual-ai.review.v1'
MAX_TOKEN_AGE = 86400


def _routes(kind):
    prefix = 'journal' if kind == 'journal' else 'publisher'
    return {action: prefix + '_visual_ai_' + action for action in ('customise', 'preview', 'edit', 'apply')}


def _resolve(request, slug, run_id, index, kind, locked=False):
    _enabled()
    if kind == 'journal':
        owner = site_views._managed_journal(request.user, slug, locked=locked)
        site = site_views._site(owner, locked=locked)
        run = site_ai_views._run(owner, request.user, run_id, locked=locked)
        options = site_ai_views._options(run)
        current = site_ai_views._current(site, run)
        fingerprint = site_views._fingerprint(site)
    else:
        owner = portal_views._organisation(request.user, slug, locked=locked)
        site = portal_views._site(owner, locked=locked)
        run = portal_ai_views._run(owner, request.user, run_id, locked=locked)
        options = portal_ai_views._options(run)
        current = portal_ai_views._current(site, run)
        fingerprint = portal_views._fingerprint(site)
    if run.site_id != site.pk or index < 0 or index >= len(options):
        raise Http404
    if not current or run.applied_revision is not None:
        return owner, site, run, None, fingerprint
    option = options[index]
    if kind == 'journal' and run.prompt_version == LEGACY_PROMPT_VERSION:
        option = legacy_to_blueprint(option['theme'], option['layout'])
    return owner, site, run, option, fingerprint


def _stale():
    return HttpResponse(_('The site draft changed or this suggestion was already applied.'), status=409)


def _editor(request, owner, site, run, index, kind, option, form=None, status=200):
    form_class = JournalAppearanceForm if kind == 'journal' else PortalAppearanceForm
    if form is None:
        form = form_class(initial=option)
    return _no_index(render(request, 'publishing/visual_ai_customise.html', {
        'owner':owner, 'site':site, 'run':run, 'index':index, 'kind':kind, 'form':form,
        'appearance':appearance_for(option), 'routes':_routes(kind),
        'result_url':reverse('site_ai_result' if kind == 'journal' else 'publisher_ai_result', args=[owner.slug, run.pk]),
    }, status=status))


def _option_fingerprint(option):
    return hashlib.sha256(json.dumps(option, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _token(user, site, run, index, kind, fingerprint, option, design):
    return signing.dumps({'user':str(user.pk), 'site':str(site.pk), 'run':str(run.pk), 'index':index,
                          'kind':kind, 'revision':site.revision, 'fingerprint':fingerprint,
                          'source':_option_fingerprint(option), 'design':design},
                         salt=SALT, compress=True)


def _decode(raw, user, site, run, index, kind, fingerprint, option):
    if not isinstance(raw, str) or len(raw) > 10000:
        raise ValueError('invalid review')
    try:
        value = signing.loads(raw, salt=SALT, max_age=MAX_TOKEN_AGE)
        if not isinstance(value, dict) or set(value) != {'user','site','run','index','kind','revision','fingerprint','source','design'}:
            raise ValueError('invalid review')
        if (value['user'], value['site'], value['run'], value['index'], value['kind'],
            value['revision'], value['fingerprint'], value['source']) != (str(user.pk), str(site.pk), str(run.pk), index,
                kind, site.revision, fingerprint, _option_fingerprint(option)):
            raise ValueError('wrong review')
        return validate_blueprint(value['design'])
    except (signing.BadSignature, InvalidDesign, KeyError, TypeError) as exc:
        raise ValueError('invalid review') from exc


def _invalid():
    return HttpResponse(_('The reviewed design is invalid or expired.'), status=400)


@login_required
@never_cache
@require_GET
def customise(request, slug, run_id, index, kind):
    owner, site, run, option, fingerprint = _resolve(request, slug, run_id, index, kind)
    if option is None:
        return _stale()
    return _editor(request, owner, site, run, index, kind, option)


@login_required
@never_cache
@require_POST
def preview(request, slug, run_id, index, kind):
    owner, site, run, option, fingerprint = _resolve(request, slug, run_id, index, kind)
    if option is None or request.POST.get('revision') != str(site.revision):
        return _stale()
    form_class = JournalAppearanceForm if kind == 'journal' else PortalAppearanceForm
    form = form_class(request.POST)
    if not form.is_valid():
        return _editor(request, owner, site, run, index, kind, option, form=form, status=400)
    design_config = form.cleaned_data
    reviewed = _token(request.user, site, run, index, kind, fingerprint, option, design_config)
    routes = _routes(kind)
    extra = {'preview':True, 'ai_custom_preview':True, 'visual_reviewed':reviewed,
             'visual_revision':site.revision,
             'visual_apply_url':reverse(routes['apply'], args=[owner.slug, run.pk, index]),
             'visual_edit_url':reverse(routes['edit'], args=[owner.slug, run.pk, index])}
    if kind == 'journal':
        design = SimpleNamespace(**{field:getattr(site, field) for field in site_views.SITE_FIELDS})
        design.design_config = design_config
        return site_views._render_site(request, owner, design, **extra)
    design = SimpleNamespace(**{field:getattr(site, field) for field in portal_views.CONTENT_FIELDS},
        design_config=design_config, sections_config=site.sections_config, pages_config=site.pages_config,
        hero_asset_id=site.hero_asset_id, hero_alt=site.hero_alt,
        hero_fit=site.hero_fit, hero_position=site.hero_position)
    return portal_views._render(request, owner, design, portal_views._draft_journals(site), **extra)


@login_required
@never_cache
@require_POST
def edit(request, slug, run_id, index, kind):
    owner, site, run, option, fingerprint = _resolve(request, slug, run_id, index, kind)
    if option is None or request.POST.get('revision') != str(site.revision):
        return _stale()
    try:
        design = _decode(request.POST.get('reviewed_design', ''), request.user, site, run, index, kind, fingerprint, option)
    except ValueError:
        return _invalid()
    return _editor(request, owner, site, run, index, kind, design)


@login_required
@never_cache
@require_POST
def apply(request, slug, run_id, index, kind):
    _enabled()
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Confirm applying this option to the draft.'), status=400)
    with transaction.atomic():
        owner, site, run, option, fingerprint = _resolve(request, slug, run_id, index, kind, locked=True)
        if option is None or request.POST.get('revision') != str(site.revision):
            return _stale()
        try:
            design = _decode(request.POST.get('reviewed_design', ''), request.user, site, run, index, kind, fingerprint, option)
        except ValueError:
            return _invalid()
        if site.design_config != design:
            site.design_config = design
            site.revision += 1
            site.save(update_fields=['design_config', 'revision'])
        run.applied_option, run.applied_revision, run.applied_option_content = index, site.revision, design
        run.save(update_fields=['applied_option','applied_revision','applied_option_content'])
        if kind == 'journal':
            JournalSiteEvent.objects.create(site=site, actor=request.user, action='site_ai_option_applied', revision=site.revision)
        else:
            portal_views._event(site, request.user, 'portal_ai_option_applied')
    messages.success(request, _('Suggestion applied to the draft. Publish separately after review.'))
    return redirect('manage_journal_site' if kind == 'journal' else 'manage_publisher_portal', slug=slug)
