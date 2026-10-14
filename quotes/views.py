from __future__ import annotations

import copy
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from accounts.models import Profile
from accounts.services import (
    RateLimitExceeded,
    client_ip,
    enforce_rate_limit,
    record_authenticated_activity,
)
from pricing.classifier import classify_description
from pricing.constants import (
    ADDITIONAL_REVISION_HOURS,
    COMPLEXITY_MULTIPLIERS,
    COMPLEXITY_RISK_BUFFERS,
    EXPERIENCE_RATES,
    FEE_QUANTUM,
    HOURLY_EQUIVALENT_QUANTUM,
    HOURS_PER_DELIVERY_DAY,
    INCLUDED_REVISION_ROUNDS,
    MAXIMUM_MULTIPLIER,
    MINIMUM_MULTIPLIER,
    PRICING_CONFIG_VERSION,
    URGENCY_MULTIPLIERS,
)
from pricing.engine import calculate_estimate
from pricing.forms import EstimateForm
from pricing.types import EstimateInputs
from quotes.forms import (
    ClientResponseForm,
    QuoteEditorForm,
    QuoteLineItemFormSet,
    QuotePublishForm,
)
from quotes.models import (
    _DRAFT_LINE_ITEM_MUTATION_GUARD,
    Quote,
    QuoteEvent,
    QuoteLineItem,
    QuoteVersion,
    ShareLink,
    draft_line_item_mutation,
)
from quotes.services.brand import readable_foreground
from quotes.services.guest import (
    CONVERSION_NONCE_KEY,
    GuestStateError,
    create_guest_quote,
    get_guest_estimate,
    get_guest_quote,
    save_guest_estimate,
)
from quotes.services.links import (
    create_share_link,
    decrypt_link_token,
    is_link_active,
    link_state,
    normalize_datetime,
    reissue_share_link,
    resolve_share_link,
    revoke_share_link,
)
from quotes.services.pdf import (
    PdfRenderError,
    pdf_filename,
    render_draft_quote_pdf,
    render_guest_quote_pdf,
    render_published_quote_pdf,
)
from quotes.services.quotes import (
    duplicate_quote,
)
from quotes.services.quotes import (
    publish_quote as publish_quote_service,
)
from quotes.services.responses import record_response
from quotes.services.storage import delete_object, stage_logo
from quotes.services.tokens import hash_token

PUBLIC_RESPONSE_RATE_LIMIT = 20
PUBLIC_RESPONSE_RATE_WINDOW = 60


def _public_response_rate_key(request: HttpRequest, token: str) -> str:
    try:
        token_hash = hash_token(token)
    except (AttributeError, TypeError, UnicodeError, ValueError):
        token_hash = "invalid"
    return f"quotes.public-response:{client_ip(request)}:{token_hash}"


def _rate_limited_public_response(request: HttpRequest, error: RateLimitExceeded):
    response = _with_public_headers(
        render(
            request,
            "quotes/public_interstitial.html",
            {
                "unavailable": False,
                "rate_limited": True,
                "unavailable_message": ("Too many requests. Please try again later."),
            },
            status=429,
        )
    )
    response["Retry-After"] = str(error.retry_after)
    return response


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _build_estimate_payload(inputs: dict) -> dict:
    estimate = calculate_estimate(EstimateInputs(**inputs))
    scope = classify_description(inputs["description"])
    return {
        "inputs": {
            "description": inputs["description"],
            "delivery_days": inputs["delivery_days"],
            "complexity": inputs["complexity"],
            "revision_rounds": inputs["revision_rounds"],
            "urgency": inputs["urgency"],
            "experience": inputs["experience"],
            "currency": inputs["currency"],
        },
        "result": {
            "effective_hours": _decimal_text(estimate.effective_hours),
            "minimum": _decimal_text(estimate.minimum),
            "recommended": _decimal_text(estimate.recommended),
            "maximum": _decimal_text(estimate.maximum),
            "hourly_equivalent": _decimal_text(estimate.hourly_equivalent),
            "pricing_config_version": estimate.pricing_config_version,
            "factors": [dict(factor) for factor in estimate.factors],
        },
        "scope": {
            "category": scope.category,
            "matched_phrases": list(scope.matched_phrases),
            "confidence": scope.confidence,
            "suggested_items": list(scope.suggested_items),
        },
    }


def _pricing_configuration() -> dict:
    return {
        "version": PRICING_CONFIG_VERSION,
        "hours_per_delivery_day": _decimal_text(HOURS_PER_DELIVERY_DAY),
        "included_revision_rounds": _decimal_text(INCLUDED_REVISION_ROUNDS),
        "additional_revision_hours": _decimal_text(ADDITIONAL_REVISION_HOURS),
        "minimum_multiplier": _decimal_text(MINIMUM_MULTIPLIER),
        "maximum_multiplier": _decimal_text(MAXIMUM_MULTIPLIER),
        "fee_quantum": _decimal_text(FEE_QUANTUM),
        "hourly_equivalent_quantum": _decimal_text(HOURLY_EQUIVALENT_QUANTUM),
        "complexity_factors": tuple(
            {
                "key": key,
                "effort": _decimal_text(COMPLEXITY_MULTIPLIERS[key]),
                "risk": _decimal_text(COMPLEXITY_RISK_BUFFERS[key]),
            }
            for key in COMPLEXITY_MULTIPLIERS
        ),
        "urgency_factors": tuple(
            {"key": key, "value": _decimal_text(value)}
            for key, value in URGENCY_MULTIPLIERS.items()
        ),
        "rate_factors": tuple(
            {
                "experience": experience,
                "currency": currency,
                "value": _decimal_text(rate),
            }
            for experience, rates in EXPERIENCE_RATES.items()
            for currency, rate in rates.items()
        ),
    }


