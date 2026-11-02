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
    class ReviewMode(models.TextChoices):
        DIRECT = "direct", _("Standard review copy")
        SEPARATE = "separate", _("Separate reviewer copy")

    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="journals")
    name = models.CharField(_("Name"), max_length=200)
    slug = models.SlugField(unique=True)
    description = models.TextField(_("Description"), blank=True)
    active = models.BooleanField(default=True)
    review_mode = models.CharField(_("Review file policy"), max_length=16, choices=ReviewMode.choices, default=ReviewMode.SEPARATE)
    allow_openai_metadata = models.BooleanField(_("Allow OpenAI metadata suggestions"), default=False)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        AUTHOR = "author", _("Author")
        EDITOR = "editor", _("Editor")
        REVIEWER = "reviewer", _("Reviewer")
        COORDINATOR = "coordinator", _("Editorial coordinator")

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
    # Policy is captured at draft creation and does not change mid-submission.
    review_mode = models.CharField(max_length=16, choices=Journal.ReviewMode.choices, default=Journal.ReviewMode.DIRECT)
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


class EditorialAssignment(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        REVOKED = "revoked", _("Removed")
        RECUSED = "recused", _("Recused")

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="editor_assignments")
    editor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="editorial_assignments")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="editorial_assignments_made")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="editorial_assignments_ended")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "editor"], condition=models.Q(status="active"), name="one_active_editor_per_submission")]
        ordering = ["-created_at", "-pk"]


class SubmissionVersion(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    manuscript = models.FileField(_("Manuscript PDF"), upload_to=manuscript_path, max_length=255)
    review_copy = models.FileField(_("Reviewer PDF"), upload_to=manuscript_path, max_length=255, blank=True)
    review_copy_confirmed = models.BooleanField(default=False)
    review_copy_checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="review_copies_checked")
    review_copy_checked_at = models.DateTimeField(null=True, blank=True)
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
        CONFLICT = "conflict", _("Conflict declared")
        SUBMITTED = "submitted", _("Report submitted")

    class Conflict(models.TextChoices):
        UNKNOWN = "unknown", _("Not declared")
        NONE = "none", _("No conflict declared")
        YES = "yes", _("Conflict declared")

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
    conflict = models.CharField(max_length=10, choices=Conflict.choices, default=Conflict.UNKNOWN)
    conflict_details = models.TextField(max_length=2000, blank=True)
    conflict_declared_at = models.DateTimeField(null=True, blank=True)
    recommendation = models.CharField(max_length=16, choices=Recommendation.choices, blank=True)
    comments = models.TextField(blank=True, max_length=20000)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["round", "reviewer"], name="unique_reviewer_per_round")]
        ordering = ["-created_at"]


class ConflictDeclaration(models.Model):
    class Choice(models.TextChoices):
        NONE = "none", _("No conflict declared")
        YES = "yes", _("Conflict declared")

    assignment = models.ForeignKey(ReviewAssignment, on_delete=models.PROTECT, related_name="conflict_history")
    declared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    choice = models.CharField(max_length=10, choices=Choice.choices)
    details = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class AIConsent(models.Model):
    """Author consent is bound to a particular version and metadata fingerprint."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="ai_consents")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    provider = models.CharField(max_length=20, default="openai")
    purpose = models.CharField(max_length=60)
    data_fingerprint = models.CharField(max_length=64)
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["version", "provider"], condition=models.Q(revoked_at__isnull=True), name="one_active_ai_consent_per_version")]
        ordering = ["-granted_at"]


class AIAssistanceRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Unavailable")
        CANCELLED = "cancelled", _("Cancelled")

    # The form supplies this UUID so resubmission cannot duplicate a request.
    id = models.UUIDField(primary_key=True, editable=False)
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="ai_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    consent = models.ForeignKey(AIConsent, on_delete=models.PROTECT)
    provider = models.CharField(max_length=20, default="openai")
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=60)
    data_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
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


def publication_proof_path(instance, filename):
    return f"publication-proofs/{uuid.uuid4().hex}.pdf"


class PublicationDraft(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="publication_draft")
    decision = models.OneToOneField(EditorialDecision, on_delete=models.PROTECT, related_name="publication_draft")
    title = models.CharField(_("Public title"), max_length=250)
    authors = models.TextField(_("Public author names"), max_length=2000, blank=True)
    abstract = models.TextField(_("Public abstract"), max_length=10000)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="publication_drafts_created")
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="test_publications")
    published_proof = models.ForeignKey("PublicationProof", on_delete=models.PROTECT, null=True, blank=True, related_name="published_in")


class PublicationProof(models.Model):
    draft = models.ForeignKey(PublicationDraft, on_delete=models.PROTECT, related_name="proofs")
    file = models.FileField(_("Publication PDF"), upload_to=publication_proof_path, max_length=255)
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pk"]


class PublicationApproval(models.Model):
    draft = models.ForeignKey(PublicationDraft, on_delete=models.PROTECT, related_name="approvals")
    proof = models.ForeignKey(PublicationProof, on_delete=models.PROTECT, related_name="approvals")
    revision = models.PositiveIntegerField()
    fingerprint = models.CharField(max_length=64)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["draft", "revision", "proof", "approved_by"], name="unique_publication_approval")]


class SubmissionEvent(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
