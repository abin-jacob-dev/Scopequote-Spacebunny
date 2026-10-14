import os
import subprocess
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import DeletionTombstone, Profile
from accounts.services import (
    client_ip,
    hard_delete_account,
    replay_restore_state,
)
from quotes.models import Quote, QuoteEvent, QuoteLineItem, ShareLink
from quotes.services.pdf import MAX_PDF_OUTPUT_BYTES
from quotes.services.quotes import publish_quote
from quotes.services.storage import MediaCleanupError, delete_object

User = get_user_model()
ROOT = Path(__file__).resolve().parents[2]
KEY = "c2NvcGVxdW90ZS1kZXYtb25seS1rZXktMzJieXRlcyE="
PASSWORD = "correct horse battery staple"


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (8, 8), "#123456").save(output, format="PNG")
    return output.getvalue()


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=KEY, SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1"
)
class FinalAuthAndOperationsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "ops-owner@example.com", password=PASSWORD
        )
        self.owner.email_verified_at = timezone.now()
        self.owner.save(update_fields=("email_verified_at",))
        self.client = Client()
        self.factory = RequestFactory()

    def test_signup_duplicate_returns_generic_creation_response(self):
        User.objects.create_user("existing@example.com", password=PASSWORD)
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": " EXISTING@example.com ",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotContains(self.client.get(response.url), "already exists")
        self.assertEqual(User.objects.filter(email="existing@example.com").count(), 1)

    def test_login_has_independent_account_and_ip_rate_buckets(self):
        fixed_now = timezone.now()
        with (
            patch("accounts.views.LOGIN_RATE_LIMIT", 1),
            patch("accounts.views.AUTHENTICATION_RATE_WINDOW", 60),
            patch("accounts.services._aware_now", return_value=fixed_now),
        ):
            first = self.client.post(
                reverse("accounts:login"),
                {"email": self.owner.email, "password": PASSWORD},
                REMOTE_ADDR="198.51.100.10",
            )
            second = self.client.post(
                reverse("accounts:login"),
                {"email": self.owner.email, "password": PASSWORD},
                REMOTE_ADDR="198.51.100.11",
            )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)

    def test_login_ip_bucket_limits_distinct_accounts_from_one_address(self):
        other = User.objects.create_user("other-login@example.com", password=PASSWORD)
        fixed_now = timezone.now()
        with (
            patch("accounts.views.LOGIN_RATE_LIMIT", 1),
            patch("accounts.views.AUTHENTICATION_RATE_WINDOW", 60),
            patch("accounts.services._aware_now", return_value=fixed_now),
        ):
            first = self.client.post(
                reverse("accounts:login"),
                {"email": self.owner.email, "password": PASSWORD},
                REMOTE_ADDR="198.51.100.20",
            )
            second = self.client.post(
                reverse("accounts:login"),
                {"email": other.email, "password": PASSWORD},
                REMOTE_ADDR="198.51.100.20",
            )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)

    def test_forwarded_ip_is_trusted_only_from_configured_proxy(self):
        request = self.factory.get(
            "/",
            HTTP_X_FORWARDED_FOR="203.0.113.8, 10.0.0.2",
            REMOTE_ADDR="10.0.0.2",
        )
        with override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=[]):
            self.assertEqual(client_ip(request), "10.0.0.2")
        with override_settings(
            TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=["10.0.0.2"]
        ):
            self.assertEqual(client_ip(request), "203.0.113.8")

    def test_profile_logo_save_failure_compensates_staged_object(self):
        self.client.force_login(self.owner)
        with tempfile_media_root() as media_root:
            Profile.create_for_user(self.owner)
            failed_client = Client(raise_request_exception=True)
            failed_client.force_login(self.owner)
            with patch.object(
                Profile, "save", side_effect=RuntimeError("db unavailable")
            ):
                response = failed_client.post(
                    reverse("accounts:profile"),
                    {
                        "full_name": "Ops",
                        "business_name": "",
                        "phone": "",
                        "website": "",
                        "default_currency": "USD",
                        "default_deposit_percentage": 50,
                        "default_quote_validity_days": 30,
                        "default_accent_color": "#E85D3F",
                        "logo": SimpleUploadedFile(
                            "logo.png", image_bytes(), content_type="image/png"
                        ),
                    },
                )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(list(Path(media_root).rglob("*.png")), [])

    def test_profile_save_compensation_surfaces_cleanup_queue_failure(self):
        self.client.force_login(self.owner)
        with tempfile_media_root() as media_root:
            Profile.create_for_user(self.owner)
            failed_client = Client(raise_request_exception=True)
            failed_client.force_login(self.owner)
            with (
                patch.object(
                    Profile,
                    "save",
                    side_effect=RuntimeError("db unavailable"),
                ),
                patch(
                    "quotes.services.storage.default_storage.delete",
                    side_effect=OSError("storage unavailable"),
                ),
                patch(
                    "accounts.models.MediaCleanupTask.objects.get_or_create",
                    side_effect=RuntimeError("queue unavailable"),
                ),
            ):
                response = failed_client.post(
                    reverse("accounts:profile"),
                    {
                        "full_name": "Ops",
                        "business_name": "",
                        "phone": "",
                        "website": "",
                        "default_currency": "USD",
                        "default_deposit_percentage": 50,
                        "default_quote_validity_days": 30,
                        "default_accent_color": "#E85D3F",
                        "logo": SimpleUploadedFile(
                            "logo.png", image_bytes(), content_type="image/png"
                        ),
                    },
                )
            self.assertEqual(response.status_code, 503)
            self.assertIn("retry", response.content.decode().lower())
            self.assertTrue(Path(media_root).exists())

    def test_cleanup_queue_persistence_failure_is_visible(self):
        key = f"profiles/{self.owner.pk}/unreachable.png"
        with patch(
            "accounts.models.MediaCleanupTask.objects.get_or_create",
            side_effect=RuntimeError("db unavailable"),
        ):
            with patch(
                "quotes.services.storage.default_storage.delete",
                side_effect=OSError("storage unavailable"),
            ):
                with self.assertRaises(MediaCleanupError):
                    delete_object(key)

    def test_restore_replay_reconciles_authoritative_response_status(self):
        quote = self._published_quote()
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=quote.current_published_version,
            event_type=QuoteEvent.EventType.ACCEPTED,
            note="",
            response_idempotency_key=uuid.uuid4(),
            payload_fingerprint="a" * 64,
        )
        from django.db import models

        models.QuerySet.update(
            Quote.objects.filter(pk=quote.pk), status=Quote.Status.SENT
        )
        result = replay_restore_state(now=timezone.now())
        self.assertTrue(result.ready)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.ACCEPTED)

    def test_restore_replay_preserves_applied_timestamp_on_noop(self):
        applied_at = timezone.now() - timedelta(hours=2)
        DeletionTombstone.objects.create(
            account_id=self.owner.pk,
            deleted_at=timezone.now() - timedelta(days=1),
            restore_status="applied",
            restore_applied_at=applied_at,
        )
        replay_restore_state(now=timezone.now())
        tombstone = DeletionTombstone.objects.get(account_id=self.owner.pk)
        self.assertEqual(tombstone.restore_applied_at, applied_at)

    def test_readiness_is_blocked_until_restore_replay_completes(self):
        DeletionTombstone.objects.create(
            account_id=self.owner.pk,
            deleted_at=timezone.now(),
        )
        response = self.client.get("/ready/")
        self.assertEqual(response.status_code, 503)
        replay_restore_state(now=timezone.now())
        response = self.client.get("/ready/")
        self.assertEqual(response.status_code, 200)

    def test_cleanup_dry_run_excludes_already_reconciled_expiry_events(self):
        quote = self._published_quote()
        link = ShareLink.objects.filter(
            quote_version=quote.current_published_version
        ).first()
        expired_at = timezone.now() - timedelta(seconds=1)
        ShareLink.objects.filter(pk=link.pk).update(expires_at=expired_at)
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=quote.current_published_version,
            share_link=link,
            event_type=QuoteEvent.EventType.EXPIRED,
            event_time=expired_at,
        )
        output = __import__("io").StringIO()
        call_command(
            "cleanup_expired_data", "--dry-run", now=timezone.now(), stdout=output
        )
        self.assertIn("expired links: 0", output.getvalue())

    def test_cleanup_apply_honors_batch_size_for_expiry_events(self):
        first_quote = self._published_quote(title="First")
        second_quote = self._published_quote(title="Second")
        now = timezone.now()
        ShareLink.objects.filter(
            quote_version__quote_id__in=[first_quote.pk, second_quote.pk]
        ).update(expires_at=now - timedelta(seconds=1))
        call_command("cleanup_expired_data", "--apply", "--batch-size", "1", now=now)
        self.assertEqual(
            QuoteEvent.objects.filter(event_type=QuoteEvent.EventType.EXPIRED).count(),
            1,
        )

    def test_hard_delete_account_uses_configured_quote_batches(self):
        self._draft_quote()
        self._draft_quote()
        from accounts import services as account_services

        with patch.object(
            account_services,
            "_delete_quote_batch",
            wraps=account_services._delete_quote_batch,
        ) as delete_batch:
            result = hard_delete_account(
                account_id=self.owner.pk,
                batch_size=1,
            )
        self.assertEqual(result, 1)
        self.assertEqual(delete_batch.call_count, 2)
        self.assertTrue(
            all(len(call.args[1]) == 1 for call in delete_batch.call_args_list)
        )

    def test_pdf_endpoint_throttles_and_rejects_oversized_output(self):
        self.client.force_login(self.owner)
        with override_settings(PDF_RATE_LIMIT=1, PDF_RATE_WINDOW=60):
            first = self.client.get(f"/quotes/{self._draft_quote().pk}/pdf/")
            second = self.client.get(f"/quotes/{self._draft_quote().pk}/pdf/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        with (
            patch("quotes.views.enforce_rate_limit"),
            patch(
                "quotes.services.pdf.HTML.write_pdf",
                return_value=b"%PDF-" + (b"x" * (MAX_PDF_OUTPUT_BYTES + 1)),
            ),
        ):
            response = self.client.get(f"/quotes/{self._draft_quote().pk}/pdf/")
        self.assertEqual(response.status_code, 503)

    def _draft_quote(self):
        quote = Quote.objects.create(
            owner=self.owner,
            project_title="Ops draft",
            project_description=(
                "A complete project overview with enough detail for review and "
                "publication."
            ),
            effective_hours=Decimal("20.00"),
            minimum=Decimal("100.00"),
            recommended=Decimal("100.00"),
            maximum=Decimal("100.00"),
            chosen_total=Decimal("100.00"),
            hourly_equivalent=Decimal("5.00"),
            client_name="Client",
            client_email="client@example.com",
            sender_name="Sender",
            sender_email="sender@example.com",
            issue_date=date.today(),
            valid_until=date.today() + timedelta(days=30),
            terms="Payment follows the agreed schedule.",
        )
        QuoteLineItem.objects.create(
            quote=quote, label="Project fee", amount=Decimal("100.00")
        )
        return quote

    def _published_quote(self, title="Ops quote"):
        quote = self._draft_quote()
        quote.project_title = title
        quote.save(update_fields=("project_title",))
        version = publish_quote(
            quote=quote,
            user=self.owner,
            intentional_adjustment_confirmed=True,
        )
        quote.refresh_from_db()
        quote.current_published_version = version
        return quote