def _estimator_context(request, form, result=None, *, status_message=None):
    guest_quote = get_guest_quote(request) if result is not None else None
    return {
        "form": form,
        "result": result,
        "guest_quote": guest_quote,
        "conversion_nonce": request.session.get(CONVERSION_NONCE_KEY),
        "duration_presets": EstimateForm.DURATION_PRESETS,
        "pricing_config": _pricing_configuration(),
        "status_message": status_message,
    }


@require_http_methods(["GET", "POST"])
def estimator(request: HttpRequest) -> HttpResponse:
    result = None
    if request.method == "POST":
        form = EstimateForm(request.POST)
        if form.is_valid():
            result = save_guest_estimate(
                request,
                _build_estimate_payload(form.cleaned_input_values()),
            )
    else:
        result = get_guest_estimate(request)
        initial = result["inputs"] if result is not None else None
        form = EstimateForm(initial=initial)

    status_message = None
    if request.method == "GET" and request.GET.get("guest_quote") == "created":
        status_message = "Guest quotation draft created."
    return render(
        request,
        "pricing/estimator.html",
        _estimator_context(request, form, result, status_message=status_message),
    )


@require_POST
def guest_quote(request: HttpRequest) -> HttpResponse:
    try:
        create_guest_quote(request, request.POST)
    except GuestStateError:
        result = get_guest_estimate(request)
        form = EstimateForm(initial=result["inputs"] if result is not None else None)
        context = _estimator_context(
            request,
            form,
            result,
            status_message=(
                "This estimate changed in another tab. Review the current result and "
                "submit again."
            ),
        )
        return render(
            request,
            "pricing/estimator.html",
            context,
            status=409,
        )
    return redirect(f"{reverse('quotes:estimator')}?guest_quote=created")


