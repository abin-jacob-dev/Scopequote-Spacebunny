# Scopequote Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Scopequote, a launch-ready Django SaaS that gives freelancers explainable project-price recommendations, professional quotations, PDF export, and version-locked client links.

**Architecture:** Use a single Django monolith with server-rendered templates and normal Django views/forms. Keep pricing, scope classification, quote snapshots, link security, and PDF rendering in isolated services backed by PostgreSQL; use only small vanilla JavaScript enhancements for live estimator previews and browser conveniences.

**Tech Stack:** Python, Django, Django templates, PostgreSQL, Pillow, WeasyPrint, WhiteNoise, Gunicorn, cryptography, Ruff, Django's test runner, vanilla JavaScript, Node's test runner, and Playwright for browser verification.

**Spec:** `docs/superpowers/specs/2026-09-24-scopequote-design.md`

## Global Constraints

- Use Python, Django, Django templates, the Django ORM, and PostgreSQL.
- Do not add Django REST Framework, React, Vue, Angular, or another frontend framework.
- Do not add `fetch`, XHR, GraphQL, or a client-side router; use normal POST/redirect Django flows.
- Keep all currency calculations server-authoritative with `Decimal`, exact configuration strings, and `ROUND_HALF_UP`.
- Keep the initial pricing configuration code-owned and identified as `pricing-v1`.
- Keep JavaScript optional; every core flow must work after a normal form submission with JavaScript disabled.
- Use Django CSRF protection, owner-scoped queries, secure upload validation, rate limits, and non-enumerating public-link errors.
- Published quote versions are immutable; revisions create a new quote draft.
- Store only hashed and encrypted share-token representations, never raw tokens in logs or analytics.
- Use the Editorial Ledger visual system: paper surfaces, ink rules, serif display type, restrained sans-serif UI, and coral actions.
- Do not commit changes unless the user explicitly requests a commit; use review checkpoints instead.
- Every task ends with a focused verification command and a review checkpoint.

---

## File Map

The implementation will use these focused files and directories:

```text
manage.py
pyproject.toml
requirements.in
requirements.txt
requirements-dev.in
requirements-dev.txt
.env.example
.gitignore
Dockerfile
docker-compose.yml
scripts/entrypoint.sh
scopequote/
  __init__.py
  settings.py
  urls.py
  wsgi.py
  asgi.py
core/
  __init__.py
  apps.py
  views.py
  urls.py
  tests/test_pages.py
  tests/test_templates.py
  middleware.py
  management/__init__.py
  management/commands/__init__.py
  management/commands/cleanup_expired_data.py
  tests/test_security.py
accounts/
  __init__.py
  admin.py
  apps.py
  forms.py
  models.py
  services.py
  urls.py
  views.py
  migrations/
  tests/test_models.py
  tests/test_auth.py
pricing/
  __init__.py
  apps.py
  classifier.py
  constants.py
  engine.py
  types.py
  fixtures/pricing_cases.json
  tests/test_classifier.py
  tests/test_engine.py
quotes/
  __init__.py
  admin.py
  apps.py
  forms.py
  models.py
  services/
    __init__.py
    guest.py
    links.py
    pdf.py
    quotes.py
    responses.py
    snapshots.py
    storage.py
  urls.py
  views.py
  migrations/
  tests/test_models.py
  tests/test_guest_flow.py
  tests/test_links.py
  tests/test_quote_views.py
  tests/test_public_views.py
  tests/test_pdf.py
templates/
  base.html
  403.html
  404.html
  500.html
  core/home.html
  core/legal/privacy.html
  core/legal/terms.html
  accounts/login.html
  accounts/signup.html
  accounts/verify.html
  accounts/password_reset.html
  accounts/password_reset_done.html
  accounts/password_reset_confirm.html
  accounts/password_reset_complete.html
  accounts/profile.html
  accounts/security.html
  accounts/delete.html
  pricing/estimator.html
  quotes/dashboard.html
  quotes/list.html
  quotes/editor.html
  quotes/detail.html
  quotes/publish.html
  quotes/links.html
  quotes/print.html
  quotes/public_interstitial.html
  quotes/public_quote.html
static/
  css/app.css
  css/print.css
  js/estimator.js
  js/quote-actions.js
  js/pricing-preview.js
tests/
  js/pricing-preview.test.js
scripts/
  __init__.py
  entrypoint.sh
  e2e_seed.py
playwright/
  tests/guest_flow.spec.js
  tests/account_flow.spec.js
  tests/public_quote.spec.js
package.json
package-lock.json
playwright.config.js
```

`scopequote/settings.py` owns environment parsing, middleware, installed apps, database, email, media, security headers, and production toggles. App modules own domain behavior; templates own presentation; static files own progressive enhancements.

---

### Task 1: Bootstrap the Django project and quality toolchain

**Files:**
- Create: `manage.py`
- Create: `scopequote/__init__.py`
- Create: `scopequote/settings.py`
- Create: `scopequote/urls.py`
- Create: `scopequote/wsgi.py`
- Create: `scopequote/asgi.py`
- Create: `core/__init__.py`
- Create: `core/apps.py`
- Create: `core/views.py`
- Create: `core/urls.py`
- Create: `core/tests/__init__.py`
- Create: `core/tests/test_pages.py`
- Create: `pyproject.toml`
- Create: `requirements.in`
- Create: `requirements-dev.in`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `package.json`
- Create: `.env.example`
- Create: `.gitignore`

**Interfaces:**
- Produces `scopequote.settings` as the only settings module.
- Produces `core.views.home(request) -> HttpResponse` at `/`.
- Produces `core.views.health(request) -> HttpResponse` at `/health/`.
- Produces the project test command `python manage.py test`.

- [ ] **Step 1: Create the dependency manifests and project configuration.**

