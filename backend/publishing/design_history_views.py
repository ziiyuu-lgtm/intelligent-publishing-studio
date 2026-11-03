"""Requester-scoped, read-only history of journal and publisher design attempts."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import BooleanField, Case, CharField, Count, Q, Value, When
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .page_ai_provider import PROMPT_VERSION as PAGE
from .page_block_ai_provider import LEGACY_PROMPT_VERSION as OLD_BLOCK, PROMPT_VERSION as BLOCK
from .page_layout_ai_provider import PROMPT_VERSION as LAYOUT
from .portal_ai_modules import PROMPT_VERSION as PORTAL_MODULE
from .production_views import _enabled, _no_index
from .site_ai_modules import PROMPT_VERSION as JOURNAL_MODULE
from .site_ai_provider import LEGACY_PROMPT_VERSION as OLD_VISUAL, PROMPT_VERSION as VISUAL, PUBLISHER_PROMPT_VERSION as PORTAL_VISUAL
from .site_page_views import _fingerprint, _managed


def _categories(kind):
    return {
        'visual': (_('Visual design'), (VISUAL, OLD_VISUAL) if kind == 'journal' else (PORTAL_VISUAL,),
                   'site_ai_result' if kind == 'journal' else 'publisher_ai_result'),
        'modules': (_('Module arrangement'), (JOURNAL_MODULE,) if kind == 'journal' else (PORTAL_MODULE,),
                    'site_ai_module_result' if kind == 'journal' else 'publisher_ai_module_result'),
        'page': (_('Independent page'), (PAGE,), 'journal_page_ai_result' if kind == 'journal' else 'publisher_page_ai_result'),
        'blocks': (_('Page blocks'), (OLD_BLOCK, BLOCK), 'journal_page_block_ai_result' if kind == 'journal' else 'publisher_page_block_ai_result'),
        'layout': (_('Page layout'), (LAYOUT,), 'journal_page_layout_ai_result' if kind == 'journal' else 'publisher_page_layout_ai_result'),
    }


def _history(request, slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    categories = _categories(kind)
    known_versions = [version for _, versions, _ in categories.values() for version in versions]
    all_runs = site.ai_design_runs.filter(requested_by=request.user)
    image_runs = site.image_generation_runs.filter(requested_by=request.user)
    counts = all_runs.aggregate(total=Count('pk'), completed=Count('pk', filter=Q(status='completed')),
                                applied=Count('pk', filter=Q(applied_revision__isnull=False)))
    image_counts = image_runs.aggregate(total=Count('pk'), completed=Count('pk', filter=Q(status='completed')),
                                        added=Count('pk', filter=Q(accepted_at__isnull=False)))
    counts['total'] += image_counts['total']
    counts['completed'] += image_counts['completed']
    selected_type = request.GET.get('type', 'all')
    selected_status = request.GET.get('status', 'all')
    if selected_type not in (*categories.keys(), 'all', 'other', 'image'):
        selected_type = 'all'
    if selected_status not in ('all', 'pending', 'completed', 'failed', 'cancelled', 'discarded'):
        selected_status = 'all'

    runs = all_runs
    if selected_type == 'other':
        runs = runs.exclude(prompt_version__in=known_versions)
    elif selected_type in categories:
        runs = runs.filter(prompt_version__in=categories[selected_type][1])
    if selected_status != 'all':
        runs = runs.filter(status=selected_status)
        image_runs = image_runs.filter(status=selected_status)

    # Merge ordered identifiers in the database, then load only one page of
    # scoped records. Image bytes are never selected by the history view.
    design_keys = runs.order_by().annotate(source=Value('design', output_field=CharField())).values('id', 'created_at', 'source')
    image_keys = image_runs.order_by().annotate(source=Value('image', output_field=CharField())).values('id', 'created_at', 'source')
    if selected_type == 'all':
        keys = design_keys.union(image_keys, all=True)
    elif selected_type == 'image':
        keys = image_keys
    else:
        keys = design_keys
    keys = keys.order_by('-created_at', '-id', 'source')
    page = Paginator(keys, 12).get_page(request.GET.get('page'))
    page_keys = list(page.object_list)
    design_ids = [key['id'] for key in page_keys if key['source'] == 'design']
    image_ids = [key['id'] for key in page_keys if key['source'] == 'image']
    designs = runs.only('id', 'site_id', 'requested_by_id', 'prompt_version', 'status', 'site_revision',
                        'site_fingerprint', 'target_page_slug', 'created_at', 'applied_revision').in_bulk(design_ids)
    images = image_runs.defer('image_data').annotate(has_candidate=Case(
        When(image_data__isnull=False, then=Value(True)), default=Value(False), output_field=BooleanField())).in_bulk(image_ids)
    fingerprint = _fingerprint(site, kind)
    rows = []
    for key in page_keys:
        run = images.get(key['id']) if key['source'] == 'image' else designs.get(key['id'])
        if run is None:  # A record might have been removed after the page query.
            continue
        if key['source'] == 'image':
            image_route = 'journal_image_ai_' if kind == 'journal' else 'portal_image_ai_'
            available_image = run.status == 'completed' and (run.has_candidate or run.accepted_asset_id)
            rows.append({'run':run, 'category':'image', 'label':_('AI image'),
                         'result_url':reverse(image_route + 'result', args=[slug, run.pk]),
                         'image_url':reverse(image_route + 'image', args=[slug, run.pk]) if available_image else None,
                         'same_snapshot':run.site_revision == site.revision and run.site_fingerprint == fingerprint})
            continue
        category = next(((name, definition) for name, definition in categories.items() if run.prompt_version in definition[1]), None)
        name, definition = category if category else ('other', (_('Other design request'), (), None))
        label, versions, route = definition
        rows.append({'run':run, 'category':name, 'label':label,
                     'result_url':reverse(route, args=[slug, run.pk]) if route else None,
                     'same_snapshot':run.site_revision == site.revision and run.site_fingerprint == fingerprint})
    return _no_index(render(request, 'publishing/design_history.html', {
        'owner':owner, 'site':site, 'kind':kind, 'page':page, 'rows':rows, 'counts':counts,
        'saved_images_count':image_counts['added'], 'selected_type':selected_type, 'selected_status':selected_status,
        'image_create_url':reverse('journal_image_ai_create' if kind == 'journal' else 'portal_image_ai_create', args=[slug]),
        'manage_route':'manage_journal_site' if kind == 'journal' else 'manage_publisher_portal',
    }))


@login_required
@never_cache
@require_GET
def journal_history(request, slug):
    return _history(request, slug, 'journal')


@login_required
@never_cache
@require_GET
def publisher_history(request, slug):
    return _history(request, slug, 'publisher')
