"""Private, local-only evidence and manual authorship verification helpers."""
import hashlib
import hmac

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ContributorConfirmationEvent, ContributorEvidence

MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
MAX_EVIDENCE_FILES = 50


class ContributorEvidenceForm(forms.Form):
    label = forms.CharField(label=_("Evidence label"), max_length=120)
    file = forms.FileField(label=_("Fictional evidence PDF"),
                           widget=forms.FileInput(attrs={"accept":".pdf,application/pdf"}))
    synthetic = forms.BooleanField(label=_("I confirm this file contains only fictional test information."))

    def clean_file(self):
        document = self.cleaned_data["file"]
        if not document.name.lower().endswith(".pdf") or not 0 < document.size <= MAX_EVIDENCE_BYTES:
            raise forms.ValidationError(_("Choose a PDF file no larger than 5 MB."))
        signature = document.read(5)
        document.seek(0)
        if signature != b"%PDF-":
            raise forms.ValidationError(_("The selected file does not have a PDF header."))
        return document


class ContributorConfirmationForm(forms.Form):
    state = forms.ChoiceField(label=_("Manual check result"), choices=ContributorConfirmationEvent.State.choices)
    evidence = forms.ModelChoiceField(label=_("Supporting evidence"), queryset=ContributorEvidence.objects.none(), required=False,
        empty_label=_("No evidence selected"))

    def __init__(self, *args, change, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence"].queryset = change.evidence_files.all()
        self.fields["evidence"].label_from_instance = lambda item: item.label

    def clean(self):
        data = super().clean()
        if data.get("state") == ContributorConfirmationEvent.State.CONFIRMED and not data.get("evidence"):
            self.add_error("evidence", _("Select the evidence you checked for this author."))
        return data


def open_intact_evidence(evidence):
    """Return a rewound stream only when the stored test file matches its digest."""
    if not evidence or not evidence.file or not 0 < evidence.size <= MAX_EVIDENCE_BYTES:
        return None
    stream = None
    try:
        stream = evidence.file.open("rb")
        digest, total = hashlib.sha256(), 0
        header = stream.read(5)
        digest.update(header)
        total += len(header)
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EVIDENCE_BYTES:
                stream.close()
                return None
            digest.update(chunk)
        if (header != b"%PDF-" or total != evidence.size or
                not hmac.compare_digest(digest.hexdigest(), evidence.sha256)):
            stream.close()
            return None
        stream.seek(0)
        return stream
    except (OSError, ValueError, TypeError):
        if stream:
            stream.close()
        return None


def confirmation_rows(change, before, proposed):
    """Build a snapshot-position checklist without assuming identity matches."""
    snapshots = {"original":before, "proposed":proposed}
    latest = {}
    for event in change.confirmation_events.select_related("evidence", "recorded_by").order_by("pk"):
        if (event.side not in snapshots or not 0 <= event.position < len(snapshots[event.side]) or
                event.state not in ContributorConfirmationEvent.State.values or
                (event.evidence_id and event.evidence.change_id != change.pk) or
                (event.state == ContributorConfirmationEvent.State.CONFIRMED and not event.evidence_id)):
            raise ValueError("invalid confirmation history")
        latest[(event.side, event.position)] = event
    rows = []
    for side, people in snapshots.items():
        for position, person in enumerate(people):
            event = latest.get((side, position))
            rows.append({"side":side, "position":position, "person":person, "event":event,
                         "confirmed":bool(event and event.state == ContributorConfirmationEvent.State.CONFIRMED)})
    return rows


def all_confirmations_intact(change, before, proposed):
    """Require a checked, intact file for every snapshot entry before approval."""
    try:
        rows = confirmation_rows(change, before, proposed)
    except ValueError:
        return False
    if not proposed or not rows or any(not row["confirmed"] for row in rows):
        return False
    seen = set()
    for row in rows:
        evidence = row["event"].evidence
        if evidence.pk in seen:
            continue
        seen.add(evidence.pk)
        stream = open_intact_evidence(evidence)
        if not stream:
            return False
        stream.close()
    return True
