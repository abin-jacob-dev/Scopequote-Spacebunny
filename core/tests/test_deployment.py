from pathlib import Path

from django.test import SimpleTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DeploymentConfigurationTests(SimpleTestCase):
    def setUp(self):
        self.dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
        self.compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
        self.playwright = (PROJECT_ROOT / "playwright.config.js").read_text()
        self.entrypoint = (PROJECT_ROOT / "scripts/entrypoint.sh").read_text()

    def test_startup_runs_restore_reconciliation_gate(self):
        self.assertIn("cleanup_expired_data --restore-check", self.entrypoint)

    def test_container_does_not_enable_access_request_logging(self):
        self.assertNotIn("--access-logfile", self.dockerfile)
        self.assertIn("--error-logfile", self.dockerfile)

    def test_compose_requires_explicit_environment_contracts(self):
        self.assertIn(
            'SCOPEQUOTE_ENV: "${SCOPEQUOTE_ENV:?SCOPEQUOTE_ENV must be set}"',
            self.compose,
        )
        self.assertIn(
            'SECURE_SSL_REDIRECT: "${SECURE_SSL_REDIRECT:?'
            'SECURE_SSL_REDIRECT must be set}"',
            self.compose,
        )
        self.assertIn(
            'TRUST_PROXY_HEADERS: "${TRUST_PROXY_HEADERS:?'
            'TRUST_PROXY_HEADERS must be set}"',
            self.compose,
        )
        self.assertIn(
            'CSRF_TRUSTED_ORIGINS: "${CSRF_TRUSTED_ORIGINS:?'
            'CSRF_TRUSTED_ORIGINS must be set}"',
            self.compose,
        )

    def test_compose_requires_safe_email_and_discrete_database_settings(self):
        self.assertNotIn("console.EmailBackend", self.compose)
        self.assertIn(
            'EMAIL_BACKEND: "${EMAIL_BACKEND:?EMAIL_BACKEND must be set}"', self.compose
        )
        self.assertNotIn("DATABASE_URL:", self.compose)
        self.assertIn("POSTGRES_PASSWORD:", self.compose)

    def test_playwright_requires_dedicated_reset_and_disables_token_artifacts(self):
        self.assertIn("E2E_DATABASE_NAME", self.playwright)
        self.assertIn("E2E_ALLOW_DESTRUCTIVE_RESET", self.playwright)
        self.assertIn("E2E_ALLOW_REUSE_SERVER", self.playwright)
        self.assertIn("reuseExistingServer: false", self.playwright)
        self.assertIn('trace: "off"', self.playwright)
        self.assertIn('video: "off"', self.playwright)
        self.assertIn('screenshot: "off"', self.playwright)
