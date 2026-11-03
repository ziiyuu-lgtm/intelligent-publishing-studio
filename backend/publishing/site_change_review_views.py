"""Private, read-only comparison of a saved site draft and current test release."""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import portal_views
from .production_views import _enabled, _no_index
from .site_design import InvalidDesign, appearance_for, legacy_to_blueprint
from .site_images import asset_is_intact, hero_reference_valid
from .module_images import IMAGE_FIELDS
from .models import HERO_FIT_CHOICES, HERO_POSITION_CHOICES
from .site_pages import validate_pages
from .page_blocks import KINDS as BLOCK_KINDS
from .site_text_diff import compare_page_text
from . import site_sections, portal_sections
from .site_page_views import _managed

TEXT_FIELDS = (('brand_name', _('Site name')), ('headline', _('Headline')),
               ('tagline', _('Tagline')), ('introduction', _('Introduction')))
HERO_FIELDS = ('hero_asset_id', 'hero_alt', 'hero_fit', 'hero_position')


def _hero(site, version, kind, slug):
    if not version or not version.hero_asset_id:
        return {'state':'empty', 'alt':'', 'image_url':None}
    try:
        valid = hero_reference_valid(site, version.hero_asset_id, version.hero_alt,
                                     version.hero_fit, version.hero_position)
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        return {'state':'unavailable', 'alt':version.hero_alt, 'image_url':None}
    route = 'private_journal_image' if kind == 'journal' else 'private_portal_image'
    return {'state':'available', 'alt':version.hero_alt,
            'image_url':reverse(route, args=[slug, version.hero_asset_id])}


def _count(value):
    return len(value) if isinstance(value, list) else None


def _section_count(version, kind):
    if version.sections_config == []:
        try:
            defaults = site_sections.default_sections(version) if kind == 'journal' else portal_sections.default_sections(version)
            return len(defaults)
        except (AttributeError, TypeError, ValueError):
            return None
    return _count(version.sections_config)


def _section_inventory(version, kind):
    """Show each version's order, without assuming modules have stable identities."""
    module = site_sections if kind == 'journal' else portal_sections
    try:
        raw = version.sections_config if version.sections_config != [] else module.default_sections(version)
        sections = module.validate_sections(raw)
        labels = dict(module.SECTION_CHOICES)
        return [{'position':index, 'title':item['heading'] or labels[item['kind']],
                 'type':labels[item['kind']], 'visible':item['visible']}
                for index, item in enumerate(sections, 1)]
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def _page_changes(before, after):
    """Match validated pages by URL name; renamed URLs appear as remove/add."""
    try:
        validate_pages(before)
        validate_pages(after)
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    old = {item['slug']:(index, item) for index, item in enumerate(before, 1)}
    new = {item['slug']:(index, item) for index, item in enumerate(after, 1)}
    ordered = [item['slug'] for item in after] + [item['slug'] for item in before if item['slug'] not in new]
    result = []

    def summary(entry):
        if entry is None:
            return None
        position, item = entry
        return {'position':position, 'title':item['title'], 'slug':item['slug'], 'visible':item['visible']}

    for slug in ordered:
        previous, current = old.get(slug), new.get(slug)
        if previous is None:
            status, fields = _('Added'), []
        elif current is None:
            status, fields = _('Removed'), []
        else:
            old_pos, old_page = previous
            new_pos, new_page = current
            fields = []
            for key, label in (('title', _('Title')), ('body', _('Page text')),
                               ('visible', _('Visibility')), ('blocks', _('Page blocks')),
                               ('design', _('Page design')), ('block_layout', _('Block layout'))):
                if old_page.get(key) != new_page.get(key):
                    fields.append(label)
            if any(old_page.get(key) != new_page.get(key) for key in IMAGE_FIELDS):
                fields.append(_('Page image'))
            if old_pos != new_pos:
                fields.append(_('Position'))
            if not fields:
                continue
            status = _('Changed')
        old_text = previous[1]['body'] if previous else ''
        new_text = current[1]['body'] if current else ''
        text_comparison = compare_page_text(old_text, new_text)
        if text_comparison is not None:
            text_comparison.update(before_present=previous is not None, after_present=current is not None,
                                   before_empty=not old_text, after_empty=not new_text)
        old_blocks = previous[1].get('blocks', []) if previous else []
        new_blocks = current[1].get('blocks', []) if current else []
        result.append({'status':status, 'fields':fields, 'before':summary(previous), 'after':summary(current),
                       'text_comparison':text_comparison, 'blocks_changed':old_blocks != new_blocks})
    return result


