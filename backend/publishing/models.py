import uuid
from django.conf import settings
from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils.translation import gettext_lazy as _

HERO_FIT_CHOICES = (("cover", _("Crop to fill")), ("contain", _("Fit the entire image")))
HERO_POSITION_CHOICES = (("center", _("Centre")), ("top", _("Top")), ("bottom", _("Bottom")),
                         ("left", _("Left")), ("right", _("Right")))


class Organisation(models.Model):
    name = models.CharField(_("Name"), max_length=200)
    slug = models.SlugField(unique=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class LocalTestRegistration(models.Model):
    """A fictional registration attempt; this is not email verification."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="local_test_registration")
    address_digest = models.CharField(max_length=64, unique=True)
    token_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    activated_at = models.DateTimeField(null=True, blank=True)


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
        PRODUCER = "producer", _("Production editor")
        SITE_MANAGER = "site_manager", _("Journal site manager")
        AI_MANAGER = "ai_manager", _("Journal AI manager")

    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="journal_memberships")
    role = models.CharField(max_length=16, choices=Role.choices)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["journal", "user", "role"], name="unique_journal_user_role")]

    def __str__(self):
        return f"{self.user} / {self.journal} / {self.role}"


class OrganisationMembership(models.Model):
    """An explicit organisation role; journal roles never imply this role."""
    class Role(models.TextChoices):
        PORTAL_MANAGER = "portal_manager", _("Publisher portal manager")
        AI_MANAGER = "ai_manager", _("Publisher AI manager")

    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organisation_memberships")
    role = models.CharField(max_length=20, choices=Role.choices)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organisation", "user", "role"], name="unique_org_user_role")]

    def __str__(self):
        return f"{self.user} / {self.organisation} / {self.role}"


class OrganisationPortalDraft(models.Model):
    organisation = models.OneToOneField(Organisation, on_delete=models.PROTECT, related_name="portal_draft")
    brand_name = models.CharField(_("Site display name"), max_length=200)
    headline = models.CharField(_("Site headline"), max_length=200)
    tagline = models.CharField(_("Site tagline"), max_length=240, blank=True)
    introduction = models.TextField(_("Publisher introduction"), max_length=4000)
    design_config = models.JSONField(default=dict)
    sections_config = models.JSONField(default=list, blank=True)
    pages_config = models.JSONField(default=list, blank=True)
    hero_asset = models.ForeignKey("SiteImageAsset", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    hero_alt = models.CharField(_("Image description"), max_length=200, blank=True)
    hero_fit = models.CharField(_("Image display"), max_length=8, choices=HERO_FIT_CHOICES, default="cover")
    hero_position = models.CharField(_("Image focus"), max_length=8, choices=HERO_POSITION_CHOICES, default="center")
    journal_ids = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organisation_portals_created")
    created_at = models.DateTimeField(auto_now_add=True)
    current_release = models.ForeignKey("OrganisationPortalRelease", on_delete=models.PROTECT, null=True, blank=True, related_name="active_for")


class OrganisationPortalRelease(models.Model):
    site = models.ForeignKey(OrganisationPortalDraft, on_delete=models.PROTECT, related_name="releases")
    revision = models.PositiveIntegerField()
    brand_name = models.CharField(_("Site display name"), max_length=200)
    headline = models.CharField(_("Site headline"), max_length=200)
    tagline = models.CharField(_("Site tagline"), max_length=240, blank=True)
    introduction = models.TextField(_("Publisher introduction"), max_length=4000)
    design_config = models.JSONField(default=dict)
    sections_config = models.JSONField(default=list, blank=True)
    pages_config = models.JSONField(default=list, blank=True)
    hero_asset = models.ForeignKey("SiteImageAsset", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    hero_alt = models.CharField(_("Image description"), max_length=200, blank=True)
    hero_fit = models.CharField(_("Image display"), max_length=8, choices=HERO_FIT_CHOICES, default="cover")
    hero_position = models.CharField(_("Image focus"), max_length=8, choices=HERO_POSITION_CHOICES, default="center")
    journals = models.JSONField(default=list)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organisation_portal_releases")
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["site", "revision"], name="unique_org_portal_revision")]
        ordering = ["-revision", "-pk"]


class OrganisationPortalEvent(models.Model):
    site = models.ForeignKey(OrganisationPortalDraft, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    revision = models.PositiveIntegerField()
    source_release = models.ForeignKey(OrganisationPortalRelease, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class OrganisationAIPolicy(models.Model):
    organisation = models.OneToOneField(Organisation, on_delete=models.PROTECT, related_name="ai_policy")
    portal_design_enabled = models.BooleanField(_("Enable publisher portal AI design"), default=False)
    portal_modules_enabled = models.BooleanField(_("Enable publisher portal AI module arrangement"), default=False)
    portal_pages_enabled = models.BooleanField(_("Enable AI independent-page suggestions"), default=False)
    portal_page_blocks_enabled = models.BooleanField(_("Enable AI page-block suggestions"), default=False)
    portal_page_layout_enabled = models.BooleanField(_("Enable AI page-layout suggestions"), default=False)
    portal_image_enabled = models.BooleanField(_("Enable AI image generation"), default=False)
    portal_daily_limit = models.PositiveSmallIntegerField(_("Publisher portal AI request limit per 24 hours"), default=10,
        validators=[MinValueValidator(1), MaxValueValidator(100)])
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(portal_daily_limit__gte=1, portal_daily_limit__lte=100),
                                               name="org_ai_portal_limit_range")]


class OrganisationAIPolicyEvent(models.Model):
    policy = models.ForeignKey(OrganisationAIPolicy, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    configuration = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision", "-pk"]
        constraints = [models.UniqueConstraint(fields=["policy", "revision"], name="unique_org_ai_policy_revision")]


class OrganisationPortalAIDesignRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Unavailable")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.UUIDField(primary_key=True, editable=False)
    site = models.ForeignKey(OrganisationPortalDraft, on_delete=models.PROTECT, related_name="ai_design_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=60)
    policy_revision = models.PositiveIntegerField()
    site_revision = models.PositiveIntegerField()
    site_fingerprint = models.CharField(max_length=64)
    brief_fingerprint = models.CharField(max_length=64)
    target_page_slug = models.CharField(max_length=60, blank=True, default="")
    options = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    applied_option = models.PositiveSmallIntegerField(null=True, blank=True)
    applied_revision = models.PositiveIntegerField(null=True, blank=True)
    applied_option_content = models.JSONField(default=dict, blank=True)
    applied_hero_content = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class JournalSiteDraft(models.Model):
    class Theme(models.TextChoices):
        FOREST = "forest", _("Forest")
        INK = "ink", _("Ink")
        TERRACOTTA = "terracotta", _("Terracotta")

    class Layout(models.TextChoices):
        CLASSIC = "classic", _("Classic")
        MAGAZINE = "magazine", _("Magazine")

    journal = models.OneToOneField(Journal, on_delete=models.PROTECT, related_name="site_draft")
    brand_name = models.CharField(_("Site display name"), max_length=200)
    headline = models.CharField(_("Site headline"), max_length=200)
    tagline = models.CharField(_("Site tagline"), max_length=240, blank=True)
    introduction = models.TextField(_("Journal introduction"), max_length=4000)
    theme = models.CharField(_("Colour theme"), max_length=16, choices=Theme.choices, default=Theme.FOREST)
    layout = models.CharField(_("Page layout"), max_length=16, choices=Layout.choices, default=Layout.CLASSIC)
    design_config = models.JSONField(default=dict, blank=True)
    sections_config = models.JSONField(default=list, blank=True)
    pages_config = models.JSONField(default=list, blank=True)
    hero_asset = models.ForeignKey("SiteImageAsset", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    hero_alt = models.CharField(_("Image description"), max_length=200, blank=True)
    hero_fit = models.CharField(_("Image display"), max_length=8, choices=HERO_FIT_CHOICES, default="cover")
    hero_position = models.CharField(_("Image focus"), max_length=8, choices=HERO_POSITION_CHOICES, default="center")
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="journal_sites_created")
    created_at = models.DateTimeField(auto_now_add=True)
    current_release = models.ForeignKey("JournalSiteRelease", on_delete=models.PROTECT, null=True, blank=True, related_name="active_for")


class JournalSiteRelease(models.Model):
    site = models.ForeignKey(JournalSiteDraft, on_delete=models.PROTECT, related_name="releases")
    revision = models.PositiveIntegerField()
    brand_name = models.CharField(_("Site display name"), max_length=200)
    headline = models.CharField(_("Site headline"), max_length=200)
    tagline = models.CharField(_("Site tagline"), max_length=240, blank=True)
    introduction = models.TextField(_("Journal introduction"), max_length=4000)
    theme = models.CharField(_("Colour theme"), max_length=16, choices=JournalSiteDraft.Theme.choices)
    layout = models.CharField(_("Page layout"), max_length=16, choices=JournalSiteDraft.Layout.choices)
    design_config = models.JSONField(default=dict, blank=True)
    sections_config = models.JSONField(default=list, blank=True)
    pages_config = models.JSONField(default=list, blank=True)
    hero_asset = models.ForeignKey("SiteImageAsset", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    hero_alt = models.CharField(_("Image description"), max_length=200, blank=True)
    hero_fit = models.CharField(_("Image display"), max_length=8, choices=HERO_FIT_CHOICES, default="cover")
    hero_position = models.CharField(_("Image focus"), max_length=8, choices=HERO_POSITION_CHOICES, default="center")
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="journal_site_releases")
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["site", "revision"], name="unique_journal_site_revision")]
        ordering = ["-revision", "-pk"]


class JournalSiteEvent(models.Model):
    site = models.ForeignKey(JournalSiteDraft, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    revision = models.PositiveIntegerField()
    source_release = models.ForeignKey(JournalSiteRelease, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class SiteImageAsset(models.Model):
    """Re-encoded local test image; each asset belongs to exactly one site."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal_site = models.ForeignKey(JournalSiteDraft, on_delete=models.PROTECT, null=True, blank=True, related_name="images")
    portal_site = models.ForeignKey(OrganisationPortalDraft, on_delete=models.PROTECT, null=True, blank=True, related_name="images")
    image_data = models.BinaryField()
    media_type = models.CharField(max_length=16)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    default_alt = models.CharField(_("Image description"), max_length=200)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=(
            models.Q(journal_site__isnull=False, portal_site__isnull=True) |
            models.Q(journal_site__isnull=True, portal_site__isnull=False)), name="site_image_exactly_one_owner")]