class tempfile_media_root:
    def __enter__(self):
        from tempfile import TemporaryDirectory

        self.directory = TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.directory.name)
        self.override.enable()
        return self.directory.name

    def __exit__(self, *_args):
        self.override.disable()
        self.directory.cleanup()


class ProductionConfigurationTests(TestCase):
    def _run_settings(self, **overrides):
        environment = os.environ.copy()
        environment.update(
            {
                "SCOPEQUOTE_ENV": "production",
                "DEBUG": "false",
                "SECRET_KEY": "production-secret-that-is-long-enough",
                "ALLOWED_HOSTS": "quote.example.com",
                "SHARE_TOKEN_ENCRYPTION_KEY": KEY,
                "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                "EMAIL_HOST": "smtp.example.com",
                "EMAIL_PORT": "587",
                "DEFAULT_FROM_EMAIL": "quotes@quote.example.com",
                "SERVER_EMAIL": "quotes@quote.example.com",
                "EMAIL_SENDER_VERIFIED": "true",
                "EMAIL_USE_TLS": "true",
                "EMAIL_USE_SSL": "false",
                "POSTGRES_DB": "quotes",
                "POSTGRES_USER": "quotes",
                "POSTGRES_PASSWORD": "password",
                "POSTGRES_HOST": "localhost",
            }
        )
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", "import scopequote.settings"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )

    def test_production_requires_secure_mutually_exclusive_smtp_transport(self):
        insecure = self._run_settings(EMAIL_USE_TLS="false", EMAIL_USE_SSL="false")
        both = self._run_settings(EMAIL_USE_TLS="true", EMAIL_USE_SSL="true")
        self.assertNotEqual(insecure.returncode, 0)
        self.assertNotEqual(both.returncode, 0)

    def test_production_rejects_placeholder_or_unverified_sender(self):
        placeholder = self._run_settings(DEFAULT_FROM_EMAIL="hello@example.com")
        unverified = self._run_settings(EMAIL_SENDER_VERIFIED="false")
        self.assertNotEqual(placeholder.returncode, 0)
        self.assertNotEqual(unverified.returncode, 0)

    def test_example_and_compose_keep_environment_contracts_separate(self):
        example = (ROOT / ".env.example").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertNotIn("DATABASE_URL=", example)
        self.assertNotIn("E2E_PYTHON=python", example)
        self.assertNotIn("E2E_BROWSER_CHANNEL=chrome", example)
        self.assertNotIn('SCOPEQUOTE_ENV: "${SCOPEQUOTE_ENV:-test}"', compose)
        self.assertNotIn(
            'SECURE_SSL_REDIRECT: "${SECURE_SSL_REDIRECT:-false}"', compose
        )


if __name__ == "__main__":
    pass