Write `pyproject.toml` with Ruff configuration and Python 3.12+ metadata. Declare runtime packages for Django, PostgreSQL, Pillow, WeasyPrint, WhiteNoise, Gunicorn, and cryptography. Declare development packages for Ruff and pip-tools; use Django's built-in `manage.py test` runner without a second test framework. Put the same direct dependencies in `requirements.in`/`requirements-dev.in`, then generate `requirements.txt` and `requirements-dev.txt` with `pip-compile --generate-hashes`. Create a private `package.json` with `name`, `version`, and the script `test: "node --test"`; reserve `test:e2e` for Task 14.

- [ ] **Step 2: Scaffold the Django project and core app.**

Run:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
django-admin startproject scopequote .
python manage.py startapp core
python manage.py startapp accounts
python manage.py startapp pricing
python manage.py startapp quotes
```

Keep the generated project package at the repository root. Replace the generated settings with environment-driven configuration for `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, PostgreSQL, email, media, session security, proxy headers, and `SHARE_TOKEN_ENCRYPTION_KEY`.

- [ ] **Step 3: Add the first failing project health test.**

Create `core/tests/test_pages.py` with a `SimpleTestCase` that requests `/` and `/health/`, asserting status `200`, the Scopequote wordmark, the estimator CTA, and a plain health response. Run `python manage.py test core.tests.test_pages` and confirm the test fails before the home view/template exists.

- [ ] **Step 4: Implement the minimal home route and base template.**

Create `core/views.py`, `core/urls.py`, `templates/base.html`, and `templates/core/home.html`. The base template must include a skip link, semantic header/main/footer landmarks, a CSRF-ready block for forms, and a `{% static %}` load. The home route must not call any external service. The health route returns a plain `ok` response without database or cache dependencies.

- [ ] **Step 5: Add environment and repository hygiene files.**

Write `.env.example` with every required variable and safe development values. Write `.gitignore` for `.venv/`, `.env`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `node_modules/`, `playwright-report/`, `test-results/`, `media/`, `staticfiles/`, `.superpowers/`, and local database files.

- [ ] **Step 6: Run the bootstrap checks.**

Run:

```bash
python manage.py check
python manage.py test core.tests.test_pages
ruff check .
ruff format --check .
```

Expected: all commands pass and the test verifies the initial route.

- [ ] **Step 7: Review checkpoint.**

Inspect the diff and confirm no secret values, generated bytecode, or frontend runtime dependency entered the project. Do not commit unless the user explicitly asks.

---

### Task 2: Add the email user and freelancer profile models