class SiteImageGenerationRun(models.Model):
    """Private, requester-scoped candidate for a fictional site image."""
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        COMPLETED = 'completed', _('Completed')
        FAILED = 'failed', _('Unavailable')
        CANCELLED = 'cancelled', _('Cancelled')
        DISCARDED = 'discarded', _('Discarded')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal_site = models.ForeignKey(JournalSiteDraft, on_delete=models.PROTECT, null=True, blank=True, related_name='image_generation_runs')
    portal_site = models.ForeignKey(OrganisationPortalDraft, on_delete=models.PROTECT, null=True, blank=True, related_name='image_generation_runs')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    model_name = models.CharField(max_length=100)
    prompt_fingerprint = models.CharField(max_length=64)
    image_description = models.CharField(max_length=200)
    size = models.CharField(max_length=16)
    policy_revision = models.PositiveIntegerField()
    site_revision = models.PositiveIntegerField()
    site_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    error_code = models.CharField(max_length=40, blank=True)
    image_data = models.BinaryField(null=True, blank=True)
    media_type = models.CharField(max_length=16, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    accepted_asset = models.ForeignKey(SiteImageAsset, on_delete=models.SET_NULL, null=True, blank=True, related_name='generation_runs')
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.CheckConstraint(condition=(
            models.Q(journal_site__isnull=False, portal_site__isnull=True) |
            models.Q(journal_site__isnull=True, portal_site__isnull=False)), name='image_gen_exactly_one_site')]


