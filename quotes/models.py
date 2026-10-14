from __future__ import annotations

import copy
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Currency, User, validate_five_point_increment
from accounts.services import validate_website_url
from pricing.constants import PRICING_CONFIG_VERSION

MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")
SNAPSHOT_SCHEMA_VERSION = 1

accent_color_validator = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message="Accent color must use #RRGGBB format.",
    code="invalid",
)


class QuoteStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent"
    VIEWED = "viewed", "Viewed"
    ACCEPTED = "accepted", "Accepted"
    CHANGES_REQUESTED = "changes_requested", "Changes requested"


class Complexity(models.TextChoices):
    STRAIGHTFORWARD = "straightforward", "Straightforward"
    STANDARD = "standard", "Standard"
    COMPLEX = "complex", "Complex"
    ADVANCED = "advanced", "Advanced"


class Urgency(models.TextChoices):
    STANDARD = "standard", "Standard"
    PRIORITY = "priority", "Priority"
    RUSH = "rush", "Rush"


class Experience(models.TextChoices):
    DEVELOPING = "developing", "Developing"
    INTERMEDIATE = "intermediate", "Intermediate"
    EXPERIENCED = "experienced", "Experienced"
    SPECIALIST = "specialist", "Specialist"


class ScopeCategory(models.TextChoices):
    WEBSITE = "website", "Website or product"
    BRAND = "brand", "Brand or visual"
    CONTENT = "content", "Written content"
    VIDEO = "video", "Video or motion"
    STRATEGY = "strategy", "Strategy or consulting"
    MIXED = "mixed", "Mixed or general"


class QuoteVersionReason(models.TextChoices):
    PUBLISHED = "published", "Published"


class QuoteEventType(models.TextChoices):
    CREATED = "created", "Created"
    PUBLISHED = "published", "Published"
    VIEWED = "viewed", "Viewed"
    ACCEPTED = "accepted", "Accepted"
    CHANGES_REQUESTED = "changes_requested", "Changes requested"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"


Status = QuoteStatus
QuoteState = QuoteStatus
QuoteCurrency = Currency
EventType = QuoteEventType
VersionReason = QuoteVersionReason


def _money(value: Decimal | int | float | str) -> Decimal:
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Money values must be valid decimals.") from exc


def _quote_aliases(kwargs: dict[str, Any]) -> None:
    aliases = {
        "title": "project_title",
        "description": "project_description",
        "duration_days": "delivery_days",
        "project_category": "category",
        "revisions": "revision_rounds",
        "revision_count": "revision_rounds",
        "estimated_hours": "effective_hours",
        "pricing_version": "pricing_config_version",
        "recommended_min": "minimum",
        "recommended_minimum": "minimum",
        "recommended_max": "maximum",
        "recommended_maximum": "maximum",
        "brand_color": "accent_color",
        "version_logo_object_key": "logo_object_key",
        "sender_business": "sender_business_name",
        "sender_company": "sender_business_name",
        "company_name": "client_company",
        "client_company_name": "client_company",
        "tax_statement": "taxes",
        "cancellation_text": "cancellation_terms",
    }
    for alias, field_name in aliases.items():
        if alias in kwargs and field_name not in kwargs:
            kwargs[field_name] = kwargs.pop(alias)


def _line_item_aliases(kwargs: dict[str, Any]) -> None:
    aliases = {
        "position": "sort_order",
        "item_description": "description",
    }
    for alias, field_name in aliases.items():
        if alias in kwargs and field_name not in kwargs:
            kwargs[field_name] = kwargs.pop(alias)


def _sequence_aliases(kwargs: dict[str, Any]) -> None:
    aliases = {
        "last_number": "last_allocated_number",
        "last_sequence": "last_allocated_number",
        "sequence_number": "last_allocated_number",
    }
    for alias, field_name in aliases.items():
        if alias in kwargs and field_name not in kwargs:
            kwargs[field_name] = kwargs.pop(alias)


def _version_aliases(kwargs: dict[str, Any]) -> None:
    aliases = {
        "version": "version_number",
        "schema_version": "snapshot_schema_version",
        "snapshot_data": "snapshot",
        "total": "frozen_total",
        "logo_key": "logo_object_key",
        "version_logo_object_key": "logo_object_key",
    }
    for alias, field_name in aliases.items():
        if alias in kwargs and field_name not in kwargs:
            kwargs[field_name] = kwargs.pop(alias)


def _share_link_aliases(kwargs: dict[str, Any]) -> None:
    aliases = {
        "version": "quote_version",
    }
    for alias, field_name in aliases.items():
        if alias in kwargs and field_name not in kwargs:
            kwargs[field_name] = kwargs.pop(alias)


def _event_aliases(kwargs: dict[str, Any]) -> None:
    aliases = {
        "occurred_at": "event_time",
        "timestamp": "event_time",
        "created_at": "event_time",
        "event_timestamp": "event_time",
        "version": "quote_version",
        "idempotency_key": "response_idempotency_key",
        "fingerprint": "payload_fingerprint",
    }
    for alias, field_name in aliases.items():
        if alias in kwargs and field_name not in kwargs:
            kwargs[field_name] = kwargs.pop(alias)


PUBLISHED_STATUSES = frozenset(
    {
        QuoteStatus.SENT,
        QuoteStatus.VIEWED,
        QuoteStatus.ACCEPTED,
        QuoteStatus.CHANGES_REQUESTED,
    }
)
OPERATIONAL_QUOTE_FIELDS = frozenset(
    {
        "status",
        "sent_at",
        "accepted_at",
        "last_viewed_at",
        "updated_at",
    }
)
LEGAL_STATUS_TRANSITIONS = {
    QuoteStatus.DRAFT: frozenset({QuoteStatus.SENT}),
    QuoteStatus.SENT: frozenset(
        {
            QuoteStatus.VIEWED,
            QuoteStatus.ACCEPTED,
            QuoteStatus.CHANGES_REQUESTED,
        }
    ),
    QuoteStatus.VIEWED: frozenset(
        {QuoteStatus.ACCEPTED, QuoteStatus.CHANGES_REQUESTED}
    ),
    QuoteStatus.ACCEPTED: frozenset(),
    QuoteStatus.CHANGES_REQUESTED: frozenset(),
}
_QUOTE_NUMBER_PATTERN = re.compile(r"^SQ-[0-9]{4}-[0-9]{4}$")
_SEQUENCE_ALLOCATION_GUARD = object()
_SEQUENCE_ALLOCATION_CONTEXT = ContextVar(
    "scopequote_sequence_allocation",
    default=None,
)
_PUBLICATION_MUTATION_GUARD = object()
_PUBLICATION_MUTATION_CONTEXT = ContextVar(
    "scopequote_publication_mutation",
    default=None,
)
_DRAFT_LINE_ITEM_MUTATION_CONTEXT = ContextVar(
    "scopequote_draft_line_item_mutation",
    default=None,
)
_DRAFT_LINE_ITEM_MUTATION_GUARD = object()


