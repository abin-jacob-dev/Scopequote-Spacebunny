import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.sessions.middleware import SessionMiddleware
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase

from accounts.models import User
from pricing.classifier import classify_description
from pricing.constants import (
    COMPLEXITIES,
    CURRENCIES,
    EXPERIENCES,
    URGENCIES,
)
from pricing.engine import calculate_estimate
from pricing.forms import EstimateForm
from pricing.types import EstimateInputs
from quotes.models import Quote, QuoteLineItem
from quotes.services.guest import (
    CONVERSION_NONCE_KEY,
    GUEST_ESTIMATE_KEY,
    GUEST_QUOTE_KEY,
    MAX_GUEST_PAYLOAD_BYTES,
    GuestPayloadTooLarge,
    GuestStateConflict,
    convert_guest_quote,
    create_guest_quote,
    get_guest_estimate,
    get_guest_quote,
    save_guest_estimate,
)

_PRIVATE_GUEST_CREATED_AT_KEY = "_scopequote.guest_created_at"


def decimal_text(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def estimate_payload(**overrides):
    inputs = {
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
    inputs.update(overrides)
    result = calculate_estimate(EstimateInputs(**inputs))
    scope = classify_description(inputs["description"])
    return {
        "inputs": inputs,
        "result": {
            "effective_hours": decimal_text(result.effective_hours),
            "minimum": decimal_text(result.minimum),
            "recommended": decimal_text(result.recommended),
            "maximum": decimal_text(result.maximum),
            "hourly_equivalent": decimal_text(result.hourly_equivalent),
            "pricing_config_version": result.pricing_config_version,
            "factors": [dict(factor) for factor in result.factors],
        },
        "scope": {
            "category": scope.category,
            "matched_phrases": scope.matched_phrases,
            "confidence": scope.confidence,
            "suggested_items": scope.suggested_items,
        },
    }


def valid_form_data(**overrides):
    data = estimate_payload()["inputs"].copy()
    data.update(overrides)
    return data


class EstimateFormTests(TestCase):
    def test_duration_presets_map_to_working_delivery_days(self):
        self.assertEqual(
            EstimateForm.DURATION_PRESETS,
            {
                3: "3 days",
                5: "1 week",
                10: "2 weeks",
                20: "4 weeks",
                40: "8 weeks",
            },
        )
        for days in EstimateForm.DURATION_PRESETS:
            with self.subTest(days=days):
                form = EstimateForm(valid_form_data(delivery_days=days))
                self.assertTrue(form.is_valid())
                self.assertEqual(form.cleaned_data["delivery_days"], days)

    def test_delivery_days_accepts_only_integers_from_one_through_sixty(self):
        for days in (1, 2, 59, 60):
            with self.subTest(days=days):
                form = EstimateForm(valid_form_data(delivery_days=days))
                self.assertTrue(form.is_valid())

        for days in (0, -1, 61, 1.5, "not-a-number"):
            with self.subTest(days=days):
                form = EstimateForm(valid_form_data(delivery_days=days))
                self.assertFalse(form.is_valid())
                self.assertIn("delivery_days", form.errors)

    def test_revision_rounds_accepts_only_integers_from_zero_through_ten(self):
        for rounds in (0, 1, 9, 10):
            with self.subTest(rounds=rounds):
                form = EstimateForm(valid_form_data(revision_rounds=rounds))
                self.assertTrue(form.is_valid())

        for rounds in (-1, 11, 2.5, "two"):
            with self.subTest(rounds=rounds):
                form = EstimateForm(valid_form_data(revision_rounds=rounds))
                self.assertFalse(form.is_valid())
                self.assertIn("revision_rounds", form.errors)

    def test_description_is_trimmed_and_limited_to_thirty_through_two_thousand(self):
        form = EstimateForm(valid_form_data(description=f"  {'a' * 30}  "))
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["description"], "a" * 30)

        for description in ("a" * 29, "a" * 2001, "   "):
            with self.subTest(length=len(description)):
                form = EstimateForm(valid_form_data(description=description))
                self.assertFalse(form.is_valid())
                self.assertIn("description", form.errors)

        self.assertTrue(
            EstimateForm(valid_form_data(description="a" * 2000)).is_valid()
        )

    def test_every_approved_enum_value_is_accepted_and_unknown_values_are_rejected(
        self,
    ):
        enum_fields = {
            "complexity": COMPLEXITIES,
            "urgency": URGENCIES,
            "experience": EXPERIENCES,
            "currency": CURRENCIES,
        }
        for field_name, values in enum_fields.items():
            for value in values:
                with self.subTest(field=field_name, value=value):
                    form = EstimateForm(valid_form_data(**{field_name: value}))
                    self.assertTrue(form.is_valid())
                    self.assertEqual(form.cleaned_data[field_name], value)

            with self.subTest(field=field_name, value="unknown"):
                form = EstimateForm(valid_form_data(**{field_name: "unknown"}))
                self.assertFalse(form.is_valid())
                self.assertIn(field_name, form.errors)

    def test_all_seven_inputs_are_required(self):
        form = EstimateForm({})

        self.assertFalse(form.is_valid())
        self.assertEqual(
            set(form.errors),
            {
                "description",
                "delivery_days",
                "complexity",
                "revision_rounds",
                "urgency",
                "experience",
                "currency",
            },
        )


class GuestSessionTestCase(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def request_with_session(self, method="post", path="/"):
        request = getattr(self.factory, method)(path)
        middleware = SessionMiddleware(lambda req: HttpResponse())
        middleware.process_request(request)
        request.session.save()
        return request

    def seed_guest_quote(self, client):
        response = client.post("/estimator/", valid_form_data())
        self.assertEqual(response.status_code, 200)
        nonce = client.session[CONVERSION_NONCE_KEY]
        response = client.post("/quotes/guest/", {"nonce": nonce})
        self.assertEqual(response.status_code, 302)
        return nonce


class GuestEstimateSessionTests(GuestSessionTestCase):
    def test_saved_estimate_contains_only_validated_inputs_and_server_results(self):
        request = self.request_with_session()

        saved = save_guest_estimate(request, estimate_payload())

        self.assertEqual(saved["inputs"], estimate_payload()["inputs"])
        self.assertEqual(saved["result"]["recommended"], "1320")
        self.assertEqual(saved["result"]["pricing_config_version"], "pricing-v1")
        self.assertEqual(
            set(saved),
            {"inputs", "result", "scope", "expires_at"},
        )
        self.assertNotIn("owner_id", repr(saved))
        self.assertNotIn("chosen_total", repr(saved))

    def test_get_guest_estimate_rejects_invalid_stored_results(self):
        request = self.request_with_session()
        save_guest_estimate(request, estimate_payload())
        payload = request.session[GUEST_ESTIMATE_KEY]
        payload["result"]["recommended"] = "1"

        self.assertIsNone(get_guest_estimate(request))
        self.assertNotIn(GUEST_ESTIMATE_KEY, request.session)

    def test_ten_day_old_session_expires_in_four_days_and_activity_does_not_extend_it(
        self,
    ):
        request = self.request_with_session()
        activity_started = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        session_created = activity_started - timedelta(days=10)
        expiry = session_created + timedelta(days=14)
        request.session[_PRIVATE_GUEST_CREATED_AT_KEY] = session_created.isoformat()
        request.session["_session_expiry"] = 999_999_999

        with patch("quotes.services.guest.timezone.now", return_value=activity_started):
            saved = save_guest_estimate(request, estimate_payload())

        self.assertEqual(saved["expires_at"], expiry.isoformat())
        self.assertEqual(
            request.session[_PRIVATE_GUEST_CREATED_AT_KEY],
            session_created.isoformat(),
        )

        later_activity = activity_started + timedelta(days=3)
        with patch("quotes.services.guest.timezone.now", return_value=later_activity):
            updated = save_guest_estimate(request, estimate_payload(delivery_days=6))
            quote = create_guest_quote(
                request,
                {"nonce": request.session[CONVERSION_NONCE_KEY]},
            )

        self.assertEqual(updated["expires_at"], expiry.isoformat())
        self.assertEqual(quote["expires_at"], expiry.isoformat())
        self.assertEqual(
            request.session[_PRIVATE_GUEST_CREATED_AT_KEY],
            session_created.isoformat(),
        )

        with patch("quotes.services.guest.timezone.now", return_value=expiry):
            self.assertIsNone(get_guest_estimate(request))
            self.assertIsNone(get_guest_quote(request))
        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY):
            self.assertNotIn(key, request.session)

    def test_missing_private_boundary_migrates_from_existing_guest_expiry(self):
        request = self.request_with_session()
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        session_created = now - timedelta(days=10)
        legacy_expiry = session_created + timedelta(days=14)
        payload = estimate_payload()
        payload["expires_at"] = legacy_expiry.isoformat()
        request.session[GUEST_ESTIMATE_KEY] = payload
        request.session["_session_expiry"] = 1209600

        with patch("quotes.services.guest.timezone.now", return_value=now):
            recovered = get_guest_estimate(request)

        self.assertEqual(recovered, payload)
        self.assertEqual(
            request.session[_PRIVATE_GUEST_CREATED_AT_KEY],
            session_created.isoformat(),
        )
        self.assertEqual(request.session["_session_expiry"], 1209600)

        with patch("quotes.services.guest.timezone.now", return_value=legacy_expiry):
            self.assertIsNone(get_guest_estimate(request))
        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY):
            self.assertNotIn(key, request.session)

    def test_malformed_private_boundary_clears_public_state_and_resets(self):
        request = self.request_with_session()
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        with patch("quotes.services.guest.timezone.now", return_value=now):
            save_guest_estimate(request, estimate_payload())
            create_guest_quote(
                request,
                {"nonce": request.session[CONVERSION_NONCE_KEY]},
            )
        request.session[_PRIVATE_GUEST_CREATED_AT_KEY] = "not-a-timestamp"
        request.session["_session_expiry"] = 999_999_999

        with patch("quotes.services.guest.timezone.now", return_value=now):
            self.assertIsNone(get_guest_estimate(request))

        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY):
            self.assertNotIn(key, request.session)
        self.assertEqual(
            request.session[_PRIVATE_GUEST_CREATED_AT_KEY],
            now.isoformat(),
        )

    def test_reset_clears_legacy_session_expiry_before_database_save(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        stale_expiry = (now - timedelta(days=1)).isoformat()

        for legacy_expiry in ("not-a-timestamp", stale_expiry):
            with self.subTest(legacy_expiry=legacy_expiry):
                request = self.request_with_session()
                with patch("quotes.services.guest.timezone.now", return_value=now):
                    save_guest_estimate(request, estimate_payload())
                request.session[_PRIVATE_GUEST_CREATED_AT_KEY] = "not-a-timestamp"
                request.session["_session_expiry"] = legacy_expiry

                with patch("quotes.services.guest.timezone.now", return_value=now):
                    self.assertIsNone(get_guest_estimate(request))

                request.session.save()
                reloaded = request.session.__class__(request.session.session_key)
                self.assertNotIn("_session_expiry", reloaded)
                self.assertEqual(
                    reloaded[_PRIVATE_GUEST_CREATED_AT_KEY],
                    now.isoformat(),
                )

    def test_expired_private_boundary_clears_public_state_and_resets(self):
        request = self.request_with_session()
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        with patch("quotes.services.guest.timezone.now", return_value=now):
            save_guest_estimate(request, estimate_payload())
            create_guest_quote(
                request,
                {"nonce": request.session[CONVERSION_NONCE_KEY]},
            )
        request.session[_PRIVATE_GUEST_CREATED_AT_KEY] = (
            now - timedelta(days=14)
        ).isoformat()
        request.session["_session_expiry"] = 999_999_999

        with patch("quotes.services.guest.timezone.now", return_value=now):
            self.assertIsNone(get_guest_estimate(request))

        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY):
            self.assertNotIn(key, request.session)
        self.assertEqual(
            request.session[_PRIVATE_GUEST_CREATED_AT_KEY],
            now.isoformat(),
        )

    def test_payload_larger_than_sixty_four_kib_is_rejected_after_validation(self):
        request = self.request_with_session()
        payload = estimate_payload()
        payload["padding"] = "x" * MAX_GUEST_PAYLOAD_BYTES
        self.assertGreater(
            len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            ),
            MAX_GUEST_PAYLOAD_BYTES,
        )

        with self.assertRaises(GuestPayloadTooLarge):
            save_guest_estimate(request, payload)
        self.assertNotIn(GUEST_ESTIMATE_KEY, request.session)

    def test_saving_a_new_estimate_invalidates_the_previous_guest_quote(self):
        request = self.request_with_session()
        save_guest_estimate(request, estimate_payload())
        first_nonce = request.session[CONVERSION_NONCE_KEY]
        create_guest_quote(request, {"nonce": first_nonce})
        self.assertIn(GUEST_QUOTE_KEY, request.session)

        save_guest_estimate(request, estimate_payload(delivery_days=6))

        self.assertNotIn(GUEST_QUOTE_KEY, request.session)
        self.assertNotEqual(request.session[CONVERSION_NONCE_KEY], first_nonce)


