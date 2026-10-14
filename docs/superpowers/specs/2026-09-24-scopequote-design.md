# Scopequote Product Design

**Date:** 2026-09-24  
**Status:** Approved in design review; awaiting written-spec approval  
**Product:** Scopequote  
**Stack:** Django, Django templates, PostgreSQL, vanilla CSS, and small progressive-enhancement JavaScript

## 1. Product Summary

Scopequote is a launch-ready SaaS application that helps independent freelancers price unfamiliar projects with confidence. A user describes the work, delivery duration, complexity, revision expectations, urgency, and experience level. Scopequote then returns an explainable price range, a recommended fee, an hourly equivalent, and a factor-by-factor rationale.

The user can turn that recommendation into a professional quotation, customize its scope, client details, brand, payment terms, and schedule, download it as a PDF, publish a private client link, and track whether the client accepts the proposal or requests changes.

The product is guest-first. Visitors can estimate and export a PDF without registering. Creating an account is optional and becomes useful when the user wants durable quote history, cross-device access, and private share links.

## 2. Goals

1. Help a freelancer reach a defensible price recommendation in under three minutes.
2. Make every pricing factor understandable instead of presenting an unexplained number.
3. Turn the estimate into a polished quotation without requiring design or accounting expertise.
4. Support USD, EUR, and GBP without implying live foreign-exchange conversion.
5. Give owners a safe way to share a version-locked quotation and see the client response.
6. Remain understandable and maintainable as a conventional Django monolith with no DRF and no frontend framework.

## 3. Non-Goals

The first release will not include:

- AI-generated scope, estimates, or copy
- Live currency conversion
- Payment collection, invoicing, or accounting integrations
- Contracts, e-signatures, project management, time tracking, or delivery milestones
- Team workspaces, roles, or organization billing
- Automated client email delivery
- Native mobile applications
- Rich-text document editing
- Real-time collaborative editing

## 4. Users and Permissions

### Guest freelancer

A guest can complete the estimator, see a live and server-verified recommendation, customize a session-backed quotation, and download a PDF. Publishing a private client link requires an account.

### Authenticated freelancer

An authenticated freelancer owns quote records and profile defaults. Owners can create, edit, duplicate, publish, revoke links, download documents, and review client responses for their own quotes. Every owner-only query is scoped to the authenticated user.

### Client visitor

A client uses a private, expiring link to view a read-only published version. The client can accept the quotation or request changes with an optional note. A client cannot edit pricing, scope, files, or owner settings.

## 5. Product Architecture

Scopequote will be a single Django project with focused internal apps:

- `core`: landing page, legal pages, health checks, global template context, and site metadata
- `accounts`: custom email-based Django authentication and the freelancer `Profile` model
- `pricing`: code-owned, versioned pricing constants, pure calculation functions, explanation output, and suggested-scope rules
- `quotes`: quote models, forms, owner views, public-link views, quote events, PDF rendering, and storage rules
- `templates`: server-rendered HTML templates
- `static`: handcrafted CSS and small vanilla JavaScript files

The application exposes normal Django HTML routes only. There is no REST API, SPA router, React, Vue, Angular, or client-side state store.

### Technology decisions

- **Web framework:** Django with server-rendered templates
- **Database:** PostgreSQL
- **Authentication:** Django authentication with a custom email-based user model
- **Forms and validation:** Django forms and model validation
- **Frontend:** semantic HTML, custom CSS, and progressive-enhancement JavaScript
- **PDF:** WeasyPrint rendering a print-specific quotation template to PDF
- **Static files:** WhiteNoise in production, served by Django in development
- **User media:** Django storage API on a persistent volume for the initial single-host launch; S3-compatible storage is an optional future adapter
- **Rate limiting:** PostgreSQL-backed counters for authentication and public-response endpoints
- **Transactional email:** SMTP for verification, password reset, and security notices
- **Application server:** Gunicorn behind a production reverse proxy
- **Deployment:** Container-ready without coupling the design to one hosting vendor

## 6. Information Architecture and Routes

### Public routes

- `/` — product landing page and primary estimate CTA
- `/estimator/` — estimate form and live result
- `/accounts/signup/` — account creation and email verification notice
- `/accounts/login/` — login
- `/accounts/verify/<uidb64>/<token>/` — email verification
- `/accounts/password-reset/` — password-reset request
- `/accounts/password-reset/<uidb64>/<token>/` — password-reset completion
- `/quotes/guest/` — POST endpoint that creates or replaces the current session quotation
- `/quotes/guest.pdf/` — PDF for the current guest quotation session
- `/q/<token>/` — private-link interstitial with quote number and explicit open action
- `/q/<token>/open/` — POST endpoint that records the first view and redirects
- `/q/<token>/view/` — read-only private quotation
- `/q/<token>/response/` — POST endpoint for accept/request-changes actions
- `/legal/privacy/` — privacy policy
- `/legal/terms/` — terms of use

