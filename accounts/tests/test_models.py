import tempfile
import uuid
from pathlib import Path

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.models import Profile, User
from accounts.services import canonicalize_email

EmailUserForm = modelform_factory(User, fields=("email",))


class CanonicalizeEmailTests(SimpleTestCase):
    def test_strips_then_normalizes_then_casefolds(self):
        value = " \tＳＴＲＡＳＳＥ@Example.ＣＯＭ\u00a0"

        self.assertEqual(canonicalize_email(value), "strasse@example.com")

    def test_rejects_internal_control_characters(self):
        for control_character in ("\n", "\r", "\t", "\x00", "\x1f", "\x7f", "\x85"):
            with self.subTest(control_character=repr(control_character)):
                with self.assertRaises(ValidationError):
                    canonicalize_email(f"person{control_character}@example.com")

    def test_rejects_empty_email_after_canonicalization(self):
        for value in ("", " \t\u00a0\n"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    canonicalize_email(value)


class UserModelTests(TestCase):
    def test_user_has_uuid_key_and_uses_email_for_authentication(self):
        user = User.objects.create_user(
            "  Freelancer@Example.COM\u00a0",
            password="correct horse battery staple",
        )

        self.assertIsInstance(user.pk, uuid.UUID)
        self.assertEqual(user.pk.version, 4)
        self.assertEqual(user.email, "freelancer@example.com")
        self.assertEqual(user.get_username(), "freelancer@example.com")
        self.assertEqual(User.USERNAME_FIELD, "email")
        self.assertEqual(User.REQUIRED_FIELDS, [])
        self.assertTrue(User._meta.get_field("email").unique)
        self.assertIsNone(user.email_verified_at)
        self.assertIsNone(user.last_activity_at)
        with self.assertRaises(FieldDoesNotExist):
            User._meta.get_field("username")

    def test_natural_key_lookup_and_email_uniqueness_are_case_insensitive(self):
        user = User.objects.create_user(
            "Freelancer@Example.COM",
            password="correct horse battery staple",
        )

        self.assertEqual(
            User.objects.get_by_natural_key(" FREELANCER@EXAMPLE.COM "),
            user,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(
                "freelancer@EXAMPLE.com",
                password="another secure password",
            )

    def test_create_superuser_normalizes_email_and_sets_permissions(self):
        admin = User.objects.create_superuser(
            "Admin@Example.COM",
            password="correct horse battery staple",
        )

        self.assertEqual(admin.email, "admin@example.com")
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)

    def test_direct_save_canonicalizes_email(self):
        user = User(email="  Direct@Example.COM\u00a0")

        user.save()

        self.assertEqual(user.email, "direct@example.com")

    def test_direct_save_rejects_whitespace_only_email(self):
        with self.assertRaises(ValidationError):
            User(email=" \t\u00a0\n").save()

    def test_direct_save_rejects_case_insensitive_duplicate(self):
        User.objects.create_user("direct@example.com")

        with self.assertRaises(IntegrityError), transaction.atomic():
            User(email="DIRECT@EXAMPLE.COM").save()

    def test_model_form_save_canonicalizes_email(self):
        form = EmailUserForm(data={"email": " Form@Example.COM\u00a0"})

        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()

        self.assertEqual(user.email, "form@example.com")

    def test_model_form_cannot_create_case_insensitive_duplicate(self):
        User.objects.create_user("form@example.com")
        form = EmailUserForm(data={"email": "FORM@EXAMPLE.COM"})

        if form.is_valid():
            with self.assertRaises(IntegrityError), transaction.atomic():
                form.save()
        else:
            self.assertTrue(form.errors)

    def test_database_constraint_rejects_bulk_created_case_variants(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create(
                [
                    User(email="Bulk@Example.com"),
                    User(email="bulk@EXAMPLE.COM"),
                ]
            )

    def test_natural_key_rejects_whitespace_only_email(self):
        with self.assertRaises(ValidationError):
            User.objects.get_by_natural_key(" \t\u00a0")


class ProfileModelTests(TestCase):
    def create_user(self):
        return User.objects.create_user(
            "freelancer@example.com",
            password="correct horse battery staple",
        )

    def test_create_for_user_uses_valid_defaults_once(self):
        user = self.create_user()

        profile = Profile.create_for_user(user)
        repeated_profile = Profile.create_for_user(user)

        self.assertEqual(profile, repeated_profile)
        self.assertEqual(profile.user, user)
        self.assertEqual(user.profile, profile)
        self.assertEqual(profile.full_name, "")
        self.assertEqual(profile.business_name, "")
        self.assertEqual(profile.phone, "")
        self.assertEqual(profile.website, "")
        self.assertEqual(profile.default_currency, "USD")
        self.assertEqual(profile.default_deposit_percentage, 50)
        self.assertEqual(profile.default_quote_validity_days, 30)
        self.assertEqual(profile.default_accent_color, "#E85D3F")
        self.assertEqual(profile.logo.name, "")
        self.assertIsNotNone(profile.created_at)
        self.assertIsNotNone(profile.updated_at)
        profile.full_clean()

    def test_default_deposit_percentage_requires_five_point_increments(self):
        profile = Profile.create_for_user(self.create_user())

        for value in (0, 5, 100):
            with self.subTest(valid_value=value):
                profile.default_deposit_percentage = value
                profile.full_clean()

        for value in (-1, 52, 101):
            with self.subTest(invalid_value=value):
                profile.default_deposit_percentage = value
                with self.assertRaises(ValidationError):
                    profile.full_clean()

    def test_default_quote_validity_requires_one_to_ninety_days(self):
        profile = Profile.create_for_user(self.create_user())

        for value in (1, 45, 90):
            with self.subTest(valid_value=value):
                profile.default_quote_validity_days = value
                profile.full_clean()

        for value in (0, 91):
            with self.subTest(invalid_value=value):
                profile.default_quote_validity_days = value
                with self.assertRaises(ValidationError):
                    profile.full_clean()

    def test_currency_and_accent_color_defaults_are_constrained(self):
        profile = Profile.create_for_user(self.create_user())

        for value in ("USD", "EUR", "GBP"):
            with self.subTest(valid_currency=value):
                profile.default_currency = value
                profile.full_clean()
        for value in ("CAD", "usd", ""):
            with self.subTest(invalid_currency=value):
                profile.default_currency = value
                with self.assertRaises(ValidationError):
                    profile.full_clean()

        profile.default_currency = "USD"
        profile.default_accent_color = "#A1b2C3"
        profile.full_clean()
        for value in ("E85D3F", "#FFF", "red", "#GGGGGG"):
            with self.subTest(invalid_color=value):
                profile.default_accent_color = value
                with self.assertRaises(ValidationError):
                    profile.full_clean()

    def test_phone_accepts_international_format_and_rejects_invalid_values(self):
        profile = Profile.create_for_user(self.create_user())
        for value in ("+1 (555) 010-1234", "+44 (0)20 7946 0958"):
            with self.subTest(valid_phone=value):
                profile.phone = value
                profile.full_clean()

        for value in ("call 123", "---", "()", "1" * 41):
            with self.subTest(invalid_phone=value):
                profile.phone = value
                with self.assertRaises(ValidationError):
                    profile.full_clean()

    def test_website_requires_https_and_maximum_length(self):
        profile = Profile.create_for_user(self.create_user())
        profile.website = "https://example.com/portfolio"
        profile.full_clean()

        for value in (
            "http://example.com",
            "not-a-url",
            f"https://example.com/{'a' * 2048}",
        ):
            with self.subTest(invalid_website=value[:32]):
                profile.website = value
                with self.assertRaises(ValidationError):
                    profile.full_clean()

    def test_logo_storage_uses_a_generated_filename(self):
        profile = Profile.create_for_user(self.create_user())

        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            profile.logo.save("company-logo.png", ContentFile(b"logo"), save=True)
            logo_name = profile.logo.name
            profile.logo.delete(save=False)

        self.assertNotEqual(logo_name, "company-logo.png")
        self.assertNotIn("company-logo", logo_name)
        self.assertEqual(Path(logo_name).suffix, ".png")
