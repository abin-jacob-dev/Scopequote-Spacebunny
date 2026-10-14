import os
import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from accounts.models import Profile, User
from quotes.models import (
    Quote,
    QuoteEvent,
    QuoteLineItem,
    QuoteVersion,
    ShareLink,
)
from quotes.services.guest import GUEST_ESTIMATE_KEY
from quotes.services.links import (
    create_share_link,
    decrypt_link_token,
)
from quotes.services.snapshots import build_quote_snapshot
from quotes.services.storage import (
    MAX_LOGO_BYTES,
    MAX_LOGO_PIXELS,
    abandoned_profile_logo_keys,
    abandoned_staged_logo_keys,
    cleanup_abandoned_staged_logos,
    commit_version_logo,
    delete_object,
    stage_logo,
)

PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
TEST_TOKEN_KEY = "c2NvcGVxdW90ZS1kZXYtb25seS1rZXktMzJieXRlcyE="


def image_bytes(image_format="PNG", size=(12, 9), metadata=False):
    output = BytesIO()
    image = Image.new("RGB", size, "#E85D3F")
    options = {}
    if metadata and image_format == "PNG":
        info = PngInfo()
        info.add_text("private_metadata", "must-not-survive")
        options["pnginfo"] = info
    image.save(output, format=image_format, **options)
    return output.getvalue()


def uploaded(name, content_type, data, image_format=None):
    return SimpleUploadedFile(name, data, content_type=content_type)


def make_published_quote(owner, *, now=NOW):
    quote = Quote.objects.create(
        owner=owner,
        quote_number=f"SQ-2026-{uuid.uuid4().int % 10000:04d}",
        project_title="Secure quotation",
        project_description=(
            "A complete project overview with implementation, testing, and handoff."
        ),
        category="website",
        delivery_days=5,
        complexity="standard",
        revision_rounds=0,
        urgency="standard",
        experience="intermediate",
        currency="USD",
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
        issue_date=date(2026, 9, 24),
        valid_until=date(2026, 10, 24),
        deposit_percentage=50,
        terms="Payment follows the agreed schedule.",
    )
    QuoteLineItem.objects.create(
        quote=quote,
        label="Project fee",
        amount=Decimal("100.00"),
    )
    quote.status = Quote.Status.SENT
    quote.sent_at = now
    quote.save()
    version = QuoteVersion.objects.create(
        quote=quote,
        version_number=1,
        snapshot=build_quote_snapshot(quote),
        frozen_total=quote.chosen_total,
        currency=quote.currency,
    )
    quote.current_published_version = version
    Quote._base_manager.filter(pk=quote.pk).update(current_published_version=version)
    return quote, version


