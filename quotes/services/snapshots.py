from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from quotes.models import Quote, QuoteLineItem


def _decimal_string(value: Any) -> str | None:
    if value is None:
        return None
    return format(Decimal(value), "f")


def _date_string(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _line_item_snapshot(item: QuoteLineItem) -> dict[str, Any]:
    return {
        "label": item.label,
        "description": item.description,
        "amount": _decimal_string(item.amount),
        "sort_order": item.sort_order,
    }


def build_quote_snapshot(quote: Quote) -> dict[str, Any]:
    if not isinstance(quote, Quote):
        raise TypeError("quote must be a Quote instance")

    line_items = (
        [_line_item_snapshot(item) for item in quote.line_items.all()]
        if quote.pk is not None
        else []
    )
    deposit_amount = quote.derive_deposit_amount()
    realized_hourly_equivalent = quote.realized_hourly_equivalent

    flat_snapshot: dict[str, Any] = {
        "snapshot_schema_version": 1,
        "quote_number": quote.quote_number,
        "status": quote.status,
        "project_title": quote.project_title,
        "project_description": quote.project_description,
        "category": quote.category,
        "project_category": quote.category,
        "delivery_days": quote.delivery_days,
        "complexity": quote.complexity,
        "revision_rounds": quote.revision_rounds,
        "urgency": quote.urgency,
        "experience": quote.experience,
        "currency": quote.currency,
        "pricing_config_version": quote.pricing_config_version,
        "pricing_version": quote.pricing_config_version,
        "effective_hours": _decimal_string(quote.effective_hours),
        "estimated_hours": _decimal_string(quote.effective_hours),
        "minimum": _decimal_string(quote.minimum),
        "recommended_minimum": _decimal_string(quote.minimum),
        "recommended": _decimal_string(quote.recommended),
        "maximum": _decimal_string(quote.maximum),
        "recommended_maximum": _decimal_string(quote.maximum),
        "hourly_equivalent": _decimal_string(quote.hourly_equivalent),
        "chosen_total": _decimal_string(quote.chosen_total),
        "realized_hourly_equivalent": _decimal_string(realized_hourly_equivalent),
        "deposit_percentage": quote.deposit_percentage,
        "deposit_amount": _decimal_string(deposit_amount),
        "client_name": quote.client_name,
        "client_company": quote.client_company,
        "client_email": quote.client_email,
        "sender_name": quote.sender_name,
        "sender_business_name": quote.sender_business_name,
        "sender_business": quote.sender_business_name,
        "sender_email": quote.sender_email,
        "sender_website": quote.sender_website,
        "sender_monogram": quote.sender_monogram,
        "accent_color": quote.accent_color,
        "brand_color": quote.accent_color,
        "logo_object_key": quote.logo_object_key,
        "version_logo_object_key": quote.logo_object_key,
        "logo_content_type": quote.logo_content_type,
        "logo_byte_size": quote.logo_byte_size,
        "issue_date": _date_string(quote.issue_date),
        "valid_until": _date_string(quote.valid_until),
        "timeline": quote.timeline,
        "revision_terms": quote.revision_terms,
        "terms": quote.terms,
        "notes": quote.notes,
        "exclusions": quote.exclusions,
        "taxes": quote.taxes,
        "cancellation_terms": quote.cancellation_terms,
        "line_items": line_items,
    }

    snapshot = {
        **flat_snapshot,
        "project": {
            "title": quote.project_title,
            "description": quote.project_description,
            "category": quote.category,
            "project_category": quote.category,
        },
        "client": {
            "name": quote.client_name,
            "company": quote.client_company,
            "email": quote.client_email,
        },
        "sender": {
            "name": quote.sender_name,
            "business_name": quote.sender_business_name,
            "business": quote.sender_business_name,
            "email": quote.sender_email,
            "website": quote.sender_website,
            "monogram": quote.sender_monogram,
        },
        "scope": {
            "delivery_days": quote.delivery_days,
            "complexity": quote.complexity,
            "revision_rounds": quote.revision_rounds,
            "urgency": quote.urgency,
            "experience": quote.experience,
            "currency": quote.currency,
            "effective_hours": _decimal_string(quote.effective_hours),
            "estimated_hours": _decimal_string(quote.effective_hours),
            "minimum": _decimal_string(quote.minimum),
            "recommended_minimum": _decimal_string(quote.minimum),
            "recommended": _decimal_string(quote.recommended),
            "maximum": _decimal_string(quote.maximum),
            "recommended_maximum": _decimal_string(quote.maximum),
            "hourly_equivalent": _decimal_string(quote.hourly_equivalent),
            "pricing_config_version": quote.pricing_config_version,
            "pricing_version": quote.pricing_config_version,
        },
        "dates": {
            "issue_date": _date_string(quote.issue_date),
            "valid_until": _date_string(quote.valid_until),
        },
        "terms_details": {
            "timeline": quote.timeline,
            "revision_terms": quote.revision_terms,
            "notes": quote.notes,
            "exclusions": quote.exclusions,
            "taxes": quote.taxes,
            "cancellation_terms": quote.cancellation_terms,
            "terms": quote.terms,
        },
        "brand": {
            "accent_color": quote.accent_color,
            "brand_color": quote.accent_color,
            "logo_object_key": quote.logo_object_key,
            "version_logo_object_key": quote.logo_object_key,
            "logo_content_type": quote.logo_content_type,
            "logo_byte_size": quote.logo_byte_size,
        },
        "pricing": {
            "config_version": quote.pricing_config_version,
            "version": quote.pricing_config_version,
            "minimum": _decimal_string(quote.minimum),
            "recommended": _decimal_string(quote.recommended),
            "maximum": _decimal_string(quote.maximum),
            "hourly_equivalent": _decimal_string(quote.hourly_equivalent),
        },
    }
    return copy.deepcopy(snapshot)
