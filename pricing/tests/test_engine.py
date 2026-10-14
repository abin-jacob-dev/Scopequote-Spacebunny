import json
from dataclasses import FrozenInstanceError, fields, is_dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from unittest import TestCase

from pricing.constants import (
    COMPLEXITY_MULTIPLIERS,
    COMPLEXITY_RISK_BUFFERS,
    CURRENCIES,
    DECIMAL_PRECISION,
    EXPERIENCE_RATES,
    INCLUDED_REVISION_ROUNDS,
    PRICING_CONFIG_VERSION,
    URGENCY_MULTIPLIERS,
)
from pricing.engine import calculate_estimate
from pricing.types import EstimateInputs, EstimateResult

REFERENCE_COMPLEXITY_MULTIPLIERS = {
    "straightforward": Decimal("0.90"),
    "standard": Decimal("1.00"),
    "complex": Decimal("1.35"),
    "advanced": Decimal("1.80"),
}
REFERENCE_COMPLEXITY_RISK_BUFFERS = {
    "straightforward": Decimal("0.05"),
    "standard": Decimal("0.10"),
    "complex": Decimal("0.15"),
    "advanced": Decimal("0.20"),
}
REFERENCE_URGENCY_MULTIPLIERS = {
    "standard": Decimal("1.00"),
    "priority": Decimal("1.18"),
    "rush": Decimal("1.35"),
}
REFERENCE_RATES = {
    "developing": {"USD": Decimal("35"), "EUR": Decimal("35"), "GBP": Decimal("30")},
    "intermediate": {
        "USD": Decimal("60"),
        "EUR": Decimal("55"),
        "GBP": Decimal("50"),
    },
    "experienced": {"USD": Decimal("90"), "EUR": Decimal("80"), "GBP": Decimal("75")},
    "specialist": {
        "USD": Decimal("125"),
        "EUR": Decimal("110"),
        "GBP": Decimal("105"),
    },
}


class EstimateInputsTests(TestCase):
    def test_inputs_are_frozen_and_expose_the_approved_fields(self):
        inputs = EstimateInputs(
            description="A focused project",
            delivery_days=5,
            complexity="standard",
            revision_rounds=0,
            urgency="standard",
            experience="intermediate",
            currency="USD",
        )

        self.assertTrue(is_dataclass(inputs))
        self.assertEqual(
            [field.name for field in fields(inputs)],
            [
                "description",
                "delivery_days",
                "complexity",
                "revision_rounds",
                "urgency",
                "experience",
                "currency",
            ],
        )
        with self.assertRaises(FrozenInstanceError):
            inputs.delivery_days = 6


class EstimateResultTests(TestCase):
    def test_result_is_frozen_and_exposes_the_approved_fields(self):
        result = EstimateResult(
            effective_hours=Decimal("20"),
            minimum=Decimal("1160"),
            recommended=Decimal("1320"),
            maximum=Decimal("1480"),
            hourly_equivalent=Decimal("66"),
            pricing_config_version=PRICING_CONFIG_VERSION,
            factors=(),
        )

        self.assertTrue(is_dataclass(result))
        self.assertEqual(
            [field.name for field in fields(result)],
            [
                "effective_hours",
                "minimum",
                "recommended",
                "maximum",
                "hourly_equivalent",
                "pricing_config_version",
                "factors",
            ],
        )
        with self.assertRaises(FrozenInstanceError):
            result.recommended = Decimal("1")