def _block_side(pages, page_slug):
    """Describe one validated page without pairing blocks or exposing image bytes."""
    try:
        validated = validate_pages(pages)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {'state':'unavailable'}
    page = next((item for item in validated if item['slug'] == page_slug), None)
    if page is None:
        return {'state':'absent'}
    kinds = dict(BLOCK_KINDS)
    blocks = []
    for index, block in enumerate(page.get('blocks', []), 1):
        blocks.append({'position':index, 'type':kinds[block['kind']],
                       'heading':block['heading'], 'body':block['body'], 'visible':block['visible'],
                       'link_label':block['link_label'], 'link_url':block['link_url'],
                       'image_alt':block.get('image_alt', ''),
                       'image_asset_id':block.get('image_asset_id'),
                       'image_fit_css':block.get('image_fit', ''),
                       'image_position_css':block.get('image_position', ''),
                       'image_fit':dict(HERO_FIT_CHOICES).get(block.get('image_fit'), ''),
                       'image_position':dict(HERO_POSITION_CHOICES).get(block.get('image_position'), ''),
                       'custom_style':bool(block.get('design')),
                       'grid_span':block.get('grid_span', 12) if page.get('block_layout') else None})
    return {'state':'present', 'title':page['title'], 'slug':page['slug'],
            'visible':page['visible'], 'blocks':blocks}


def _attach_block_images(site, kind, slug, versions):
    """Attach only intact images protected by each version's site-scoped references."""
    relevant = [(version, side) for version, side in versions if version is not None and side['state'] == 'present']
    ids = {block['image_asset_id'] for _, side in relevant for block in side['blocks'] if block['image_asset_id']}
    if not ids:
        return
    intact = set()
    assets = site.images.filter(pk__in=ids).only('id','image_data','media_type','width','height','sha256')
    for asset in assets.iterator(chunk_size=1):
        try:
            if asset_is_intact(asset):
                intact.add(str(asset.pk))
        except (TypeError, ValueError):
            continue
    image_route = 'private_journal_image' if kind == 'journal' else 'private_portal_image'
    for version, side in relevant:
        referenced = {str(value) for value in version.module_image_references.filter(asset_id__in=ids).values_list('asset_id', flat=True)}
        for block in side['blocks']:
            asset_id = block['image_asset_id']
            if not asset_id:
                continue
            if asset_id in referenced and asset_id in intact:
                block['image_state'] = 'available'
                block['image_url'] = reverse(image_route, args=[slug,asset_id])
            else:
                block['image_state'] = 'unavailable'


def _appearance(version, kind):
    if version is None:
        return None
    try:
        config = version.design_config
        if kind == 'journal' and not config:
            config = legacy_to_blueprint(version.theme, version.layout)
        return appearance_for(config)
    except (InvalidDesign, TypeError, ValueError):
        return None