class SiteModuleImageReference(models.Model):
    """Protect images referenced by a draft or an immutable release."""
    asset = models.ForeignKey(SiteImageAsset, on_delete=models.PROTECT, related_name="module_references")
    journal_draft = models.ForeignKey(JournalSiteDraft, on_delete=models.CASCADE, null=True, blank=True, related_name="module_image_references")
    journal_release = models.ForeignKey(JournalSiteRelease, on_delete=models.CASCADE, null=True, blank=True, related_name="module_image_references")
    portal_draft = models.ForeignKey(OrganisationPortalDraft, on_delete=models.CASCADE, null=True, blank=True, related_name="module_image_references")
    portal_release = models.ForeignKey(OrganisationPortalRelease, on_delete=models.CASCADE, null=True, blank=True, related_name="module_image_references")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(
                models.Q(journal_draft__isnull=False, journal_release__isnull=True, portal_draft__isnull=True, portal_release__isnull=True) |
                models.Q(journal_draft__isnull=True, journal_release__isnull=False, portal_draft__isnull=True, portal_release__isnull=True) |
                models.Q(journal_draft__isnull=True, journal_release__isnull=True, portal_draft__isnull=False, portal_release__isnull=True) |
                models.Q(journal_draft__isnull=True, journal_release__isnull=True, portal_draft__isnull=True, portal_release__isnull=False)
            ), name="module_image_one_owner"),
            *[models.UniqueConstraint(fields=["asset", field], name=f"module_image_{field}_uniq")
              for field in ("journal_draft", "journal_release", "portal_draft", "portal_release")],
        ]


class JournalAIPolicy(models.Model):
    """Fail-closed policies for local metadata and journal site design tasks."""

    journal = models.OneToOneField(Journal, on_delete=models.PROTECT, related_name="ai_policy")
    metadata_enabled = models.BooleanField(_("Enable metadata writing suggestions"), default=False)
    allow_author = models.BooleanField(_("Allow submitting authors to request suggestions"), default=False)
    allow_editor = models.BooleanField(_("Allow assigned editors to request suggestions"), default=False)
    site_design_enabled = models.BooleanField(_("Enable journal site design suggestions"), default=False)
    site_modules_enabled = models.BooleanField(_("Enable AI module arrangement suggestions"), default=False)
    site_pages_enabled = models.BooleanField(_("Enable AI independent-page suggestions"), default=False)
    site_page_blocks_enabled = models.BooleanField(_("Enable AI page-block suggestions"), default=False)
    site_page_layout_enabled = models.BooleanField(_("Enable AI page-layout suggestions"), default=False)
    site_image_enabled = models.BooleanField(_("Enable AI image generation"), default=False)
    keyword_suggestions_enabled = models.BooleanField(_("Enable author AI keyword suggestions"), default=False)
    revision_plan_enabled = models.BooleanField(_("Enable author AI revision planning"), default=False)
    editor_revision_comparison_enabled = models.BooleanField(_("Enable editor AI revision comparison"), default=False)
    review_synthesis_enabled = models.BooleanField(_("Enable AI review report comparison"), default=False)
    journal_daily_limit = models.PositiveSmallIntegerField(_("Journal request limit per 24 hours"), default=10,
        validators=[MinValueValidator(1), MaxValueValidator(100)])
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(journal_daily_limit__gte=1, journal_daily_limit__lte=100),
                                   name="ai_policy_daily_limit_range"),
            models.CheckConstraint(condition=models.Q(metadata_enabled=False) | models.Q(allow_author=True) | models.Q(allow_editor=True),
                                   name="ai_policy_enabled_requires_requester"),
        ]


