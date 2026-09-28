import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0004_attachment_versions")]

    operations = [
        migrations.CreateModel(
            name="LoginAttemptThrottle",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "dimension",
                    models.CharField(
                        choices=[("account", "Account"), ("client", "Client")],
                        max_length=16,
                    ),
                ),
                ("key_hash", models.CharField(max_length=64)),
                ("failure_count", models.PositiveSmallIntegerField(default=0)),
                ("window_started_at", models.DateTimeField()),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [models.Index(fields=["updated_at"], name="login_throttle_updated_idx")],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("dimension", "key_hash"),
                        name="uniq_login_throttle_dimension_key",
                    )
                ],
            },
        )
    ]