class GuestQuoteSessionTests(GuestSessionTestCase):
    def test_guest_quote_is_derived_from_the_estimate_and_is_pdf_eligible(self):
        request = self.request_with_session()
        estimate = save_guest_estimate(request, estimate_payload())
        nonce = request.session[CONVERSION_NONCE_KEY]

        quote = create_guest_quote(request, {"nonce": nonce})

        self.assertTrue(quote["pdf_eligible"])
        self.assertEqual(quote["inputs"], estimate["inputs"])
        self.assertEqual(quote["result"], estimate["result"])
        self.assertEqual(quote["scope"], estimate["scope"])
        self.assertEqual(
            quote["conversion_nonce"], request.session[CONVERSION_NONCE_KEY]
        )
        self.assertEqual(get_guest_quote(request), quote)
        self.assertEqual(Quote.objects.count(), 0)

    def test_missing_estimate_cannot_create_a_guest_quote_or_database_record(self):
        request = self.request_with_session()
        request.session[CONVERSION_NONCE_KEY] = "current-nonce"

        with self.assertRaises(GuestStateConflict):
            create_guest_quote(request, {"nonce": "current-nonce"})

        self.assertIsNone(get_guest_quote(request))
        self.assertEqual(Quote.objects.count(), 0)

    def test_stale_nonce_returns_recoverable_conflict_without_overwriting(self):
        request = self.request_with_session()
        save_guest_estimate(request, estimate_payload())
        stale_nonce = request.session[CONVERSION_NONCE_KEY]
        current_quote = create_guest_quote(request, {"nonce": stale_nonce})
        current_nonce = request.session[CONVERSION_NONCE_KEY]

        with self.assertRaises(GuestStateConflict):
            create_guest_quote(request, {"nonce": stale_nonce})

        self.assertEqual(get_guest_quote(request), current_quote)
        self.assertEqual(request.session[CONVERSION_NONCE_KEY], current_nonce)
        self.assertEqual(Quote.objects.count(), 0)

    def test_invalid_stored_guest_quote_is_not_pdf_eligible(self):
        request = self.request_with_session()
        estimate = save_guest_estimate(request, estimate_payload())
        estimate["result"]["recommended"] = "999999"
        request.session[GUEST_QUOTE_KEY] = estimate

        self.assertIsNone(get_guest_quote(request))
        self.assertEqual(Quote.objects.count(), 0)

    def test_malformed_stored_nonce_is_rejected_without_raising(self):
        request = self.request_with_session()
        save_guest_estimate(request, estimate_payload())
        nonce = request.session[CONVERSION_NONCE_KEY]
        quote = create_guest_quote(request, {"nonce": nonce})
        quote["conversion_nonce"] = 123
        request.session[GUEST_QUOTE_KEY] = quote

        self.assertIsNone(get_guest_quote(request))
        self.assertEqual(Quote.objects.count(), 0)

    def test_guest_quote_post_requires_csrf_and_never_creates_a_quote(self):
        client = Client(enforce_csrf_checks=True)
        client.get("/estimator/")
        csrf_token = client.cookies["csrftoken"].value
        estimate_data = valid_form_data()
        estimate_data["csrfmiddlewaretoken"] = csrf_token
        seed_response = client.post("/estimator/", estimate_data)
        self.assertEqual(seed_response.status_code, 200)
        nonce = client.session[CONVERSION_NONCE_KEY]

        response = client.post("/quotes/guest/", {"nonce": nonce})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Quote.objects.count(), 0)