@contextmanager
def _sequence_allocation_context(guard):
    if guard is not _SEQUENCE_ALLOCATION_GUARD:
        raise PermissionError("The sequence allocation guard is private.")
    token = _SEQUENCE_ALLOCATION_CONTEXT.set(guard)
    try:
        yield
    finally:
        _SEQUENCE_ALLOCATION_CONTEXT.reset(token)


@contextmanager
def _publication_mutation_context(guard):
    if guard is not _PUBLICATION_MUTATION_GUARD:
        raise PermissionError("The publication mutation guard is private.")
    token = _PUBLICATION_MUTATION_CONTEXT.set(guard)
    try:
        yield
    finally:
        _PUBLICATION_MUTATION_CONTEXT.reset(token)


@contextmanager
def draft_line_item_mutation(guard):
    if guard is not _DRAFT_LINE_ITEM_MUTATION_GUARD:
        raise PermissionError("The draft line-item guard is private.")
    token = _DRAFT_LINE_ITEM_MUTATION_CONTEXT.set(guard)
    try:
        yield
    finally:
        _DRAFT_LINE_ITEM_MUTATION_CONTEXT.reset(token)


def _is_allocated_quote_number(value: Any) -> bool:
    return isinstance(value, str) and _QUOTE_NUMBER_PATTERN.fullmatch(value) is not None


def _status_transition_is_legal(previous: str, target: str) -> bool:
    if previous == target:
        return True
    return target in LEGAL_STATUS_TRANSITIONS.get(previous, frozenset())


def _assert_publish_ready(quote: Quote) -> None:
    if not _is_allocated_quote_number(quote.quote_number):
        raise ValidationError(
            {
                "quote_number": [
                    "An allocated quote number is required before publishing."
                ]
            }
        )
    quote.validate_for_publish()


class QuoteQuerySet(models.QuerySet):
    def _locked_rows(self):
        primary_keys = list(self.values_list("pk", flat=True))
        if not primary_keys:
            return []
        return list(self.model.objects.select_for_update().filter(pk__in=primary_keys))

    def _validate_operational_rows(self, rows, updates):
        target_status = updates.get("status")
        for row in rows:
            if row.status == QuoteStatus.DRAFT:
                raise ValidationError(
                    "Published quote bulk updates cannot change draft quotes."
                )
            if target_status is not None and not _status_transition_is_legal(
                row.status, target_status
            ):
                raise ValidationError(
                    "The requested quote status transition is illegal."
                )
            _assert_publish_ready(row)

    def update(self, **kwargs):
        if not kwargs:
            return super().update(**kwargs)
        if set(kwargs) - OPERATIONAL_QUOTE_FIELDS:
            raise ValidationError(
                "Only quote status and lifecycle timestamps may be bulk updated."
            )
        with transaction.atomic():
            rows = self._locked_rows()
            self._validate_operational_rows(rows, kwargs)
            return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        fields = set(fields)
        if fields - OPERATIONAL_QUOTE_FIELDS:
            raise ValidationError(
                "Only quote status and lifecycle timestamps may be bulk updated."
            )
        objs = list(objs)
        if not objs:
            return 0
        with transaction.atomic():
            primary_keys = [obj.pk for obj in objs]
            rows = {
                row.pk: row
                for row in self.model.objects.select_for_update().filter(
                    pk__in=primary_keys
                )
            }
            if len(rows) != len(primary_keys):
                raise ValidationError(
                    "Every quote must already exist before bulk update."
                )
            for obj in objs:
                row = rows[obj.pk]
                if row.status == QuoteStatus.DRAFT:
                    raise ValidationError(
                        "Published quote bulk updates cannot change draft quotes."
                    )
                if "status" in fields and not _status_transition_is_legal(
                    row.status, obj.status
                ):
                    raise ValidationError(
                        "The requested quote status transition is illegal."
                    )
                _assert_publish_ready(row)
            return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if ignore_conflicts or update_conflicts:
            raise ValidationError("Conflict-aware quote bulk creation is not allowed.")
        objs = list(objs)
        for obj in objs:
            if obj.status != QuoteStatus.DRAFT:
                raise ValidationError("New quotes must be created as drafts.")
            try:
                obj.deposit_amount = obj.derive_deposit_amount()
            except ValueError as exc:
                raise ValidationError({"deposit_percentage": [str(exc)]}) from exc
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        quote_ids = list(self.values_list("pk", flat=True))
        if self.filter(
            status__in=PUBLISHED_STATUSES
        ).exists() or _quote_has_protected_children(quote_ids):
            raise ValidationError(
                "Quotes with published status or immutable children cannot be deleted."
            )
        return super().delete()


class QuoteLineItemQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Line items cannot be changed through a queryset update.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Line items cannot be changed through bulk_update.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if ignore_conflicts or update_conflicts:
            raise ValidationError(
                "Conflict-aware line-item bulk creation is not allowed."
            )
        objs = list(objs)
        quote_ids = {obj.quote_id for obj in objs}
        published_ids = set(
            Quote.objects.filter(
                pk__in=quote_ids,
                status__in=PUBLISHED_STATUSES,
            ).values_list("pk", flat=True)
        )
        if published_ids.intersection(quote_ids):
            raise ValidationError("Line items cannot be added to a published quote.")
        for obj in objs:
            obj.amount = _money(obj.amount)
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        quote_ids = set(self.values_list("quote_id", flat=True))
        if Quote.objects.filter(
            pk__in=quote_ids,
            status__in=PUBLISHED_STATUSES,
        ).exists():
            raise ValidationError("Published quote line items cannot be deleted.")
        return super().delete()


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Immutable records cannot be updated.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Immutable records cannot be bulk updated.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError("Immutable records must be created individually.")

    def delete(self):
        raise ValidationError("Immutable records cannot be deleted.")


