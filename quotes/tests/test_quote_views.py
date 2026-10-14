import re
import tempfile
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from accounts.models import Profile
from pricing.constants import PRICING_CONFIG_VERSION
from quotes.forms import QuoteEditorForm, QuoteLineItemFormSet, QuotePublishForm
from quotes.models import Quote, QuoteEvent, QuoteLineItem, QuoteVersion, ShareLink
from quotes.services.links import create_share_link
from quotes.services.quotes import duplicate_quote, publish_quote
from quotes.services.storage import commit_version_logo, delete_object, stage_logo

User = get_user_model()
ENCRYPTION_KEY = "c2NvcGVxdW90ZS1kZXYtb25seS1rZXktMzJieXRlcyE="


def line_formset_data(*amounts):
    data = {
        "line_items-TOTAL_FORMS": str(len(amounts)),
        "line_items-INITIAL_FORMS": "0",
        "line_items-MIN_NUM_FORMS": "1",
        "line_items-MAX_NUM_FORMS": "50",
    }
    for index, amount in enumerate(amounts):
        data.update(
            {
                f"line_items-{index}-label": f"Item {index + 1}",
                f"line_items-{index}-description": "",
                f"line_items-{index}-amount": str(amount),
                f"line_items-{index}-sort_order": str(index),
                f"line_items-{index}-id": "",
                f"line_items-{index}-quote": "",
            }
        )
    return data


class QuoteViewTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "task9-owner@example.com",
            password="correct horse battery staple",
        )
        self.other_owner = User.objects.create_user(
            "task9-other@example.com",
            password="correct horse battery staple",
        )
        self.owner.email_verified_at = timezone.now()
        self.owner.save(update_fields=("email_verified_at",))
        self.quote = self.make_quote(self.owner)
        self.client.force_login(self.owner)

    def make_quote(
        self, owner=None, *, status=Quote.Status.DRAFT, line_total=Decimal("100.00")
    ):
        quote = Quote.objects.create(
            owner=owner or self.owner,
            project_title="A draft project",
            project_description=(
                "A complete project overview with enough detail for the editor "
                "and publication flow."
            ),
            category="website",
            delivery_days=5,
            complexity="standard",
            revision_rounds=0,
            urgency="standard",
            experience="intermediate",
            currency="USD",
            pricing_config_version=PRICING_CONFIG_VERSION,
            effective_hours=Decimal("20.00"),
            minimum=Decimal("1160.00"),
            recommended=Decimal("1320.00"),
            maximum=Decimal("1480.00"),
            chosen_total=line_total,
            hourly_equivalent=Decimal("66.00"),
            client_name="Client Name",
            client_email="client@example.com",
            sender_name="Sender Name",
            sender_email="sender@example.com",
            issue_date=date(2026, 9, 24),
            valid_until=date(2026, 10, 24),
            deposit_percentage=50,
            terms="Payment follows the agreed schedule.",
        )
        QuoteLineItem.objects.create(
            quote=quote,
            label="Project fee",
            amount=line_total,
        )
        if status != Quote.Status.DRAFT:
            quote.status = status
            quote.quote_number = "SQ-2026-0099"
            quote.save()
        return quote

    def editor_payload(self, quote=None, **overrides):
        quote = quote or self.quote
        values = {
            "project_title": quote.project_title,
            "project_description": quote.project_description,
            "category": quote.category,
            "delivery_days": quote.delivery_days,
            "complexity": quote.complexity,
            "revision_rounds": quote.revision_rounds,
            "urgency": quote.urgency,
            "experience": quote.experience,
            "currency": quote.currency,
            "client_name": quote.client_name,
            "client_company": quote.client_company,
            "client_email": quote.client_email,
            "sender_name": quote.sender_name,
            "sender_business_name": quote.sender_business_name,
            "sender_email": quote.sender_email,
            "sender_website": quote.sender_website,
            "sender_monogram": quote.sender_monogram,
            "accent_color": quote.accent_color,
            "issue_date": quote.issue_date.isoformat(),
            "valid_until": quote.valid_until.isoformat(),
            "deposit_percentage": quote.deposit_percentage,
            "timeline": quote.timeline,
            "revision_terms": quote.revision_terms,
            "terms": quote.terms,
            "notes": quote.notes,
            "exclusions": quote.exclusions,
            "taxes": quote.taxes,
            "cancellation_terms": quote.cancellation_terms,
            "updated_at": quote.updated_at.isoformat(),
        }
        values.update(overrides)
        return values

    def publish_payload(self, quote=None, **overrides):
        quote = quote or self.quote
        values = {
            "client_name": quote.client_name,
            "client_email": quote.client_email,
            "project_title": quote.project_title,
            "project_description": quote.project_description,
            "sender_name": quote.sender_name,
            "sender_email": quote.sender_email,
            "issue_date": quote.issue_date.isoformat(),
            "valid_until": quote.valid_until.isoformat(),
            "terms": quote.terms,
            "updated_at": quote.updated_at.isoformat(),
        }
        values.update(overrides)
        return values

    def _image_bytes(self):
        return self._image_bytes_for_format("PNG")

    def _image_bytes_for_format(self, image_format):
        image = Image.new("RGB", (8, 8), "#E85D3F")
        output = BytesIO()
        image.save(output, format=image_format)
        return output.getvalue()

    def _storage_keys(self, path=""):
        directories, files = default_storage.listdir(path)
        keys = list(files)
        for directory in directories:
            keys.extend(
                self._storage_keys(f"{path}/{directory}" if path else directory)
            )
        return keys


