"""Account-scoped, fictional author responses; no messages or identity verification."""
import hashlib
import json

from django import forms
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from .models import (ContributorResponseEvent, ContributorResponseRequest, EditorialAssignment, EditorialDecision, Membership,
                     ProductionAssignment, ReviewAssignment)


def is_response_participant(user, submission):
    # Access to a response task may reveal author names even after cancellation.
    return ContributorResponseRequest.objects.filter(change__submission=submission, recipient=user).exists()


def response_snapshot_digest(change):
    payload = {"before":change.before, "proposed":change.proposed, "reason":change.reason}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def author_responses_ready(change, before, proposed):
    """An optional assigned account task must match its snapshot and agree."""
    snapshots = {"original":before, "proposed":proposed}
    digest = response_snapshot_digest(change)
    tasks = list(change.author_response_requests.exclude(status=ContributorResponseRequest.Status.REVOKED).select_related("recipient"))
    if not tasks:
        return True
    submission = change.submission
    if not submission.journal.active or not submission.journal.organisation.active:
        return False
    recipient_ids = {task.recipient_id for task in tasks}
    allowed = set(Membership.objects.filter(user_id__in=recipient_ids, journal=submission.journal,
        role=Membership.Role.AUTHOR, active=True).values_list("user_id", flat=True))
    excluded = conflicting_recipient_ids(submission)
    latest = {}
    for event in ContributorResponseEvent.objects.filter(response_request__in=tasks,
            action__in=["agreed", "disagreed"]).order_by("response_request_id", "-pk"):
        latest.setdefault(event.response_request_id, event)
    for task in tasks:
        if (task.side not in snapshots or not 0 <= task.position < len(snapshots[task.side]) or
                task.snapshot_digest != digest or task.status != ContributorResponseRequest.Status.AGREED or
                not task.recipient.is_active or task.recipient_id not in allowed or task.recipient_id in excluded):
            return False
        last_event = latest.get(task.pk)
        if not last_event or last_event.action != "agreed" or last_event.actor_id != task.recipient_id:
            return False
    return True


def conflicting_recipient_ids(submission):
    """Exclude staff or reviewers who already handled this submission."""
    excluded = set()
    for reviewer, inviter in ReviewAssignment.objects.filter(submission=submission).values_list("reviewer_id", "invited_by_id"):
        excluded.update((reviewer, inviter))
    for editor, assigner, ender in EditorialAssignment.objects.filter(submission=submission).values_list("editor_id", "assigned_by_id", "ended_by_id"):
        excluded.update((editor, assigner, ender))
    excluded.update(EditorialDecision.objects.filter(submission=submission).values_list("decided_by_id", flat=True))
    for producer, assigner, ender in ProductionAssignment.objects.filter(draft__submission=submission).values_list("producer_id", "assigned_by_id", "ended_by_id"):
        excluded.update((producer, assigner, ender))
    excluded.update(Membership.objects.filter(journal=submission.journal, role=Membership.Role.COORDINATOR,
        active=True).exclude(user_id=submission.author_id).values_list("user_id", flat=True))
    excluded.discard(None)
    return excluded


def eligible_recipient(user, submission):
    if not user or not user.is_active or not submission.journal.active or not submission.journal.organisation.active:
        return False
    if not Membership.objects.filter(user=user, journal=submission.journal, role=Membership.Role.AUTHOR, active=True).exists():
        return False
    return user.pk not in conflicting_recipient_ids(submission)


class AssignAuthorResponseForm(forms.Form):
    recipient = forms.ModelChoiceField(label=_("Fictional author account"), queryset=get_user_model().objects.none(),
                                       empty_label=_("Choose an author account"))
    verified = forms.BooleanField(label=_("I manually checked that this account corresponds to the author entry and assessed potential conflicts."))

    def __init__(self, *args, change, side, **kwargs):
        super().__init__(*args, **kwargs)
        submission = change.submission
        active = get_user_model().objects.filter(is_active=True, journal_memberships__journal=submission.journal,
            journal_memberships__role=Membership.Role.AUTHOR, journal_memberships__active=True).distinct()
        excluded = set(change.author_response_requests.filter(side=side).exclude(status="revoked").values_list("recipient_id", flat=True))
        excluded.update(conflicting_recipient_ids(submission))
        # The same conditions are rechecked under the submission lock before creation.
        self.fields["recipient"].queryset = active.exclude(pk__in=excluded).order_by("username")


class AuthorResponseForm(forms.Form):
    response = forms.ChoiceField(label=_("Your response"), choices=[
        ("agreed", _("I agree to this author-list change")),
        ("disagreed", _("I do not agree to this author-list change"))], widget=forms.RadioSelect)
    reviewed = forms.BooleanField(label=_("I reviewed both author lists and the stated reason for this fictional request."))
