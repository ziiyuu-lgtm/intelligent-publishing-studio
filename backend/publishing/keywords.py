"""Bounded human-supplied keywords; None represents unrecorded legacy data."""
import unicodedata

from django import forms
from django.utils.translation import gettext_lazy as _

MAX_KEYWORDS = 10
MAX_KEYWORD_LENGTH = 100
MAX_KEYWORDS_TEXT = MAX_KEYWORDS * (MAX_KEYWORD_LENGTH + 1)


def validate_keywords(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_KEYWORDS:
        raise ValueError('invalid keyword list')
    result, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError('invalid keyword')
        normalized = unicodedata.normalize('NFC', item.strip())
        if (not normalized or len(normalized) > MAX_KEYWORD_LENGTH or normalized != item or
                any(unicodedata.category(c) in ('Cc', 'Cs', 'Zl', 'Zp') for c in normalized)):
            raise ValueError('invalid keyword')
        key = normalized.casefold()
        if key in seen:
            raise ValueError('duplicate keyword')
        seen.add(key)
        result.append(normalized)
    return result


def keywords_text(value):
    values = validate_keywords(value)
    return '\n'.join(values or [])


def visible_keywords(value):
    try:
        return validate_keywords(value)
    except ValueError:
        return None


class KeywordListField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('required', False)
        kwargs.setdefault('max_length', MAX_KEYWORDS_TEXT)
        kwargs.setdefault('label', _('Keywords'))
        kwargs.setdefault('help_text', _('Optional: one keyword per line, up to 10; each may contain up to 100 characters.'))
        kwargs.setdefault('widget', forms.Textarea(attrs={'rows':4, 'placeholder':_('One keyword per line')}))
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, list):
            return '\n'.join(value)
        return super().prepare_value(value)

    def clean(self, value):
        text = super().clean(value)
        entries = [unicodedata.normalize('NFC', line.strip()) for line in text.splitlines() if line.strip()]
        try:
            return validate_keywords(entries)
        except ValueError:
            raise forms.ValidationError(_('Use at most 10 distinct keywords, each no longer than 100 characters.'))
