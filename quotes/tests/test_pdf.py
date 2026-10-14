import re
import shutil
import subprocess
import time
from datetime import date
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from pricing.constants import PRICING_CONFIG_VERSION
from quotes.models import Quote, QuoteLineItem
from quotes.services.links import create_share_link, decrypt_link_token
from quotes.services.pdf import (
    MAX_PDF_INPUT_BYTES,
    PdfRenderError,
    render_draft_quote_pdf,
)
from quotes.services.quotes import publish_quote

User = get_user_model()
PDFINFO = shutil.which("pdfinfo")
PDFTOTEXT = shutil.which("pdftotext")
ENCRYPTION_KEY = "c2NvcGVxdW90ZS1kZXYtb25seS1rZXktMzJieXRlcyE="


def extract_pdf_text(pdf):
    result = subprocess.run(
        [PDFTOTEXT, "-layout", "-", "-"],
        input=pdf,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8")


def extract_pdf_page_count(pdf):
    result = subprocess.run(
        [PDFINFO, "-"],
        input=pdf,
        check=True,
        capture_output=True,
    )
    match = re.search(rb"^Pages:\s+(\d+)$", result.stdout, re.MULTILINE)
    if match is None:
        raise AssertionError("pdfinfo did not report a page count")
    return int(match.group(1))


def guest_form_data(**overrides):
    data = {
        "description": (
            "Create a responsive website with discovery, implementation, testing, "
            "and a clear handoff."
        ),
        "delivery_days": 5,
        "complexity": "standard",
        "revision_rounds": 0,
        "urgency": "standard",
        "experience": "intermediate",
        "currency": "USD",
    }
    data.update(overrides)
    return data


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=ENCRYPTION_KEY,
    SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1",
)
class QuotePdfViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "pdf-owner@example.com",
            password="correct horse battery staple",
        )
        self.owner.email_verified_at = timezone.now()
        self.owner.save(update_fields=("email_verified_at",))
        self.quote = self.make_quote()
        self.client.force_login(self.owner)

    def make_quote(self, **overrides):
        values = {
            "owner": self.owner,
            "project_title": "Printable project",
            "project_description": (
                "A complete project overview with enough detail to review the "
                "scope, schedule, and commercial terms."
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
            "client_name": "Print Client",
            "client_company": "Print Client Company",
            "client_email": "client@example.com",
            "sender_name": "Print Sender",
            "sender_business_name": "Sender Studio",
            "sender_email": "sender@example.com",
            "sender_website": "https://sender.example.com",
            "sender_monogram": "PS",
            "issue_date": date(2026, 9, 24),
            "valid_until": date(2026, 10, 24),
            "deposit_percentage": 50,
            "terms": "Payment follows the agreed schedule.",
            "timeline": "Five focused delivery days.",
            "revision_terms": "One revision round is included.",
        }
        values.update(overrides)
        quote = Quote.objects.create(**values)
        QuoteLineItem.objects.create(
            quote=quote,
            label="Project fee",
            description="The complete project fee.",
            amount=Decimal("100.00"),
        )
        return quote

    def test_owner_draft_pdf_is_an_attachment_marked_draft(self):
        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="scopequote-draft.pdf"',
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertEqual(Quote.objects.get(pk=self.quote.pk).status, Quote.Status.DRAFT)
        if PDFTOTEXT is not None:
            text = extract_pdf_text(response.content)
            self.assertIn("DRAFT", text)
            self.assertIn("Printable project", text)
            self.assertIn("Print Client", text)
            self.assertIn("Print Sender", text)
            self.assertIn("100.00 USD", text)
            self.assertIn("50.00 USD", text)
            self.assertIn("Payment follows the agreed schedule.", text)

    def test_pdf_input_size_bound_rejects_before_weasyprint(self):
        with (
            patch(
                "quotes.services.pdf.render_to_string",
                return_value="x" * (MAX_PDF_INPUT_BYTES + 1),
            ),
            patch("quotes.services.pdf.HTML") as html,
        ):
            with self.assertRaises(PdfRenderError):
                render_draft_quote_pdf(quote=self.quote)
        html.assert_not_called()

    def test_pdf_render_timeout_is_reported_as_a_retryable_render_error(self):
        with patch(
            "quotes.services.pdf.HTML.write_pdf",
            side_effect=TimeoutError("render timed out"),
        ):
            with self.assertRaises(PdfRenderError):
                render_draft_quote_pdf(quote=self.quote)

    def test_pdf_render_timeout_stops_slow_weasyprint(self):
        def slow_write_pdf(*_args, **_kwargs):
            time.sleep(2)
            return b"%PDF-1.4"

        with (
            override_settings(PDF_RENDER_TIMEOUT_SECONDS=1),
            patch("quotes.services.pdf.HTML.write_pdf", side_effect=slow_write_pdf),
        ):
            with self.assertRaises(PdfRenderError):
                render_draft_quote_pdf(quote=self.quote)

    def test_owner_published_pdf_uses_frozen_version_commercial_data(self):
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )

        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="scopequote-{self.quote.quote_number}.pdf"',
        )
        if PDFTOTEXT is not None:
            text = extract_pdf_text(response.content)
            self.assertIn(self.quote.quote_number, text)
            self.assertIn("Print Client", text)
            self.assertIn("Print Sender", text)
            self.assertIn("100.00 USD", text)
            self.assertIn("50.00 USD", text)
            self.assertIn("2026-10-24", text)
            self.assertIn("Payment follows the agreed schedule.", text)
            self.assertNotIn("DRAFT", text)

    def test_published_pdf_and_public_page_remain_isolated_from_mutable_quote(self):
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        link = create_share_link(
            version=self.quote.current_published_version,
            now=timezone.now(),
        )
        token = decrypt_link_token(link)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE quotes_quote
                SET project_title = %s,
                    project_description = %s,
                    chosen_total = %s,
                    valid_until = %s,
                    terms = %s
                WHERE id = %s
                """,
                [
                    "Mutable title",
                    "Mutable description.",
                    Decimal("999.00"),
                    date(2026, 12, 31),
                    "Mutable terms.",
                    self.quote.pk,
                ],
            )
        self.quote.refresh_from_db()

        public_response = self.client.get(f"/q/{token}/view/")
        pdf_response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")

        self.assertEqual(public_response.status_code, 200)
        self.assertEqual(pdf_response.status_code, 200)
        public_content = public_response.content.decode()
        self.assertIn("Printable project", public_content)
        self.assertIn("100.00 USD", public_content)
        self.assertIn("2026-10-24", public_content)
        self.assertIn("Payment follows the agreed schedule.", public_content)
        self.assertNotIn("Mutable title", public_content)
        self.assertNotIn("Mutable description.", public_content)
        self.assertNotIn("Mutable terms.", public_content)
        if PDFTOTEXT is not None:
            pdf_text = extract_pdf_text(pdf_response.content)
            self.assertIn("Printable project", pdf_text)
            self.assertIn("100.00 USD", pdf_text)
            self.assertIn("2026-10-24", pdf_text)
            self.assertIn("Payment follows the agreed schedule.", pdf_text)
            self.assertNotIn("Mutable title", pdf_text)
            self.assertNotIn("Mutable description.", pdf_text)
            self.assertNotIn("Mutable terms.", pdf_text)

    @skipUnless(PDFINFO is not None, "pdfinfo is required")
    @skipUnless(PDFTOTEXT is not None, "pdftotext is required")
    def test_long_unbroken_url_and_multi_page_content_are_preserved(self):
        long_url = "https://example.com/" + "a" * 420
        self.quote.project_description = " ".join(
            f"Scope paragraph {index:03d} contains implementation and review detail."
            for index in range(45)
        )
        self.quote.terms = " ".join(
            f"Commercial term {index:03d} remains part of the frozen offer."
            for index in range(35)
        )
        self.quote.sender_website = long_url
        self.quote.save(
            update_fields=("project_description", "terms", "sender_website")
        )
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )

        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")

        self.assertEqual(response.status_code, 200)
        page_count = extract_pdf_page_count(response.content)
        text = extract_pdf_text(response.content)
        self.assertGreater(page_count, 1)
        self.assertIn("Scope paragraph 044", text)
        self.assertIn("Commercial term 034", text)
        unwrapped_text = re.sub(r"\s+", "", text)
        self.assertIn("https://example.com/" + "a" * 420, unwrapped_text)
        self.assertRegex(text, rf"Page\s+1\s+of\s+{page_count}")

    def test_published_pdf_rejects_unauthorized_owner_branch(self):
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        other_owner = User.objects.create_user(
            "pdf-unauthorized@example.com",
            password="correct horse battery staple",
        )
        self.client.force_login(other_owner)

        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")

        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Printable project", status_code=404)

    @skipUnless(PDFTOTEXT is not None, "pdftotext is required")
    def test_published_pdf_uses_exact_client_name_and_excludes_mutated_sentinel(self):
        self.quote.client_name = "Exact Client Sentinel"
        self.quote.save(update_fields=("client_name",))
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        sentinel = "MUTATED_SENTINEL_SHOULD_NOT_APPEAR"
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE quotes_quote SET client_name = %s WHERE id = %s",
                [sentinel, self.quote.pk],
            )
        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")
        text = extract_pdf_text(response.content)
        self.assertIn("Exact Client Sentinel", text)
        self.assertNotIn(sentinel, text)

    @skipUnless(PDFINFO is not None, "pdfinfo is required")
    @skipUnless(PDFTOTEXT is not None, "pdftotext is required")
    def test_published_pdf_uses_a4_and_numbers_later_pages(self):
        self.quote.project_description = " ".join(
            f"Detailed scope paragraph {index:03d} for pagination."
            for index in range(45)
        )
        self.quote.save(update_fields=("project_description",))
        publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")
        page_count = extract_pdf_page_count(response.content)
        text = extract_pdf_text(response.content)
        info = subprocess.run(
            [PDFINFO, "-"],
            input=response.content,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        self.assertGreater(page_count, 1)
        self.assertIn("A4", info)
        self.assertRegex(text, rf"Page\s+2\s+of\s+{page_count}")

        other_owner = User.objects.create_user(
            "pdf-other-owner@example.com",
            password="correct horse battery staple",
        )
        self.client.force_login(other_owner)

        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")

        self.assertEqual(response.status_code, 404)

    def test_owner_pdf_requires_authentication(self):
        self.client.logout()

        response = self.client.get(f"/quotes/{self.quote.pk}/pdf/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])


class GuestPdfViewTests(TestCase):
    def seed_guest_quote(self, **overrides):
        estimate_response = self.client.post(
            "/estimator/",
            guest_form_data(**overrides),
        )
        self.assertEqual(estimate_response.status_code, 200)
        nonce = self.client.session["scopequote.conversion_nonce"]
        quote_response = self.client.post(
            "/quotes/guest/",
            {"nonce": nonce},
        )
        self.assertEqual(quote_response.status_code, 302)

    @skipUnless(PDFTOTEXT is not None, "pdftotext is required")
    def test_validated_guest_quote_downloads_a_draft_pdf(self):
        self.seed_guest_quote()

        response = self.client.get("/quotes/guest.pdf/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="scopequote-guest-draft.pdf"',
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        text = extract_pdf_text(response.content)
        self.assertIn("DRAFT", text)
        self.assertIn("Create a responsive website", text)
        self.assertIn("1320 USD", text)

    def test_weasyprint_failure_returns_retryable_error_without_losing_guest_state(
        self,
    ):
        self.seed_guest_quote()
        successful_pdf = self.client.get("/quotes/guest.pdf/").content
        retry_client = Client(raise_request_exception=False)
        retry_client.cookies[settings.SESSION_COOKIE_NAME] = self.client.cookies[
            settings.SESSION_COOKIE_NAME
        ].value

        with patch(
            "quotes.services.pdf.HTML.write_pdf",
            side_effect=[RuntimeError("renderer failed"), successful_pdf],
        ):
            failed = retry_client.get("/quotes/guest.pdf/")
            retried = retry_client.get("/quotes/guest.pdf/")

        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed["Cache-Control"], "no-store")
        self.assertContains(
            failed,
            "The PDF could not be generated. Please try again.",
            status_code=503,
        )
        self.assertIn("scopequote.guest_quote", retry_client.session)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried["Content-Type"], "application/pdf")
        self.assertEqual(retried.content, successful_pdf)

    def test_guest_pdf_without_a_valid_quote_is_unavailable(self):
        response = self.client.get("/quotes/guest.pdf/")

        self.assertEqual(response.status_code, 404)
        self.assertContains(
            response,
            "Create a guest quotation and try again.",
            status_code=404,
        )
        self.assertEqual(Quote.objects.count(), 0)

    def test_expired_guest_quote_cannot_be_rendered(self):
        self.seed_guest_quote()
        session = self.client.session
        guest_quote = session["scopequote.guest_quote"]
        guest_quote["expires_at"] = "2000-01-01T00:00:00+00:00"
        session["scopequote.guest_quote"] = guest_quote
        session.save()

        response = self.client.get("/quotes/guest.pdf/")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("scopequote.guest_quote", self.client.session)
        self.assertEqual(Quote.objects.count(), 0)
