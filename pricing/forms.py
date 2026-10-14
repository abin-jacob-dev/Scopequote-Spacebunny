from typing import Final

from django import forms

from pricing.constants import (
    COMPLEXITIES,
    CURRENCIES,
    EXPERIENCES,
    INCLUDED_REVISION_ROUNDS,
    URGENCIES,
)

COMPLEXITY_CHOICES: Final[tuple[tuple[str, str], ...]] = tuple(
    zip(
        COMPLEXITIES,
        ("Straightforward", "Standard", "Complex", "Advanced"),
        strict=True,
    )
)
URGENCY_CHOICES: Final[tuple[tuple[str, str], ...]] = tuple(
    zip(URGENCIES, ("Standard", "Priority", "Rush"), strict=True)
)
EXPERIENCE_CHOICES: Final[tuple[tuple[str, str], ...]] = tuple(
    zip(
        EXPERIENCES,
        ("Developing", "Intermediate", "Experienced", "Specialist"),
        strict=True,
    )
)
CURRENCY_CHOICES: Final[tuple[tuple[str, str], ...]] = tuple(
    (currency, currency) for currency in CURRENCIES
)


class EstimateForm(forms.Form):
    DURATION_PRESETS: Final[dict[int, str]] = {
        3: "3 days",
        5: "1 week",
        10: "2 weeks",
        20: "4 weeks",
        40: "8 weeks",
    }

    description = forms.CharField(
        label="Project description",
        min_length=30,
        max_length=2000,
        strip=True,
        error_messages={
            "min_length": "Enter at least 30 characters.",
            "max_length": "Enter no more than 2,000 characters.",
        },
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": "Describe the outcome, audience, and key deliverables.",
            }
        ),
        help_text="Use 30–2,000 characters. HTML is treated as plain text.",
    )
    delivery_days = forms.IntegerField(
        label="Expected duration",
        min_value=1,
        max_value=60,
        error_messages={
            "invalid": "Enter a whole number from 1 through 60.",
            "min_value": "Enter a whole number from 1 through 60.",
            "max_value": "Enter a whole number from 1 through 60.",
        },
        widget=forms.NumberInput(
            attrs={"min": 1, "max": 60, "step": 1, "inputmode": "numeric"}
        ),
        help_text="Focused working delivery days, from 1 through 60.",
    )
    complexity = forms.ChoiceField(
        label="Complexity",
        choices=COMPLEXITY_CHOICES,
    )
    revision_rounds = forms.IntegerField(
        label="Revision rounds",
        min_value=0,
        max_value=10,
        error_messages={
            "invalid": "Enter a whole number from 0 through 10.",
            "min_value": "Enter a whole number from 0 through 10.",
            "max_value": "Enter a whole number from 0 through 10.",
        },
        widget=forms.NumberInput(
            attrs={"min": 0, "max": 10, "step": 1, "inputmode": "numeric"}
        ),
        help_text=(
            f"The first {INCLUDED_REVISION_ROUNDS} rounds are included in the base "
            "time allowance."
        ),
    )
    urgency = forms.ChoiceField(label="Urgency", choices=URGENCY_CHOICES)
    experience = forms.ChoiceField(
        label="Experience",
        choices=EXPERIENCE_CHOICES,
    )
    currency = forms.ChoiceField(label="Currency", choices=CURRENCY_CHOICES)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("complexity", "urgency", "experience", "currency"):
            self.fields[field_name].widget.attrs["data-pricing-choice"] = field_name

    def cleaned_input_values(self) -> dict[str, object]:
        return {
            "description": self.cleaned_data["description"],
            "delivery_days": self.cleaned_data["delivery_days"],
            "complexity": self.cleaned_data["complexity"],
            "revision_rounds": self.cleaned_data["revision_rounds"],
            "urgency": self.cleaned_data["urgency"],
            "experience": self.cleaned_data["experience"],
            "currency": self.cleaned_data["currency"],
        }