class ShareLinkQuerySet(models.QuerySet):
    _version_binding_fields = frozenset({"quote_version", "quote_version_id"})

    def update(self, **kwargs):
        if self._version_binding_fields.intersection(kwargs):
            raise ValidationError(
                "A share link cannot be reassigned to another version."
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if self._version_binding_fields.intersection(fields):
            raise ValidationError(
                "A share link cannot be reassigned through bulk_update."
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if ignore_conflicts or update_conflicts:
            raise ValidationError(
                "Conflict-aware share link creation cannot change version bindings."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class QuoteNumberSequenceQuerySet(models.QuerySet):
    def _require_allocation_context(self):
        if _SEQUENCE_ALLOCATION_CONTEXT.get() is not _SEQUENCE_ALLOCATION_GUARD:
            raise ValidationError(
                "Quote sequence counters are managed only by the allocation service."
            )

    def update(self, **kwargs):
        self._require_allocation_context()
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        self._require_allocation_context()
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if ignore_conflicts or update_conflicts:
            raise ValidationError(
                "Conflict-aware sequence bulk creation is not allowed."
            )
        self._require_allocation_context()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        raise ValidationError("Quote sequence records cannot be deleted.")


QuoteManager = models.Manager.from_queryset(QuoteQuerySet)
QuoteLineItemManager = models.Manager.from_queryset(QuoteLineItemQuerySet)
ImmutableManager = models.Manager.from_queryset(ImmutableQuerySet)
ShareLinkManager = models.Manager.from_queryset(ShareLinkQuerySet)
QuoteNumberSequenceManager = models.Manager.from_queryset(QuoteNumberSequenceQuerySet)


def _quote_has_protected_children(quote_ids) -> bool:
    quote_ids = list(quote_ids)
    if not quote_ids:
        return False
    return (
        QuoteVersion.objects.filter(quote_id__in=quote_ids).exists()
        or QuoteEvent.objects.filter(quote_id__in=quote_ids).exists()
        or ShareLink.objects.filter(quote_version__quote_id__in=quote_ids).exists()
    )


class Quote(models.Model):
    Status = QuoteStatus
    Currency = Currency
    Complexity = Complexity
    Urgency = Urgency
    Experience = Experience
    Category = ScopeCategory
    objects = QuoteManager()

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    owner = models.ForeignKey(
        User,
        editable=False,
        on_delete=models.CASCADE,
        related_name="quotes",
    )
    guest_conversion_nonce = models.CharField(
        blank=True,
        max_length=128,
        null=True,
        unique=True,
    )
    quote_number = models.CharField(
        blank=True,
        editable=False,
        max_length=32,
        null=True,
        unique=True,
    )
    status = models.CharField(
        choices=QuoteStatus.choices,
        default=QuoteStatus.DRAFT,
        max_length=24,
    )
    project_title = models.CharField(blank=True, max_length=160)
    project_description = models.TextField(blank=True)
    category = models.CharField(
        blank=True,
        choices=ScopeCategory.choices,
        max_length=20,
    )
    delivery_days = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(60)],
    )
    complexity = models.CharField(
        choices=Complexity.choices,
        default=Complexity.STANDARD,
        max_length=24,
    )
    revision_rounds = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
    )
    urgency = models.CharField(
        choices=Urgency.choices,
        default=Urgency.STANDARD,
        max_length=16,
    )
    experience = models.CharField(
        choices=Experience.choices,
        default=Experience.INTERMEDIATE,
        max_length=24,
    )
    currency = models.CharField(
        choices=Currency.choices,
        default=Currency.USD,
        max_length=3,
    )
    pricing_config_version = models.CharField(
        default=PRICING_CONFIG_VERSION,
        max_length=32,
    )
    effective_hours = models.DecimalField(
        default=ZERO,
        max_digits=12,
        decimal_places=2,
    )
    minimum = models.DecimalField(default=ZERO, max_digits=14, decimal_places=2)
    recommended = models.DecimalField(default=ZERO, max_digits=14, decimal_places=2)
    maximum = models.DecimalField(default=ZERO, max_digits=14, decimal_places=2)
    chosen_total = models.DecimalField(default=ZERO, max_digits=14, decimal_places=2)
    hourly_equivalent = models.DecimalField(
        default=ZERO,
        max_digits=14,
        decimal_places=2,
    )
    client_name = models.CharField(blank=True, max_length=160)
    client_company = models.CharField(blank=True, max_length=255)
    client_email = models.EmailField(blank=True)
    sender_name = models.CharField(blank=True, max_length=160)
    sender_business_name = models.CharField(blank=True, max_length=255)
    sender_email = models.EmailField(blank=True)
    sender_website = models.URLField(
        blank=True,
        max_length=2048,
        validators=[validate_website_url],
    )
    sender_monogram = models.CharField(blank=True, max_length=8)
    accent_color = models.CharField(
        blank=True,
        default="#E85D3F",
        max_length=7,
        validators=[accent_color_validator],
    )
    logo_object_key = models.CharField(blank=True, max_length=255)
    logo_content_type = models.CharField(blank=True, max_length=64)
    logo_byte_size = models.PositiveIntegerField(blank=True, null=True)
    issue_date = models.DateField(blank=True, null=True)
    valid_until = models.DateField(blank=True, null=True)
    deposit_percentage = models.PositiveSmallIntegerField(
        default=50,
        validators=[
            MinValueValidator(0),
            MaxValueValidator(100),
            validate_five_point_increment,
        ],
    )
    deposit_amount = models.DecimalField(
        default=ZERO,
        editable=False,
        max_digits=14,
        decimal_places=2,
    )
    timeline = models.TextField(blank=True)
    revision_terms = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    exclusions = models.TextField(blank=True)
    taxes = models.TextField(blank=True)
    cancellation_terms = models.TextField(blank=True)
    current_published_version = models.ForeignKey(
        "quotes.QuoteVersion",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="current_for_quotes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    accepted_at = models.DateTimeField(blank=True, null=True)
    last_viewed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("owner", "status", "created_at"),
                name="q_owner_status_created_idx",
            ),
            models.Index(
                fields=("owner", "status", "updated_at"),
                name="q_owner_status_updated_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(deposit_percentage__gte=0) & Q(deposit_percentage__lte=100),
                name="quotes_deposit_percentage_range",
            ),
        ]

    def __init__(self, *args, **kwargs):
        _quote_aliases(kwargs)
        super().__init__(*args, **kwargs)

    @property
    def title(self) -> str:
        return self.project_title

    @title.setter
    def title(self, value: str) -> None:
        self.project_title = value

    @property
    def description(self) -> str:
        return self.project_description

    @description.setter
    def description(self, value: str) -> None:
        self.project_description = value

    @property
    def duration_days(self) -> int:
        return self.delivery_days

    @duration_days.setter
    def duration_days(self, value: int) -> None:
        self.delivery_days = value

    @property
    def project_category(self) -> str:
        return self.category

    @project_category.setter
    def project_category(self, value: str) -> None:
        self.category = value

    @property
    def revisions(self) -> int:
        return self.revision_rounds

    @revisions.setter
    def revisions(self, value: int) -> None:
        self.revision_rounds = value

    @property
    def pricing_version(self) -> str:
        return self.pricing_config_version

    @pricing_version.setter
    def pricing_version(self, value: str) -> None:
        self.pricing_config_version = value

    @property
    def recommended_min(self) -> Decimal:
        return self.minimum

    @recommended_min.setter
    def recommended_min(self, value: Decimal) -> None:
        self.minimum = value

    @property
    def recommended_minimum(self) -> Decimal:
        return self.minimum

    @recommended_minimum.setter
    def recommended_minimum(self, value: Decimal) -> None:
        self.minimum = value

    @property
    def recommended_max(self) -> Decimal:
        return self.maximum

    @recommended_max.setter
    def recommended_max(self, value: Decimal) -> None:
        self.maximum = value

    @property
    def recommended_maximum(self) -> Decimal:
        return self.maximum

    @recommended_maximum.setter
    def recommended_maximum(self, value: Decimal) -> None:
        self.maximum = value

    @property
    def brand_color(self) -> str:
        return self.accent_color

    @brand_color.setter
    def brand_color(self, value: str) -> None:
        self.accent_color = value

    @property
    def version_logo_object_key(self) -> str:
        return self.logo_object_key

    @version_logo_object_key.setter
    def version_logo_object_key(self, value: str) -> None:
        self.logo_object_key = value

    @property
    def sender_business(self) -> str:
        return self.sender_business_name

    @sender_business.setter
    def sender_business(self, value: str) -> None:
        self.sender_business_name = value

    @property
    def sender_company(self) -> str:
        return self.sender_business_name

    @sender_company.setter
    def sender_company(self, value: str) -> None:
        self.sender_business_name = value

    @property
    def company_name(self) -> str:
        return self.client_company

    @company_name.setter
    def company_name(self, value: str) -> None:
        self.client_company = value

    @property
    def tax_statement(self) -> str:
        return self.taxes

    @tax_statement.setter
    def tax_statement(self, value: str) -> None:
        self.taxes = value

    @property
    def cancellation_text(self) -> str:
        return self.cancellation_terms

    @cancellation_text.setter
    def cancellation_text(self, value: str) -> None:
        self.cancellation_terms = value

    @property
    def estimated_hours(self) -> Decimal:
        return self.effective_hours

    @estimated_hours.setter
    def estimated_hours(self, value: Decimal) -> None:
        self.effective_hours = value

    @property
    def quote_status(self) -> str:
        return self.status

    @quote_status.setter
    def quote_status(self, value: str) -> None:
        self.status = value

    @property
    def is_draft(self) -> bool:
        return self.status == QuoteStatus.DRAFT

    @property
    def can_edit(self) -> bool:
        return self.is_draft

    @property
    def is_published(self) -> bool:
        return not self.is_draft

    @property
    def published_version(self):
        return self.current_published_version

    @published_version.setter
    def published_version(self, value) -> None:
        self.current_published_version = value

    @property
    def realized_hourly_equivalent(self) -> Decimal | None:
        if self.effective_hours is None or self.effective_hours <= ZERO:
            return None
        return (Decimal(self.chosen_total) / Decimal(self.effective_hours)).quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )

    def derive_deposit_amount(self) -> Decimal:
        from quotes.services.quotes import calculate_deposit_amount

        return calculate_deposit_amount(self.chosen_total, self.deposit_percentage)

    calculate_deposit_amount = derive_deposit_amount
    calculated_deposit_amount = derive_deposit_amount

    @property
    def line_item_total(self) -> Decimal:
        from quotes.services.quotes import calculate_line_item_total

        if not self.pk or not self.line_items.exists():
            return _money(self.chosen_total)
        return calculate_line_item_total(self.line_items.all())

    @property
    def calculated_realized_hourly_equivalent(self) -> Decimal | None:
        return self.realized_hourly_equivalent

    def calculate_realized_hourly_equivalent(self) -> Decimal | None:
        return self.realized_hourly_equivalent

    def recalculate_line_item_total(self) -> Decimal:
        from quotes.services.quotes import calculate_line_item_total

        total = (
            calculate_line_item_total(self.line_items.all())
            if self.pk and self.line_items.exists()
            else ZERO
        )
        self.chosen_total = total
        self.deposit_amount = self.derive_deposit_amount()
        return total

    def requires_intentional_adjustment(self) -> bool:
        from quotes.services.quotes import requires_intentional_adjustment

        return requires_intentional_adjustment(
            chosen_total=self.chosen_total,
            recommended_min=self.minimum,
            recommended_max=self.maximum,
        )

    def clean(self):
        super().clean()
        if self.issue_date and self.valid_until:
            from quotes.services.quotes import quote_validity_days

            try:
                quote_validity_days(
                    issue_date=self.issue_date,
                    valid_until=self.valid_until,
                )
            except ValueError as error:
                raise ValidationError({"valid_until": [str(error)]}) from error
        if not self._state.adding or self.status != QuoteStatus.DRAFT:
            self._validate_persistence_boundary()

    def validate_for_publish(self) -> None:
        from quotes.services.quotes import calculate_line_item_total

        errors: dict[str, list[str]] = {}
        required_fields = {
            "client_name": self.client_name,
            "client_email": self.client_email,
            "project_title": self.project_title,
            "project_description": self.project_description,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "terms": self.terms,
        }
        for field_name, value in required_fields.items():
            if not isinstance(value, str) or not value.strip():
                errors[field_name] = ["This field is required before publishing."]
        for field_name, value in {
            "issue_date": self.issue_date,
            "valid_until": self.valid_until,
        }.items():
            if value is None:
                errors[field_name] = ["This field is required before publishing."]
        if self.issue_date and self.valid_until:
            from quotes.services.quotes import quote_validity_days

            try:
                quote_validity_days(
                    issue_date=self.issue_date,
                    valid_until=self.valid_until,
                )
            except ValueError as error:
                errors["valid_until"] = [str(error)]
        try:
            self.derive_deposit_amount()
        except ValueError as exc:
            errors["deposit_percentage"] = [str(exc)]
        if self.pk:
            line_items = list(self.line_items.all())
        else:
            line_items = []
        if not line_items:
            errors["line_items"] = ["At least one line item is required."]
        elif len(line_items) > 50:
            errors["line_items"] = ["A quote cannot contain more than 50 line items."]
        else:
            try:
                total = calculate_line_item_total(line_items)
            except ValueError as exc:
                errors["line_items"] = [str(exc)]
            else:
                self.chosen_total = total
                self.deposit_amount = self.derive_deposit_amount()
        if errors:
            raise ValidationError(errors)
        return True

    validate_publish_fields = validate_for_publish

    def _persisted_status(self) -> str | None:
        if not self.pk:
            return None
        return (
            type(self)
            .objects.filter(pk=self.pk)
            .values_list("status", flat=True)
            .first()
        )

    def _changed_fields(self) -> set[str]:
        if not self.pk:
            return set()
        field_names = [
            field.attname
            for field in self._meta.concrete_fields
            if not field.primary_key
        ]
        persisted = type(self).objects.filter(pk=self.pk).values(*field_names).first()
        if persisted is None:
            return set()
        changed = set()
        for field_name in field_names:
            if persisted[field_name] != getattr(self, field_name):
                changed.add(field_name)
        return changed

    def _persisted_owner_id(self):
        if not self.pk:
            return None
        return (
            type(self)
            .objects.filter(pk=self.pk)
            .values_list("owner_id", flat=True)
            .first()
        )

    def _ensure_owner_immutable(self) -> None:
        if self.pk and not self._state.adding:
            if self.owner_id != self._persisted_owner_id():
                raise ValidationError("A quote owner cannot be changed after creation.")

    def _ensure_status_transition(self) -> None:
        if self._state.adding:
            if self.status != QuoteStatus.DRAFT:
                raise ValidationError("New quotes must be created as drafts.")
            return
        persisted_status = self._persisted_status()
        if persisted_status is None:
            raise ValidationError("The quote no longer exists.")
        if not _status_transition_is_legal(persisted_status, self.status):
            raise ValidationError("The requested quote status transition is illegal.")

    def _validate_persistence_boundary(self) -> None:
        self._ensure_owner_immutable()
        self._ensure_status_transition()
        if self.status != QuoteStatus.DRAFT:
            _assert_publish_ready(self)
            self._ensure_mutable()

    def _ensure_mutable(self) -> None:
        persisted_status = self._persisted_status()
        if persisted_status is None or persisted_status == QuoteStatus.DRAFT:
            return
        allowed_operational_fields = {
            "status",
            "sent_at",
            "accepted_at",
            "last_viewed_at",
            "updated_at",
        }
        changed = self._changed_fields()
        publication_fields = set()
        if _PUBLICATION_MUTATION_CONTEXT.get() is _PUBLICATION_MUTATION_GUARD:
            publication_fields.update(
                {
                    "quote_number",
                    "chosen_total",
                    "deposit_amount",
                    "logo_object_key",
                    "logo_content_type",
                    "logo_byte_size",
                    "current_published_version_id",
                }
            )
        if changed - allowed_operational_fields - publication_fields:
            raise ValidationError(
                "Published quotes are immutable; duplicate this quote to edit it."
            )
        if "current_published_version_id" in changed:
            version = self.current_published_version
            if version is None or version.quote_id != self.pk:
                raise ValidationError("The published version must belong to the quote.")
        if self.status not in {
            QuoteStatus.SENT,
            QuoteStatus.VIEWED,
            QuoteStatus.ACCEPTED,
            QuoteStatus.CHANGES_REQUESTED,
        }:
            raise ValidationError("Published quotes cannot return to draft.")

    def save(self, *args, **kwargs):
        if self._state.adding:
            self._ensure_status_transition()
        else:
            self._validate_persistence_boundary()
            if (
                self.status == QuoteStatus.DRAFT
                and _DRAFT_LINE_ITEM_MUTATION_CONTEXT.get()
                is not _DRAFT_LINE_ITEM_MUTATION_GUARD
                and not self.line_items.exists()
            ):
                raise ValidationError("A saved draft requires at least one line item.")
        self.deposit_amount = self.derive_deposit_amount()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"deposit_amount"}
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            if self._persisted_status() != QuoteStatus.DRAFT:
                raise ValidationError("Published quotes cannot be deleted in place.")
            if _quote_has_protected_children([self.pk]):
                raise ValidationError(
                    "Quotes with immutable children require controlled cleanup."
                )
        return super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return self.quote_number or f"Draft {self.pk}"


