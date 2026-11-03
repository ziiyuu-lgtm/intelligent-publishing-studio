"""Private visual grid editing for an existing fictional site page."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .ai_provider import ProviderError
from .models import JournalSiteEvent
from .module_images import images_valid
from .page_layout_ai_forms import LayoutBlockFormSet, LayoutSettingsForm
from .page_layout_ai_provider import describe_blocks, materialize_layout, validate_options
from .portal_views import _event as portal_event
from .production_views import _enabled, _no_index
from .site_page_block_views import _page as managed_page
from .site_page_views import _fingerprint, _managed, _page as render_page

SALT = 'ips.manual-page-layout.v1'
MAX_TOKEN_AGE = 86400


def _route(kind, action=''):
    return ('journal' if kind == 'journal' else 'publisher') + '_manual_layout' + ('_' + action if action else '')


def _initial(descriptors, reviewed=None):
    placements = {block['id']:(i + 1, block['grid_span']) for i, block in enumerate(reviewed['blocks'])} if reviewed else {}
    return [{'block_id':d['id'], 'order':placements.get(d['id'], (i + 1, d['grid_span']))[0],
             'grid_span':placements.get(d['id'], (i + 1, d['grid_span']))[1]} for i, d in enumerate(descriptors)]


def _render(request, owner, site, page, kind, settings_form=None, formset=None, reviewed=None, status=200):
    blocks = page.get('blocks', [])
    descriptors = describe_blocks(blocks) if blocks else []
    if descriptors:
        if settings_form is None:
            layout = reviewed if reviewed else page.get('block_layout', {'gap':24, 'alignment':'start'})
            settings_form = LayoutSettingsForm(prefix='settings', initial=layout)
        if formset is None:
            formset = LayoutBlockFormSet(prefix='blocks', initial=_initial(descriptors, reviewed), descriptors=descriptors)
        rows = [{'form':form, 'label':blocks[i]['heading'] or blocks[i]['kind'], 'visible':blocks[i]['visible']}
                for i, form in enumerate(formset)]
    else:
        rows = []
    return _no_index(render(request, 'publishing/manual_layout_editor.html', {
        'owner':owner, 'site':site, 'page':page, 'kind':kind, 'settings_form':settings_form, 'formset':formset,
        'rows':rows, 'fingerprint':_fingerprint(site, kind), 'previously_enabled':bool(page.get('block_layout')),
        'manage_route':'manage_journal_page_blocks' if kind == 'journal' else 'manage_publisher_page_blocks',
        'preview_route':_route(kind, 'preview'),
    }, status=status))


def _token_context(user, site, page_slug, kind, proposal):
    return {'user':str(user.pk), 'site':str(site.pk), 'kind':kind, 'page':page_slug,
            'revision':site.revision, 'fingerprint':_fingerprint(site, kind), 'layout':proposal}


def _decode(raw, user, site, page, kind):
    if not isinstance(raw, str) or len(raw) > 20000:
        raise ValueError('invalid token')
    try:
        data = signing.loads(raw, salt=SALT, max_age=MAX_TOKEN_AGE)
        if not isinstance(data, dict) or set(data) != {'user','site','kind','page','revision','fingerprint','layout'}:
            raise ValueError('invalid token')
        if (data['user'], data['site'], data['kind'], data['page']) != (str(user.pk), str(site.pk), kind, page['slug']):
            raise ValueError('wrong target')
        if data['revision'] != site.revision or data['fingerprint'] != _fingerprint(site, kind):
            raise RuntimeError('stale')
        return validate_options([data['layout']], describe_blocks(page.get('blocks', [])))[0]
    except (signing.BadSignature, ProviderError, TypeError, KeyError) as exc:
        raise ValueError('invalid token') from exc


def _stale():
    return HttpResponse(_('The site draft changed; reload the layout designer.'), status=409)


def _invalid():
    return HttpResponse(_('The reviewed layout is invalid or expired.'), status=400)


@login_required
@never_cache
@require_GET
def editor(request, slug, page_slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    current_pages, page = managed_page(site, kind, page_slug)
    return _render(request, owner, site, page, kind)


@login_required
@never_cache
@require_POST
def preview(request, slug, page_slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    current_pages, page = managed_page(site, kind, page_slug)
    if request.POST.get('revision') != str(site.revision) or request.POST.get('fingerprint') != _fingerprint(site, kind):
        return _stale()
    blocks = page.get('blocks', [])
    if not blocks:
        return HttpResponse(_('Add a content block before designing the layout.'), status=409)
    descriptors = describe_blocks(blocks)
    count = str(len(descriptors))
    if request.POST.get('blocks-INITIAL_FORMS') != count or request.POST.get('blocks-TOTAL_FORMS') != count:
        return _stale()
    settings_form = LayoutSettingsForm(request.POST, prefix='settings')
    formset = LayoutBlockFormSet(request.POST, prefix='blocks', initial=_initial(descriptors), descriptors=descriptors)
    settings_valid, formset_valid = settings_form.is_valid(), formset.is_valid()
    if not settings_valid or not formset_valid:
        return _render(request, owner, site, page, kind, settings_form, formset, status=400)
    proposal = {**settings_form.cleaned_data, 'blocks':formset.placements}
    token = signing.dumps(_token_context(request.user, site, page_slug, kind, proposal), salt=SALT, compress=True)
    return render_page(request, slug, page_slug, kind, True, expected=(site.revision, _fingerprint(site, kind)),
        layout_suggestion=proposal, manual_layout_preview=True, ai_revision=site.revision, layout_reviewed=token,
        layout_apply_url=reverse(_route(kind, 'apply'), args=[slug, page_slug]),
        layout_edit_url=reverse(_route(kind, 'edit'), args=[slug, page_slug]))


@login_required
@never_cache
@require_POST
def edit(request, slug, page_slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    current_pages, page = managed_page(site, kind, page_slug)
    if request.POST.get('revision') != str(site.revision):
        return _stale()
    try:
        reviewed = _decode(request.POST.get('reviewed_layout', ''), request.user, site, page, kind)
    except RuntimeError:
        return _stale()
    except ValueError:
        return _invalid()
    return _render(request, owner, site, page, kind, reviewed=reviewed)


@login_required
@never_cache
@require_POST
def apply(request, slug, page_slug, kind):
    _enabled()
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Confirm applying this option to the draft.'), status=400)
    with transaction.atomic():
        actor = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        owner, site = _managed(actor, slug, kind, locked=True)
        current_pages, page = managed_page(site, kind, page_slug)
        if request.POST.get('revision') != str(site.revision):
            return _stale()
        try:
            reviewed = _decode(request.POST.get('reviewed_layout', ''), actor, site, page, kind)
            candidate = materialize_layout(page, reviewed)
        except RuntimeError:
            return _stale()
        except (ValueError, ProviderError):
            return _invalid()
        pages = [candidate if entry['slug'] == page_slug else entry for entry in site.pages_config]
        if not images_valid(site, site.sections_config, owner=site, pages=pages):
            return HttpResponse(_('The site draft or its images are invalid.'), status=409)
        if pages != site.pages_config:
            site.pages_config = pages
            site.revision += 1
            site.save(update_fields=['pages_config','revision'])
            if kind == 'journal':
                JournalSiteEvent.objects.create(site=site, actor=actor, action='site_manual_layout_applied', revision=site.revision)
            else:
                portal_event(site, actor, 'portal_manual_layout_applied')
    messages.success(request, _('The reviewed layout was applied to the draft. Publishing remains separate.'))
    return redirect('manage_journal_page_blocks' if kind == 'journal' else 'manage_publisher_page_blocks',
                    slug=slug, page_slug=page_slug)
