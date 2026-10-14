from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from accounts.services import canonicalize_email
from quotes.models import (
    _PUBLICATION_MUTATION_GUARD,
    _SEQUENCE_ALLOCATION_GUARD,
    Quote,
    QuoteEvent,
    QuoteLineItem,
    QuoteNumberSequence,
    QuoteStatus,
    QuoteVersion,
    ShareLink,
    _publication_mutation_context,
    _sequence_allocation_context,
)
from quotes.services.snapshots import build_quote_snapshot
from quotes.services.storage import (
    StoredLogo,
    commit_version_logo,
    delete_object,
    inspect_stored_logo,
)

MONEY_QUANTUM = Decimal("0.01")
DISPLAY_FEE_QUANTUM = Decimal("5")
MAX_LINE_ITEMS = 50
MAX_QUOTE_NUMBER_SUFFIX = 9999
MIN_QUOTE_VALIDITY_DAYS = 1
MAX_QUOTE_VALIDITY_DAYS = 90
ZERO = Decimal("0.00")


class QuoteNumberExhausted(ValueError):
    pass


def quote_validity_days(*, issue_date: date, valid_until: date) -> int:
    if not isinstance(issue_date, date) or isinstance(issue_date, datetime):
        raise ValueError("Issue date must be a date.")
    if not isinstance(valid_until, date) or isinstance(valid_until, datetime):
        raise ValueError("Valid-until date must be a date.")
    days = (valid_until - issue_date).days
    if days < MIN_QUOTE_VALIDITY_DAYS or days > MAX_QUOTE_VALIDITY_DAYS:
        raise ValueError("Quote validity must be from 1 to 90 days.")
    return days


def _decimal(value: Any, field_name: str = "amount") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal.") from exc


def _money(value: Any, field_name: str = "amount") -> Decimal:
    try:
        return _decimal(value, field_name).quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a valid decimal.") from exc


def _utc_year(now: datetime) -> int:
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).year


def _get_locked_sequence(year: int) -> QuoteNumberSequence:
    sequence = QuoteNumberSequence.objects.select_for_update().filter(year=year).first()
    if sequence is not None:
        return sequence
    try:
        with transaction.atomic():
            return QuoteNumberSequence.objects.create(
                year=year,
                last_allocated_number=0,
            )
    except IntegrityError:
        return QuoteNumberSequence.objects.select_for_update().get(year=year)


def allocate_quote_number(*, now: datetime) -> str:
    year = _utc_year(now)
    with _sequence_allocation_context(_SEQUENCE_ALLOCATION_GUARD), transaction.atomic():
        sequence = _get_locked_sequence(year)
        if sequence.last_allocated_number >= MAX_QUOTE_NUMBER_SUFFIX:
            raise QuoteNumberExhausted(f"Quote numbers for {year} are exhausted.")
        sequence.last_allocated_number += 1
        sequence.save(update_fields=("last_allocated_number", "updated_at"))
    return f"SQ-{year:04d}-{sequence.last_allocated_number:04d}"


def delete_quote_tree(quote: Quote):
    if not isinstance(quote, Quote) or quote.pk is None:
        return 0, {}
    quote_id = quote.pk
    with transaction.atomic():
        models.QuerySet.delete(QuoteEvent.objects.filter(quote_id=quote_id))
        models.QuerySet.delete(
            ShareLink.objects.filter(quote_version__quote_id=quote_id)
        )
        models.QuerySet.delete(QuoteVersion.objects.filter(quote_id=quote_id))
        models.QuerySet.delete(QuoteLineItem.objects.filter(quote_id=quote_id))
        return models.QuerySet.delete(Quote.objects.filter(pk=quote_id))


