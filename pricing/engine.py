from decimal import ROUND_HALF_UP, Decimal, localcontext

from pricing.constants import (
    ADDITIONAL_REVISION_HOURS,
    COMPLEXITY_MULTIPLIERS,
    COMPLEXITY_RISK_BUFFERS,
    CURRENCIES,
    EXPERIENCE_RATES,
    FEE_QUANTUM,
    HOURLY_EQUIVALENT_QUANTUM,
    HOURS_PER_DELIVERY_DAY,
    INCLUDED_REVISION_ROUNDS,
    MAXIMUM_MULTIPLIER,
    MINIMUM_MULTIPLIER,
    PRICING_CONFIG_VERSION,
    PRICING_PRECISION,
    URGENCY_MULTIPLIERS,
)
from pricing.types import EstimateInputs, EstimateResult, FactorRow


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quantize_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def _validate_choice(value, choices, field_name):
    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(choices)
        raise ValueError(f"{field_name} must be one of: {allowed}")


def _validate_integer(value, field_name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")


def _factor(identifier: str, label: str, value: str) -> FactorRow:
    return {"id": identifier, "label": label, "value": value}


def calculate_estimate(inputs: EstimateInputs) -> EstimateResult:
    if not isinstance(inputs, EstimateInputs):
        raise TypeError("inputs must be an EstimateInputs instance")
    if not isinstance(inputs.description, str):
        raise ValueError("description must be a string")

    _validate_choice(inputs.complexity, COMPLEXITY_MULTIPLIERS, "complexity")
    _validate_choice(inputs.urgency, URGENCY_MULTIPLIERS, "urgency")
    _validate_choice(inputs.experience, EXPERIENCE_RATES, "experience")
    _validate_choice(inputs.currency, CURRENCIES, "currency")
    if inputs.delivery_days == 0:
        raise ValueError("effective_hours must be positive")
    _validate_integer(inputs.delivery_days, "delivery_days", 1, 60)
    _validate_integer(inputs.revision_rounds, "revision_rounds", 0, 10)

    complexity_multiplier = COMPLEXITY_MULTIPLIERS[inputs.complexity]
    risk_buffer = COMPLEXITY_RISK_BUFFERS[inputs.complexity]
    urgency_multiplier = URGENCY_MULTIPLIERS[inputs.urgency]
    hourly_rate = EXPERIENCE_RATES[inputs.experience][inputs.currency]

    with localcontext() as context:
        context.prec = int(PRICING_PRECISION)
        context.rounding = ROUND_HALF_UP
        effective_hours = (
            Decimal(inputs.delivery_days)
            * HOURS_PER_DELIVERY_DAY
            * complexity_multiplier
        )
        additional_revisions = max(
            Decimal(inputs.revision_rounds) - INCLUDED_REVISION_ROUNDS,
            Decimal("0"),
        )
        additional_revision_time = additional_revisions * ADDITIONAL_REVISION_HOURS
        effective_hours += additional_revision_time
        if effective_hours <= 0:
            raise ValueError("effective_hours must be positive")

        base_fee = effective_hours * hourly_rate
        risk_adjusted_fee = base_fee * (Decimal("1") + risk_buffer)
        recommended_fee_exact = risk_adjusted_fee * urgency_multiplier
        minimum_fee_exact = recommended_fee_exact * MINIMUM_MULTIPLIER
        maximum_fee_exact = recommended_fee_exact * MAXIMUM_MULTIPLIER
        hourly_equivalent_exact = recommended_fee_exact / effective_hours

        minimum = _quantize_to_step(minimum_fee_exact, FEE_QUANTUM)
        recommended = _quantize_to_step(recommended_fee_exact, FEE_QUANTUM)
        maximum = _quantize_to_step(maximum_fee_exact, FEE_QUANTUM)
        hourly_equivalent = _quantize_to_step(
            hourly_equivalent_exact, HOURLY_EQUIVALENT_QUANTUM
        )

    factors = (
        _factor(
            "effective_hours",
            (
                f"Effective hours ({_format_decimal(HOURS_PER_DELIVERY_DAY)} "
                "focused hours per delivery day)"
            ),
            _format_decimal(effective_hours),
        ),
        _factor(
            "complexity_multiplier",
            "Complexity effort multiplier",
            _format_decimal(complexity_multiplier),
        ),
        _factor(
            "experience_rate",
            f"{inputs.experience.title()} reference rate ({inputs.currency})",
            _format_decimal(hourly_rate),
        ),
        _factor(
            "revision_time",
            (
                f"Revision time ({_format_decimal(INCLUDED_REVISION_ROUNDS)} included "
                "rounds)"
            ),
            (
                f"+{_format_decimal(additional_revision_time)}"
                if additional_revisions
                else "0"
            ),
        ),
        _factor(
            "risk_buffer",
            "Complexity risk buffer",
            f"{_format_decimal(risk_buffer * Decimal('100'))}%",
        ),
        _factor(
            "urgency_multiplier",
            "Urgency multiplier",
            _format_decimal(urgency_multiplier),
        ),
        _factor("minimum", "Minimum fee", _format_decimal(minimum)),
        _factor("recommended", "Recommended fee", _format_decimal(recommended)),
        _factor("maximum", "Maximum fee", _format_decimal(maximum)),
        _factor(
            "hourly_equivalent",
            "Hourly equivalent",
            _format_decimal(hourly_equivalent),
        ),
    )

    return EstimateResult(
        effective_hours=effective_hours,
        minimum=minimum,
        recommended=recommended,
        maximum=maximum,
        hourly_equivalent=hourly_equivalent,
        pricing_config_version=PRICING_CONFIG_VERSION,
        factors=factors,
    )
