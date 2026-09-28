from datetime import timedelta

from django.db import migrations, models
from django.db.models import F, Q
from django.utils import timezone


def expire_legacy_tokens(apps, schema_editor):
    ApiToken = apps.get_model("core", "ApiToken")
    now = timezone.now()
    ApiToken.objects.filter(expires_at__isnull=True, revoked_at__isnull=True).update(
        expires_at=now,
        revoked_at=now,
    )
    ApiToken.objects.filter(expires_at__isnull=True).update(expires_at=now)
    ApiToken.objects.filter(expires_at__lte=F("created_at"), revoked_at__isnull=True).update(
        revoked_at=now
    )
    ApiToken.objects.filter(expires_at__lte=F("created_at")).update(
        expires_at=F("created_at") + timedelta(microseconds=1)
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_platform_guards")]

    operations = [
        migrations.RunPython(expire_legacy_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="apitoken",
            name="expires_at",
            field=models.DateTimeField(),
        ),
        migrations.AddConstraint(
            model_name="apitoken",
            constraint=models.CheckConstraint(
                condition=Q(expires_at__gt=F("created_at")),
                name="api_token_expiry_after_creation",
            ),
        ),
    ]
