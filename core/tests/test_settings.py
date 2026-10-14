import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VALID_SHARE_TOKEN_KEY = "c2NvcGVxdW90ZS1kZXYtb25seS1rZXktMzJieXRlcyE="
SETTING_ENVIRONMENT_KEYS = (
    "SCOPEQUOTE_ENV",
    "DEBUG",
    "SECRET_KEY",
    "ALLOWED_HOSTS",
    "DATABASE_URL",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "SESSION_COOKIE_SECURE",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_SAMESITE",
    "CSRF_COOKIE_SECURE",
    "CSRF_COOKIE_HTTPONLY",
    "TRUST_PROXY_HEADERS",
    "SECURE_PROXY_SSL_HEADER",
    "SECURE_SSL_REDIRECT",
    "EMAIL_BACKEND",
    "EMAIL_HOST",
    "EMAIL_USE_TLS",
    "EMAIL_USE_SSL",
    "SHARE_TOKEN_ENCRYPTION_KEY",
)


class SettingsContractTests(SimpleTestCase):
    def import_settings(self, **overrides):
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in SETTING_ENVIRONMENT_KEYS:
            environment.pop(name, None)
        environment.update(
            {
                "EMAIL_USE_TLS": "true",
                "EMAIL_USE_SSL": "false",
                "EMAIL_SENDER_VERIFIED": "true",
                "DEFAULT_FROM_EMAIL": "quotes@quote.example.com",
                "SERVER_EMAIL": "quotes@quote.example.com",
                "EMAIL_HOST": "smtp.quote.example.com",
                "EMAIL_PORT": "587",
            }
        )
        environment.update(overrides)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from scopequote import settings; "
                    "print(settings.DEBUG); "
                    "print(settings.DATABASES['default']['NAME']); "
                    "print(settings.DATABASES['default']['USER']); "
                    "print(settings.DATABASES['default']['PASSWORD']); "
                    "print(settings.SHARE_TOKEN_ENCRYPTION_KEY); "
                    "print(settings.SECURE_PROXY_SSL_HEADER); "
                    "print(settings.SECURE_SSL_REDIRECT); "
                    "print(settings.EMAIL_BACKEND)"
                ),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_default_configuration_is_not_debug(self):
        result = self.import_settings()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "False")

    def test_production_rejects_debug(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="true",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG", result.stderr)

    def test_production_rejects_missing_secret(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            ALLOWED_HOSTS="example.test",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY", result.stderr)

    def test_production_rejects_insecure_session_cookie(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            SESSION_COOKIE_SECURE="false",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SESSION_COOKIE_SECURE", result.stderr)

    def test_production_rejects_insecure_csrf_cookie(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            CSRF_COOKIE_SECURE="false",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CSRF_COOKIE_SECURE", result.stderr)

    def test_production_rejects_missing_share_token_key(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHARE_TOKEN_ENCRYPTION_KEY", result.stderr)

    def test_production_rejects_non_smtp_email_backend(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EMAIL_BACKEND", result.stderr)

    def test_production_rejects_empty_email_host(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            EMAIL_HOST="",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EMAIL_HOST", result.stderr)

    def test_production_requires_explicit_proxy_opt_in(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            SECURE_PROXY_SSL_HEADER="HTTP_X_FORWARDED_PROTO,https",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[5], "None")

    def test_production_accepts_explicit_proxy_opt_in(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            TRUST_PROXY_HEADERS="true",
            SECURE_PROXY_SSL_HEADER="HTTP_X_FORWARDED_PROTO,https",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines()[5],
            "('HTTP_X_FORWARDED_PROTO', 'https')",
        )

    def test_production_rejects_proxy_opt_in_without_header(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            TRUST_PROXY_HEADERS="true",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECURE_PROXY_SSL_HEADER", result.stderr)

    def test_production_rejects_disabled_ssl_redirect(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="production",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            ALLOWED_HOSTS="example.test",
            SECURE_SSL_REDIRECT="false",
            SHARE_TOKEN_ENCRYPTION_KEY=VALID_SHARE_TOKEN_KEY,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECURE_SSL_REDIRECT", result.stderr)

    def test_database_url_uses_discrete_password(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="development",
            DEBUG="true",
            SECRET_KEY="test-only-key",
            DATABASE_URL="postgresql://postgres@localhost:5432/postgres",
            POSTGRES_DB="postgres",
            POSTGRES_USER="postgres",
            POSTGRES_PASSWORD="password",
            POSTGRES_HOST="localhost",
            POSTGRES_PORT="5432",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines()[1:4],
            ["postgres", "postgres", "password"],
        )

    def test_invalid_share_token_key_is_rejected(self):
        result = self.import_settings(
            SCOPEQUOTE_ENV="local",
            DEBUG="false",
            SECRET_KEY="test-only-key",
            SHARE_TOKEN_ENCRYPTION_KEY="not-a-32-byte-key",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHARE_TOKEN_ENCRYPTION_KEY", result.stderr)