**Files:**
- Create: `accounts/__init__.py`
- Create: `accounts/apps.py`
- Create: `accounts/models.py`
- Create: `accounts/services.py`
- Create: `accounts/admin.py`
- Create: `accounts/migrations/__init__.py`
- Create: `accounts/migrations/0001_initial.py`
- Create: `accounts/tests/__init__.py`
- Create: `accounts/tests/test_models.py`
- Modify: `scopequote/settings.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces `accounts.models.User`, a UUID-keyed `AbstractUser` with `email` as `USERNAME_FIELD`.
- Produces `accounts.models.Profile`, a one-to-one profile with defaults and logo storage.
- Produces `canonicalize_email(value: str) -> str` in `accounts/services.py`.

- [ ] **Step 1: Write failing model and canonicalization tests.**

Add tests for UUID user keys, case-insensitive unique email lookup, `canonicalize_email` stripping and NFKC/case-folding behavior, profile default validation, and invalid website/phone values. Run `python manage.py test accounts.tests.test_models` and confirm the expected import/model failures.

- [ ] **Step 2: Implement the custom user and profile.**

Create `User(AbstractUser)` with `id = UUIDField(default=uuid.uuid4, editable=False, primary_key=True)`, `username = None`, `email = EmailField(unique=True)`, `email_verified_at`, and `last_activity_at`. Override the manager to normalize email before `create_user` and `create_superuser`. Implement `canonicalize_email` exactly as `strip` → Unicode NFKC → Unicode case-fold → reject control characters. Implement `Profile` with the ranges in the spec, a generated logo path, and a `create_for_user` helper.

- [ ] **Step 3: Add the first migration and settings switch.**

Set `AUTH_USER_MODEL = "accounts.User"` before generating any project migrations. Generate `accounts/migrations/0001_initial.py` and verify it contains the custom user and profile. Do not create a second user model later.

- [ ] **Step 4: Run focused tests and migration checks.**

Run:

```bash
python manage.py makemigrations --check --dry-run
python manage.py test accounts.tests.test_models
python manage.py check
```

Expected: no missing migration changes and all model tests pass.

- [ ] **Step 5: Review checkpoint.**

Inspect the migration for UUID keys, unique email constraints, and no plaintext password or token fields. Do not commit unless explicitly requested.

---

### Task 3: Implement the deterministic pricing engine

**Files:**
- Create: `pricing/__init__.py`
- Create: `pricing/apps.py`
- Create: `pricing/constants.py`
- Create: `pricing/types.py`
- Create: `pricing/engine.py`
- Create: `pricing/fixtures/pricing_cases.json`
- Create: `pricing/tests/__init__.py`
- Create: `pricing/tests/test_engine.py`

**Interfaces:**
- Produces `pricing.types.EstimateInputs` with `description`, `delivery_days`, `complexity`, `revision_rounds`, `urgency`, `experience`, and `currency`.
- Produces `pricing.types.EstimateResult` with `effective_hours`, `minimum`, `recommended`, `maximum`, `hourly_equivalent`, `pricing_config_version`, and `factors`.
- Produces `pricing.engine.calculate_estimate(inputs: EstimateInputs) -> EstimateResult`.
- Produces `pricing.constants.PRICING_CONFIG_VERSION = "pricing-v1"`.

- [ ] **Step 1: Write formula, boundary, and rounding tests.**

Add parameterized tests for the approved rates and multipliers, 0/1/2/3/10 revisions, every currency, duration 1/60, complexity and urgency choices, positive-hour invariants, and half-boundary values around five-unit fee quantization and one-unit hourly quantization. Add a test that `pricing_config_version == "pricing-v1"`.

- [ ] **Step 2: Define typed inputs and immutable configuration.**

Use frozen dataclasses for `EstimateInputs` and `EstimateResult`. Store all numeric configuration values as exact `Decimal` strings. Keep the approved rate table, complexity table, urgency table, included revision count, and `Decimal("28")` precision in `pricing/constants.py`. Do not read these values from the database or environment.

- [ ] **Step 3: Implement the server-authoritative calculation.**

Implement the exact sequence from the spec: effective hours, base fee, risk buffer, urgency, exact range values, final half-up quantization, and hourly equivalent. Store human-readable factor rows in `EstimateResult.factors` without changing the numeric outputs. Raise `ValueError` for unknown enum values or non-positive effective hours.

- [ ] **Step 4: Add canonical JSON fixtures for later JavaScript parity.**

Create cases covering each currency, all complexity levels, all urgency levels, a zero-hour boundary, and half-cent rounding edges. The JSON file must contain input values, expected displayed values, and factor identifiers, not executable JavaScript.

- [ ] **Step 5: Run the focused pricing suite.**

Run:

```bash
python manage.py test pricing.tests.test_engine
ruff check pricing
ruff format --check pricing
```

Expected: all pricing cases pass and the fixture is valid JSON.

- [ ] **Step 6: Review checkpoint.**

Check that no browser-supplied value is accepted by `calculate_estimate`, no intermediate value is rounded, and the server output is the only source used by later views. Do not commit unless explicitly requested.

---

### Task 4: Implement deterministic scope classification

**Files:**
- Create: `pricing/classifier.py`
- Create: `pricing/tests/test_classifier.py`

**Interfaces:**
- Produces `pricing.classifier.ScopeSuggestion` with `category`, `matched_phrases`, `confidence`, and `suggested_items`.
- Produces `pricing.classifier.classify_description(description: str) -> ScopeSuggestion`.

- [ ] **Step 1: Write classification tests.**

Cover each exact keyword category, phrase-overlap behavior such as `visual identity` versus `identity`, zero-score generic results, tied mixed results, winner confirmation, and the 80-character confidence boundary. Assert that classification never changes a price.

- [ ] **Step 2: Implement normalization and scoring.**

Use the exact pipeline and lexicons in the spec. Longest phrase matches take precedence; distinct non-overlapping matches score one point. Return `medium` for all results except a non-mixed winner with at least 80 normalized characters and two matches.

- [ ] **Step 3: Return editable suggestion templates.**

Return copies of the fixed deliverable lists so callers cannot mutate module state. Use the mixed template for zero-score and top-score ties, and return matched phrases for the UI confirmation message.

- [ ] **Step 4: Run focused tests and quality checks.**

Run `python manage.py test pricing.tests.test_classifier`, `ruff check pricing`, and `ruff format --check pricing`. Expected: all classifier cases pass.

- [ ] **Step 5: Review checkpoint.**

Confirm the classifier is deterministic, has no network calls, and never feeds numeric pricing multipliers. Do not commit unless explicitly requested.

---

### Task 5: Define quote models, snapshots, and line-item arithmetic

**Files:**
- Create: `quotes/__init__.py`
- Create: `quotes/apps.py`
- Create: `quotes/models.py`
- Create: `quotes/admin.py`
- Create: `quotes/migrations/__init__.py`
- Create: `quotes/migrations/0001_initial.py`
- Create: `quotes/services/__init__.py`
- Create: `quotes/services/quotes.py`
- Create: `quotes/services/snapshots.py`
- Create: `quotes/tests/__init__.py`
- Create: `quotes/tests/test_models.py`

**Interfaces:**
- Produces `quotes.models.Quote`, `QuoteLineItem`, `QuoteNumberSequence`, `QuoteVersion`, `ShareLink`, and `QuoteEvent` foundations.
- Produces `quotes.services.quotes.allocate_quote_number(*, now: datetime) -> str`.
- Produces `quotes.services.quotes.calculate_line_item_total(items: Iterable[QuoteLineItem]) -> Decimal`.
- Produces `quotes.services.snapshots.build_quote_snapshot(quote: Quote) -> dict`.

- [ ] **Step 1: Write failing model and arithmetic tests.**

Test UUID quote keys, owner ownership, valid statuses, line-item sum behavior, unique quote numbers, year rollover, UTC sequence allocation, draft immutability after publish, immutable `QuoteEvent` creation, and the required publish fields. Test `deposit_amount` as a derived two-decimal value and `realized_hourly_equivalent` as chosen total divided by effective hours.

- [ ] **Step 2: Implement the relational quote models.**

Create the models with explicit choices, decimal fields, UUID primary keys, foreign-key ownership, and indexes for owner/status/date, token hash, and event time. Store `guest_conversion_nonce` as nullable unique. Store snapshot schema version `1` and version-specific logo object keys. Keep the `Quote` mutable only while its status is `draft`.

- [ ] **Step 3: Implement number allocation and line-item services.**

Use `transaction.atomic()` and `select_for_update()` on the year sequence. Use `Decimal("0.01")` for line items and deposits. Reject empty item sets, totals less than or equal to zero, and totals without at least one positive item. Implement the intentional-adjustment predicate using quantized displayed minimum and maximum.

- [ ] **Step 4: Implement immutable snapshot construction.**

`build_quote_snapshot` must copy all client, sender, scope, line-item, date, term, brand, pricing-version, and version-specific logo fields into a new plain dictionary. It must not retain a mutable model reference. Include `snapshot_schema_version: 1`.

- [ ] **Step 5: Generate and test migrations.**

Run `python manage.py makemigrations quotes`, then run `python manage.py test quotes.tests.test_models` and `python manage.py makemigrations --check --dry-run`. Expected: migrations are complete and model tests pass.

- [ ] **Step 6: Review checkpoint.**

Inspect the migration for cascading behavior, non-enumerating owner queries, and no accidental deletion of an immutable published version. Do not commit unless explicitly requested.

---

### Task 6: Implement token security, link lifecycle, and response idempotency

**Files:**
- Create: `quotes/services/tokens.py`
- Create: `quotes/services/links.py`
- Create: `quotes/services/responses.py`
- Create: `quotes/tests/test_links.py`

**Interfaces:**
- Produces `quotes.services.tokens.generate_token() -> str`, `hash_token(token: str) -> str`, and `encrypt_token(token: str, *, share_link_id: UUID, quote_version_id: UUID) -> bytes`.
- Produces `quotes.services.links.create_share_link(*, version: QuoteVersion, now: datetime) -> ShareLink`.
- Produces `quotes.services.links.resolve_share_link(*, token: str, now: datetime) -> ShareLink | None`.
- Produces `quotes.services.links.reissue_share_link(*, link: ShareLink, now: datetime) -> ShareLink`.
- Produces `quotes.services.responses.record_response(*, link: ShareLink, action: str, note: str, idempotency_key: UUID, now: datetime) -> ResponseResult`.
- `ResponseResult` is a frozen dataclass with `status_code: int`, `status: str`, `message: str`, and `existing_response: QuoteEvent | None`.

- [ ] **Step 1: Write failing security and lifecycle tests.**

Cover token entropy/length, SHA-256 lookup, AES-GCM round trips, associated-data mismatch, key rotation failure, automatic expiry, explicit revocation, commercial validity-date capping, reissue, and no raw-token logging. Test response duplicate keys, same-key/different-payload conflicts, concurrent first responses, post-revocation retries, post-expiry retries, and acceptance revoking all active links.

- [ ] **Step 2: Implement token primitives.**

Use `secrets.token_urlsafe(32)`, SHA-256 for lookup, and a versioned 32-byte AES-GCM key from `SHARE_TOKEN_ENCRYPTION_KEY`. Store encrypted token copies with a key version and the share-link/version IDs as associated data. Return a generic unavailable result for unknown, expired, or revoked tokens.

- [ ] **Step 3: Implement link state and lifecycle services.**

Compute `active`, `expired`, and `revoked` from timestamps rather than from event order. Cap `expires_at` at the earlier of 90 days and the quote's `valid_until`. Revoke all active links in the same transaction as an accepted response. Keep the original token unavailable in logs and error messages.

- [ ] **Step 4: Implement response locking and idempotency.**

Lock the quote version row with `select_for_update()`. Store a response idempotency key and SHA-256 payload fingerprint. Same key plus same payload returns the existing result even when the link is inactive; a different payload or a second response type returns a conflict. A first response sets the terminal quote status and creates an immutable `QuoteEvent`.

- [ ] **Step 5: Run the focused link suite.**

Run `python manage.py test quotes.tests.test_links`, `ruff check quotes`, and `ruff format --check quotes`. Expected: all token, lifecycle, and concurrency tests pass.

- [ ] **Step 6: Review checkpoint.**

Verify that application logs, exception messages, and analytics calls never receive raw token values. Do not commit unless explicitly requested.

---

### Task 7: Build the estimator forms, session draft, and guest conversion

**Files:**
- Create: `pricing/forms.py`
- Create: `quotes/services/guest.py`
- Create: `quotes/views.py`
- Create: `quotes/urls.py`
- Create: `quotes/tests/test_guest_flow.py`
- Create: `templates/pricing/estimator.html`

**Interfaces:**
- Produces `pricing.forms.EstimateForm` with the seven validated estimator inputs.
- Produces `quotes.services.guest.get_guest_estimate(request) -> dict | None`, `save_guest_estimate(request, payload)`, and `create_guest_quote(request, payload) -> dict`.
- Produces `quotes.services.guest.convert_guest_quote(request, user) -> Quote | None`.
- Produces `GET/POST /estimator/` and `POST /quotes/guest/`.

- [ ] **Step 1: Write failing form and guest-flow tests.**

Test duration presets, 1–60 day limits, description length, revision limits, all enum values, server calculation overriding any hidden display values, session nonce conflicts, 14-day session expiry, 64 KB payload rejection, guest PDF eligibility, duplicate conversion retries, owner-scoped nonce lookup, and failed-conversion recovery.

- [ ] **Step 2: Implement the estimator form and server view.**

Use Django form validation for every input. On valid POST, call `calculate_estimate` and `classify_description`, store only the validated result in the session, and render the result. Render pricing configuration and factors as safe data attributes for the optional JavaScript preview.

- [ ] **Step 3: Implement session-backed guest quotation state.**

Use the exact session keys, absolute expiry, nonce, size cap, and hidden stale-write behavior from the spec. The guest PDF action must reject a missing or invalid guest quote without creating a database record.

- [ ] **Step 4: Implement idempotent conversion.**

On login/signup, query `Quote.objects.filter(owner=user, guest_conversion_nonce=nonce)`, create the quote in a transaction if absent, and clear guest keys only after commit. Reject a nonce already associated with another account and issue a fresh nonce.

- [ ] **Step 5: Add the estimator template with a no-JavaScript fallback.**

Create a semantic three-section form, visible labels, inline errors, server-rendered result after POST, and a `noscript` explanation. Do not add a client-side router or framework.

- [ ] **Step 6: Run focused tests and Django checks.**

Run:

```bash
python manage.py test quotes.tests.test_guest_flow
python manage.py check
ruff check pricing quotes
ruff format --check pricing quotes
```

Expected: guest estimate, PDF eligibility, and conversion tests pass.

- [ ] **Step 7: Review checkpoint.**

Verify the browser cannot submit a trusted total, owner ID, or conversion result. Do not commit unless explicitly requested.

---

### Task 8: Add authentication, profile settings, rate limits, and landing/dashboard pages

**Files:**
- Create: `accounts/forms.py`
- Modify: `accounts/services.py`
- Create: `accounts/views.py`
- Create: `accounts/urls.py`
- Modify: `accounts/models.py`
- Create: `accounts/migrations/0002_rate_limit.py`
- Create: `accounts/tests/test_auth.py`
- Create: `templates/accounts/login.html`
- Create: `templates/accounts/signup.html`
- Create: `templates/accounts/verify.html`
- Create: `templates/accounts/password_reset.html`
- Create: `templates/accounts/password_reset_done.html`
- Create: `templates/accounts/password_reset_confirm.html`
- Create: `templates/accounts/password_reset_complete.html`
- Create: `templates/accounts/profile.html`
- Create: `templates/accounts/security.html`
- Create: `templates/accounts/delete.html`
- Modify: `templates/core/home.html`
- Create: `templates/quotes/dashboard.html`
- Create: `templates/quotes/list.html`
- Modify: `scopequote/urls.py`

**Interfaces:**
- Produces email-based `LoginView`, `SignupView`, verification, reset, profile, security, and deletion views.
- Produces `accounts.services.enforce_rate_limit(*, key: str, limit: int, window_seconds: int) -> None`.
- Produces `GET /dashboard/` and `GET /quotes/` for authenticated owners.

- [ ] **Step 1: Write failing auth and page tests.**

Test normalized email login, duplicate email rejection, verification requirement before publishing, password reset, session rotation, logout, rate limits, protected redirects, owner dashboard empty state, quote-list filters, profile validation, and reauthenticated deletion.

- [ ] **Step 2: Implement auth forms and transactional email.**

Use Django's `AuthenticationForm` with the custom user and forms for signup, verification notice, reset request, reset confirmation, profile, and deletion. Use the configured SMTP backend for verification and password-reset messages. Rotate the session key after login and password changes.

- [ ] **Step 3: Implement database-backed rate limiting.**

Create `RateLimitBucket` with a hashed route/identifier key, window start, counter, expiration timestamp, and a unique key/window constraint. Implement `enforce_rate_limit` so it increments inside `transaction.atomic()`, raises a generic retry response after the limit, and schedules expired buckets for cleanup.

- [ ] **Step 4: Implement owner dashboard and quote list.**

Query only `Quote.objects.filter(owner=request.user)`, show draft, sent, viewed, accepted, and changes-requested states, and provide empty states. Add accessible search and status filters using GET parameters with validated choices.

- [ ] **Step 5: Build the approved landing and account templates.**

Use the Editorial Ledger palette and semantic sections. Include product outcome, sample result, transparent explanation, trust/security copy, pricing-engine disclaimer, and estimator CTA. Keep legal and privacy links visible.

- [ ] **Step 6: Run auth/page tests and quality checks.**

Run:

```bash
python manage.py test accounts.tests.test_auth core.tests.test_pages
python manage.py check
ruff check accounts core
ruff format --check accounts core
```

Expected: auth, rate-limit, page, and route tests pass.

- [ ] **Step 7: Review checkpoint.**

Verify no protected page leaks another user's quote count or object identifiers. Do not commit unless explicitly requested.

---

### Task 9: Build quote editing, publishing, duplication, and owner permissions

**Files:**
- Create: `quotes/forms.py`
- Modify: `quotes/models.py`
- Create: `quotes/services/quotes.py`
- Create: `quotes/services/storage.py`
- Modify: `quotes/views.py`
- Modify: `quotes/urls.py`
- Create: `templates/quotes/editor.html`
- Create: `templates/quotes/detail.html`
- Create: `templates/quotes/publish.html`
- Create: `templates/quotes/links.html`
- Create: `quotes/tests/test_quote_views.py`

**Interfaces:**
- Produces `quotes.forms.QuoteEditorForm`, `QuoteLineItemFormSet`, and `QuotePublishForm`.
- Produces `quotes.services.quotes.publish_quote(*, quote: Quote, user: User) -> QuoteVersion`.
- Produces `quotes.services.quotes.duplicate_quote(*, quote: Quote, user: User) -> Quote`.
- Produces `quotes.services.storage.StoredLogo`, a frozen dataclass with `storage_key: str`, `content_type: str`, `byte_size: int`, and `owner_id: UUID`.
- Produces `/quotes/new/`, `/quotes/<uuid>/edit/`, `/quotes/<uuid>/publish/`, `/quotes/<uuid>/duplicate/`, and `/quotes/<uuid>/links/`.

- [ ] **Step 1: Write failing editor and permission tests.**

Test draft-only mutation, required publish fields, one-to-fifty line items, positive total, deposit derivation, intentional-adjustment confirmation, stale `updated_at` conflicts, owner-only access returning 404, publish snapshot immutability, quote-number uniqueness, duplication clearing links, and profile defaults.

- [ ] **Step 2: Implement editor and line-item formsets.**

Use a normal Django `Form` for quote metadata and a `formset_factory` for line items. Recalculate the estimate on duration/revision changes. Keep the client total as the server sum of line items. Show the realized hourly equivalent separately from the estimator's recommended equivalent.

- [ ] **Step 3: Implement owner-scoped quote views.**

Create detail, edit, publish, duplicate, and link-management views. Use `get_object_or_404(Quote, pk=pk, owner=request.user)` for every owner route. Reject edits after publication and direct them to duplication.

- [ ] **Step 4: Implement transactional publication.**

Implement `stage_logo`, `commit_version_logo`, and `delete_object` in `quotes.services.storage`. Validate and re-encode the selected image before publication, then copy the selected logo into a version-specific object path, build the immutable snapshot, create the first `QuoteVersion`, allocate a quote number under the sequence lock, and set status to `sent` in one transaction. If the file copy fails, roll back the database transaction and run compensating object cleanup.

- [ ] **Step 5: Build editor and publication templates.**

Render a responsive editor with an A4-like preview, client fields, suggested deliverables, line items, terms, brand controls, explicit draft/published labels, and accessible action states. Do not add a rich-text editor.

- [ ] **Step 6: Run quote view and PDF-adjacent tests.**

Run `python manage.py test quotes.tests.test_quote_views`, `ruff check quotes`, and `ruff format --check quotes`. Expected: ownership, editing, publication, duplication, and transaction tests pass.

- [ ] **Step 7: Review checkpoint.**

Inspect a published snapshot and confirm it contains no mutable model references and no active link from a previous draft. Do not commit unless explicitly requested.

---

### Task 10: Implement public quote viewing and client response flows

**Files:**
- Modify: `quotes/forms.py`
- Modify: `quotes/services/responses.py`
- Modify: `quotes/views.py`
- Modify: `quotes/urls.py`
- Create: `templates/quotes/public_interstitial.html`
- Create: `templates/quotes/public_quote.html`
- Create: `quotes/tests/test_public_views.py`

**Interfaces:**
- Produces `GET /q/<token>/`, `POST /q/<token>/open/`, `GET /q/<token>/view/`, and `POST /q/<token>/response/`.
- Produces `quotes.forms.ClientResponseForm` with `action`, `note`, and UUID `idempotency_key`.
- Produces `record_response(...) -> ResponseResult` with `status_code`, `status`, `message`, and `existing_response`.

- [ ] **Step 1: Write failing public-flow tests.**

Test generic unavailable states for unknown/expired/revoked tokens, interstitial GET not recording a view, open POST recording the first view, read-only rendering, CSRF enforcement, accept/request-changes validation, first-response locking, duplicate retries after revocation, conflicting payloads, and acceptance revoking other links.

- [ ] **Step 2: Implement the interstitial and explicit open action.**

The interstitial exposes only the sender/monogram, quote number, client label, expiry, and a CSRF-protected **Open secure quotation** button. The POST records the first view and redirects to the read-only version. Both responses use `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

