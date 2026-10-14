from __future__ import annotations

import copy
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from pricing.classifier import classify_description
from pricing.engine import calculate_estimate
from pricing.forms import EstimateForm
from pricing.types import EstimateInputs
from quotes.models import Quote, QuoteLineItem

GUEST_ESTIMATE_KEY = "scopequote.guest_estimate"
GUEST_QUOTE_KEY = "scopequote.guest_quote"
CONVERSION_NONCE_KEY = "scopequote.conversion_nonce"
GUEST_SESSION_AGE = timedelta(days=14)
MAX_GUEST_PAYLOAD_BYTES = 64 * 1024

_ESTIMATE_CONTENT_KEYS = frozenset({"inputs", "result", "scope"})
_GUEST_STATE_KEYS = (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY)
_GUEST_SESSION_CREATED_AT_KEY = "_scopequote.guest_created_at"


class GuestStateError(ValueError):
    pass


class GuestStateConflict(GuestStateError):
    pass


class GuestPayloadTooLarge(GuestStateError):
    pass


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _authoritative_payload(inputs: dict[str, Any]) -> dict[str, Any]:
    estimate_inputs = EstimateInputs(**inputs)
    estimate = calculate_estimate(estimate_inputs)
    scope = classify_description(estimate_inputs.description)
    return {
        "inputs": {
            "description": estimate_inputs.description,
            "delivery_days": estimate_inputs.delivery_days,
            "complexity": estimate_inputs.complexity,
            "revision_rounds": estimate_inputs.revision_rounds,
            "urgency": estimate_inputs.urgency,
            "experience": estimate_inputs.experience,
            "currency": estimate_inputs.currency,
        },
        "result": {
            "effective_hours": _decimal_text(estimate.effective_hours),
            "minimum": _decimal_text(estimate.minimum),
            "recommended": _decimal_text(estimate.recommended),
            "maximum": _decimal_text(estimate.maximum),
            "hourly_equivalent": _decimal_text(estimate.hourly_equivalent),
            "pricing_config_version": estimate.pricing_config_version,
            "factors": [dict(factor) for factor in estimate.factors],
        },
        "scope": {
            "category": scope.category,
            "matched_phrases": list(scope.matched_phrases),
            "confidence": scope.confidence,
            "suggested_items": list(scope.suggested_items),
        },
    }


def _payload_size(payload: dict[str, Any]) -> int:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GuestStateError("Guest data must be JSON serializable.") from exc
    return len(encoded)


def _enforce_size(payload: dict[str, Any]) -> None:
    if _payload_size(payload) > MAX_GUEST_PAYLOAD_BYTES:
        raise GuestPayloadTooLarge("Guest data cannot exceed 64 KB.")


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise GuestStateError("Guest expiry is invalid.") from exc
    else:
        raise GuestStateError("Guest expiry is invalid.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clear_guest_state(request) -> None:
    for key in _GUEST_STATE_KEYS:
        request.session.pop(key, None)


def _reset_guest_boundary(request, now: datetime) -> datetime:
    _clear_guest_state(request)
    request.session.pop("_session_expiry", None)
    request.session[_GUEST_SESSION_CREATED_AT_KEY] = now.isoformat()
    return now + GUEST_SESSION_AGE


def _migrate_guest_created_at(request, now: datetime) -> datetime | None:
    expiries = []
    for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY):
        if key not in request.session:
            continue
        payload = request.session[key]
        if not isinstance(payload, dict) or "expires_at" not in payload:
            return None
        try:
            expiries.append(_parse_datetime(payload["expires_at"]))
        except GuestStateError:
            return None
    if not expiries:
        return now
    if any(expiry != expiries[0] for expiry in expiries[1:]):
        return None
    created_at = expiries[0] - GUEST_SESSION_AGE
    if created_at > now or expiries[0] <= now:
        return None
    return created_at


def _guest_session_expiry(request) -> datetime:
    now = timezone.now().astimezone(UTC)
    if _GUEST_SESSION_CREATED_AT_KEY not in request.session:
        created_at = _migrate_guest_created_at(request, now)
        if created_at is None:
            return _reset_guest_boundary(request, now)
        request.session[_GUEST_SESSION_CREATED_AT_KEY] = created_at.isoformat()
    else:
        try:
            created_at = _parse_datetime(request.session[_GUEST_SESSION_CREATED_AT_KEY])
        except GuestStateError:
            return _reset_guest_boundary(request, now)
        if created_at > now or created_at + GUEST_SESSION_AGE <= now:
            return _reset_guest_boundary(request, now)
    return created_at + GUEST_SESSION_AGE