class QuoteLineItem(models.Model):
    objects = QuoteLineItemManager()

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    quote = models.ForeignKey(
        Quote,
        on_delete=models.CASCADE,
        related_name="line_items",
    )
    label = models.CharField(max_length=160)
    description = models.CharField(blank=True, max_length=500)
    amount = models.DecimalField(default=ZERO, max_digits=14, decimal_places=2)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("sort_order", "created_at")
        indexes = [
            models.Index(
                fields=("quote", "sort_order"),
                name="q_line_quote_order_idx",
            ),
        ]

    def __init__(self, *args, **kwargs):
        _line_item_aliases(kwargs)
        super().__init__(*args, **kwargs)

    @property
    def position(self) -> int:
        return self.sort_order

    @position.setter
    def position(self, value: int) -> None:
        self.sort_order = value

    @property
    def item_description(self) -> str:
        return self.description

    @item_description.setter
    def item_description(self, value: str) -> None:
        self.description = value

    def clean(self):
        super().clean()
        if self.quote_id:
            self._ensure_quote_is_draft()

    def _ensure_quote_is_draft(self) -> None:
        if not self.quote_id:
            return
        status = (
            Quote.objects.filter(pk=self.quote_id)
            .values_list("status", flat=True)
            .first()
        )
        if status is not None and status != QuoteStatus.DRAFT:
            raise ValidationError(
                "Line items are immutable after a quote is published."
            )

    def save(self, *args, **kwargs):
        self._ensure_quote_is_draft()
        self.amount = _money(self.amount)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"amount"}
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._ensure_quote_is_draft()
        return super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return self.label


