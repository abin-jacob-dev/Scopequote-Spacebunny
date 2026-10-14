import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from pricing.constants import PRICING_CONFIG_VERSION
from quotes.forms import QuoteEditorForm, QuoteLineItemFormSet
from quotes.models import (
    Quote,
    QuoteEvent,
    QuoteLineItem,
    QuoteNumberSequence,
    QuoteVersion,
    ShareLink,
)
from quotes.services.brand import (
    authorized_logo_data_uri,
    contrast_ratio,
    readable_foreground,
)
from quotes.services.links import (
    LinkLifecycleError,
    create_share_link,
    decrypt_link_token,
)
from quotes.services.pdf import render_published_quote_pdf
from quotes.services.quotes import (
    QuoteNumberExhausted,
    allocate_quote_number,
    duplicate_quote,
    publish_quote,
)
from quotes.services.responses import response_fingerprint
from quotes.services.storage import stage_logo

User = get_user_model()
KEY = "c2NvcGVxdW90ZS1kZXYtb25seS1rZXktMzJieXRlcyE="


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
            }
        )
    return data


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=KEY, SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1"
)
class FinalQuoteFixesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "final-owner@example.com", password="correct horse battery staple"
        )
        self.owner.email_verified_at = timezone.now()
        self.owner.save(update_fields=("email_verified_at",))
        self.client.force_login(self.owner)

    def make_quote(self, **overrides):
        values = {
            "owner": self.owner,
            "project_title": "A draft project",
            "project_description": (
                "A complete project overview with enough detail for publication "
                "and review."
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
            "minimum": Decimal("100.00"),
            "recommended": Decimal("100.00"),
            "maximum": Decimal("100.00"),
            "chosen_total": Decimal("100.00"),
            "hourly_equivalent": Decimal("5.00"),
            "client_name": "Exact Client Name",
            "client_email": "client@example.com",
            "sender_name": "Exact Sender Name",
            "sender_email": "sender@example.com",
            "issue_date": date(2026, 9, 24),
            "valid_until": date(2026, 10, 24),
            "deposit_percentage": 50,
            "terms": "Payment follows the agreed schedule.",
        }
        values.update(overrides)
        quote = Quote.objects.create(**values)
        QuoteLineItem.objects.create(
            quote=quote, label="Project fee", amount=Decimal("100.00")
        )
        return quote

    def publish(self, quote=None):
        return publish_quote(
            quote=quote or self.make_quote(),
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )

    def editor_data(self, quote, **overrides):
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

    def test_saved_forms_require_one_to_fifty_line_items(self):
        self.assertFalse(QuoteLineItemFormSet(line_formset_data()).is_valid())
        self.assertTrue(QuoteLineItemFormSet(line_formset_data("10.00")).is_valid())
        self.assertFalse(
            QuoteLineItemFormSet(line_formset_data(*(["1.00"] * 51))).is_valid()
        )

    def test_new_quote_rejects_formsetless_post_without_creating_quote(self):
        quote = self.make_quote()
        initial_count = Quote.objects.count()
        response = self.client.post(
            reverse("quotes:new"),
            self.editor_data(quote),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Quote.objects.count(), initial_count)
        self.assertIn("line item", response.content.decode().lower())

    def test_edit_quote_rejects_formsetless_post_without_mutating_quote(self):
        quote = self.make_quote()
        data = self.editor_data(quote, project_title="Must not save")
        response = self.client.post(
            reverse("quotes:edit", kwargs={"pk": quote.pk}),
            data,
        )
        self.assertEqual(response.status_code, 200)
        quote.refresh_from_db()
        self.assertEqual(quote.project_title, "A draft project")
        self.assertEqual(quote.line_items.count(), 1)
        self.assertIn("line item", response.content.decode().lower())

    def test_saved_draft_cannot_remain_without_a_line_item(self):
        quote = self.make_quote()
        quote.line_items.all().delete()
        with self.assertRaises(ValidationError):
            quote.save(update_fields=("project_title",))

    def test_validity_period_accepts_one_and_ninety_days_only(self):
        quote = self.make_quote()
        one_day = self.editor_data(
            quote, valid_until=(quote.issue_date + timedelta(days=1)).isoformat()
        )
        ninety_days = self.editor_data(
            quote, valid_until=(quote.issue_date + timedelta(days=90)).isoformat()
        )
        same_day = self.editor_data(quote, valid_until=quote.issue_date.isoformat())
        ninety_one_days = self.editor_data(
            quote, valid_until=(quote.issue_date + timedelta(days=91)).isoformat()
        )
        self.assertTrue(QuoteEditorForm(one_day, quote=quote).is_valid())
        self.assertTrue(QuoteEditorForm(ninety_days, quote=quote).is_valid())
        self.assertFalse(QuoteEditorForm(same_day, quote=quote).is_valid())
        self.assertFalse(QuoteEditorForm(ninety_one_days, quote=quote).is_valid())

    def test_quote_number_exhaustion_is_explicit(self):
        QuoteNumberSequence.objects.create(year=2026)
        models.QuerySet.update(
            QuoteNumberSequence.objects.filter(year=2026),
            last_allocated_number=9999,
        )
        with self.assertRaises(QuoteNumberExhausted):
            allocate_quote_number(now=timezone.now().replace(year=2026))

    def test_publication_creates_first_link_and_exposes_it_without_logging_token(self):
        quote = self.make_quote()
        with self.assertNoLogs("scopequote", level="INFO"):
            version = self.publish(quote)
        self.assertEqual(ShareLink.objects.filter(quote_version=version).count(), 1)
        link = version.share_link
        token = decrypt_link_token(link)
        self.assertTrue(token)

    def test_publication_rolls_back_when_first_link_creation_fails(self):
        quote = self.make_quote()
        with patch(
            "quotes.services.links.create_share_link",
            side_effect=RuntimeError("link storage failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.publish(quote)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertEqual(QuoteVersion.objects.filter(quote=quote).count(), 0)
        self.assertEqual(
            ShareLink.objects.filter(quote_version__quote=quote).count(), 0
        )

        version = self.publish()
        quote = version.quote
        models.QuerySet.update(
            Quote.objects.filter(pk=quote.pk), status=Quote.Status.ACCEPTED
        )
        with self.assertRaises(LinkLifecycleError):
            create_share_link(version=version, now=timezone.now())
        models.QuerySet.update(
            Quote.objects.filter(pk=quote.pk),
            status=Quote.Status.SENT,
            valid_until=timezone.localdate() - timedelta(days=1),
        )
        with self.assertRaises(LinkLifecycleError):
            create_share_link(version=version, now=timezone.now())

    def test_owner_link_management_uses_absolute_url_and_private_cache_headers(self):
        version = self.publish()
        response = self.client.get(
            reverse("quotes:links", kwargs={"pk": version.quote_id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "http://testserver/q/")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_public_page_uses_nonce_csp_and_preserves_accent_logo_in_pdf(self):
        quote = self.make_quote(accent_color="#FFFFFF")
        with (
            TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            stored = stage_logo(
                SimpleUploadedFile(
                    "brand.png",
                    self._image_bytes(),
                    content_type="image/png",
                ),
                owner_id=self.owner.pk,
            )
            quote.logo_object_key = stored.storage_key
            quote.logo_content_type = stored.content_type
            quote.logo_byte_size = stored.byte_size
            quote.save()
            version = self.publish(quote)
            self.assertTrue(version.logo_object_key)
            self.assertEqual(
                version.snapshot["logo_object_key"],
                version.logo_object_key,
            )
            self.assertTrue(
                authorized_logo_data_uri(
                    storage_key=version.logo_object_key,
                    owner_id=self.owner.pk,
                )
            )
            link = version.share_link
            token = decrypt_link_token(link)
            public_response = self.client.get(f"/q/{token}/view/")
            logo_response = self.client.get(f"/q/{token}/logo/")
            with patch("quotes.services.pdf.HTML") as html:
                html.return_value.write_pdf.return_value = b"%PDF-1.4"
                render_published_quote_pdf(version=version)
        content = public_response.content.decode()
        nonce_match = re.search(r'<style nonce="([^"]+)">', content)
        self.assertIsNotNone(nonce_match)
        nonce = nonce_match.group(1)
        self.assertIn(f"'nonce-{nonce}'", public_response["Content-Security-Policy"])
        self.assertIn(f'<style nonce="{nonce}">', content)
        self.assertNotIn(" style=", content)
        self.assertIn("#FFFFFF", content)
        self.assertIn("../logo/", content)
        self.assertIn("--scopequote-accent: #FFFFFF", html.call_args.kwargs["string"])
        self.assertIn("data:image/png;base64,", html.call_args.kwargs["string"])
        self.assertEqual(public_response.status_code, 200)
        self.assertEqual(logo_response.status_code, 200)
        self.assertEqual(logo_response["Cache-Control"], "no-store")

    def test_owner_detail_renders_escaped_response_history(self):
        quote = self.make_quote()
        version = self.publish(quote)
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=version,
            event_type=QuoteEvent.EventType.CHANGES_REQUESTED,
            note="<script>alert('x')</script>",
            response_idempotency_key=uuid.uuid4(),
            payload_fingerprint=response_fingerprint(
                action="changes_requested", note="<script>alert('x')</script>"
            ),
        )
        response = self.client.get(reverse("quotes:detail", kwargs={"pk": quote.pk}))
        self.assertContains(response, "&lt;script&gt;", status_code=200)
        self.assertNotContains(response, "<script>alert('x')</script>")

    def test_stale_edit_preserves_submitted_formset_and_renders_current_formset(self):
        quote = self.make_quote()
        current_item = quote.line_items.get()
        current_item.label = "Current saved line item"
        current_item.save(update_fields=("label",))
        stale_timestamp = "2000-01-01T00:00:00+00:00"
        submitted_items = line_formset_data("100.00")
        submitted_items["line_items-0-label"] = "Submitted unsaved line item"
        data = {
            **self.editor_data(quote, project_title="Submitted unsaved title"),
            **submitted_items,
            "updated_at": stale_timestamp,
        }
        response = self.client.post(
            reverse("quotes:edit", kwargs={"pk": quote.pk}), data
        )
        self.assertEqual(response.status_code, 409)
        content = response.content.decode()
        self.assertIn("Submitted unsaved title", content)
        self.assertIn("Submitted unsaved line item", content)
        self.assertIn("Current saved line item", content)
        self.assertEqual(
            response.context["current_formset"].forms[0].initial["label"],
            "Current saved line item",
        )

    def test_stale_publish_preserves_submitted_form_and_shows_current_state(self):
        quote = self.make_quote()
        response = self.client.post(
            reverse("quotes:publish", kwargs={"pk": quote.pk}),
            {
                "project_title": "Submitted publish title",
                "project_description": quote.project_description,
                "client_name": "Submitted client",
                "client_email": quote.client_email,
                "sender_name": quote.sender_name,
                "sender_email": quote.sender_email,
                "issue_date": quote.issue_date.isoformat(),
                "valid_until": quote.valid_until.isoformat(),
                "terms": quote.terms,
                "intentional_adjustment_confirmed": "on",
                "updated_at": "2000-01-01T00:00:00+00:00",
            },
        )
        self.assertEqual(response.status_code, 409)
        content = response.content.decode()
        self.assertIn("Submitted publish title", content)
        self.assertIn(quote.project_title, content)

        quote = self.make_quote(logo_object_key="quotes/logos/staging/shared.png")
        duplicate = duplicate_quote(quote=quote, user=self.owner)
        self.assertEqual(duplicate.logo_object_key, "")
        self.assertEqual(duplicate.logo_content_type, "")
        self.assertIsNone(duplicate.logo_byte_size)

    def test_website_validation_allows_localhost_only_in_debug(self):
        quote = self.make_quote()
        with override_settings(DEBUG=True):
            self.assertTrue(
                QuoteEditorForm(
                    self.editor_data(quote, sender_website="http://localhost:8000"),
                    quote=quote,
                ).is_valid()
            )
        self.assertFalse(
            QuoteEditorForm(
                self.editor_data(quote, sender_website="http://evil.example"),
                quote=quote,
            ).is_valid()
        )
        quote.sender_website = "http://evil.example"
        with self.assertRaises(ValidationError):
            quote.full_clean()

    def test_public_response_endpoint_is_database_rate_limited(self):
        with override_settings(
            PUBLIC_RESPONSE_RATE_LIMIT=1, PUBLIC_RESPONSE_RATE_WINDOW=60
        ):
            first = self.client.post("/q/not-a-real-token/response/")
            second = self.client.post("/q/not-a-real-token/response/")
        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
        self.assertNotIn("not-a-real-token", second.content.decode())

    def test_brand_foreground_meets_wcag_contrast_threshold(self):
        self.assertEqual(readable_foreground("#FFFFFF"), "#20231F")
        self.assertEqual(readable_foreground("#000000"), "#FFFFFF")
        self.assertGreaterEqual(
            contrast_ratio("#FFFFFF", readable_foreground("#FFFFFF")),
            Decimal("4.5"),
        )

        response = self.client.post(
            "/estimator/",
            {
                "description": (
                    "A complete responsive website with implementation, testing, "
                    "and handoff."
                ),
                "delivery_days": 5,
                "complexity": "standard",
                "revision_rounds": 0,
                "urgency": "standard",
                "experience": "intermediate",
                "currency": "USD",
            },
        )
        self.assertContains(response, "/quotes/new/")
        self.assertContains(response, "Save quote")

    def _image_bytes(self):
        output = BytesIO()
        Image.new("RGB", (8, 8), "#123456").save(output, format="PNG")
        return output.getvalue()
