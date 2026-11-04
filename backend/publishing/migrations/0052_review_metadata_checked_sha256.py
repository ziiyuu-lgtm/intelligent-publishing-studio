from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0051_review_copy_checked_sha256")]

    operations = [
        migrations.AddField(
            model_name="submissionversion",
            name="review_metadata_checked_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