class JournalAIPolicyEvent(models.Model):
    policy = models.ForeignKey(JournalAIPolicy, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    # Configuration values only: never include manuscript text or credentials.
    configuration = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision", "-pk"]
        constraints = [models.UniqueConstraint(fields=["policy", "revision"], name="unique_ai_policy_revision")]


class JournalSiteAIDesignRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Unavailable")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.UUIDField(primary_key=True, editable=False)
    site = models.ForeignKey(JournalSiteDraft, on_delete=models.PROTECT, related_name="ai_design_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=60)
    policy_revision = models.PositiveIntegerField()
    site_revision = models.PositiveIntegerField()
    site_fingerprint = models.CharField(max_length=64)
    brief_fingerprint = models.CharField(max_length=64)
    target_page_slug = models.CharField(max_length=60, blank=True, default="")
    options = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    applied_option = models.PositiveSmallIntegerField(null=True, blank=True)
    applied_revision = models.PositiveIntegerField(null=True, blank=True)
    applied_option_content = models.JSONField(default=dict, blank=True)
    applied_hero_content = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


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
    # NULL is unknown for legacy data; [] is an explicitly empty list.
    keywords_config = models.JSONField(null=True, blank=True, default=None)
    # Contributor metadata is private; after submission only a reviewed request may change it.
    contributors_config = models.JSONField(default=list, blank=True)
    contributors_revision = models.PositiveIntegerField(default=0)
    manuscript = models.FileField(_("Manuscript PDF"), upload_to=manuscript_path, max_length=255)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=["journal", "status"])]
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class ContributorChangeRequest(models.Model):
    """Private before/after snapshots and a human-reviewed outcome."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending review")
        APPROVED = "approved", _("Approved")
        DECLINED = "declined", _("Declined")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="contributor_change_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="contributor_changes_requested")
    reason = models.TextField(max_length=2000)
    before = models.JSONField(default=list)
    proposed = models.JSONField(default=list)
    base_revision = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    acknowledged = models.BooleanField(default=False)
    requested_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="contributor_changes_closed")
    closed_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(max_length=2000, blank=True)
    editor_attested = models.BooleanField(default=False)

    class Meta:
        ordering = ["-requested_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["submission"], condition=models.Q(status="pending"),
                                    name="one_pending_contributor_change"),
            models.CheckConstraint(condition=models.Q(status__in=["pending", "approved", "declined", "withdrawn"]),
                                   name="valid_contributor_change_status"),
        ]


def contributor_evidence_path(instance, filename):
    # Never retain user-provided names or paths in private storage.
    return f"contributor-evidence/{uuid.uuid4().hex}.pdf"


class ContributorEvidence(models.Model):
    """Private, fictional test evidence attached to one author-change request."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    change = models.ForeignKey(ContributorChangeRequest, on_delete=models.PROTECT, related_name="evidence_files")
    label = models.CharField(max_length=120)
    file = models.FileField(upload_to=contributor_evidence_path, max_length=255)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="contributor_evidence_uploaded")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class ContributorConfirmationEvent(models.Model):
    """Append-only manual checks refer to a position in an immutable snapshot."""
    class Side(models.TextChoices):
        ORIGINAL = "original", _("Original list")
        PROPOSED = "proposed", _("Proposed list")

    class State(models.TextChoices):
        UNCHECKED = "unchecked", _("Not checked")
        CONFIRMED = "confirmed", _("Evidence checked")
        DISPUTED = "disputed", _("Disputed or not agreed")

    change = models.ForeignKey(ContributorChangeRequest, on_delete=models.PROTECT, related_name="confirmation_events")
    side = models.CharField(max_length=10, choices=Side.choices)
    position = models.PositiveSmallIntegerField()
    state = models.CharField(max_length=12, choices=State.choices)
    evidence = models.ForeignKey(ContributorEvidence, on_delete=models.PROTECT, null=True, blank=True, related_name="confirmation_events")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="contributor_confirmations_recorded")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(side__in=["original", "proposed"]), name="valid_confirmation_side"),
            models.CheckConstraint(condition=models.Q(position__lt=100), name="valid_confirmation_position"),
            models.CheckConstraint(condition=models.Q(state__in=["unchecked", "confirmed", "disputed"]), name="valid_confirmation_state"),
            models.CheckConstraint(condition=~models.Q(state="confirmed") | models.Q(evidence__isnull=False),
                                   name="confirmed_requires_evidence"),
        ]