def calculate_line_item_total(items: Iterable[QuoteLineItem]) -> Decimal:
    try:
        materialized = tuple(items)
    except TypeError as exc:
        raise TypeError("items must be an iterable of quote line items") from exc
    if not materialized:
        raise ValueError("At least one line item is required.")
    if len(materialized) > MAX_LINE_ITEMS:
        raise ValueError("A quote cannot contain more than 50 line items.")

    total = ZERO
    has_positive_item = False
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_UP
        for item in materialized:
            raw_amount = getattr(item, "amount", item)
            amount = _money(raw_amount)
            total += amount
            has_positive_item = has_positive_item or amount > ZERO
    if not has_positive_item:
        raise ValueError("At least one line item must have a positive amount.")
    if total <= ZERO:
        raise ValueError("The line-item total must be greater than zero.")
    return total.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_deposit_amount(
    chosen_total: Decimal,
    deposit_percentage: int,
) -> Decimal:
    percentage = _decimal(deposit_percentage, "deposit_percentage")
    if percentage != percentage.to_integral_value():
        raise ValueError("Deposit percentage must be a whole number.")
    percentage_int = int(percentage)
    if percentage_int < 0 or percentage_int > 100 or percentage_int % 5:
        raise ValueError(
            "Deposit percentage must be from 0 to 100 in five-point increments."
        )
    total = _decimal(chosen_total, "chosen_total")
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_UP
        return (total * percentage / Decimal("100")).quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )


def calculate_realized_hourly_equivalent(
    chosen_total: Decimal,
    effective_hours: Decimal,
) -> Decimal:
    total = _decimal(chosen_total, "chosen_total")
    hours = _decimal(effective_hours, "effective_hours")
    if hours <= ZERO:
        raise ValueError("Effective hours must be greater than zero.")
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_UP
        return (total / hours).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _display_fee(value: Any, field_name: str) -> Decimal:
    amount = _decimal(value, field_name)
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_UP
        return (amount / DISPLAY_FEE_QUANTUM).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        ) * DISPLAY_FEE_QUANTUM


def requires_intentional_adjustment(
    *,
    chosen_total: Decimal,
    recommended_min: Decimal,
    recommended_max: Decimal,
) -> bool:
    total = _money(chosen_total, "chosen_total")
    minimum = _display_fee(recommended_min, "recommended_min")
    maximum = _display_fee(recommended_max, "recommended_max")
    if minimum > maximum:
        raise ValueError("The recommended minimum cannot exceed the maximum.")
    return total < minimum or total > maximum


is_intentional_adjustment_required = requires_intentional_adjustment
requires_intentional_adjustment_confirmation = requires_intentional_adjustment


def is_intentional_adjustment(
    chosen_total: Decimal,
    recommended_min: Decimal,
    recommended_max: Decimal,
) -> bool:
    return requires_intentional_adjustment(
        chosen_total=chosen_total,
        recommended_min=recommended_min,
        recommended_max=recommended_max,
    )


def requires_intentional_confirmation(
    chosen_total: Decimal,
    recommended_min: Decimal,
    recommended_max: Decimal,
) -> bool:
    return is_intentional_adjustment(
        chosen_total,
        recommended_min,
        recommended_max,
    )


def is_within_recommended_range(
    *,
    chosen_total: Decimal,
    recommended_min: Decimal,
    recommended_max: Decimal,
) -> bool:
    return not requires_intentional_adjustment(
        chosen_total=chosen_total,
        recommended_min=recommended_min,
        recommended_max=recommended_max,
    )


derive_deposit_amount = calculate_deposit_amount
calculate_deposit = calculate_deposit_amount
calculate_realized_hourly = calculate_realized_hourly_equivalent
calculate_quote_total = calculate_line_item_total


