import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import RateLimitBucket, User
from accounts.services import hash_rate_limit_key
from scripts import e2e_seed

RUN_ID_ERROR = (
    "E2E_RUN_ID must be 1-64 lowercase ASCII letters or digits "
    "separated by single hyphens."
)
VALID_RUN_ID_BOUNDARIES = ("a", "a" * 64)
INVALID_RUN_IDS = (
    " round-5-run",
    "round-5-run ",
    "\tround-5-run",
    "round-5-run\t",
    "\nround-5-run",
    "round-5-run\n",
    "Round-5-run",
    "round_5_run",
    "",
    "-leading",
    "trailing-",
    "double--hyphen",
    "a" * 65,
)


class E2ESeedCleanupTests(TestCase):
    def setUp(self):
        self.allowed_user = User.objects.create_user(
            "e2e-owner@example.test",
            password="correct horse battery staple",
        )
        self.unrelated_user = User.objects.create_user(
            "e2e-prefix-only@example.test",
            password="correct horse battery staple",
        )
        self.run_id = "test-run"
        self.run_root = Path(tempfile.gettempdir()) / "scopequote-e2e" / self.run_id
        self.media_root = self.run_root / "media"
        self.static_root = self.run_root / "static"
        self.output_root = self.run_root / "playwright"
        self.identity_root = self.run_root / "identities"
        self.identity_registry = self.identity_root / "emails.log"
        for root in (
            self.media_root,
            self.static_root,
            self.output_root,
            self.identity_root,
        ):
            root.mkdir(parents=True, exist_ok=True)
        self.identity_registry.write_text("", encoding="utf-8")
        environment_patch = patch.dict(
            os.environ,
            {
                "E2E_DATABASE_NAME": str(connection.settings_dict["NAME"]),
                "E2E_ALLOW_DESTRUCTIVE_RESET": "1",
                "SCOPEQUOTE_ENV": "test",
                "E2E_RUN_ID": self.run_id,
                "E2E_RUN_ROOT": str(self.run_root),
                "E2E_MEDIA_ROOT": str(self.media_root),
                "E2E_STATIC_ROOT": str(self.static_root),
                "E2E_OUTPUT_DIR": str(self.output_root),
                "E2E_IDENTITY_ROOT": str(self.identity_root),
                "E2E_IDENTITY_REGISTRY": str(self.identity_registry),
            },
        )
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        identity_patches = (
            patch.object(e2e_seed, "E2E_USER_EMAIL", self.allowed_user.email),
            patch.object(
                e2e_seed,
                "E2E_ALLOWED_USER_EMAILS",
                frozenset({self.allowed_user.email}),
                create=True,
            ),
            patch.object(e2e_seed, "E2E_CLIENT_IP", "127.0.0.1"),
            patch.object(e2e_seed, "E2E_CLIENT_IPS", frozenset({"127.0.0.1"})),
        )
        for identity_patch in identity_patches:
            identity_patch.start()
            self.addCleanup(identity_patch.stop)

    def test_cleanup_rejects_repository_temporary_root(self):
        project_root = Path(__file__).resolve().parents[2]
        with patch(
            "scripts.e2e_seed.tempfile.gettempdir",
            return_value=str(project_root),
        ):
            with self.assertRaisesRegex(RuntimeError, "outside"):
                e2e_seed._isolated_storage_roots()

    def test_cleanup_rejects_shared_default_database_identity(self):
        with patch.dict(os.environ, {"E2E_DATABASE_NAME": "postgres"}):
            with self.assertRaisesRegex(RuntimeError, "dedicated"):
                e2e_seed._require_configuration()

    def test_run_id_contract_accepts_exact_boundaries(self):
        temporary_base = Path(tempfile.gettempdir())
        for run_id in VALID_RUN_ID_BOUNDARIES:
            with self.subTest(run_id_length=len(run_id)):
                run_root = temporary_base / "scopequote-e2e" / run_id
                environment = {
                    "E2E_RUN_ID": run_id,
                    "E2E_RUN_ROOT": str(run_root),
                    "E2E_MEDIA_ROOT": str(run_root / "media"),
                    "E2E_STATIC_ROOT": str(run_root / "static"),
                    "E2E_OUTPUT_DIR": str(run_root / "playwright"),
                    "E2E_IDENTITY_ROOT": str(run_root / "identities"),
                    "E2E_IDENTITY_REGISTRY": str(
                        run_root / "identities" / "emails.log"
                    ),
                }
                with patch.dict(os.environ, environment):
                    roots = e2e_seed._isolated_storage_roots()
                self.assertEqual(
                    set(roots),
                    {
                        "E2E_MEDIA_ROOT",
                        "E2E_STATIC_ROOT",
                        "E2E_OUTPUT_DIR",
                        "E2E_IDENTITY_ROOT",
                    },
                )

    def test_cleanup_rejects_run_ids_outside_the_strict_grammar(self):
        for run_id in INVALID_RUN_IDS:
            with self.subTest(run_id=run_id):
                with patch.dict(os.environ, {"E2E_RUN_ID": run_id}):
                    with self.assertRaises(RuntimeError) as raised:
                        e2e_seed._isolated_storage_roots()
                self.assertEqual(str(raised.exception), RUN_ID_ERROR)

    def test_run_id_validation_does_not_trim_empty_or_padded_values(self):
        for run_id in (
            "",
            " round-5-run",
            "round-5-run ",
            "\tround-5-run",
            "round-5-run\t",
            "\nround-5-run",
            "round-5-run\n",
        ):
            with self.subTest(run_id=run_id):
                with patch.dict(os.environ, {"E2E_RUN_ID": run_id}):
                    with self.assertRaises(RuntimeError) as raised:
                        e2e_seed._isolated_storage_roots()
                self.assertEqual(str(raised.exception), RUN_ID_ERROR)

    def test_cleanup_removes_only_registered_run_identities(self):
        first_user = User.objects.create_user(
            "e2e-account-test-run-chromium-desktop-retry-regression-retry-1@example.test",
            password="correct horse battery staple",
        )
        second_user = User.objects.create_user(
            "e2e-account-test-run-chromium-desktop-retry-regression-retry-2@example.test",
            password="correct horse battery staple",
        )
        self.identity_registry.write_text(
            f"{first_user.email}\n{second_user.email}\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self.identity_registry.read_text(encoding="utf-8").splitlines(),
            [first_user.email, second_user.email],
        )

        e2e_seed.cleanup()

        self.assertFalse(User._base_manager.filter(pk=first_user.pk).exists())
        self.assertFalse(User._base_manager.filter(pk=second_user.pk).exists())
        self.assertTrue(User._base_manager.filter(pk=self.unrelated_user.pk).exists())

    def test_cleanup_removes_only_explicit_test_identities(self):
        e2e_seed.cleanup()

        self.assertFalse(User._base_manager.filter(pk=self.allowed_user.pk).exists())
        self.assertTrue(User._base_manager.filter(pk=self.unrelated_user.pk).exists())

    def test_cleanup_removes_known_guest_sessions_and_rate_buckets_only(self):
        guest_session = SessionStore()
        guest_session["scopequote.guest_estimate"] = {
            "description": "E2E: isolated guest flow",
        }
        guest_session.create()
        other_session = SessionStore()
        other_session["ordinary"] = {"value": "keep"}
        other_session.create()
        rate_key = f"accounts.login:127.0.0.1:{self.allowed_user.email}"
        other_rate_key = "accounts.login:127.0.0.1:other@example.test"
        now = timezone.now()
        RateLimitBucket.objects.create(
            key=hash_rate_limit_key(rate_key),
            window_start=now,
            counter=1,
            expires_at=now + timedelta(minutes=1),
        )
        RateLimitBucket.objects.create(
            key=hash_rate_limit_key(other_rate_key),
            window_start=now,
            counter=1,
            expires_at=now + timedelta(minutes=1),
        )

        e2e_seed.cleanup()

        self.assertFalse(
            Session.objects.filter(session_key=guest_session.session_key).exists()
        )
        self.assertTrue(
            Session.objects.filter(session_key=other_session.session_key).exists()
        )
        self.assertFalse(
            RateLimitBucket.objects.filter(key=hash_rate_limit_key(rate_key)).exists()
        )
        self.assertTrue(
            RateLimitBucket.objects.filter(
                key=hash_rate_limit_key(other_rate_key)
            ).exists()
        )

    def test_cleanup_removes_only_isolated_generated_directories(self):
        (self.media_root / "upload.bin").write_bytes(b"test")
        (self.static_root / "asset.css").write_text("test", encoding="utf-8")
        with tempfile.TemporaryDirectory() as production_root:
            production_path = Path(production_root)
            sentinel = production_path / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with override_settings(
                MEDIA_ROOT=str(self.media_root), STATIC_ROOT=str(self.static_root)
            ):
                e2e_seed.cleanup()

            self.assertTrue(sentinel.exists())
        self.assertFalse(self.media_root.exists())
        self.assertFalse(self.static_root.exists())
        self.assertFalse(self.output_root.exists())
        self.assertFalse(self.identity_root.exists())
