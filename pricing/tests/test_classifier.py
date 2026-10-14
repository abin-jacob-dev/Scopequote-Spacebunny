from dataclasses import FrozenInstanceError, fields, is_dataclass
from unittest import TestCase

from pricing.classifier import ScopeSuggestion, classify_description
from pricing.engine import calculate_estimate
from pricing.types import EstimateInputs

WEBSITE_ITEMS = [
    "discovery",
    "structure",
    "responsive design",
    "implementation",
    "testing",
    "handoff",
]
BRAND_ITEMS = [
    "discovery",
    "directions",
    "core assets",
    "applications",
    "guidelines",
    "final files",
]
CONTENT_ITEMS = [
    "research",
    "outline",
    "draft",
    "editing",
    "review",
    "delivery",
]
VIDEO_ITEMS = [
    "concept",
    "script or treatment",
    "production",
    "editing",
    "review",
    "delivery",
]
STRATEGY_ITEMS = [
    "discovery",
    "analysis",
    "recommendations",
    "workshop",
    "written summary",
]
MIXED_ITEMS = [
    "discovery",
    "project plan",
    "production",
    "review",
    "handoff",
]


class ScopeSuggestionTests(TestCase):
    def test_suggestion_has_approved_fields_and_is_frozen(self):
        suggestion = classify_description("A website project")

        self.assertTrue(is_dataclass(suggestion))
        self.assertIsInstance(suggestion, ScopeSuggestion)
        self.assertEqual(
            [field.name for field in fields(suggestion)],
            ["category", "matched_phrases", "confidence", "suggested_items"],
        )
        with self.assertRaises(FrozenInstanceError):
            suggestion.category = "changed"


