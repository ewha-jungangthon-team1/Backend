from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("measurements", "0003_sensorreading_material_moisture_percent"),
    ]

    operations = [
        migrations.AddField(
            model_name="measurementsession",
            name="include_in_report",
            field=models.BooleanField(default=True),
        ),
    ]
