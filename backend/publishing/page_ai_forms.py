"""Review and edit generated page suggestions before applying them."""
from django import forms
from django.utils.translation import gettext_lazy as _

from .page_ai_provider import MAX_BODY_LENGTH
from .page_design import ALIGNMENTS, LAYOUTS, InvalidPageDesign, validate_page_design


class PageAISuggestionForm(forms.Form):
    title = forms.CharField(label=_("Suggested page title"), max_length=120)
    body = forms.CharField(label=_("Suggested page text"), max_length=MAX_BODY_LENGTH,
                           widget=forms.Textarea(attrs={"rows":7}))
    layout = forms.ChoiceField(label=_("Page layout"), choices=LAYOUTS)
    width = forms.IntegerField(label=_("Content width (%)"), min_value=55, max_value=100)
    background = forms.RegexField(label=_("Page background colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7)
    accent = forms.RegexField(label=_("Page accent colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7)
    alignment = forms.ChoiceField(label=_("Page text alignment"), choices=ALIGNMENTS)

    def __init__(self, *args, option=None, **kwargs):
        if option is not None and not args and "data" not in kwargs:
            kwargs["initial"] = {"title":option["title"], "body":option["body"], **option["design"]}
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        try:
            design = validate_page_design({key:data[key] for key in ("layout", "width", "background", "accent", "alignment")})
        except InvalidPageDesign as exc:
            raise forms.ValidationError(_("Complete every custom page design setting.")) from exc
        # The manager's text is displayed as escaped plain text, including any URLs.
        self.option = {"title":data["title"], "body":data["body"], "design":design}
        return data
