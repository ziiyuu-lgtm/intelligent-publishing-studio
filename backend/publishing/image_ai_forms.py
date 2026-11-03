"""Explicit inputs for a private fictional image-generation request."""
from django import forms
from django.utils.translation import gettext_lazy as _
from .image_ai_provider import MAX_BRIEF


class ImageGenerationForm(forms.Form):
    brief = forms.CharField(label=_('Describe the image you want'), max_length=MAX_BRIEF,
        widget=forms.Textarea(attrs={'rows':5, 'placeholder':_('Describe the subject, visual style, mood and colours.')}))
    description = forms.CharField(label=_('Image description'), max_length=200,
        help_text=_('Describe the image for people using screen readers. This description is not sent to the image provider.'))
    size = forms.ChoiceField(label=_('Image shape'), choices=(('1536x1024',_('Landscape')),
        ('1024x1024',_('Square')),('1024x1536',_('Portrait'))))