### Authenticated routes

- `/dashboard/` — overview and recent quote activity
- `/quotes/` — searchable and filterable quote history
- `/quotes/new/` — create a saved quote from the estimator
- `/quotes/<uuid>/` — quote summary and status
- `/quotes/<uuid>/edit/` — quote editor
- `/quotes/<uuid>/publish/` — review and publish a frozen version
- `/quotes/<uuid>/links/` — create, copy, revoke, and inspect private links
- `/quotes/<uuid>/pdf/` — download the current draft or published version as PDF
- `/quotes/<uuid>/duplicate/` — duplicate a quote as a new draft
- `/settings/profile/` — sender details, defaults, logo, and brand color
- `/settings/security/` — password and active-session controls
- `/settings/delete/` — reauthenticated account-deletion request and recovery status

GET is used only for reading pages and file downloads. Every state-changing action uses POST, CSRF protection, and an explicit redirect after success.

## 7. Core User Journeys

### Estimate without an account

1. The user opens `/estimator/`.
2. They enter a project description and choose delivery, scope, and pricing inputs.
3. Vanilla JavaScript updates a non-authoritative preview as values change.
4. The user submits the standard Django form.
5. The server validates the input and calculates the authoritative result.
6. The result page shows the range, recommendation, hourly equivalent, explanation, and suggested scope.
7. The user can create a guest quotation and download a PDF.
8. If the user signs in or creates an account, the session draft is attached to the new account without losing entered data.

### Save and publish

1. An authenticated user selects **Save quote** from an estimate.
2. Django creates a draft owned by the current user.
3. The user edits client details, line items, schedule, terms, sender snapshot, and brand settings.
4. On publish, Django validates required client data and creates an immutable `QuoteVersion` snapshot.
5. Scopequote creates a random private-link token associated with that version.
6. The owner can copy the link, download the matching PDF, or revoke the link.
7. Publishing locks the quote. Later revisions start by duplicating it into a new editable draft.

### Client response

1. A client opens an active private link and sees a minimal interstitial.
2. The client explicitly selects **Open secure quotation**; Django records the first view through a POST and redirects to the read-only quote.
3. Scopequote updates the owner-visible status to `viewed`.
4. The client selects **Accept quotation** or **Request changes**.
5. Requesting changes may include a short note; the note is plain text.
6. The first response for a published version wins under a database lock. Repeated identical submissions are idempotent, conflicting responses are rejected, and accepted quotations revoke all remaining active links.
7. Accepted and change-requested versions cannot be edited in place. The owner duplicates the quote into a new draft.

## 8. Estimator Inputs and Validation

The estimator contains the following inputs:

| Input | Values | Validation and behavior |
|---|---|---|
| Project description | Plain text | Required, 30–2,000 characters, trimmed and escaped on output |
| Expected duration | Integer working delivery days | Required, 1–60; presets map 3 days → 3, 1 week → 5, 2 weeks → 10, 4 weeks → 20, and 8 weeks → 40 |
| Complexity | Straightforward, Standard, Complex, Advanced | Required single choice |
| Revision rounds | Integer | Required, 0–10; this value also seeds the editable included-revision count in the quote |
| Urgency | Standard, Priority, Rush | Required single choice |
| Experience | Developing, Intermediate, Experienced, Specialist | Required single choice |
| Currency | USD, EUR, GBP | Required single choice |

Duration means focused working days across the delivery calendar, not elapsed calendar days. Changing duration or revision rounds recalculates the recommendation server-side. The quote editor uses the same stored values as the estimator; there is no second pricing engine.

Project titles are 1–160 characters, overviews are 30–3,000 characters, client names are 1–160 characters, client email is normalized and limited to 254 characters, notes and terms are limited to 2,000 characters each, and a quote may contain up to 100 deliverables or 50 line items. Website URLs accept HTTPS only in production; `http://localhost` is allowed only when `DEBUG` is true. Accent colors must be `#RRGGBB` values and the renderer selects a readable foreground color using the WCAG contrast formula.

The estimator is one focused page with three visible sections: Project, Delivery, and Pricing Context. The result remains visible in a sticky side panel on desktop and becomes a compact summary on mobile.

## 9. Pricing Model

All server calculations use `decimal.Decimal` with a local context precision of 28 and `ROUND_HALF_UP`. Configuration decimals are written as exact string literals, never binary floating-point values. Intermediate hours and fees are not rounded; only estimator recommendation outputs are quantized. Estimator recommendation outputs are quantized to the nearest five major units, while quote line-item amounts, chosen totals, and deposits retain two decimal places, and hourly equivalents are quantized to one major unit.

### Reference hourly rates

