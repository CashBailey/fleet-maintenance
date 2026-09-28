from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("integrations", "0001_initial")]

    operations = [
        migrations.RemoveConstraint(
            model_name="telematicsmessage",
            name="uniq_device_message_id",
        ),
        migrations.RemoveConstraint(
            model_name="telematicsmessage",
            name="uniq_device_canonical_message",
        ),
        migrations.AddConstraint(
            model_name="telematicsmessage",
            constraint=models.UniqueConstraint(
                fields=("device", "message_id"),
                condition=~models.Q(message_id="") & models.Q(status="accepted"),
                name="uniq_device_message_id",
            ),
        ),
        migrations.AddConstraint(
            model_name="telematicsmessage",
            constraint=models.UniqueConstraint(
                fields=("device", "canonical_hash"),
                condition=models.Q(status="accepted"),
                name="uniq_device_canonical_message",
            ),
        ),
    ]
