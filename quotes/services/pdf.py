from __future__ import annotations

import re
import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from weasyprint import HTML

from core.middleware import new_csp_nonce
from quotes.models import Quote, QuoteVersion
from quotes.services.brand import (
    authorized_logo_data_uri,
    readable_foreground,
)
from quotes.services.snapshots import build_quote_snapshot

MAX_PDF_INPUT_BYTES = 2 * 1024 * 1024
MAX_PDF_OUTPUT_BYTES = 10 * 1024 * 1024
DEFAULT_PDF_RENDER_TIMEOUT_SECONDS = 15


class PdfRenderError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _section(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    value = snapshot.get(name)
    return value if isinstance(value, dict) else {}


def _line_items(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    raw_items = snapshot.get("line_items")
    if not isinstance(raw_items, list):
        return []
    return [
        {
            "label": _text(item.get("label")),
            "description": _text(item.get("description")),
            "amount": _text(item.get("amount")),
        }
        for item in raw_items
        if isinstance(item, dict)
    ]


def _snapshot_context(
    snapshot: dict[str, Any],
    *,
    is_draft: bool,
    owner_id=None,
) -> dict[str, Any]:
    terms_details = _section(snapshot, "terms_details")
    line_items = _line_items(snapshot)
    accent_color = _text(snapshot.get("accent_color")) or "#E85D3F"
    try:
        accent_foreground = readable_foreground(accent_color)
    except (TypeError, ValueError):
        accent_color = "#E85D3F"
        accent_foreground = readable_foreground(accent_color)
    logo_data_uri = ""
    if snapshot.get("logo_object_key") and owner_id is not None:
        try:
            logo_data_uri = (
                authorized_logo_data_uri(
                    storage_key=str(snapshot["logo_object_key"]),
                    owner_id=owner_id,
                )
                or ""
            )
        except (OSError, ValueError, ValidationError):
            logo_data_uri = ""
    return {
        "document_label": "DRAFT" if is_draft else "QUOTATION",
        "is_draft": is_draft,
        "quote_number": _text(snapshot.get("quote_number")),
        "project_title": _text(snapshot.get("project_title")),
        "project_description": _text(snapshot.get("project_description")),
        "category": _text(snapshot.get("category")),
        "client_name": _text(snapshot.get("client_name")),
        "client_company": _text(snapshot.get("client_company")),
        "client_email": _text(snapshot.get("client_email")),
        "sender_name": _text(snapshot.get("sender_name")),
        "sender_business_name": _text(snapshot.get("sender_business_name")),
        "sender_email": _text(snapshot.get("sender_email")),
        "sender_website": _text(snapshot.get("sender_website")),
        "sender_monogram": _text(snapshot.get("sender_monogram")),
        "accent_color": accent_color,
        "accent_foreground": accent_foreground,
        "logo_data_uri": logo_data_uri,
        "currency": _text(snapshot.get("currency")),
        "delivery_days": snapshot.get("delivery_days"),
        "complexity": _text(snapshot.get("complexity")),
        "revision_rounds": snapshot.get("revision_rounds"),
        "urgency": _text(snapshot.get("urgency")),
        "experience": _text(snapshot.get("experience")),
        "effective_hours": _text(snapshot.get("effective_hours")),
        "hourly_equivalent": _text(snapshot.get("hourly_equivalent")),
        "chosen_total": _text(snapshot.get("chosen_total")),
        "deposit_percentage": snapshot.get("deposit_percentage"),
        "deposit_amount": _text(snapshot.get("deposit_amount")),
        "issue_date": _text(snapshot.get("issue_date")),
        "valid_until": _text(snapshot.get("valid_until")),
        "terms": _text(snapshot.get("terms")),
        "timeline": _text(terms_details.get("timeline")),
        "revision_terms": _text(terms_details.get("revision_terms")),
        "notes": _text(terms_details.get("notes")),
        "exclusions": _text(terms_details.get("exclusions")),
        "taxes": _text(terms_details.get("taxes")),
        "cancellation_terms": _text(terms_details.get("cancellation_terms")),
        "line_items": line_items,
    }


def _guest_context(quote_data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(quote_data, dict):
        raise TypeError("quote_data must be a dictionary")
    inputs = quote_data.get("inputs")
    result = quote_data.get("result")
    scope = quote_data.get("scope")
    if not all(isinstance(value, dict) for value in (inputs, result, scope)):
        raise ValueError("Guest quotation data is invalid")
    total = result.get("recommended")
    if total is None:
        raise ValueError("Guest quotation total is required")
    delivery_days = inputs.get("delivery_days")
    revision_rounds = inputs.get("revision_rounds", 0)
    created_at = _text(quote_data.get("created_at"))
    expires_at = _text(quote_data.get("expires_at"))
    return {
        "document_label": "DRAFT",
        "is_draft": True,
        "quote_number": "",
        "project_title": "Project quotation",
        "project_description": _text(inputs.get("description")),
        "category": _text(scope.get("category")),
        "client_name": "Guest",
        "client_company": "",
        "client_email": "",
        "sender_name": "Scopequote",
        "sender_business_name": "",
        "sender_email": "",
        "sender_website": "",
        "sender_monogram": "SQ",
        "accent_color": "#E85D3F",
        "accent_foreground": readable_foreground("#E85D3F"),
        "logo_data_uri": "",
        "currency": _text(inputs.get("currency")),
        "delivery_days": delivery_days,
        "complexity": _text(inputs.get("complexity")),
        "revision_rounds": revision_rounds,
        "urgency": _text(inputs.get("urgency")),
        "experience": _text(inputs.get("experience")),
        "effective_hours": _text(result.get("effective_hours")),
        "hourly_equivalent": _text(result.get("hourly_equivalent")),
        "chosen_total": _text(total),
        "deposit_percentage": None,
        "deposit_amount": "",
        "issue_date": created_at[:10],
        "valid_until": expires_at[:10],
        "terms": "No terms have been added to this guest estimate.",
        "timeline": (
            f"{delivery_days} focused delivery days."
            if delivery_days is not None
            else ""
        ),
        "revision_terms": (
            f"{revision_rounds} revision rounds included."
            if revision_rounds is not None
            else ""
        ),
        "notes": "",
        "exclusions": "",
        "taxes": "",
        "cancellation_terms": "",
        "line_items": [
            {
                "label": "Project fee",
                "description": "Server-calculated recommended project fee.",
                "amount": _text(total),
            }
        ],
    }


def _published_context(version: QuoteVersion) -> dict[str, Any]:
    if not isinstance(version, QuoteVersion):
        raise TypeError("version must be a QuoteVersion")
    snapshot = version.snapshot
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_schema_version") != 1:
        raise ValueError("Published quotation snapshot is invalid")
    return _snapshot_context(
        snapshot,
        is_draft=False,
        owner_id=version.quote.owner_id,
    )


def _draft_context(quote: Quote) -> dict[str, Any]:
    if not isinstance(quote, Quote) or quote.pk is None:
        raise TypeError("quote must be a persisted Quote")
    if quote.status != Quote.Status.DRAFT:
        raise ValueError("Only current drafts may use the draft PDF context")
    return _snapshot_context(
        build_quote_snapshot(quote),
        is_draft=True,
        owner_id=quote.owner_id,
    )


def _trusted_static_base_url() -> str:
    static_directories = getattr(settings, "STATICFILES_DIRS", ())
    if static_directories:
        static_directory = Path(static_directories[0]).resolve()
    else:
        static_directory = Path(settings.STATIC_ROOT).resolve()
    return f"{static_directory.as_uri()}/"


@contextmanager
def _render_timeout(seconds: int):
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def handle_timeout(_signum, _frame):
        raise TimeoutError("PDF rendering exceeded its time limit.")

    previous = signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _render_pdf(context: dict[str, Any]) -> bytes:
    try:
        render_context = dict(context)
        render_context["csp_nonce"] = new_csp_nonce()
        html = render_to_string("quotes/print.html", render_context)
        if len(html.encode("utf-8")) > MAX_PDF_INPUT_BYTES:
            raise PdfRenderError("The quotation PDF input is too large.")
        timeout = int(
            getattr(
                settings,
                "PDF_RENDER_TIMEOUT_SECONDS",
                DEFAULT_PDF_RENDER_TIMEOUT_SECONDS,
            )
        )
        with _render_timeout(timeout):
            pdf = HTML(
                string=html,
                base_url=_trusted_static_base_url(),
            ).write_pdf()
    except PdfRenderError:
        raise
    except Exception as exc:
        raise PdfRenderError("The quotation PDF could not be generated.") from exc
    if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
        raise PdfRenderError("The quotation renderer returned an invalid document.")
    if len(pdf) > MAX_PDF_OUTPUT_BYTES:
        raise PdfRenderError("The quotation PDF output is too large.")
    return pdf


def pdf_filename(quote_data: dict[str, Any]) -> str:
    if "quote_number" not in quote_data:
        return "scopequote-guest-draft.pdf"
    quote_number = _text(quote_data.get("quote_number")).strip()
    if not quote_number:
        return "scopequote-draft.pdf"
    safe_number = re.sub(r"[^A-Za-z0-9._-]+", "-", quote_number)
    safe_number = safe_number.strip("._-")[:80]
    if not safe_number:
        return "scopequote-quotation.pdf"
    return f"scopequote-{safe_number}.pdf"


def render_guest_quote_pdf(*, quote_data: dict) -> bytes:
    return _render_pdf(_guest_context(quote_data))


def render_published_quote_pdf(*, version: QuoteVersion) -> bytes:
    return _render_pdf(_published_context(version))


def render_draft_quote_pdf(*, quote: Quote) -> bytes:
    return _render_pdf(_draft_context(quote))