class GuestConversionTests(GuestSessionTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            "guest-owner@example.com",
            password="correct horse battery staple",
        )

    def request_for_client_session(self, client):
        request = self.request_with_session()
        request.session = client.session
        return request

    def test_conversion_creates_an_owner_scoped_draft_and_line_item(self):
        client = Client()
        self.seed_guest_quote(client)
        request = self.request_for_client_session(client)
        nonce = request.session[CONVERSION_NONCE_KEY]

        quote = convert_guest_quote(request, self.user)

        self.assertIsNotNone(quote)
        self.assertEqual(quote.owner, self.user)
        self.assertEqual(quote.guest_conversion_nonce, nonce)
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertEqual(
            quote.project_description, estimate_payload()["inputs"]["description"]
        )
        self.assertEqual(quote.recommended, Decimal("1320.00"))
        self.assertEqual(quote.chosen_total, Decimal("1320.00"))
        self.assertEqual(quote.category, "website")
        self.assertEqual(quote.deposit_amount, Decimal("660.00"))
        line_item = QuoteLineItem.objects.get(quote=quote)
        self.assertEqual(line_item.label, "Project fee")
        self.assertEqual(line_item.amount, Decimal("1320.00"))
        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY):
            self.assertNotIn(key, request.session)

    def test_retry_after_partial_session_cleanup_finds_the_committed_quote(self):
        client = Client()
        self.seed_guest_quote(client)
        request = self.request_for_client_session(client)
        original_pop = request.session.pop

        def fail_after_removing_estimate(key, default=None):
            original_pop(key, default)
            if key == GUEST_ESTIMATE_KEY:
                raise RuntimeError("cleanup interrupted")

        with patch.object(
            request.session, "pop", side_effect=fail_after_removing_estimate
        ):
            with self.assertRaises(RuntimeError):
                convert_guest_quote(request, self.user)

        committed = Quote.objects.get(owner=self.user)
        self.assertNotIn(GUEST_ESTIMATE_KEY, request.session)
        self.assertIn(GUEST_QUOTE_KEY, request.session)
        self.assertIn(CONVERSION_NONCE_KEY, request.session)

        retried = convert_guest_quote(request, self.user)

        self.assertEqual(retried, committed)
        self.assertEqual(Quote.objects.filter(owner=self.user).count(), 1)
        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY):
            self.assertNotIn(key, request.session)

    def test_owner_scoped_retry_returns_existing_quote_and_clears_guest_state(self):
        client = Client()
        self.seed_guest_quote(client)
        request = self.request_for_client_session(client)
        nonce = request.session[CONVERSION_NONCE_KEY]
        existing = Quote.objects.create(
            owner=self.user,
            guest_conversion_nonce=nonce,
            project_description=estimate_payload()["inputs"]["description"],
            category="website",
            delivery_days=5,
            complexity="standard",
            revision_rounds=0,
            urgency="standard",
            experience="intermediate",
            currency="USD",
            effective_hours=Decimal("20.00"),
            minimum=Decimal("1160.00"),
            recommended=Decimal("1320.00"),
            maximum=Decimal("1480.00"),
            chosen_total=Decimal("1320.00"),
            hourly_equivalent=Decimal("66.00"),
        )

        converted = convert_guest_quote(request, self.user)

        self.assertEqual(converted, existing)
        self.assertEqual(Quote.objects.filter(guest_conversion_nonce=nonce).count(), 1)
        self.assertNotIn(GUEST_QUOTE_KEY, request.session)

    def test_nonce_owned_by_another_account_is_rejected_and_replaced(self):
        other_user = User.objects.create_user(
            "other-owner@example.com",
            password="correct horse battery staple",
        )
        client = Client()
        self.seed_guest_quote(client)
        request = self.request_for_client_session(client)
        occupied_nonce = request.session[CONVERSION_NONCE_KEY]
        Quote.objects.create(
            owner=other_user,
            guest_conversion_nonce=occupied_nonce,
            project_description=estimate_payload()["inputs"]["description"],
            category="website",
            delivery_days=5,
            complexity="standard",
            revision_rounds=0,
            urgency="standard",
            experience="intermediate",
            currency="USD",
            effective_hours=Decimal("20.00"),
            minimum=Decimal("1160.00"),
            recommended=Decimal("1320.00"),
            maximum=Decimal("1480.00"),
            chosen_total=Decimal("1320.00"),
            hourly_equivalent=Decimal("66.00"),
        )

        converted = convert_guest_quote(request, self.user)

        self.assertIsNone(converted)
        self.assertNotEqual(request.session[CONVERSION_NONCE_KEY], occupied_nonce)
        self.assertIn(GUEST_QUOTE_KEY, request.session)
        self.assertEqual(Quote.objects.filter(owner=self.user).count(), 0)

    def test_failed_conversion_preserves_guest_state_and_can_be_retried(self):
        client = Client()
        self.seed_guest_quote(client)
        request = self.request_for_client_session(client)
        original_nonce = request.session[CONVERSION_NONCE_KEY]

        with patch.object(Quote.objects, "create", side_effect=DatabaseError("boom")):
            self.assertIsNone(convert_guest_quote(request, self.user))

        self.assertEqual(request.session[CONVERSION_NONCE_KEY], original_nonce)
        self.assertIn(GUEST_ESTIMATE_KEY, request.session)
        self.assertIn(GUEST_QUOTE_KEY, request.session)
        self.assertEqual(Quote.objects.filter(owner=self.user).count(), 0)

        quote = convert_guest_quote(request, self.user)

        self.assertIsNotNone(quote)
        self.assertEqual(quote.guest_conversion_nonce, original_nonce)
        self.assertEqual(Quote.objects.filter(owner=self.user).count(), 1)

    def test_conversion_without_a_valid_guest_quote_does_nothing(self):
        request = self.request_with_session()

        self.assertIsNone(convert_guest_quote(request, self.user))
        self.assertEqual(Quote.objects.count(), 0)