def validate_publish_quote(
    quote: Quote,
    *,
    intentional_adjustment_confirmed: bool = False,
) -> Decimal:
    if not isinstance(quote, Quote):
        raise TypeError("quote must be a Quote instance")

    errors: dict[str, list[str]] = {}

    def add_text_error(field_name: str, label: str, minimum: int, maximum: int) -> None:
        value = getattr(quote, field_name)
        if not isinstance(value, str) or not value.strip():
            errors[field_name] = [f"{label} is required before publishing."]
        elif len(value.strip()) < minimum:
            errors[field_name] = [
                f"{label} must contain at least {minimum} characters."
            ]
        elif len(value) > maximum:
            errors[field_name] = [
                f"{label} must contain no more than {maximum} characters."
            ]

    def add_email_error(field_name: str, label: str) -> None:
        value = getattr(quote, field_name)
        if not isinstance(value, str) or not value.strip():
            errors[field_name] = [f"{label} is required before publishing."]
            return
        if len(value) > 254:
            errors[field_name] = [f"{label} must contain no more than 254 characters."]
            return
        try:
            validate_email(canonicalize_email(value))
        except ValidationError:
            errors[field_name] = [f"Enter a valid {label.lower()}."]

    add_text_error("project_title", "Project title", 1, 160)
    add_text_error("project_description", "Project overview", 30, 3000)
    add_text_error("client_name", "Client name", 1, 160)
    add_email_error("client_email", "Client email")
    add_text_error("sender_name", "Sender name", 1, 160)
    add_email_error("sender_email", "Sender email")
    add_text_error("terms", "Terms", 1, 2000)

    for field_name, label in (
        ("issue_date", "Issue date"),
        ("valid_until", "Valid-until date"),
    ):
        value = getattr(quote, field_name)
        if not isinstance(value, date) or isinstance(value, datetime):
            errors[field_name] = [f"{label} is required before publishing."]
    if (
        isinstance(quote.issue_date, date)
        and not isinstance(quote.issue_date, datetime)
        and isinstance(quote.valid_until, date)
        and not isinstance(quote.valid_until, datetime)
    ):
        try:
            quote_validity_days(
                issue_date=quote.issue_date,
                valid_until=quote.valid_until,
            )
        except ValueError as error:
            errors["valid_until"] = [str(error)]

    try:
        quote.validate_for_publish()
    except ValidationError as error:
        for field_name, messages in error.message_dict.items():
            errors.setdefault(field_name, []).extend(messages)

    total = None
    try:
        total = calculate_line_item_total(tuple(quote.line_items.all()))
    except (TypeError, ValueError) as error:
        errors.setdefault("line_items", []).append(str(error))

    try:
        effective_hours = _decimal(quote.effective_hours, "effective_hours")
    except ValueError as error:
        errors.setdefault("effective_hours", []).append(str(error))
    else:
        if effective_hours <= ZERO:
            errors.setdefault("effective_hours", []).append(
                "Effective hours must be greater than zero before publishing."
            )

    if total is not None:
        try:
            requires_confirmation = requires_intentional_adjustment(
                chosen_total=total,
                recommended_min=quote.minimum,
                recommended_max=quote.maximum,
            )
        except ValueError as error:
            errors.setdefault("pricing", []).append(str(error))
        else:
            if requires_confirmation and not intentional_adjustment_confirmed:
                errors["intentional_adjustment_confirmed"] = [
                    "Confirm the intentional adjustment before publishing."
                ]

    if errors:
        raise ValidationError(errors)
    return total


validate_publish_domain = validate_publish_quote


def _check_publish_permission(quote: Quote, user) -> None:
    if not isinstance(quote, Quote) or quote.pk is None:
        raise ValueError("A persisted quote is required.")
    if user is None or not getattr(user, "is_authenticated", False):
        raise PermissionDenied("Authentication is required to publish a quote.")
    if quote.owner_id != user.pk:
        raise PermissionDenied("You do not own this quote.")
    if not user.can_publish:
        raise PermissionDenied("Verify your email before publishing a quote.")


