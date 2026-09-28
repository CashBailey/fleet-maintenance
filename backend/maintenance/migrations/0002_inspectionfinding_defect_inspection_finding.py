import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assets', '0002_initial'),
        ('core', '0002_platform_guards'),
        ('maintenance', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='InspectionFinding',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(choices=[('Open', 'Open'), ('Acknowledged', 'Acknowledged'), ('Resolved', 'Resolved')], default='Open', max_length=16)),
                ('severity', models.CharField(choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('safety', 'Safety')], default='medium', max_length=10)),
                ('safety_related', models.BooleanField(default=False)),
                ('description', models.TextField(max_length=5000)),
                ('asset', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='inspection_findings', to='assets.asset')),
                ('inspection', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='findings', to='maintenance.inspection')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='core.organization')),
                ('reported_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='inspection_findings_reported', to=settings.AUTH_USER_MODEL)),
                ('response', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='finding', to='maintenance.inspectionresponse')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='defect',
            name='inspection_finding',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='defect', to='maintenance.inspectionfinding'),
        ),
    ]