class ContributorResponseRequest(models.Model):
    """One manually mapped test account for one author-list entry."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Awaiting response")
        AGREED = "agreed", _("Agreed")
        DISAGREED = "disagreed", _("Disagreed")
        REVOKED = "revoked", _("Cancelled")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    change = models.ForeignKey(ContributorChangeRequest, on_delete=models.PROTECT, related_name="author_response_requests")
    side = models.CharField(max_length=10, choices=ContributorConfirmationEvent.Side.choices)
    position = models.PositiveSmallIntegerField()
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="authorship_responses")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="authorship_response_requests_made")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    snapshot_digest = models.CharField(max_length=64)
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="authorship_response_requests_revoked")
    revocation_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["change", "side", "position"], condition=~models.Q(status="revoked"), name="one_live_author_response_per_entry"),
            models.UniqueConstraint(fields=["change", "side", "recipient"], condition=~models.Q(status="revoked"), name="one_live_response_account_per_list"),
            models.CheckConstraint(condition=models.Q(side__in=["original", "proposed"]), name="valid_response_side"),
            models.CheckConstraint(condition=models.Q(position__lt=100), name="valid_response_position"),
            models.CheckConstraint(condition=models.Q(status__in=["pending", "agreed", "disagreed", "revoked"]), name="valid_author_response_status"),
        ]


class ContributorResponseEvent(models.Model):
    response_request = models.ForeignKey(ContributorResponseRequest, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="authorship_response_events")
    action = models.CharField(max_length=12, choices=[("issued", _("Assigned")), ("agreed", _("Agreed")),
        ("disagreed", _("Disagreed")), ("revoked", _("Cancelled"))])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


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
    # Empty for legacy checks: the editor must review the current file again.
    review_copy_checked_sha256 = models.CharField(max_length=64, blank=True)
    review_metadata_checked_sha256 = models.CharField(max_length=64, blank=True)
    author_response = models.TextField(_("Response to the editor"), max_length=20000, blank=True)
    # Older records have no trustworthy version-specific metadata; NULL means unknown.
    title_snapshot = models.CharField(_("Version title"), max_length=250, null=True, blank=True)
    abstract_snapshot = models.TextField(_("Version abstract"), max_length=10000, null=True, blank=True)
    keywords_snapshot = models.JSONField(null=True, blank=True, default=None)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "number"], name="unique_submission_version"),
            models.CheckConstraint(condition=models.Q(number__gte=1), name="positive_version_number"),
            models.CheckConstraint(condition=(models.Q(title_snapshot__isnull=True, abstract_snapshot__isnull=True) |
                models.Q(title_snapshot__isnull=False, abstract_snapshot__isnull=False)), name="version_metadata_both_or_none"),
        ]
        ordering = ["number"]

    @property
    def metadata_recorded(self):
        return self.title_snapshot is not None and self.abstract_snapshot is not None

    @property
    def reviewer_metadata_visible(self):
        # Reviewer-facing templates use this rather than trusting live metadata.
        from .review_copy import reviewer_metadata_is_visible
        return reviewer_metadata_is_visible(self)


def supporting_file_path(instance, filename):
    # Do not use an untrusted filename or path as the storage key.
    return f"supporting/{uuid.uuid4().hex}.pdf"


class SupportingFile(models.Model):
    """Private, version-bound fictional supporting file in the local prototype."""
    class Visibility(models.TextChoices):
        EDITOR = "editor", _("Editorial only")
        REVIEWER = "reviewer", _("Reviewer copy")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="supporting_files")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="supporting_files")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="supporting_files_uploaded")
    file = models.FileField(upload_to=supporting_file_path, max_length=255)
    original_name = models.CharField(max_length=180)
    visibility = models.CharField(max_length=10, choices=Visibility.choices)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="supporting_files_checked")
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_fingerprint = models.CharField(max_length=64, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="supporting_files_removed")

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(visibility__in=["editor", "reviewer"]), name="supporting_visibility_valid"),
            models.CheckConstraint(condition=(models.Q(checked_at__isnull=True, checked_by__isnull=True, checked_fingerprint="") |
                (models.Q(visibility="reviewer", checked_at__isnull=False, checked_by__isnull=False) &
                 ~models.Q(checked_fingerprint=""))), name="supporting_review_check_valid"),
            models.CheckConstraint(condition=(models.Q(removed_at__isnull=True, removed_by__isnull=True) |
                models.Q(removed_at__isnull=False, removed_by__isnull=False)), name="supporting_removal_consistent"),
        ]


class ReplacedDraftFile(models.Model):
    """A retained private file replaced while the submission was still a draft."""
    class Kind(models.TextChoices):
        MANUSCRIPT = "manuscript", _("Manuscript")
        REVIEW_COPY = "review_copy", _("Reviewer copy")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="replaced_draft_files")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="replaced_draft_files")
    replaced_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="replaced_draft_files")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    file = models.FileField(max_length=255)
    replaced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["replaced_at", "pk"]
        constraints = [models.CheckConstraint(condition=models.Q(kind__in=["manuscript", "review_copy"]),
                                              name="replaced_draft_file_kind_valid")]


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
    # An optional date in the local test system's UTC calendar; it never closes the task.
    due_on = models.DateField(null=True, blank=True)
    deadline_revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["round", "reviewer"], name="unique_reviewer_per_round")]
        ordering = ["-created_at"]


class ReviewAIConsent(models.Model):
    """Narrow permission for a future review-report AI task; never enables a task itself."""
    class Subject(models.TextChoices):
        AUTHOR = "author", _("Submitting author")
        REVIEWER = "reviewer", _("Reviewer")

    class EndReason(models.TextChoices):
        WITHDRAWN = "withdrawn", _("Withdrawn")
        SUPERSEDED = "superseded", _("Superseded")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="review_ai_consents")
    review_round = models.ForeignKey(ReviewRound, on_delete=models.PROTECT, related_name="review_ai_consents")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="review_ai_consents")
    assignment = models.ForeignKey(ReviewAssignment, on_delete=models.PROTECT, null=True, blank=True, related_name="review_ai_consents")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_ai_consents")
    subject = models.CharField(max_length=10, choices=Subject.choices)
    provider = models.CharField(max_length=30)
    purpose = models.CharField(max_length=60)
    source_fingerprint = models.CharField(max_length=64)
    granted_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=12, choices=EndReason.choices, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(models.Q(subject="author", assignment__isnull=True) |
                                                models.Q(subject="reviewer", assignment__isnull=False)),
                                   name="review_ai_subject_assignment"),
            models.CheckConstraint(condition=(models.Q(ended_at__isnull=True, end_reason="") |
                                                models.Q(ended_at__isnull=False, end_reason__in=["withdrawn", "superseded"])),
                                   name="review_ai_consent_end_consistent"),
            models.UniqueConstraint(fields=["review_round", "provider", "purpose"],
                                    condition=models.Q(subject="author", ended_at__isnull=True),
                                    name="review_ai_one_author_round"),
            models.UniqueConstraint(fields=["assignment", "provider", "purpose"],
                                    condition=models.Q(subject="reviewer", ended_at__isnull=True),
                                    name="review_ai_one_reviewer_report"),
        ]


class ReviewAIPermissionRequest(models.Model):
    """An in-app request for one person's permission, bound to a particular source."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Awaiting response")
        GRANTED = "granted", _("Permission recorded")
        DECLINED = "declined", _("Declined")
        OUTDATED = "outdated", _("Source changed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="review_ai_requests")
    review_round = models.ForeignKey(ReviewRound, on_delete=models.PROTECT, related_name="review_ai_requests")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="review_ai_requests")
    assignment = models.ForeignKey(ReviewAssignment, on_delete=models.PROTECT, null=True, blank=True, related_name="review_ai_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_ai_requests_sent")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_ai_requests_received")
    subject = models.CharField(max_length=10, choices=ReviewAIConsent.Subject.choices)
    provider = models.CharField(max_length=30)
    purpose = models.CharField(max_length=60)
    source_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=(models.Q(subject="author", assignment__isnull=True) |
                                                models.Q(subject="reviewer", assignment__isnull=False)),
                                   name="review_ai_request_subject_assignment"),
            models.CheckConstraint(condition=(models.Q(status="pending", responded_at__isnull=True) |
                                                models.Q(status__in=["granted", "declined", "outdated"], responded_at__isnull=False)),
                                   name="review_ai_request_status_consistent"),
            models.UniqueConstraint(fields=["review_round", "recipient", "provider", "purpose", "source_fingerprint"],
                condition=models.Q(subject="author", status__in=["pending", "granted", "declined"]),
                name="review_ai_request_author_scope"),
            models.UniqueConstraint(fields=["assignment", "recipient", "provider", "purpose", "source_fingerprint"],
                condition=models.Q(subject="reviewer", status__in=["pending", "granted", "declined"]),
                name="review_ai_request_reviewer_scope"),
        ]


