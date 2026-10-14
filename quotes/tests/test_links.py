import base64
import hashlib
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import (
    IntegrityError,
    close_old_connections,
    connections,
    models,
    transaction,
)
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from accounts.models import User
from pricing.constants import PRICING_CONFIG_VERSION
from quotes.models import Quote, QuoteEvent, QuoteLineItem, QuoteVersion, ShareLink
from quotes.services.links import (
    LinkLifecycleError,
    create_share_link,
    decrypt_link_token,
    reissue_share_link,
    resolve_share_link,
    revoke_share_link,
)
from quotes.services.responses import record_response
from quotes.services.snapshots import build_quote_snapshot
from quotes.services.tokens import (
    TokenDecryptionError,
    build_associated_data,
    decrypt_token,
    generate_token,
    hash_token,
)

KEY_V1 = base64.b64encode(b"v1" + b"a" * 30).decode()
KEY_V2 = base64.b64encode(b"v2" + b"b" * 30).decode()


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=KEY_V1,
    SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1",
)
class TokenPrimitiveTests(SimpleTestCase):
    def test_generate_token_has_url_safe_256_bit_entropy(self):
        token = generate_token()

        self.assertEqual(len(token), 43)
        self.assertRegex(token, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(len(base64.urlsafe_b64decode(token + "=")), 32)

    def test_generate_token_uses_32_random_bytes(self):
        with patch(
            "quotes.services.tokens.secrets.token_urlsafe",
            return_value="a" * 43,
        ) as token_urlsafe:
            self.assertEqual(generate_token(), "a" * 43)

        token_urlsafe.assert_called_once_with(32)

    def test_hash_token_is_sha256_hex_digest(self):
        token = generate_token()

        self.assertEqual(hash_token(token), hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(len(hash_token(token)), 64)
        self.assertNotEqual(hash_token(token), hash_token(token + "x"))

    def test_encrypt_token_round_trips_with_aad(self):
        token = generate_token()
        link_id = uuid.uuid4()
        version_id = uuid.uuid4()

        encrypted = decrypt_token(
            _encrypt_for_test(token, link_id, version_id),
            share_link_id=link_id,
            quote_version_id=version_id,
            key_version="v1",
        )

        self.assertEqual(encrypted, token)
        self.assertNotIn(token.encode(), _encrypt_for_test(token, link_id, version_id))

    def test_associated_data_contains_both_identifiers(self):
        link_id = uuid.uuid4()
        version_id = uuid.uuid4()

        associated_data = build_associated_data(link_id, version_id)

        self.assertIn(str(link_id), associated_data)
        self.assertIn(str(version_id), associated_data)
        self.assertEqual(
            associated_data,
            build_associated_data(link_id, version_id),
        )

    def test_associated_data_mismatch_fails_without_exposing_token(self):
        token = generate_token()
        link_id = uuid.uuid4()
        version_id = uuid.uuid4()
        encrypted = _encrypt_for_test(token, link_id, version_id)

        with self.assertRaises(TokenDecryptionError) as error:
            decrypt_token(
                encrypted,
                share_link_id=uuid.uuid4(),
                quote_version_id=version_id,
                key_version="v1",
            )

        self.assertNotIn(token, str(error.exception))

    @override_settings(
        SHARE_TOKEN_ENCRYPTION_KEY=KEY_V2,
        SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v2",
    )
    def test_key_version_mismatch_fails_safely(self):
        token = generate_token()
        link_id = uuid.uuid4()
        version_id = uuid.uuid4()
        encrypted = _encrypt_for_test(token, link_id, version_id)

        with self.assertRaises(TokenDecryptionError):
            decrypt_token(
                encrypted,
                share_link_id=link_id,
                quote_version_id=version_id,
                key_version="v1",
            )

    def test_token_hash_and_associated_data_do_not_log(self):
        token = generate_token()
        with self.assertNoLogs("quotes.services", level=logging.DEBUG):
            hash_token(token)
            build_associated_data(uuid.uuid4(), uuid.uuid4())


def _encrypt_for_test(token, share_link_id, quote_version_id):
    from quotes.services.tokens import encrypt_token

    return encrypt_token(
        token,
        share_link_id=share_link_id,
        quote_version_id=quote_version_id,
    )


class LinkServiceTestCase(TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        self.owner = User.objects.create_user(
            "link-owner@example.com",
            password="correct horse battery staple",
        )
        self.quote = self.make_published_quote("SQ-2026-0901")
        self.version = self.make_version(self.quote, 1)

    def make_published_quote(self, quote_number, *, valid_until=date(2026, 10, 1)):
        quote = Quote.objects.create(
            owner=self.owner,
            quote_number=quote_number,
            project_title="A published project",
            project_description="A complete project overview for publication.",
            category="website",
            delivery_days=5,
            complexity="standard",
            revision_rounds=0,
            urgency="standard",
            experience="intermediate",
            currency="USD",
            pricing_config_version=PRICING_CONFIG_VERSION,
            effective_hours=Decimal("20.00"),
            minimum=Decimal("100.00"),
            recommended=Decimal("100.00"),
            maximum=Decimal("100.00"),
            chosen_total=Decimal("100.00"),
            hourly_equivalent=Decimal("5.00"),
            client_name="Client Name",
            client_email="client@example.com",
            sender_name="Sender Name",
            sender_email="sender@example.com",
            issue_date=date(2026, 9, 24),
            valid_until=valid_until,
            deposit_percentage=50,
            terms="Payment follows the agreed schedule.",
        )
        QuoteLineItem.objects.create(
            quote=quote,
            label="Project fee",
            amount=Decimal("100.00"),
            sort_order=0,
        )
        quote.status = Quote.Status.SENT
        quote.sent_at = self.now
        quote.save()
        return quote

    def make_version(self, quote, version_number):
        return QuoteVersion.objects.create(
            quote=quote,
            version_number=version_number,
            snapshot=build_quote_snapshot(quote),
            frozen_total=quote.chosen_total,
            currency=quote.currency,
        )

    def create_link(self, now=None):
        return create_share_link(version=self.version, now=now or self.now)

    def test_create_link_stores_hashed_and_encrypted_token_with_capped_expiry(self):
        link = self.create_link()
        raw_token = decrypt_link_token(link)
        expected_expiry = datetime.combine(
            self.quote.valid_until,
            time.max,
            tzinfo=UTC,
        )

        self.assertEqual(link.quote_version_id, self.version.pk)
        self.assertEqual(link.created_by_id, self.owner.pk)
        self.assertEqual(link.encryption_key_version, "v1")
        self.assertEqual(
            link.associated_data, build_associated_data(link.pk, self.version.pk)
        )
        self.assertEqual(link.token_hash, hash_token(raw_token))
        self.assertEqual(link.expires_at, expected_expiry)
        self.assertNotIn(raw_token.encode(), link.encrypted_token)
        self.assertEqual(resolve_share_link(token=raw_token, now=self.now), link)

    def test_create_link_caps_expiry_at_ninety_days(self):
        quote = self.make_published_quote("SQ-2026-0903")
        models.QuerySet.update(
            Quote.objects.filter(pk=quote.pk), valid_until=date(2027, 6, 1)
        )
        quote.refresh_from_db()
        version = self.make_version(quote, 1)
        link = create_share_link(version=version, now=self.now)

        self.assertEqual(link.expires_at, self.now + timedelta(days=90))

    def test_resolve_rejects_unknown_expired_and_revoked_tokens(self):
        link = self.create_link()
        raw_token = decrypt_link_token(link)

        self.assertIsNone(resolve_share_link(token="unknown", now=self.now))
        self.assertIsNone(resolve_share_link(token="\ud800", now=self.now))
        ShareLink.objects.filter(pk=link.pk).update(
            expires_at=self.now - timedelta(seconds=1),
        )
        self.assertIsNone(resolve_share_link(token=raw_token, now=self.now))
        ShareLink.objects.filter(pk=link.pk).update(
            expires_at=self.now + timedelta(days=1),
            revoked_at=self.now,
        )
        self.assertIsNone(resolve_share_link(token=raw_token, now=self.now))

    def test_recopy_returns_token_but_key_rotation_keeps_link_valid(self):
        link = self.create_link()
        raw_token = decrypt_link_token(link)

        with override_settings(
            SHARE_TOKEN_ENCRYPTION_KEY=KEY_V2,
            SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v2",
        ):
            with self.assertRaises(TokenDecryptionError):
                decrypt_link_token(link)

        link.refresh_from_db()
        self.assertTrue(link.is_active)
        self.assertIsNotNone(resolve_share_link(token=raw_token, now=self.now))

    def test_reissue_revokes_old_link_and_returns_new_token(self):
        old_link = self.create_link()
        old_token = decrypt_link_token(old_link)

        new_link = reissue_share_link(link=old_link, now=self.now)
        new_token = decrypt_link_token(new_link)

        self.assertNotEqual(new_link.pk, old_link.pk)
        self.assertEqual(new_link.quote_version_id, old_link.quote_version_id)
        self.assertNotEqual(new_link.token_hash, old_link.token_hash)
        self.assertNotEqual(new_link.encrypted_token, old_link.encrypted_token)
        old_link.refresh_from_db()
        self.assertEqual(old_link.revoked_at, self.now)
        self.assertEqual(old_link.state, "revoked")
        self.assertIsNone(resolve_share_link(token=old_token, now=self.now))
        self.assertEqual(resolve_share_link(token=new_token, now=self.now), new_link)

    def test_explicit_revocation_is_idempotent_and_does_not_rebind_version(self):
        link = self.create_link()

        revoke_share_link(link=link, now=self.now)
        revoke_share_link(link=link, now=self.now + timedelta(minutes=1))

        link.refresh_from_db()
        self.assertEqual(link.revoked_at, self.now)
        self.assertEqual(link.quote_version_id, self.version.pk)
        self.assertEqual(link.state, "revoked")

    def test_expiry_event_is_unique_for_a_link(self):
        link = self.create_link()
        QuoteEvent.objects.create(
            quote=self.quote,
            quote_version=self.version,
            share_link=link,
            event_type=QuoteEvent.EventType.EXPIRED,
            event_time=self.now,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuoteEvent.objects.create(
                quote=self.quote,
                quote_version=self.version,
                share_link=link,
                event_type=QuoteEvent.EventType.EXPIRED,
                event_time=self.now,
            )

        link = self.create_link()
        raw_token = decrypt_link_token(link)
        with self.assertNoLogs("quotes.services", level=logging.DEBUG):
            resolve_share_link(token=raw_token, now=self.now)
            resolve_share_link(token=raw_token + "tampered", now=self.now)

    def test_link_state_prioritizes_explicit_revocation_over_expiry(self):
        link = self.create_link()
        real_now = datetime.now(UTC)
        ShareLink.objects.filter(pk=link.pk).update(
            expires_at=real_now - timedelta(seconds=1),
            revoked_at=real_now,
        )
        link.refresh_from_db()

        self.assertEqual(link.state, "revoked")
        self.assertFalse(link.is_active)
        self.assertTrue(link.is_revoked)
        self.assertTrue(link.is_expired)


class ResponseServiceTestCase(LinkServiceTestCase):
    def response(self, link, *, key=None, action="accepted", note="", now=None):
        return record_response(
            link=link,
            action=action,
            note=note,
            idempotency_key=key or uuid.uuid4(),
            now=now or self.now,
        )

    def test_first_accepted_response_is_recorded_and_revokes_all_active_links(self):
        first_link = self.create_link()
        second_link = self.create_link()
        key = uuid.uuid4()

        result = self.response(
            first_link, key=key, action="accepted", note="Looks good"
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.status, "accepted")
        self.assertIsNone(result.existing_response)
        event = QuoteEvent.objects.get(response_idempotency_key=key)
        self.assertEqual(event.event_type, QuoteEvent.EventType.ACCEPTED)
        self.assertEqual(event.note, "Looks good")
        self.assertEqual(event.quote_version_id, self.version.pk)
        self.assertEqual(event.share_link_id, first_link.pk)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
        self.assertEqual(self.quote.accepted_at, self.now)
        for link in (first_link, second_link):
            link.refresh_from_db()
            self.assertIsNotNone(link.revoked_at)
            self.assertEqual(link.state, "revoked")

    def test_first_changes_requested_response_sets_terminal_quote_status(self):
        link = self.create_link()

        result = self.response(
            link,
            action="changes_requested",
            note="Please adjust the scope.",
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.status, "changes_requested")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.CHANGES_REQUESTED)
        self.assertEqual(
            QuoteEvent.objects.get(
                event_type=QuoteEvent.EventType.CHANGES_REQUESTED
            ).note,
            "Please adjust the scope.",
        )

    def test_same_key_same_payload_after_revocation_returns_existing_response(self):
        link = self.create_link()
        key = uuid.uuid4()
        first = self.response(link, key=key, note="  Ready  ")

        ShareLink.objects.filter(pk=link.pk).update(revoked_at=self.now)
        retry = self.response(link, key=key, note="Ready")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.status, "accepted")
        self.assertIsNotNone(retry.existing_response)
        self.assertEqual(retry.existing_response.pk, first_event_pk(self, key))
        self.assertEqual(
            QuoteEvent.objects.filter(response_idempotency_key=key).count(), 1
        )

    def test_same_key_different_payload_returns_conflict(self):
        link = self.create_link()
        key = uuid.uuid4()
        self.response(link, key=key, note="Original")

        result = self.response(link, key=key, note="Changed")

        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.status, "conflict")
        self.assertIsNotNone(result.existing_response)
        self.assertEqual(
            QuoteEvent.objects.filter(response_idempotency_key=key).count(), 1
        )

    def test_different_key_after_first_response_returns_conflict(self):
        link = self.create_link()
        self.response(link, key=uuid.uuid4(), action="changes_requested")

        result = self.response(link, key=uuid.uuid4(), action="accepted")

        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(
            QuoteEvent.objects.filter(
                event_type__in=["accepted", "changes_requested"]
            ).count(),
            1,
        )

    def test_different_key_after_revocation_is_generically_unavailable(self):
        link = self.create_link()
        self.response(link, key=uuid.uuid4(), action="accepted")

        result = self.response(link, key=uuid.uuid4())

        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.existing_response)

    def test_inactive_link_without_an_existing_key_is_generically_unavailable(self):
        link = self.create_link()
        ShareLink.objects.filter(pk=link.pk).update(revoked_at=self.now)

        result = self.response(link, key=uuid.uuid4())

        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.existing_response)
        self.assertNotIn(str(link.pk), result.message)

    def test_same_key_retry_after_expiry_returns_existing_response(self):
        link = self.create_link()
        key = uuid.uuid4()
        self.response(link, key=key)
        retry = self.response(link, key=key, now=self.now + timedelta(days=8))

        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.status, "accepted")
        self.assertEqual(retry.existing_response.pk, first_event_pk(self, key))

    def test_same_key_retry_after_unrevoked_expiry_returns_existing_response(self):
        link = self.create_link()
        key = uuid.uuid4()
        first = self.response(
            link, key=key, action="changes_requested", note="Please review"
        )
        ShareLink.objects.filter(pk=link.pk).update(
            expires_at=self.now - timedelta(seconds=1),
            revoked_at=None,
        )
        retry = self.response(
            link, key=key, action="changes_requested", note="Please review"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.existing_response.pk, first_event_pk(self, key))

    def test_same_key_different_action_returns_conflict(self):
        link = self.create_link()
        key = uuid.uuid4()
        self.response(link, key=key, action="accepted")
        result = self.response(link, key=key, action="changes_requested")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.existing_response.event_type, "accepted")

    def test_response_rejects_invalid_action_and_long_note_without_writing(self):
        link = self.create_link()

        invalid_action = self.response(link, action="maybe")
        long_note = self.response(link, note="x" * 2001)

        self.assertEqual(invalid_action.status_code, 400)
        self.assertEqual(long_note.status_code, 400)
        self.assertEqual(
            QuoteEvent.objects.filter(quote_version=self.version).count(), 0
        )
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_response_does_not_return_raw_token_in_messages(self):
        link = self.create_link()
        raw_token = decrypt_link_token(link)
        result = self.response(link, key=uuid.uuid4())

        self.assertNotIn(raw_token, result.message)
        self.assertNotIn(raw_token, repr(result))


