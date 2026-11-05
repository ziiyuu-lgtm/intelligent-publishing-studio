import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0052_review_metadata_checked_sha256"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PublishedIssueProposal",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_fingerprint", models.CharField(max_length=64)),
                ("issue_snapshot", models.JSONField()),
                ("status", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"),
                                                     ("declined", "Declined"), ("withdrawn", "Withdrawn")],
                                           default="pending", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("draft", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                            related_name="published_issue_proposals", to="publishing.publicationdraft")),
                ("issue", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                            related_name="online_first_proposals", to="publishing.journalissue")),
                ("proposed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                  related_name="published_issue_proposals_made", to=settings.AUTH_USER_MODEL)),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                                  related_name="published_issue_proposals_resolved", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(condition=models.Q(status="pending"), fields=("draft",), name="one_pending_published_issue"),
                    models.CheckConstraint(
                        condition=(models.Q(status="pending", resolved_at__isnull=True, resolved_by__isnull=True) |
                                   models.Q(status__in=["accepted", "declined", "withdrawn"], resolved_at__isnull=False, resolved_by__isnull=False)),
                        name="published_issue_resolution_state",
                    ),
                ],
            },
        ),
    ]
