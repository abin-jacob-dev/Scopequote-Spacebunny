from __future__ import annotations

import hashlib
import ipaddress
import math
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import chain, islice
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone


class RateLimitExceeded(Exception):
    def __init__(self, *, retry_after: int, bucket=None):
        self.retry_after = max(1, int(retry_after))
        self.retry_after_seconds = self.retry_after
        self.bucket = bucket
        super().__init__("Too many requests. Please try again later.")


RateLimitError = RateLimitExceeded


def canonicalize_email(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.strip()).casefold()
    if not normalized:
        raise ValidationError("An email address is required.", code="required")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValidationError(
            "Email addresses cannot contain control characters.",
            code="invalid",
        )
    return normalized


def client_ip(request) -> str:
    remote_address = str(request.META.get("REMOTE_ADDR", "")).strip()
    trusted_proxies = {
        str(value).strip()
        for value in getattr(settings, "TRUSTED_PROXY_IPS", ())
        if str(value).strip()
    }
    if (
        getattr(settings, "TRUST_PROXY_HEADERS", False)
        and remote_address in trusted_proxies
    ):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        remote_address = str(forwarded).split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(remote_address))
    except ValueError:
        return "unknown"


def validate_website_url(value: str) -> None:
    if not value:
        return
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValidationError("Enter a valid website URL.", code="invalid") from exc
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme == "https" and hostname:
        return
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if settings.DEBUG and parsed.scheme == "http" and hostname in local_hosts:
        return
    raise ValidationError(
        "Use an HTTPS website URL; HTTP is allowed only for localhost in debug mode.",
        code="invalid",
    )


