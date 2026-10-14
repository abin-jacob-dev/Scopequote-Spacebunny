from django.db import migrations, models

import accounts.services


class Migration(migrations.Migration):
    dependencies = [
        ("quotes", "0003_quoteevent_quotes_event_link_lifecycle_unique"),
    ]

    operations = [
        migrations.AlterField(
            model_name="quote",
            name="sender_website",
            field=models.URLField(
                blank=True,
                max_length=2048,
                validators=[accounts.services.validate_website_url],
            ),
        ),
    ]