class ReviewAIReplyNotice(models.Model):
    """A historical in-app reply, private to the editor who asked."""
    class Outcome(models.TextChoices):
        GRANTED = "granted", _("Permission granted")
        DECLINED = "declined", _("Permission declined")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    permission_request = models.OneToOneField(ReviewAIPermissionRequest, on_delete=models.PROTECT,
                                               related_name="editor_reply_notice")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                  related_name="review_ai_reply_notices")
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=models.Q(outcome__in=["granted", "declined"]),
                                              name="review_ai_reply_valid_outcome")]


class ReviewSynthesisRun(models.Model):
    """A private request bound to an editor, a round and current consents."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Unavailable")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.UUIDField(primary_key=True, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="review_synthesis_runs")
    review_round = models.ForeignKey(ReviewRound, on_delete=models.PROTECT, related_name="review_synthesis_runs")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="review_synthesis_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_synthesis_runs")
    author_consent = models.ForeignKey(ReviewAIConsent, on_delete=models.PROTECT, related_name="author_synthesis_runs")
    sources = models.JSONField(default=list)
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=60)
    policy_revision = models.PositiveIntegerField()
    source_fingerprint = models.CharField(max_length=64)
    input_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class ReviewDeadlineEvent(models.Model):
    """Append-only record of an editor changing a fictional review deadline."""
    assignment = models.ForeignKey(ReviewAssignment, on_delete=models.PROTECT, related_name="deadline_events")
    previous_date = models.DateField(null=True, blank=True)
    new_date = models.DateField(null=True, blank=True)
    revision = models.PositiveIntegerField()
    reason = models.CharField(max_length=500)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_deadlines_recorded")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [models.UniqueConstraint(fields=["assignment", "revision"], name="unique_review_deadline_revision")]


class ReviewNotice(models.Model):
    """Private in-app notice linked to one existing fictional review task."""
    class Kind(models.TextChoices):
        INVITATION = "invitation", _("Review invitation")
        DEADLINE = "deadline", _("Review deadline changed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assignment = models.ForeignKey(ReviewAssignment, on_delete=models.PROTECT, related_name="notices")
    # Pin the recipient at creation; a later account change must not transfer history.
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_notices")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    # Invitation uses revision zero; deadline notices use the recorded deadline revision.
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["assignment", "kind", "revision"], name="unique_review_notice_event"),
            models.CheckConstraint(condition=(models.Q(kind="invitation", revision=0) |
                models.Q(kind="deadline", revision__gte=1)), name="valid_review_notice_event"),
        ]


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
    policy_revision = models.PositiveIntegerField(null=True, blank=True)
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


class RevisionPlanRun(models.Model):
    """One expressly confirmed, author-only local test request; inputs are not saved."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Unavailable")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.UUIDField(primary_key=True, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="revision_plan_runs")
    decision = models.ForeignKey(EditorialDecision, on_delete=models.PROTECT, related_name="revision_plan_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="revision_plan_runs")
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=60)
    policy_revision = models.PositiveIntegerField()
    input_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    confirmed_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]