- [ ] **Step 3: Implement the read-only quote view.**

Resolve the token hash, validate the link/version state, and render the immutable snapshot with no owner controls. Display total, scope, terms, validity, and a response form. Never expose mutable quote fields or internal IDs in client-facing HTML.

- [ ] **Step 4: Implement client responses.**

Use the response service and hidden idempotency key. Same-key/same-payload retries return the existing result even if the link became inactive; different payloads return a conflict. Plain notes are escaped and limited to 2,000 characters. Accepted responses revoke all active links in the same transaction.

- [ ] **Step 5: Build accessible public templates.**

Use a quiet, document-like layout with clear response actions, error summary, status text, keyboard focus movement, and no marketing navigation. Provide a non-script fallback for the response form.

- [ ] **Step 6: Run public-flow tests and quality checks.**

Run:

```bash
python manage.py test quotes.tests.test_public_views
python manage.py check
ruff check quotes
ruff format --check quotes
```

Expected: all public-link, view, and response tests pass.

- [ ] **Step 7: Review checkpoint.**

Attempt access with another user's quote UUID, a tampered token, an expired link, and a revoked link; all must fail without revealing which condition occurred. Do not commit unless explicitly requested.

---

### Task 11: Add PDF rendering for guest drafts and published versions

**Files:**
- Create: `quotes/services/pdf.py`
- Create: `templates/quotes/print.html`
- Create: `static/css/print.css`
- Create: `quotes/tests/test_pdf.py`
- Modify: `quotes/urls.py`

