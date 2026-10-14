import re
import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urljoin

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from pricing.constants import PRICING_CONFIG_VERSION
from quotes.forms import ClientResponseForm
from quotes.models import Quote, QuoteEvent, QuoteLineItem, ShareLink
from quotes.services.links import create_share_link, decrypt_link_token
from quotes.services.quotes import publish_quote

KEY = "c2NvcGVxdW90ZS1kZXYtb25seS1rZXktMzJieXRlcyE="


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=KEY,
    SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1",
)
class PublicQuoteFlowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "public-owner@example.com",
            password="correct horse battery staple",
        )
        self.owner.email_verified_at = timezone.now()
        self.owner.save(update_fields=("email_verified_at",))
        self.quote = Quote.objects.create(
            owner=self.owner,
            project_title="Immutable public project",
            project_description=(
                "A complete public project overview with enough detail to render "
                "the frozen scope and terms."
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
            minimum=Decimal("100.00"),
            recommended=Decimal("100.00"),
            maximum=Decimal("100.00"),
            chosen_total=Decimal("100.00"),
            hourly_equivalent=Decimal("5.00"),
            client_name="Public Client",
            client_company="Client Company",
            client_email="client@example.com",
            sender_name="Public Sender",
            sender_business_name="Sender Studio",
            sender_email="sender@example.com",
            sender_website="https://sender.example.com",
            sender_monogram="PS",
            issue_date=timezone.localdate(),
            valid_until=timezone.localdate() + timedelta(days=30),
            deposit_percentage=50,
            terms="Payment follows the agreed schedule.",
            timeline="Five focused delivery days.",
            revision_terms="One revision round is included.",
            notes="Please review the scope carefully.",
            exclusions="Hosting is not included.",
            taxes="Taxes are not included.",
            cancellation_terms="Either party may cancel before work begins.",
        )
        QuoteLineItem.objects.create(
            quote=self.quote,
            label="Project fee",
            description="The frozen project fee.",
            amount=Decimal("100.00"),
        )
        self.version = publish_quote(
            quote=self.quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        self.link = create_share_link(version=self.version, now=timezone.now())
        self.token = decrypt_link_token(self.link)

    def make_link(self):
        link = create_share_link(version=self.version, now=timezone.now())
        return link, decrypt_link_token(link)

    def csrf_token(self, client=None, path=None):
        client = client or self.client
        response = client.get(path or f"/q/{self.token}/")
        return response.cookies["csrftoken"].value

    def open_quote(self, client=None):
        client = client or self.client
        csrf = self.csrf_token(client)
        return client.post(
            f"/q/{self.token}/open/",
            HTTP_X_CSRFTOKEN=csrf,
        )

    def response_data(self, **overrides):
        data = {
            "action": "accepted",
            "note": "",
            "idempotency_key": str(uuid.uuid4()),
        }
        data.update(overrides)
        return data

    def post_response(self, client=None, **overrides):
        client = client or self.client
        self.link.refresh_from_db()
        csrf = None
        if self.link.is_active:
            self.open_quote(client)
            csrf = self.csrf_token(client, f"/q/{self.token}/view/")
        extra = {"HTTP_X_CSRFTOKEN": csrf} if csrf else {}
        return client.post(
            f"/q/{self.token}/response/",
            self.response_data(**overrides),
            **extra,
        )

    def test_null_origin_same_origin_browser_form_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        csrf = self.csrf_token(client)

        response = client.post(
            f"/q/{self.token}/open/",
            HTTP_X_CSRFTOKEN=csrf,
            HTTP_ORIGIN="null",
            HTTP_SEC_FETCH_SITE="same-origin",
            HTTP_SEC_FETCH_MODE="navigate",
            HTTP_SEC_FETCH_DEST="document",
        )

        self.assertEqual(response.status_code, 302)

    def test_null_origin_cross_site_browser_form_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        csrf = self.csrf_token(client)

        response = client.post(
            f"/q/{self.token}/open/",
            HTTP_X_CSRFTOKEN=csrf,
            HTTP_ORIGIN="null",
            HTTP_SEC_FETCH_SITE="cross-site",
            HTTP_SEC_FETCH_MODE="navigate",
            HTTP_SEC_FETCH_DEST="document",
        )

        self.assertEqual(response.status_code, 403)

    def test_public_forms_retain_no_referrer_and_csrf_tokens(self):
        interstitial = self.client.get(f"/q/{self.token}/")
        public_quote = self.client.get(f"/q/{self.token}/view/")

        self.assertContains(interstitial, "csrfmiddlewaretoken")
        self.assertContains(public_quote, "csrfmiddlewaretoken")
        self.assertNotContains(interstitial, 'referrerpolicy="same-origin"')
        self.assertNotContains(public_quote, 'referrerpolicy="same-origin"')
        self.assertEqual(interstitial["Referrer-Policy"], "no-referrer")
        self.assertEqual(public_quote["Referrer-Policy"], "no-referrer")

    def test_unknown_expired_and_revoked_tokens_share_generic_unavailable_state(self):
        active_content = self.client.get(f"/q/{self.token}/").content
        ShareLink.objects.filter(pk=self.link.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        expired = self.client.get(f"/q/{self.token}/")
        ShareLink.objects.filter(pk=self.link.pk).update(
            expires_at=timezone.now() + timedelta(days=1),
            revoked_at=timezone.now(),
        )
        revoked = self.client.get(f"/q/{self.token}/")
        unknown = self.client.get("/q/not-a-real-token/")

        self.assertEqual(expired.status_code, 404)
        self.assertEqual(revoked.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(expired.content, revoked.content)
        self.assertEqual(expired.content, unknown.content)
        self.assertNotEqual(active_content, expired.content)
        self.assertIn(b"This quotation link is unavailable.", expired.content)

    def test_interstitial_is_minimal_and_does_not_record_a_view(self):
        response = self.client.get(f"/q/{self.token}/")
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("Public Sender", content)
        self.assertIn("PS", content)
        self.assertIn(self.quote.quote_number, content)
        self.assertIn("Open secure quotation", content)
        self.assertNotIn(self.quote.project_description, content)
        self.assertNotIn(str(self.quote.pk), content)
        self.assertNotIn(str(self.version.pk), content)
        self.assertNotIn(str(self.link.pk), content)
        self.assertIsNone(self.link.first_viewed_at)
        self.assertEqual(
            QuoteEvent.objects.filter(
                share_link=self.link,
                event_type=QuoteEvent.EventType.VIEWED,
            ).count(),
            0,
        )
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_public_form_actions_are_token_free_relative_urls(self):
        interstitial = self.client.get(f"/q/{self.token}/")
        interstitial_content = interstitial.content.decode()
        self.open_quote()
        public_quote = self.client.get(f"/q/{self.token}/view/")
        public_content = public_quote.content.decode()
        invalid_response = self.post_response(action="maybe")
        invalid_response_content = invalid_response.content.decode()

        interstitial_actions = re.findall(
            r'<form[^>]*\baction="([^"]*)"',
            interstitial_content,
        )
        public_actions = re.findall(
            r'<form[^>]*\baction="([^"]*)"',
            public_content,
        )

        self.assertEqual(interstitial_actions, ["open/"])
        self.assertEqual(public_actions, ["../response/"])
        self.assertEqual(
            urljoin(f"/q/{self.token}/", interstitial_actions[0]),
            f"/q/{self.token}/open/",
        )
        self.assertEqual(
            urljoin(f"/q/{self.token}/view/", public_actions[0]),
            f"/q/{self.token}/response/",
        )
        self.assertNotIn(self.token, interstitial_content)
        self.assertNotIn(self.token, public_content)
        self.assertIn('action="../response/"', invalid_response_content)
        self.assertNotIn(self.token, invalid_response_content)

    def test_repeated_open_is_idempotent(self):
        client = Client(enforce_csrf_checks=True)
        csrf = self.csrf_token(client)
        first = client.post(
            f"/q/{self.token}/open/",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.link.refresh_from_db()
        first_viewed_at = self.link.first_viewed_at
        second = client.post(
            f"/q/{self.token}/open/",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.link.refresh_from_db()

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(first_viewed_at, self.link.first_viewed_at)
        self.assertEqual(
            QuoteEvent.objects.filter(
                share_link=self.link,
                event_type=QuoteEvent.EventType.VIEWED,
            ).count(),
            1,
        )

    def test_error_summary_has_a_focusable_semantic_target(self):
        response = self.post_response(action="maybe")
        content = response.content.decode()

        self.assertEqual(response.status_code, 400)
        self.assertIn('role="alert"', content)
        self.assertIn('id="response-error-summary"', content)
        self.assertIn('id="response-error-title"', content)
        self.assertIn('tabindex="-1"', content)
        self.assertIn("response-error-title", content)
        self.assertNotIn("autofocus", content)

    def test_public_page_uses_frozen_snapshot_after_current_quote_mutation(self):
        self.open_quote()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE quotes_quote
                SET project_title = %s,
                    project_description = %s,
                    chosen_total = %s
                WHERE id = %s
                """,
                [
                    "Mutable title after publication",
                    "Mutable description after publication.",
                    "999.00",
                    self.quote.pk,
                ],
            )
        self.quote.refresh_from_db()
        response = self.client.get(f"/q/{self.token}/view/")
        content = response.content.decode()

        self.assertEqual(self.quote.project_title, "Mutable title after publication")
        self.assertIn("Immutable public project", content)
        self.assertIn("A complete public project overview", content)
        self.assertIn("100.00 USD", content)
        self.assertNotIn("Mutable title after publication", content)
        self.assertNotIn("Mutable description after publication.", content)

    def test_open_post_records_first_view_and_redirects_to_read_only_page(self):
        client = Client(enforce_csrf_checks=True)
        without_csrf = client.post(f"/q/{self.token}/open/")
        csrf = self.csrf_token(client)
        response = client.post(
            f"/q/{self.token}/open/",
            HTTP_X_CSRFTOKEN=csrf,
        )

        self.assertEqual(without_csrf.status_code, 403)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/q/{self.token}/view/")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.link.refresh_from_db()
        self.quote.refresh_from_db()
        self.assertIsNotNone(self.link.first_viewed_at)
        self.assertEqual(self.quote.status, Quote.Status.VIEWED)
        self.assertEqual(
            QuoteEvent.objects.filter(
                share_link=self.link,
                event_type=QuoteEvent.EventType.VIEWED,
            ).count(),
            1,
        )

    def test_read_only_view_renders_snapshot_without_owner_controls_or_ids(self):
        self.open_quote()
        response = self.client.get(f"/q/{self.token}/view/")
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn(self.quote.project_title, content)
        self.assertIn("100.00 USD", content)
        self.assertIn("Payment follows the agreed schedule.", content)
        self.assertIn("Valid until", content)
        self.assertIn("Scope", content)
        self.assertIn("Accept quotation", content)
        self.assertIn("Request changes", content)
        self.assertNotIn("Edit draft", content)
        self.assertNotIn("Manage private links", content)
        self.assertNotIn("Publish", content)
        self.assertNotIn(str(self.quote.pk), content)
        self.assertNotIn(str(self.version.pk), content)
        self.assertNotIn(str(self.link.pk), content)

    def test_response_post_requires_csrf_and_accepts_valid_action(self):
        client = Client(enforce_csrf_checks=True)
        self.open_quote(client)
        csrf = self.csrf_token(client, f"/q/{self.token}/view/")
        without_csrf = client.post(
            f"/q/{self.token}/response/",
            self.response_data(),
        )
        response = client.post(
            f"/q/{self.token}/response/",
            self.response_data(),
            HTTP_X_CSRFTOKEN=csrf,
        )

        self.assertEqual(without_csrf.status_code, 403)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Quotation accepted.", response.content.decode())
        self.assertIn("Status: Accepted", response.content.decode())
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
        self.assertEqual(
            QuoteEvent.objects.filter(
                event_type=QuoteEvent.EventType.ACCEPTED,
            ).count(),
            1,
        )

    def test_response_form_rejects_invalid_actions_and_long_notes(self):
        self.open_quote()
        invalid_action = self.post_response(action="maybe")
        long_note = self.post_response(note="x" * 2001)

        self.assertEqual(invalid_action.status_code, 400)
        self.assertEqual(long_note.status_code, 400)
        self.assertIn("Choose a valid response", invalid_action.content.decode())
        self.assertIn("2000", long_note.content.decode())
        self.assertEqual(
            QuoteEvent.objects.filter(
                event_type__in=[
                    QuoteEvent.EventType.ACCEPTED,
                    QuoteEvent.EventType.CHANGES_REQUESTED,
                ]
            ).count(),
            0,
        )

    def test_request_changes_escapes_note_and_sets_terminal_status(self):
        response = self.post_response(
            action="maybe",
            note="<script>alert('x')</script>",
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("<script>alert", response.content.decode())
        self.assertIn("&lt;script&gt;", response.content.decode())
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.VIEWED)
        self.assertEqual(
            QuoteEvent.objects.filter(
                event_type=QuoteEvent.EventType.CHANGES_REQUESTED,
            ).count(),
            0,
        )

    def test_valid_request_changes_sets_terminal_status(self):
        response = self.post_response(
            action="changes_requested",
            note="Please revise the scope.",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Changes requested.", response.content.decode())
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.CHANGES_REQUESTED)
        event = QuoteEvent.objects.get(
            event_type=QuoteEvent.EventType.CHANGES_REQUESTED,
        )
        self.assertEqual(event.note, "Please revise the scope.")

    def test_acceptance_revokes_other_active_links(self):
        second_link, _second_token = self.make_link()
        response = self.post_response()
        second_link.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(second_link.revoked_at)
        self.assertEqual(second_link.state, "revoked")

    def test_same_key_retry_after_revocation_returns_existing_result(self):
        key = uuid.uuid4()
        first = self.post_response(idempotency_key=key)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.revoked_at)
        retry = self.client.post(
            f"/q/{self.token}/response/",
            self.response_data(idempotency_key=key),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertIn("Quotation accepted.", retry.content.decode())
        self.assertEqual(
            QuoteEvent.objects.filter(response_idempotency_key=key).count(),
            1,
        )

    def test_same_key_retry_after_expiry_returns_existing_result(self):
        key = uuid.uuid4()
        first = self.post_response(
            action="changes_requested",
            idempotency_key=key,
            note="Original",
        )
        ShareLink.objects.filter(pk=self.link.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        retry = self.post_response(
            action="changes_requested",
            idempotency_key=key,
            note="Original",
        )
        conflict = self.post_response(
            action="changes_requested",
            idempotency_key=key,
            note="Changed",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            QuoteEvent.objects.filter(response_idempotency_key=key).count(),
            1,
        )

    def test_conflicting_payload_or_second_response_returns_conflict(self):
        key = uuid.uuid4()
        self.post_response(idempotency_key=key, note="Original")
        changed = self.post_response(idempotency_key=key, note="Changed")
        second = self.post_response()

        self.assertEqual(changed.status_code, 409)
        self.assertEqual(second.status_code, 404)
        self.assertIn("already been recorded", changed.content.decode())
        self.assertIn("This quotation link is unavailable.", second.content.decode())
        self.assertEqual(
            QuoteEvent.objects.filter(
                event_type__in=[
                    QuoteEvent.EventType.ACCEPTED,
                    QuoteEvent.EventType.CHANGES_REQUESTED,
                ]
            ).count(),
            1,
        )

    def test_client_response_form_exposes_only_action_note_and_uuid_key(self):
        form = ClientResponseForm(
            data=self.response_data(action="request-changes", note="Please revise"),
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            set(form.fields),
            {"action", "note", "idempotency_key"},
        )
        self.assertEqual(form.cleaned_data["action"], "changes_requested")
        self.assertEqual(form.cleaned_data["note"], "Please revise")
        self.assertIsInstance(form.cleaned_data["idempotency_key"], uuid.UUID)
