from django import forms
from django.utils.translation import gettext_lazy as _

from .forms import JournalAppearanceForm
from .models import Journal

MAX_JOURNALS = 50


class PortalContentForm(forms.Form):
    brand_name = forms.CharField(label=_("Site display name"), max_length=200)
    headline = forms.CharField(label=_("Site headline"), max_length=200)
    tagline = forms.CharField(label=_("Site tagline"), max_length=240, required=False)
    introduction = forms.CharField(label=_("Publisher introduction"), max_length=4000,
                                   widget=forms.Textarea(attrs={"rows": 6}))
    journals = forms.ModelMultipleChoiceField(label=_("Journals to display"), queryset=Journal.objects.none(),
        required=False, widget=forms.CheckboxSelectMultiple,
        help_text=_("Choose up to 50 active journals from this publisher. Unpublished journal sites are shown without a link."))

    def __init__(self, *args, organisation, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["journals"].queryset = Journal.objects.filter(organisation=organisation, active=True).order_by("name", "pk")

    def clean_journals(self):
        journals = self.cleaned_data["journals"]
        if len(journals) > MAX_JOURNALS:
            raise forms.ValidationError(_("Choose no more than 50 journals."))
        return journals


class PortalAppearanceForm(JournalAppearanceForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["section_order"].choices = (("about_first", _("Publisher introduction first")),
                                                  ("archive_first", _("Journal list first")))
