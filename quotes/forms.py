from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils import timezone

from accounts.services import canonicalize_email, validate_website_url
from pricing.engine import calculate_estimate
from pricing.forms import (
    COMPLEXITY_CHOICES,
    CURRENCY_CHOICES,
    EXPERIENCE_CHOICES,
    URGENCY_CHOICES,
)
from pricing.types import EstimateInputs
from quotes.models import Quote, ScopeCategory
from quotes.services.quotes import (
    calculate_line_item_total,
    requires_intentional_adjustment,
)
from quotes.services.responses import normalize_action

MAX_LINE_ITEMS = 50
ZERO = Decimal("0.00")


class ISODateTimeInput(forms.DateTimeInput):
    supports_microseconds = True


_ACCENT_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message="Accent color must use #RRGGBB format.",
)


def _decimal_exact(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid decimal.") from exc
    if not result.is_finite() or result.as_tuple().exponent < -2:
        raise ValidationError(f"{field_name} must use no more than two decimal places.")
    return result


def _normalize_editor_data(data):
    if data is None:
        return None
    normalized = data.copy()
    aliases = {
        "title": "project_title",
        "description": "project_description",
        "duration_days": "delivery_days",
        "project_category": "category",
        "revisions": "revision_rounds",
        "revision_count": "revision_rounds",
        "sender_business": "sender_business_name",
        "sender_company": "sender_business_name",
        "company_name": "client_company",
        "brand_color": "accent_color",
        "tax_statement": "taxes",
        "cancellation_text": "cancellation_terms",
    }
    for alias, field_name in aliases.items():
        if not normalized.get(field_name) and normalized.get(alias) is not None:
            normalized[field_name] = normalized.get(alias)
    return normalized


def _quote_initial(quote: Quote | None) -> dict[str, Any]:
    if quote is None:
        return {}
    return {
        "project_title": quote.project_title,
        "project_description": quote.project_description,
        "category": quote.category,
        "delivery_days": quote.delivery_days,
        "complexity": quote.complexity,
        "revision_rounds": quote.revision_rounds,
        "urgency": quote.urgency,
        "experience": quote.experience,
        "currency": quote.currency,
        "client_name": quote.client_name,
        "client_company": quote.client_company,
        "client_email": quote.client_email,
        "sender_name": quote.sender_name,
        "sender_business_name": quote.sender_business_name,
        "sender_email": quote.sender_email,
        "sender_website": quote.sender_website,
        "sender_monogram": quote.sender_monogram,
        "accent_color": quote.accent_color,
        "issue_date": quote.issue_date,
        "valid_until": quote.valid_until,
        "deposit_percentage": quote.deposit_percentage,
        "timeline": quote.timeline,
        "revision_terms": quote.revision_terms,
        "terms": quote.terms,
        "notes": quote.notes,
        "exclusions": quote.exclusions,
        "taxes": quote.taxes,
        "cancellation_terms": quote.cancellation_terms,
        "updated_at": quote.updated_at,
    }


def _profile_initial(profile) -> dict[str, Any]:
    if profile is None:
        return {}
    today = timezone.localdate()
    validity_days = profile.default_quote_validity_days or 30
    return {
        "sender_name": profile.full_name,
        "sender_business_name": profile.business_name,
        "sender_email": profile.user.email,
        "sender_website": profile.website,
        "currency": profile.default_currency,
        "deposit_percentage": profile.default_deposit_percentage,
        "accent_color": profile.default_accent_color,
        "issue_date": today,
        "valid_until": today + timedelta(days=validity_days),
    }


class QuoteEditorForm(forms.Form):
    project_title = forms.CharField(
        label="Project title",
        max_length=160,
        min_length=1,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    project_description = forms.CharField(
        label="Project overview",
        max_length=3000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
    )
    category = forms.ChoiceField(
        label="Scope category",
        required=False,
        choices=(("", "Choose a category"), *ScopeCategory.choices),
    )
    delivery_days = forms.IntegerField(
        label="Expected duration",
        min_value=1,
        max_value=60,
        widget=forms.NumberInput(attrs={"min": 1, "max": 60, "step": 1}),
    )
    complexity = forms.ChoiceField(label="Complexity", choices=COMPLEXITY_CHOICES)
    revision_rounds = forms.IntegerField(
        label="Revision rounds",
        min_value=0,
        max_value=10,
        widget=forms.NumberInput(attrs={"min": 0, "max": 10, "step": 1}),
    )
    urgency = forms.ChoiceField(label="Urgency", choices=URGENCY_CHOICES)
    experience = forms.ChoiceField(label="Experience", choices=EXPERIENCE_CHOICES)
    currency = forms.ChoiceField(label="Currency", choices=CURRENCY_CHOICES)
    client_name = forms.CharField(label="Client name", max_length=160, required=False)
    client_company = forms.CharField(
        label="Client company", max_length=255, required=False
    )
    client_email = forms.EmailField(
        label="Client email", max_length=254, required=False
    )
    sender_name = forms.CharField(label="Sender name", max_length=160, required=False)
    sender_business_name = forms.CharField(
        label="Sender business",
        max_length=255,
        required=False,
    )
    sender_email = forms.EmailField(
        label="Sender email", max_length=254, required=False
    )
    sender_website = forms.URLField(
        label="Sender website", max_length=2048, required=False
    )
    sender_monogram = forms.CharField(
        label="Sender monogram", max_length=8, required=False
    )
    accent_color = forms.CharField(
        label="Accent color",
        max_length=7,
        required=False,
        validators=[_ACCENT_COLOR_VALIDATOR],
        widget=forms.TextInput(attrs={"placeholder": "#RRGGBB"}),
    )
    issue_date = forms.DateField(label="Issue date", required=False)
    valid_until = forms.DateField(label="Valid until", required=False)
    deposit_percentage = forms.IntegerField(
        label="Deposit percentage",
        min_value=0,
        max_value=100,
        widget=forms.NumberInput(attrs={"min": 0, "max": 100, "step": 5}),
    )
    timeline = forms.CharField(
        label="Delivery timeline",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    revision_terms = forms.CharField(
        label="Revision terms",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    terms = forms.CharField(
        label="Terms",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    notes = forms.CharField(
        label="Notes",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    exclusions = forms.CharField(
        label="Exclusions",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    taxes = forms.CharField(
        label="Taxes",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    cancellation_terms = forms.CharField(
        label="Cancellation terms",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    updated_at = forms.DateTimeField(
        required=False,
        widget=ISODateTimeInput(
            format="%Y-%m-%dT%H:%M:%S.%f",
            attrs={"type": "hidden"},
        ),
        input_formats=[
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
        ],
    )
    logo = forms.FileField(required=False, widget=forms.ClearableFileInput)

    def __init__(
        self,
        *args,
        quote: Quote | None = None,
        instance: Quote | None = None,
        user=None,
        profile=None,
        **kwargs,
    ):
        quote = quote or instance
        self.quote = quote
        self.user = user
        if profile is None and user is not None and getattr(user, "pk", None):
            try:
                profile = user.profile
            except Exception:
                profile = None
        initial = _profile_initial(profile)
        initial.update(_quote_initial(quote))
        if kwargs.get("initial"):
            initial.update(kwargs["initial"])
        kwargs["initial"] = initial
        if args:
            args = (_normalize_editor_data(args[0]), *args[1:])
        if "data" in kwargs:
            kwargs["data"] = _normalize_editor_data(kwargs["data"])
        super().__init__(*args, **kwargs)

    def clean_client_email(self):
        value = self.cleaned_data.get("client_email", "")
        return canonicalize_email(value) if value else value

    def clean_sender_email(self):
        value = self.cleaned_data.get("sender_email", "")
        return canonicalize_email(value) if value else value

    def clean_currency(self):
        value = self.cleaned_data.get("currency")
        if self.quote is not None and value != self.quote.currency:
            raise ValidationError("Duplicate the quote to change its currency.")
        return value

    def clean_sender_website(self):
        value = self.cleaned_data.get("sender_website", "")
        validate_website_url(value)
        return value

    def clean(self):
        cleaned_data = super().clean()
        issue_date = cleaned_data.get("issue_date")
        valid_until = cleaned_data.get("valid_until")
        if issue_date and valid_until:
            from quotes.services.quotes import quote_validity_days

            try:
                quote_validity_days(
                    issue_date=issue_date,
                    valid_until=valid_until,
                )
            except ValueError as error:
                self.add_error("valid_until", str(error))
        deposit = cleaned_data.get("deposit_percentage")
        if deposit is not None and deposit % 5:
            self.add_error(
                "deposit_percentage",
                "Deposit percentage must use five-point increments.",
            )
        return cleaned_data

    def estimate_inputs(self) -> EstimateInputs:
        if not self.is_valid():
            raise ValueError("The quote editor form is invalid.")
        return EstimateInputs(
            description=self.cleaned_data["project_description"],
            delivery_days=self.cleaned_data["delivery_days"],
            complexity=self.cleaned_data["complexity"],
            revision_rounds=self.cleaned_data["revision_rounds"],
            urgency=self.cleaned_data["urgency"],
            experience=self.cleaned_data["experience"],
            currency=self.cleaned_data["currency"],
        )

    def estimate(self):
        return calculate_estimate(self.estimate_inputs())

    def estimate_result(self):
        return self.estimate()

    get_estimate = estimate
    calculate_estimate = estimate

    def apply_to_quote(self, quote: Quote) -> Quote:
        for field_name in (
            self.Meta.fields
            if hasattr(self, "Meta")
            else (
                "project_title",
                "project_description",
                "category",
                "delivery_days",
                "complexity",
                "revision_rounds",
                "urgency",
                "experience",
                "currency",
                "client_name",
                "client_company",
                "client_email",
                "sender_name",
                "sender_business_name",
                "sender_email",
                "sender_website",
                "sender_monogram",
                "accent_color",
                "issue_date",
                "valid_until",
                "deposit_percentage",
                "timeline",
                "revision_terms",
                "terms",
                "notes",
                "exclusions",
                "taxes",
                "cancellation_terms",
            )
        ):
            if field_name in self.cleaned_data:
                setattr(quote, field_name, self.cleaned_data[field_name])
        return quote


class _ResponseActionField(forms.ChoiceField):
    def to_python(self, value):
        normalized = normalize_action(value)
        if normalized is not None:
            return normalized
        return super().to_python(value)


class ClientResponseForm(forms.Form):
    action = _ResponseActionField(
        label="Response",
        choices=(
            ("accepted", "Accept quotation"),
            ("changes_requested", "Request changes"),
        ),
        widget=forms.RadioSelect(attrs={"required": True}),
    )
    note = forms.CharField(
        label="Note",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    idempotency_key = forms.UUIDField(
        label="Response key",
        widget=forms.HiddenInput,
    )


class QuoteLineItemForm(forms.Form):
    label = forms.CharField(label="Deliverable", max_length=160, required=False)
    description = forms.CharField(
        label="Description",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    amount = forms.DecimalField(
        label="Amount", max_digits=14, decimal_places=2, required=False
    )
    sort_order = forms.IntegerField(label="Order", min_value=0, required=False)

    def clean_amount(self):
        value = self.cleaned_data.get("amount")
        if value is not None:
            _decimal_exact(value, "Amount")
        return value

    def clean(self):
        cleaned_data = super().clean()
        if not any(
            cleaned_data.get(name) not in (None, "")
            for name in ("label", "description", "amount", "sort_order")
        ):
            return cleaned_data
        if not cleaned_data.get("label"):
            self.add_error("label", "A line-item label is required.")
        if cleaned_data.get("amount") is None:
            self.add_error("amount", "A line-item amount is required.")
        return cleaned_data


class _QuoteLineItemFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        items = []
        for form in self.forms:
            if form.cleaned_data.get("DELETE"):
                continue
            if not form.cleaned_data or not any(
                form.cleaned_data.get(name) not in (None, "")
                for name in ("label", "description", "amount", "sort_order")
            ):
                continue
            items.append(form.cleaned_data)
        if len(items) > MAX_LINE_ITEMS:
            raise ValidationError("A quote cannot contain more than 50 line items.")
        if not items:
            self.total = ZERO
            return
        amounts = [item["amount"] for item in items]
        if not any(amount > ZERO for amount in amounts):
            raise ValidationError("At least one line item must have a positive amount.")
        if sum(amounts, ZERO) <= ZERO:
            raise ValidationError("The line-item total must be greater than zero.")
        self.total = sum(amounts, ZERO).quantize(ZERO)


_GeneratedQuoteLineItemFormSet = forms.formset_factory(
    QuoteLineItemForm,
    formset=_QuoteLineItemFormSet,
    extra=1,
    can_delete=True,
    can_order=False,
    min_num=1,
    max_num=MAX_LINE_ITEMS,
    validate_min=True,
    validate_max=False,
)


class QuoteLineItemFormSet(_GeneratedQuoteLineItemFormSet):
    def __init__(self, *args, **kwargs):
        data = args[0] if args else kwargs.get("data")
        if (
            data is not None
            and "line_items-TOTAL_FORMS" not in data
            and "form-TOTAL_FORMS" in data
        ):
            kwargs["prefix"] = "form"
        else:
            kwargs.setdefault("prefix", "line_items")
        prefix = kwargs["prefix"]
        self._missing_management_form = (
            data is not None and f"{prefix}-TOTAL_FORMS" not in data
        )
        queryset = kwargs.pop("queryset", None)
        initial_form_count = kwargs.pop("initial_form_count", None)
        if queryset is not None:
            items = list(queryset)
            kwargs.setdefault(
                "initial",
                [
                    {
                        "label": item.label,
                        "description": item.description,
                        "amount": item.amount,
                        "sort_order": item.sort_order,
                    }
                    for item in items
                ],
            )
            if initial_form_count is None:
                initial_form_count = len(items)
        if initial_form_count is None and data is not None:
            try:
                initial_form_count = int(data.get(f"{prefix}-INITIAL_FORMS", 0))
            except (TypeError, ValueError):
                initial_form_count = 0
        self._initial_form_count = initial_form_count or 0
        self.extra = 0 if self._initial_form_count >= MAX_LINE_ITEMS else 1
        super().__init__(*args, **kwargs)

    def clean(self):
        if self._missing_management_form:
            raise ValidationError("At least one line item is required.")
        return super().clean()

    def initial_form_count(self):
        return self._initial_form_count


class QuotePublishForm(forms.Form):
    project_title = forms.CharField(label="Project title", max_length=160)
    project_description = forms.CharField(
        label="Project overview",
        min_length=30,
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    client_name = forms.CharField(label="Client name", max_length=160)
    client_email = forms.EmailField(label="Client email", max_length=254)
    sender_name = forms.CharField(label="Sender name", max_length=160)
    sender_email = forms.EmailField(label="Sender email", max_length=254)
    issue_date = forms.DateField(label="Issue date")
    valid_until = forms.DateField(label="Valid until")
    terms = forms.CharField(
        label="Terms",
        min_length=1,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    intentional_adjustment_confirmed = forms.BooleanField(
        label="I intentionally adjusted the total outside the recommended range",
        required=False,
    )
    confirm_intentional_adjustment = forms.BooleanField(
        required=False, widget=forms.HiddenInput
    )
    confirm_adjustment = forms.BooleanField(required=False, widget=forms.HiddenInput)
    intentional_adjustment = forms.BooleanField(
        required=False, widget=forms.HiddenInput
    )
    logo = forms.FileField(required=False, widget=forms.ClearableFileInput)
    updated_at = forms.DateTimeField(
        required=False,
        widget=ISODateTimeInput(
            format="%Y-%m-%dT%H:%M:%S.%f",
            attrs={"type": "hidden"},
        ),
        input_formats=[
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
        ],
    )

    def __init__(
        self,
        *args,
        quote: Quote | None = None,
        instance: Quote | None = None,
        **kwargs,
    ):
        quote = quote or instance
        self.quote = quote
        initial = {}
        if quote is not None:
            initial = {
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
            }
        if kwargs.get("initial"):
            initial.update(kwargs["initial"])
        kwargs["initial"] = initial
        if args:
            args = (_normalize_editor_data(args[0]), *args[1:])
        if "data" in kwargs:
            kwargs["data"] = _normalize_editor_data(kwargs["data"])
        super().__init__(*args, **kwargs)

    def clean_client_email(self):
        value = self.cleaned_data.get("client_email", "")
        return canonicalize_email(value) if value else value

    def clean_sender_email(self):
        value = self.cleaned_data.get("sender_email", "")
        return canonicalize_email(value) if value else value

    def clean(self):
        cleaned_data = super().clean()
        if self.quote is not None:
            for field_name in (
                "project_title",
                "project_description",
                "client_name",
                "client_email",
                "sender_name",
                "sender_email",
                "issue_date",
                "valid_until",
                "terms",
            ):
                if not cleaned_data.get(field_name):
                    value = getattr(self.quote, field_name)
                    if value not in (None, ""):
                        cleaned_data[field_name] = value
        required_fields = (
            "project_title",
            "project_description",
            "client_name",
            "client_email",
            "sender_name",
            "sender_email",
            "issue_date",
            "valid_until",
            "terms",
        )
        for field_name in required_fields:
            if not cleaned_data.get(field_name):
                self.add_error(field_name, "This field is required before publishing.")
        issue_date = cleaned_data.get("issue_date")
        valid_until = cleaned_data.get("valid_until")
        if issue_date and valid_until:
            from quotes.services.quotes import quote_validity_days

            try:
                quote_validity_days(
                    issue_date=issue_date,
                    valid_until=valid_until,
                )
            except ValueError as error:
                self.add_error("valid_until", str(error))
        if self.quote is not None:
            if not self.quote.line_items.exists():
                self.add_error(None, "At least one line item is required.")
            else:
                try:
                    total = calculate_line_item_total(self.quote.line_items.all())
                except ValueError as error:
                    self.add_error(None, str(error))
                else:
                    try:
                        needs_confirmation = requires_intentional_adjustment(
                            chosen_total=total,
                            recommended_min=self.quote.minimum,
                            recommended_max=self.quote.maximum,
                        )
                    except ValueError as error:
                        self.add_error(None, str(error))
                    else:
                        confirmed = bool(
                            cleaned_data.get("intentional_adjustment_confirmed")
                            or cleaned_data.get("confirm_intentional_adjustment")
                            or cleaned_data.get("confirm_adjustment")
                            or cleaned_data.get("intentional_adjustment")
                        )
                        if needs_confirmation and not confirmed:
                            self.add_error(
                                "intentional_adjustment_confirmed",
                                "Confirm the intentional adjustment before publishing.",
                            )
        return cleaned_data

    def apply_to_quote(self, quote: Quote) -> Quote:
        for field_name in (
            "project_title",
            "project_description",
            "client_name",
            "client_email",
            "sender_name",
            "sender_email",
            "issue_date",
            "valid_until",
            "terms",
        ):
            if field_name in self.cleaned_data:
                setattr(quote, field_name, self.cleaned_data[field_name])
        return quote
