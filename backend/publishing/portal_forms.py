from django import forms
from django.forms import BaseFormSet, formset_factory
from django.utils.translation import gettext_lazy as _

from .forms import JournalAppearanceForm
from .models import Journal, OrganisationAIPolicy
from .module_images import SectionImageForm, form_image_fields
from .portal_sections import (MAX_BODY_LENGTH, MAX_HEADING_LENGTH, MAX_SECTIONS, SECTION_CHOICES,
                              InvalidSections, validate_sections)

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


class OrganisationAIPolicyForm(forms.ModelForm):
    class Meta:
        model = OrganisationAIPolicy
        fields = ["portal_design_enabled", "portal_modules_enabled", "portal_daily_limit"]


class PortalSectionForm(SectionImageForm):
    kind = forms.ChoiceField(label=_("Module type"), choices=(("", _("Select a module type")), *SECTION_CHOICES))
    heading = forms.CharField(label=_("Custom heading"), max_length=MAX_HEADING_LENGTH, required=False,
        help_text=_("Leave blank to use the module's default heading; custom modules need a heading."))
    body = forms.CharField(label=_("Module text"), max_length=MAX_BODY_LENGTH, required=False,
        widget=forms.Textarea(attrs={"rows":3}),
        help_text=_("Plain text only. Leave the publisher introduction blank to use the saved introduction."))
    visible = forms.BooleanField(label=_("Show this module"), required=False)


class BasePortalSectionFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active = []
        for index, form in enumerate(self.forms):
            data = getattr(form, "cleaned_data", {})
            if not data or data.get("DELETE") or not data.get("kind"):
                continue
            order = data.get("ORDER")
            active.append(((order is None, order if order is not None else 0, index),
                           {**{key:data[key] for key in ("kind", "heading", "body", "visible")}, **form_image_fields(data)}))
        try:
            self.cleaned_sections = validate_sections([item for _, item in sorted(active, key=lambda row:row[0])])
        except InvalidSections as exc:
            raise forms.ValidationError(_("Keep at least one visible module, use About and Journals at most once, and name every custom module.")) from exc


PortalSectionFormSet = formset_factory(PortalSectionForm, formset=BasePortalSectionFormSet, extra=1,
    can_order=True, can_delete=True, max_num=MAX_SECTIONS, absolute_max=MAX_SECTIONS * 2, validate_max=True)
