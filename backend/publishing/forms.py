from django import forms
from django.contrib.auth import get_user_model
from django.forms import BaseFormSet, formset_factory
from django.utils.translation import gettext_lazy as _
from .models import EditorialDecision, Journal, JournalAIPolicy, JournalSiteDraft, Membership, ProductionAssignment, PublicationDraft, ReviewAssignment, Submission
from .site_design import ALIGNMENTS, COMPOSITIONS, CORNERS, DENSITIES, FONTS, HERO_STYLES, SECTION_ORDERS
from .module_images import SectionImageForm, form_image_fields
from .site_sections import (MAX_BODY_LENGTH, MAX_HEADING_LENGTH, MAX_SECTIONS, SECTION_CHOICES,
                            InvalidSections, validate_sections)

MAX_MANUSCRIPT_BYTES = 10 * 1024 * 1024


def validate_pdf(document):
    if not document.name.lower().endswith(".pdf") or not 0 < document.size <= MAX_MANUSCRIPT_BYTES:
        raise forms.ValidationError(_("Choose a PDF file no larger than 10 MB."))
    signature = document.read(5)
    document.seek(0)
    if signature != b"%PDF-":
        raise forms.ValidationError(_("The selected file does not have a PDF header."))
    return document


class SubmissionForm(forms.ModelForm):
    review_copy = forms.FileField(label=_("Separate reviewer PDF"), widget=forms.FileInput(attrs={"accept": ".pdf,application/pdf"}))
    review_copy_confirmed = forms.BooleanField(label=_("I checked the reviewer PDF, title and abstract for identifying information."))

    class Meta:
        model = Submission
        fields = ["title", "abstract", "manuscript"]
        widgets = {"abstract": forms.Textarea(attrs={"rows": 6}), "manuscript": forms.FileInput(attrs={"accept": ".pdf,application/pdf"})}

    def __init__(self, *args, review_mode, **kwargs):
        super().__init__(*args, **kwargs)
        if review_mode != Journal.ReviewMode.SEPARATE:
            self.fields.pop("review_copy")
            self.fields.pop("review_copy_confirmed")

    def clean_manuscript(self):
        return validate_pdf(self.cleaned_data["manuscript"])

    def clean_review_copy(self):
        return validate_pdf(self.cleaned_data["review_copy"])


class RevisionForm(forms.Form):
    manuscript = forms.FileField(label=_("Revised manuscript PDF"), widget=forms.FileInput(attrs={"accept": ".pdf,application/pdf"}))
    review_copy = forms.FileField(label=_("Separate reviewer PDF"), widget=forms.FileInput(attrs={"accept": ".pdf,application/pdf"}))
    review_copy_confirmed = forms.BooleanField(label=_("I checked the reviewer PDF, title and abstract for identifying information."))
    author_response = forms.CharField(label=_("Response to the editor"), max_length=20000, widget=forms.Textarea(attrs={"rows": 8}))

    def __init__(self, *args, review_mode, **kwargs):
        super().__init__(*args, **kwargs)
        if review_mode != Journal.ReviewMode.SEPARATE:
            self.fields.pop("review_copy")
            self.fields.pop("review_copy_confirmed")

    def clean_manuscript(self):
        return validate_pdf(self.cleaned_data["manuscript"])

    def clean_review_copy(self):
        return validate_pdf(self.cleaned_data["review_copy"])

    def clean_author_response(self):
        value = self.cleaned_data["author_response"].strip()
        if not value:
            raise forms.ValidationError(_("Please provide a response."))
        return value


class DecisionForm(forms.ModelForm):
    class Meta:
        model = EditorialDecision
        fields = ["outcome", "author_message"]
        labels = {"outcome": _("Editorial decision"), "author_message": _("Message to the author")}
        widgets = {"author_message": forms.Textarea(attrs={"rows": 6})}

    def clean_author_message(self):
        value = self.cleaned_data["author_message"].strip()
        if not value:
            raise forms.ValidationError(_("Please provide a message for the author."))
        return value


class InviteForm(forms.Form):
    reviewer = forms.ModelChoiceField(queryset=get_user_model().objects.none(), label=_("Reviewer"))

    def __init__(self, *args, submission, actor, **kwargs):
        super().__init__(*args, **kwargs)
        prior_editors = list(submission.reviews.values_list("invited_by_id", flat=True))
        prior_editors += list(submission.decisions.values_list("decided_by_id", flat=True))
        prior_editors += list(submission.editor_assignments.values_list("editor_id", flat=True))
        prior_editors += list(ProductionAssignment.objects.filter(draft__submission=submission).values_list("producer_id", flat=True))
        prior_editors += list(Membership.objects.filter(journal=submission.journal, role=Membership.Role.EDITOR).values_list("user_id", flat=True))
        self.fields["reviewer"].queryset = get_user_model().objects.filter(
            journal_memberships__journal=submission.journal,
            journal_memberships__role=Membership.Role.REVIEWER,
            journal_memberships__active=True, is_active=True,
        ).exclude(pk__in=[submission.author_id, actor.pk, *prior_editors]).distinct().order_by("username")