class QuoteFormTests(QuoteViewTestCase):
    def test_editor_form_recalculates_pricing_inputs_and_ignores_client_totals(self):
        form = QuoteEditorForm(
            self.editor_payload(
                delivery_days=10,
                revision_rounds=4,
                minimum="1",
                recommended="999999",
                maximum="999999",
            )
        )

        self.assertTrue(form.is_valid(), form.errors)
        estimate = form.estimate()
        self.assertEqual(form.cleaned_data["delivery_days"], 10)
        self.assertEqual(form.cleaned_data["revision_rounds"], 4)
        self.assertEqual(estimate.pricing_config_version, PRICING_CONFIG_VERSION)

    def test_line_item_formset_enforces_one_to_fifty_and_positive_total(self):
        valid = QuoteLineItemFormSet(line_formset_data("10.00", "-2.00"))
        self.assertTrue(valid.is_valid(), valid.errors)

        empty = QuoteLineItemFormSet(line_formset_data())
        self.assertFalse(empty.is_valid())
        too_many = QuoteLineItemFormSet(line_formset_data(*(["1.00"] * 51)))
        self.assertFalse(too_many.is_valid())
        nonpositive = QuoteLineItemFormSet(line_formset_data("0.00", "-1.00"))
        self.assertFalse(nonpositive.is_valid())

        fifty_plus_blank = line_formset_data(*(["1.00"] * 50))
        fifty_plus_blank["line_items-TOTAL_FORMS"] = "51"
        fifty_plus_blank.update(
            {
                "line_items-50-label": "",
                "line_items-50-description": "",
                "line_items-50-amount": "",
                "line_items-50-sort_order": "",
            }
        )
        fifty_forms = QuoteLineItemFormSet(fifty_plus_blank)
        self.assertTrue(fifty_forms.is_valid(), fifty_forms.errors)
        self.assertEqual(
            sum(
                bool(form.cleaned_data.get("label"))
                for form in fifty_forms.forms
                if form.cleaned_data
            ),
            50,
        )

        default_prefix_data = {
            key.replace("line_items-", "form-"): value
            for key, value in line_formset_data("10.00").items()
        }
        default_prefix = QuoteLineItemFormSet(default_prefix_data)
        self.assertTrue(default_prefix.is_valid(), default_prefix.errors)

        existing = QuoteLineItemFormSet(
            queryset=QuoteLineItem.objects.filter(quote=self.quote)
        )
        self.assertEqual(existing.initial_form_count(), 1)
        self.assertEqual(existing.forms[0].initial["label"], "Project fee")

    def test_editor_form_accepts_model_compatible_aliases_and_instance(self):
        values = self.editor_payload()
        values["title"] = values.pop("project_title")
        values["description"] = values.pop("project_description")
        values["duration_days"] = values.pop("delivery_days")
        values["revisions"] = values.pop("revision_rounds")
        form = QuoteEditorForm(values, instance=self.quote)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["project_title"], "A draft project")

    def test_publish_form_accepts_instance_alias(self):
        form = QuotePublishForm(instance=self.quote)
        self.assertEqual(form.initial["client_name"], "Client Name")

    def test_editor_form_normalizes_client_and_sender_email(self):
        form = QuoteEditorForm(
            self.editor_payload(
                client_email=" Client@Example.com ",
                sender_email=" Sender@Example.com ",
            ),
            quote=self.quote,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["client_email"], "client@example.com")
        self.assertEqual(form.cleaned_data["sender_email"], "sender@example.com")

    def test_editor_form_does_not_allow_currency_changes(self):
        form = QuoteEditorForm(
            self.editor_payload(currency="EUR"),
            quote=self.quote,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("currency", form.errors)

    def test_publish_form_accepts_confirmation_alias(self):
        form = QuotePublishForm(
            self.publish_payload(confirm_adjustment="on"),
            quote=self.quote,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_publish_form_rejects_more_than_fifty_line_items(self):
        for index in range(50):
            QuoteLineItem.objects.create(
                quote=self.quote,
                label=f"Extra {index}",
                amount="1.00",
            )
        form = QuotePublishForm(
            self.publish_payload(intentional_adjustment_confirmed="on"),
            quote=self.quote,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("50 line items", str(form.errors))

    def test_publish_form_rejects_quote_without_line_items(self):
        self.quote.line_items.all().delete()
        form = QuotePublishForm(
            self.publish_payload(intentional_adjustment_confirmed="on"),
            quote=self.quote,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("line item", str(form.errors).lower())

    def test_publish_form_requires_intentional_adjustment_confirmation(self):
        form = QuotePublishForm(
            self.publish_payload(),
            quote=self.quote,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("intentional_adjustment_confirmed", form.errors)

        confirmed = QuotePublishForm(
            self.publish_payload(intentional_adjustment_confirmed="on"),
            quote=self.quote,
        )
        self.assertTrue(confirmed.is_valid(), confirmed.errors)


class QuotePermissionAndEditorViewTests(QuoteViewTestCase):
    def test_owner_routes_return_404_for_another_owner(self):
        self.client.force_login(self.other_owner)
        for suffix in ("", "edit/", "publish/", "duplicate/", "links/"):
            with self.subTest(suffix=suffix):
                response = self.client.get(f"/quotes/{self.quote.pk}/{suffix}")
                self.assertEqual(response.status_code, 404)

    def test_new_quote_uses_profile_defaults(self):
        profile = Profile.create_for_user(self.owner)
        profile.default_currency = "EUR"
        profile.default_deposit_percentage = 35
        profile.default_accent_color = "#123456"
        profile.save()
        self.client.force_login(self.owner)

        response = self.client.get("/quotes/new/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["currency"], "EUR")
        self.assertEqual(response.context["form"].initial["deposit_percentage"], 35)
        self.assertEqual(response.context["form"].initial["accent_color"], "#123456")
        self.assertEqual(
            response.context["form"].initial["valid_until"],
            timezone.localdate() + timedelta(days=30),
        )
        self.assertContains(response, "Suggested deliverables")
        self.assertContains(response, "discovery")

    def test_edit_keeps_existing_quote_values_over_profile_defaults(self):
        profile = Profile.create_for_user(self.owner)
        profile.default_currency = "EUR"
        profile.default_deposit_percentage = 35
        profile.default_accent_color = "#123456"
        profile.save()

        response = self.client.get(f"/quotes/{self.quote.pk}/edit/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["currency"], "USD")
        self.assertEqual(response.context["form"].initial["deposit_percentage"], 50)
        self.assertEqual(response.context["form"].initial["accent_color"], "#E85D3F")

    def test_editor_upload_persists_staged_logo_metadata_and_publish_uses_it(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            upload = SimpleUploadedFile(
                "brand.jpg",
                self._image_bytes_for_format("JPEG"),
                content_type="image/jpeg",
            )
            response = self.client.post(
                f"/quotes/{self.quote.pk}/edit/",
                {
                    **self.editor_payload(),
                    **line_formset_data("100.00"),
                    "logo": upload,
                },
            )

            self.assertEqual(response.status_code, 302)
            self.quote.refresh_from_db()
            staged_key = self.quote.logo_object_key
            self.assertTrue(staged_key)
            self.assertEqual(self.quote.logo_content_type, "image/jpeg")
            self.assertGreater(self.quote.logo_byte_size, 0)
            self.assertTrue(default_storage.exists(staged_key))

            version = publish_quote(
                quote=self.quote,
                user=self.owner,
                intentional_adjustment_confirmed=True,
            )
            self.assertNotEqual(version.logo_object_key, staged_key)
            self.assertTrue(default_storage.exists(version.logo_object_key))

    def test_failed_editor_upload_transaction_cleans_staged_logo(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            upload = SimpleUploadedFile(
                "brand.png",
                self._image_bytes(),
                content_type="image/png",
            )
            with patch(
                "quotes.views._save_line_items", side_effect=OSError("db write")
            ):
                response = self.client.post(
                    f"/quotes/{self.quote.pk}/edit/",
                    {
                        **self.editor_payload(),
                        **line_formset_data("100.00"),
                        "logo": upload,
                    },
                )

            self.assertEqual(response.status_code, 503)
            self.quote.refresh_from_db()
            self.assertEqual(self.quote.logo_object_key, "")
            self.assertFalse(
                any(
                    key.startswith(f"quotes/logos/staging/{self.owner.pk}/")
                    for key in self._storage_keys()
                )
            )

    def test_failed_new_quote_database_error_cleans_staged_logo(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            upload = SimpleUploadedFile(
                "brand.png",
                self._image_bytes(),
                content_type="image/png",
            )
            self.client.raise_request_exception = False
            with patch(
                "quotes.views._save_line_items",
                side_effect=DatabaseError("db write"),
            ):
                response = self.client.post(
                    "/quotes/new/",
                    {
                        **self.editor_payload(),
                        **line_formset_data("100.00"),
                        "logo": upload,
                    },
                )

            self.assertEqual(response.status_code, 503)
            self.assertFalse(
                any(
                    key.startswith(f"quotes/logos/staging/{self.owner.pk}/")
                    for key in self._storage_keys()
                )
            )

    def test_failed_edit_database_error_cleans_staged_logo(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            upload = SimpleUploadedFile(
                "brand.png",
                self._image_bytes(),
                content_type="image/png",
            )
            self.client.raise_request_exception = False
            with patch(
                "quotes.views._save_line_items",
                side_effect=DatabaseError("db write"),
            ):
                response = self.client.post(
                    f"/quotes/{self.quote.pk}/edit/",
                    {
                        **self.editor_payload(),
                        **line_formset_data("100.00"),
                        "logo": upload,
                    },
                )

            self.assertEqual(response.status_code, 503)
            self.quote.refresh_from_db()
            self.assertEqual(self.quote.logo_object_key, "")
            self.assertFalse(
                any(
                    key.startswith(f"quotes/logos/staging/{self.owner.pk}/")
                    for key in self._storage_keys()
                )
            )

    def test_failed_publish_upload_cleans_staged_logo(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            upload = SimpleUploadedFile(
                "brand.png",
                self._image_bytes(),
                content_type="image/png",
            )
            with patch(
                "quotes.services.quotes.commit_version_logo",
                side_effect=OSError("copy failed"),
            ):
                response = self.client.post(
                    f"/quotes/{self.quote.pk}/publish/",
                    {
                        **self.publish_payload(intentional_adjustment_confirmed="on"),
                        "logo": upload,
                    },
                )

            self.assertEqual(response.status_code, 503)
            self.assertFalse(
                any(
                    key.startswith(f"quotes/logos/staging/{self.owner.pk}/")
                    for key in self._storage_keys()
                )
            )

        response = self.client.post(
            f"/quotes/{self.quote.pk}/edit/",
            {
                **self.editor_payload(project_title="Updated draft"),
                **line_formset_data("120.00"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.project_title, "Updated draft")
        self.assertEqual(self.quote.chosen_total, Decimal("120.00"))

        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        response = self.client.get(f"/quotes/{self.quote.pk}/edit/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/quotes/{self.quote.pk}/duplicate/", response.url)

    def test_incomplete_overview_clears_derived_estimate_on_pricing_change(self):
        for description in ("", "short overview"):
            with self.subTest(description=description):
                quote = self.make_quote(self.owner)
                response = self.client.post(
                    f"/quotes/{quote.pk}/edit/",
                    {
                        **self.editor_payload(
                            quote,
                            project_description=description,
                            delivery_days=10,
                            revision_rounds=4,
                        ),
                        **line_formset_data("100.00"),
                    },
                )

                self.assertEqual(response.status_code, 302)
                quote.refresh_from_db()
                self.assertEqual(quote.effective_hours, Decimal("0.00"))
                self.assertEqual(quote.minimum, Decimal("0.00"))
                self.assertEqual(quote.recommended, Decimal("0.00"))
                self.assertEqual(quote.maximum, Decimal("0.00"))
                self.assertEqual(quote.hourly_equivalent, Decimal("0.00"))

    def test_stale_edit_returns_conflict_without_overwriting(self):
        stale_timestamp = self.quote.updated_at.isoformat()
        self.quote.project_title = "Newer title"
        self.quote.save()
        payload = {
            **self.editor_payload(
                project_title="Stale title", updated_at=stale_timestamp
            ),
            **line_formset_data("100.00"),
        }

        response = self.client.post(f"/quotes/{self.quote.pk}/edit/", payload)

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "changed", status_code=409)
        self.assertContains(response, "Newer title", status_code=409)
        self.assertEqual(response.context["quote"].project_title, "Newer title")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.project_title, "Newer title")

    def test_edit_without_an_updated_at_token_is_rejected(self):
        payload = {
            **self.editor_payload(),
            **line_formset_data("100.00"),
        }
        payload.pop("updated_at")

        response = self.client.post(f"/quotes/{self.quote.pk}/edit/", payload)

        self.assertEqual(response.status_code, 409)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.project_title, "A draft project")


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=ENCRYPTION_KEY, SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1"
)
class QuotePublicationAndStorageTests(QuoteViewTestCase):
    def test_publish_allocates_number_and_creates_immutable_snapshot(self):
        version = publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        self.quote.refresh_from_db()

        self.assertEqual(version.version_number, 1)
        self.assertEqual(self.quote.status, Quote.Status.SENT)
        self.assertRegex(self.quote.quote_number, r"^SQ-\d{4}-\d{4}$")
        self.assertEqual(self.quote.current_published_version_id, version.pk)
        self.assertEqual(version.snapshot["project_title"], self.quote.project_title)
        self.assertNotIn(self.quote, version.snapshot.values())
        self.assertEqual(
            QuoteEvent.objects.filter(
                quote=self.quote,
                event_type=QuoteEvent.Type.PUBLISHED,
            ).count(),
            1,
        )
        with self.assertRaises(ValidationError):
            version.snapshot = {"changed": True}
            version.save()

    def test_direct_publish_requires_confirmation_for_out_of_range_total(self):
        with self.assertRaises(ValidationError) as error:
            publish_quote(quote=self.quote, user=self.owner)

        self.assertIn("intentional_adjustment_confirmed", error.exception.message_dict)
        self.assertEqual(QuoteVersion.objects.filter(quote=self.quote).count(), 0)
        version = publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        self.assertIsNotNone(version)

    def test_direct_publish_validates_lengths_and_emails(self):
        self.quote.project_description = "x" * 3001
        self.quote.terms = "x" * 2001
        self.quote.client_email = "not-an-email"
        self.quote.sender_email = "also-not-an-email"
        self.quote.save(
            update_fields=(
                "project_description",
                "terms",
                "client_email",
                "sender_email",
            )
        )

        with self.assertRaises(ValidationError) as error:
            publish_quote(
                quote=self.quote,
                user=self.owner,
                intentional_adjustment_confirmed=True,
            )

        message_dict = error.exception.message_dict
        self.assertIn("project_description", message_dict)
        self.assertIn("terms", message_dict)
        self.assertIn("client_email", message_dict)
        self.assertIn("sender_email", message_dict)
        self.assertEqual(QuoteVersion.objects.filter(quote=self.quote).count(), 0)

    def test_logo_storage_preserves_supported_content_type_metadata(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            output = BytesIO()
            Image.new("RGB", (8, 8), "#E85D3F").save(output, format="JPEG")
            stored = stage_logo(
                SimpleUploadedFile("logo.jpg", output.getvalue()),
                owner_id=self.owner.pk,
            )

            self.assertEqual(stored.content_type, "image/jpeg")
            delete_object(stored.storage_key)

    def test_publish_reencodes_existing_quote_logos_and_preserves_format(self):
        formats = (("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp"))
        for image_format, extension, content_type in formats:
            with (
                self.subTest(image_format=image_format),
                tempfile.TemporaryDirectory() as directory,
                override_settings(MEDIA_ROOT=directory),
            ):
                quote = self.make_quote(self.owner)
                key = f"quotes/logos/legacy/{self.owner.pk}/logo.{extension}"
                default_storage.save(
                    key,
                    ContentFile(self._image_bytes_for_format(image_format)),
                )
                quote.logo_object_key = key
                quote.save(update_fields=("logo_object_key",))

                version = publish_quote(
                    quote=quote,
                    user=self.owner,
                    intentional_adjustment_confirmed=True,
                )

                self.assertTrue(version.logo_object_key.endswith(f".{extension}"))
                self.assertEqual(quote.logo_content_type, content_type)
                self.assertGreater(quote.logo_byte_size, 0)
                with default_storage.open(version.logo_object_key, "rb") as source:
                    self.assertEqual(Image.open(source).format, image_format)

    def test_publish_rejects_invalid_legacy_quote_logo(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            key = f"quotes/logos/legacy/{self.owner.pk}/bad.png"
            default_storage.save(key, ContentFile(b"not an image"))
            self.quote.logo_object_key = key
            self.quote.save(update_fields=("logo_object_key",))

            with self.assertRaises(ValidationError):
                publish_quote(
                    quote=self.quote,
                    user=self.owner,
                    intentional_adjustment_confirmed=True,
                )

            self.quote.refresh_from_db()
            self.assertEqual(self.quote.status, Quote.Status.DRAFT)
            self.assertEqual(QuoteVersion.objects.filter(quote=self.quote).count(), 0)

    def test_publish_view_rejects_unverified_owner(self):
        self.owner.email_verified_at = None
        self.owner.save(update_fields=("email_verified_at",))

        response = self.client.post(
            f"/quotes/{self.quote.pk}/publish/",
            self.publish_payload(intentional_adjustment_confirmed="on"),
        )

        self.assertEqual(response.status_code, 403)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

    def test_publish_view_rechecks_the_timestamp_under_the_row_lock(self):
        with patch(
            "quotes.views._updated_at_matches",
            side_effect=[True, False],
        ):
            response = self.client.post(
                f"/quotes/{self.quote.pk}/publish/",
                self.publish_payload(intentional_adjustment_confirmed="on"),
            )

        self.assertEqual(response.status_code, 409)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

    def test_publish_view_requires_an_updated_at_token(self):
        payload = self.publish_payload(intentional_adjustment_confirmed="on")
        payload.pop("updated_at")
        response = self.client.post(f"/quotes/{self.quote.pk}/publish/", payload)

        self.assertEqual(response.status_code, 409)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

    def test_publish_view_renders_an_iso_updated_at_token(self):
        response = self.client.get(f"/quotes/{self.quote.pk}/publish/")
        content = response.content.decode()
        match = re.search(r'name="updated_at" value="([^"]+)"', content)

        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"^\d{4}-\d{2}-\d{2}T")

    def test_publish_view_creates_a_version_and_redirects_to_links(self):
        response = self.client.post(
            f"/quotes/{self.quote.pk}/publish/",
            self.publish_payload(intentional_adjustment_confirmed="on"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/quotes/{self.quote.pk}/links/")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)
        self.assertEqual(QuoteVersion.objects.filter(quote=self.quote).count(), 1)

    def test_logo_storage_reencodes_and_copies_to_version_key(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            stored = stage_logo(
                SimpleUploadedFile("logo.png", self._image_bytes()),
                owner_id=self.owner.pk,
            )
            self.assertTrue(default_storage.exists(stored.storage_key))
            destination = commit_version_logo(stored, version_id=uuid.uuid4())

            self.assertIn("quotes/versions", destination)
            self.assertTrue(default_storage.exists(destination))
            self.assertFalse(default_storage.exists(stored.storage_key))
            delete_object(destination)
            self.assertFalse(default_storage.exists(destination))

    def test_publish_uses_profile_logo_when_no_new_logo_is_uploaded(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            profile = Profile.create_for_user(self.owner)
            profile.logo.save(
                "brand.png",
                ContentFile(self._image_bytes()),
                save=True,
            )
            response = self.client.post(
                f"/quotes/{self.quote.pk}/publish/",
                self.publish_payload(intentional_adjustment_confirmed="on"),
            )

            self.assertEqual(response.status_code, 302)
            version = QuoteVersion.objects.get(quote=self.quote)
            self.assertTrue(version.logo_object_key)
            self.assertTrue(default_storage.exists(version.logo_object_key))

    def test_publish_copies_an_existing_owner_logo_to_the_version_key(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MEDIA_ROOT=directory),
        ):
            stored = stage_logo(
                SimpleUploadedFile("logo.png", self._image_bytes()),
                owner_id=self.owner.pk,
            )
            self.quote.logo_object_key = stored.storage_key
            self.quote.save(update_fields=("logo_object_key",))

            version = publish_quote(
                quote=self.quote,
                user=self.owner,
                intentional_adjustment_confirmed=True,
            )

            self.assertNotEqual(version.logo_object_key, stored.storage_key)
            self.assertTrue(default_storage.exists(version.logo_object_key))

    def test_publish_rolls_back_when_logo_copy_fails(self):
        upload = SimpleUploadedFile(
            "logo.png",
            self._image_bytes(),
            content_type="image/png",
        )
        with patch(
            "quotes.services.quotes.commit_version_logo",
            side_effect=OSError("copy failed"),
        ):
            with self.assertRaises(OSError):
                publish_quote(
                    quote=self.quote,
                    user=self.owner,
                    staged_logo=stage_logo(upload, owner_id=self.owner.pk),
                    intentional_adjustment_confirmed=True,
                )

        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)
        self.assertEqual(QuoteVersion.objects.filter(quote=self.quote).count(), 0)
        self.assertEqual(QuoteEvent.objects.filter(quote=self.quote).count(), 0)

    def test_duplicate_copies_draft_data_without_links(self):
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        version = self.quote.current_published_version
        ShareLink.objects.create(
            quote_version=version,
            token_hash="a" * 64,
            encrypted_token=b"ciphertext",
            encryption_key_version="v1",
            expires_at=timezone.now() + timedelta(days=1),
        )

        duplicate = duplicate_quote(quote=self.quote, user=self.owner)

        self.assertNotEqual(duplicate.pk, self.quote.pk)
        self.assertEqual(duplicate.owner, self.owner)
        self.assertEqual(duplicate.status, Quote.Status.DRAFT)
        self.assertIsNone(duplicate.quote_number)
        self.assertIsNone(duplicate.current_published_version)
        self.assertEqual(duplicate.line_items.count(), 1)
        self.assertEqual(
            ShareLink.objects.filter(quote_version__quote=duplicate).count(), 0
        )

    def test_links_page_rejects_reissue_for_unverified_owner(self):
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        link = create_share_link(
            version=self.quote.current_published_version,
            now=timezone.now(),
        )
        self.owner.email_verified_at = None
        self.owner.save(update_fields=("email_verified_at",))

        response = self.client.post(
            f"/quotes/{self.quote.pk}/links/",
            {"action": "reissue", "link_id": link.pk},
        )

        self.assertEqual(response.status_code, 403)
        link.refresh_from_db()
        self.assertIsNone(link.revoked_at)
        self.assertGreaterEqual(
            ShareLink.objects.filter(quote_version=link.quote_version).count(), 2
        )

    def test_links_page_creates_and_revokes_owner_scoped_links(self):
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        response = self.client.post(
            f"/quotes/{self.quote.pk}/links/", {"action": "create"}
        )
        self.assertEqual(response.status_code, 302)
        link = (
            ShareLink.objects.filter(quote_version__quote=self.quote)
            .order_by("-created_at")
            .first()
        )

        response = self.client.post(
            f"/quotes/{self.quote.pk}/links/",
            {"action": "revoke", "link_id": link.pk},
        )
        self.assertEqual(response.status_code, 302)
        link.refresh_from_db()
        self.assertIsNotNone(link.revoked_at)