def publish_quote(
    *,
    quote: Quote,
    user,
    staged_logo: StoredLogo | None = None,
    intentional_adjustment_confirmed: bool = False,
) -> QuoteVersion:
    _check_publish_permission(quote, user)
    if staged_logo is not None and not isinstance(staged_logo, StoredLogo):
        raise TypeError("staged_logo must be a StoredLogo")
    if staged_logo is not None and staged_logo.owner_id != user.pk:
        raise PermissionDenied("The staged logo belongs to another account.")

    destination_key = None
    version = None
    try:
        with transaction.atomic():
            locked_quote = Quote.objects.select_for_update().get(pk=quote.pk)
            if locked_quote.owner_id != user.pk:
                raise PermissionDenied("You do not own this quote.")
            if locked_quote.status != QuoteStatus.DRAFT:
                raise ValidationError("Published quotes cannot be published again.")
            if locked_quote.current_published_version_id is not None:
                raise ValidationError("This quote already has a published version.")
            validate_publish_quote(
                locked_quote,
                intentional_adjustment_confirmed=intentional_adjustment_confirmed,
            )

            logo_key = locked_quote.logo_object_key
            logo_source = staged_logo
            remove_source = True
            if logo_source is None and logo_key:
                logo_source = StoredLogo(
                    storage_key=logo_key,
                    content_type=locked_quote.logo_content_type,
                    byte_size=locked_quote.logo_byte_size or 0,
                    owner_id=locked_quote.owner_id,
                )
                remove_source = False
            version_id = uuid.uuid4()
            if logo_source is not None:
                validated_logo = inspect_stored_logo(logo_source)
                commit_kwargs = {"version_id": version_id}
                if not remove_source:
                    commit_kwargs["remove_source"] = False
                destination_key = commit_version_logo(
                    validated_logo,
                    **commit_kwargs,
                )
                logo_key = destination_key
                locked_quote.logo_content_type = validated_logo.content_type
                locked_quote.logo_byte_size = validated_logo.byte_size
            else:
                locked_quote.logo_content_type = ""
                locked_quote.logo_byte_size = None

            locked_quote.quote_number = allocate_quote_number(now=timezone.now())
            locked_quote.logo_object_key = logo_key
            locked_quote.status = QuoteStatus.SENT
            published_at = timezone.now()
            locked_quote.sent_at = published_at
            locked_quote.chosen_total = locked_quote.line_item_total
            locked_quote.deposit_amount = locked_quote.derive_deposit_amount()
            snapshot = build_quote_snapshot(locked_quote)
            version = QuoteVersion(
                id=version_id,
                quote=locked_quote,
                version_number=1,
                snapshot=snapshot,
                frozen_total=locked_quote.chosen_total,
                currency=locked_quote.currency,
                logo_object_key=logo_key,
            )
            version = QuoteVersion.objects.create(
                id=version.id,
                quote=locked_quote,
                version_number=version.version_number,
                snapshot=version.snapshot,
                frozen_total=version.frozen_total,
                currency=version.currency,
                logo_object_key=version.logo_object_key,
            )
            locked_quote.current_published_version = version
            with _publication_mutation_context(_PUBLICATION_MUTATION_GUARD):
                locked_quote.save()
            from quotes.services.links import create_share_link

            first_link = create_share_link(version=version, now=published_at)
            version.share_link = first_link
            version.first_share_link = first_link
            version.initial_share_link = first_link
            QuoteEvent.objects.create(
                quote=locked_quote,
                quote_version=version,
                event_type=QuoteEvent.Type.PUBLISHED,
                event_time=locked_quote.sent_at,
            )
    except Exception:
        if destination_key:
            delete_object(destination_key)
        if staged_logo is not None:
            delete_object(staged_logo.storage_key)
        raise

    quote.refresh_from_db()
    return version


_DUPLICATABLE_FIELDS = (
    "project_title",
    "project_description",
    "category",
    "delivery_days",
    "complexity",
    "revision_rounds",
    "urgency",
    "experience",
    "currency",
    "pricing_config_version",
    "effective_hours",
    "minimum",
    "recommended",
    "maximum",
    "chosen_total",
    "hourly_equivalent",
    "client_name",
    "client_company",
    "client_email",
    "sender_name",
    "sender_business_name",
    "sender_email",
    "sender_website",
    "sender_monogram",
    "accent_color",
    "issue_date",
    "valid_until",
    "deposit_percentage",
    "timeline",
    "revision_terms",
    "terms",
    "notes",
    "exclusions",
    "taxes",
    "cancellation_terms",
)


def duplicate_quote(*, quote: Quote, user) -> Quote:
    if not isinstance(quote, Quote) or quote.pk is None:
        raise ValueError("A persisted quote is required.")
    if user is None or not getattr(user, "is_authenticated", False):
        raise PermissionDenied("Authentication is required to duplicate a quote.")
    if quote.owner_id != user.pk:
        raise PermissionDenied("You do not own this quote.")

    with transaction.atomic():
        source = (
            Quote.objects.select_for_update().filter(pk=quote.pk, owner=user).first()
        )
        if source is None:
            raise PermissionDenied("You do not own this quote.")
        values = {
            field_name: getattr(source, field_name)
            for field_name in _DUPLICATABLE_FIELDS
        }
        duplicate = Quote.objects.create(owner=user, **values)
        for item in source.line_items.all():
            QuoteLineItem.objects.create(
                quote=duplicate,
                label=item.label,
                description=item.description,
                amount=item.amount,
                sort_order=item.sort_order,
            )
        duplicate.recalculate_line_item_total()
        duplicate.save()
    return duplicate


copy_quote = duplicate_quote