def _with_pdf_headers(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _pdf_rate_limited(request: HttpRequest, error: RateLimitExceeded) -> HttpResponse:
    response = _with_pdf_headers(
        render(
            request,
            "quotes/public_interstitial.html",
            {
                "unavailable": True,
                "unavailable_message": "Too many requests. Please try again later.",
            },
            status=429,
        )
    )
    response["Retry-After"] = str(error.retry_after)
    return response


def _enforce_pdf_rate_limit(request: HttpRequest, *, route: str) -> HttpResponse | None:
    identifier = (
        str(request.user.pk)
        if getattr(request.user, "is_authenticated", False)
        else client_ip(request)
    )
    try:
        enforce_rate_limit(
            key=f"quotes.{route}:{identifier}",
            limit=int(getattr(settings, "PDF_RATE_LIMIT", 20)),
            window_seconds=int(getattr(settings, "PDF_RATE_WINDOW", 60)),
        )
    except RateLimitExceeded as error:
        return _pdf_rate_limited(request, error)
    return None


def _pdf_download(pdf: bytes, *, filename: str) -> HttpResponse:
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return _with_pdf_headers(response)


def _pdf_unavailable(request: HttpRequest, message: str) -> HttpResponse:
    return _with_pdf_headers(
        render(
            request,
            "quotes/public_interstitial.html",
            {"unavailable": True, "unavailable_message": message},
            status=404,
        )
    )


def _pdf_render_error(request: HttpRequest) -> HttpResponse:
    return _with_pdf_headers(
        render(
            request,
            "quotes/public_interstitial.html",
            {
                "unavailable": True,
                "unavailable_message": (
                    "The PDF could not be generated. Please try again."
                ),
            },
            status=503,
        )
    )


@require_http_methods(["GET"])
def guest_quote_pdf(request: HttpRequest) -> HttpResponse:
    limited = _enforce_pdf_rate_limit(request, route="guest-pdf")
    if limited is not None:
        return limited
    quote_data = get_guest_quote(request)
    if quote_data is None:
        return _pdf_unavailable(
            request,
            "Create a guest quotation and try again.",
        )
    try:
        pdf = render_guest_quote_pdf(quote_data=quote_data)
    except PdfRenderError:
        return _pdf_render_error(request)
    return _pdf_download(pdf, filename=pdf_filename(quote_data))


def _profile_for(user):
    return Profile.objects.filter(user=user).first() or Profile(user=user)


def _is_owner_staged_key(key, owner_id):
    return isinstance(key, str) and key.startswith(f"quotes/logos/staging/{owner_id}/")


def _set_staged_logo(quote, stored_logo):
    quote.logo_object_key = stored_logo.storage_key
    quote.logo_content_type = stored_logo.content_type
    quote.logo_byte_size = stored_logo.byte_size


def _cleanup_staged_logo(stored_logo):
    if stored_logo is not None:
        delete_object(stored_logo.storage_key)


def _add_domain_form_error(form, error):
    if hasattr(error, "message_dict"):
        for messages in error.message_dict.values():
            for message in messages:
                form.add_error(None, message)
    else:
        form.add_error(None, str(error))


def _editor_initial(profile=None):
    profile = profile or Profile()
    return {
        "delivery_days": 5,
        "complexity": "standard",
        "revision_rounds": 0,
        "urgency": "standard",
        "experience": "intermediate",
        "currency": profile.default_currency,
        "deposit_percentage": profile.default_deposit_percentage,
        "accent_color": profile.default_accent_color,
        "sender_name": profile.full_name,
        "sender_business_name": profile.business_name,
        "sender_email": getattr(profile.user, "email", ""),
        "sender_website": profile.website,
    }


def _editor_initial_for_request(request, profile):
    initial = _editor_initial(profile)
    guest_quote = get_guest_quote(request)
    estimate = guest_quote or get_guest_estimate(request)
    if estimate is not None:
        inputs = estimate.get("inputs", {})
        initial.update(
            {
                "project_description": inputs.get("description", ""),
                "delivery_days": inputs.get("delivery_days", initial["delivery_days"]),
                "complexity": inputs.get("complexity", initial["complexity"]),
                "revision_rounds": inputs.get(
                    "revision_rounds", initial["revision_rounds"]
                ),
                "urgency": inputs.get("urgency", initial["urgency"]),
                "experience": inputs.get("experience", initial["experience"]),
                "currency": inputs.get("currency", initial["currency"]),
            }
        )
        if guest_quote is not None:
            initial["category"] = guest_quote.get("scope", {}).get("category", "")
    return initial


def _estimate_for_form(form: QuoteEditorForm):
    if not form.is_valid():
        return None
    try:
        inputs = form.estimate_inputs()
        if len(inputs.description.strip()) < 30:
            return None
        return calculate_estimate(inputs)
    except (TypeError, ValueError, ValidationError):
        return None


def _apply_estimate(quote: Quote, form: QuoteEditorForm):
    estimate = _estimate_for_form(form)
    if estimate is None:
        quote.effective_hours = Decimal("0.00")
        quote.minimum = Decimal("0.00")
        quote.recommended = Decimal("0.00")
        quote.maximum = Decimal("0.00")
        quote.hourly_equivalent = Decimal("0.00")
        return None
    quote.effective_hours = estimate.effective_hours
    quote.minimum = estimate.minimum
    quote.recommended = estimate.recommended
    quote.maximum = estimate.maximum
    quote.hourly_equivalent = estimate.hourly_equivalent
    quote.pricing_config_version = estimate.pricing_config_version
    return estimate


def _line_formset_for_quote(quote):
    items = list(quote.line_items.all())
    return QuoteLineItemFormSet(
        initial=[
            {
                "label": item.label,
                "description": item.description,
                "amount": item.amount,
                "sort_order": item.sort_order,
            }
            for item in items
        ],
        initial_form_count=len(items),
        prefix="line_items",
    )


def _publish_form_for_quote(quote):
    return QuotePublishForm(
        data={
            "project_title": quote.project_title,
            "project_description": quote.project_description,
            "client_name": quote.client_name,
            "client_email": quote.client_email,
            "sender_name": quote.sender_name,
            "sender_email": quote.sender_email,
            "issue_date": quote.issue_date,
            "valid_until": quote.valid_until,
            "terms": quote.terms,
            "updated_at": quote.updated_at,
        },
        quote=quote,
    )


def _line_formset(request, quote=None):
    if quote is not None and quote.pk and request.method != "POST":
        return _line_formset_for_quote(quote)
    initial = None
    initial_form_count = 0
    if quote is not None and quote.pk:
        items = list(quote.line_items.all())
        initial_form_count = len(items)
        initial = [
            {
                "label": item.label,
                "description": item.description,
                "amount": item.amount,
                "sort_order": item.sort_order,
            }
            for item in items
        ]
    return QuoteLineItemFormSet(
        request.POST if request.method == "POST" else None,
        initial=initial,
        initial_form_count=initial_form_count,
        prefix="line_items",
    )


def _save_line_items(quote: Quote, formset, *, estimate=None):
    if formset is None:
        raise ValidationError("At least one line item is required.")
    quote.line_items.all().delete()
    for index, form in enumerate(formset.forms):
        if not form.cleaned_data or form.cleaned_data.get("DELETE"):
            continue
        if not form.cleaned_data.get("label"):
            continue
        QuoteLineItem.objects.create(
            quote=quote,
            label=form.cleaned_data["label"],
            description=form.cleaned_data.get("description", ""),
            amount=form.cleaned_data["amount"],
            sort_order=form.cleaned_data.get("sort_order") or index,
        )
    quote.recalculate_line_item_total()
    quote.save()


def _updated_at_matches(quote: Quote, submitted) -> bool:
    if not submitted:
        return True
    if isinstance(submitted, datetime):
        current = submitted
    else:
        try:
            current = datetime.fromisoformat(str(submitted))
        except ValueError:
            return False
    if timezone.is_naive(current):
        current = timezone.make_aware(current, timezone=timezone.get_current_timezone())
    return quote.updated_at == current


def _quote_snapshot_context(quote: Quote) -> dict:
    if quote.current_published_version_id:
        snapshot = quote.current_published_version.snapshot
        return {
            "snapshot": snapshot,
            "line_items": snapshot.get("line_items", []),
            "chosen_total": snapshot.get("chosen_total", quote.chosen_total),
            "realized_hourly_equivalent": snapshot.get(
                "realized_hourly_equivalent",
                quote.realized_hourly_equivalent,
            ),
            "hourly_equivalent": snapshot.get(
                "hourly_equivalent", quote.hourly_equivalent
            ),
        }
    return {
        "snapshot": None,
        "line_items": quote.line_items.all(),
        "chosen_total": quote.chosen_total,
        "realized_hourly_equivalent": quote.realized_hourly_equivalent,
        "hourly_equivalent": quote.hourly_equivalent,
    }


def _suggested_deliverables(form: QuoteEditorForm, quote: Quote | None):
    if form.is_bound:
        description = form.data.get("project_description", "")
    else:
        description = quote.project_description if quote is not None else ""
    if not isinstance(description, str) or not description.strip():
        description = quote.project_description if quote is not None else ""
    if not isinstance(description, str):
        return []
    return list(classify_description(description).suggested_items)


def _editor_context(request, form, formset, quote=None, *, status=200, conflict=False):
    estimate = _estimate_for_form(form)
    return {
        "form": form,
        "line_item_formset": formset,
        "quote": quote,
        "estimate": estimate,
        "recommended_hourly": getattr(estimate, "hourly_equivalent", None),
        "realized_hourly": quote.realized_hourly_equivalent if quote else None,
        "suggested_deliverables": _suggested_deliverables(form, quote),
        "conflict": conflict,
        "status_label": "Draft"
        if quote is None or quote.is_draft
        else quote.get_status_display(),
    }, status


@login_required
@require_http_methods(["GET", "POST"])
def new_quote(request: HttpRequest) -> HttpResponse:
    profile = _profile_for(request.user)
    form = QuoteEditorForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        user=request.user,
        profile=profile,
        initial=_editor_initial_for_request(request, profile),
    )
    formset = _line_formset(request)
    render_status = 200
    if (
        request.method == "POST"
        and form.is_valid()
        and (formset is None or formset.is_valid())
    ):
        stored_logo = None
        try:
            upload = form.cleaned_data.get("logo")
            if upload:
                stored_logo = stage_logo(upload, owner_id=request.user.pk)
            with transaction.atomic():
                quote = Quote(owner=request.user)
                form.apply_to_quote(quote)
                if stored_logo is not None:
                    _set_staged_logo(quote, stored_logo)
                estimate = _apply_estimate(quote, form)
                with draft_line_item_mutation(_DRAFT_LINE_ITEM_MUTATION_GUARD):
                    quote.save()
                    _save_line_items(quote, formset, estimate=estimate)
        except ValidationError as error:
            _cleanup_staged_logo(stored_logo)
            _add_domain_form_error(form, error)
            render_status = 200
        except Exception:
            _cleanup_staged_logo(stored_logo)
            form.add_error(None, "The quote could not be saved. Please try again.")
            render_status = 503
        else:
            record_authenticated_activity(request.user)
            return redirect("quotes:detail", pk=quote.pk)

    context, status = _editor_context(request, form, formset)
    return render(
        request,
        "quotes/editor.html",
        context,
        status=render_status or status,
    )


