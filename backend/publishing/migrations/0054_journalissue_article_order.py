from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0053_publishedissueproposal")]

    operations = [
        migrations.AddField(
            model_name="journalissue",
            name="article_order",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