| Experience | USD | EUR | GBP |
|---|---:|---:|---:|
| Developing | $35 | €35 | £30 |
| Intermediate | $60 | €55 | £50 |
| Experienced | $90 | €80 | £75 |
| Specialist | $125 | €110 | £105 |

These are transparent, code-owned reference rates, not claims about universal market rates. Changes require a versioned code change and test-fixture update; there is no runtime pricing-admin screen in the first release. The initial configuration identifier is `pricing-v1`. Every estimate and quote snapshot records that identifier, and the interface labels the output as guidance.

### Multipliers

| Factor | Value |
|---|---:|
| Straightforward complexity | 0.90 effort multiplier; 5% risk buffer |
| Standard complexity | 1.00 effort multiplier; 10% risk buffer |
| Complex complexity | 1.35 effort multiplier; 15% risk buffer |
| Advanced complexity | 1.80 effort multiplier; 20% risk buffer |
| Standard urgency | 1.00 multiplier |
| Priority urgency | 1.18 multiplier |
| Rush urgency | 1.35 multiplier |
| Included revisions | 2 rounds |
| Each revision above two | Additional 1.5 estimated hours |

### Calculation

```text
effective_hours = (delivery_days × 4 × complexity_multiplier)
                  + max(revision_rounds - 2, 0) × 1.5

base_fee = effective_hours × experience_hourly_rate
risk_adjusted_fee = base_fee × (1 + complexity_risk_buffer)
recommended_fee_exact = risk_adjusted_fee × urgency_multiplier
recommended_min_exact = recommended_fee_exact × 0.88
recommended_max_exact = recommended_fee_exact × 1.12
hourly_equivalent_exact = recommended_fee_exact ÷ effective_hours

recommended_fee = quantize(recommended_fee_exact, 5, ROUND_HALF_UP)
recommended_min = quantize(recommended_min_exact, 5, ROUND_HALF_UP)
recommended_max = quantize(recommended_max_exact, 5, ROUND_HALF_UP)
hourly_equivalent = quantize(hourly_equivalent_exact, 1, ROUND_HALF_UP)
```

The four effective hours per working delivery day represent focused project work rather than an eight-hour attendance schedule. The explanation explicitly states this assumption so users can interpret the estimate correctly.

The JavaScript preview represents every configuration value as an exact decimal string and performs intermediate calculations with `BigInt` rational numerators and denominators. It never uses binary floating-point `Number` arithmetic and never rounds an intermediate value. Only the final display values are quantized. Canonical half-boundary cases cover the five-unit fee quantization, one-unit hourly quantization, two-unit revision increments, and all three currencies.

### Explanation output

The result includes structured rows for:

- Effective hours and delivery assumption
- Complexity effort multiplier
- Selected currency and experience rate
- Included and additional revision time
- Complexity risk buffer
- Urgency multiplier
- Minimum, recommended, and maximum amounts
- Final hourly equivalent
- Estimate confidence

Confidence is `high` when the normalized description is at least 80 characters, the winning category has at least two distinct keyword matches, and it is not a mixed-category result. All other descriptions are `medium`. Confidence is informational and does not change the price.

## 10. Hybrid Scope Suggestions

The project description influences quotation content but never changes the calculated price silently.

A deterministic keyword and template system maps description signals to editable suggested deliverables:

- Website or product work: discovery, structure, responsive design, implementation, testing, and handoff
- Brand or visual identity: discovery, directions, core assets, applications, guidelines, and final files
- Written content: research, outline, draft, editing, review, and delivery
- Video or motion: concept, script or treatment, production, editing, review, and delivery
- Strategy or consulting: discovery, analysis, recommendations, workshop, and written summary
- Unrecognized or mixed work: discovery, project plan, production, review, and handoff

The classifier normalizes text with Unicode NFKC, lowercases it, converts hyphens to spaces, and tokenizes alphanumeric words. It uses these exact phrase sets:

- **Website/product:** `website`, `web site`, `web`, `landing page`, `ecommerce`, `shop`, `cms`, `responsive`, `frontend`, `ui`, `ux`, `web app`
- **Brand/visual:** `brand`, `logo`, `identity`, `visual identity`, `style guide`, `art direction`
- **Written content:** `copy`, `copywriting`, `article`, `blog`, `editorial`, `content writing`, `newsletter`
- **Video/motion:** `video`, `motion`, `animation`, `reel`, `footage`, `editing`
- **Strategy/consulting:** `strategy`, `consulting`, `research`, `audit`, `positioning`, `workshop`, `advisory`

Each distinct matched phrase or token scores one point, with longest phrase matches taking precedence so an overlapping shorter token is not counted twice. A zero-score result uses the generic template. The highest score wins; a tie between the top two categories uses the mixed template. A winning category with a lower second-place score is shown with a prompt to confirm the suggested scope. The estimator shows the detected category and inclusions, and the user can add, remove, or rewrite every item.