class ClassifierTests(TestCase):
    def test_each_category_selects_its_exact_keyword_template(self):
        cases = {
            "website": (
                "website web site web landing page ecommerce shop cms "
                "responsive frontend ui ux web app",
                WEBSITE_ITEMS,
                {
                    "website",
                    "web site",
                    "web",
                    "landing page",
                    "ecommerce",
                    "shop",
                    "cms",
                    "responsive",
                    "frontend",
                    "ui",
                    "ux",
                    "web app",
                },
            ),
            "brand": (
                "brand logo identity visual identity style guide art direction",
                BRAND_ITEMS,
                {
                    "brand",
                    "logo",
                    "identity",
                    "visual identity",
                    "style guide",
                    "art direction",
                },
            ),
            "content": (
                "copy copywriting article blog editorial content writing newsletter",
                CONTENT_ITEMS,
                {
                    "copy",
                    "copywriting",
                    "article",
                    "blog",
                    "editorial",
                    "content writing",
                    "newsletter",
                },
            ),
            "video": (
                "video motion animation reel footage editing",
                VIDEO_ITEMS,
                {"video", "motion", "animation", "reel", "footage", "editing"},
            ),
            "strategy": (
                "strategy consulting research audit positioning workshop advisory",
                STRATEGY_ITEMS,
                {
                    "strategy",
                    "consulting",
                    "research",
                    "audit",
                    "positioning",
                    "workshop",
                    "advisory",
                },
            ),
        }

        for category, (description, expected_items, expected_phrases) in cases.items():
            with self.subTest(category=category):
                suggestion = classify_description(description)
                self.assertEqual(suggestion.category, category)
                self.assertEqual(suggestion.suggested_items, expected_items)
                self.assertEqual(set(suggestion.matched_phrases), expected_phrases)

    def test_normalization_uses_nfkc_lowercase_hyphens_and_word_tokens(self):
        suggestion = classify_description("  ＷＥＢ-ＳＩＴＥ\nvisual identity  ")

        self.assertEqual(suggestion.category, "mixed")
        self.assertEqual(
            suggestion.matched_phrases,
            ["web site", "visual identity"],
        )

    def test_longest_phrase_match_suppresses_overlapping_token(self):
        suggestion = classify_description("Visual identity")

        self.assertEqual(suggestion.category, "brand")
        self.assertEqual(suggestion.matched_phrases, ["visual identity"])
        self.assertNotIn("identity", suggestion.matched_phrases)

    def test_distinct_non_overlapping_matches_each_score_once(self):
        suggestion = classify_description(
            "website web site landing page web app responsive"
        )

        self.assertEqual(suggestion.category, "website")
        self.assertEqual(
            suggestion.matched_phrases,
            ["website", "web site", "landing page", "web app", "responsive"],
        )

    def test_zero_score_uses_generic_mixed_template(self):
        suggestion = classify_description("A project with a defined handoff")

        self.assertEqual(suggestion.category, "mixed")
        self.assertEqual(suggestion.matched_phrases, [])
        self.assertEqual(suggestion.suggested_items, MIXED_ITEMS)
        self.assertEqual(suggestion.confidence, "medium")

    def test_top_score_tie_uses_mixed_template(self):
        suggestion = classify_description("website and brand")

        self.assertEqual(suggestion.category, "mixed")
        self.assertEqual(
            suggestion.matched_phrases,
            ["website", "brand"],
        )
        self.assertEqual(suggestion.suggested_items, MIXED_ITEMS)
        self.assertEqual(suggestion.confidence, "medium")

    def test_clear_winner_is_returned_with_its_matches(self):
        suggestion = classify_description("website brand logo")

        self.assertEqual(suggestion.category, "brand")
        self.assertEqual(suggestion.matched_phrases, ["brand", "logo"])
        self.assertEqual(suggestion.suggested_items, BRAND_ITEMS)
        self.assertEqual(suggestion.confidence, "medium")

    def test_confidence_boundary_uses_normalized_character_count(self):
        exact_80 = "website landing page" + " x" * 30
        just_under_80 = "website landing page" + " x" * 29

        self.assertEqual(len(exact_80), 80)
        self.assertEqual(len(just_under_80), 78)
        self.assertEqual(classify_description(exact_80).confidence, "high")
        self.assertEqual(classify_description(just_under_80).confidence, "medium")

    def test_high_confidence_requires_two_matches_and_clear_winner(self):
        one_match = "website" + "x" * 73
        tied = "website brand" + "x" * 67

        self.assertEqual(len(one_match), 80)
        self.assertEqual(len(tied), 80)
        self.assertEqual(classify_description(one_match).confidence, "medium")
        self.assertEqual(classify_description(tied).confidence, "medium")

    def test_suggestion_lists_are_copies_between_calls(self):
        first = classify_description("website")
        second = classify_description("website")

        first.matched_phrases.append("changed")
        first.suggested_items.append("changed")

        self.assertEqual(second.matched_phrases, ["website"])
        self.assertEqual(second.suggested_items, WEBSITE_ITEMS)

    def test_classification_does_not_change_pricing_outputs(self):
        website_inputs = EstimateInputs(
            description="website and landing page",
            delivery_days=5,
            complexity="standard",
            revision_rounds=2,
            urgency="standard",
            experience="intermediate",
            currency="USD",
        )
        brand_inputs = EstimateInputs(
            description="brand and logo",
            delivery_days=5,
            complexity="standard",
            revision_rounds=2,
            urgency="standard",
            experience="intermediate",
            currency="USD",
        )

        classify_description(website_inputs.description)
        classify_description(brand_inputs.description)

        website_estimate = calculate_estimate(website_inputs)
        brand_estimate = calculate_estimate(brand_inputs)
        self.assertEqual(website_estimate, brand_estimate)

    def test_repeated_phrase_is_normalized_to_one_match(self):
        suggestion = classify_description("website website landing page")

        self.assertEqual(suggestion.category, "website")
        self.assertEqual(suggestion.matched_phrases, ["website", "landing page"])
        self.assertEqual(suggestion.confidence, "medium")

    def test_repeated_classification_is_deterministic(self):
        description = "A responsive web app with visual identity and research"

        self.assertEqual(
            classify_description(description),
            classify_description(description),
        )
