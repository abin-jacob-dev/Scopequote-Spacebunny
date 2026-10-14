import uuid

import django.core.validators
import django.db.models.deletion
import django.db.models.functions.text
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import accounts.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="User",
            fields=[
                ("password", models.CharField(max_length=128, verbose_name="password")),
                (
                    "last_login",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="last login"
                    ),
                ),
                (
                    "is_superuser",
                    models.BooleanField(
                        default=False,
                        help_text="Designates that this user has all permissions without explicitly assigning them.",
                        verbose_name="superuser status",
                    ),
                ),
                (
                    "first_name",
                    models.CharField(
                        blank=True, max_length=150, verbose_name="first name"
                    ),
                ),
                (
                    "last_name",
                    models.CharField(
                        blank=True, max_length=150, verbose_name="last name"
                    ),
                ),
                (
                    "is_staff",
                    models.BooleanField(
                        default=False,
                        help_text="Designates whether the user can log into this admin site.",
                        verbose_name="staff status",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Designates whether this user should be treated as active. Unselect this instead of deleting accounts.",
                        verbose_name="active",
                    ),
                ),
                (
                    "date_joined",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="date joined"
                    ),
                ),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("email_verified_at", models.DateTimeField(blank=True, null=True)),
                ("last_activity_at", models.DateTimeField(blank=True, null=True)),
                (
                    "groups",
                    models.ManyToManyField(
                        blank=True,
                        help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.group",
                        verbose_name="groups",
                    ),
                ),
                (
                    "user_permissions",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Specific permissions for this user.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.permission",
                        verbose_name="user permissions",
                    ),
                ),
            ],
            managers=[
                ("objects", accounts.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name="Profile",
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
                ("full_name", models.CharField(blank=True, max_length=160)),
                ("business_name", models.CharField(blank=True, max_length=255)),
                (
                    "phone",
                    models.CharField(
                        blank=True,
                        max_length=40,
                        validators=[accounts.models.validate_phone_number],
                    ),
                ),
                (
                    "website",
                    models.URLField(
                        blank=True,
                        max_length=2048,
                        validators=[
                            django.core.validators.URLValidator(schemes=["https"])
                        ],
                    ),
                ),
                (
                    "default_currency",
                    models.CharField(
                        choices=[("USD", "USD"), ("EUR", "EUR"), ("GBP", "GBP")],
                        default="USD",
                        max_length=3,
                    ),
                ),
                (
                    "default_deposit_percentage",
                    models.PositiveSmallIntegerField(
                        default=50,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(100),
                            accounts.models.validate_five_point_increment,
                        ],
                    ),
                ),
                (
                    "default_quote_validity_days",
                    models.PositiveSmallIntegerField(
                        default=30,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(90),
                        ],
                    ),
                ),
                (
                    "default_accent_color",
                    models.CharField(
                        default="#E85D3F",
                        max_length=7,
                        validators=[
                            django.core.validators.RegexValidator(
                                code="invalid",
                                message="Accent color must use #RRGGBB format.",
                                regex="^#[0-9A-Fa-f]{6}$",
                            )
                        ],
                    ),
                ),
                (
                    "logo",
                    models.ImageField(
                        blank=True, upload_to=accounts.models.profile_logo_upload_path
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("email"),
                name="accounts_user_email_ci_unique",
            ),
        ),
    ]
