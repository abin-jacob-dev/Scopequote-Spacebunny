from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from django.db import transaction

from quotes.models import Quote, QuoteEvent, QuoteVersion, ShareLink
from quotes.services.tokens import (
    TokenDecryptionError,
    build_associated_data,
    current_key_version,
    decrypt_token,
    encrypt_token,
    generate_token,
    hash_token,
)

MAX_LINK_AGE = timedelta(days=90)


class LinkLifecycleError(ValueError):
    pass


def normalize_datetime(value: datetime, field_name: str = "now") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _expiry_for_quote(quote: Quote, now: datetime) -> datetime:
    expiry = now + MAX_LINK_AGE
    if quote.valid_until is not None:
        commercial_expiry = datetime.combine(
            quote.valid_until,
            time.max,
            tzinfo=UTC,
        )
        expiry = min(expiry, commercial_expiry)
    return expiry


def link_state(link: ShareLink, *, now: datetime | None = None) -> str:
    if not isinstance(link, ShareLink):
        raise TypeError("link must be a ShareLink")
    if link.revoked_at is not None:
        return "revoked"
    current = normalize_datetime(now or datetime.now(UTC))
    expires_at = normalize_datetime(link.expires_at, "expires_at")
    return "expired" if expires_at <= current else "active"


def is_link_active(link: ShareLink, *, now: datetime) -> bool:
    return link_state(link, now=now) == "active"


def get_link_state(link: ShareLink, *, now: datetime) -> str:
    return link_state(link, now=now)


def _create_link_for_version(
    *,
    version: QuoteVersion,
    quote: Quote,
    now: datetime,
) -> ShareLink:
    if not isinstance(version, QuoteVersion) or version.pk is None:
        raise LinkLifecycleError("A persisted quote version is required.")
    if version.quote_id != quote.pk:
        raise LinkLifecycleError("The quote version does not belong to the quote.")
    link_id = uuid.uuid4()
    token = generate_token()
    link = ShareLink(
        id=link_id,
        quote_version=version,
        token_hash=hash_token(token),
        encrypted_token=encrypt_token(
            token,
            share_link_id=link_id,
            quote_version_id=version.pk,
        ),
        encryption_key_version=current_key_version(),
        associated_data=build_associated_data(link_id, version.pk),
        expires_at=_expiry_for_quote(quote, now),
        created_by_id=quote.owner_id,
    )
    return ShareLink.objects.create(
        id=link.id,
        quote_version=link.quote_version,
        token_hash=link.token_hash,
        encrypted_token=link.encrypted_token,
        encryption_key_version=link.encryption_key_version,
        associated_data=link.associated_data,
        expires_at=link.expires_at,
        created_by_id=link.created_by_id,
    )


def _validate_link_parent(
    *,
    quote: Quote,
    version: QuoteVersion,
    now: datetime,
) -> None:
    if quote.status not in {Quote.Status.SENT, Quote.Status.VIEWED}:
        raise LinkLifecycleError("Only a sent or viewed quote can receive a link.")
    if quote.valid_until is not None and now.date() > quote.valid_until:
        raise LinkLifecycleError("The quote validity period has ended.")


def create_share_link(*, version: QuoteVersion, now: datetime) -> ShareLink:
    current = normalize_datetime(now)
    if not isinstance(version, QuoteVersion) or version.pk is None:
        raise LinkLifecycleError("A persisted quote version is required.")
    with transaction.atomic():
        quote = Quote.objects.select_for_update().get(pk=version.quote_id)
        locked_version = QuoteVersion.objects.select_for_update().get(pk=version.pk)
        if locked_version.quote_id != quote.pk:
            raise LinkLifecycleError("The quote version does not belong to the quote.")
        _validate_link_parent(quote=quote, version=locked_version, now=current)
        return _create_link_for_version(
            version=locked_version,
            quote=quote,
            now=current,
        )


def resolve_share_link(*, token: str, now: datetime) -> ShareLink | None:
    current = normalize_datetime(now)
    try:
        token_hash = hash_token(token)
    except (TypeError, ValueError):
        return None
    try:
        link = ShareLink.objects.select_related(
            "quote_version",
            "quote_version__quote",
        ).get(token_hash=token_hash)
    except ShareLink.DoesNotExist:
        return None
    if link_state(link, now=current) != "active":
        return None
    return link


def decrypt_link_token(link: ShareLink) -> str:
    if not isinstance(link, ShareLink) or link.pk is None:
        raise LinkLifecycleError("A persisted share link is required.")
    expected_associated_data = build_associated_data(link.pk, link.quote_version_id)
    if link.associated_data != expected_associated_data:
        raise TokenDecryptionError("The encrypted share token could not be read.")
    return decrypt_token(
        link.encrypted_token,
        share_link_id=link.pk,
        quote_version_id=link.quote_version_id,
        key_version=link.encryption_key_version,
    )


def recopy_token(link: ShareLink) -> str:
    return decrypt_link_token(link)


def recopy_share_link_token(link: ShareLink) -> str:
    return decrypt_link_token(link)


def _create_revocation_event(link: ShareLink, *, now: datetime) -> QuoteEvent:
    return QuoteEvent.objects.create(
        quote=link.quote,
        quote_version=link.quote_version,
        share_link=link,
        event_type=QuoteEvent.EventType.REVOKED,
        event_time=now,
    )


def _revoke_locked_link(link: ShareLink, *, now: datetime) -> ShareLink:
    if link.revoked_at is not None:
        return link
    link.revoked_at = now
    link.save(update_fields=("revoked_at",))
    _create_revocation_event(link, now=now)
    return link


def revoke_share_link(*, link: ShareLink, now: datetime) -> ShareLink:
    current = normalize_datetime(now)
    if not isinstance(link, ShareLink) or link.pk is None:
        raise LinkLifecycleError("A persisted share link is required.")
    with transaction.atomic():
        identity = ShareLink.objects.get(pk=link.pk)
        Quote.objects.select_for_update().get(pk=identity.quote_version.quote_id)
        QuoteVersion.objects.select_for_update().get(pk=identity.quote_version_id)
        locked = ShareLink.objects.select_for_update().get(pk=link.pk)
        return _revoke_locked_link(locked, now=current)


def reissue_share_link(*, link: ShareLink, now: datetime) -> ShareLink:
    current = normalize_datetime(now)
    if not isinstance(link, ShareLink) or link.pk is None:
        raise LinkLifecycleError("A persisted share link is required.")
    with transaction.atomic():
        identity = ShareLink.objects.select_related("quote_version").get(pk=link.pk)
        quote = Quote.objects.select_for_update().get(
            pk=identity.quote_version.quote_id
        )
        old_version = QuoteVersion.objects.select_for_update().get(
            pk=identity.quote_version_id
        )
        old_link = ShareLink.objects.select_for_update().get(pk=link.pk)
        _validate_link_parent(quote=quote, version=old_version, now=current)
        new_link = _create_link_for_version(
            version=old_version,
            quote=quote,
            now=current,
        )
        _revoke_locked_link(old_link, now=current)
        return new_link