## 11. Quotation Experience

The estimate opens a quotation workspace with three functional areas:

1. **Editor navigation:** client and project, scope and deliverables, investment, schedule and terms, review and publish
2. **Live paper preview:** a print-accurate representation of the quotation
3. **Delivery panel:** create link, copy link, download PDF, print, and status summary

The user can edit:

- Project title and overview
- Client name, company, and email
- Sender name, business, email, website, and monogram or logo
- Accent color
- Deliverables and custom line items
- One to fifty line items whose server-computed sum is the final amount
- Deposit percentage
- Delivery timeline
- Included revisions
- Quote validity period
- Notes, exclusions, taxes, and cancellation terms

The chosen total is not a second client-supplied price field. The server seeds one **Project fee** line item with the recommendation, and the final total is always the sum of one to fifty line items. Line-item amounts use two decimal places, may be positive or negative for discounts and adjustments, and must sum to a value greater than zero. The owner supplies a `deposit_percentage` from 0–100 in five-point increments; the server derives `deposit_amount = chosen_total × deposit_percentage / 100` and quantizes it to two decimal places. The deposit is a payment schedule rather than an additional charge. The out-of-range confirmation compares the chosen total with the quantized displayed `recommended_min` and `recommended_max`; an exact total outside that displayed range requires an explicit intentional-adjustment confirmation.

The estimator's hourly equivalent always uses the recommended fee. The quote detail view separately shows the **realized hourly equivalent**, calculated as the chosen total divided by effective hours. Scopequote does not calculate taxes; tax wording is plain owner-entered text and the total remains tax-exclusive unless the owner explicitly states otherwise in the terms.

The quote number follows the format `SQ-YYYY-NNNN`. The system generates it server-side and never accepts it as user input.

## 12. Quotation Document

The PDF uses a separate print-optimized template rendered by WeasyPrint. It contains:

- Sender brand or monogram
- Quote number, issue date, and valid-until date
- Client and sender details
- Project title and overview
- Scope and deliverables
- Itemized investment and total
- Timeline, deposit, and revision terms
- Notes, exclusions, tax statement, and cancellation terms
- Page numbering for documents longer than one page

Screen UI is never passed through the PDF renderer. Published public pages and published PDFs consume the same immutable `QuoteVersion` snapshot so the client link and PDF always represent the same commercial offer. An owner’s current-draft PDF is explicitly marked `DRAFT`, is generated from the mutable draft, and cannot be used as a published client link.

## 13. Data Model

### `User`

- Custom `AbstractUser` model created before the first migration
- UUID primary key to prevent identifier reuse after deletion
- `username` removed; normalized email is the unique login identifier
- Email verification timestamp
- Last owner activity timestamp
- Django password validators enabled
- Login rotates the session key; password change and logout invalidate prior sessions

A verified email is required before an owner can publish a private client link. Email canonicalization is exactly: strip leading and trailing Unicode whitespace, apply Unicode NFKC, apply Unicode case-folding, reject control characters, then store the result in a unique case-insensitive field. No provider-specific dot or plus-address rewriting is applied. Guests can still estimate, save a local guest quotation, and download a PDF. Verification, password reset, and password change use Django's signed-token flows with dedicated routes.

### `DeletionTombstone`

- UUID of the deleted account
- Deletion timestamp
- Source: owner request, retention job, or administrator action
- Quarantine and backup-restore application status

Tombstones are retained for at least as long as the oldest backup that could contain the account and are applied before a restored database accepts traffic.

### `RateLimitBucket`

- Hashed key combining route class, normalized identifier, and time window
- Window start and counter
- Unique key/window constraint
- Expiration timestamp

The counter is incremented in a short database transaction. Expired buckets are removed by the scheduled cleanup job.

### `Profile`

- One-to-one link to `User`
- Full name
- Business name
- Phone, maximum 40 characters
- Website, HTTPS-only and maximum 2,048 characters
- Default currency
- Default deposit percentage from 0–100 in five-point increments
- Default quote validity from 1–90 whole days
- Default accent color in `#RRGGBB` form
- Logo image
- Created and updated timestamps

Email and password remain on the custom `User` model.

### `Quote`

- UUID primary key
- Owner foreign key to `User`
- Optional unique `guest_conversion_nonce` used to make guest-to-account conversion idempotent
- Server-generated quote number
- Status: `draft`, `sent`, `viewed`, `accepted`, or `changes_requested`
- Project title, description, and category
- Duration, complexity, revisions, urgency, experience, and currency inputs
- Pricing configuration version used for the estimate
- Estimated hours, minimum, recommended, maximum, chosen total, and hourly equivalent
- Client name, company, and email
- Sender snapshot fields
- Brand color and version-specific logo reference
- Issue date, valid-until date, `deposit_percentage`, derived `deposit_amount`, timeline, revision terms, notes, exclusions, taxes, and cancellation text
- Current published version reference
- Created, updated, sent, accepted, and last-viewed timestamps

