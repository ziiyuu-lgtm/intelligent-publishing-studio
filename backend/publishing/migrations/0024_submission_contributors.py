from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0023_page_layout_ai_policy")]
    operations = [
        migrations.AddField(model_name="submission", name="contributors_config",
                            field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="submission", name="contributors_revision",
                            field=models.PositiveIntegerField(default=0)),
        # Existing records stay empty; never infer identities or silently discard added data.
        migrations.RunPython(migrations.RunPython.noop),
    ]