class QuoteNumberSequence(models.Model):
    objects = QuoteNumberSequenceManager()

    year = models.PositiveIntegerField(primary_key=True, editable=False)
    last_allocated_number = models.PositiveIntegerField(
        default=0,
        editable=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("year",)

    def __init__(self, *args, **kwargs):
        _sequence_aliases(kwargs)
        super().__init__(*args, **kwargs)

    @property
    def last_number(self) -> int:
        return self.last_allocated_number

    @last_number.setter
    def last_number(self, value: int) -> None:
        self.last_allocated_number = value

    @property
    def last_sequence(self) -> int:
        return self.last_allocated_number

    @last_sequence.setter
    def last_sequence(self, value: int) -> None:
        self.last_allocated_number = value

    @property
    def last_sequence_number(self) -> int:
        return self.last_allocated_number

    @last_sequence_number.setter
    def last_sequence_number(self, value: int) -> None:
        self.last_allocated_number = value

    @property
    def sequence_number(self) -> int:
        return self.last_allocated_number

    @sequence_number.setter
    def sequence_number(self, value: int) -> None:
        self.last_allocated_number = value

    def save(self, *args, **kwargs):
        if _SEQUENCE_ALLOCATION_CONTEXT.get() is not _SEQUENCE_ALLOCATION_GUARD:
            if not self._state.adding:
                raise ValidationError(
                    "Quote sequence counters are managed only by the "
                    "allocation service."
                )
            if self.last_allocated_number != 0:
                raise ValidationError("A new quote sequence must start at zero.")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.year}: {self.last_allocated_number}"