Revocation and expiry are properties of individual `ShareLink` records. Revoking one link does not change the quote status when another active link exists. A quote's commercial `valid_until` date is independent from a link's technical `expires_at` value.

### `QuoteNumberSequence`

- Year primary key
- Last allocated sequence number
- Updated under `select_for_update()` during allocation

A unique database constraint on `Quote.quote_number` prevents duplicates. Sequence allocation uses the UTC year and rolls over naturally on January 1.

### `QuoteLineItem`

- Quote foreign key
- Label, maximum 160 characters
- Optional description, maximum 500 characters
- Signed two-decimal amount
- Sort order
- Creation and update timestamps

The form permits one to fifty items, requires at least one positive amount, and requires the summed total to be greater than zero.

### `QuoteVersion`

- Quote foreign key
- Monotonic version number, initially `1` because published quotes are immutable and revisions use duplication
- Snapshot schema version, initially `1`
- Immutable structured JSON snapshot of all commercial and presentation fields, including pricing configuration identifier, line items, sender details, client details, dates, terms, brand, and version-specific logo object key
- Frozen total and currency
- Creation timestamp
- Reason: `published`

A version is created only when an owner publishes a quote. A published link always points to a `QuoteVersion`, never directly to a mutable `Quote`. The publish form requires a client name and email, project title and overview, at least one positive line item, sender name and email, issue date, valid-until date, and explicit terms text.

### `ShareLink`

- Quote version foreign key
- URL-safe token generated by `secrets.token_urlsafe(32)`, containing 256 bits of entropy
- Unique SHA-256 token hash used for indexed lookup
- AES-GCM encrypted token copy used only to let the owner re-copy the active link
- Encryption key version and associated data containing the share-link and quote-version identifiers
- Expiration and revocation timestamps
- First-viewed timestamp
- Creator and creation timestamps

The encryption key is a versioned 32-byte secret supplied through the environment. Incoming tokens are hashed for lookup; direct comparisons use constant-time helpers. A link is `active` while its timestamp is before `expires_at` and `revoked_at` is null, `expired` once `expires_at` is reached, and `revoked` when an owner explicitly revokes it. Expiry is automatic; revocation is an owner action. A scheduled cleanup may append an `expired` event, but link state is computed from the link record and is not inferred from the latest event. Link state is independent of commercial quote status: the quote follows `draft` → `sent` → `viewed` → the first response (`accepted` or `changes_requested`), and terminal response status remains visible even when all links expire or are revoked. If re-copy decryption fails, the link remains valid but the owner is offered a reissue action that creates a new token and revokes the old one. Raw tokens are never written to application logs, analytics, error payloads, referrers, or cache keys.

### `QuoteEvent`

- Quote and optional share-link foreign keys
- Immutable quote-version identifier for response events
- Event type: `created`, `published`, `viewed`, `accepted`, `changes_requested`, `revoked`, or `expired`
- Optional plain-text client note, maximum 2,000 characters
- Response idempotency key with a unique constraint per quote version
- SHA-256 payload fingerprint of the response action and normalized note
- Event timestamp
- Optional salted user-agent fingerprint

A response transaction locks the quote version. The first accepted or change-requested event for that version becomes authoritative; repeating the same idempotency key returns the existing result even if the link is later expired or revoked, and a different response with that key or another response type returns a conflict state. The public response view resolves the token and checks this idempotency record before returning the generic unavailable state for an expired or revoked link; a retry without the original key is unavailable. Commercial status precedence is `draft` < `sent` < `viewed` < the first response (`accepted` or `changes_requested`); link state is separate. `Quote.status` is updated in the same transaction for efficient dashboard queries, while events remain the append-only audit record. Raw IP addresses and raw user-agent strings are not stored.

### Guest state

Guest estimator and quotation data are stored in Django's database-backed, signed session under `scopequote.guest_estimate`, `scopequote.guest_quote`, and `scopequote.conversion_nonce`. Sessions use an absolute 14-day maximum lifetime from creation; activity does not extend the lifetime. The guest payload is capped at 64 KB after validation. No public guest URL exposes a database identifier. A guest-quotation POST includes a hidden nonce; a stale nonce produces a recoverable conflict instead of overwriting a newer tab. On successful login or registration, the conversion service uses the nonce as `Quote.guest_conversion_nonce` in one transaction and removes the guest keys only after the database commit succeeds. A retry first looks up `Quote.objects.filter(owner=request.user, guest_conversion_nonce=nonce)`; a nonce owned by another account is rejected and a new nonce is issued. A crash after commit but before session cleanup cannot create a second owned quote. A failed conversion leaves the guest draft intact and shows a retryable message.

