import uuid
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Organisation(models.Model):
    name = models.CharField(_("Name"), max_length=200)
    slug = models.SlugField(unique=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Journal(models.Model):
    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="journals")
    name = models.CharField(_("Name"), max_length=200)
    slug = models.SlugField(unique=True)
    description = models.TextField(_("Description"), blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        AUTHOR = "author", _("Author")
        EDITOR = "editor", _("Editor")
        REVIEWER = "reviewer", _("Reviewer")

    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="journal_memberships")
    role = models.CharField(max_length=16, choices=Role.choices)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["journal", "user", "role"], name="unique_journal_user_role")]

    def __str__(self):
        return f"{self.user} / {self.journal} / {self.role}"


def manuscript_path(instance, filename):
    # Never preserve a supplied file path or filename in storage.
    return f"manuscripts/{uuid.uuid4().hex}.pdf"


class Submission(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SUBMITTED = "submitted", _("Submitted")
        UNDER_REVIEW = "under_review", _("Under review")
        REVISION_REQUESTED = "revision_requested", _("Revision requested")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="submissions")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submissions")
    title = models.CharField(_("Title"), max_length=250)
    abstract = models.TextField(_("Abstract"), max_length=10000)
    manuscript = models.FileField(_("Manuscript PDF"), upload_to=manuscript_path, max_length=255)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=["journal", "status"])]
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class SubmissionVersion(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    manuscript = models.FileField(_("Manuscript PDF"), upload_to=manuscript_path, max_length=255)
    author_response = models.TextField(_("Response to the editor"), max_length=20000, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "number"], name="unique_submission_version"),
            models.CheckConstraint(condition=models.Q(number__gte=1), name="positive_version_number"),
        ]
        ordering = ["number"]


class ReviewRound(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CLOSED = "closed", _("Closed")

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="rounds")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="review_rounds")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "number"], name="unique_submission_round"),
            models.UniqueConstraint(fields=["submission"], condition=models.Q(status="open"), name="one_open_round_per_submission"),
            models.CheckConstraint(condition=models.Q(number__gte=1), name="positive_round_number"),
        ]
        ordering = ["number"]


class ReviewAssignment(models.Model):
    class Status(models.TextChoices):
        INVITED = "invited", _("Invited")
        ACCEPTED = "accepted", _("Accepted")
        DECLINED = "declined", _("Declined")
        SUBMITTED = "submitted", _("Report submitted")

    class Recommendation(models.TextChoices):
        ACCEPT = "accept", _("Accept")
        MINOR = "minor", _("Minor revision")
        MAJOR = "major", _("Major revision")
        REJECT = "reject", _("Reject")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="reviews")
    round = models.ForeignKey(ReviewRound, on_delete=models.PROTECT, related_name="assignments")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_assignments")
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_invitations_sent")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INVITED)
    recommendation = models.CharField(max_length=16, choices=Recommendation.choices, blank=True)
    comments = models.TextField(blank=True, max_length=20000)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["round", "reviewer"], name="unique_reviewer_per_round")]
        ordering = ["-created_at"]


class EditorialDecision(models.Model):
    class Outcome(models.TextChoices):
        MINOR = "minor", _("Minor revision")
        MAJOR = "major", _("Major revision")
        ACCEPT = "accept", _("Accept")
        REJECT = "reject", _("Reject")

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="decisions")
    round = models.OneToOneField(ReviewRound, on_delete=models.PROTECT, related_name="decision")
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    author_message = models.TextField(_("Message to the author"), max_length=20000)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["round__number"]


class SubmissionEvent(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