class ReportForm(forms.ModelForm):
    class Meta:
        model = ReviewAssignment
        fields = ["recommendation", "comments"]
        labels = {"recommendation": _("Recommendation"), "comments": _("Confidential comments to the editor")}
        widgets = {"comments": forms.Textarea(attrs={"rows": 8})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recommendation"].required = True
        self.fields["comments"].required = True

    def clean_comments(self):
        value = self.cleaned_data["comments"].strip()
        if not value:
            raise forms.ValidationError(_("Please provide comments."))
        return value


class PublicationMetadataForm(forms.ModelForm):
    class Meta:
        model = PublicationDraft
        fields = ["title", "authors", "abstract"]
        widgets = {"authors": forms.Textarea(attrs={"rows": 3}), "abstract": forms.Textarea(attrs={"rows": 6})}

    def clean_authors(self):
        value = self.cleaned_data["authors"].strip()
        if not value:
            raise forms.ValidationError(_("Provide fictional author names."))
        return value


class PublicationProofForm(forms.Form):
    file = forms.FileField(label=_("Fictional publication PDF"), widget=forms.FileInput(attrs={"accept": ".pdf,application/pdf"}))
    synthetic = forms.BooleanField(label=_("I confirm this PDF contains only fictional test data."))

    def clean_file(self):
        return validate_pdf(self.cleaned_data["file"])


class JournalIssueForm(forms.Form):
    volume = forms.IntegerField(label=_("Volume"), min_value=1, max_value=9999)
    number = forms.IntegerField(label=_("Issue number"), min_value=1, max_value=9999)
    year = forms.IntegerField(label=_("Year"), min_value=1000, max_value=9999)
    title = forms.CharField(label=_("Issue title"), max_length=200, required=False)
    synthetic = forms.BooleanField(label=_("I confirm that this is a fictional test issue."))


class JournalSiteForm(forms.ModelForm):
    class Meta:
        model = JournalSiteDraft
        fields = ["brand_name", "headline", "tagline", "introduction", "theme", "layout"]
        widgets = {"introduction": forms.Textarea(attrs={"rows": 7})}


class JournalAppearanceForm(forms.Form):
    primary = forms.RegexField(label=_("Primary colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7,
        widget=forms.TextInput(attrs={"type":"color"}))
    surface = forms.RegexField(label=_("Panel colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7,
        widget=forms.TextInput(attrs={"type":"color"}))
    background = forms.RegexField(label=_("Page colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7,
        widget=forms.TextInput(attrs={"type":"color"}))
    composition = forms.ChoiceField(label=_("Page composition"), choices=COMPOSITIONS)
    font = forms.ChoiceField(label=_("Typography"), choices=FONTS)
    hero_style = forms.ChoiceField(label=_("Hero background"), choices=HERO_STYLES)
    alignment = forms.ChoiceField(label=_("Hero alignment"), choices=ALIGNMENTS)
    density = forms.ChoiceField(label=_("Spacing"), choices=DENSITIES)
    corners = forms.ChoiceField(label=_("Corners"), choices=CORNERS)
    section_order = forms.ChoiceField(label=_("Section order"), choices=SECTION_ORDERS)
    show_feature = forms.BooleanField(label=_("Show feature panel"), required=False)

    def clean(self):
        from .site_design import InvalidDesign, validate_blueprint
        data = super().clean()
        if not self.errors:
            try:
                return validate_blueprint(data)
            except InvalidDesign as exc:
                raise forms.ValidationError(_("The site design is invalid.")) from exc
        return data


class JournalSectionForm(SectionImageForm):
    kind = forms.ChoiceField(label=_("Module type"), choices=(("", _("Select a module type")), *SECTION_CHOICES))
    heading = forms.CharField(label=_("Custom heading"), max_length=MAX_HEADING_LENGTH, required=False,
        help_text=_("Leave blank to use the module's default heading; custom modules need a heading."))
    body = forms.CharField(label=_("Module text"), max_length=MAX_BODY_LENGTH, required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Plain text only. Leave the journal introduction blank to use the saved introduction."))
    visible = forms.BooleanField(label=_("Show this module"), required=False)


class BaseJournalSectionFormSet(BaseFormSet):
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
            self.cleaned_sections = validate_sections([item for _, item in sorted(active, key=lambda row: row[0])])
        except InvalidSections as exc:
            raise forms.ValidationError(_("Keep at least one visible module, use About and Articles at most once, and name every custom module.")) from exc


JournalSectionFormSet = formset_factory(JournalSectionForm, formset=BaseJournalSectionFormSet, extra=1,
    can_order=True, can_delete=True, max_num=MAX_SECTIONS, absolute_max=MAX_SECTIONS * 2, validate_max=True)


class JournalAIPolicyForm(forms.ModelForm):
    class Meta:
        model = JournalAIPolicy
        fields = ["metadata_enabled", "allow_author", "allow_editor", "site_design_enabled", "site_modules_enabled", "site_pages_enabled", "journal_daily_limit"]

    def clean(self):
        data = super().clean()
        if data.get("metadata_enabled") and not (data.get("allow_author") or data.get("allow_editor")):
            raise forms.ValidationError(_("Enable at least one requester role before enabling this task."))
        return data
