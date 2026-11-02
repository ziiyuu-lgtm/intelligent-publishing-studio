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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="submissions")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submissions")
    title = models.CharField(_("Title"), max_length=250)
    abstract = models.TextField(_("Abstract"), max_length=10000)
    manuscript = models.FileField(_("Manuscript PDF"), upload_to=manuscript_path, max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=["journal", "status"])]
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


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
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_assignments")
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_invitations_sent")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INVITED)
    recommendation = models.CharField(max_length=16, choices=Recommendation.choices, blank=True)
    comments = models.TextField(blank=True, max_length=20000)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "reviewer"], name="unique_reviewer_per_submission")]
        ordering = ["-created_at"]


class SubmissionEvent(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