**Interfaces:**
- Produces `render_guest_quote_pdf(*, quote_data: dict) -> bytes`.
- Produces `render_published_quote_pdf(*, version: QuoteVersion) -> bytes`.
- Produces authenticated `/quotes/<uuid>/pdf/` and guest `/quotes/guest.pdf/` downloads.

- [ ] **Step 1: Write failing PDF tests.**

Test content type `application/pdf`, attachment filename, quote number, client/sender names, total, deposit, terms, long-content wrapping, multi-page numbering, draft labeling, and frozen-version isolation. Mock WeasyPrint only for failure-path tests; test the renderer output for a normal document.

- [ ] **Step 2: Implement the print context builders.**

Build a dedicated context from the guest dictionary or immutable `QuoteVersion` snapshot. Mark current drafts as `DRAFT`; published versions display the frozen commercial terms and no mutable owner actions.

- [ ] **Step 3: Implement WeasyPrint rendering and response headers.**

Render with an explicit base URL limited to trusted static/font paths, set `Content-Disposition: attachment`, set a safe generated filename, and return a retryable error page if rendering fails. Do not pass arbitrary user HTML into the template.

- [ ] **Step 4: Implement print-specific CSS.**

Set A4 page size, margins, page-break rules, table headers, long-word wrapping, and page counters. Keep screen CSS out of the PDF context. Test with a quote containing a long unbroken URL and a multi-page scope.

