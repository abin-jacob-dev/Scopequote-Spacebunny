from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict


class FactorRow(TypedDict):
    id: str
    label: str
    value: str


@dataclass(frozen=True)
class EstimateInputs:
    description: str
    delivery_days: int
    complexity: str
    revision_rounds: int
    urgency: str
    experience: str
    currency: str


@dataclass(frozen=True)
class EstimateResult:
    effective_hours: Decimal
    minimum: Decimal
    recommended: Decimal
    maximum: Decimal
    hourly_equivalent: Decimal
    pricing_config_version: str
    factors: tuple[FactorRow, ...]