def _rotate_nonce(request, *, previous: str | None = None) -> str:
    nonce = secrets.token_urlsafe(32)
    while previous is not None and hmac.compare_digest(nonce, previous):
        nonce = secrets.token_urlsafe(32)
    request.session[CONVERSION_NONCE_KEY] = nonce
    return nonce


def _validated_estimate_payload(
    payload: Any, *, expected_expiry: datetime | None
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not _ESTIMATE_CONTENT_KEYS.issubset(payload):
        raise GuestStateError("Guest estimate data is invalid.")
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        raise GuestStateError("Guest estimate inputs are invalid.")
    form = EstimateForm(inputs)
    if not form.is_valid():
        raise GuestStateError("Guest estimate inputs are invalid.")
    expected = _authoritative_payload(form.cleaned_input_values())
    if inputs != expected["inputs"]:
        raise GuestStateError("Guest estimate inputs are invalid.")
    if payload.get("result") != expected["result"]:
        raise GuestStateError("Guest estimate results are invalid.")
    if payload.get("scope") != expected["scope"]:
        raise GuestStateError("Guest scope suggestions are invalid.")

    expires_at = _parse_datetime(payload.get("expires_at"))
    if expected_expiry is not None and expires_at != expected_expiry:
        raise GuestStateError("Guest estimate expiry is invalid.")
    canonical = {
        "inputs": copy.deepcopy(expected["inputs"]),
        "result": copy.deepcopy(expected["result"]),
        "scope": copy.deepcopy(expected["scope"]),
        "expires_at": expires_at.isoformat(),
    }
    _enforce_size(canonical)
    return canonical


def save_guest_estimate(request, payload):
    if not isinstance(payload, dict) or not _ESTIMATE_CONTENT_KEYS.issubset(payload):
        raise GuestStateError("Guest estimate data is invalid.")
    temporary_payload = copy.deepcopy(payload)
    if "expires_at" in temporary_payload:
        _parse_datetime(temporary_payload["expires_at"])
    else:
        temporary_payload["expires_at"] = "1970-01-01T00:00:00+00:00"
    _validated_estimate_payload(temporary_payload, expected_expiry=None)
    _enforce_size(temporary_payload)

    expiry = _guest_session_expiry(request)
    canonical = {
        "inputs": copy.deepcopy(payload["inputs"]),
        "result": copy.deepcopy(payload["result"]),
        "scope": copy.deepcopy(payload["scope"]),
        "expires_at": expiry.isoformat(),
    }
    _validated_estimate_payload(canonical, expected_expiry=expiry)
    previous_nonce = request.session.get(CONVERSION_NONCE_KEY)
    request.session[GUEST_ESTIMATE_KEY] = canonical
    request.session.pop(GUEST_QUOTE_KEY, None)
    _rotate_nonce(request, previous=previous_nonce)
    return copy.deepcopy(canonical)


def get_guest_estimate(request) -> dict | None:
    payload = request.session.get(GUEST_ESTIMATE_KEY)
    if payload is None:
        return None
    try:
        expiry = _guest_session_expiry(request)
        if request.session.get(GUEST_ESTIMATE_KEY) is None:
            return None
        return _validated_estimate_payload(payload, expected_expiry=expiry)
    except GuestStateError:
        _clear_guest_state(request)
        return None


def _validated_guest_quote(
    request, payload: Any, estimate: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GuestStateError("Guest quote data is invalid.")
    required = {
        "inputs",
        "result",
        "scope",
        "conversion_nonce",
        "pdf_eligible",
        "created_at",
        "expires_at",
    }
    if set(payload) != required:
        raise GuestStateError("Guest quote data is invalid.")
    if any(payload[key] != estimate[key] for key in ("inputs", "result", "scope")):
        raise GuestStateError("Guest quote does not match its estimate.")
    nonce = request.session.get(CONVERSION_NONCE_KEY)
    quote_nonce = payload["conversion_nonce"]
    if (
        not isinstance(nonce, str)
        or not isinstance(quote_nonce, str)
        or not hmac.compare_digest(quote_nonce, nonce)
    ):
        raise GuestStateConflict("The guest quote nonce is stale.")
    if payload["pdf_eligible"] is not True:
        raise GuestStateError("Guest quote is not eligible for PDF rendering.")
    expires_at = _parse_datetime(payload["expires_at"])
    estimate_expiry = _parse_datetime(estimate["expires_at"])
    if expires_at != estimate_expiry or timezone.now().astimezone(UTC) >= expires_at:
        raise GuestStateError("Guest quote has expired.")
    _parse_datetime(payload["created_at"])
    canonical = copy.deepcopy(payload)
    _enforce_size(canonical)
    return canonical


def get_guest_quote(request) -> dict | None:
    estimate = get_guest_estimate(request)
    if estimate is None:
        return None
    payload = request.session.get(GUEST_QUOTE_KEY)
    if payload is None:
        return None
    try:
        return _validated_guest_quote(request, payload, estimate)
    except GuestStateError:
        request.session.pop(GUEST_QUOTE_KEY, None)
        previous_nonce = request.session.get(CONVERSION_NONCE_KEY)
        _rotate_nonce(request, previous=previous_nonce)
        return None


def create_guest_quote(request, payload) -> dict:
    if not isinstance(payload, dict):
        raise GuestStateConflict("The guest quote request is stale.")
    submitted_nonce = payload.get("nonce")
    current_nonce = request.session.get(CONVERSION_NONCE_KEY)
    if not isinstance(submitted_nonce, str) or not isinstance(current_nonce, str):
        raise GuestStateConflict("The guest quote request is stale.")
    if not hmac.compare_digest(submitted_nonce, current_nonce):
        raise GuestStateConflict("The guest quote request is stale.")

    estimate = get_guest_estimate(request)
    if estimate is None:
        raise GuestStateConflict("Start a new estimate before creating a quote.")
    expiry = _parse_datetime(estimate["expires_at"])
    nonce = _rotate_nonce(request, previous=current_nonce)
    quote = {
        "inputs": copy.deepcopy(estimate["inputs"]),
        "result": copy.deepcopy(estimate["result"]),
        "scope": copy.deepcopy(estimate["scope"]),
        "conversion_nonce": nonce,
        "pdf_eligible": True,
        "created_at": timezone.now().astimezone(UTC).isoformat(),
        "expires_at": expiry.isoformat(),
    }
    _enforce_size(quote)
    request.session[GUEST_QUOTE_KEY] = quote
    return copy.deepcopy(quote)


def _replace_guest_nonce(request, quote: dict[str, Any]) -> None:
    previous = request.session.get(CONVERSION_NONCE_KEY)
    nonce = _rotate_nonce(request, previous=previous)
    quote["conversion_nonce"] = nonce
    _enforce_size(quote)
    request.session[GUEST_QUOTE_KEY] = quote


def _quote_fields(user, guest_quote: dict[str, Any]) -> dict[str, Any]:
    inputs = guest_quote["inputs"]
    result = guest_quote["result"]
    scope = guest_quote["scope"]
    try:
        effective_hours = Decimal(result["effective_hours"])
        minimum = Decimal(result["minimum"])
        recommended = Decimal(result["recommended"])
        maximum = Decimal(result["maximum"])
        hourly_equivalent = Decimal(result["hourly_equivalent"])
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise GuestStateError("Guest quote amounts are invalid.") from exc
    return {
        "owner": user,
        "project_description": inputs["description"],
        "category": scope["category"],
        "delivery_days": inputs["delivery_days"],
        "complexity": inputs["complexity"],
        "revision_rounds": inputs["revision_rounds"],
        "urgency": inputs["urgency"],
        "experience": inputs["experience"],
        "currency": inputs["currency"],
        "pricing_config_version": result["pricing_config_version"],
        "effective_hours": effective_hours,
        "minimum": minimum,
        "recommended": recommended,
        "maximum": maximum,
        "chosen_total": recommended,
        "hourly_equivalent": hourly_equivalent,
    }


def convert_guest_quote(request, user) -> Quote | None:
    if user is None or not user.is_authenticated:
        return None
    nonce = request.session.get(CONVERSION_NONCE_KEY)
    if not isinstance(nonce, str) or not nonce:
        return None

    try:
        with transaction.atomic():
            quote = Quote.objects.filter(
                owner=user,
                guest_conversion_nonce=nonce,
            ).first()
            if quote is None:
                guest_quote = get_guest_quote(request)
                if guest_quote is None or not hmac.compare_digest(
                    guest_quote["conversion_nonce"], nonce
                ):
                    return None
                if (
                    Quote.objects.filter(guest_conversion_nonce=nonce)
                    .exclude(owner=user)
                    .exists()
                ):
                    _replace_guest_nonce(request, guest_quote)
                    return None
                quote = Quote.objects.create(
                    guest_conversion_nonce=nonce,
                    **_quote_fields(user, guest_quote),
                )
                QuoteLineItem.objects.create(
                    quote=quote,
                    label="Project fee",
                    amount=quote.chosen_total,
                    sort_order=0,
                )
    except IntegrityError:
        quote = Quote.objects.filter(
            owner=user,
            guest_conversion_nonce=nonce,
        ).first()
        if quote is None:
            guest_quote = get_guest_quote(request)
            if guest_quote is not None:
                _replace_guest_nonce(request, guest_quote)
            return None
    except DatabaseError:
        return None

    _clear_guest_state(request)
    return quote