class KeywordSuggestionRun(models.Model):
    """One expressly confirmed, author-only keyword request for a fictional draft."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Unavailable")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.UUIDField(primary_key=True, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="keyword_suggestion_runs")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="keyword_suggestion_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=60)
    policy_revision = models.PositiveIntegerField()
    input_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

class EditorRevisionComparisonConsent(models.Model):
    """A submitting author's source-bound permission for one editorial AI task."""
    class EndReason(models.TextChoices):
        WITHDRAWN = "withdrawn", _("Withdrawn")
        SUPERSEDED = "superseded", _("Superseded")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="editor_ai_consents")
    review_round = models.ForeignKey(ReviewRound, on_delete=models.PROTECT, related_name="editor_ai_consents")
    decision = models.ForeignKey(EditorialDecision, on_delete=models.PROTECT, related_name="editor_ai_consents")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="editor_ai_consents")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="editor_ai_consents")
    provider = models.CharField(max_length=20)
    purpose = models.CharField(max_length=60)
    source_fingerprint = models.CharField(max_length=64)
    granted_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=12, choices=EndReason.choices, blank=True)

    class Meta:
        ordering = ["-granted_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "granted_by", "provider", "purpose"],
                condition=models.Q(ended_at__isnull=True), name="one_active_editor_ai_consent"),
            models.CheckConstraint(condition=(models.Q(ended_at__isnull=True, end_reason="") |
                models.Q(ended_at__isnull=False, end_reason__in=["withdrawn", "superseded"])),
                name="editor_ai_consent_end_consistent"),
        ]


class EditorRevisionComparisonRun(models.Model):
    """A private, confirmed editor request bound to one current revision package."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Unavailable")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.UUIDField(primary_key=True, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="editor_revision_runs")
    review_round = models.ForeignKey(ReviewRound, on_delete=models.PROTECT, related_name="editor_revision_runs")
    decision = models.ForeignKey(EditorialDecision, on_delete=models.PROTECT, related_name="editor_revision_runs")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="editor_revision_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="editor_revision_runs")
    author_consent = models.ForeignKey(EditorRevisionComparisonConsent, on_delete=models.PROTECT,
                                       null=True, blank=True, related_name="comparison_runs")
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=60)
    policy_revision = models.PositiveIntegerField()
    source_fingerprint = models.CharField(max_length=64)
    input_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class RevisionResponseDraft(models.Model):
    """Author-owned, private point-by-point response for a specific decision."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="response_drafts")
    decision = models.ForeignKey(EditorialDecision, on_delete=models.PROTECT, related_name="response_drafts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="revision_response_drafts")
    source_run = models.ForeignKey(RevisionPlanRun, on_delete=models.PROTECT, null=True, blank=True, related_name="response_drafts")
    entries = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["decision", "author"], name="one_response_draft_per_author_decision")]