def _changes(site, release, kind, owner):
    if release is None:
        return []
    changes = []
    for field, label in TEXT_FIELDS:
        before, after = getattr(release, field), getattr(site, field)
        if before != after:
            changes.append({'label':label, 'kind':'text', 'before':before, 'after':after})
    visual_fields = ('design_config', 'theme', 'layout') if kind == 'journal' else ('design_config',)
    if any(getattr(site, f) != getattr(release, f) for f in visual_fields):
        changes.append({'label':_('Visual design'), 'kind':'summary',
                        'summary':_('Colours, typography or layout settings have changed.')})
    if any(getattr(site, f) != getattr(release, f) for f in HERO_FIELDS):
        changes.append({'label':_('Hero image'), 'kind':'summary',
                        'summary':_('The image, description or display settings have changed.')})
    for field, label, unit in (('sections_config', _('Page modules'), _('modules')),
                               ('pages_config', _('Independent pages'), _('pages'))):
        before, after = getattr(release, field), getattr(site, field)
        before_items = _section_inventory(release, kind) if field == 'sections_config' else None
        after_items = _section_inventory(site, kind) if field == 'sections_config' else None
        # Empty legacy module lists derive their displayed order from the design.
        legacy_order_changed = (field == 'sections_config' and before == [] and after == []
                                and before_items != after_items)
        if before != after or legacy_order_changed:
            change = {'label':label, 'kind':'sections' if field == 'sections_config' else 'pages', 'unit':unit,
                      'before':_section_count(release, kind) if field == 'sections_config' else _count(before),
                      'after':_section_count(site, kind) if field == 'sections_config' else _count(after),
                      'summary':_('Content, order or visibility may have changed.')}
            if field == 'sections_config':
                change['before_items'] = before_items
                change['after_items'] = after_items
            else:
                change['page_changes'] = _page_changes(before, after)
            changes.append(change)
    if kind != 'journal':
        # These are the currently resolvable lists shown by the two portal views.
        current_journals = portal_views._draft_journals(site)
        released_journals = portal_views._snapshot_journals(owner, release.journals)
        if site.journal_ids != [item['id'] for item in release.journals] or current_journals != released_journals:
            changes.append({'label':_('Selected journals'), 'kind':'journals',
                            'before':released_journals, 'after':current_journals,
                            'summary':_('The visible journal list or link availability has changed.')})
    return changes


@login_required
@never_cache
@require_GET
def review(request, slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    release = site.current_release
    if release is not None and release.site_id != site.pk:
        raise Http404
    changes = _changes(site, release, kind, owner)
    block_route = 'journal_site_block_review' if kind == 'journal' else 'publisher_site_block_review'
    for change in changes:
        if change['kind'] == 'pages' and change['page_changes']:
            for page in change['page_changes']:
                if page['blocks_changed']:
                    page['blocks_url'] = reverse(block_route, args=[slug, (page['after'] or page['before'])['slug']])
    draft_route = 'preview_journal_site' if kind == 'journal' else 'preview_publisher_portal'
    public_route = 'public_journal_site' if kind == 'journal' else 'public_publisher_portal'
    manage_route = 'manage_journal_site' if kind == 'journal' else 'manage_publisher_portal'
    return _no_index(render(request, 'publishing/site_change_review.html', {
        'owner':owner, 'site':site, 'release':release, 'kind':kind,
        'changes':changes, 'change_count':len(changes),
        'draft_hero':_hero(site, site, kind, slug), 'release_hero':_hero(site, release, kind, slug),
        'draft_appearance':_appearance(site, kind), 'release_appearance':_appearance(release, kind),
        'draft_url':reverse(draft_route,args=[slug]),
        'release_url':reverse(public_route,args=[slug]) if release else None,
        'manage_url':reverse(manage_route,args=[slug]),
    }))


@login_required
@never_cache
@require_GET
def block_review(request, slug, page_slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    release = site.current_release
    if release is not None and release.site_id != site.pk:
        raise Http404
    before = _block_side(release.pages_config, page_slug) if release else {'state':'unpublished'}
    after = _block_side(site.pages_config, page_slug)
    if before['state'] != 'present' and after['state'] != 'present':
        raise Http404
    _attach_block_images(site, kind, slug, [(release,before),(site,after)])
    draft_route = 'preview_journal_page' if kind == 'journal' else 'preview_publisher_page'
    public_route = 'public_journal_page' if kind == 'journal' else 'public_publisher_page'
    compare_route = 'journal_site_change_review' if kind == 'journal' else 'publisher_site_change_review'
    return _no_index(render(request, 'publishing/site_block_review.html', {
        'owner':owner, 'site':site, 'release':release, 'before':before, 'after':after,
        'page_slug':page_slug, 'compare_url':reverse(compare_route,args=[slug]),
        'draft_url':reverse(draft_route,args=[slug,page_slug]) if after['state']=='present' else None,
        'release_url':reverse(public_route,args=[slug,page_slug]) if before['state']=='present' and before['visible'] else None,
    }))