A quote remains a mutable `draft` until its first publish. Publishing creates one immutable `QuoteVersion`, links the version, and changes the quote to `sent`. A first view changes it to `viewed`; the first client response changes it to `accepted` or `changes_requested`. No endpoint reopens a published quote in place. Duplication creates a new draft with copied client, scope, terms, and brand data but a new quote number, version sequence, and no active links.

## 14. Request and Data Flow

1. Django renders the estimator form with the versioned pricing configuration in safe `data-*` attributes.
2. Vanilla JavaScript mirrors the formula only to provide immediate visual feedback.
3. The browser never supplies an authoritative total, hourly equivalent, owner ID, or share token.
4. A standard form POST recalculates the result in the Python pricing service.
5. Result data is rendered into the normal response and may be stored in the guest session.
6. The guest-quotation POST validates the session nonce and stores a size-limited quotation draft.
7. Save actions validate ownership and persist a `Quote` through the ORM.
8. Publish actions create an immutable `QuoteVersion` and `ShareLink` in a transaction.
9. Public interstitial GETs resolve a token hash without recording a view; the explicit open POST records the first view and redirects to a read-only version.
10. Client response POSTs use CSRF protection, row locking, and the response idempotency key.
11. PDF endpoints render either the guest session draft or a selected immutable version.

No `fetch`, XHR, GraphQL, or DRF endpoint is required.

## 15. Progressive Enhancement and JavaScript Parity

JavaScript is optional. A user with JavaScript disabled receives the same calculator after submitting the form.

The live preview reads the same configuration values rendered by Django and implements the approved formula in a small pure function. Canonical JSON pricing cases are consumed by both Python and JavaScript tests. If a configuration value or formula changes, the parity suite must be updated in the same change.

JavaScript is limited to:

- Live estimate preview
- Mobile summary expansion
- Clipboard and native share actions
- Confirmation dialogs for destructive actions
- Dismissible non-critical notices

Navigation, validation, authorization, pricing, quote mutations, and public responses remain server-controlled.

## 16. Security and Privacy

- Use Django's CSRF protection on every POST.
- Use secure, HTTP-only, same-site session cookies in production; rotate the session key on login and invalidate prior sessions after password change or account deletion.
- Require authentication for quote history, settings, saved drafts, and link management; require a verified email before publishing a client link.
- Scope every quote query to the authenticated owner; return a non-enumerating 404 for unauthorized access.
- Validate all form input on the server and constrain uploads by decoded content, not filename or extension.
- Allow PNG, JPEG, and WebP logos up to 2 MB and 4 million decoded pixels; re-encode the image, strip metadata, reject animation, and use generated filenames.
- Do not accept SVG uploads because active content and external references create avoidable risk.
- Copy a selected logo into a version-specific object at publish time; public and PDF routes authorize access through the version rather than exposing mutable profile media.
- Use autoescaped templates and plain text instead of user-authored HTML.
- Generate URL-safe share tokens with `secrets`, hash them for lookup, encrypt retrievable copies with an environment key, and use constant-time comparisons for direct token checks.
- Expire links automatically at their timestamp and allow explicit owner revocation; never include tokens in analytics, logs, error messages, referrers, or cache keys.
- Protect sign-in, signup, password-reset, and public-response endpoints with PostgreSQL-backed rate limits and generic failure messages.
- Validate `next` redirect targets to prevent open redirects.
- Use PostgreSQL parameterized queries through the Django ORM.
- Set secure proxy headers, HSTS, `X-Content-Type-Options`, frame denial, a restrictive content security policy, and `Referrer-Policy: no-referrer` on private-link pages in production.
- Keep sensitive quote data out of analytics events and error-reporting payloads.
- Document the retention and deletion periods below in the privacy policy.

## 17. Error Handling and Edge Cases

- Invalid estimator or quote forms preserve all user input and show field-specific errors.
- JavaScript failure never blocks the server-rendered estimator.
- Expired or invalid sessions show a recoverable message and retain the submitted values in the page for resubmission.
- Invalid, expired, revoked, or unknown share tokens show the same non-enumerating unavailable state.
- Accepted published versions are immutable; further work starts from a new draft.
- A client response is idempotent so a double submit does not create contradictory events; conflicting response types return a conflict message.
- Stale quote edits are detected with an `updated_at` value; conflicting saves show the current version and preserve the user's unsaved text.
- Invalid logos are rejected with file-type, dimension, and size guidance; URLs accept HTTPS only and colors accept `#RRGGBB` only.
- PDF rendering failures do not mutate quote state and return a retryable error page.
- A database rollback cannot roll back a file-system object, so staged media is deleted in a compensating cleanup step. Failed cleanup is recorded in an internal retry queue and surfaced to operators without exposing user data.
- Storage failures roll back quote creation, publishing, and link creation transactions and leave no database reference to an orphaned object.
- Long descriptions and notes wrap safely in HTML and PDF without truncating commercial terms.
- Currency cannot be changed on an existing quote; duplicating the quote starts a new estimate in the selected currency.