class PricingEngineTests(TestCase):
    def make_inputs(self, **overrides):
        values = {
            "description": "A representative project description",
            "delivery_days": 5,
            "complexity": "standard",
            "revision_rounds": 0,
            "urgency": "standard",
            "experience": "intermediate",
            "currency": "USD",
        }
        values.update(overrides)
        return EstimateInputs(**values)

    def calculate_reference(self, inputs):
        def quantize_to_step(value, step):
            return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step

        with localcontext() as context:
            context.prec = 28
            complexity_multiplier = REFERENCE_COMPLEXITY_MULTIPLIERS[inputs.complexity]
            risk_buffer = REFERENCE_COMPLEXITY_RISK_BUFFERS[inputs.complexity]
            urgency_multiplier = REFERENCE_URGENCY_MULTIPLIERS[inputs.urgency]
            effective_hours = (
                Decimal(inputs.delivery_days) * Decimal("4") * complexity_multiplier
            )
            effective_hours += max(
                Decimal(inputs.revision_rounds) - Decimal("2"), Decimal("0")
            ) * Decimal("1.5")
            base_fee = (
                effective_hours * REFERENCE_RATES[inputs.experience][inputs.currency]
            )
            risk_adjusted_fee = base_fee * (Decimal("1") + risk_buffer)
            recommended_exact = risk_adjusted_fee * urgency_multiplier
            minimum_exact = recommended_exact * Decimal("0.88")
            maximum_exact = recommended_exact * Decimal("1.12")
            hourly_exact = recommended_exact / effective_hours
            return {
                "effective_hours": effective_hours,
                "minimum": quantize_to_step(minimum_exact, Decimal("5")),
                "recommended": quantize_to_step(recommended_exact, Decimal("5")),
                "maximum": quantize_to_step(maximum_exact, Decimal("5")),
                "hourly_equivalent": quantize_to_step(hourly_exact, Decimal("1")),
            }

    def assert_matches_reference(self, inputs):
        result = calculate_estimate(inputs)
        expected = self.calculate_reference(inputs)
        for name, value in expected.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(result, name), value)
                self.assertIsInstance(getattr(result, name), Decimal)
        self.assertEqual(result.pricing_config_version, "pricing-v1")
        return result

    def test_configuration_tables_match_approved_literals(self):
        self.assertEqual(
            {key: str(value) for key, value in COMPLEXITY_MULTIPLIERS.items()},
            {
                key: str(value)
                for key, value in REFERENCE_COMPLEXITY_MULTIPLIERS.items()
            },
        )
        self.assertEqual(
            {key: str(value) for key, value in COMPLEXITY_RISK_BUFFERS.items()},
            {
                key: str(value)
                for key, value in REFERENCE_COMPLEXITY_RISK_BUFFERS.items()
            },
        )
        self.assertEqual(
            {key: str(value) for key, value in URGENCY_MULTIPLIERS.items()},
            {key: str(value) for key, value in REFERENCE_URGENCY_MULTIPLIERS.items()},
        )
        self.assertIsInstance(INCLUDED_REVISION_ROUNDS, Decimal)
        self.assertEqual(INCLUDED_REVISION_ROUNDS, Decimal("2"))

    def test_approved_rate_table_and_standard_urgency(self):
        for experience, rates in EXPERIENCE_RATES.items():
            for currency, _rate in rates.items():
                with self.subTest(experience=experience, currency=currency):
                    inputs = self.make_inputs(
                        delivery_days=1,
                        complexity="standard",
                        experience=experience,
                        urgency="standard",
                        currency=currency,
                    )
                    result = self.assert_matches_reference(inputs)
                    self.assertGreater(result.effective_hours, Decimal("0"))
                    self.assertLess(result.minimum, result.recommended)
                    self.assertLess(result.recommended, result.maximum)

    def test_all_complexity_multipliers_and_risk_buffers(self):
        for complexity, multiplier in REFERENCE_COMPLEXITY_MULTIPLIERS.items():
            with self.subTest(complexity=complexity):
                inputs = self.make_inputs(complexity=complexity)
                result = self.assert_matches_reference(inputs)
                factors = {factor["id"]: factor for factor in result.factors}
                self.assertEqual(
                    factors["complexity_multiplier"]["value"],
                    str(multiplier).rstrip("0").rstrip("."),
                )
                risk_buffer = REFERENCE_COMPLEXITY_RISK_BUFFERS[complexity]
                self.assertEqual(
                    factors["risk_buffer"]["value"],
                    f"{str(risk_buffer * Decimal('100')).rstrip('0').rstrip('.')}%",
                )

    def test_all_urgency_multipliers(self):
        for urgency, multiplier in REFERENCE_URGENCY_MULTIPLIERS.items():
            with self.subTest(urgency=urgency):
                result = self.assert_matches_reference(
                    self.make_inputs(urgency=urgency)
                )
                urgency_factor = next(
                    factor
                    for factor in result.factors
                    if factor["id"] == "urgency_multiplier"
                )
                self.assertEqual(
                    urgency_factor["value"], str(multiplier).rstrip("0").rstrip(".")
                )

    def test_every_currency_uses_its_exact_rate(self):
        expected_rates = {
            "developing": {"USD": "35", "EUR": "35", "GBP": "30"},
            "intermediate": {"USD": "60", "EUR": "55", "GBP": "50"},
            "experienced": {"USD": "90", "EUR": "80", "GBP": "75"},
            "specialist": {"USD": "125", "EUR": "110", "GBP": "105"},
        }
        self.assertEqual(
            {
                experience: {currency: str(rate) for currency, rate in rates.items()}
                for experience, rates in EXPERIENCE_RATES.items()
            },
            expected_rates,
        )
        for currency in CURRENCIES:
            with self.subTest(currency=currency):
                self.assert_matches_reference(self.make_inputs(currency=currency))

    def test_revision_boundaries(self):
        expected_hours = {
            0: Decimal("20"),
            1: Decimal("20"),
            2: Decimal("20"),
            3: Decimal("21.5"),
            10: Decimal("32"),
        }
        for revisions, expected in expected_hours.items():
            with self.subTest(revision_rounds=revisions):
                result = self.assert_matches_reference(
                    self.make_inputs(revision_rounds=revisions)
                )
                self.assertEqual(result.effective_hours, expected)

    def test_duration_boundaries(self):
        for days, expected in ((1, Decimal("4")), (60, Decimal("240"))):
            with self.subTest(delivery_days=days):
                result = self.assert_matches_reference(
                    self.make_inputs(delivery_days=days)
                )
                self.assertEqual(result.effective_hours, expected)

    def test_known_outputs(self):
        result = calculate_estimate(self.make_inputs())

        self.assertEqual(result.effective_hours, Decimal("20"))
        self.assertEqual(result.minimum, Decimal("1160"))
        self.assertEqual(result.recommended, Decimal("1320"))
        self.assertEqual(result.maximum, Decimal("1480"))
        self.assertEqual(result.hourly_equivalent, Decimal("66"))

    def test_fee_quantization_uses_half_up_at_five_unit_boundaries(self):
        result = calculate_estimate(
            self.make_inputs(
                delivery_days=1,
                complexity="straightforward",
                experience="specialist",
                currency="USD",
            )
        )

        self.assertEqual(result.effective_hours, Decimal("3.6"))
        self.assertEqual(result.recommended, Decimal("475"))
        self.assertEqual(result.minimum, Decimal("415"))
        self.assertEqual(result.maximum, Decimal("530"))

    def test_hourly_quantization_uses_half_up_at_one_unit_boundaries(self):
        result = calculate_estimate(
            self.make_inputs(
                delivery_days=1,
                complexity="standard",
                experience="developing",
                currency="USD",
            )
        )

        self.assertEqual(result.hourly_equivalent, Decimal("39"))

    def test_intermediate_values_are_not_rounded_before_the_final_outputs(self):
        result = calculate_estimate(
            self.make_inputs(
                delivery_days=1,
                complexity="straightforward",
                experience="specialist",
                currency="USD",
            )
        )

        self.assertEqual(result.recommended, Decimal("475"))
        self.assertEqual(result.minimum, Decimal("415"))
        self.assertEqual(result.maximum, Decimal("530"))

    def test_unknown_enum_values_raise_value_error(self):
        invalid_values = (
            {"complexity": "unknown"},
            {"urgency": "unknown"},
            {"experience": "unknown"},
            {"currency": "CAD"},
        )
        for override in invalid_values:
            with self.subTest(override=override), self.assertRaises(ValueError):
                calculate_estimate(self.make_inputs(**override))

    def test_non_positive_effective_hours_raise_value_error(self):
        for overrides in (
            {"delivery_days": 0},
            {"delivery_days": 0, "revision_rounds": 3},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                calculate_estimate(self.make_inputs(**overrides))

    def test_invalid_revision_and_duration_ranges_raise_value_error(self):
        for override in (
            {"delivery_days": -1},
            {"delivery_days": 61},
            {"revision_rounds": -1},
            {"revision_rounds": 11},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                calculate_estimate(self.make_inputs(**override))

    def test_factors_are_human_readable_and_do_not_change_numeric_outputs(self):
        result = calculate_estimate(
            self.make_inputs(
                delivery_days=5,
                complexity="complex",
                revision_rounds=3,
                urgency="priority",
                experience="experienced",
                currency="EUR",
            )
        )
        by_id = {factor["id"]: factor for factor in result.factors}

        self.assertEqual(
            set(by_id),
            {
                "effective_hours",
                "complexity_multiplier",
                "experience_rate",
                "revision_time",
                "risk_buffer",
                "urgency_multiplier",
                "minimum",
                "recommended",
                "maximum",
                "hourly_equivalent",
            },
        )
        self.assertEqual(by_id["complexity_multiplier"]["value"], "1.35")
        self.assertEqual(by_id["experience_rate"]["value"], "80")
        self.assertEqual(by_id["revision_time"]["value"], "+1.5")
        self.assertEqual(by_id["risk_buffer"]["value"], "15%")
        self.assertEqual(by_id["urgency_multiplier"]["value"], "1.18")
        for factor in result.factors:
            self.assertIsInstance(factor["label"], str)
            self.assertIsInstance(factor["value"], str)

    def test_fixture_is_valid_and_matches_server_output(self):
        fixture_path = (
            Path(__file__).resolve().parents[1] / "fixtures" / "pricing_cases.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(fixture["pricing_config_version"], "pricing-v1")
        cases = fixture["cases"]
        self.assertGreaterEqual(len(cases), 10)
        currencies = {case["input"]["currency"] for case in cases if "expected" in case}
        complexities = {
            case["input"]["complexity"] for case in cases if "expected" in case
        }
        urgencies = {case["input"]["urgency"] for case in cases if "expected" in case}
        self.assertEqual(currencies, set(CURRENCIES))
        self.assertEqual(complexities, set(COMPLEXITY_MULTIPLIERS))
        self.assertEqual(urgencies, set(URGENCY_MULTIPLIERS))
        self.assertTrue(any(case["input"]["delivery_days"] == 0 for case in cases))

        for case in cases:
            with self.subTest(case=case["id"]):
                inputs = EstimateInputs(**case["input"])
                if "error" in case["expected"]:
                    with self.assertRaises(ValueError) as error:
                        calculate_estimate(inputs)
                    self.assertEqual(str(error.exception), case["expected"]["error"])
                    self.assertEqual(case["factor_ids"], [])
                    continue
                result = calculate_estimate(inputs)
                for field_name in (
                    "effective_hours",
                    "minimum",
                    "recommended",
                    "maximum",
                    "hourly_equivalent",
                ):
                    self.assertEqual(
                        str(getattr(result, field_name)), case["expected"][field_name]
                    )
                self.assertEqual(
                    [factor["id"] for factor in result.factors],
                    case["factor_ids"],
                )

    def test_precision_is_the_approved_decimal_precision(self):
        self.assertEqual(DECIMAL_PRECISION, Decimal("28"))
