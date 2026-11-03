from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0022_journalaipolicy_site_page_blocks_enabled_and_more")]
    operations = [
        migrations.AddField(model_name="journalaipolicy", name="site_page_layout_enabled",
            field=models.BooleanField(default=False, verbose_name="Enable AI page-layout suggestions")),
        migrations.AddField(model_name="organisationaipolicy", name="portal_page_layout_enabled",
            field=models.BooleanField(default=False, verbose_name="Enable AI page-layout suggestions")),
        # Prevent a rollback from silently discarding an enabled policy.
        migrations.RunPython(migrations.RunPython.noop),
    ]
