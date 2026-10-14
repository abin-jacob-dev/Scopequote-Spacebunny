from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from quotes.models import Quote, QuoteEvent, QuoteVersion, ShareLink
from quotes.services.links import is_link_active, normalize_datetime

RESPONSE_ACTIONS = {
    "accepted",
    "changes_requested",
}
_RESPONSE_ACTION_ALIASES = {
    "accept": "accepted",
    "accepted": "accepted",
    "request_changes": "changes_requested",
    "request-changes": "changes_requested",
    "changes_requested": "changes_requested",
    "changes-requested": "changes_requested",
}


@dataclass(frozen=True)
class ResponseResult:
    status_code: int
    status: str
    message: str
    existing_response: QuoteEvent | None


def _result(
    status_code: int,
    status: str,
    message: str,
    existing_response: QuoteEvent | None = None,
) -> ResponseResult:
    return ResponseResult(status_code, status, message, existing_response)


def _unavailable() -> ResponseResult:
    return _result(404, "unavailable", "This quotation link is unavailable.")


def _conflict(existing_response: QuoteEvent | None = None) -> ResponseResult:
    return _result(
        409,
        "conflict",
        "A response has already been recorded for this quotation.",
        existing_response,
    )


def _invalid() -> ResponseResult:
    return _result(400, "invalid", "The response could not be recorded.")


def normalize_action(action: str) -> str | None:
    if not isinstance(action, str):
        return None
    return _RESPONSE_ACTION_ALIASES.get(action.strip().lower())


def normalize_note(note: str) -> str | None:
    if note is None:
        return ""
    if not isinstance(note, str):
        return None
    normalized = unicodedata.normalize("NFKC", note)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > 2000:
        return None
    return normalized


def response_fingerprint(*, action: str, note: str) -> str:
    payload = json.dumps(
        {"action": action, "note": note},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_result(event: QuoteEvent, *, existing: bool) -> ResponseResult:
    if event.event_type == QuoteEvent.EventType.ACCEPTED:
        message = "Quotation accepted."
    else:
        message = "Changes requested."
    return _result(
        200,
        event.event_type,
        message,
        event if existing else None,
    )


def _response_event_type(action: str) -> str:
    if action == "accepted":
        return QuoteEvent.EventType.ACCEPTED
    return QuoteEvent.EventType.CHANGES_REQUESTED


def _lock_version(version_id: uuid.UUID):
    return QuoteVersion.objects.select_for_update().get(pk=version_id)


def _existing_for_key(version: QuoteVersion, key: uuid.UUID) -> QuoteEvent | None:
    return QuoteEvent.objects.filter(
        quote_version=version,
        response_idempotency_key=key,
    ).first()


def _authoritative_response(version: QuoteVersion) -> QuoteEvent | None:
    return (
        QuoteEvent.objects.filter(
            quote_version=version,
            event_type__in=(
                QuoteEvent.EventType.ACCEPTED,
                QuoteEvent.EventType.CHANGES_REQUESTED,
            ),
        )
        .order_by("event_time", "pk")
        .first()
    )


def _revoke_active_links(quote: Quote, *, now: datetime) -> None:
    links = list(
        ShareLink.objects.select_for_update()
        .select_related("quote_version__quote")
        .filter(quote_version__quote_id=quote.pk)
    )
    for link in links:
        if link.revoked_at is not None or not is_link_active(link, now=now):
            continue
        link.revoked_at = now
        link.save(update_fields=("revoked_at",))
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=link.quote_version,
            share_link=link,
            event_type=QuoteEvent.EventType.REVOKED,
            event_time=now,
        )


def record_response(
    *,
    link: ShareLink,
    action: str,
    note: str,
    idempotency_key: uuid.UUID,
    now: datetime,
) -> ResponseResult:
    try:
        current = normalize_datetime(now)
    except (TypeError, ValueError):
        return _invalid()

    normalized_action = normalize_action(action)
    normalized_note = normalize_note(note)
    try:
        key = (
            idempotency_key
            if isinstance(idempotency_key, uuid.UUID)
            else uuid.UUID(str(idempotency_key))
        )
    except (TypeError, ValueError, AttributeError):
        return _invalid()
    if normalized_action not in RESPONSE_ACTIONS or normalized_note is None:
        return _invalid()
    if not isinstance(link, ShareLink) or link.pk is None:
        return _unavailable()

    fingerprint = response_fingerprint(
        action=normalized_action,
        note=normalized_note,
    )

    try:
        with transaction.atomic():
            try:
                unlocked_link = ShareLink.objects.get(pk=link.pk)
                version_identity = QuoteVersion.objects.get(
                    pk=unlocked_link.quote_version_id,
                )
                quote = Quote.objects.select_for_update().get(
                    pk=version_identity.quote_id,
                )
                version = _lock_version(unlocked_link.quote_version_id)
                locked_link = ShareLink.objects.select_for_update().get(pk=link.pk)
            except (
                QuoteVersion.DoesNotExist,
                Quote.DoesNotExist,
                ShareLink.DoesNotExist,
            ):
                return _unavailable()

            existing = _existing_for_key(version, key)
            if existing is not None:
                existing_fingerprint = existing.payload_fingerprint
                if isinstance(existing_fingerprint, str) and hmac.compare_digest(
                    existing_fingerprint,
                    fingerprint,
                ):
                    return _event_result(existing, existing=True)
                return _conflict(existing)

            if not is_link_active(locked_link, now=current):
                return _unavailable()

            authoritative = _authoritative_response(version)
            if authoritative is not None:
                return _conflict(authoritative)

            if quote.status not in {Quote.Status.SENT, Quote.Status.VIEWED}:
                return _conflict()

            event_type = _response_event_type(normalized_action)
            event = QuoteEvent.objects.create(
                quote=quote,
                quote_version=version,
                share_link=locked_link,
                event_type=event_type,
                note=normalized_note,
                response_idempotency_key=key,
                payload_fingerprint=fingerprint,
                event_time=current,
            )

            if normalized_action == "accepted":
                quote.status = Quote.Status.ACCEPTED
                quote.accepted_at = current
                _revoke_active_links(quote, now=current)
            else:
                quote.status = Quote.Status.CHANGES_REQUESTED
            quote.save()
            return _event_result(event, existing=False)
    except ValidationError:
        return _conflict()
    except (QuoteEvent.DoesNotExist, QuoteVersion.DoesNotExist, ShareLink.DoesNotExist):
        return _unavailable()
