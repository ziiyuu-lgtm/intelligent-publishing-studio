"""Render validated page blocks in a private, approximate layout canvas."""
from django.urls import reverse

from .block_design import block_appearance


def canvas_row(form, block, owner, kind):
    """Build display data only after the page and its image references are validated."""
    image_route = 'private_journal_image' if kind == 'journal' else 'private_portal_image'
    image_url = (reverse(image_route, args=[owner.slug, block['image_asset_id']])
                 if block['visible'] and block.get('image_asset_id') else '')
    return {'form':form, 'label':block['heading'] or block['kind'], 'visible':block['visible'],
            'block':block, 'image_url':image_url,
            'appearance':block_appearance(block['design']) if block.get('design') else None}