_MISSING = object()
_SNAPSHOT_SECTION_FIELDS = {
    "project": (
        ("title", "project_title"),
        ("description", "project_description"),
        ("category", "category"),
        ("project_category", "project_category"),
    ),
    "client": (
        ("name", "client_name"),
        ("company", "client_company"),
        ("email", "client_email"),
    ),
    "sender": (
        ("name", "sender_name"),
        ("business_name", "sender_business_name"),
        ("business", "sender_business"),
        ("email", "sender_email"),
        ("website", "sender_website"),
        ("monogram", "sender_monogram"),
    ),
    "scope": (
        ("delivery_days", "delivery_days"),
        ("complexity", "complexity"),
        ("revision_rounds", "revision_rounds"),
        ("urgency", "urgency"),
        ("experience", "experience"),
        ("currency", "currency"),
        ("effective_hours", "effective_hours"),
        ("estimated_hours", "estimated_hours"),
        ("minimum", "minimum"),
        ("recommended_minimum", "recommended_minimum"),
        ("recommended", "recommended"),
        ("maximum", "maximum"),
        ("recommended_maximum", "recommended_maximum"),
        ("hourly_equivalent", "hourly_equivalent"),
        ("pricing_config_version", "pricing_config_version"),
        ("pricing_version", "pricing_version"),
    ),
    "dates": (
        ("issue_date", "issue_date"),
        ("valid_until", "valid_until"),
    ),
    "terms_details": (
        ("timeline", "timeline"),
        ("revision_terms", "revision_terms"),
        ("notes", "notes"),
        ("exclusions", "exclusions"),
        ("taxes", "taxes"),
        ("cancellation_terms", "cancellation_terms"),
        ("terms", "terms"),
    ),
    "brand": (
        ("accent_color", "accent_color"),
        ("brand_color", "brand_color"),
        ("logo_object_key", "logo_object_key"),
        ("version_logo_object_key", "version_logo_object_key"),
    ),
    "pricing": (
        ("config_version", "pricing_config_version"),
        ("version", "pricing_version"),
        ("minimum", "minimum"),
        ("recommended", "recommended"),
        ("maximum", "maximum"),
        ("hourly_equivalent", "hourly_equivalent"),
    ),
}
_SNAPSHOT_FLAT_ALIASES = {
    "project_category": "category",
    "pricing_version": "pricing_config_version",
    "estimated_hours": "effective_hours",
    "recommended_minimum": "minimum",
    "recommended_maximum": "maximum",
    "sender_business": "sender_business_name",
    "brand_color": "accent_color",
    "version_logo_object_key": "logo_object_key",
}
_SNAPSHOT_DECIMAL_FIELDS = (
    ("effective_hours", "effective_hours"),
    ("minimum", "minimum"),
    ("recommended", "recommended"),
    ("maximum", "maximum"),
    ("hourly_equivalent", "hourly_equivalent"),
    ("chosen_total", "chosen_total"),
    ("realized_hourly_equivalent", "realized_hourly_equivalent"),
    ("deposit_amount", "deposit_amount"),
)


def _snapshot_values_equal(left, right) -> bool:
    return type(left) is type(right) and left == right


def _validate_snapshot_shape(snapshot, errors: dict[str, list[str]]) -> None:
    for alias, canonical in _SNAPSHOT_FLAT_ALIASES.items():
        if alias not in snapshot:
            errors[alias] = ["This canonical snapshot alias is required."]
        elif canonical not in snapshot:
            errors[canonical] = ["This canonical snapshot field is required."]
        elif not _snapshot_values_equal(snapshot[alias], snapshot[canonical]):
            errors[alias] = ["Snapshot aliases must agree with canonical fields."]

    for section_name, fields in _SNAPSHOT_SECTION_FIELDS.items():
        section = snapshot.get(section_name)
        if not isinstance(section, dict):
            errors[section_name] = ["This grouped snapshot section must be an object."]
            continue
        for section_key, flat_key in fields:
            field_name = f"{section_name}.{section_key}"
            if flat_key not in snapshot:
                errors[flat_key] = ["This canonical snapshot field is required."]
            if section_key not in section:
                errors[field_name] = ["This grouped snapshot field is required."]
            elif flat_key in snapshot and not _snapshot_values_equal(
                section[section_key], snapshot[flat_key]
            ):
                errors[field_name] = [
                    "Grouped snapshot fields must match canonical flat fields."
                ]


def _snapshot_value(snapshot, flat_key, section=None, section_key=None):
    if flat_key in snapshot:
        value = snapshot[flat_key]
        if not (section and isinstance(value, dict)):
            return value
    if section and section_key:
        section_value = snapshot.get(section)
        if isinstance(section_value, dict) and section_key in section_value:
            return section_value[section_key]
    return _MISSING


