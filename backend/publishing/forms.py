from django import forms
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from .models import Membership, ReviewAssignment, Submission

MAX_MANUSCRIPT_BYTES = 10 * 1024 * 1024


class SubmissionForm(forms.ModelForm):
    class Meta:
        model = Submission
        fields = ["title", "abstract", "manuscript"]
        widgets = {"abstract": forms.Textarea(attrs={"rows": 6}), "manuscript": forms.FileInput(attrs={"accept": ".pdf,application/pdf"})}

    def clean_manuscript(self):
        document = self.cleaned_data["manuscript"]
        if not document.name.lower().endswith(".pdf") or not 0 < document.size <= MAX_MANUSCRIPT_BYTES:
            raise forms.ValidationError(_("Choose a PDF file no larger than 10 MB."))
        signature = document.read(5)
        document.seek(0)
        if signature != b"%PDF-":
            raise forms.ValidationError(_("The selected file does not have a PDF header."))
        return document


class InviteForm(forms.Form):
    reviewer = forms.ModelChoiceField(queryset=get_user_model().objects.none(), label=_("Reviewer"))

    def __init__(self, *args, submission, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reviewer"].queryset = get_user_model().objects.filter(
            journal_memberships__journal=submission.journal,
            journal_memberships__role=Membership.Role.REVIEWER,
            journal_memberships__active=True, is_active=True,
        ).exclude(pk__in=[submission.author_id, actor.pk]).distinct().order_by("username")


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
