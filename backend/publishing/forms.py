from django import forms
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from .models import EditorialDecision, Journal, Membership, ProductionAssignment, PublicationDraft, ReviewAssignment, Submission

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
