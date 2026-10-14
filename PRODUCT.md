# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Independent freelancers and small creative studios who need to price unfamiliar client projects, explain their reasoning, and send professional quotations without relying on guesswork.

## Product Purpose

Scopequote turns project scope, delivery duration, complexity, revisions, urgency, and experience into an explainable recommended price range. Freelancers can turn that recommendation into a branded quotation, export it as a PDF, share a private client link, and see whether the client accepted or requested changes.

## Positioning

A guest-first pricing workspace that makes an initially uncertain commercial decision legible, editable, and client-ready. Its differentiator is a transparent, deterministic explanation of the estimate rather than an opaque score.

## Operating Context

Freelancers work from project descriptions, delivery schedules, revision expectations, client details, payment terms, and brand assets. They may work from a desktop while editing a proposal or from a phone while sharing and checking a client response. The application is web-based, server-rendered, and usable without client-side JavaScript for core flows.

## Capabilities and Constraints

- Email/password accounts are optional for estimating and PDF export; authenticated users gain durable quote history and private links.
- Supported currencies are USD, EUR, and GBP through code-owned localized reference rates, not live foreign exchange.
- Pricing is deterministic and explainable, with no AI-generated estimate in the first release.
- Published quote versions are immutable; revisions create new drafts.
- Client links are private, expiring, revocable, and record accept/request-changes responses.
- The first release uses Django templates, standard Django forms/views, PostgreSQL, vanilla JavaScript enhancements, and no REST API or frontend framework.
- Payments, contracts, invoicing, project management, automated client email, team workspaces, and live currency conversion are outside the first release.
- The product targets WCAG 2.2 AA and requires keyboard, screen-reader, responsive, and no-JavaScript-compatible core flows.

## Brand Commitments

The product name is Scopequote. The voice is calm, precise, practical, and respectful of a freelancer's commercial confidence. The approved visual direction is Editorial Ledger; detailed visual decisions belong to the design system and product surfaces rather than this product record.

## Evidence on Hand

The approved design specification and implementation plan define the product behavior, pricing rules, routes, data model, accessibility requirements, and launch gates. No customer testimonials, performance claims, partner logos, or market benchmarks are available and must not be fabricated.

## Product Principles

- Make the number explainable before asking the user to trust it.
- Keep the first useful estimate available before account creation.
- Preserve freelancer control over the final commercial offer.
- Treat client-facing documents as immutable, versioned records.
- Prefer a calm server-rendered path over a fragile client-side application.

## Accessibility & Inclusion

Core estimator, account, quotation, and client-response flows must work with keyboard navigation, visible labels, semantic landmarks, clear error recovery, responsive layouts down to 320 CSS pixels, and JavaScript disabled where the flow can be completed through standard form submission.
