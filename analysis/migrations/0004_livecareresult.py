from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("analysis", "0003_analysisreport_ai_result"),
        ("measurements", "0004_measurementsession_include_in_report"),
    ]

    operations = [
        migrations.CreateModel(
            name="LiveCareResult",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("GENERATING", "Generating"),
                            ("READY", "Ready"),
                        ],
                        default="GENERATING",
                        max_length=20,
                    ),
                ),
                ("context_snapshot", models.JSONField(default=dict)),
                ("care_result", models.JSONField(default=dict)),
                (
                    "generated_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("fallback_used", models.BooleanField(default=False)),
                (
                    "fallback_reason",
                    models.CharField(
                        blank=True,
                        max_length=100,
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "session",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="live_care_result",
                        to="measurements.measurementsession",
                    ),
                ),
            ],
        ),
    ]
