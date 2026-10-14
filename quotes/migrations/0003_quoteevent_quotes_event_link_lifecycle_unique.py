from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("quotes", "0002_quote_logo_byte_size_quote_logo_content_type"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="quoteevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("event_type__in", ("viewed", "revoked", "expired")),
                    ("share_link__isnull", False),
                ),
                fields=("share_link", "event_type"),
                name="quotes_event_link_lifecycle_unique",
            ),
        ),
    ]
