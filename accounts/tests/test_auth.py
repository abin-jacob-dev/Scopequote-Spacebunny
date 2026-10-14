from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode

from accounts.models import DeletionTombstone, Profile, RateLimitBucket, User
from accounts.services import RateLimitExceeded, enforce_rate_limit
from quotes.models import Quote, QuoteLineItem
from quotes.services.guest import (
    CONVERSION_NONCE_KEY,
    GUEST_ESTIMATE_KEY,
    GUEST_QUOTE_KEY,
)

PASSWORD = "correct horse battery staple"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="security@example.com",
)
class AuthenticationFlowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("owner@example.com", password=PASSWORD)

    def login(self, email="owner@example.com", password=PASSWORD, next_url=None):
        path = reverse("accounts:login")
        if next_url:
            path = f"{path}?next={next_url}"
        return self.client.post(path, {"email": email, "password": password})

    def test_login_browser_field_is_email_consistently(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertContains(response, 'name="email"', status_code=200)
        self.assertNotContains(response, 'name="username"', status_code=200)
        self.assertIn("email", response.context["form"].fields)
        self.assertNotIn("username", response.context["form"].fields)

    def test_login_rate_limit_uses_normalized_submitted_email(self):
        with patch(
            "accounts.views.enforce_rate_limit",
            wraps=enforce_rate_limit,
        ) as limiter:
            response = self.client.post(
                reverse("accounts:login"),
                {"email": "  OWNER@EXAMPLE.COM  ", "password": "wrong"},
            )

        self.assertEqual(response.status_code, 200)
        key = limiter.call_args.kwargs["key"]
        self.assertIn("owner@example.com", key)
        self.assertNotIn("OWNER@EXAMPLE.COM", key)
        self.assertNotIn("invalid", key)

    def test_http_rate_limit_returns_429_and_retry_after(self):
        with patch(
            "accounts.views.enforce_rate_limit",
            side_effect=RateLimitExceeded(retry_after=37),
        ):
            response = self.client.post(
                reverse("accounts:login"),
                {"email": self.user.email, "password": "wrong"},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "37")
        self.assertContains(response, "Too many requests", status_code=429)

    def seed_guest_quote(self):
        estimate = {
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
        response = self.client.post("/estimator/", estimate)
        self.assertEqual(response.status_code, 200)
        nonce = self.client.session[CONVERSION_NONCE_KEY]
        response = self.client.post("/quotes/guest/", {"nonce": nonce})
        self.assertEqual(response.status_code, 302)
        return nonce

    def test_normalized_email_login_rotates_session_and_converts_guest_after_success(
        self,
    ):
        self.seed_guest_quote()
        old_session_key = self.client.session.session_key

        response = self.login(email="  OWNER@EXAMPLE.COM  ")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("accounts:dashboard"))
        self.assertEqual(self.client.session.get("_auth_user_id"), str(self.user.pk))
        self.assertNotEqual(self.client.session.session_key, old_session_key)
        self.assertEqual(Quote.objects.filter(owner=self.user).count(), 1)
        dashboard = self.client.get(response.url)
        self.assertContains(dashboard, "guest quotation is now saved")
        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY):
            self.assertNotIn(key, self.client.session)

    def test_failed_login_does_not_convert_guest_quote_or_rotate_session(self):
        self.seed_guest_quote()
        old_session_key = self.client.session.session_key

        response = self.login(password="not-the-password")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Please enter a correct email address and password."
        )
        self.assertEqual(Quote.objects.filter(owner=self.user).count(), 0)
        self.assertEqual(self.client.session.session_key, old_session_key)
        self.assertIn(GUEST_QUOTE_KEY, self.client.session)

    def test_login_honors_safe_next_and_blocks_external_next(self):
        safe = self.client.post(
            f"{reverse('accounts:login')}?next=/quotes/",
            {"email": self.user.email, "password": PASSWORD},
        )
        self.assertEqual(safe.status_code, 302)
        self.assertEqual(safe.url, "/quotes/")

        self.client.logout()
        external = self.client.post(
            f"{reverse('accounts:login')}?next=https://evil.example/",
            {"email": self.user.email, "password": PASSWORD},
        )
        self.assertEqual(external.status_code, 302)
        self.assertEqual(external.url, reverse("accounts:dashboard"))

    def test_signup_creates_unverified_user_profile_and_transactional_notice(self):
        email = "new-owner@example.com"
        response = self.client.post(
            reverse("accounts:signup"),
            {"email": email, "password1": PASSWORD, "password2": PASSWORD},
        )

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email=email)
        self.assertIsNone(user.email_verified_at)
        self.assertIsNotNone(Profile.objects.get(user=user))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("verify", mail.outbox[0].body)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.pk))

    def test_signup_verification_send_failure_rolls_back_and_can_retry(self):
        data = {
            "email": "retry-owner@example.com",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
        with patch(
            "accounts.views.send_verification_email",
            side_effect=RuntimeError("mail unavailable"),
        ):
            failed = self.client.post(reverse("accounts:signup"), data)

        self.assertEqual(failed.status_code, 200)
        self.assertFalse(User.objects.filter(email=data["email"]).exists())
        self.assertContains(failed, "try again")

        retried = self.client.post(reverse("accounts:signup"), data)
        self.assertEqual(retried.status_code, 302)
        self.assertEqual(User.objects.filter(email=data["email"]).count(), 1)

    def test_anonymous_verification_resend_is_generic_and_usable(self):
        response = self.client.post(
            reverse("accounts:verify-notice"),
            {"email": self.user.email},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        followup = self.client.get(response.url)
        self.assertContains(followup, "If an active account", status_code=200)

    def test_anonymous_verification_resend_send_failure_is_generic(self):
        with patch(
            "accounts.views.send_verification_email",
            side_effect=RuntimeError("mail unavailable"),
        ):
            response = self.client.post(
                reverse("accounts:verify-notice"),
                {"email": self.user.email},
            )

        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response.url), "If an active account")

    def test_anonymous_verification_resend_is_rate_limited_without_disclosure(self):
        with (
            patch(
                "accounts.services.timezone.now",
                return_value=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            ),
            patch(
                "accounts.views.VERIFICATION_RESEND_RATE_LIMIT",
                2,
                create=True,
            ),
            patch(
                "accounts.views.enforce_rate_limit",
                wraps=enforce_rate_limit,
            ) as limiter,
        ):
            first = self.client.post(
                reverse("accounts:verify-notice"),
                {"email": self.user.email},
            )
            second = self.client.post(
                reverse("accounts:verify-notice"),
                {"email": self.user.email},
            )
            limited = self.client.post(
                reverse("accounts:verify-notice"),
                {"email": self.user.email},
            )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)
        self.assertContains(limited, "Too many requests", status_code=429)
        self.assertNotContains(limited, self.user.email, status_code=429)
        keys = [call.kwargs["key"] for call in limiter.call_args_list]
        self.assertTrue(any("accounts.verification-resend" in key for key in keys))
        self.assertTrue(any(self.user.email in key for key in keys))
        self.assertTrue(all("accounts.password-reset" not in key for key in keys))
        bucket = RateLimitBucket.objects.order_by("-window_start").first()
        self.assertNotIn("accounts.verification-resend", bucket.key)
        self.assertNotIn(self.user.email, bucket.key)

    def test_duplicate_signup_is_rejected_without_creating_or_emailing_another_user(
        self,
    ):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": " OWNER@example.com ",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotContains(self.client.get(response.url), "already exists")
        self.assertEqual(User.objects.filter(email="owner@example.com").count(), 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Profile.objects.filter(user=self.user).count(), 0)

    def test_verification_marks_email_verified_and_rejects_invalid_tokens(self):
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        uid = force_str(urlsafe_base64_encode(force_bytes(self.user.pk)))
        token = default_token_generator.make_token(self.user)

        response = self.client.get(
            reverse(
                "accounts:verify",
                kwargs={"uidb64": uid, "token": token},
            )
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)
        self.assertTrue(self.user.can_publish)

        invalid = self.client.get(
            reverse(
                "accounts:verify",
                kwargs={"uidb64": uid, "token": "invalid-token"},
            )
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "verification link")

    def test_malformed_verification_links_render_recoverable_page(self):
        valid_uid = force_str(urlsafe_base64_encode(force_bytes(self.user.pk)))
        for uidb64, token in (
            ("!!!!", "token"),
            ("Zm9v", "!!!!"),
            (valid_uid, "!!!!"),
        ):
            with self.subTest(uidb64=uidb64, token=token):
                response = self.client.get(
                    reverse(
                        "accounts:verify",
                        kwargs={"uidb64": uidb64, "token": token},
                    )
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "verification link")

    def test_password_reset_is_generic_and_changes_password_with_session_rotation(self):
        response = self.client.post(
            reverse("accounts:password-reset"),
            {"email": " OWNER@example.com "},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/accounts/password-reset/", mail.outbox[0].body)
        reset_path = mail.outbox[0].body.split("http://testserver", 1)[1].split()[0]
        reset_path = reset_path.rstrip(".,")
        session_key_before_reset = self.client.session.session_key

        response = self.client.post(
            reset_path,
            {
                "new_password1": "a new sufficiently secure password",
                "new_password2": "a new sufficiently secure password",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(session_key_before_reset, self.client.session.session_key)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a new sufficiently secure password"))

    def test_password_reset_does_not_reveal_unknown_accounts(self):
        response = self.client.post(
            reverse("accounts:password-reset"),
            {"email": "missing@example.com"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_send_failure_keeps_generic_response(self):
        with patch(
            "accounts.forms.send_password_reset_email",
            side_effect=RuntimeError("mail unavailable"),
        ):
            response = self.client.post(
                reverse("accounts:password-reset"),
                {"email": self.user.email},
            )

        self.assertEqual(response.status_code, 302)
        done = self.client.get(response.url)
        self.assertEqual(done.status_code, 200)
        self.assertContains(done, "If an active account matches")

    def test_queued_messages_render_with_accessible_status_markup(self):
        response = self.client.post(
            reverse("accounts:password-reset"),
            {"email": "missing@example.com"},
        )
        done = self.client.get(response.url)

        self.assertContains(done, "If an active account matches")
        self.assertContains(done, 'class="message message-info"', status_code=200)
        self.assertContains(done, 'role="status"', status_code=200)

    def test_malformed_password_reset_links_render_recoverable_page(self):
        valid_uid = force_str(urlsafe_base64_encode(force_bytes(self.user.pk)))
        for uidb64, token in (
            ("!!!!", "token"),
            ("Zm9v", "!!!!"),
            (valid_uid, "!!!!"),
        ):
            with self.subTest(uidb64=uidb64, token=token):
                path = reverse(
                    "accounts:password-reset-confirm",
                    kwargs={"uidb64": uidb64, "token": token},
                )
                get_response = self.client.get(path)
                post_response = self.client.post(
                    path,
                    {
                        "new_password1": "a new sufficiently secure password",
                        "new_password2": "a new sufficiently secure password",
                    },
                )

                self.assertEqual(get_response.status_code, 200)
                self.assertContains(get_response, "invalid or has expired")
                self.assertEqual(post_response.status_code, 200)
                self.assertContains(post_response, "invalid or has expired")

    def test_security_password_change_requires_current_password_and_rotates_session(
        self,
    ):
        self.login()
        old_session_key = self.client.session.session_key

        denied = self.client.post(
            reverse("accounts:security"),
            {
                "old_password": "wrong",
                "new_password1": "a new sufficiently secure password",
                "new_password2": "a new sufficiently secure password",
            },
        )
        self.assertEqual(denied.status_code, 200)
        self.assertIn("old_password", denied.context["form"].errors)
        self.assertEqual(self.client.session.session_key, old_session_key)

        changed = self.client.post(
            reverse("accounts:security"),
            {
                "old_password": PASSWORD,
                "new_password1": "a new sufficiently secure password",
                "new_password2": "a new sufficiently secure password",
            },
        )
        self.assertEqual(changed.status_code, 302)
        self.assertNotEqual(self.client.session.session_key, old_session_key)
        security_page = self.client.get(changed.url)
        self.assertContains(security_page, "Your password has been changed.")
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.client.post(reverse("accounts:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_security_notice_failure_does_not_block_password_change(self):
        self.login()
        with patch(
            "accounts.views.send_security_notice_email",
            side_effect=RuntimeError("mail unavailable"),
        ):
            response = self.client.post(
                reverse("accounts:security"),
                {
                    "old_password": PASSWORD,
                    "new_password1": "a new sufficiently secure password",
                    "new_password2": "a new sufficiently secure password",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a new sufficiently secure password"))

    def test_profile_get_uses_read_only_fallback_without_creating_profile(self):
        self.login()

        first = self.client.get(reverse("accounts:profile"))
        second = self.client.get(reverse("accounts:profile"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(Profile.objects.filter(user=self.user).exists())

    def test_profile_updates_validated_defaults(self):
        self.login()
        response = self.client.post(
            reverse("accounts:profile"),
            {
                "full_name": "Owner Example",
                "business_name": "Example Studio",
                "phone": "+1 (555) 010-1234",
                "website": "https://example.com",
                "default_currency": "EUR",
                "default_deposit_percentage": 45,
                "default_quote_validity_days": 21,
                "default_accent_color": "#A1b2C3",
            },
        )

        self.assertEqual(response.status_code, 302)
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.full_name, "Owner Example")
        self.assertEqual(profile.default_currency, "EUR")
        self.assertEqual(profile.default_deposit_percentage, 45)

        invalid = self.client.post(
            reverse("accounts:profile"),
            {
                "default_currency": "CAD",
                "default_deposit_percentage": 42,
                "default_quote_validity_days": 91,
                "default_accent_color": "red",
                "website": "http://example.com",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("default_deposit_percentage", invalid.context["form"].errors)
        profile.refresh_from_db()
        self.assertEqual(profile.default_currency, "EUR")

    def test_deletion_requires_reauthentication_and_explicit_confirmation(self):
        self.login()
        denied = self.client.post(
            reverse("accounts:delete"),
            {"password": "wrong", "confirm": "on"},
        )
        self.assertEqual(denied.status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

        response = self.client.post(
            reverse("accounts:delete"),
            {"password": PASSWORD, "confirm": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.user.is_quarantined)
        self.assertTrue(
            DeletionTombstone.objects.filter(account_id=self.user.pk).exists()
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        home = self.client.get(response.url)
        self.assertContains(home, "Your account has been deleted.")

    def test_authentication_pages_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse("accounts:login"),
            {"email": self.user.email, "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 403)


class RateLimitTests(TestCase):
    def test_rate_limit_counts_window_and_raises_generic_exception(self):
        for _ in range(2):
            enforce_rate_limit(
                key="login:owner@example.com", limit=2, window_seconds=60
            )

        with self.assertRaises(RateLimitExceeded) as error:
            enforce_rate_limit(
                key="login:owner@example.com",
                limit=2,
                window_seconds=60,
            )

        self.assertNotIn("owner@example.com", str(error.exception))
        bucket = error.exception.bucket
        self.assertEqual(bucket.counter, 3)
        bucket.refresh_from_db()
        self.assertEqual(bucket.counter, 3)
        self.assertNotIn("login:owner@example.com", bucket.key)

    def test_expired_buckets_are_removed_before_new_window_counting(self):
        from django.utils import timezone

        now = timezone.now()
        RateLimitBucket.objects.create(
            key="a" * 64,
            window_start=now - timedelta(minutes=2),
            counter=4,
            expires_at=now - timedelta(minutes=1),
        )

        enforce_rate_limit(key="new-key", limit=1, window_seconds=60)

        self.assertEqual(RateLimitBucket.objects.filter(key="a" * 64).count(), 0)

    def test_expired_window_allows_a_new_request(self):
        with patch(
            "accounts.services.timezone.now",
            return_value=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        ):
            enforce_rate_limit(key="window-key", limit=1, window_seconds=1)
        with patch(
            "accounts.services.timezone.now",
            return_value=datetime(2026, 9, 24, 12, 0, 2, tzinfo=UTC),
        ):
            enforce_rate_limit(key="window-key", limit=1, window_seconds=1)


class ProtectedPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner@example.com", password=PASSWORD)
        self.other = User.objects.create_user("other@example.com", password=PASSWORD)
        self.client.force_login(self.user)

    def make_quote(self, owner, title, status=Quote.Status.DRAFT, number=None):
        values = {
            "owner": owner,
            "project_title": title,
            "project_description": (
                "A sufficiently detailed project description for the owner list."
            ),
            "client_name": "Client",
            "client_email": "client@example.com",
            "sender_name": "Sender",
            "sender_email": "sender@example.com",
            "issue_date": date(2026, 1, 1),
            "valid_until": date(2026, 1, 31),
            "terms": "Terms are agreed in writing.",
            "quote_number": number,
        }
        quote = Quote.objects.create(**values)
        QuoteLineItem.objects.create(
            quote=quote,
            label="Project fee",
            amount=Decimal("100.00"),
        )
        if status != Quote.Status.DRAFT:
            quote.status = status
            quote.save()
        return quote

    def test_dashboard_and_quote_list_redirect_anonymous_users(self):
        self.client.logout()
        for path in (
            "/dashboard/",
            "/quotes/",
            "/settings/profile/",
            "/settings/security/",
            "/settings/delete/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)
                self.assertIn("next=", response.url)

    def test_dashboard_empty_state_and_owner_counts_are_isolated(self):
        dashboard = self.client.get("/dashboard/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "No quotes yet")

        self.make_quote(self.user, "Owner draft", Quote.Status.DRAFT, "SQ-2026-0001")
        self.make_quote(
            self.other, "Private other title", Quote.Status.DRAFT, "SQ-2026-0002"
        )
        dashboard = self.client.get("/dashboard/")
        self.assertContains(dashboard, "Owner draft")
        self.assertNotContains(dashboard, "Private other title")
        self.assertContains(dashboard, "1")

    def test_quote_list_search_and_status_filter_use_owner_scope(self):
        self.make_quote(self.user, "Brand refresh", Quote.Status.DRAFT, "SQ-2026-0003")
        self.make_quote(self.other, "Other project", Quote.Status.DRAFT, "SQ-2026-0004")

        response = self.client.get("/quotes/", {"q": "Brand"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brand refresh")
        self.assertNotContains(response, "Other project")

        response = self.client.get("/quotes/", {"status": "not-a-status"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid choice")
        self.assertContains(response, "Brand refresh")

    def test_valid_status_filter_returns_only_matching_owner_quotes(self):
        self.make_quote(self.user, "Draft project", Quote.Status.DRAFT, "SQ-2026-0005")
        self.make_quote(
            self.user,
            "Sent project",
            Quote.Status.SENT,
            "SQ-2026-0006",
        )

        response = self.client.get("/quotes/", {"status": "sent"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sent project")
        self.assertNotContains(response, "Draft project")


class GuestConversionBoundaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner@example.com", password=PASSWORD)
        self.client = Client()

    def test_signup_conversion_failure_keeps_guest_state_for_retry(self):
        self.client.post(
            "/estimator/",
            {
                "description": (
                    "Create a responsive website with discovery, implementation, "
                    "testing, and a clear handoff."
                ),
                "delivery_days": 5,
                "complexity": "standard",
                "revision_rounds": 0,
                "urgency": "standard",
                "experience": "intermediate",
                "currency": "USD",
            },
        )
        nonce = self.client.session[CONVERSION_NONCE_KEY]
        self.client.post("/quotes/guest/", {"nonce": nonce})

        with patch(
            "quotes.services.guest.Quote.objects.create",
            side_effect=DatabaseError("boom"),
        ):
            response = self.client.post(
                reverse("accounts:signup"),
                {
                    "email": "new@example.com",
                    "password1": PASSWORD,
                    "password2": PASSWORD,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(GUEST_ESTIMATE_KEY, self.client.session)
        self.assertIn(GUEST_QUOTE_KEY, self.client.session)
        self.assertEqual(
            Quote.objects.filter(owner__email="new@example.com").count(), 0
        )

        retry = self.client.post(
            reverse("accounts:login"),
            {"email": "new@example.com", "password": PASSWORD},
        )
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(
            Quote.objects.filter(owner__email="new@example.com").count(), 1
        )
        self.assertNotIn(GUEST_QUOTE_KEY, self.client.session)
