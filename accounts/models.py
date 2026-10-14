from __future__ import annotations

import uuid
from pathlib import Path

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.services import canonicalize_email, validate_website_url


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields: object):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=canonicalize_email(email), **extra_fields)
        user.password = make_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self,
        email: str,
        password: str | None = None,
        **extra_fields: object,
    ):
        return self._create_user(email, password, **extra_fields)

    def create_superuser(
        self,
        email: str,
        password: str | None = None,
        **extra_fields: object,
    ):
        extra_fields["is_staff"] = True
        extra_fields["is_superuser"] = True
        return self._create_user(email, password, **extra_fields)

    def get_by_natural_key(self, username: str):
        return self.get(email=canonicalize_email(username))


class User(AbstractUser):
    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    username = None
    email = models.EmailField(unique=True)
    email_verified_at = models.DateTimeField(blank=True, null=True)
    last_activity_at = models.DateTimeField(blank=True, null=True)
    is_quarantined = models.BooleanField(default=False, editable=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="accounts_user_email_ci_unique",
            )
        ]

    def save(self, *args, **kwargs):
        self.email = canonicalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.email

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_verified(self) -> bool:
        return self.is_email_verified

    @property
    def can_publish(self) -> bool:
        return self.is_active and not self.is_quarantined and self.is_email_verified


class DeletionSource(models.TextChoices):
    OWNER = "owner", "Owner request"
    RETENTION = "retention", "Retention job"
    ADMINISTRATOR = "administrator", "Administrator action"


class RestoreApplicationState(models.TextChoices):
    PENDING = "pending", "Pending"
    APPLIED = "applied", "Applied"
    FAILED = "failed", "Failed"


class DeletionTombstone(models.Model):
    account_id = models.UUIDField(unique=True, editable=False)
    deleted_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(
        choices=DeletionSource.choices,
        default=DeletionSource.OWNER,
        max_length=24,
    )
    quarantined = models.BooleanField(default=True)
    restore_status = models.CharField(
        choices=RestoreApplicationState.choices,
        default=RestoreApplicationState.PENDING,
        max_length=16,
    )
    restore_applied_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-deleted_at",)
        indexes = [
            models.Index(fields=("deleted_at",), name="accounts_tombstone_deleted_idx"),
            models.Index(
                fields=("restore_status",),
                name="accounts_tombstone_restore_idx",
            ),
        ]

    @property
    def account_uuid(self):
        return self.account_id

    @property
    def deletion_timestamp(self):
        return self.deleted_at

    @property
    def restore_applied(self) -> bool:
        return self.restore_applied_at is not None

    @property
    def restore_state(self) -> str:
        return self.restore_status

    def __str__(self) -> str:
        return f"Deletion tombstone {self.account_id}"


class MediaCleanupTask(models.Model):
    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    storage_key = models.CharField(max_length=1024, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("next_attempt_at", "created_at")
        indexes = [
            models.Index(
                fields=("next_attempt_at",),
                name="accounts_media_cleanup_due_idx",
            )
        ]

    def __str__(self) -> str:
        return f"Media cleanup {self.storage_key}"


class Currency(models.TextChoices):
    USD = "USD", "USD"
    EUR = "EUR", "EUR"
    GBP = "GBP", "GBP"


def validate_five_point_increment(value: int) -> None:
    if value % 5:
        raise ValidationError(
            "Deposit percentage must use five-point increments.",
            code="invalid",
        )


def validate_phone_number(value: str) -> None:
    if not any(character.isdigit() for character in value):
        raise ValidationError("Enter a valid phone number.", code="invalid")
    RegexValidator(
        regex=r"^\+?[0-9 ()-]+$",
        message="Enter a valid phone number.",
        code="invalid",
    )(value)


def profile_logo_upload_path(instance: Profile, filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".png"
    return f"profiles/{instance.user_id}/{uuid.uuid4()}{extension}"


class Profile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    full_name = models.CharField(blank=True, max_length=160)
    business_name = models.CharField(blank=True, max_length=255)
    phone = models.CharField(
        blank=True,
        max_length=40,
        validators=[validate_phone_number],
    )
    website = models.URLField(
        blank=True,
        max_length=2048,
        validators=[validate_website_url],
    )
    default_currency = models.CharField(
        choices=Currency.choices,
        default=Currency.USD,
        max_length=3,
    )
    default_deposit_percentage = models.PositiveSmallIntegerField(
        default=50,
        validators=[
            MinValueValidator(0),
            MaxValueValidator(100),
            validate_five_point_increment,
        ],
    )
    default_quote_validity_days = models.PositiveSmallIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(90)],
    )
    default_accent_color = models.CharField(
        default="#E85D3F",
        max_length=7,
        validators=[
            RegexValidator(
                regex=r"^#[0-9A-Fa-f]{6}$",
                message="Accent color must use #RRGGBB format.",
                code="invalid",
            )
        ],
    )
    logo = models.ImageField(blank=True, upload_to=profile_logo_upload_path)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def create_for_user(cls, user: User) -> Profile:
        profile, _ = cls.objects.get_or_create(user=user)
        return profile

    def __str__(self) -> str:
        return self.full_name or self.user.email


class RateLimitBucket(models.Model):
    key = models.CharField(max_length=64, editable=False)
    window_start = models.DateTimeField()
    counter = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ("-window_start",)
        indexes = [
            models.Index(fields=("expires_at",), name="accounts_rate_expiry_idx")
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("key", "window_start"),
                name="accounts_rate_key_window_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(counter__gte=0),
                name="accounts_rate_counter_nonnegative",
            ),
        ]

    @property
    def key_hash(self) -> str:
        return self.key

    @key_hash.setter
    def key_hash(self, value: str) -> None:
        self.key = value

    @property
    def count(self) -> int:
        return self.counter

    @count.setter
    def count(self, value: int) -> None:
        self.counter = value

    @property
    def window_started_at(self):
        return self.window_start

    @window_started_at.setter
    def window_started_at(self, value) -> None:
        self.window_start = value

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone

        return self.expires_at <= timezone.now()

    def __str__(self) -> str:
        return f"Rate limit bucket {self.key[:12]}"
