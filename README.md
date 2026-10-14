# Scopequote

A calm, explainable project estimator for independent freelancers and small creative studios.

Scopequote turns project scope, delivery duration, complexity, revisions, urgency, and experience into a transparent, deterministic price recommendation — then turns that recommendation into a branded quotation you can export as PDF, share through a private client link, and track to acceptance.

## Why this exists (importance)

Freelancers pricing unfamiliar work usually choose between guesswork and opaque calculators. A wrong number either leaves money on the table or loses the client. Scopequote's differentiator is that **the number is explainable before you are asked to trust it**: every factor (hours, rate, risk buffer, urgency, revision time) is shown as its own row, computed with exact decimal arithmetic from versioned, code-owned reference rates (`pricing-v1`) — no AI-generated estimate, no live FX sleight of hand.

Guiding principles:

- Make the number explainable before asking the user to trust it.
- Keep the first useful estimate available before account creation.
- Preserve freelancer control over the final commercial offer.
- Treat client-facing documents as immutable, versioned records.
- Prefer a calm server-rendered path over a fragile client-side application.

## Features

### Estimator (guest-first, no account needed)

- One focused page (`/estimator/`) with three sections: Project, Delivery, Pricing Context.
- Inputs: project description (30–2,000 chars), delivery days (1–60, with presets like 1 week → 5 days), complexity (Straightforward / Standard / Complex / Advanced), revision rounds (0–10), urgency (Standard / Priority / Rush), experience (Developing / Intermediate / Experienced / Specialist), currency (USD / EUR / GBP).
- Live non-authoritative preview in vanilla JavaScript; the **authoritative result is always recalculated server-side** on form submit, so the flow works with JavaScript disabled.
- Result shows: price range (min / recommended / max), recommended fee, hourly equivalent, factor-by-factor explanation, estimate confidence, and suggested scope.

### Deterministic pricing engine (`pricing/`)

- Reference hourly rates per experience level and currency:

  | Experience   | USD  | EUR  | GBP  |
  | ------------ | ---: | ---: | ---: |
  | Developing   |  $35 |  €35 |  £30 |
  | Intermediate |  $60 |  €55 |  £50 |
  | Experienced  |  $90 |  €80 |  £75 |
  | Specialist   | $125 | €110 | £105 |

  These are transparent reference rates, not market claims. Changing them requires a versioned code change plus test-fixture update — there is no runtime pricing-admin screen.
- Complexity effort multipliers (0.90 / 1.00 / 1.35 / 1.80) with risk buffers (5–20%), urgency multipliers (1.00 / 1.18 / 1.35), 2 included revision rounds, +1.5 hours per extra round.
- Formula: `effective_hours = (days × 4 × complexity) + extra_revisions × 1.5` → base fee → risk-adjusted → urgency-adjusted → ±12% range band.
- All math in `Decimal` (`ROUND_HALF_UP`); recommendations quantized to the nearest 5 units, hourly equivalents to 1 unit. The JS preview uses exact decimal strings / `BigInt` rationals — never binary floating point — with Python/JS parity fixtures.
- Confidence is `high` only for descriptions ≥ 80 chars with ≥ 2 distinct keyword matches in the winning category; otherwise `medium`. Confidence never changes the price.

### Suggested scope (never silently affects price)

- A deterministic keyword classifier maps the description to a category — website/product, brand/visual identity, written content, video/motion, strategy/consulting, or generic/mixed — and proposes editable deliverables (discovery, design, implementation, testing, handoff, …).
- Every suggestion is addable, removable, and rewritable. Description text influences scope content only, never the calculated price.

### Guest quotation + PDF

- Guests can build a session-backed quotation from an estimate and download a PDF (`/quotes/guest.pdf/`) without registering.
- Signing up or logging in attaches the existing guest draft to the new account with no data loss.

### Accounts (`accounts/`)

- Custom email-based user model (UUID primary key, no username), email verification, password reset via signed-token flows.
- Profile defaults: sender details, brand color, logo. Security page: password change, active-session controls. Reauthenticated account deletion with a 30-day recovery window.
- A verified email is required before publishing a client link.

### Quote workspace (`quotes/`)

- Dashboard with recent activity; searchable, filterable quote history.
- Editor with client/project details, deliverables, 1–50 line items (the server-computed sum **is** the total — there is no second price field), deposit percentage (0–100 in steps of 5), timeline, revisions, validity period, notes/exclusions/taxes/cancellation terms.
- Quote numbers are server-generated (`SQ-YYYY-NNNN`), never user input. Currency cannot be changed on an existing quote — duplicate into a new estimate instead.
- Choosing a total outside the displayed recommended range requires an explicit intentional-adjustment confirmation.
- Branded PDFs via WeasyPrint from a print-optimized template (sender brand, quote number, dates, itemized investment, terms, page numbers). Draft PDFs are watermarked `DRAFT`.
- Uploads: PNG/JPEG/WebP logos up to 2 MB and 4M decoded pixels, re-encoded with metadata stripped; SVG rejected.

### Publish, private links, client responses