@login_required
@require_http_methods(["GET"])
def quote_detail(request: HttpRequest, pk) -> HttpResponse:
    quote = get_object_or_404(Quote, pk=pk, owner=request.user)
    context = {
        "quote": quote,
        "version": quote.current_published_version,
        **_quote_snapshot_context(quote),
        "can_edit": quote.can_edit,
        "links": quote.current_published_version.share_links.all()
        if quote.current_published_version_id
        else ShareLink.objects.none(),
        "response_events": quote.events.filter(
            event_type__in=(
                QuoteEvent.EventType.ACCEPTED,
                QuoteEvent.EventType.CHANGES_REQUESTED,
            )
        ),
    }
    return render(request, "quotes/detail.html", context)


@login_required
@require_http_methods(["GET"])
def quote_pdf(request: HttpRequest, pk) -> HttpResponse:
    limited = _enforce_pdf_rate_limit(request, route="owner-pdf")
    if limited is not None:
        return limited
    quote = get_object_or_404(
        Quote.objects.select_related("current_published_version"),
        pk=pk,
        owner=request.user,
    )
    if quote.status == Quote.Status.DRAFT:
        try:
            pdf = render_draft_quote_pdf(quote=quote)
        except PdfRenderError:
            return _pdf_render_error(request)
        return _pdf_download(pdf, filename="scopequote-draft.pdf")
    version = quote.current_published_version
    if version is None:
        return _pdf_unavailable(
            request,
            "This quotation PDF is unavailable.",
        )
    try:
        pdf = render_published_quote_pdf(version=version)
    except PdfRenderError:
        return _pdf_render_error(request)
    return _pdf_download(pdf, filename=pdf_filename(version.snapshot))


