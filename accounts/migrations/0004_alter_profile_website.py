from django.db import migrations, models

import accounts.services


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_deletion_tombstone"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="website",
            field=models.URLField(
                blank=True,
                max_length=2048,
                validators=[accounts.services.validate_website_url],
            ),
        ),
    ]