- Publishing freezes an immutable `QuoteVersion` snapshot; the public page and the PDF render the **same** snapshot, so the link and the document can never disagree.
- Private links (`/q/<token>/`) are random `secrets` tokens (stored hashed + encrypted), expiring, revocable, with a minimal interstitial and an explicit POST to record first view.
- Clients can **Accept** or **Request changes** (optional plain-text note). First response per version wins under a database lock; repeats are idempotent; conflicts are rejected; acceptance revokes remaining active links and locks the version — further work starts by duplicating into a new draft.
- Owners manage links per quote: create, copy (re-encryption safe), revoke, inspect status (`/quotes/<uuid>/links/`).

### Security and privacy

- CSRF on every POST; state changes are POST + redirect; `next` targets validated against open redirects.
- Owner-scoped queries everywhere; unauthorized access returns a non-enumerating 404. Share tokens never appear in logs, analytics, or error messages.
- PostgreSQL-backed rate limits on sign-in, signup, password reset, and public-response endpoints with generic failure messages.
- Hardened production headers (HSTS, frame denial, restrictive CSP, `Referrer-Policy: no-referrer` on private pages).
- Retention: guest sessions live 14 days; staged guest uploads expire after 24h; inactive accounts (24 months) and deletion follow a documented tombstone + 30-day recovery + 35-day backup-expiry procedure.

### Design, accessibility, reliability

- **Editorial Ledger** visual system: warm paper surfaces (`#F2EADF`), document white (`#FFFDF8`), ink (`#20231F`), coral actions (`#E85D3F`), confidence accent (`#D7F45B`). Serif display type, restrained sans UI, self-hosted open-source fonts.
- Targets **WCAG 2.2 AA**: persistent visible labels, focus management, error summaries, keyboard-reachable estimator and client actions, 320px–desktop responsive layouts, `prefers-reduced-motion` respected, core flows completable with JavaScript disabled.
- No frontend framework, no REST API, no client router — plain Django views/forms plus small progressive-enhancement scripts.

### Deliberately out of scope (first release)

AI-generated estimates, live currency conversion, payments/invoicing, contracts/e-signatures, project management, team workspaces, automated client email, native mobile apps, rich-text editing, real-time collaboration.

## When to use it

**Use Scopequote when you are a freelancer or small studio that needs to:**

- price an unfamiliar project in under three minutes and defend the number to a client;
- produce a professional, branded quotation without design or accounting tooling;
- share a version-locked offer link and see whether the client accepted or asked for changes;
- keep a durable, searchable history of quotes across devices (free account).

**Do not use it (yet) when you need** payment collection, bookkeeping, e-signatures, team billing, live FX quotes, or AI-written proposals — those are explicit non-goals of this release.

## How to use (user journeys)

### 1. Estimate without an account

1. Open `/estimator/`, describe the project, pick delivery/complexity/revisions/urgency/experience/currency.
2. The preview updates as you type; submit the form for the server-verified result.
3. Read the range, recommended fee, hourly equivalent, and per-factor explanation.
4. Optionally create a guest quotation and download the PDF — no signup required.

### 2. Save and publish (account)

1. From an estimate, choose **Save quote** → an owned draft is created (`/quotes/new/`, `/quotes/<uuid>/edit/`).
2. Edit client details, line items, schedule, terms, sender snapshot, brand.
3. **Publish** (`/quotes/<uuid>/publish/`) → Django validates client data and freezes an immutable version.
4. Copy the private link, download the matching PDF, or revoke the link from `/quotes/<uuid>/links/`.
5. Publishing locks the quote; revisions start by duplicating it into a new draft.

### 3. Client response (no account needed)

1. Client opens the private link → minimal interstitial → **Open secure quotation** (records first view; owner sees `viewed`).
2. Client clicks **Accept quotation** or **Request changes** (+ optional note).
3. Owner sees the status and note; accepted or change-requested versions are immutable — duplicate to revise.

### Key routes

| Area      | Routes |
| --------- | ------ |
| Public    | `/`, `/estimator/`, `/quotes/guest/`, `/quotes/guest.pdf/`, `/q/<token>/`, `/q/<token>/open/`, `/q/<token>/view/`, `/q/<token>/response/`, `/legal/privacy/`, `/legal/terms/`, `/health/`, `/ready/` |
| Accounts  | `/accounts/signup/`, `/accounts/login/`, `/accounts/logout/`, `/accounts/verify/...`, `/accounts/password-reset/...` |
| Workspace | `/dashboard/`, `/quotes/`, `/quotes/new/`, `/quotes/<uuid>/`, `/quotes/<uuid>/edit/`, `/quotes/<uuid>/publish/`, `/quotes/<uuid>/links/`, `/quotes/<uuid>/pdf/`, `/quotes/<uuid>/duplicate/`, `/settings/profile/`, `/settings/security/`, `/settings/delete/` |

## Running the project

### Prerequisites

- Python 3.12, PostgreSQL 16 reachable with a database + credentials.
- System libraries for PDF rendering (already in `Dockerfile`): Cairo, Pango, GDK-Pixbuf, libjpeg, OpenJPEG, fonts.
- Node.js only for JS unit tests and Playwright browser tests — not for running the app.