def _version_logo_response(version: QuoteVersion) -> FileResponse:
    key = version.logo_object_key
    owner_prefix = f"quotes/versions/{version.quote.owner_id}/"
    if not key or not key.startswith(owner_prefix):
        raise Http404("Logo unavailable.")
    try:
        source = default_storage.open(key, "rb")
    except OSError as exc:
        raise Http404("Logo unavailable.") from exc
    content_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(key.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    response = FileResponse(source, content_type=content_type)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_http_methods(["GET"])
def version_logo(request: HttpRequest, version_id) -> FileResponse:
    if not request.user.is_authenticated:
        raise Http404("Logo unavailable.")
    version = get_object_or_404(
        QuoteVersion.objects.select_related("quote"),
        pk=version_id,
        quote__owner=request.user,
    )
    return _version_logo_response(version)


@login_required
@require_http_methods(["GET", "POST"])
def edit_quote(request: HttpRequest, pk) -> HttpResponse:
    quote = get_object_or_404(Quote, pk=pk, owner=request.user)
    if not quote.can_edit:
        return redirect("quotes:duplicate", pk=quote.pk)

    form = QuoteEditorForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        quote=quote,
        user=request.user,
    )
    formset = _line_formset(request, quote)
    render_status = 200
    if request.method == "POST":
        submitted_timestamp = request.POST.get("updated_at")
        if not submitted_timestamp or not _updated_at_matches(
            quote, submitted_timestamp
        ):
            quote.refresh_from_db()
            current_form = QuoteEditorForm(quote=quote, user=request.user)
            current_formset = _line_formset_for_quote(quote)
            context, status = _editor_context(
                request,
                form,
                formset,
                quote,
                status=409,
                conflict=True,
            )
            context.update(
                {
                    "current_form": current_form,
                    "current_formset": current_formset,
                    "current_quote": quote,
                    "conflict_message": (
                        "This quote changed in another tab. Review the current values "
                        "and submit again."
                    ),
                }
            )
            return render(request, "quotes/editor.html", context, status=status)
        if form.is_valid() and (formset is None or formset.is_valid()):
            render_status = 200
            stored_logo = None
            stale = False
            old_logo_key = None
            try:
                upload = form.cleaned_data.get("logo")
                if upload:
                    stored_logo = stage_logo(upload, owner_id=request.user.pk)
                with transaction.atomic():
                    locked = Quote.objects.select_for_update().get(
                        pk=quote.pk, owner=request.user
                    )
                    if not _updated_at_matches(locked, submitted_timestamp):
                        stale = True
                    else:
                        old_logo_key = locked.logo_object_key
                        form.apply_to_quote(locked)
                        if stored_logo is not None:
                            _set_staged_logo(locked, stored_logo)
                        estimate = _apply_estimate(locked, form)
                        locked.save()
                        _save_line_items(locked, formset, estimate=estimate)
                if stale:
                    _cleanup_staged_logo(stored_logo)
                    quote.refresh_from_db()
                    current_form = QuoteEditorForm(quote=quote, user=request.user)
                    current_formset = _line_formset_for_quote(quote)
                    context, status = _editor_context(
                        request,
                        form,
                        formset,
                        quote,
                        status=409,
                        conflict=True,
                    )
                    context.update(
                        {
                            "current_form": current_form,
                            "current_formset": current_formset,
                            "current_quote": quote,
                            "conflict_message": (
                                "This quote changed in another tab. Review the current "
                                "values and submit again."
                            ),
                        }
                    )
                    return render(
                        request,
                        "quotes/editor.html",
                        context,
                        status=status,
                    )
                if (
                    stored_logo is not None
                    and old_logo_key
                    and old_logo_key != stored_logo.storage_key
                    and _is_owner_staged_key(old_logo_key, request.user.pk)
                ):
                    delete_object(old_logo_key)
            except ValidationError as error:
                _cleanup_staged_logo(stored_logo)
                _add_domain_form_error(form, error)
                render_status = 200
            except Exception:
                _cleanup_staged_logo(stored_logo)
                form.add_error(None, "The quote could not be saved. Please try again.")
                render_status = 503
            else:
                record_authenticated_activity(request.user)
                return redirect("quotes:detail", pk=quote.pk)

    context, status = _editor_context(request, form, formset, quote)
    return render(request, "quotes/editor.html", context, status=render_status)


@login_required
@require_http_methods(["GET", "POST"])
def publish_quote(request: HttpRequest, pk) -> HttpResponse:
    quote = get_object_or_404(Quote, pk=pk, owner=request.user)
    if not quote.can_edit:
        return redirect("quotes:detail", pk=quote.pk)
    if request.method == "POST" and not request.user.can_publish:
        raise PermissionDenied("Verify your email before publishing a quote.")
    form = QuotePublishForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        quote=quote,
    )
    if request.method == "POST":
        submitted_timestamp = request.POST.get("updated_at")
        if not submitted_timestamp or not _updated_at_matches(
            quote, submitted_timestamp
        ):
            quote.refresh_from_db()
            current_form = _publish_form_for_quote(quote)
            current_form.is_valid()
            current_form.add_error(
                None, "This quote changed in another tab. Review it before publishing."
            )
            return render(
                request,
                "quotes/publish.html",
                {
                    "quote": quote,
                    "form": form,
                    "current_form": current_form,
                    "conflict": True,
                },
                status=409,
            )
    if request.method == "POST" and form.is_valid():
        staged_logo = None
        stale = False
        old_logo_key = None
        try:
            upload = form.cleaned_data.get("logo")
            if upload:
                staged_logo = stage_logo(upload, owner_id=request.user.pk)
            elif not quote.logo_object_key:
                profile = _profile_for(request.user)
                if profile.logo:
                    staged_logo = stage_logo(
                        profile.logo,
                        owner_id=request.user.pk,
                    )
            with transaction.atomic():
                locked = Quote.objects.select_for_update().get(
                    pk=quote.pk,
                    owner=request.user,
                )
                if not _updated_at_matches(locked, submitted_timestamp):
                    stale = True
                else:
                    old_logo_key = locked.logo_object_key
                    form.apply_to_quote(locked)
                    locked.save()
                    publish_quote_service(
                        quote=locked,
                        user=request.user,
                        staged_logo=staged_logo,
                        intentional_adjustment_confirmed=any(
                            form.cleaned_data.get(name)
                            for name in (
                                "intentional_adjustment_confirmed",
                                "confirm_intentional_adjustment",
                                "confirm_adjustment",
                                "intentional_adjustment",
                            )
                        ),
                    )
            if stale:
                _cleanup_staged_logo(staged_logo)
                quote.refresh_from_db()
                current_form = _publish_form_for_quote(quote)
                current_form.is_valid()
                current_form.add_error(
                    None,
                    "This quote changed in another tab. Review it before publishing.",
                )
                return render(
                    request,
                    "quotes/publish.html",
                    {
                        "quote": quote,
                        "form": form,
                        "current_form": current_form,
                        "conflict": True,
                    },
                    status=409,
                )
            if (
                staged_logo is not None
                and old_logo_key
                and old_logo_key != staged_logo.storage_key
                and _is_owner_staged_key(old_logo_key, request.user.pk)
            ):
                delete_object(old_logo_key)
        except (ValidationError, PermissionDenied) as error:
            _cleanup_staged_logo(staged_logo)
            if hasattr(error, "message_dict"):
                for _field_name, errors in error.message_dict.items():
                    for message in errors:
                        form.add_error(None, message)
            else:
                form.add_error(None, str(error))
            return render(
                request,
                "quotes/publish.html",
                {"quote": quote, "form": form, "conflict": False},
            )
        except OSError:
            _cleanup_staged_logo(staged_logo)
            form.add_error(None, "The logo could not be copied. Please try again.")
            return render(
                request,
                "quotes/publish.html",
                {"quote": quote, "form": form, "conflict": False},
                status=503,
            )
        record_authenticated_activity(request.user)
        return redirect("quotes:links", pk=quote.pk)

    return render(
        request,
        "quotes/publish.html",
        {"quote": quote, "form": form, "conflict": False},
    )