- [ ] **Step 5: Run PDF and quote tests.**

Run `python manage.py test quotes.tests.test_pdf quotes.tests.test_quote_views`, `ruff check quotes`, and `ruff format --check quotes`. Expected: both guest and published PDF paths pass.

- [ ] **Step 6: Review checkpoint.**

Compare the published PDF text with the public quote snapshot and verify the totals, validity, and terms are identical. Do not commit unless explicitly requested.

---

### Task 12: Build the Editorial Ledger template system and vanilla JavaScript enhancements

**Files:**
- Modify: `templates/base.html`
- Create: `templates/403.html`
- Create: `templates/404.html`
- Create: `templates/500.html`
- Create: `static/css/app.css`
- Modify: `static/css/print.css`
- Create: `static/js/estimator.js`
- Create: `static/js/quote-actions.js`
- Create: `static/js/pricing-preview.js`
- Create: `tests/js/pricing-preview.test.js`
- Modify: `templates/core/home.html`
- Create: `templates/core/legal/privacy.html`
- Create: `templates/core/legal/terms.html`
- Modify: `templates/pricing/estimator.html`
- Modify: `templates/quotes/dashboard.html`
- Modify: `templates/quotes/editor.html`
- Modify: `templates/quotes/public_quote.html`
- Create: `core/tests/test_templates.py`