def _contains_model_instance(value) -> bool:
    if isinstance(value, models.Model):
        return True
    if isinstance(value, dict):
        return any(
            _contains_model_instance(key) or _contains_model_instance(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_model_instance(item) for item in value)
    return False


def _snapshot_text(snapshot, flat_key, section=None, section_key=None) -> str | None:
    value = _snapshot_value(snapshot, flat_key, section, section_key)
    if value is _MISSING or value is None:
        return None
    return str(value).strip()


def _exact_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Monetary values must be finite decimals.")
    try:
        exact = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Monetary values must be finite decimals.") from exc
    if not exact.is_finite() or exact.as_tuple().exponent < -2:
        raise ValueError("Monetary values must use at most two decimal places.")
    return exact


def _validate_quote_snapshot(
    snapshot,
    *,
    quote,
    frozen_total,
    currency,
    logo_object_key,
) -> None:
    if not isinstance(snapshot, dict):
        raise ValidationError({"snapshot": ["The snapshot must be a JSON object."]})
    if _contains_model_instance(snapshot):
        raise ValidationError(
            {"snapshot": ["Model instances cannot be stored in snapshots."]}
        )
    if snapshot.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValidationError({"snapshot": ["The snapshot schema version must be 1."]})

    errors: dict[str, list[str]] = {}
    _validate_snapshot_shape(snapshot, errors)
    quote_number_value = _snapshot_value(snapshot, "quote_number")
    required_text = {
        "project_title": _snapshot_text(snapshot, "project_title"),
        "project_description": _snapshot_text(snapshot, "project_description"),
        "client_name": _snapshot_text(snapshot, "client_name"),
        "client_email": _snapshot_text(snapshot, "client_email"),
        "sender_name": _snapshot_text(snapshot, "sender_name"),
        "sender_email": _snapshot_text(snapshot, "sender_email"),
        "terms": _snapshot_text(snapshot, "terms"),
        "issue_date": _snapshot_text(snapshot, "issue_date"),
        "valid_until": _snapshot_text(snapshot, "valid_until"),
        "pricing_config_version": _snapshot_text(snapshot, "pricing_config_version"),
    }
    for field_name, value in required_text.items():
        if not value:
            errors[field_name] = ["This commercial field is required in the snapshot."]
    if not _is_allocated_quote_number(quote_number_value):
        errors["quote_number"] = [
            "The snapshot must contain an allocated quote number."
        ]

    line_items = _snapshot_value(snapshot, "line_items")
    if not isinstance(line_items, list) or not line_items:
        errors["line_items"] = ["At least one snapshot line item is required."]
        line_items = []
    elif len(line_items) > 50:
        errors["line_items"] = ["A snapshot cannot contain more than 50 line items."]
    line_total = ZERO
    has_positive_item = False
    for index, item in enumerate(line_items):
        if not isinstance(item, dict):
            errors[f"line_items[{index}]"] = ["Each line item must be an object."]
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            errors[f"line_items[{index}].label"] = ["A line-item label is required."]
        if not isinstance(item.get("description"), str):
            errors[f"line_items[{index}].description"] = [
                "A line-item description must be text."
            ]
        sort_order = item.get("sort_order")
        if (
            isinstance(sort_order, bool)
            or not isinstance(sort_order, int)
            or sort_order < 0
        ):
            errors[f"line_items[{index}].sort_order"] = [
                "A line-item sort order must be a non-negative integer."
            ]
        try:
            amount = _exact_decimal(item.get("amount"))
        except ValueError:
            errors[f"line_items[{index}].amount"] = [
                "A line-item amount must be a valid decimal."
            ]
            continue
        line_total += amount
        has_positive_item = has_positive_item or amount > ZERO
    if line_items and not has_positive_item:
        errors["line_items"] = ["At least one snapshot line item must be positive."]
    if line_total <= ZERO:
        errors["line_items"] = ["The snapshot line-item total must be positive."]

    snapshot_status = _snapshot_value(snapshot, "status")
    if snapshot_status != quote.status:
        errors["status"] = ["The snapshot status must match the quote."]

    deposit_percentage = _snapshot_value(snapshot, "deposit_percentage")
    if (
        isinstance(deposit_percentage, bool)
        or not isinstance(deposit_percentage, int)
        or deposit_percentage != quote.deposit_percentage
    ):
        errors["deposit_percentage"] = [
            "The snapshot deposit percentage must match the quote."
        ]

    for snapshot_field, quote_field in _SNAPSHOT_DECIMAL_FIELDS:
        raw_value = _snapshot_value(snapshot, snapshot_field)
        if raw_value is _MISSING:
            continue
        expected_value = getattr(quote, quote_field)
        if raw_value is None:
            if expected_value is not None:
                errors[snapshot_field] = [
                    "The snapshot decimal value must match the quote."
                ]
            continue
        try:
            exact_value = _exact_decimal(raw_value)
            expected_exact = (
                _exact_decimal(expected_value) if expected_value is not None else None
            )
        except ValueError:
            errors[snapshot_field] = [
                "The snapshot decimal value must be a valid decimal."
            ]
            continue
        if expected_exact is not None and exact_value != expected_exact:
            errors[snapshot_field] = [
                "The snapshot decimal value must match the quote."
            ]

    snapshot_total_value = _snapshot_value(snapshot, "chosen_total")
    try:
        snapshot_total = _exact_decimal(snapshot_total_value)
    except (TypeError, ValueError):
        errors["chosen_total"] = ["The snapshot chosen total must be a valid decimal."]
        snapshot_total = None
    deposit_amount_value = _snapshot_value(snapshot, "deposit_amount")
    try:
        deposit_amount = _exact_decimal(deposit_amount_value)
    except (TypeError, ValueError):
        errors["deposit_amount"] = ["The snapshot deposit amount must be a decimal."]
        deposit_amount = None
    if deposit_amount is not None and deposit_amount != quote.deposit_amount:
        errors["deposit_amount"] = ["The snapshot deposit amount must match the quote."]
    try:
        frozen_value = _exact_decimal(frozen_total)
    except (TypeError, ValueError):
        errors["frozen_total"] = ["The frozen total must be a valid decimal."]
        frozen_value = None

    snapshot_currency = _snapshot_text(snapshot, "currency")
    if snapshot_currency != currency:
        errors["currency"] = [
            "The snapshot currency must match the frozen version currency."
        ]
    if currency != quote.currency:
        errors["currency"] = ["The frozen version currency must match the quote."]
    snapshot_logo = _snapshot_value(snapshot, "logo_object_key")
    if not _snapshot_values_equal(snapshot_logo, logo_object_key):
        errors["logo_object_key"] = [
            "The snapshot logo must match the frozen version logo."
        ]
    elif quote.logo_object_key != logo_object_key:
        errors["logo_object_key"] = [
            "The frozen version logo must match the quote logo."
        ]
    if frozen_value is not None and snapshot_total != frozen_value:
        errors["frozen_total"] = [
            "The frozen total must match the snapshot chosen total."
        ]
    if frozen_value is not None and line_total != frozen_value:
        errors["frozen_total"] = [
            "The frozen total must match the snapshot line-item total."
        ]
    if frozen_value is not None and quote.chosen_total is not None:
        try:
            if _exact_decimal(quote.chosen_total) != frozen_value:
                errors["frozen_total"] = [
                    "The frozen total must match the quote chosen total."
                ]
        except (TypeError, ValueError):
            pass
    if quote.quote_number != quote_number_value:
        errors["quote_number"] = ["The snapshot quote number must match the quote."]
    if errors:
        raise ValidationError(errors)


class QuoteVersion(models.Model):
    objects = ImmutableManager()
    Reason = QuoteVersionReason
    VersionReason = QuoteVersionReason

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    quote = models.ForeignKey(
        Quote,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField(default=1)
    snapshot_schema_version = models.PositiveSmallIntegerField(
        default=SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = models.JSONField()
    frozen_total = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(choices=Currency.choices, max_length=3)
    logo_object_key = models.CharField(blank=True, max_length=255)
    reason = models.CharField(
        choices=QuoteVersionReason.choices,
        default=QuoteVersionReason.PUBLISHED,
        max_length=16,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-version_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("quote", "version_number"),
                name="quotes_version_quote_number_unique",
            ),
            models.CheckConstraint(
                condition=Q(snapshot_schema_version=1),
                name="quotes_version_snapshot_schema_one",
            ),
            models.CheckConstraint(
                condition=Q(version_number__gte=1),
                name="quotes_version_number_positive",
            ),
        ]

    def __init__(self, *args, **kwargs):
        _version_aliases(kwargs)
        super().__init__(*args, **kwargs)

    @property
    def version(self) -> int:
        return self.version_number

    @version.setter
    def version(self, value: int) -> None:
        self.version_number = value

    @property
    def schema_version(self) -> int:
        return self.snapshot_schema_version

    @schema_version.setter
    def schema_version(self, value: int) -> None:
        self.snapshot_schema_version = value

    @property
    def snapshot_data(self):
        return self.snapshot

    @snapshot_data.setter
    def snapshot_data(self, value) -> None:
        self.snapshot = value

    @property
    def total(self) -> Decimal:
        return self.frozen_total

    @total.setter
    def total(self, value: Decimal) -> None:
        self.frozen_total = value

    @property
    def logo_key(self) -> str:
        return self.logo_object_key

    @logo_key.setter
    def logo_key(self, value: str) -> None:
        self.logo_object_key = value

    def validate_snapshot(self) -> None:
        _validate_quote_snapshot(
            self.snapshot,
            quote=self.quote,
            frozen_total=self.frozen_total,
            currency=self.currency,
            logo_object_key=self.logo_object_key,
        )

    def clean(self):
        super().clean()
        if not self._state.adding:
            raise ValidationError("Published quote versions are immutable.")
        self.validate_snapshot()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Published quote versions are immutable.")
        if self.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValidationError("Quote versions must use snapshot schema version 1.")
        self.validate_snapshot()
        self.snapshot = copy.deepcopy(self.snapshot)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Published quote versions are immutable.")

    def __str__(self) -> str:
        return f"{self.quote.quote_number or self.quote_id} v{self.version_number}"


class ShareLink(models.Model):
    objects = ShareLinkManager()

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    quote_version = models.ForeignKey(
        QuoteVersion,
        on_delete=models.PROTECT,
        related_name="share_links",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    encrypted_token = models.BinaryField()
    encryption_key_version = models.CharField(max_length=32)
    associated_data = models.CharField(blank=True, max_length=255)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True)
    first_viewed_at = models.DateTimeField(blank=True, null=True)
    created_by = models.ForeignKey(
        User,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_share_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("quote_version", "expires_at"),
                name="quotes_link_version_exp_idx",
            ),
        ]

    def __init__(self, *args, **kwargs):
        _share_link_aliases(kwargs)
        super().__init__(*args, **kwargs)

    def _validate_quote_version_binding(self, *, lock=False) -> None:
        if self.pk is None:
            return
        links = ShareLink.objects
        if lock:
            links = links.select_for_update()
        persisted_version_id = (
            links.filter(pk=self.pk).values_list("quote_version_id", flat=True).first()
        )
        if (
            persisted_version_id is not None
            and self.quote_version_id != persisted_version_id
        ):
            raise ValidationError(
                {
                    "quote_version": [
                        "A share link cannot be reassigned to another version."
                    ]
                }
            )

    def clean(self):
        super().clean()
        self._validate_quote_version_binding()

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self._validate_quote_version_binding(lock=True)
            return super().save(*args, **kwargs)

    @property
    def quote(self) -> Quote:
        return self.quote_version.quote

    @property
    def version(self) -> QuoteVersion:
        return self.quote_version

    @version.setter
    def version(self, value: QuoteVersion) -> None:
        self.quote_version = value

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        now = timezone.now()
        expires_at = self.expires_at
        if timezone.is_naive(expires_at):
            expires_at = timezone.make_aware(expires_at, timezone=UTC)
        return expires_at <= now

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired

    @property
    def state(self) -> str:
        if self.is_revoked:
            return "revoked"
        if self.is_expired:
            return "expired"
        return "active"

    @property
    def status(self) -> str:
        return self.state

    @property
    def is_valid(self) -> bool:
        return self.is_active

    def __str__(self) -> str:
        return f"Share link {self.pk}"


class QuoteEvent(models.Model):
    objects = ImmutableManager()
    Type = QuoteEventType
    EventType = QuoteEventType

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    quote = models.ForeignKey(
        Quote,
        on_delete=models.PROTECT,
        related_name="events",
    )
    share_link = models.ForeignKey(
        ShareLink,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )
    quote_version = models.ForeignKey(
        QuoteVersion,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="events",
    )
    quote_version_id_snapshot = models.UUIDField(blank=True, null=True, editable=False)
    event_type = models.CharField(
        choices=QuoteEventType.choices,
        max_length=24,
    )
    note = models.TextField(blank=True, max_length=2000)
    response_idempotency_key = models.UUIDField(blank=True, null=True)
    payload_fingerprint = models.CharField(blank=True, max_length=64)
    event_time = models.DateTimeField(default=timezone.now, db_index=True)
    user_agent_fingerprint = models.CharField(blank=True, max_length=128)

    class Meta:
        ordering = ("-event_time",)
        indexes = [
            models.Index(
                fields=("quote", "event_time"), name="quotes_event_quote_time_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("quote_version", "response_idempotency_key"),
                condition=Q(response_idempotency_key__isnull=False),
                name="quotes_event_version_idempotency_unique",
            ),
            models.UniqueConstraint(
                fields=("share_link", "event_type"),
                condition=Q(
                    share_link__isnull=False,
                    event_type__in=(
                        QuoteEventType.VIEWED,
                        QuoteEventType.REVOKED,
                        QuoteEventType.EXPIRED,
                    ),
                ),
                name="quotes_event_link_lifecycle_unique",
            ),
        ]

    def __init__(self, *args, **kwargs):
        _event_aliases(kwargs)
        super().__init__(*args, **kwargs)

    @property
    def occurred_at(self):
        return self.event_time

    @property
    def event_timestamp(self):
        return self.event_time

    @property
    def timestamp(self):
        return self.event_time

    @property
    def created_at(self):
        return self.event_time

    def _validate_references(self) -> None:
        errors: dict[str, list[str]] = {}
        version = None
        if self.quote_version_id:
            version = QuoteVersion.objects.filter(pk=self.quote_version_id).first()
            if version is None or version.quote_id != self.quote_id:
                errors["quote_version"] = [
                    "The event version must belong to the event quote."
                ]
        link = None
        if self.share_link_id:
            link = ShareLink.objects.filter(pk=self.share_link_id).first()
            if link is None:
                errors["share_link"] = ["The event share link does not exist."]
            elif self.quote_version_id != link.quote_version_id:
                errors["share_link"] = [
                    "The event link and version must belong together."
                ]
            elif version is not None and link.quote_version.quote_id != self.quote_id:
                errors["share_link"] = [
                    "The event share link must belong to the event quote."
                ]
        if self.share_link_id and not self.quote_version_id:
            errors["quote_version"] = [
                "A share-linked event must identify its quote version."
            ]
        if self.event_type in {
            QuoteEventType.ACCEPTED,
            QuoteEventType.CHANGES_REQUESTED,
        }:
            if not self.quote_version_id:
                errors["quote_version"] = ["Response events require a quote version."]
            if self.response_idempotency_key is None:
                errors["response_idempotency_key"] = [
                    "Response events require an idempotency key."
                ]
            if (
                not isinstance(self.payload_fingerprint, str)
                or len(self.payload_fingerprint) != 64
            ):
                errors["payload_fingerprint"] = [
                    "Response events require a SHA-256 payload fingerprint."
                ]
        if (
            self.quote_version_id_snapshot is not None
            and self.quote_version_id_snapshot != self.quote_version_id
        ):
            errors["quote_version_id_snapshot"] = [
                "The event version identifier must match its quote version."
            ]
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        if not self._state.adding:
            raise ValidationError("Quote events are immutable.")
        self._validate_references()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Quote events are immutable.")
        if self.quote_version_id and self.quote_version_id_snapshot is None:
            self.quote_version_id_snapshot = self.quote_version_id
        self._validate_references()
        self.note = self.note or ""
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Quote events are immutable.")

    def __str__(self) -> str:
        return f"{self.quote_id} {self.event_type}"
