from decimal import Decimal
from types import MappingProxyType
from typing import Final

PRICING_CONFIG_VERSION: Final[str] = "pricing-v1"
PRICING_PRECISION: Final[Decimal] = Decimal("28")
DECIMAL_PRECISION: Final[Decimal] = PRICING_PRECISION
PRECISION: Final[Decimal] = PRICING_PRECISION

HOURS_PER_DELIVERY_DAY: Final[Decimal] = Decimal("4")
HOURS_PER_DAY: Final[Decimal] = HOURS_PER_DELIVERY_DAY
INCLUDED_REVISION_ROUNDS: Final[Decimal] = Decimal("2")
ADDITIONAL_REVISION_HOURS: Final[Decimal] = Decimal("1.5")
MINIMUM_MULTIPLIER: Final[Decimal] = Decimal("0.88")
MAXIMUM_MULTIPLIER: Final[Decimal] = Decimal("1.12")
FEE_QUANTUM: Final[Decimal] = Decimal("5")
HOURLY_EQUIVALENT_QUANTUM: Final[Decimal] = Decimal("1")

CURRENCIES: Final[tuple[str, ...]] = ("USD", "EUR", "GBP")
COMPLEXITIES: Final[tuple[str, ...]] = (
    "straightforward",
    "standard",
    "complex",
    "advanced",
)
URGENCIES: Final[tuple[str, ...]] = ("standard", "priority", "rush")
EXPERIENCES: Final[tuple[str, ...]] = (
    "developing",
    "intermediate",
    "experienced",
    "specialist",
)

COMPLEXITY_MULTIPLIERS = MappingProxyType(
    {
        "straightforward": Decimal("0.90"),
        "standard": Decimal("1.00"),
        "complex": Decimal("1.35"),
        "advanced": Decimal("1.80"),
    }
)
COMPLEXITY_RISK_BUFFERS = MappingProxyType(
    {
        "straightforward": Decimal("0.05"),
        "standard": Decimal("0.10"),
        "complex": Decimal("0.15"),
        "advanced": Decimal("0.20"),
    }
)
URGENCY_MULTIPLIERS = MappingProxyType(
    {
        "standard": Decimal("1.00"),
        "priority": Decimal("1.18"),
        "rush": Decimal("1.35"),
    }
)
EXPERIENCE_RATES = MappingProxyType(
    {
        "developing": MappingProxyType(
            {
                "USD": Decimal("35"),
                "EUR": Decimal("35"),
                "GBP": Decimal("30"),
            }
        ),
        "intermediate": MappingProxyType(
            {
                "USD": Decimal("60"),
                "EUR": Decimal("55"),
                "GBP": Decimal("50"),
            }
        ),
        "experienced": MappingProxyType(
            {
                "USD": Decimal("90"),
                "EUR": Decimal("80"),
                "GBP": Decimal("75"),
            }
        ),
        "specialist": MappingProxyType(
            {
                "USD": Decimal("125"),
                "EUR": Decimal("110"),
                "GBP": Decimal("105"),
            }
        ),
    }
)

COMPLEXITY_CONFIG = MappingProxyType(
    {
        key: MappingProxyType(
            {
                "effort_multiplier": COMPLEXITY_MULTIPLIERS[key],
                "risk_buffer": COMPLEXITY_RISK_BUFFERS[key],
            }
        )
        for key in COMPLEXITIES
    }
)
COMPLEXITY_TABLE = COMPLEXITY_CONFIG
RATE_TABLE = EXPERIENCE_RATES
URGENCY_TABLE = URGENCY_MULTIPLIERS
COMPLEXITY_MULTIPLIER = COMPLEXITY_MULTIPLIERS
HOURLY_RATES = EXPERIENCE_RATES