### Option A — local development (no Docker)

```bash
cp .env.example .env   # then fill in real values; settings read environment variables
export SCOPEQUOTE_ENV=local DEBUG=True \
  POSTGRES_DB=postgres POSTGRES_USER=postgres POSTGRES_PASSWORD=password \
  POSTGRES_HOST=localhost POSTGRES_PORT=5432
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000/ (landing) and http://127.0.0.1:8000/estimator/.

### Option B — Docker Compose

```bash
cp .env.example .env   # fill in SECRET_KEY, POSTGRES_*, EMAIL_*, SHARE_TOKEN_ENCRYPTION_KEY, ...
docker compose up --build
```

The `web` service runs migrations, cleanup, `collectstatic`, then Gunicorn on `${PORT:-8000}`; `db` is PostgreSQL 16 with a health check gate.

### Environment

All configuration comes from environment variables (see `.env.example` for the full list): `SCOPEQUOTE_ENV` (`local`/`development`/`test`/`production`), `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL` or discrete `POSTGRES_*` settings, cookie/security flags, `SHARE_TOKEN_ENCRYPTION_KEY` (base64, 32 bytes — never commit the real one), SMTP settings (verification + password-reset mail only), media/static roots. In `production`, secure cookies, SSL redirect, and HSTS are enforced.

### Verification (must all pass)

```text
python -m pip check
ruff check .
ruff format --check .
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
npm test
npm run test:e2e
```

CI runs the Django suite against PostgreSQL (not SQLite) and the browser suite against production-like settings.

### Project structure

```text
scopequote/   project settings, root URLs, WSGI/ASGI
core/         landing, legal pages, health/readiness, shared middleware
accounts/     email-based auth, Profile, dashboard, quote list, settings
pricing/      versioned rates (pricing-v1), pure calculation engine, classifier
quotes/       quote models/forms/views, versions, share links, PDF, guest flow
templates/    server-rendered HTML (incl. print-optimized PDF template)
static/       handcrafted CSS + small vanilla JS (estimator, pricing preview, actions)
scripts/      container entrypoint, Playwright e2e setup/teardown/seed helpers
docs/superpowers/specs/   approved product design spec
docs/superpowers/plans/   implementation plan
```

Implementation boundaries (from the approved spec): Python + Django + Django templates only — no DRF, no React/Vue/Angular, no `fetch`/XHR client router; pricing stays deterministic and view-independent; Editorial Ledger styling is preserved.

## Vendors and third-party dependencies

No customer testimonials, performance claims, or partner endorsements exist — the following are the project's actual software vendors and services, listed with their role:

### Python (pinned in `requirements.txt`, declared in `requirements.in`)

- **Django 5.2** — web framework, ORM, auth, forms, templates, migrations.
- **psycopg 3.2 (binary)** — PostgreSQL driver.
- **Pillow 11** — logo upload decoding, validation, re-encoding.
- **WeasyPrint 66** — PDF rendering of the print quotation template (needs system Cairo/Pango libraries, see `Dockerfile`).
- **WhiteNoise 6.9** — static-file serving in production.
- **Gunicorn 23** — production WSGI server (run behind a reverse proxy).
- **cryptography 44+** — encryption of retrievable share-link token copies.
- Dev: **Ruff** (lint + format), **pip-tools** (hash-locked requirements compilation).

### JavaScript / browser testing (`package.json`, dev-only)

- **@playwright/test** — browser verification of guest, account, publishing, link, and client-response flows, plus keyboard/a11y assertions. Node is a test tool only; there is no JS build pipeline or frontend runtime.

### Infrastructure and services

- **PostgreSQL 16** — primary database (`db` service based on `postgres:16-alpine` in Compose; local installs work the same).
- **SMTP provider (bring your own)** — transactional mail only (email verification, password reset, security notices); no marketing or automated client email.
- **Container host (your choice)** — `Dockerfile` + `docker-compose.yml` are vendor-neutral; persistent volume required for media, PostgreSQL with tested backups required for data.
- **System packages (via Debian apt in `Dockerfile`)** — Cairo, Pango, Freetype, Harfbuzz, GDK-Pixbuf, JPEG/OpenJPEG libraries, DejaVu fonts: all in service of WeasyPrint PDF output.

Currencies note: USD, EUR, and GBP are supported through code-owned reference rates — there is **no live foreign-exchange vendor** by design.

## Docs and planning

- Product record: `PRODUCT.md`
- Approved design spec: `docs/superpowers/specs/2026-09-24-scopequote-design.md`
- Implementation plan: `docs/superpowers/plans/2026-09-24-scopequote.md`

## Status

Working local build (Django checks pass; landing, estimator, admin, and login routes return 200 against local PostgreSQL). See the acceptance criteria in the spec (§24) for the launch-readiness checklist: guest estimate → PDF, multi-currency math, save/edit/duplicate/brand/publish, guest-draft conversion on signup, private expiring revocable links, client accept/request-changes, PDF/link snapshot parity, owner isolation, and full lint/unit/browser verification.