def hash_rate_limit_key(key: str) -> str:
    normalized = unicodedata.normalize("NFKC", key.strip()).casefold()
    if not normalized:
        raise ValueError("A rate-limit key is required.")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError("Rate-limit keys cannot contain control characters.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _aware_now() -> datetime:
    now = timezone.now()
    if timezone.is_naive(now):
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _window_start(now: datetime, window_seconds: int) -> datetime:
    epoch_seconds = int(now.timestamp())
    return datetime.fromtimestamp(
        epoch_seconds - (epoch_seconds % window_seconds),
        tz=UTC,
    )


def cleanup_expired_rate_limit_buckets(
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    from accounts.models import RateLimitBucket

    current = _aware_now() if now is None else now
    if timezone.is_naive(current):
        current = current.replace(tzinfo=UTC)
    queryset = RateLimitBucket.objects.filter(expires_at__lte=current)
    if limit is not None:
        bucket_ids = list(queryset.values_list("pk", flat=True)[:limit])
        if not bucket_ids:
            return 0
        deleted, _ = RateLimitBucket.objects.filter(pk__in=bucket_ids).delete()
        return deleted
    deleted, _ = queryset.delete()
    return deleted


def enforce_rate_limit(*, key: str, limit: int, window_seconds: int) -> None:
    from accounts.models import RateLimitBucket

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("The rate limit must be a positive integer.")
    if not isinstance(window_seconds, int) or isinstance(window_seconds, bool):
        raise ValueError("The rate-limit window must be a positive integer.")
    if window_seconds < 1:
        raise ValueError("The rate-limit window must be a positive integer.")

    key_hash = hash_rate_limit_key(key)
    now = _aware_now()
    window_start = _window_start(now, window_seconds)
    expires_at = window_start + timedelta(seconds=window_seconds)

    with transaction.atomic():
        cleanup_expired_rate_limit_buckets(now=now, limit=1000)
        bucket, created = RateLimitBucket.objects.get_or_create(
            key=key_hash,
            window_start=window_start,
            defaults={"counter": 1, "expires_at": expires_at},
        )
        bucket = RateLimitBucket.objects.select_for_update().get(pk=bucket.pk)
        if not created:
            bucket.counter += 1
            bucket.save(update_fields=("counter",))

        rate_error = None
        if bucket.counter > limit:
            retry_after = max(
                1,
                math.ceil((bucket.expires_at - now).total_seconds()),
            )
            rate_error = RateLimitExceeded(retry_after=retry_after, bucket=bucket)

    if rate_error is not None:
        raise rate_error


ACCOUNT_INACTIVITY_MONTHS = 24

ACCOUNT_DELETION_GRACE_DAYS = 30


def account_inactivity_cutoff(now: datetime | None = None) -> datetime:
    current = _aware_now() if now is None else _aware_now_from(now)
    month_index = current.year * 12 + current.month - 1 - ACCOUNT_INACTIVITY_MONTHS
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(current.day, monthrange(year, month)[1])
    return current.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class RestoreReplayResult:
    ready: bool
    tombstones: int = 0
    expired_sessions: int = 0
    reconciled_links: int = 0
    errors: tuple[str, ...] = ()


def record_authenticated_activity(user, *, now: datetime | None = None):
    from accounts.models import User

    if user is None or not getattr(user, "is_authenticated", False) or not user.pk:
        return None
    current = _aware_now() if now is None else _aware_now_from(now)
    User.objects.filter(pk=user.pk).update(last_activity_at=current)
    user.last_activity_at = current
    return current


def _aware_now_from(value: datetime) -> datetime:
    if timezone.is_naive(value):
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _session_keys_for_user(user_id):
    from django.contrib.sessions.backends.db import SessionStore
    from django.contrib.sessions.models import Session

    for session in Session.objects.exclude(session_key=None).iterator():
        try:
            data = SessionStore(session_key=session.session_key).load()
        except Exception:
            continue
        if str(data.get("_auth_user_id", "")) == str(user_id):
            yield session.session_key


def _session_data(session_key: str):
    from django.contrib.sessions.backends.db import SessionStore

    try:
        return SessionStore(session_key=session_key).load()
    except Exception:
        return {}


def _guest_boundary_expired(data, *, current: datetime) -> bool:
    if not isinstance(data, dict):
        return False
    boundary = data.get("_scopequote.guest_created_at")
    guest_keys = {
        "scopequote.guest_estimate",
        "scopequote.guest_quote",
        "scopequote.conversion_nonce",
    }
    if not boundary or not guest_keys.intersection(data):
        return False
    try:
        created_at = datetime.fromisoformat(str(boundary))
    except (TypeError, ValueError):
        return False
    if timezone.is_naive(created_at):
        created_at = created_at.replace(tzinfo=UTC)
    from quotes.services.guest import GUEST_SESSION_AGE

    return created_at.astimezone(UTC) + GUEST_SESSION_AGE <= current


def expired_guest_session_keys(
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[str]:
    from django.contrib.sessions.models import Session

    current = _aware_now() if now is None else _aware_now_from(now)
    keys = []
    for session in Session.objects.exclude(session_key=None).iterator():
        if session.expire_date <= current or _guest_boundary_expired(
            _session_data(session.session_key),
            current=current,
        ):
            keys.append(session.session_key)
            if limit is not None and len(keys) >= limit:
                break
    return keys


def revoke_user_sessions(user, *, now: datetime | None = None) -> int:
    from django.contrib.sessions.models import Session

    if user is None or not user.pk:
        return 0
    deleted = 0
    batch_size = getattr(settings, "CLEANUP_BATCH_SIZE", 1000)
    for keys in _batched(_session_keys_for_user(user.pk), batch_size):
        with transaction.atomic():
            deleted += Session.objects.filter(session_key__in=keys).delete()[0]
    return deleted


def _normalize_deletion_source(source) -> str:
    from accounts.models import DeletionSource

    value = str(source or DeletionSource.OWNER)
    aliases = {
        "owner_request": DeletionSource.OWNER,
        "retention_job": DeletionSource.RETENTION,
        "administrator_action": DeletionSource.ADMINISTRATOR,
    }
    return aliases.get(value, value)


def _link_event(link, event_type):
    from quotes.models import QuoteEvent

    return (
        QuoteEvent.objects.filter(
            share_link=link,
            event_type=event_type,
        )
        .order_by("event_time", "pk")
        .first()
    )


def _append_link_event(link, event_type, event_time) -> bool:
    from quotes.models import QuoteEvent

    if _link_event(link, event_type) is not None:
        return False
    try:
        with transaction.atomic():
            QuoteEvent.objects.create(
                quote_id=link.quote_version.quote_id,
                quote_version_id=link.quote_version_id,
                share_link=link,
                event_type=event_type,
                event_time=event_time,
            )
    except IntegrityError:
        return False
    return True


def _reconcile_link_state(link, *, now: datetime, force_revoke_at=None) -> bool:
    from quotes.models import QuoteEvent

    changed = False
    revoked_event = _link_event(link, QuoteEvent.EventType.REVOKED)
    expired_event = _link_event(link, QuoteEvent.EventType.EXPIRED)
    if revoked_event is not None:
        if link.revoked_at != revoked_event.event_time:
            link.revoked_at = revoked_event.event_time
            changed = True
    elif link.revoked_at is not None or force_revoke_at is not None:
        revoked_at = link.revoked_at or force_revoke_at or now
        if link.revoked_at != revoked_at:
            link.revoked_at = revoked_at
            changed = True
        _append_link_event(link, QuoteEvent.EventType.REVOKED, revoked_at)
        changed = True

    if expired_event is not None:
        if link.expires_at != expired_event.event_time:
            link.expires_at = expired_event.event_time
            changed = True
    elif link.expires_at <= now:
        _append_link_event(link, QuoteEvent.EventType.EXPIRED, link.expires_at)
        changed = True

    if changed:
        update_fields = []
        if revoked_event is not None or link.revoked_at is not None:
            update_fields.append("revoked_at")
        if expired_event is not None or link.expires_at <= now:
            update_fields.append("expires_at")
        if update_fields:
            link.save(update_fields=tuple(update_fields))
    return changed


def request_account_deletion(
    *,
    user,
    source="owner",
    now: datetime | None = None,
):
    from accounts.models import DeletionTombstone, User
    from quotes.models import ShareLink

    if user is None or not getattr(user, "is_authenticated", False) or not user.pk:
        raise ValueError("An authenticated account is required.")
    current = _aware_now() if now is None else _aware_now_from(now)
    source_value = _normalize_deletion_source(source)
    with transaction.atomic():
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        tombstone, _ = DeletionTombstone.objects.get_or_create(
            account_id=locked_user.pk,
            defaults={
                "deleted_at": current,
                "source": source_value,
                "quarantined": True,
            },
        )
        tombstone.quarantined = True
        tombstone.save(update_fields=("quarantined",))
        locked_user.is_active = False
        locked_user.is_quarantined = True
        locked_user.save(update_fields=("is_active", "is_quarantined"))
        links = (
            ShareLink.objects.select_for_update()
            .select_related("quote_version__quote")
            .filter(quote_version__quote__owner_id=locked_user.pk)
            .iterator()
        )
        for link in links:
            _reconcile_link_state(
                link, now=current, force_revoke_at=tombstone.deleted_at
            )
        revoke_user_sessions(locked_user, now=current)
    return tombstone


def delete_account(*, user, source="owner", now: datetime | None = None):
    return request_account_deletion(user=user, source=source, now=now)


def schedule_account_deletion(*, user, source="owner", now: datetime | None = None):
    return request_account_deletion(user=user, source=source, now=now)


def _batched(values, size: int):
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("The cleanup batch size must be a positive integer.")
    iterator = iter(values)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            return
        yield batch


def _delete_quote_batch(account_id, quote_ids) -> None:
    from quotes.models import Quote, QuoteVersion
    from quotes.services.quotes import delete_quote_tree
    from quotes.services.storage import delete_object

    ids = list(quote_ids)
    storage_keys = list(
        Quote.objects.filter(owner_id=account_id, pk__in=ids)
        .exclude(logo_object_key="")
        .values_list("logo_object_key", flat=True)
    )
    storage_keys.extend(
        QuoteVersion.objects.filter(quote_id__in=ids)
        .exclude(logo_object_key="")
        .values_list("logo_object_key", flat=True)
    )
    with transaction.atomic():
        for quote_id in ids:
            quote = Quote.objects.get(pk=quote_id, owner_id=account_id)
            delete_quote_tree(quote)
    for storage_key in storage_keys:
        delete_object(storage_key)


def hard_delete_account(*, account_id, batch_size: int | None = None) -> int:
    from accounts.models import DeletionTombstone, Profile, User
    from quotes.models import Quote
    from quotes.services.storage import delete_object, profile_storage_keys

    current_batch_size = (
        getattr(settings, "CLEANUP_BATCH_SIZE", 1000)
        if batch_size is None
        else batch_size
    )
    if (
        not isinstance(current_batch_size, int)
        or isinstance(current_batch_size, bool)
        or current_batch_size < 1
    ):
        raise ValueError("The cleanup batch size must be a positive integer.")
    if User.objects.filter(pk=account_id).exists() is False:
        return 0
    quote_ids = (
        Quote.objects.filter(owner_id=account_id)
        .order_by("pk")
        .values_list("pk", flat=True)
        .iterator()
    )
    for batch in _batched(quote_ids, current_batch_size):
        _delete_quote_batch(account_id, batch)
    profile_logo_key = (
        Profile.objects.filter(user_id=account_id)
        .exclude(logo="")
        .values_list("logo", flat=True)
        .first()
        or ""
    )
    with transaction.atomic():
        User.objects.filter(pk=account_id).delete()
        tombstone = DeletionTombstone.objects.filter(account_id=account_id).first()
        if tombstone is not None:
            tombstone.quarantined = True
            tombstone.restore_status = "applied"
            tombstone.restore_applied_at = timezone.now()
            tombstone.save(
                update_fields=(
                    "quarantined",
                    "restore_status",
                    "restore_applied_at",
                )
            )
    profile_keys = profile_storage_keys(account_id)
    if profile_logo_key:
        profile_keys = chain((profile_logo_key,), profile_keys)
    for batch in _batched(profile_keys, current_batch_size):
        with transaction.atomic():
            for storage_key in batch:
                delete_object(storage_key)
    return 1


def _reconcile_quote_statuses(*, current: datetime) -> int:
    from quotes.models import Quote, QuoteEvent

    response_rows = (
        QuoteEvent.objects.filter(
            event_type__in=(
                QuoteEvent.EventType.ACCEPTED,
                QuoteEvent.EventType.CHANGES_REQUESTED,
            )
        )
        .order_by("quote_id", "event_time", "pk")
        .values_list("quote_id", "event_type", "event_time")
    )
    reconciled = 0
    batch_size = getattr(settings, "CLEANUP_BATCH_SIZE", 1000)
    previous_quote_id = None
    batch = []

    def process_batch() -> None:
        nonlocal reconciled
        with transaction.atomic():
            for quote_id, event_type, event_time in batch:
                quote = Quote.objects.select_for_update().filter(pk=quote_id).first()
                if quote is None:
                    continue
                expected = (
                    Quote.Status.ACCEPTED
                    if event_type == QuoteEvent.EventType.ACCEPTED
                    else Quote.Status.CHANGES_REQUESTED
                )
                if quote.status != expected or (
                    expected == Quote.Status.ACCEPTED
                    and quote.accepted_at != event_time
                ):
                    models.QuerySet.update(
                        Quote.objects.filter(pk=quote_id),
                        status=expected,
                        accepted_at=event_time
                        if expected == Quote.Status.ACCEPTED
                        else quote.accepted_at,
                    )
                    reconciled += 1

    for quote_id, event_type, event_time in response_rows.iterator():
        if quote_id == previous_quote_id:
            continue
        previous_quote_id = quote_id
        batch.append((quote_id, event_type, event_time))
        if len(batch) >= batch_size:
            process_batch()
            batch = []
    if batch:
        process_batch()
    return reconciled


def restore_reconciliation_pending(
    *,
    now: datetime | None = None,
    include_expired_sessions: bool = True,
    include_unreplayed_expiry: bool = True,
) -> bool:
    from accounts.models import DeletionTombstone, User
    from quotes.models import QuoteEvent, ShareLink

    current = _aware_now() if now is None else _aware_now_from(now)
    for tombstone in DeletionTombstone.objects.iterator():
        if tombstone.restore_status != "applied":
            return True
        user = User.objects.filter(pk=tombstone.account_id).first()
        if user is not None and (user.is_active or not user.is_quarantined):
            return True
        if next(_session_keys_for_user(tombstone.account_id), None) is not None:
            return True
    if include_expired_sessions and expired_guest_session_keys(now=current):
        return True
    for link in ShareLink.objects.select_related("quote_version__quote").iterator():
        revoked_event = _link_event(link, QuoteEvent.EventType.REVOKED)
        expired_event = _link_event(link, QuoteEvent.EventType.EXPIRED)
        owner_is_tombstoned = DeletionTombstone.objects.filter(
            account_id=link.quote_version.quote.owner_id
        ).exists()
        if owner_is_tombstoned and link.revoked_at is None:
            return True
        if revoked_event is not None and link.revoked_at != revoked_event.event_time:
            return True
        if link.revoked_at is not None and revoked_event is None:
            return True
        if expired_event is not None and link.expires_at != expired_event.event_time:
            return True
        if (
            include_unreplayed_expiry
            and expired_event is None
            and link.expires_at <= current
        ):
            return True
    from quotes.models import Quote, QuoteEvent

    for quote_id, event_type in QuoteEvent.objects.filter(
        event_type__in=(
            QuoteEvent.EventType.ACCEPTED,
            QuoteEvent.EventType.CHANGES_REQUESTED,
        )
    ).values_list("quote_id", "event_type"):
        quote = Quote.objects.filter(pk=quote_id).only("status").first()
        if quote is None:
            continue
        expected = (
            Quote.Status.ACCEPTED
            if event_type == QuoteEvent.EventType.ACCEPTED
            else Quote.Status.CHANGES_REQUESTED
        )
        if quote.status != expected:
            return True
    return False


def replay_restore_state(*, now: datetime | None = None) -> RestoreReplayResult:
    from django.contrib.sessions.models import Session

    from accounts.models import DeletionTombstone, User
    from quotes.models import ShareLink

    current = _aware_now() if now is None else _aware_now_from(now)
    batch_size = getattr(settings, "CLEANUP_BATCH_SIZE", 1000)
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < 1
    ):
        return RestoreReplayResult(
            ready=False,
            errors=("Restore reconciliation could not be completed.",),
        )
    tombstone_count = 0
    expired_sessions = 0
    reconciled_links = 0
    try:
        tombstones = DeletionTombstone.objects.order_by(
            "deleted_at", "account_id"
        ).iterator()
        for tombstone_batch in _batched(tombstones, batch_size):
            tombstone_count += len(tombstone_batch)
            tombstone_times = {
                tombstone.account_id: tombstone.deleted_at
                for tombstone in tombstone_batch
            }
            with transaction.atomic():
                for account_id in tombstone_times:
                    User.objects.filter(pk=account_id).update(
                        is_active=False,
                        is_quarantined=True,
                    )
                    revoke_user_sessions(User(pk=account_id), now=current)
                for tombstone in tombstone_batch:
                    updates = ["quarantined"]
                    if (
                        tombstone.restore_status != "applied"
                        or tombstone.restore_applied_at is None
                    ):
                        tombstone.restore_status = "applied"
                        tombstone.restore_applied_at = current
                        updates.extend(("restore_status", "restore_applied_at"))
                    tombstone.quarantined = True
                    tombstone.save(update_fields=tuple(updates))
        links = (
            ShareLink.objects.select_related("quote_version__quote")
            .order_by("pk")
            .iterator()
        )
        for link_batch in _batched(links, batch_size):
            with transaction.atomic():
                locked_links = (
                    ShareLink.objects.select_for_update()
                    .select_related("quote_version__quote")
                    .filter(pk__in=[link.pk for link in link_batch])
                    .iterator()
                )
                for link in locked_links:
                    tombstone = (
                        DeletionTombstone.objects.filter(
                            account_id=link.quote_version.quote.owner_id
                        )
                        .values_list("deleted_at", flat=True)
                        .first()
                    )
                    if _reconcile_link_state(
                        link,
                        now=current,
                        force_revoke_at=tombstone,
                    ):
                        reconciled_links += 1
        while True:
            session_keys = expired_guest_session_keys(
                now=current,
                limit=batch_size,
            )
            if not session_keys:
                break
            with transaction.atomic():
                expired_sessions += Session.objects.filter(
                    session_key__in=session_keys
                ).delete()[0]
        _reconcile_quote_statuses(current=current)
    except Exception:
        return RestoreReplayResult(
            ready=False,
            errors=("Restore reconciliation could not be completed.",),
        )
    return RestoreReplayResult(
        ready=not restore_reconciliation_pending(now=current),
        tombstones=tombstone_count,
        expired_sessions=expired_sessions,
        reconciled_links=reconciled_links,
    )


replay_restore = replay_restore_state
apply_restore_replay = replay_restore_state
reconcile_restore_state = replay_restore_state
restore_replay_state = replay_restore_state
