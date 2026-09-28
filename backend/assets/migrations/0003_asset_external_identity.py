from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0002_initial")]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="external_id",
            field=models.CharField(blank=True, default="", max_length=160),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="asset",
            name="source_system",
            field=models.CharField(blank=True, default="", max_length=40),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.UniqueConstraint(
                condition=~models.Q(external_id=""),
                fields=("organization", "source_system", "external_id"),
                name="uniq_asset_external_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(source_system="", external_id="")
                    | (~models.Q(source_system="") & ~models.Q(external_id=""))
                ),
                name="asset_external_identity_pair",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="meterreading",
            name="uniq_meter_external_reading",
        ),
        migrations.AddConstraint(
            model_name="meterreading",
            constraint=models.UniqueConstraint(
                condition=~models.Q(external_id=""),
                fields=("organization", "source", "external_id"),
                name="uniq_meter_external_reading",
            ),
        ),
    ]
