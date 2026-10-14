import re
from pathlib import Path

from django.template.loader import get_template
from django.test import TestCase, override_settings

from accounts.models import User


def _relative_luminance(color):
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    channels = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(foreground, background):
    values = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (values[0] + 0.05) / (values[1] + 0.05)


class EditorialTemplateTests(TestCase):
    def test_home_has_landmarks_skip_link_navigation_and_legal_links(self):
        response = self.client.get("/")
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('<a class="skip-link" href="#main-content">', content)
        self.assertIn('<main id="main-content"', content)
        self.assertIn('aria-label="Primary navigation"', content)
        self.assertIn("Estimate your project", content)
        self.assertIn("Privacy", content)
        self.assertIn("Terms", content)

    def test_estimator_has_labels_csrf_status_and_enhancement_hooks(self):
        response = self.client.get("/estimator/")
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('for="id_description"', content)
        self.assertIn('name="csrfmiddlewaretoken"', content)
        self.assertIn("data-scopequote-pricing", content)
        self.assertIn("data-scopequote-preview", content)
        self.assertIn("JavaScript is optional", content)
        self.assertIn("Calculate estimate", content)
        self.assertIn("Server-verified", content)

    def test_invalid_estimator_preserves_input_and_renders_errors(self):
        response = self.client.post(
            "/estimator/",
            {
                "description": "short",
                "delivery_days": "5",
                "revision_rounds": "0",
                "complexity": "standard",
                "urgency": "standard",
                "experience": "intermediate",
                "currency": "USD",
            },
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("There are errors to fix", content)
        self.assertIn('role="alert"', content)
        self.assertIn("short</textarea>", content)
        self.assertIn("Enter at least 30 characters", content)

    def test_authentication_page_uses_a_named_main_landmark_and_visible_labels(self):
        response = self.client.get("/accounts/login/")
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('<main id="main-content"', content)
        self.assertIn("Sign in to Scopequote", content)
        self.assertIn('for="id_email"', content)
        self.assertIn('name="csrfmiddlewaretoken"', content)

    def test_error_templates_are_available_and_have_recovery_guidance(self):
        for template_name, expected_text in (
            ("403.html", "You do not have access"),
            ("404.html", "That page is not here"),
            ("500.html", "Something went wrong"),
        ):
            template = get_template(template_name)
            rendered = template.render({})
            self.assertIn(expected_text, rendered)

    def test_editor_invalid_fields_have_summary_and_described_errors(self):
        owner = User.objects.create_user(
            "template-editor@example.com",
            password="correct horse battery staple",
        )
        self.client.force_login(owner)
        response = self.client.post(
            "/quotes/new/",
            {
                "project_title": "Draft project",
                "project_description": (
                    "A complete project overview with enough detail."
                ),
                "delivery_days": "5",
                "complexity": "standard",
                "revision_rounds": "0",
                "urgency": "standard",
                "experience": "intermediate",
                "currency": "USD",
                "deposit_percentage": "13",
                "sender_name": "Sender",
                "sender_email": "not-an-email",
                "issue_date": "2026-09-24",
                "valid_until": "2026-10-24",
                "terms": "Payment follows the agreed schedule.",
            },
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("There are errors to fix", content)
        self.assertIn('id="id_sender_email_error"', content)
        self.assertIn('aria-describedby="id_sender_email_error', content)
        self.assertIn('aria-invalid="true"', content)

    def test_invalid_login_renders_field_error_summary(self):
        response = self.client.post(
            "/accounts/login/",
            {"email": "not-an-email", "password": "x"},
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Check the details below and try again", content)
        self.assertIn('id="id_email_error"', content)
        self.assertIn('aria-describedby="id_email_error', content)

    @override_settings(PUBLIC_SUPPORT_EMAIL="support@example.test")
    def test_legal_pages_expose_configured_contact_without_draft_language(self):
        for path in ("/legal/privacy/", "/legal/terms/"):
            response = self.client.get(path)
            content = response.content.decode()
            self.assertEqual(response.status_code, 200)
            self.assertIn("mailto:support@example.test", content)
            self.assertNotIn("launch owner", content.lower())

    def test_pending_mutation_templates_have_loading_status_hooks(self):
        project_root = Path(__file__).resolve().parents[2]
        paths = (
            "templates/quotes/editor.html",
            "templates/quotes/public_quote.html",
            "templates/accounts/login.html",
            "templates/accounts/signup.html",
            "templates/accounts/profile.html",
            "templates/accounts/security.html",
            "templates/accounts/delete.html",
        )
        for relative_path in paths:
            content = (project_root / relative_path).read_text(encoding="utf-8")
            self.assertIn("data-scopequote-pending", content)
            self.assertIn("data-scopequote-pending-status", content)

    def test_editor_copy_is_last_saved_and_public_header_targets_are_touch_sized(self):
        project_root = Path(__file__).resolve().parents[2]
        editor = (project_root / "templates/quotes/editor.html").read_text(
            encoding="utf-8"
        )
        css = (project_root / "static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("Last-saved summary", editor)
        self.assertNotIn("print-accurate", editor)
        self.assertNotIn('class="eyebrow"', editor)
        self.assertIn("min-height: 44px", css)

    def test_private_link_has_safe_wrapping_css_and_markup(self):
        project_root = Path(__file__).resolve().parents[2]
        links = (project_root / "templates/quotes/links.html").read_text(
            encoding="utf-8"
        )
        css = (project_root / "static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("private-link", links)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("word-break: break-word", css)
        self.assertIn("max-width: 100%", css)

    def test_action_muted_placeholder_and_control_tokens_meet_wcag_aa(self):
        css = (Path(__file__).resolve().parents[2] / "static/css/app.css").read_text(
            encoding="utf-8"
        )
        tokens = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", css))
        for foreground in ("ink", "muted", "action", "placeholder"):
            self.assertGreaterEqual(
                _contrast_ratio(tokens[foreground], tokens["paper"]),
                4.5,
                foreground,
            )
        self.assertGreaterEqual(
            _contrast_ratio(tokens["control-border"], tokens["paper"]),
            3,
            "control-border",
        )
        self.assertGreaterEqual(
            _contrast_ratio(tokens["ink"], tokens["document"]),
            4.5,
            "ink/document",
        )


class StaticSurfaceTests(TestCase):
    project_root = Path(__file__).resolve().parents[2]

    def test_required_javascript_files_are_present_without_framework_runtime(self):
        for relative_path in (
            "static/js/pricing-preview.js",
            "static/js/estimator.js",
            "static/js/quote-actions.js",
        ):
            source = (self.project_root / relative_path).read_text(encoding="utf-8")
            self.assertNotRegex(source, r"react|vue|angular|next/router|fetch\s*\(")
            self.assertNotIn("XMLHttpRequest", source)

    def test_print_stylesheet_keeps_pdf_rules_and_avoids_screen_dependencies(self):
        source = (self.project_root / "static/css/print.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("@page", source)
        self.assertIn("counter(page)", source)
        self.assertNotIn("app.css", source)
