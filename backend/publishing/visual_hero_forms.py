"""Site-scoped hero-image choices for private visual-design review."""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import HERO_FIT_CHOICES, HERO_POSITION_CHOICES


class VisualHeroForm(forms.Form):
    hero_asset_id = forms.ChoiceField(label=_("Hero image"), required=False, choices=())
    hero_alt = forms.CharField(label=_("Image description"), required=False, max_length=200)
    hero_fit = forms.ChoiceField(label=_("Image display"), choices=HERO_FIT_CHOICES)
    hero_position = forms.ChoiceField(label=_("Image focus"), choices=HERO_POSITION_CHOICES)

    def __init__(self, *args, image_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['hero_asset_id'].choices = [('', _('No image')), *image_choices]

    def clean(self):
        data = super().clean()
        if data.get('hero_asset_id') and not data.get('hero_alt', '').strip():
            self.add_error('hero_alt', _('Provide a description for the selected hero image.'))
        if not data.get('hero_asset_id'):
            data.update(hero_alt='', hero_fit='cover', hero_position='center')
        return data