class ConcurrentResponseTestCase(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        self.owner = User.objects.create_user(
            "concurrent-owner@example.com",
            password="correct horse battery staple",
        )
        quote = Quote.objects.create(
            owner=self.owner,
            quote_number="SQ-2026-0902",
            project_title="A concurrent project",
            project_description="A complete project overview for publication.",
            category="website",
            delivery_days=5,
            complexity="standard",
            revision_rounds=0,
            urgency="standard",
            experience="intermediate",
            currency="USD",
            pricing_config_version=PRICING_CONFIG_VERSION,
            effective_hours=Decimal("20.00"),
            minimum=Decimal("100.00"),
            recommended=Decimal("100.00"),
            maximum=Decimal("100.00"),
            chosen_total=Decimal("100.00"),
            hourly_equivalent=Decimal("5.00"),
            client_name="Client Name",
            client_email="client@example.com",
            sender_name="Sender Name",
            sender_email="sender@example.com",
            issue_date=date(2026, 9, 24),
            valid_until=date(2026, 10, 1),
            deposit_percentage=50,
            terms="Payment follows the agreed schedule.",
        )
        QuoteLineItem.objects.create(
            quote=quote,
            label="Project fee",
            amount=Decimal("100.00"),
        )
        quote.status = Quote.Status.SENT
        quote.sent_at = self.now
        quote.save()
        self.quote = quote
        self.version = QuoteVersion.objects.create(
            quote=quote,
            version_number=1,
            snapshot=build_quote_snapshot(quote),
            frozen_total=Decimal("100.00"),
            currency="USD",
        )
        self.link = create_share_link(version=self.version, now=self.now)
        self.other_link = create_share_link(version=self.version, now=self.now)

    def test_concurrent_first_responses_serialize_to_one_authoritative_event(self):
        barrier = __import__("threading").Barrier(2)

        def submit(link, action):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return record_response(
                    link=link,
                    action=action,
                    note="",
                    idempotency_key=uuid.uuid4(),
                    now=self.now,
                )
            finally:
                close_old_connections()
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    submit,
                    (self.link, self.other_link),
                    ("accepted", "changes_requested"),
                ),
            )

        status_codes = [result.status_code for result in results]
        non_success_codes = [code for code in status_codes if code != 200]
        self.assertEqual(status_codes.count(200), 1)
        self.assertEqual(len(non_success_codes), 1)
        self.assertIn(non_success_codes[0], {404, 409})
        self.assertEqual(
            QuoteEvent.objects.filter(
                quote_version=self.version,
                event_type__in=["accepted", "changes_requested"],
            ).count(),
            1,
        )
        self.quote.refresh_from_db()
        self.assertIn(self.quote.status, {"accepted", "changes_requested"})

    def test_link_creation_and_acceptance_race_leaves_no_active_link(self):
        barrier = __import__("threading").Barrier(2)

        def create():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                try:
                    create_share_link(version=self.version, now=self.now)
                except LinkLifecycleError:
                    return "rejected"
                return "created"
            finally:
                close_old_connections()
                connections.close_all()

        def accept():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                result = record_response(
                    link=self.link,
                    action="accepted",
                    note="",
                    idempotency_key=uuid.uuid4(),
                    now=self.now,
                )
                return result
            finally:
                close_old_connections()
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            creation = executor.submit(create)
            acceptance = executor.submit(accept)
            creation_result = creation.result(timeout=15)
            acceptance_result = acceptance.result(timeout=15)

        self.assertIn(creation_result, {"created", "rejected"})
        self.assertEqual(acceptance_result.status_code, 200)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
        self.assertEqual(
            ShareLink.objects.filter(
                quote_version=self.version,
                revoked_at__isnull=True,
            ).count(),
            0,
        )
        self.assertEqual(
            QuoteEvent.objects.filter(
                quote_version=self.version,
                event_type=QuoteEvent.EventType.ACCEPTED,
            ).count(),
            1,
        )


def first_event_pk(test_case, key):
    return QuoteEvent.objects.get(response_idempotency_key=key).pk
