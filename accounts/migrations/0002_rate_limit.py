from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="RateLimitBucket",
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
                ("key", models.CharField(editable=False, max_length=64)),
                ("window_start", models.DateTimeField()),
                ("counter", models.PositiveIntegerField(default=0)),
                ("expires_at", models.DateTimeField()),
            ],
            options={
                "ordering": ("-window_start",),
                "indexes": [
                    models.Index(
                        fields=("expires_at",),
                        name="accounts_rate_expiry_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("key", "window_start"),
                        name="accounts_rate_key_window_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(counter__gte=0),
                        name="accounts_rate_counter_nonnegative",
                    ),
                ],
            },
        ),
    ]