@login_required
@require_http_methods(["GET", "POST"])
def duplicate_quote_view(request: HttpRequest, pk) -> HttpResponse:
    quote = get_object_or_404(Quote, pk=pk, owner=request.user)
    if request.method == "POST":
        duplicate = duplicate_quote(quote=quote, user=request.user)
        record_authenticated_activity(request.user)
        return redirect("quotes:edit", pk=duplicate.pk)
    return render(
        request,
        "quotes/detail.html",
        {
            "quote": quote,
            "version": quote.current_published_version,
            "duplicate_confirmation": True,
            **_quote_snapshot_context(quote),
        },
    )


def _owner_link(quote: Quote, link_id, user):
    return get_object_or_404(
        ShareLink,
        pk=link_id,
        quote_version__quote=quote,
        quote_version__quote__owner=user,
    )


def _link_context(request: HttpRequest, quote: Quote, user):
    version = quote.current_published_version
    links = []
    if version is not None:
        for link in version.share_links.all():
            token = None
            try:
                token = decrypt_link_token(link)
            except Exception:
                token = None
            links.append(
                {
                    "link": link,
                    "state": link.state,
                    "url": request.build_absolute_uri(f"/q/{token}/") if token else "",
                    "can_recopy": token is not None,
                }
            )
    return {
        "quote": quote,
        "version": version,
        "links": links,
        "can_create": version is not None and user.can_publish,
    }


@login_required
@require_http_methods(["GET", "POST"])
def link_management(request: HttpRequest, pk) -> HttpResponse:
    quote = get_object_or_404(Quote, pk=pk, owner=request.user)
    if request.method == "POST":
        if quote.current_published_version_id is None:
            return redirect("quotes:detail", pk=quote.pk)
        action = request.POST.get("action")
        activity_recorded = False
        try:
            if action == "create":
                if not request.user.can_publish:
                    raise PermissionDenied(
                        "Verify your email before creating a private link."
                    )
                create_share_link(
                    version=quote.current_published_version,
                    now=timezone.now(),
                )
                activity_recorded = True
                messages.success(request, "Private link created.")
            elif action == "revoke":
                link = _owner_link(quote, request.POST.get("link_id"), request.user)
                revoke_share_link(link=link, now=timezone.now())
                activity_recorded = True
                messages.success(request, "Private link revoked.")
            elif action == "reissue":
                link = _owner_link(quote, request.POST.get("link_id"), request.user)
                if not request.user.can_publish:
                    raise PermissionDenied(
                        "Verify your email before reissuing a private link."
                    )
                reissue_share_link(link=link, now=timezone.now())
                activity_recorded = True
                messages.success(request, "A replacement private link was created.")
            else:
                messages.error(request, "Choose a valid link action.")
        except PermissionDenied:
            raise
        except (
            ImproperlyConfigured,
            ValidationError,
            ValueError,
        ) as error:
            messages.error(request, str(error))
        if activity_recorded:
            record_authenticated_activity(request.user)
        return redirect("quotes:links", pk=quote.pk)
    return render(
        request,
        "quotes/links.html",
        _link_context(request, quote, request.user),
    )


_PUBLIC_UNAVAILABLE_MESSAGE = "This quotation link is unavailable."
_PUBLIC_STATUS_LABELS = {
    Quote.Status.SENT: "Sent",
    Quote.Status.VIEWED: "Viewed",
    Quote.Status.ACCEPTED: "Accepted",
    Quote.Status.CHANGES_REQUESTED: "Changes requested",
}


