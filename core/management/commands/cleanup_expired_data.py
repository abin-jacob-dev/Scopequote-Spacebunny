from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import (
    DeletionTombstone,
    MediaCleanupTask,
    RateLimitBucket,
    RestoreApplicationState,
    User,
)
from accounts.services import (
    ACCOUNT_DELETION_GRACE_DAYS,
    account_inactivity_cutoff,
    expired_guest_session_keys,
    hard_delete_account,
    replay_restore_state,
    request_account_deletion,
    restore_reconciliation_pending,
)
from quotes.models import QuoteEvent, ShareLink
from quotes.services.storage import (
    abandoned_profile_logo_keys,
    abandoned_staged_logo_keys,
    cleanup_abandoned_profile_logos,
    cleanup_abandoned_staged_logos,
    retry_media_cleanup_tasks,
)


class Command(BaseCommand):
    help = "Remove expired guest, rate-limit, link, media, and inactive account data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Report eligible records without deleting them.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            dest="apply",
            help="Apply the bounded cleanup work.",
        )
        parser.add_argument(
            "--now",
            dest="now",
            help="UTC timestamp used for deterministic cleanup.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=getattr(settings, "CLEANUP_BATCH_SIZE", 1000),
            dest="batch_size",
            help="Maximum records processed per cleanup batch.",
        )
        parser.add_argument(
            "--restore",
            action="store_true",
            dest="restore",
            help="Replay tombstones, sessions, and link state after a restore.",
        )
        parser.add_argument(
            "--restore-replay",
            action="store_true",
            dest="restore",
            help="Alias for --restore.",
        )
        parser.add_argument(
            "--restore-check",
            action="store_true",
            dest="restore_check",
            help="Fail until restore reconciliation is complete.",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            dest="restore_check",
            help="Alias for --restore-check.",
        )

    def _now(self, value) -> datetime:
        if value is None or value == "":
            return timezone.now().astimezone(UTC)
        if isinstance(value, datetime):
            current = value
        else:
            try:
                current = datetime.fromisoformat(str(value))
            except ValueError as exc:
                raise CommandError("--now must be an ISO timestamp.") from exc
        return (
            current.astimezone(UTC)
            if timezone.is_aware(current)
            else current.replace(tzinfo=UTC)
        )

    def _counts(self, now: datetime, batch_size: int) -> dict[str, int]:
        cutoff = account_inactivity_cutoff(now)
        deletion_cutoff = now - timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
        inactive = User.objects.filter(
            Q(last_activity_at__lte=cutoff)
            | Q(last_activity_at__isnull=True, date_joined__lte=cutoff),
            is_quarantined=False,
            is_superuser=False,
        )
        return {
            "guest sessions": len(
                expired_guest_session_keys(now=now, limit=batch_size)
            ),
            "abandoned uploads": len(
                abandoned_staged_logo_keys(now=now, limit=batch_size)
            ),
            "abandoned profile uploads": len(
                abandoned_profile_logo_keys(now=now, limit=batch_size)
            ),
            "rate-limit buckets": min(
                RateLimitBucket.objects.filter(expires_at__lte=now).count(),
                batch_size,
            ),
            "expired links": min(
                ShareLink.objects.filter(
                    expires_at__lte=now,
                    revoked_at__isnull=True,
                )
                .exclude(
                    events__event_type=QuoteEvent.EventType.EXPIRED,
                )
                .count(),
                batch_size,
            ),
            "inactive accounts": min(inactive.count(), batch_size),
            "scheduled account deletions": min(
                DeletionTombstone.objects.filter(
                    deleted_at__lte=deletion_cutoff,
                )
                .exclude(restore_status=RestoreApplicationState.APPLIED)
                .count(),
                batch_size,
            ),
            "media cleanup tasks": min(
                MediaCleanupTask.objects.filter(next_attempt_at__lte=now).count(),
                batch_size,
            ),
        }

    def _expire_links(self, now: datetime, batch_size: int) -> int:
        processed = 0
        while processed < batch_size:
            link_ids = list(
                ShareLink.objects.filter(expires_at__lte=now)
                .exclude(events__event_type=QuoteEvent.EventType.EXPIRED)
                .values_list("pk", flat=True)[: batch_size - processed]
            )
            if not link_ids:
                break
            with transaction.atomic():
                for link_id in link_ids:
                    link = (
                        ShareLink.objects.select_for_update()
                        .select_related("quote_version__quote")
                        .get(pk=link_id)
                    )
                    if QuoteEvent.objects.filter(
                        share_link=link,
                        event_type=QuoteEvent.EventType.EXPIRED,
                    ).exists():
                        continue
                    try:
                        with transaction.atomic():
                            QuoteEvent.objects.create(
                                quote_id=link.quote_version.quote_id,
                                quote_version_id=link.quote_version_id,
                                share_link=link,
                                event_type=QuoteEvent.EventType.EXPIRED,
                                event_time=link.expires_at,
                            )
                    except IntegrityError:
                        continue
                    processed += 1
        return processed

    def _delete_inactive_accounts(self, now: datetime, batch_size: int) -> int:
        cutoff = account_inactivity_cutoff(now)
        accounts = User.objects.filter(
            Q(last_activity_at__lte=cutoff)
            | Q(last_activity_at__isnull=True, date_joined__lte=cutoff),
            is_quarantined=False,
            is_superuser=False,
        ).values_list("pk", flat=True)[:batch_size]
        deleted = 0
        for account_id in accounts.iterator():
            account = User.objects.get(pk=account_id)
            request_account_deletion(user=account, source="retention", now=now)
            deleted += hard_delete_account(
                account_id=account.pk,
                batch_size=batch_size,
            )
        return deleted

    def _delete_scheduled_accounts(self, now: datetime, batch_size: int) -> int:
        cutoff = now - timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
        deleted = 0
        account_ids = list(
            DeletionTombstone.objects.filter(
                deleted_at__lte=cutoff,
            )
            .exclude(restore_status=RestoreApplicationState.APPLIED)
            .values_list("account_id", flat=True)[:batch_size]
        )
        for account_id in account_ids:
            deleted += hard_delete_account(
                account_id=account_id,
                batch_size=batch_size,
            )
        return deleted

    def _apply(self, now: datetime, batch_size: int) -> dict[str, int]:
        session_keys = expired_guest_session_keys(now=now, limit=batch_size)
        with transaction.atomic():
            deleted_sessions = Session.objects.filter(
                session_key__in=session_keys
            ).delete()[0]
        deleted_uploads = cleanup_abandoned_staged_logos(now=now, limit=batch_size)
        deleted_profile_uploads = cleanup_abandoned_profile_logos(
            now=now, limit=batch_size
        )
        from accounts.services import cleanup_expired_rate_limit_buckets

        deleted_buckets = min(
            cleanup_expired_rate_limit_buckets(now=now, limit=batch_size),
            batch_size,
        )
        expired_links = self._expire_links(now, batch_size)
        deleted_inactive = self._delete_inactive_accounts(now, batch_size)
        deleted_scheduled = self._delete_scheduled_accounts(now, batch_size)
        retried_media = retry_media_cleanup_tasks(now=now, limit=batch_size)
        return {
            "guest sessions": deleted_sessions,
            "abandoned uploads": deleted_uploads,
            "abandoned profile uploads": deleted_profile_uploads,
            "rate-limit buckets": deleted_buckets,
            "expired links": expired_links,
            "inactive accounts": deleted_inactive,
            "scheduled account deletions": deleted_scheduled,
            "media cleanup tasks": retried_media,
        }

    def handle(self, *args, **options):
        if options.get("apply") and options.get("dry_run"):
            raise CommandError("Choose either --dry-run or --apply.")
        if options.get("restore") and options.get("restore_check"):
            raise CommandError("Choose either --restore or --restore-check.")
        now = self._now(options.get("now"))
        batch_size = options.get("batch_size")
        if not isinstance(batch_size, int) or batch_size < 1:
            raise CommandError("--batch-size must be a positive integer.")
        if options.get("restore_check"):
            if restore_reconciliation_pending(now=now):
                raise CommandError("Restore reconciliation is incomplete.")
            self.stdout.write(self.style.SUCCESS("Restore reconciliation is complete."))
            return
        if (
            not options.get("restore")
            and not options.get("apply")
            and restore_reconciliation_pending(
                now=now,
                include_expired_sessions=False,
            )
        ):
            raise CommandError("Restore reconciliation is incomplete.")
        if options.get("restore"):
            result = replay_restore_state(now=now)
            if not result.ready:
                raise CommandError("Restore reconciliation is incomplete.")
            self.stdout.write(
                self.style.SUCCESS(
                    "Restore replay complete: "
                    f"tombstones={result.tombstones}, "
                    f"sessions={result.expired_sessions}, "
                    f"links={result.reconciled_links}."
                )
            )
        counts = self._counts(now, batch_size)
        if not options.get("apply"):
            for label, count in counts.items():
                self.stdout.write(f"{label}: {count}")
            self.stdout.write("dry-run: no records were deleted.")
            return
        applied = self._apply(now, batch_size)
        if restore_reconciliation_pending(
            now=now,
            include_expired_sessions=False,
            include_unreplayed_expiry=False,
        ):
            raise CommandError("Restore reconciliation is incomplete.")
        for label, count in applied.items():
            self.stdout.write(f"{label}: {count}")
        self.stdout.write(self.style.SUCCESS("Cleanup apply completed."))
