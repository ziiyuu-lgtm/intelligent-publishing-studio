from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0050_local_test_registration")]

    operations = [
        migrations.AddField(
            model_name="submissionversion",
            name="review_copy_checked_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