def _with_public_headers(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _public_unavailable(request: HttpRequest) -> HttpResponse:
    return _with_public_headers(
        render(
            request,
            "quotes/public_interstitial.html",
            {
                "unavailable": True,
                "unavailable_message": _PUBLIC_UNAVAILABLE_MESSAGE,
            },
            status=404,
        )
    )


def _public_snapshot(link: ShareLink) -> dict | None:
    try:
        version = link.quote_version
        quote = version.quote
        snapshot = version.snapshot
    except (
        AttributeError,
        Quote.DoesNotExist,
        QuoteVersion.DoesNotExist,
        ShareLink.DoesNotExist,
    ):
        return None
    if link.quote_version_id != version.pk or quote.pk != version.quote_id:
        return None
    if quote.current_published_version_id != version.pk:
        return None
    if quote.status == Quote.Status.DRAFT:
        return None
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_schema_version") != 1:
        return None
    if snapshot.get("quote_number") != quote.quote_number:
        return None
    if snapshot.get("currency") != version.currency:
        return None
    if not isinstance(snapshot.get("line_items"), list):
        return None
    try:
        if Decimal(str(snapshot.get("chosen_total"))) != version.frozen_total:
            return None
    except (InvalidOperation, TypeError, ValueError):
        return None
    return copy.deepcopy(snapshot)


def _public_link_and_snapshot(
    token: str,
    *,
    allow_inactive: bool = False,
    now: datetime | None = None,
) -> tuple[ShareLink, dict] | None:
    try:
        current = normalize_datetime(now or timezone.now())
    except (TypeError, ValueError):
        return None
    if allow_inactive:
        try:
            token_hash = hash_token(token)
            link = ShareLink.objects.select_related(
                "quote_version",
                "quote_version__quote",
            ).get(token_hash=token_hash)
            state = link_state(link, now=current)
        except (
            AttributeError,
            TypeError,
            UnicodeError,
            ValueError,
            ShareLink.DoesNotExist,
        ):
            return None
        if state not in {"active", "expired", "revoked"}:
            return None
    else:
        try:
            link = resolve_share_link(token=token, now=current)
        except (AttributeError, TypeError, UnicodeError, ValueError):
            return None
        if link is None:
            return None
    snapshot = _public_snapshot(link)
    if snapshot is None:
        return None
    return link, snapshot


def _public_client_label(snapshot: dict) -> str:
    values = [
        str(value).strip()
        for value in (
            snapshot.get("client_company"),
            snapshot.get("client_name"),
        )
        if value is not None and str(value).strip()
    ]
    return " — ".join(values) or "Client"


def _public_status_label(status: str) -> str:
    return _PUBLIC_STATUS_LABELS.get(status, "Quotation")


def _new_response_form() -> ClientResponseForm:
    return ClientResponseForm(initial={"idempotency_key": uuid.uuid4()})


def _public_quote_context(
    *,
    request: HttpRequest,
    link: ShareLink,
    snapshot: dict,
    token: str | None = None,
    form: ClientResponseForm | None = None,
    response_message: str = "",
    response_status: str = "",
    response_alert: bool = False,
    response_submitted: bool = False,
) -> dict:
    quote = link.quote_version.quote
    scope = snapshot.get("scope")
    terms_details = snapshot.get("terms_details")
    dates = snapshot.get("dates")
    if not isinstance(scope, dict):
        scope = {}
    if not isinstance(terms_details, dict):
        terms_details = {}
    if not isinstance(dates, dict):
        dates = {}
    try:
        link_is_active = is_link_active(link, now=timezone.now())
    except (TypeError, ValueError):
        link_is_active = False
    if form is None:
        form = _new_response_form()
    accent_color = snapshot.get("accent_color", "#E85D3F")
    try:
        accent_foreground = readable_foreground(accent_color)
    except (TypeError, ValueError):
        accent_color = "#E85D3F"
        accent_foreground = readable_foreground(accent_color)
    logo_url = "../logo/" if token and snapshot.get("logo_object_key") else ""
    return {
        "snapshot": snapshot,
        "line_items": snapshot.get("line_items", []),
        "quote_number": snapshot.get("quote_number", ""),
        "project_title": snapshot.get("project_title", ""),
        "project_description": snapshot.get("project_description", ""),
        "client_name": snapshot.get("client_name", ""),
        "client_company": snapshot.get("client_company", ""),
        "client_label": _public_client_label(snapshot),
        "sender_name": snapshot.get("sender_name", ""),
        "sender_business_name": snapshot.get("sender_business_name", ""),
        "sender_monogram": snapshot.get("sender_monogram", ""),
        "accent_color": accent_color,
        "accent_foreground": accent_foreground,
        "csp_nonce": getattr(request, "csp_nonce", ""),
        "logo_url": logo_url,
        "currency": snapshot.get("currency", ""),
        "chosen_total": snapshot.get("chosen_total", ""),
        "deposit_amount": snapshot.get("deposit_amount", ""),
        "deposit_percentage": snapshot.get("deposit_percentage", ""),
        "scope": scope,
        "terms": snapshot.get("terms", ""),
        "terms_details": terms_details,
        "dates": dates,
        "issue_date": snapshot.get("issue_date", ""),
        "valid_until": snapshot.get("valid_until", ""),
        "status_label": _public_status_label(quote.status),
        "response_path": "../response/",
        "response_available": link_is_active
        and quote.status in {Quote.Status.SENT, Quote.Status.VIEWED},
        "form": form,
        "response_message": response_message,
        "response_status": response_status,
        "response_alert": response_alert,
        "response_submitted": response_submitted,
    }


def _public_interstitial_context(*, link: ShareLink, snapshot: dict) -> dict:
    return {
        "sender_name": snapshot.get("sender_name", ""),
        "sender_business_name": snapshot.get("sender_business_name", ""),
        "sender_monogram": snapshot.get("sender_monogram", ""),
        "client_label": _public_client_label(snapshot),
        "quote_number": snapshot.get("quote_number", ""),
        "expires_at": link.expires_at,
        "open_path": "open/",
    }


def _record_first_view(link: ShareLink, *, now: datetime) -> bool | None:
    try:
        current = normalize_datetime(now)
        with transaction.atomic():
            unlocked_link = ShareLink.objects.get(pk=link.pk)
            version_id = unlocked_link.quote_version_id
            version_identity = QuoteVersion.objects.get(pk=version_id)
            quote = Quote.objects.select_for_update().get(
                pk=version_identity.quote_id,
            )
            version = QuoteVersion.objects.select_for_update().get(pk=version_id)
            locked_link = ShareLink.objects.select_for_update().get(pk=link.pk)
            if not is_link_active(locked_link, now=current):
                return None
            if quote.current_published_version_id != version.pk:
                return None
            if quote.status == Quote.Status.DRAFT:
                return None
            if locked_link.first_viewed_at is None:
                locked_link.first_viewed_at = current
                locked_link.save(update_fields=("first_viewed_at",))
            if (
                quote.status in {Quote.Status.SENT, Quote.Status.VIEWED}
                and not QuoteEvent.objects.filter(
                    share_link=locked_link,
                    quote_version=version,
                    event_type=QuoteEvent.EventType.VIEWED,
                ).exists()
            ):
                QuoteEvent.objects.create(
                    quote=quote,
                    quote_version=version,
                    share_link=locked_link,
                    event_type=QuoteEvent.EventType.VIEWED,
                    event_time=current,
                )
            if quote.status == Quote.Status.SENT:
                quote.status = Quote.Status.VIEWED
                quote.last_viewed_at = current
                quote.save(update_fields=("status", "last_viewed_at"))
            elif quote.status == Quote.Status.VIEWED and quote.last_viewed_at is None:
                quote.last_viewed_at = current
                quote.save(update_fields=("last_viewed_at",))
        return True
    except (
        Quote.DoesNotExist,
        QuoteEvent.DoesNotExist,
        QuoteVersion.DoesNotExist,
        ShareLink.DoesNotExist,
        TypeError,
        ValidationError,
        ValueError,
    ):
        return None


def public_version_logo(request: HttpRequest, token: str) -> FileResponse:
    resolved = _public_link_and_snapshot(token, now=timezone.now())
    if resolved is None:
        raise Http404("Logo unavailable.")
    link, _snapshot = resolved
    return _version_logo_response(link.quote_version)


@require_http_methods(["GET"])
def public_interstitial(request: HttpRequest, token: str) -> HttpResponse:
    resolved = _public_link_and_snapshot(token, now=timezone.now())
    if resolved is None:
        return _public_unavailable(request)
    link, snapshot = resolved
    return _with_public_headers(
        render(
            request,
            "quotes/public_interstitial.html",
            _public_interstitial_context(
                link=link,
                snapshot=snapshot,
            ),
        )
    )


@require_POST
def public_open(request: HttpRequest, token: str) -> HttpResponse:
    now = timezone.now()
    resolved = _public_link_and_snapshot(token, now=now)
    if resolved is None:
        return _public_unavailable(request)
    link, _snapshot = resolved
    if _record_first_view(link, now=now) is None:
        return _public_unavailable(request)
    return _with_public_headers(redirect(f"/q/{token}/view/"))


@require_http_methods(["GET"])
def public_quote(request: HttpRequest, token: str) -> HttpResponse:
    resolved = _public_link_and_snapshot(token, now=timezone.now())
    if resolved is None:
        return _public_unavailable(request)
    link, snapshot = resolved
    return _with_public_headers(
        render(
            request,
            "quotes/public_quote.html",
            _public_quote_context(
                request=request,
                link=link,
                snapshot=snapshot,
                token=token,
            ),
        )
    )


@require_POST
def public_response(request: HttpRequest, token: str) -> HttpResponse:
    try:
        enforce_rate_limit(
            key=_public_response_rate_key(request, token),
            limit=int(
                getattr(
                    settings, "PUBLIC_RESPONSE_RATE_LIMIT", PUBLIC_RESPONSE_RATE_LIMIT
                )
            ),
            window_seconds=int(
                getattr(
                    settings, "PUBLIC_RESPONSE_RATE_WINDOW", PUBLIC_RESPONSE_RATE_WINDOW
                )
            ),
        )
    except RateLimitExceeded as error:
        return _rate_limited_public_response(request, error)
    now = timezone.now()
    resolved = _public_link_and_snapshot(
        token,
        allow_inactive=True,
        now=now,
    )
    if resolved is None:
        return _public_unavailable(request)
    link, snapshot = resolved
    form = ClientResponseForm(request.POST)
    if not form.is_valid():
        try:
            link_is_active = is_link_active(link, now=now)
        except (TypeError, ValueError):
            link_is_active = False
        if not link_is_active:
            return _public_unavailable(request)
        context = _public_quote_context(
            request=request,
            link=link,
            snapshot=snapshot,
            token=token,
            form=form,
            response_message=(
                "Review the highlighted fields before sending your response."
            ),
            response_alert=True,
        )
        return _with_public_headers(
            render(request, "quotes/public_quote.html", context, status=400)
        )
    result = record_response(
        link=link,
        action=form.cleaned_data["action"],
        note=form.cleaned_data["note"],
        idempotency_key=form.cleaned_data["idempotency_key"],
        now=now,
    )
    if result.status_code == 404:
        return _public_unavailable(request)
    try:
        link.refresh_from_db()
        link.quote_version.refresh_from_db()
        link.quote_version.quote.refresh_from_db()
    except (
        AttributeError,
        Quote.DoesNotExist,
        QuoteVersion.DoesNotExist,
        ShareLink.DoesNotExist,
    ):
        return _public_unavailable(request)
    context = _public_quote_context(
        request=request,
        link=link,
        snapshot=snapshot,
        token=token,
        form=form if result.status_code >= 400 else None,
        response_message=result.message,
        response_status=result.status,
        response_alert=result.status_code >= 400,
        response_submitted=result.status_code == 200,
    )
    return _with_public_headers(
        render(
            request,
            "quotes/public_quote.html",
            context,
            status=result.status_code,
        )
    )


public_interstitial_view = public_interstitial
public_open_view = public_open
public_quote_view = public_quote
public_response_view = public_response
public_view = public_quote
version_logo_view = version_logo
public_logo = public_version_logo
public_version_logo_view = public_version_logo
public_submit_response = public_response


new = new_quote
detail = quote_detail
edit = edit_quote
publish = publish_quote
duplicate = duplicate_quote_view
links = link_management
guest_pdf = guest_quote_pdf
pdf = quote_pdf