class EditorComparisonPermissionRequest(models.Model):
    """One in-app request for an author's specific current-revision permission."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Awaiting author")
        GRANTED = "granted", _("Allowed by author")
        DECLINED = "declined", _("Declined by author")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="editor_ai_permission_requests")
    review_round = models.ForeignKey(ReviewRound, on_delete=models.PROTECT, related_name="editor_ai_permission_requests")
    decision = models.ForeignKey(EditorialDecision, on_delete=models.PROTECT, related_name="editor_ai_permission_requests")
    version = models.ForeignKey(SubmissionVersion, on_delete=models.PROTECT, related_name="editor_ai_permission_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="editor_ai_permission_requests_made")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="editor_ai_permission_requests_received")
    provider = models.CharField(max_length=20)
    purpose = models.CharField(max_length=60)
    source_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "review_round", "decision", "version", "recipient",
                                             "provider", "purpose", "source_fingerprint"], name="one_editor_ai_permission_request_source"),
            models.CheckConstraint(condition=(models.Q(status="pending", responded_at__isnull=True) |
                models.Q(status__in=["granted", "declined"], responded_at__isnull=False)), name="editor_ai_permission_response_consistent"),
        ]


class EditorAIPermissionReplyNotice(models.Model):
    """Historical author response, visible only to the original requesting editor."""
    class Outcome(models.TextChoices):
        GRANTED = "granted", _("Author allowed this request")
        DECLINED = "declined", _("Author declined this request")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    permission_request = models.OneToOneField(EditorComparisonPermissionRequest, on_delete=models.PROTECT,
                                               related_name="editor_reply_notice")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                  related_name="editor_ai_permission_replies")
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=models.Q(outcome__in=["granted", "declined"]),
                                              name="editor_ai_reply_valid_outcome")]


class AuthorNotice(models.Model):
    """Private local notice for the submitting author, pinned to the original account."""
    class Kind(models.TextChoices):
        SUBMITTED = "submitted", _("Submission received")
        DECISION = "decision", _("Editorial decision recorded")
        AUTHOR_CHANGE = "author_change", _("Author change reviewed")
        AI_PERMISSION = "ai_permission", _("AI permission requested")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="author_notices")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submission_notices")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    decision = models.ForeignKey(EditorialDecision, on_delete=models.PROTECT, null=True, blank=True, related_name="author_notices")
    author_change = models.ForeignKey(ContributorChangeRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="author_notices")
    ai_permission_request = models.OneToOneField(EditorComparisonPermissionRequest, on_delete=models.PROTECT,
        null=True, blank=True, related_name="author_notice")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["submission"], condition=models.Q(kind="submitted"), name="one_author_submission_notice"),
            models.UniqueConstraint(fields=["decision"], condition=models.Q(kind="decision"), name="one_author_decision_notice"),
            models.UniqueConstraint(fields=["author_change"], condition=models.Q(kind="author_change"), name="one_author_change_notice"),
            models.CheckConstraint(condition=(
                models.Q(kind="submitted", decision__isnull=True, author_change__isnull=True, ai_permission_request__isnull=True) |
                models.Q(kind="decision", decision__isnull=False, author_change__isnull=True, ai_permission_request__isnull=True) |
                models.Q(kind="author_change", decision__isnull=True, author_change__isnull=False, ai_permission_request__isnull=True) |
                models.Q(kind="ai_permission", decision__isnull=True, author_change__isnull=True, ai_permission_request__isnull=False)),
                name="valid_author_notice_source"),
        ]


def publication_proof_path(instance, filename):
    return f"publication-proofs/{uuid.uuid4().hex}.pdf"


class JournalIssue(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="test_issues")
    volume = models.PositiveIntegerField(_("Volume"))
    number = models.PositiveIntegerField(_("Issue number"))
    year = models.PositiveIntegerField(_("Year"))
    title = models.CharField(_("Issue title"), max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="test_issues_created")
    created_at = models.DateTimeField(auto_now_add=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="test_issues_released")
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["journal", "volume", "number"], name="unique_test_issue_per_journal"),
            models.CheckConstraint(condition=models.Q(volume__gte=1, volume__lte=9999), name="test_issue_volume_range"),
            models.CheckConstraint(condition=models.Q(number__gte=1, number__lte=9999), name="test_issue_number_range"),
            models.CheckConstraint(condition=models.Q(year__gte=1000, year__lte=9999), name="test_issue_year_range"),
        ]
        ordering = ["-year", "-volume", "-number"]


class PublicationDraft(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="publication_draft")
    decision = models.OneToOneField(EditorialDecision, on_delete=models.PROTECT, related_name="publication_draft")
    issue = models.ForeignKey(JournalIssue, on_delete=models.PROTECT, null=True, blank=True, related_name="articles")
    title = models.CharField(_("Public title"), max_length=250)
    authors = models.TextField(_("Public author names"), max_length=2000, blank=True)
    abstract = models.TextField(_("Public abstract"), max_length=10000)
    keywords_config = models.JSONField(null=True, blank=True, default=None)
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


class ProductionAssignment(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        REVOKED = "revoked", _("Removed")
        RECUSED = "recused", _("Recused")

    draft = models.ForeignKey(PublicationDraft, on_delete=models.PROTECT, related_name="production_assignments")
    producer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="production_assignments")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="production_assignments_made")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="production_assignments_ended")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["draft", "producer"], condition=models.Q(status="active"), name="one_active_producer_per_draft")]
        ordering = ["-created_at", "-pk"]


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


class LocalLoginThrottleBucket(models.Model):
    """Short-lived, pseudonymous counters for fictional local tests only."""
    scope = models.CharField(max_length=8, choices=[("source", "Source"), ("pair", "Source and username")])
    digest = models.CharField(max_length=64)
    window_start = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["scope", "digest", "window_start"], name="unique_local_login_bucket")]


class LocalReviewMailJob(models.Model):
    """A simulated invitation only; no external delivery is permitted."""
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending simulation")
        SIMULATED = "simulated", _("Simulated")
        SUPPRESSED = "suppressed", _("Suppressed")

    notice = models.OneToOneField(ReviewNotice, on_delete=models.PROTECT, related_name="local_mail_job")
    recipient_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(models.Q(status="pending", processed_at__isnull=True) |
                       models.Q(status__in=["simulated", "suppressed"], processed_at__isnull=False)),
            name="local_mail_processing_state",
        )]