## 18. Retention and Deletion

- Guest sessions have an absolute 14-day lifetime from session creation; abandoned guest uploads expire 24 hours after staging.
- An authenticated account is considered inactive after 24 months without owner activity. Owner activity means a successful authenticated mutation; public client views and automated jobs do not extend the inactivity period.
- Account deletion requires reauthentication, revokes all links and sessions immediately, schedules database and media deletion within 30 days, and allows a recovery window during that period.
- Quote versions, line items, events, and version-specific logos are deleted with their parent quote.
- Application logs retain request metadata for 14 days and never contain quote bodies, email addresses, tokens, or uploaded content.
- Account deletion writes a `DeletionTombstone` at request time and quarantines the account during the 30-day recovery window, so a restored backup cannot make the account usable again.
- Database backups expire within 35 days. Before a restored database serves traffic, the restore procedure reapplies account tombstones, expires guest sessions and staged uploads by their original timestamps, replays link revocation and expiry events, and recomputes link and commercial status; unresolved restore state blocks the readiness check.
- The privacy policy presents these periods and the owner/account deletion contact path.

## 19. Visual Design System

The approved direction is **Editorial Ledger**: warm paper surfaces, strong black rules, confident serif display type, restrained sans-serif UI type, and coral as the action color.

### Core palette

- Paper: `#F2EADF`
- Document: `#FFFDF8`
- Ink: `#20231F`
- Coral action: `#E85D3F`
- Confidence accent: `#D7F45B`
- Warm border: `#DED4C6`
- Muted text: `#6F6B64`

### Design principles

- Use editorial hierarchy, rules, labels, and generous whitespace rather than decorative gradients.
- Use self-hosted open-source display and UI fonts with system-font fallbacks.
- Keep forms calm and scannable, with the live estimate acting as the visual anchor.
- Use compact uppercase labels only for metadata, not body content.
- Animate only small state transitions and respect `prefers-reduced-motion`.
- Keep touch targets at least 44 by 44 CSS pixels.
- Ensure visible keyboard focus and logical heading order.
- Use explicit loading, success, empty, and error states for every mutation.

## 20. Accessibility and Responsive Behavior

The application targets WCAG 2.2 AA.

- All inputs have persistent visible labels and programmatic associations.
- Errors identify the field, explain the correction, and move focus to an error summary when appropriate.
- Color is never the only indicator of quote status or validation.
- The estimator result and quote actions are keyboard reachable.
- Public quote actions use descriptive labels and clear focus movement.
- Tables used for financial data include proper headers and captions where needed.
- The layout supports 320 CSS pixels through large desktop widths.
- The desktop result panel becomes a compact mobile summary without obscuring form controls.
- Reduced-motion preferences disable nonessential transitions.
- Decorative images and SVG shapes are hidden from assistive technology.
- Automated checks are supplemented by keyboard and screen-reader review of the estimator, quote editor, and client response flow.

## 21. Performance and Reliability

- Keep the default page payload lightweight; the application uses no frontend framework runtime.
- Serve the public interstitial and quote view with `Cache-Control: no-store`; only explicitly versioned public assets may be cached.
- Set security and cache headers explicitly for authenticated HTML, guest sessions, public links, and PDFs.
- Use database transactions for publishing, response recording, and guest-to-account conversion.
- Add database indexes for owner/status/date quote lists, token-hash lookup, and event timelines.
- Add bounded cleanup for expired sessions, abandoned guest uploads, and account-deletion work according to the retention policy.
- Render PDFs with strict template, font, image, and execution time limits.
- Expose a lightweight authenticated or deployment-internal health endpoint.
- Configure structured logs with request IDs while excluding quote bodies, email addresses, tokens, and uploaded content.

## 22. Testing and Verification

### Pricing tests

- Every complexity, urgency, experience, revision boundary, and currency combination
- Minimum and maximum input values
- Decimal rounding and stable hourly equivalents
- Invariants: minimum below recommendation, recommendation below maximum, and positive hours
- Canonical Python/JavaScript parity cases, including half-boundary and three-currency fixtures
- Description category and suggested-deliverable rules, including overlap, ties, and mixed results

### Django tests