**Interfaces:**
- Produces `window.ScopequotePricingPreview.calculateFromConfig(config, inputs)` as a pure browser function.
- The same module exports `calculateFromConfig` through CommonJS when loaded by Node tests; no bundler or module framework is used.
- Produces DOM enhancement hooks through `data-scopequote-*` attributes only.
- Produces no global application state store.

- [ ] **Step 1: Write template and static-surface tests.**

Add Django template tests for landmarks, skip link, visible labels, CSRF forms, status text, and error summaries. Add Node tests for the pure pricing preview, canonical JSON fixtures, and no-`fetch` behavior.

- [ ] **Step 2: Implement the shared base template and design tokens.**

Define the Editorial Ledger palette, fluid type scale, spacing scale, focus ring, form controls, buttons, cards, tables, status pills, alert blocks, and responsive breakpoints. Use self-hostable font stacks with system fallbacks. Add reduced-motion rules and 44-pixel touch targets.

- [ ] **Step 3: Implement server-rendered page templates.**

Add the landing, estimator, dashboard, editor, detail, publish, links, public interstitial, public quote, account, and legal templates. Use visible labels, error summaries, semantic tables, status text, and clear empty/error states. Keep the estimator result server-rendered after POST.

- [ ] **Step 4: Implement exact fixed-point JavaScript preview.**

Parse decimal strings, represent rational intermediate values without binary floating-point `Number`, and round only display values with the same canonical fixture cases as Python. If the browser cannot parse the data safely, leave the server-rendered result untouched.

- [ ] **Step 5: Implement non-framework browser conveniences.**

Use only DOM event listeners for live preview, mobile summary expansion, clipboard fallback, Web Share API when available, confirmation dialogs, and dismissible notices. Never send a request from JavaScript; all actions remain normal form submissions.

- [ ] **Step 6: Run template, JavaScript, and accessibility checks.**

Run:

```bash
python manage.py test core.tests.test_templates
npm test
python manage.py check
ruff check .
ruff format --check .
```

Expected: server-rendered templates, browser pricing fixtures, and static quality checks pass.

- [ ] **Step 7: Review checkpoint.**

Disable JavaScript and complete the estimator, quote creation, login, and client response flows. Verify keyboard focus, labels, 320-pixel layout, and reduced motion manually. Do not commit unless explicitly requested.

---

### Task 13: Add account deletion, retention cleanup, secure media handling, and security headers

**Files:**
- Modify: `accounts/services.py` for deletion and activity updates
- Modify: `accounts/models.py`
- Create: `accounts/migrations/0003_deletion_tombstone.py`
- Modify: `quotes/services/storage.py`
- Create: `core/middleware.py`
- Create: `core/management/__init__.py`
- Create: `core/management/commands/__init__.py`
- Create: `core/management/commands/cleanup_expired_data.py`
- Create: `core/tests/test_security.py`
- Modify: `scopequote/settings.py`
- Modify: `scopequote/urls.py`
- Modify: `accounts/views.py`
- Modify: `quotes/services/snapshots.py`

**Interfaces:**
- Produces `quotes.services.storage.stage_logo(upload, *, owner_id) -> StoredLogo`.
- Produces `quotes.services.storage.commit_version_logo(stored_logo, *, version_id) -> str`.
- Produces `quotes.services.storage.delete_object(key) -> None` with compensating retry behavior.
- Produces `accounts.models.DeletionTombstone` with account UUID, deletion timestamp, source, quarantine state, and restore-application state.
- Produces `core.middleware.SecurityHeadersMiddleware`.
- Produces `cleanup_expired_data` management command with `--dry-run` and `--apply` modes.

- [ ] **Step 1: Write failing security and retention tests.**

Test upload type/content/pixel/size rejection, metadata stripping, generated filenames, version-specific logo authorization, compensating cleanup, session expiry, account inactivity, deletion tombstones, link expiry replay, rate-limit cleanup, security headers, and referrer policy.

- [ ] **Step 2: Implement safe media staging and publication copies.**

Use Pillow with a decoded-pixel guard, reject animation and SVG, re-encode allowed images, strip metadata, and generate owner-scoped UUID paths. Stage before the database transaction, commit a version-specific key on publish, and delete staged objects after rollback or failed commit. Persist failed cleanup work for the management command.

- [ ] **Step 3: Implement account activity and deletion lifecycle.**

Update `last_activity_at` only after successful authenticated mutations. Add `DeletionTombstone` to `accounts.models` and generate `0003_deletion_tombstone.py`. Require password reauthentication for deletion, quarantine the account, write a tombstone, revoke links and sessions, and schedule hard deletion after 30 days. Do not let a public client view extend owner inactivity.

- [ ] **Step 4: Implement retention cleanup and backup restore guards.**

The cleanup command removes expired guest sessions/uploads, rate-limit buckets, expired link state, and accounts inactive for 24 months. Restore tooling must apply account tombstones, expire sessions, replay link revocations/expiry, and block readiness until the replay completes.

- [ ] **Step 5: Add production security headers and settings.**

Add HSTS, `X-Content-Type-Options`, frame denial, restrictive CSP, `Referrer-Policy: no-referrer` on private-link responses, secure proxy headers, secure cookies, and environment-controlled media paths. Do not enable debug or permissive headers in production.