class EstimatorViewTests(TestCase):
    def test_get_renders_semantic_sections_labels_pricing_data_and_noscript(self):
        response = self.client.get("/estimator/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Project")
        self.assertContains(response, "Delivery")
        self.assertContains(response, "Pricing Context")
        self.assertContains(response, '<label for="id_description">')
        self.assertContains(response, "<noscript>")
        self.assertContains(response, 'data-pricing-version="pricing-v1"')
        self.assertContains(response, 'data-complexity-factor="standard:1:0.1"')
        self.assertContains(response, 'data-rate-factor="intermediate:USD=60"')
        self.assertContains(response, "pricing-preview.js")

    def test_post_calculates_authoritative_values_and_ignores_hidden_business_fields(
        self,
    ):
        data = valid_form_data(
            minimum="1",
            recommended="999999",
            maximum="999999",
            chosen_total="1",
            owner_id=str(self.client.get("/estimator/").wsgi_request.user.pk),
            conversion_result="accepted",
        )

        response = self.client.post("/estimator/", data)

        self.assertEqual(response.status_code, 200)
        stored = self.client.session[GUEST_ESTIMATE_KEY]
        self.assertEqual(stored["result"]["recommended"], "1320")
        self.assertEqual(stored["result"]["minimum"], "1160")
        self.assertEqual(stored["result"]["maximum"], "1480")
        self.assertNotIn("owner_id", repr(stored))
        self.assertNotIn("conversion_result", repr(stored))
        self.assertEqual(Quote.objects.count(), 0)

    def test_invalid_post_reports_inline_errors_without_saving_state(self):
        response = self.client.post(
            "/estimator/",
            valid_form_data(
                description="too short", delivery_days=61, revision_rounds=11
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertNotIn(GUEST_ESTIMATE_KEY, self.client.session)
        self.assertContains(response, "Enter at least 30 characters")
        self.assertContains(response, "Enter a whole number from 1 through 60")
        self.assertContains(response, "Enter a whole number from 0 through 10")

    def test_stale_guest_quote_post_renders_recoverable_conflict_without_database_write(
        self,
    ):
        self.client.post("/estimator/", valid_form_data())
        stale_nonce = self.client.session[CONVERSION_NONCE_KEY]
        first_response = self.client.post("/quotes/guest/", {"nonce": stale_nonce})
        self.assertEqual(first_response.status_code, 302)
        current_nonce = self.client.session[CONVERSION_NONCE_KEY]

        conflict = self.client.post("/quotes/guest/", {"nonce": stale_nonce})

        self.assertEqual(conflict.status_code, 409)
        self.assertContains(conflict, "changed in another tab", status_code=409)
        self.assertEqual(self.client.session[CONVERSION_NONCE_KEY], current_nonce)
        self.assertEqual(Quote.objects.count(), 0)