- Model constraints, indexes, ownership, quote number generation, and immutable versions
- Estimator and quote form validation
- Guest estimate, guest PDF, absolute session expiry, stale guest nonces, guest-to-account conversion, and duplicate conversion retries
- Custom email login, verification, logout, password rules, session rotation, password reset, and protected-route redirects
- Database-backed rate limits and generic authentication failure responses
- Owner isolation and non-enumerating unauthorized access
- Draft editing, stale-write conflict handling, duplication, publishing, and version locking
- Link creation, lookup, automatic expiry, explicit revocation, re-copy encryption, token secrecy, and link-status aggregation
- Public interstitial, first-view POST, accept, request-changes, same-key retry after revocation, conflicting-payload rejection, concurrent response locking, and immutable acceptance
- Dashboard filters and status transitions
- Upload validation, pixel limits, metadata stripping, ownership, version copying, and authorized serving
- Transaction rollback, compensating media cleanup, account-deletion retention, and backup tombstone replay
- Transactional account email, error pages, legal pages, and health endpoint

### Rendering tests

- Core templates render expected accessible labels and status text
- Print template contains the frozen total, client, sender, terms, and quote number
- Generated PDFs have the correct content type, filename, and extractable required text
- HTML and PDF both handle long content and multi-page output

### Browser tests

Use Playwright as a test tool, not as an application runtime, to verify:

- Guest estimate and PDF flow
- Account creation, verification, and guest-draft conversion
- Quote editing and publishing
- Link copy fallback, expiry, and revocation
- Client acceptance and change-request flow
- Keyboard navigation, focus visibility, reduced motion, and mobile layouts
- Automated accessibility assertions for labels, landmarks, focus order, and error summaries

### Required verification commands

The implementation is complete only when these project-provided commands pass:

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

`package.json` is used only for Node-based pure-function tests and Playwright browser verification; it does not provide an application build pipeline or frontend runtime. CI runs the Django suite against PostgreSQL, not SQLite, and runs the browser suite against a production-like settings module. The browser suite and deployment smoke test run in the configured CI environment.

## 23. Configuration and Deployment

Required environment settings include:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `DATABASE_URL` or discrete PostgreSQL settings
- `SECURE_PROXY_SSL_HEADER`
- Session and CSRF cookie security settings
- `SHARE_TOKEN_ENCRYPTION_KEY`
- Media storage settings
- SMTP host, port, credentials, and verified sender for transactional account-security and password-reset messages only
- Production static-file and allowed-host configuration

Dependency versions are committed in a hash-locked Python requirements file and `package-lock.json`; CI installs from those locks and fails on drift. The dependency manifest records the tested Python, Django, Node, PostgreSQL, WeasyPrint, and Playwright versions. The application has no development dependency on SQLite for release verification.

Launch gates are explicit:

- PostgreSQL is reachable, migrated, backed up, and restore-tested; a restored backup is blocked from serving traffic until tombstones, guest-session expiry, and link-state replay complete.
- The SMTP configuration sends a verification and password-reset message in a staging environment.
- The media volume is persistent, private, and included in backup/restore procedures.
- The share-token encryption key is present, versioned, and absent from source control.
- Static files collect successfully and the health endpoint passes.
- Legal and privacy pages contain product-specific text and have been reviewed by the launch owner.
- Rate limits, secure proxy headers, backups, and rollback steps are documented for the selected host.

The release process runs migrations, collects static files, verifies required secrets, starts Gunicorn, and checks the health endpoint. A failed migration, missing SMTP, missing media persistence, or failed restore test blocks release.

## 24. Acceptance Criteria

The release is ready for launch when:

1. A guest can complete an estimate, see an explainable range and hourly equivalent, create a quotation, and download a PDF without registering.
2. JavaScript updates the preview immediately, while Django independently recalculates and validates the result.
3. The project description suggests editable deliverables without silently affecting price.
4. USD, EUR, and GBP calculations follow the approved configuration and rounding rules.
5. A signed-in user can save, edit, duplicate, brand, publish, and track quotes across devices.
6. Registration attaches an existing guest draft without data loss.
7. Published client links are private, expiring, revocable, version-specific, and safe to re-copy by their owner.
8. A client can view, accept, or request changes, and the owner sees the resulting status and note.
9. PDFs and public pages render the same immutable commercial snapshot.
10. Unauthorized users cannot infer or access another user's quotes or active links.
11. The application meets the accessibility, responsive, performance, and security requirements above.
12. All linting, Django checks, unit, integration, browser, and deployment verification passes.

## 25. Implementation Boundaries

Implementation must preserve the approved product decisions:

- Use Python, Django, and Django templates.
- Do not add Django REST Framework.
- Do not add React, Vue, Angular, or another frontend framework.
- Keep interactivity in small vanilla JavaScript enhancements.
- Keep the pricing engine deterministic, transparent, and independent from views and templates.
- Do not replace the approved Editorial Ledger direction with a generic component-library aesthetic.
- Do not expand scope into payments, contracts, email delivery, AI estimation, or team workspaces without a new design review.