- [ ] **Step 6: Run security and cleanup tests.**

Run:

```bash
python manage.py test core.tests.test_security quotes.tests.test_links
python manage.py cleanup_expired_data --dry-run
python manage.py check
ruff check .
ruff format --check .
```

Expected: all security tests pass and dry-run reports eligible records without deleting them.

- [ ] **Step 7: Review checkpoint.**

Inspect logs, response headers, and media paths for secrets, raw tokens, and mutable profile objects. Do not commit unless explicitly requested.

---

### Task 14: Add deployment packaging and automated end-to-end verification

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `scripts/entrypoint.sh`
- Create: `scripts/__init__.py`
- Modify: `package.json`
- Create: `playwright.config.js`
- Create: `scripts/e2e_seed.py`
- Create: `package-lock.json`
- Create: `playwright/tests/guest_flow.spec.js`
- Create: `playwright/tests/account_flow.spec.js`
- Create: `playwright/tests/public_quote.spec.js`
- Modify: `scopequote/settings.py`
- Modify: `.env.example`

**Interfaces:**
- Produces a local PostgreSQL-backed environment through `docker compose`.
- Produces `scripts.e2e_seed.seed() -> None` and `scripts.e2e_seed.cleanup() -> None` for isolated browser data.
- Produces `npm test` for Node pure-function tests and `npm run test:e2e` for Playwright.
- Produces a release smoke sequence: migrate, collect static, start Gunicorn, check health, and exercise one guest flow.

- [ ] **Step 1: Write failing deployment smoke tests.**

Add Playwright specs for guest estimate/PDF, signup and guest conversion, quote publish/link/client acceptance, keyboard navigation, mobile layout, and reduced motion. Add a settings test that production mode rejects debug, missing secret, insecure cookie, or missing encryption-key configuration.

- [ ] **Step 2: Add Node and Playwright manifests without an application build pipeline.**

Update `package.json` to the following test-only manifest shape:

```json
{
  "name": "scopequote-tests",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "test": "node --test",
    "test:e2e": "playwright test"
  }
}
```

Add only Playwright and its test runner as development dependencies, run `npm install --package-lock-only`, and keep the resulting `package-lock.json` with the manifest. Do not add React, Vue, Angular, a bundler, or a client-side router.

- [ ] **Step 3: Create the container and local database setup.**

Write a `Dockerfile` that installs Python dependencies and WeasyPrint system libraries, runs as a non-root user, collects static files, and starts Gunicorn through `scripts/entrypoint.sh`. Write `docker-compose.yml` with PostgreSQL, a persistent media volume, and the Django service. Keep secrets in environment variables.

- [ ] **Step 4: Configure Playwright against the production-like service.**

Set the base URL, database-backed settings, test email backend, isolated media directory, and browser project matrix. Use `scripts/e2e_seed.py` to create test users and clean up test data through the ORM rather than UI-only deletion.

- [ ] **Step 5: Run the full verification suite.**

Run:

```bash
python -m pip check
ruff check .
ruff format --check .
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
npm test
npm run test:e2e
```

Expected: all unit, integration, template, JavaScript, browser, migration, and quality checks pass against PostgreSQL.

- [ ] **Step 6: Run the release smoke sequence.**

Run:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn scopequote.wsgi:application --bind 0.0.0.0:8000
```

In a second shell, request `/health/`, complete one guest estimate, download one guest PDF, and verify the response headers. Stop the process after the smoke check.

- [ ] **Step 7: Review checkpoint.**

Inspect the final file tree, dependency locks, environment example, migrations, and browser artifacts. Confirm no `.env`, media, database, token, or test credential is tracked. Do not commit unless explicitly requested.

---

## Execution Order and Coverage Check

Implement in this order so each task supplies interfaces to the next:

1. Bootstrap settings and tools.
2. Custom user and profile.
3. Pricing engine.
4. Scope classifier.
5. Quote models and snapshot arithmetic.
6. Token/link/response services.
7. Estimator and guest conversion.
8. Auth, landing, dashboard, and rate limits.
9. Quote editor and publication.
10. Public quote and client responses.
11. PDF export.
12. Editorial Ledger templates and vanilla JavaScript.
13. Security, retention, and cleanup.
14. Deployment and end-to-end verification.

Coverage against the approved spec:

- Guest-first estimation, explanatory range, hourly equivalent, and editable scope: Tasks 3, 4, 7, and 12.
- USD/EUR/GBP and deterministic rounding: Task 3 and Task 12 parity tests.
- Professional quotation, branding, line items, terms, and PDF: Tasks 5, 9, 11, and 12.
- Optional account, quote history, and guest conversion: Tasks 2, 7, 8, and 9.
- Private, expiring, revocable, version-locked client links: Tasks 5, 6, 9, 10, and 13.
- Accept/request-changes status tracking: Tasks 6 and 10.
- Security, privacy, retention, accessibility, responsive behavior, and deployment: Tasks 8, 12, 13, and 14.

## Plan Self-Review

- **Spec coverage:** Every approved product section maps to at least one task; no task introduces payments, contracts, AI, email delivery, or a frontend framework.
- **Placeholder scan:** The plan contains no deferred requirements or unspecified implementation steps. Each task names files, interfaces, tests, and commands.
- **Type consistency:** `EstimateInputs`/`EstimateResult`, `ScopeSuggestion`, `Quote`, `QuoteVersion`, `ShareLink`, `ResponseResult`, `StoredLogo`, and the route names are defined before their consumers.
- **Security review:** Token secrecy, owner scoping, CSRF, upload limits, idempotency, retention replay, and no-store public responses are implemented before the end-to-end suite.
- **Verification review:** The final task runs the exact required checks against PostgreSQL and a production-like service.
