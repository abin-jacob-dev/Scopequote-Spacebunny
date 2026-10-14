import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import SimpleTestCase, TestCase

from accounts.models import User
from pricing.constants import PRICING_CONFIG_VERSION
from quotes.models import (
    Quote,
    QuoteEvent,
    QuoteLineItem,
    QuoteNumberSequence,
    QuoteVersion,
    ShareLink,
)
from quotes.services.quotes import (
    allocate_quote_number,
    calculate_deposit_amount,
    calculate_line_item_total,
    calculate_realized_hourly_equivalent,
    requires_intentional_adjustment,
)
from quotes.services.snapshots import build_quote_snapshot


class QuoteServiceTests(SimpleTestCase):
    def test_line_item_total_sums_signed_two_decimal_amounts(self):
        items = [
            QuoteLineItem(amount=Decimal("100.00")),
            QuoteLineItem(amount=Decimal("-25.50")),
            QuoteLineItem(amount=Decimal("0.005")),
        ]

        self.assertEqual(calculate_line_item_total(items), Decimal("74.51"))
        self.assertIsInstance(calculate_line_item_total(items), Decimal)

    def test_line_item_total_rejects_invalid_collections_and_totals(self):
        with self.assertRaises(ValueError):
            calculate_line_item_total([])
        with self.assertRaises(ValueError):
            calculate_line_item_total([QuoteLineItem(amount=Decimal("0.00"))])
        with self.assertRaises(ValueError):
            calculate_line_item_total(
                [
                    QuoteLineItem(amount=Decimal("-10.00")),
                    QuoteLineItem(amount=Decimal("0.00")),
                ]
            )
        with self.assertRaises(ValueError):
            calculate_line_item_total([QuoteLineItem(amount=Decimal("1.00"))] * 51)

    def test_deposit_amount_uses_half_up_two_decimal_quantization(self):
        self.assertEqual(
            calculate_deposit_amount(Decimal("123.45"), 50),
            Decimal("61.73"),
        )
        self.assertEqual(
            calculate_deposit_amount(
                chosen_total=Decimal("10.00"), deposit_percentage=0
            ),
            Decimal("0.00"),
        )

    def test_realized_hourly_equivalent_uses_chosen_total_and_effective_hours(self):
        self.assertEqual(
            calculate_realized_hourly_equivalent(Decimal("1000.00"), Decimal("20")),
            Decimal("50.00"),
        )
        with self.assertRaises(ValueError):
            calculate_realized_hourly_equivalent(Decimal("1000.00"), Decimal("0"))

    def test_intentional_adjustment_uses_displayed_fee_range(self):
        self.assertTrue(
            requires_intentional_adjustment(
                chosen_total=Decimal("118.00"),
                recommended_min=Decimal("117.50"),
                recommended_max=Decimal("132.50"),
            )
        )
        self.assertFalse(
            requires_intentional_adjustment(
                chosen_total=Decimal("120.00"),
                recommended_min=Decimal("117.50"),
                recommended_max=Decimal("132.50"),
            )
        )
        self.assertFalse(
            requires_intentional_adjustment(
                chosen_total=Decimal("135.00"),
                recommended_min=Decimal("117.50"),
                recommended_max=Decimal("132.50"),
            )
        )
        self.assertTrue(
            requires_intentional_adjustment(
                chosen_total=Decimal("136.00"),
                recommended_min=Decimal("117.50"),
                recommended_max=Decimal("132.50"),
            )
        )


class QuoteModelTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "owner@example.com",
            password="correct horse battery staple",
        )

    def make_quote(self, **overrides):
        values = {
            "owner": self.owner,
            "project_title": "A useful project",
            "project_description": (
                "A project overview with enough detail for publication."
            ),
            "category": "website",
            "delivery_days": 5,
            "complexity": "standard",
            "revision_rounds": 0,
            "urgency": "standard",
            "experience": "intermediate",
            "currency": "USD",
            "pricing_config_version": PRICING_CONFIG_VERSION,
            "effective_hours": Decimal("20.00"),
            "minimum": Decimal("1160.00"),
            "recommended": Decimal("1320.00"),
            "maximum": Decimal("1480.00"),
            "hourly_equivalent": Decimal("66.00"),
            "chosen_total": Decimal("100.00"),
            "client_name": "Client Name",
            "client_email": "client@example.com",
            "sender_name": "Sender Name",
            "sender_email": "sender@example.com",
            "issue_date": date(2026, 1, 1),
            "valid_until": date(2026, 1, 31),
            "deposit_percentage": 50,
            "timeline": "Five focused delivery days.",
            "terms": "Payment is due according to the agreed schedule.",
        }
        values.update(overrides)
        return Quote.objects.create(**values)

    def add_line_items(self, quote, *amounts):
        return [
            QuoteLineItem.objects.create(
                quote=quote,
                label=f"Item {index}",
                amount=amount,
                sort_order=index,
            )
            for index, amount in enumerate(amounts)
        ]

    def make_publishable_quote(
        self,
        *,
        quote_number="SQ-2026-0001",
        **overrides,
    ):
        quote = self.make_quote(quote_number=quote_number, **overrides)
        self.add_line_items(quote, Decimal("100.00"))
        return quote

    def publish_quote(self, quote=None):
        quote = quote or self.make_publishable_quote()
        quote.status = Quote.Status.SENT
        quote.save()
        return quote

    def make_version(self, quote, *, version_number=1):
        return QuoteVersion.objects.create(
            quote=quote,
            version_number=version_number,
            snapshot=build_quote_snapshot(quote),
            frozen_total=Decimal("100.00"),
            currency=quote.currency,
            logo_object_key=quote.logo_object_key,
        )

    def make_link(self, version, *, token_hash):
        return ShareLink.objects.create(
            quote_version=version,
            token_hash=token_hash,
            encrypted_token=b"encrypted",
            encryption_key_version="v1",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )

    def test_quote_has_uuid_key_owner_and_explicit_status_choices(self):
        quote = self.make_quote()

        self.assertIsInstance(quote.pk, uuid.UUID)
        self.assertEqual(quote.owner, self.owner)
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertEqual(
            set(Quote.Status.values),
            {"draft", "sent", "viewed", "accepted", "changes_requested"},
        )
        self.assertEqual(quote.guest_conversion_nonce, None)
        self.assertTrue(Quote._meta.get_field("owner").remote_field.related_name)

    def test_quote_number_is_server_generated_and_not_model_editable(self):
        quote_form = modelform_factory(Quote, fields="__all__")()

        self.assertFalse(Quote._meta.get_field("quote_number").editable)
        self.assertNotIn("quote_number", quote_form.fields)

    def test_new_quote_cannot_be_created_with_non_draft_status(self):
        with self.assertRaises(ValidationError):
            self.make_quote(status=Quote.Status.SENT)

    def test_draft_publish_requires_allocated_number_and_valid_fields(self):
        quote = self.make_quote()
        self.add_line_items(quote, Decimal("100.00"))
        quote.status = Quote.Status.SENT

        with self.assertRaises(ValidationError) as error:
            quote.save()
        self.assertIn("quote_number", str(error.exception))

        quote.quote_number = "SQ-2026-0001"
        quote.client_name = ""
        with self.assertRaises(ValidationError) as error:
            quote.save()
        self.assertIn("client_name", str(error.exception))

    def test_legal_publish_and_operational_status_transitions_are_allowed(self):
        quote = self.make_publishable_quote()
        quote.status = Quote.Status.SENT
        quote.save()

        quote.status = Quote.Status.VIEWED
        quote.last_viewed_at = datetime.now(UTC)
        quote.save()
        quote.status = Quote.Status.ACCEPTED
        quote.accepted_at = datetime.now(UTC)
        quote.save()

        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.ACCEPTED)

    def test_illegal_status_transitions_are_rejected(self):
        draft = self.make_publishable_quote(quote_number="SQ-2026-0002")
        draft.status = Quote.Status.VIEWED
        with self.assertRaises(ValidationError):
            draft.save()

        sent = self.publish_quote(
            self.make_publishable_quote(quote_number="SQ-2026-0003")
        )
        sent.status = Quote.Status.DRAFT
        with self.assertRaises(ValidationError):
            sent.save()

        sent.status = Quote.Status.ACCEPTED
        sent.save()
        sent.status = Quote.Status.VIEWED
        with self.assertRaises(ValidationError):
            sent.save()

    def test_owner_cannot_be_reassigned_through_instance_or_queryset(self):
        quote = self.make_quote()
        other_owner = User.objects.create_user("other-owner@example.com")
        self.assertFalse(Quote._meta.get_field("owner").editable)

        quote.owner = other_owner
        with self.assertRaises(ValidationError):
            quote.save()
        with self.assertRaises(ValidationError):
            Quote.objects.filter(pk=quote.pk).update(owner=other_owner)

        quote.refresh_from_db()
        self.assertEqual(quote.owner, self.owner)

    def test_sequence_counter_cannot_be_tampered_through_public_model_paths(self):
        sequence = QuoteNumberSequence.objects.create(
            year=2030,
            last_allocated_number=0,
        )
        self.assertFalse(QuoteNumberSequence._meta.get_field("year").editable)
        self.assertFalse(
            QuoteNumberSequence._meta.get_field("last_allocated_number").editable
        )

        sequence.last_allocated_number = 7
        with self.assertRaises(ValidationError):
            sequence.save()
        with self.assertRaises(ValidationError):
            QuoteNumberSequence.objects.filter(pk=sequence.pk).update(
                last_allocated_number=0
            )
        with self.assertRaises(ValidationError):
            QuoteNumberSequence.objects.bulk_update(
                [sequence],
                ["last_allocated_number"],
            )
        with self.assertRaises(ValidationError):
            QuoteNumberSequence.objects.bulk_create(
                [QuoteNumberSequence(year=2031, last_allocated_number=1)]
            )

    def test_quote_queryset_update_cannot_mutate_published_commercial_fields(self):
        quote = self.publish_quote()

        with self.assertRaises(ValidationError):
            Quote.objects.filter(pk=quote.pk).update(project_title="Bulk change")

    def test_quote_bulk_update_cannot_bypass_published_boundary(self):
        quote = self.publish_quote()
        quote.project_title = "Bulk change"

        with self.assertRaises(ValidationError):
            Quote.objects.bulk_update([quote], ["project_title"])

    def test_published_quote_queryset_delete_is_blocked(self):
        quote = self.publish_quote()

        with self.assertRaises(ValidationError):
            Quote.objects.filter(pk=quote.pk).delete()
        self.assertTrue(Quote.objects.filter(pk=quote.pk).exists())

    def test_quote_bulk_create_rejects_non_draft_status(self):
        with self.assertRaises(ValidationError):
            Quote.objects.bulk_create(
                [Quote(owner=self.owner, status=Quote.Status.SENT)]
            )

    def test_safe_operational_quote_queryset_update_is_allowed(self):
        quote = self.publish_quote()
        viewed_at = datetime.now(UTC)

        updated = Quote.objects.filter(pk=quote.pk).update(
            status=Quote.Status.VIEWED,
            last_viewed_at=viewed_at,
        )

        self.assertEqual(updated, 1)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.VIEWED)
        self.assertEqual(quote.last_viewed_at, viewed_at)

    def test_line_item_queryset_update_cannot_bypass_published_boundary(self):
        quote = self.publish_quote()
        item = quote.line_items.get()

        with self.assertRaises(ValidationError):
            QuoteLineItem.objects.filter(pk=item.pk).update(amount=Decimal("200.00"))

    def test_line_item_bulk_update_cannot_bypass_published_boundary(self):
        quote = self.publish_quote()
        item = quote.line_items.get()
        item.amount = Decimal("200.00")

        with self.assertRaises(ValidationError):
            QuoteLineItem.objects.bulk_update([item], ["amount"])

    def test_published_line_item_queryset_delete_is_blocked(self):
        quote = self.publish_quote()
        item = quote.line_items.get()

        with self.assertRaises(ValidationError):
            QuoteLineItem.objects.filter(pk=item.pk).delete()
        self.assertTrue(QuoteLineItem.objects.filter(pk=item.pk).exists())

    def test_line_item_bulk_create_cannot_target_published_quote(self):
        quote = self.publish_quote()

        with self.assertRaises(ValidationError):
            QuoteLineItem.objects.bulk_create(
                [QuoteLineItem(quote=quote, label="Bulk", amount=Decimal("1.00"))]
            )

    def test_quote_version_and_event_bulk_mutations_are_blocked(self):
        quote = self.publish_quote()
        version = QuoteVersion.objects.create(
            quote=quote,
            version_number=1,
            snapshot=build_quote_snapshot(quote),
            frozen_total=Decimal("100.00"),
            currency="USD",
        )
        event = QuoteEvent.objects.create(quote=quote, event_type="created")

        with self.assertRaises(ValidationError):
            QuoteVersion.objects.filter(pk=version.pk).update(
                frozen_total=Decimal("1.00")
            )
        with self.assertRaises(ValidationError):
            QuoteVersion.objects.bulk_update([version], ["frozen_total"])
        with self.assertRaises(ValidationError):
            QuoteVersion.objects.filter(pk=version.pk).delete()
        with self.assertRaises(ValidationError):
            QuoteVersion.objects.bulk_create([version])

        with self.assertRaises(ValidationError):
            QuoteEvent.objects.filter(pk=event.pk).update(note="Bulk change")
        with self.assertRaises(ValidationError):
            QuoteEvent.objects.bulk_update([event], ["note"])
        with self.assertRaises(ValidationError):
            QuoteEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            QuoteEvent.objects.bulk_create([event])

    def test_quote_version_rejects_invalid_snapshot_contracts(self):
        quote = self.make_publishable_quote()
        snapshot = build_quote_snapshot(quote)
        invalid_snapshots = []

        missing = dict(snapshot)
        missing.pop("client_name")
        invalid_snapshots.append(missing)

        wrong_schema = dict(snapshot)
        wrong_schema["snapshot_schema_version"] = 2
        invalid_snapshots.append(wrong_schema)

        wrong_total = dict(snapshot)
        wrong_total["chosen_total"] = "999.00"
        invalid_snapshots.append(wrong_total)

        wrong_currency = dict(snapshot)
        wrong_currency["currency"] = "EUR"
        invalid_snapshots.append(wrong_currency)

        with_model = dict(snapshot)
        with_model["client"] = dict(snapshot["client"])
        with_model["client"]["name"] = quote
        invalid_snapshots.append(with_model)

        for index, invalid_snapshot in enumerate(invalid_snapshots):
            with self.subTest(snapshot=invalid_snapshot):
                invalid_quote = self.make_publishable_quote(
                    quote_number=f"SQ-{2027 + index}-0001"
                )
                with self.assertRaises(ValidationError):
                    QuoteVersion.objects.create(
                        quote=invalid_quote,
                        version_number=1,
                        snapshot=invalid_snapshot,
                        frozen_total=Decimal("100.00"),
                        currency="USD",
                    )

    def test_quote_version_rejects_missing_allocated_quote_number(self):
        quote = self.make_publishable_quote()
        snapshot = build_quote_snapshot(quote)
        snapshot["quote_number"] = None

        with self.assertRaises(ValidationError) as error:
            QuoteVersion.objects.create(
                quote=quote,
                version_number=1,
                snapshot=snapshot,
                frozen_total=Decimal("100.00"),
                currency=quote.currency,
            )

        self.assertIn("quote_number", error.exception.message_dict)

    def test_quote_version_rejects_padded_quote_numbers(self):
        for quote_number in ("SQ-2026-0014", "SQ-2026-0015"):
            with self.subTest(quote_number=quote_number):
                padded_number = f" {quote_number} "
                quote = self.make_publishable_quote(quote_number=quote_number)
                snapshot = build_quote_snapshot(quote)
                snapshot["quote_number"] = padded_number

                with self.assertRaises(ValidationError) as error:
                    QuoteVersion.objects.create(
                        quote=quote,
                        version_number=1,
                        snapshot=snapshot,
                        frozen_total=Decimal("100.00"),
                        currency=quote.currency,
                    )

                self.assertIn("quote_number", error.exception.message_dict)

    def test_quote_version_rejects_excess_precision_monetary_values(self):
        cases = (
            ("minimum", "1160.004", Decimal("100.00")),
            ("chosen_total", "100.004", Decimal("100.00")),
            ("deposit_amount", "50.004", Decimal("100.00")),
            ("line_items", "100.004", Decimal("100.00")),
            ("frozen_total", None, Decimal("100.004")),
        )

        for index, (field_name, snapshot_value, frozen_total) in enumerate(cases):
            with self.subTest(field_name=field_name):
                quote = self.make_publishable_quote(
                    quote_number=f"SQ-{2027 + index}-0001"
                )
                snapshot = build_quote_snapshot(quote)
                if field_name == "line_items":
                    snapshot["line_items"][0]["amount"] = snapshot_value
                elif field_name == "minimum":
                    snapshot["minimum"] = snapshot_value
                    snapshot["recommended_minimum"] = snapshot_value
                    snapshot["scope"]["minimum"] = snapshot_value
                    snapshot["scope"]["recommended_minimum"] = snapshot_value
                    snapshot["pricing"]["minimum"] = snapshot_value
                elif field_name != "frozen_total":
                    snapshot[field_name] = snapshot_value

                with self.assertRaises(ValidationError):
                    QuoteVersion.objects.create(
                        quote=quote,
                        version_number=1,
                        snapshot=snapshot,
                        frozen_total=frozen_total,
                        currency=quote.currency,
                    )

    def test_quote_version_rejects_grouped_client_conflict(self):
        quote = self.make_publishable_quote()
        snapshot = build_quote_snapshot(quote)
        snapshot["client"] = {
            **snapshot["client"],
            "name": "Conflicting Client Name",
        }

        with self.assertRaises(ValidationError) as error:
            QuoteVersion.objects.create(
                quote=quote,
                version_number=1,
                snapshot=snapshot,
                frozen_total=Decimal("100.00"),
                currency=quote.currency,
            )

        self.assertIn("client.name", error.exception.message_dict)

    def test_quote_version_rejects_grouped_currency_conflict(self):
        quote = self.make_publishable_quote()
        snapshot = build_quote_snapshot(quote)
        snapshot["scope"] = {**snapshot["scope"], "currency": "EUR"}

        with self.assertRaises(ValidationError) as error:
            QuoteVersion.objects.create(
                quote=quote,
                version_number=1,
                snapshot=snapshot,
                frozen_total=Decimal("100.00"),
                currency=quote.currency,
            )

        self.assertIn("scope.currency", error.exception.message_dict)

    def test_quote_version_rejects_every_other_grouped_flat_conflict(self):
        quote = self.make_publishable_quote()
        conflicts = (
            ("project", "title", "Conflicting project title"),
            ("sender", "name", "Conflicting Sender Name"),
            ("dates", "issue_date", "2026-02-01"),
            ("scope", "delivery_days", 6),
            ("terms_details", "taxes", "Conflicting taxes"),
            ("brand", "accent_color", "#FFFFFF"),
            ("pricing", "recommended", "1321.00"),
        )

        for section, section_key, conflicting_value in conflicts:
            with self.subTest(section=section, field=section_key):
                snapshot = build_quote_snapshot(quote)
                snapshot[section][section_key] = conflicting_value
                with self.assertRaises(ValidationError) as error:
                    QuoteVersion.objects.create(
                        quote=quote,
                        version_number=1,
                        snapshot=snapshot,
                        frozen_total=Decimal("100.00"),
                        currency=quote.currency,
                    )
                self.assertIn(
                    f"{section}.{section_key}",
                    error.exception.message_dict,
                )

    def test_quote_version_rejects_deposit_and_line_item_mismatches(self):
        quote = self.make_publishable_quote()
        invalid_snapshots = []

        wrong_deposit = build_quote_snapshot(quote)
        wrong_deposit["deposit_amount"] = "49.99"
        invalid_snapshots.append(("deposit_amount", wrong_deposit))

        wrong_line_item = build_quote_snapshot(quote)
        wrong_line_item["line_items"][0]["amount"] = "99.00"
        invalid_snapshots.append(("frozen_total", wrong_line_item))

        for error_field, snapshot in invalid_snapshots:
            with self.subTest(error_field=error_field):
                with self.assertRaises(ValidationError) as error:
                    QuoteVersion.objects.create(
                        quote=quote,
                        version_number=1,
                        snapshot=snapshot,
                        frozen_total=Decimal("100.00"),
                        currency=quote.currency,
                    )
                self.assertIn(error_field, error.exception.message_dict)

    def test_quote_version_rejects_logo_mismatch(self):
        quote = self.make_publishable_quote(
            logo_object_key="quotes/versions/source.png"
        )
        snapshot = build_quote_snapshot(quote)

        with self.assertRaises(ValidationError) as error:
            QuoteVersion.objects.create(
                quote=quote,
                version_number=1,
                snapshot=snapshot,
                frozen_total=Decimal("100.00"),
                currency=quote.currency,
                logo_object_key="quotes/versions/different.png",
            )

        self.assertIn("logo_object_key", error.exception.message_dict)

    def test_quote_conflict_upsert_cannot_bypass_ownership_and_publish_rules(self):
        quote = self.make_quote(quote_number="SQ-2026-0010")
        conflict = Quote(
            id=quote.pk,
            owner=self.owner,
            quote_number=quote.quote_number,
            project_title="Conflict overwrite",
        )

        with self.assertRaises(ValidationError):
            Quote.objects.bulk_create(
                [conflict],
                update_conflicts=True,
                update_fields=["project_title"],
                unique_fields=["id"],
            )
        with self.assertRaises(ValidationError):
            Quote.objects.bulk_create([conflict], ignore_conflicts=True)

    def test_line_item_conflict_upsert_cannot_bypass_draft_boundary(self):
        quote = self.make_quote()
        item = self.add_line_items(quote, Decimal("100.00"))[0]
        conflict = QuoteLineItem(
            id=item.pk,
            quote=quote,
            label="Conflict overwrite",
            amount=Decimal("200.00"),
        )

        with self.assertRaises(ValidationError):
            QuoteLineItem.objects.bulk_create(
                [conflict],
                update_conflicts=True,
                update_fields=["label", "amount"],
                unique_fields=["id"],
            )
        with self.assertRaises(ValidationError):
            QuoteLineItem.objects.bulk_create([conflict], ignore_conflicts=True)

    def test_draft_quote_with_immutable_children_cannot_be_deleted(self):
        from quotes.services.quotes import delete_quote_tree

        quote = self.make_publishable_quote()
        version = self.make_version(quote)
        self.make_link(version, token_hash="a" * 64)
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=version,
            share_link=version.share_links.get(),
            event_type="created",
        )

        with self.assertRaises(ValidationError):
            quote.delete()
        with self.assertRaises(ValidationError):
            Quote.objects.filter(pk=quote.pk).delete()

        delete_quote_tree(quote)
        self.assertFalse(Quote.objects.filter(pk=quote.pk).exists())
        self.assertFalse(QuoteVersion.objects.filter(pk=version.pk).exists())
        self.assertFalse(QuoteEvent.objects.filter(quote_id=quote.pk).exists())
        self.assertFalse(ShareLink.objects.filter(quote_version_id=version.pk).exists())

    def test_published_quote_with_children_cannot_be_deleted(self):
        quote = self.publish_quote()
        version = self.make_version(quote)
        self.make_link(version, token_hash="b" * 64)
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=version,
            event_type="created",
        )

        with self.assertRaises(ValidationError):
            quote.delete()
        with self.assertRaises(ValidationError):
            Quote.objects.filter(pk=quote.pk).delete()
        self.assertTrue(Quote.objects.filter(pk=quote.pk).exists())

    def test_quote_version_rejects_more_than_fifty_line_items(self):
        quote = self.make_publishable_quote(quote_number="SQ-2026-0011")
        snapshot = build_quote_snapshot(quote)
        snapshot["line_items"] = [dict(snapshot["line_items"][0]) for _ in range(51)]

        with self.assertRaises(ValidationError):
            QuoteVersion.objects.create(
                quote=quote,
                version_number=1,
                snapshot=snapshot,
                frozen_total=Decimal("100.00"),
                currency="USD",
            )

    def test_quote_event_rejects_cross_quote_version_and_link_references(self):
        first_quote = self.make_publishable_quote(quote_number="SQ-2026-0012")
        second_quote = self.make_publishable_quote(quote_number="SQ-2026-0013")
        first_version = self.make_version(first_quote)
        second_version = self.make_version(second_quote)
        first_link = self.make_link(first_version, token_hash="c" * 64)
        second_link = self.make_link(second_version, token_hash="d" * 64)

        with self.assertRaises(ValidationError):
            QuoteEvent.objects.create(
                quote=first_quote,
                quote_version=second_version,
                event_type="created",
            )
        with self.assertRaises(ValidationError):
            QuoteEvent.objects.create(
                quote=first_quote,
                quote_version=first_version,
                share_link=second_link,
                event_type="created",
            )
        with self.assertRaises(ValidationError):
            QuoteEvent.objects.create(
                quote=first_quote,
                share_link=first_link,
                event_type="created",
            )

    def test_response_events_require_version_idempotency_and_fingerprint(self):
        quote = self.publish_quote()
        version = self.make_version(quote)

        with self.assertRaises(ValidationError):
            QuoteEvent.objects.create(
                quote=quote,
                quote_version=version,
                event_type=QuoteEvent.EventType.ACCEPTED,
            )
        with self.assertRaises(ValidationError):
            QuoteEvent.objects.create(
                quote=quote,
                quote_version=version,
                response_idempotency_key=uuid.uuid4(),
                event_type=QuoteEvent.EventType.ACCEPTED,
            )

        event = QuoteEvent.objects.create(
            quote=quote,
            quote_version=version,
            response_idempotency_key=uuid.uuid4(),
            payload_fingerprint="f" * 64,
            event_type=QuoteEvent.EventType.ACCEPTED,
        )
        self.assertEqual(event.quote_version_id_snapshot, version.pk)

    def test_sequence_fake_allocation_paths_are_rejected(self):
        with self.assertRaises(ValidationError):
            QuoteNumberSequence.objects.create(
                year=2040,
                last_allocated_number=1,
            )
        sequence = QuoteNumberSequence.objects.create(
            year=2041,
            last_allocated_number=0,
        )
        with self.assertRaises(ValidationError):
            QuoteNumberSequence.objects.bulk_create(
                [QuoteNumberSequence(year=sequence.year, last_allocated_number=99)],
                update_conflicts=True,
                update_fields=["last_allocated_number"],
                unique_fields=["year"],
            )

    def test_guest_conversion_nonce_is_nullable_and_unique(self):
        first = self.make_quote(guest_conversion_nonce="nonce-one")
        second = self.make_quote(guest_conversion_nonce="nonce-two")

        self.assertEqual(first.guest_conversion_nonce, "nonce-one")
        self.assertEqual(second.guest_conversion_nonce, "nonce-two")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_quote(guest_conversion_nonce="nonce-one")

    def test_quote_numbers_are_unique_and_allocated_by_utc_year(self):
        first_year = datetime(2025, 12, 31, 23, 59, tzinfo=UTC)
        second_year = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)

        first = allocate_quote_number(now=first_year)
        second = allocate_quote_number(now=first_year)
        rolled_over = allocate_quote_number(now=second_year)

        self.assertEqual(first, "SQ-2025-0001")
        self.assertEqual(second, "SQ-2025-0002")
        self.assertEqual(rolled_over, "SQ-2026-0001")
        self.assertEqual(
            QuoteNumberSequence.objects.get(year=2025).last_allocated_number, 2
        )
        self.assertEqual(
            QuoteNumberSequence.objects.get(year=2026).last_allocated_number, 1
        )

        self.make_quote(quote_number=first)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_quote(quote_number=first)

    def test_quote_number_conversion_uses_utc_not_input_timezone(self):
        local_time = datetime(2025, 1, 1, 1, tzinfo=timezone(timedelta(hours=2)))

        self.assertEqual(allocate_quote_number(now=local_time), "SQ-2024-0001")

    def test_deposit_and_realized_hourly_values_are_derived(self):
        quote = self.make_quote(chosen_total=Decimal("123.45"), deposit_percentage=50)

        quote.refresh_from_db()
        self.assertEqual(quote.deposit_amount, Decimal("61.73"))
        self.assertEqual(quote.realized_hourly_equivalent, Decimal("6.17"))

    def test_quote_requires_one_to_fifty_line_items_and_positive_total(self):
        quote = self.make_quote()
        self.add_line_items(quote, Decimal("10.00"), Decimal("-5.00"))

        self.assertTrue(quote.validate_for_publish())
        self.assertEqual(quote.chosen_total, Decimal("5.00"))

        quote.line_items.all().delete()
        with self.assertRaises(ValidationError):
            quote.validate_for_publish()

    def test_quote_publish_fields_are_required(self):
        quote = self.make_quote(
            client_name="",
            client_email="",
            project_title="",
            project_description="",
            sender_name="",
            sender_email="",
            issue_date=None,
            valid_until=None,
            terms="",
        )

        with self.assertRaises(ValidationError) as error:
            quote.validate_for_publish()
        message = str(error.exception)
        for field_name in (
            "client_name",
            "client_email",
            "project_title",
            "project_description",
            "sender_name",
            "sender_email",
            "issue_date",
            "valid_until",
            "terms",
        ):
            self.assertIn(field_name, message)

    def test_draft_is_mutable_until_publish_then_immutable(self):
        quote = self.make_publishable_quote()
        quote.project_title = "Updated draft title"
        quote.save()

        quote.status = Quote.Status.SENT
        quote.save()
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)

        quote.project_title = "Changed after publish"
        with self.assertRaises(ValidationError):
            quote.save()
        with self.assertRaises(ValidationError):
            quote.delete()

        self.assertTrue(Quote.objects.filter(pk=quote.pk).exists())

    def test_line_items_cannot_change_after_publish(self):
        quote = self.make_publishable_quote()
        item = quote.line_items.get()
        quote.status = Quote.Status.SENT
        quote.save()

        item.amount = Decimal("200.00")
        with self.assertRaises(ValidationError):
            item.save()

    def test_quote_version_is_immutable_after_creation(self):
        quote = self.make_publishable_quote(logo_object_key="quotes/versions/logo.png")
        version = QuoteVersion.objects.create(
            quote=quote,
            version_number=1,
            snapshot=build_quote_snapshot(quote),
            frozen_total=Decimal("100.00"),
            currency="USD",
            logo_object_key="quotes/versions/logo.png",
        )

        self.assertEqual(
            version.snapshot_schema_version
            if hasattr(version, "snapshot_schema_version")
            else version.snapshot.get("snapshot_schema_version"),
            1,
        )
        version.snapshot = {"changed": True}
        with self.assertRaises(ValidationError):
            version.save()
        with self.assertRaises(ValidationError):
            version.delete()
        self.assertTrue(QuoteVersion.objects.filter(pk=version.pk).exists())

    def test_quote_event_is_immutable_after_creation(self):
        quote = self.make_quote()
        event = QuoteEvent.objects.create(quote=quote, event_type="created")

        event.note = "changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        self.assertTrue(QuoteEvent.objects.filter(pk=event.pk).exists())

    def test_share_link_fields_support_hashed_lookup_and_lifecycle_timestamps(self):
        quote = self.make_publishable_quote()
        version = QuoteVersion.objects.create(
            quote=quote,
            version_number=1,
            snapshot=build_quote_snapshot(quote),
            frozen_total=Decimal("100.00"),
            currency="USD",
        )
        link = ShareLink.objects.create(
            quote_version=version,
            token_hash="a" * 64,
            encrypted_token=b"encrypted",
            encryption_key_version="v1",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )

        self.assertIsInstance(link.pk, uuid.UUID)
        self.assertEqual(link.quote_version, version)
        self.assertEqual(link.state, "active")
        self.assertTrue(ShareLink._meta.get_field("token_hash").db_index)
        self.assertTrue(ShareLink._meta.get_field("token_hash").unique)

    def test_share_link_quote_version_is_immutable_through_instance_save(self):
        quote = self.make_publishable_quote()
        first_version = self.make_version(quote, version_number=1)
        second_version = self.make_version(quote, version_number=2)
        link = self.make_link(first_version, token_hash="a" * 64)
        link.version = second_version

        with self.assertRaises(ValidationError):
            link.save()

        link.refresh_from_db()
        self.assertEqual(link.quote_version_id, first_version.pk)

    def test_share_link_quote_version_is_immutable_through_force_update(self):
        quote = self.make_publishable_quote()
        first_version = self.make_version(quote, version_number=1)
        second_version = self.make_version(quote, version_number=2)
        link = self.make_link(first_version, token_hash="a" * 64)
        forced = ShareLink(
            id=link.pk,
            quote_version=second_version,
            token_hash=link.token_hash,
            encrypted_token=link.encrypted_token,
            encryption_key_version=link.encryption_key_version,
            expires_at=link.expires_at,
        )

        with self.assertRaises(ValidationError):
            forced.save(force_update=True, update_fields=["quote_version"])

        link.refresh_from_db()
        self.assertEqual(link.quote_version_id, first_version.pk)

        reissued = self.make_link(second_version, token_hash="b" * 64)
        self.assertEqual(reissued.quote_version_id, second_version.pk)

    def test_share_link_quote_version_is_immutable_through_queryset_update(self):
        quote = self.make_publishable_quote()
        first_version = self.make_version(quote, version_number=1)
        second_version = self.make_version(quote, version_number=2)
        link = self.make_link(first_version, token_hash="b" * 64)

        updates = (
            {"quote_version": second_version},
            {"quote_version_id": second_version.pk},
        )
        for update in updates:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                ShareLink.objects.filter(pk=link.pk).update(**update)

        link.refresh_from_db()
        self.assertEqual(link.quote_version_id, first_version.pk)

    def test_share_link_quote_version_is_immutable_through_bulk_update(self):
        quote = self.make_publishable_quote()
        first_version = self.make_version(quote, version_number=1)
        second_version = self.make_version(quote, version_number=2)
        link = self.make_link(first_version, token_hash="c" * 64)
        link.quote_version = second_version

        with self.assertRaises(ValidationError):
            ShareLink.objects.bulk_update([link], ["quote_version"])

        link.refresh_from_db()
        self.assertEqual(link.quote_version_id, first_version.pk)

    def test_share_link_conflict_upsert_cannot_reassign_quote_version(self):
        quote = self.make_publishable_quote()
        first_version = self.make_version(quote, version_number=1)
        second_version = self.make_version(quote, version_number=2)
        link = self.make_link(first_version, token_hash="d" * 64)
        conflict = ShareLink(
            id=link.pk,
            quote_version=second_version,
            token_hash=link.token_hash,
            encrypted_token=link.encrypted_token,
            encryption_key_version=link.encryption_key_version,
            expires_at=link.expires_at,
        )

        with self.assertRaises(ValidationError):
            ShareLink.objects.bulk_create(
                [conflict],
                update_conflicts=True,
                update_fields=["quote_version"],
                unique_fields=["id"],
            )

        link.refresh_from_db()
        self.assertEqual(link.quote_version_id, first_version.pk)

    def test_snapshot_contains_independent_plain_data_for_all_commercial_fields(self):
        quote = self.make_quote(
            project_title="Snapshot project",
            project_description="A complete project overview.",
            client_name="Snapshot Client",
            client_company="Client Company",
            client_email="snapshot-client@example.com",
            sender_name="Snapshot Sender",
            sender_business_name="Sender Business",
            sender_email="snapshot-sender@example.com",
            sender_website="https://sender.example.com",
            sender_monogram="SS",
            accent_color="#123456",
            logo_object_key="quotes/versions/source.png",
            timeline="Delivery timeline",
            revision_terms="Two revision rounds",
            notes="Notes",
            exclusions="Exclusions",
            taxes="Taxes excluded",
            cancellation_terms="Cancellation terms",
        )
        self.add_line_items(quote, Decimal("100.00"), Decimal("-10.00"))

        snapshot = build_quote_snapshot(quote)

        self.assertIs(type(snapshot), dict)
        self.assertEqual(snapshot["snapshot_schema_version"], 1)
        self.assertEqual(snapshot["client"]["name"], "Snapshot Client")
        self.assertEqual(snapshot["sender"]["business_name"], "Sender Business")
        self.assertEqual(
            snapshot["scope"]["pricing_config_version"], PRICING_CONFIG_VERSION
        )
        self.assertEqual(snapshot["line_items"][1]["amount"], "-10.00")
        self.assertEqual(snapshot["dates"]["issue_date"], "2026-01-01")
        self.assertEqual(
            snapshot["terms"],
            "Payment is due according to the agreed schedule.",
        )
        self.assertEqual(snapshot["terms_details"]["taxes"], "Taxes excluded")
        self.assertEqual(
            snapshot["brand"]["logo_object_key"], "quotes/versions/source.png"
        )
        self.assertNotIn(quote, snapshot.values())
        self.assertNotIn(quote.line_items.first(), snapshot["line_items"])

        snapshot["line_items"][0]["label"] = "mutated"
        snapshot["client"]["name"] = "mutated"
        second_snapshot = build_quote_snapshot(quote)
        self.assertEqual(second_snapshot["line_items"][0]["label"], "Item 0")
        self.assertEqual(second_snapshot["client"]["name"], "Snapshot Client")