def command_cleanup():
    try:
        from core.management.commands.cleanup_expired_data import Command
    except (ImportError, ModuleNotFoundError):
        raise AssertionError("cleanup_expired_data command is required") from None
    return Command


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=TEST_TOKEN_KEY,
    SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1",
)
class MediaValidationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "media-owner@example.com",
            password=PASSWORD,
        )

    def test_rejects_invalid_type_svg_animation_and_spoofed_extension(self):
        invalid_uploads = [
            uploaded(
                "logo.png", "image/png", b"<svg xmlns='http://www.w3.org/2000/svg'/>"
            ),
            uploaded("logo.gif", "image/gif", image_bytes("GIF")),
        ]
        animated = BytesIO()
        frames = [Image.new("RGB", (4, 4), color) for color in ("red", "blue")]
        frames[0].save(
            animated,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
        invalid_uploads.append(uploaded("logo.webp", "image/webp", animated.getvalue()))

        for item in invalid_uploads:
            with self.subTest(name=item.name):
                with self.assertRaises(ValidationError):
                    stage_logo(item, owner_id=self.owner.pk)

    def test_rejects_decoded_pixel_limit_and_uploaded_byte_limit(self):
        oversized_pixels = Image.new("RGB", (2001, 2001), "white")
        output = BytesIO()
        oversized_pixels.save(output, format="PNG")

        with self.assertRaises(ValidationError):
            stage_logo(
                uploaded("pixels.png", "image/png", output.getvalue()),
                owner_id=self.owner.pk,
            )
        with self.assertRaises(ValidationError):
            stage_logo(
                uploaded("oversized.png", "image/png", b"x" * (MAX_LOGO_BYTES + 1)),
                owner_id=self.owner.pk,
            )

    def test_reencodes_and_strips_metadata_with_generated_owner_path(self):
        upload = uploaded(
            "private-company-logo.png",
            "image/png",
            image_bytes(metadata=True),
        )
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            stored = stage_logo(upload, owner_id=self.owner.pk)
            self.assertTrue(
                stored.storage_key.startswith(f"quotes/logos/staging/{self.owner.pk}/")
            )
            self.assertNotIn("private-company-logo", stored.storage_key)
            self.assertRegex(Path(stored.storage_key).stem, r"^[0-9a-f-]{36}$")
            with default_storage.open(stored.storage_key, "rb") as source:
                encoded = source.read()
            image = Image.open(BytesIO(encoded))
            self.assertNotIn("private_metadata", image.info)
            self.assertNotIn("exif", image.info)
            self.assertEqual(stored.content_type, "image/png")
            self.assertEqual(stored.byte_size, len(encoded))
            self.assertLessEqual(image.width * image.height, MAX_LOGO_PIXELS)

    def test_commit_version_logo_creates_version_key_and_removes_staging_object(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            stored = stage_logo(
                uploaded("logo.png", "image/png", image_bytes()),
                owner_id=self.owner.pk,
            )
            version_id = uuid.uuid4()
            destination = commit_version_logo(stored, version_id=version_id)

            self.assertTrue(
                destination.startswith(f"quotes/versions/{self.owner.pk}/{version_id}.")
            )
            self.assertTrue(default_storage.exists(destination))
            self.assertFalse(default_storage.exists(stored.storage_key))
            delete_object(destination)

    def test_version_logo_route_requires_owner_and_serves_version_object(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            quote, version = make_published_quote(self.owner)
            stored = stage_logo(
                uploaded("logo.png", "image/png", image_bytes()),
                owner_id=self.owner.pk,
            )
            version.logo_object_key = commit_version_logo(stored, version_id=version.pk)
            version.snapshot = {
                **version.snapshot,
                "logo_object_key": version.logo_object_key,
                "version_logo_object_key": version.logo_object_key,
                "brand": {
                    **version.snapshot["brand"],
                    "logo_object_key": version.logo_object_key,
                    "version_logo_object_key": version.logo_object_key,
                },
            }
            version.save = None
            QuoteVersion._base_manager.filter(pk=version.pk).update(
                logo_object_key=version.logo_object_key,
                snapshot=version.snapshot,
            )
            client = Client()
            response = client.get(
                reverse("quotes:version-logo", kwargs={"version_id": version.pk})
            )
            self.assertEqual(response.status_code, 404)
            client.force_login(self.owner)
            response = client.get(
                reverse("quotes:version-logo", kwargs={"version_id": version.pk})
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "image/png")
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(quote.owner, self.owner)

    def test_abandoned_upload_cleanup_preserves_live_draft_logo(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            stored = stage_logo(
                uploaded("draft.png", "image/png", image_bytes()),
                owner_id=self.owner.pk,
            )
            quote = Quote.objects.create(
                owner=self.owner,
                project_title="Draft with logo",
                logo_object_key=stored.storage_key,
            )
            timestamp = (NOW - timedelta(days=2)).timestamp()
            os.utime(default_storage.path(stored.storage_key), (timestamp, timestamp))

            self.assertNotIn(
                stored.storage_key,
                abandoned_staged_logo_keys(now=NOW),
            )
            cleanup_abandoned_staged_logos(now=NOW)

            self.assertTrue(default_storage.exists(stored.storage_key))
            self.assertEqual(quote.logo_object_key, stored.storage_key)

    def test_abandoned_logo_scans_use_bounded_reference_queries(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                staged_key = stage_logo(
                    uploaded("staged.png", "image/png", image_bytes()),
                    owner_id=self.owner.pk,
                ).storage_key
                profile_key = f"profiles/{self.owner.pk}/orphan.png"
                default_storage.save(profile_key, ContentFile(image_bytes()))
                timestamp = (NOW - timedelta(days=2)).timestamp()
                os.utime(default_storage.path(staged_key), (timestamp, timestamp))
                os.utime(default_storage.path(profile_key), (timestamp, timestamp))
                with patch("quotes.models.Quote.objects.exclude") as quote_exclude:
                    self.assertEqual(
                        abandoned_staged_logo_keys(now=NOW, limit=1),
                        [staged_key],
                    )
                quote_exclude.assert_not_called()
                with patch(
                    "accounts.models.Profile.objects.exclude"
                ) as profile_exclude:
                    self.assertEqual(
                        abandoned_profile_logo_keys(now=NOW, limit=1),
                        [profile_key],
                    )
                profile_exclude.assert_not_called()

    def test_failed_delete_is_persisted_for_retry_without_exposing_error(self):
        task_model = self._task_model()
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            stored = stage_logo(
                uploaded("logo.png", "image/png", image_bytes()),
                owner_id=self.owner.pk,
            )
            with patch.object(
                default_storage,
                "delete",
                side_effect=OSError("storage unavailable"),
            ):
                delete_object(stored.storage_key)

            task = task_model.objects.get(storage_key=stored.storage_key)
            self.assertEqual(task.attempts, 2)
            self.assertNotIn("storage unavailable", task.last_error)

    def test_cleanup_command_removes_unreferenced_old_profile_object(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            key = f"profiles/{self.owner.pk}/orphan.png"
            default_storage.save(key, ContentFile(image_bytes()))
            timestamp = (NOW - timedelta(days=2)).timestamp()
            os.utime(default_storage.path(key), (timestamp, timestamp))

            call_command("cleanup_expired_data", "--apply", now=NOW, stdout=StringIO())

            self.assertFalse(default_storage.exists(key))

    def _task_model(self):
        try:
            return apps.get_model("accounts", "MediaCleanupTask")
        except (LookupError, KeyError):
            self.fail("MediaCleanupTask model is required")

    def test_cleanup_task_retry_is_idempotent(self):
        task_model = self._task_model()
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            stored = stage_logo(
                uploaded("logo.png", "image/png", image_bytes()),
                owner_id=self.owner.pk,
            )
            with patch.object(
                default_storage,
                "delete",
                side_effect=OSError("storage unavailable"),
            ):
                delete_object(stored.storage_key)
            task = task_model.objects.get(storage_key=stored.storage_key)
            task_pk = task.pk
            output = StringIO()
            call_command("cleanup_expired_data", "--apply", stdout=output)
            call_command("cleanup_expired_data", "--apply", stdout=output)
            self.assertFalse(task_model.objects.filter(pk=task_pk).exists())
            self.assertFalse(default_storage.exists(stored.storage_key))


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=TEST_TOKEN_KEY,
    SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1",
)
class ActivityAndDeletionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            "deletion-owner@example.com",
            password=PASSWORD,
        )
        self.client.force_login(self.user)

    def _tombstone_model(self):
        try:
            return apps.get_model("accounts", "DeletionTombstone")
        except (LookupError, KeyError):
            self.fail("DeletionTombstone model is required")

    def _task_model(self):
        try:
            return apps.get_model("accounts", "MediaCleanupTask")
        except (LookupError, KeyError):
            self.fail("MediaCleanupTask model is required")

    def test_deletion_page_explains_the_recovery_window(self):
        response = self.client.get(reverse("accounts:delete"))

        self.assertContains(response, "30-day recovery window")
        self.assertNotContains(response, "cannot be undone")

    def test_successful_authenticated_mutation_records_activity(self):
        response = self.client.post(
            reverse("accounts:profile"),
            {
                "full_name": "Updated Owner",
                "business_name": "",
                "phone": "",
                "website": "",
                "default_currency": "USD",
                "default_deposit_percentage": 50,
                "default_quote_validity_days": 30,
                "default_accent_color": "#E85D3F",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_activity_at)

    def test_invalid_authenticated_mutation_does_not_record_activity(self):
        self.client.post(
            reverse("accounts:profile"),
            {
                "default_currency": "CAD",
                "default_deposit_percentage": 42,
                "default_quote_validity_days": 30,
                "default_accent_color": "red",
            },
        )

        self.user.refresh_from_db()
        self.assertIsNone(self.user.last_activity_at)

    def test_profile_logo_upload_is_reencoded_without_metadata(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            response = self.client.post(
                reverse("accounts:profile"),
                {
                    "full_name": "Profile Owner",
                    "business_name": "",
                    "phone": "",
                    "website": "",
                    "default_currency": "USD",
                    "default_deposit_percentage": 50,
                    "default_quote_validity_days": 30,
                    "default_accent_color": "#E85D3F",
                    "logo": uploaded(
                        "profile.png",
                        "image/png",
                        image_bytes(metadata=True),
                    ),
                },
            )
            self.assertEqual(response.status_code, 302)
            profile = Profile.objects.get(user=self.user)
            with profile.logo.open("rb") as source:
                stored = Image.open(BytesIO(source.read()))
            self.assertNotIn("private_metadata", stored.info)
            self.assertNotIn("exif", stored.info)

    def test_replacing_profile_logo_deletes_previous_object(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            profile = Profile.create_for_user(self.user)
            profile.logo.save("old.png", ContentFile(image_bytes()), save=True)
            old_key = profile.logo.name

            response = self.client.post(
                reverse("accounts:profile"),
                {
                    "full_name": "Profile Owner",
                    "business_name": "",
                    "phone": "",
                    "website": "",
                    "default_currency": "USD",
                    "default_deposit_percentage": 50,
                    "default_quote_validity_days": 30,
                    "default_accent_color": "#E85D3F",
                    "logo": uploaded("new.png", "image/png", image_bytes()),
                },
            )

            self.assertEqual(response.status_code, 302)
            profile.refresh_from_db()
            self.assertNotEqual(profile.logo.name, old_key)
            self.assertFalse(default_storage.exists(old_key))

    def test_clearing_profile_logo_deletes_previous_object(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            profile = Profile.create_for_user(self.user)
            profile.logo.save("old.png", ContentFile(image_bytes()), save=True)
            old_key = profile.logo.name

            response = self.client.post(
                reverse("accounts:profile"),
                {
                    "full_name": "Profile Owner",
                    "business_name": "",
                    "phone": "",
                    "website": "",
                    "default_currency": "USD",
                    "default_deposit_percentage": 50,
                    "default_quote_validity_days": 30,
                    "default_accent_color": "#E85D3F",
                    "logo-clear": "on",
                },
            )

            self.assertEqual(response.status_code, 302)
            profile.refresh_from_db()
            self.assertFalse(profile.logo)
            self.assertFalse(default_storage.exists(old_key))

    def test_failed_profile_logo_delete_is_queued_for_retry(self):
        task_model = self._task_model()
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            profile = Profile.create_for_user(self.user)
            profile.logo.save("old.png", ContentFile(image_bytes()), save=True)
            old_key = profile.logo.name

            with patch.object(
                default_storage,
                "delete",
                side_effect=OSError("storage unavailable"),
            ):
                response = self.client.post(
                    reverse("accounts:profile"),
                    {
                        "full_name": "Profile Owner",
                        "business_name": "",
                        "phone": "",
                        "website": "",
                        "default_currency": "USD",
                        "default_deposit_percentage": 50,
                        "default_quote_validity_days": 30,
                        "default_accent_color": "#E85D3F",
                        "logo": uploaded("new.png", "image/png", image_bytes()),
                    },
                )

            self.assertEqual(response.status_code, 302)
            self.assertTrue(task_model.objects.filter(storage_key=old_key).exists())

    def test_public_view_does_not_extend_owner_inactivity(self):
        quote, version = make_published_quote(self.user)
        link = create_share_link(version=version, now=NOW)
        old_activity = NOW - timedelta(days=30)
        User.objects.filter(pk=self.user.pk).update(last_activity_at=old_activity)
        client = Client()

        response = client.get(
            reverse("quotes:public-quote", kwargs={"token": decrypt_link_token(link)})
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_activity_at, old_activity)
        self.assertEqual(quote.status, Quote.Status.SENT)

    def test_deletion_quarantines_account_writes_tombstone_and_revokes_access(self):
        tombstone_model = self._tombstone_model()
        quote, version = make_published_quote(self.user)
        link = create_share_link(version=version, now=NOW)
        other_session = SessionStore()
        other_session["_auth_user_id"] = str(self.user.pk)
        other_session["_auth_user_backend"] = (
            "django.contrib.auth.backends.ModelBackend"
        )
        other_session.create()
        self.assertEqual(
            Session.objects.filter(session_key=other_session.session_key).count(),
            1,
        )

        response = self.client.post(
            reverse("accounts:delete"),
            {"password": PASSWORD, "confirm": "on"},
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        tombstone = tombstone_model.objects.get(account_id=self.user.pk)
        self.assertFalse(self.user.is_active)
        self.assertTrue(tombstone.quarantined)
        self.assertEqual(tombstone.source, "owner")
        self.assertIsNotNone(tombstone.deleted_at)
        link.refresh_from_db()
        self.assertIsNotNone(link.revoked_at)
        self.assertEqual(
            QuoteEvent.objects.filter(
                quote=quote,
                share_link=link,
                event_type=QuoteEvent.EventType.REVOKED,
            ).count(),
            1,
        )
        self.assertFalse(SessionStore().exists(other_session.session_key))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_deletion_password_leaves_account_usable_and_creates_no_tombstone(
        self,
    ):
        tombstone_model = self._tombstone_model()
        response = self.client.post(
            reverse("accounts:delete"),
            {"password": "wrong password", "confirm": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(
            tombstone_model.objects.filter(account_id=self.user.pk).count(), 0
        )

    def test_hard_deletion_removes_all_profile_logo_objects(self):
        tombstone_model = self._tombstone_model()
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            profile = Profile.create_for_user(self.user)
            profile.logo.save("current.png", ContentFile(image_bytes()), save=True)
            orphan_key = f"profiles/{self.user.pk}/orphan.png"
            default_storage.save(orphan_key, ContentFile(image_bytes()))
            self.client.post(
                reverse("accounts:delete"),
                {"password": PASSWORD, "confirm": "on"},
            )
            tombstone_model.objects.filter(account_id=self.user.pk).update(
                deleted_at=NOW - timedelta(days=31)
            )

            call_command("cleanup_expired_data", "--apply", now=NOW, stdout=StringIO())

            self.assertFalse(default_storage.exists(profile.logo.name))
            self.assertFalse(default_storage.exists(orphan_key))

    def test_hard_deletion_cleanup_removes_account_after_thirty_days(self):
        tombstone_model = self._tombstone_model()
        self.client.post(
            reverse("accounts:delete"),
            {"password": PASSWORD, "confirm": "on"},
        )
        tombstone_model.objects.filter(account_id=self.user.pk).update(
            deleted_at=NOW - timedelta(days=31)
        )
        self.user.refresh_from_db()
        output = StringIO()
        call_command("cleanup_expired_data", "--apply", stdout=output)

        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertIn("accounts", output.getvalue())


@override_settings(
    SHARE_TOKEN_ENCRYPTION_KEY=TEST_TOKEN_KEY,
    SHARE_TOKEN_ENCRYPTION_KEY_VERSION="v1",
)
class RetentionAndRestoreTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "retention-owner@example.com",
            password=PASSWORD,
        )

    def _tombstone_model(self):
        try:
            return apps.get_model("accounts", "DeletionTombstone")
        except (LookupError, KeyError):
            self.fail("DeletionTombstone model is required")

    def test_cleanup_dry_run_reports_retention_candidates(
        self,
    ):
        command_cleanup()
        old_session = SessionStore()
        old_session[GUEST_ESTIMATE_KEY] = {"expires_at": "2020-01-01T00:00:00+00:00"}
        old_session.set_expiry(value=NOW - timedelta(seconds=1))
        old_session.save()
        from accounts.models import RateLimitBucket

        RateLimitBucket.objects.create(
            key="a" * 64,
            window_start=NOW - timedelta(minutes=2),
            counter=1,
            expires_at=NOW - timedelta(minutes=1),
        )
        inactive = User.objects.create_user(
            "inactive@example.com",
            password=PASSWORD,
        )
        User.objects.filter(pk=inactive.pk).update(
            last_activity_at=NOW - timedelta(days=24 * 31)
        )
        quote, version = make_published_quote(self.owner)
        link = create_share_link(version=version, now=NOW - timedelta(days=2))
        output = StringIO()

        call_command("cleanup_expired_data", "--dry-run", now=NOW, stdout=output)

        self.assertIn("guest sessions", output.getvalue())
        self.assertIn("rate-limit buckets", output.getvalue())
        self.assertIn("inactive accounts", output.getvalue())
        self.assertIn("expired links", output.getvalue())
        self.assertTrue(User.objects.filter(pk=inactive.pk).exists())
        self.assertTrue(SessionStore().exists(old_session.session_key))
        self.assertTrue(RateLimitBucket.objects.exists())
        link.refresh_from_db()
        self.assertIsNone(link.revoked_at)

    def test_retention_cleanup_records_tombstone_before_hard_deletion(self):
        self.owner.last_activity_at = NOW - timedelta(days=24 * 31)
        self.owner.date_joined = NOW - timedelta(days=24 * 31)
        self.owner.save(update_fields=("last_activity_at", "date_joined"))

        call_command("cleanup_expired_data", "--apply", now=NOW, stdout=StringIO())

        tombstone = self._tombstone_model().objects.get(account_id=self.owner.pk)
        self.assertEqual(tombstone.source, "retention")
        self.assertEqual(tombstone.restore_status, "applied")
        self.assertFalse(User.objects.filter(pk=self.owner.pk).exists())

    def test_cleanup_apply_is_bounded_and_replays_expired_link_events_once(self):
        command_cleanup()
        quote, version = make_published_quote(self.owner)
        link = create_share_link(version=version, now=NOW - timedelta(days=2))
        ShareLink.objects.filter(pk=link.pk).update(
            expires_at=NOW - timedelta(seconds=1)
        )
        call_command("cleanup_expired_data", "--apply", now=NOW, stdout=StringIO())
        call_command("cleanup_expired_data", "--apply", now=NOW, stdout=StringIO())

        self.assertEqual(
            QuoteEvent.objects.filter(
                quote=quote,
                share_link=link,
                event_type=QuoteEvent.EventType.EXPIRED,
            ).count(),
            1,
        )

    def test_restore_replay_expires_guest_sessions_by_private_boundary(self):
        from accounts.services import replay_restore_state

        session = SessionStore()
        session["_scopequote.guest_created_at"] = (NOW - timedelta(days=15)).isoformat()
        session[GUEST_ESTIMATE_KEY] = {
            "expires_at": (NOW + timedelta(days=30)).isoformat()
        }
        session.set_expiry(value=NOW + timedelta(days=30))
        session.save()

        result = replay_restore_state(now=NOW)

        self.assertTrue(result.ready)
        self.assertFalse(SessionStore().exists(session.session_key))

    def test_restore_replay_applies_tombstone_sessions_and_link_state_idempotently(
        self,
    ):
        tombstone_model = self._tombstone_model()
        replay = getattr(
            __import__("accounts.services", fromlist=["replay_restore_state"]),
            "replay_restore_state",
            None,
        )
        self.assertIsNotNone(replay, "replay_restore_state service is required")
        quote, version = make_published_quote(self.owner)
        link = create_share_link(version=version, now=NOW)
        revoked_at = NOW - timedelta(hours=1)
        ShareLink.objects.filter(pk=link.pk).update(revoked_at=revoked_at)
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=version,
            share_link=link,
            event_type=QuoteEvent.EventType.REVOKED,
            event_time=revoked_at,
        )
        tombstone_model.objects.create(
            account_id=self.owner.pk,
            deleted_at=NOW - timedelta(days=1),
            source="owner",
            quarantined=True,
        )
        session = SessionStore()
        session["_auth_user_id"] = str(self.owner.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session.create()

        first = replay(now=NOW)
        second = replay(now=NOW)

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(second.reconciled_links, 0)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertFalse(SessionStore().exists(session.session_key))
        link.refresh_from_db()
        self.assertEqual(link.revoked_at, revoked_at)
        self.assertEqual(
            QuoteEvent.objects.filter(
                quote=quote,
                share_link=link,
                event_type=QuoteEvent.EventType.REVOKED,
            ).count(),
            1,
        )
        tombstone = tombstone_model.objects.get(account_id=self.owner.pk)
        self.assertIsNotNone(tombstone.restore_applied_at)
        self.assertEqual(tombstone.restore_status, "applied")

    def test_restore_replay_applies_revocation_event_to_link_state(self):
        from accounts.services import (
            replay_restore_state,
            restore_reconciliation_pending,
        )

        quote, version = make_published_quote(self.owner)
        link = create_share_link(version=version, now=NOW)
        event_time = NOW - timedelta(hours=1)
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=version,
            share_link=link,
            event_type=QuoteEvent.EventType.REVOKED,
            event_time=event_time,
        )

        self.assertTrue(restore_reconciliation_pending(now=NOW))
        result = replay_restore_state(now=NOW)

        self.assertTrue(result.ready)
        link.refresh_from_db()
        self.assertEqual(link.revoked_at, event_time)

    def test_restore_replay_applies_expiry_event_to_link_state(self):
        from accounts.services import (
            replay_restore_state,
            restore_reconciliation_pending,
        )

        quote, version = make_published_quote(self.owner)
        link = create_share_link(version=version, now=NOW)
        event_time = NOW - timedelta(hours=1)
        QuoteEvent.objects.create(
            quote=quote,
            quote_version=version,
            share_link=link,
            event_type=QuoteEvent.EventType.EXPIRED,
            event_time=event_time,
        )

        self.assertTrue(restore_reconciliation_pending(now=NOW))
        result = replay_restore_state(now=NOW)

        self.assertTrue(result.ready)
        link.refresh_from_db()
        self.assertEqual(link.expires_at, event_time)

    def test_restore_readiness_requires_applied_tombstone_user_to_be_quarantined(self):
        from accounts.services import (
            replay_restore_state,
            restore_reconciliation_pending,
        )

        self._tombstone_model().objects.create(
            account_id=self.owner.pk,
            deleted_at=NOW - timedelta(days=1),
            source="owner",
            quarantined=True,
            restore_status="applied",
            restore_applied_at=NOW - timedelta(hours=1),
        )
        User.objects.filter(pk=self.owner.pk).update(
            is_active=True,
            is_quarantined=False,
        )

        self.assertTrue(restore_reconciliation_pending(now=NOW))
        result = replay_restore_state(now=NOW)

        self.assertTrue(result.ready)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertTrue(self.owner.is_quarantined)

    def test_inactivity_uses_calendar_months_at_leap_boundary(self):
        from accounts import services

        cutoff_fn = getattr(services, "account_inactivity_cutoff", None)
        self.assertIsNotNone(cutoff_fn, "calendar-month cutoff is required")
        now = datetime(2028, 2, 29, 12, 0, tzinfo=UTC)
        expected_cutoff = datetime(2026, 2, 28, 12, 0, tzinfo=UTC)
        self.assertEqual(cutoff_fn(now), expected_cutoff)
        exact = User.objects.create_user("exact@example.com", password=PASSWORD)
        newer = User.objects.create_user("newer@example.com", password=PASSWORD)
        User.objects.filter(pk=exact.pk).update(last_activity_at=expected_cutoff)
        User.objects.filter(pk=newer.pk).update(
            last_activity_at=expected_cutoff + timedelta(microseconds=1)
        )
        output = StringIO()

        call_command("cleanup_expired_data", "--dry-run", now=now, stdout=output)

        self.assertIn("inactive accounts: 1", output.getvalue())
        self.assertTrue(User.objects.filter(pk=exact.pk).exists())
        self.assertTrue(User.objects.filter(pk=newer.pk).exists())

    def test_restore_replay_records_expiry_and_revocation_for_same_link(self):
        from accounts.services import replay_restore_state

        quote, version = make_published_quote(self.owner)
        link = create_share_link(version=version, now=NOW - timedelta(days=2))
        revoked_at = NOW - timedelta(hours=1)
        ShareLink.objects.filter(pk=link.pk).update(
            expires_at=NOW - timedelta(seconds=1),
            revoked_at=revoked_at,
        )

        result = replay_restore_state(now=NOW)

        self.assertTrue(result.ready)
        self.assertEqual(
            QuoteEvent.objects.filter(
                quote=quote,
                share_link=link,
                event_type=QuoteEvent.EventType.EXPIRED,
            ).count(),
            1,
        )
        self.assertEqual(
            QuoteEvent.objects.filter(
                quote=quote,
                share_link=link,
                event_type=QuoteEvent.EventType.REVOKED,
            ).count(),
            1,
        )

    def test_restore_readiness_rechecks_applied_tombstone_access_state(self):
        from accounts.services import restore_reconciliation_pending

        quote, version = make_published_quote(self.owner)
        link = create_share_link(version=version, now=NOW)
        self._tombstone_model().objects.create(
            account_id=self.owner.pk,
            deleted_at=NOW - timedelta(days=1),
            source="owner",
            quarantined=True,
            restore_status="applied",
            restore_applied_at=NOW - timedelta(hours=1),
        )
        session = SessionStore()
        session["_auth_user_id"] = str(self.owner.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session.create()

        self.assertTrue(restore_reconciliation_pending(now=NOW))
        self.assertIsNotNone(link)

    def test_restore_command_remains_nonzero_until_replay_completes(self):
        command_cleanup()
        self._tombstone_model().objects.create(
            account_id=self.owner.pk,
            deleted_at=NOW,
            source="owner",
            quarantined=True,
        )
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "cleanup_expired_data",
                "--restore-check",
                now=NOW,
                stdout=output,
            )

    def test_cleanup_entry_point_is_blocked_by_pending_restore_state(self):
        self._tombstone_model().objects.create(
            account_id=self.owner.pk,
            deleted_at=NOW,
            source="owner",
            quarantined=True,
        )

        with self.assertRaises(CommandError):
            call_command("cleanup_expired_data", "--dry-run", now=NOW)

    def test_cleanup_command_dry_run_does_not_delete(self):
        command_cleanup()
        output = StringIO()
        call_command("cleanup_expired_data", "--dry-run", stdout=output)

        self.assertIn("dry-run", output.getvalue().lower())
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())


class SecurityHeaderTests(TestCase):
    def test_form_pages_use_same_origin_referrer_policy_for_csrf(self):
        response = self.client.get("/estimator/")

        self.assertEqual(response["Referrer-Policy"], "same-origin")

        response = self.client.get("/")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    @override_settings(
        SCOPEQUOTE_ENV="production",
        SECURE_HSTS_SECONDS=31536000,
        SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
        SECURE_HSTS_PRELOAD=True,
    )
    def test_production_responses_include_hsts(self):
        response = self.client.get("/")
        self.assertEqual(
            response["Strict-Transport-Security"],
            "31536000; includeSubDomains; preload",
        )

    @override_settings(SECURE_REFERRER_POLICY="unsafe-url")
    def test_private_link_referrer_policy_cannot_be_weakened(self):
        from core.middleware import SecurityHeadersMiddleware

        request = RequestFactory().get("/q/private/")
        response = SecurityHeadersMiddleware(lambda _request: HttpResponse())(request)

        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_private_link_responses_have_no_referrer_policy(self):
        response = self.client.get("/q/not-a-real-token/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_production_settings_keep_media_root_environment_controlled(self):
        self.assertEqual(settings.MEDIA_ROOT, Path(settings.MEDIA_ROOT))
        self.assertIsInstance(settings.SESSION_COOKIE_SECURE, bool)
        self.assertIsInstance(settings.CSRF_COOKIE_SECURE, bool)
