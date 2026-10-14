import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
MIXED_CATEGORY: Final[str] = "mixed"
CONFIDENCE_HIGH: Final[str] = "high"
CONFIDENCE_MEDIUM: Final[str] = "medium"


CATEGORY_KEYWORDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "website": (
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
        ),
        "brand": (
            "brand",
            "logo",
            "identity",
            "visual identity",
            "style guide",
            "art direction",
        ),
        "content": (
            "copy",
            "copywriting",
            "article",
            "blog",
            "editorial",
            "content writing",
            "newsletter",
        ),
        "video": (
            "video",
            "motion",
            "animation",
            "reel",
            "footage",
            "editing",
        ),
        "strategy": (
            "strategy",
            "consulting",
            "research",
            "audit",
            "positioning",
            "workshop",
            "advisory",
        ),
    }
)

SCOPE_TEMPLATES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "website": (
            "discovery",
            "structure",
            "responsive design",
            "implementation",
            "testing",
            "handoff",
        ),
        "brand": (
            "discovery",
            "directions",
            "core assets",
            "applications",
            "guidelines",
            "final files",
        ),
        "content": (
            "research",
            "outline",
            "draft",
            "editing",
            "review",
            "delivery",
        ),
        "video": (
            "concept",
            "script or treatment",
            "production",
            "editing",
            "review",
            "delivery",
        ),
        "strategy": (
            "discovery",
            "analysis",
            "recommendations",
            "workshop",
            "written summary",
        ),
        MIXED_CATEGORY: (
            "discovery",
            "project plan",
            "production",
            "review",
            "handoff",
        ),
    }
)

SUGGESTION_TEMPLATES = SCOPE_TEMPLATES
KEYWORDS = CATEGORY_KEYWORDS

_PHRASE_TOKENS: Final[Mapping[tuple[str, str], tuple[str, ...]]] = MappingProxyType(
    {
        (category, phrase): tuple(_TOKEN_PATTERN.findall(phrase))
        for category, phrases in CATEGORY_KEYWORDS.items()
        for phrase in phrases
    }
)


@dataclass(frozen=True)
class ScopeSuggestion:
    category: str
    matched_phrases: list[str]
    confidence: str
    suggested_items: list[str]


@dataclass(frozen=True)
class _PhraseMatch:
    category: str
    phrase: str
    start: int
    end: int
    order: int


def _normalize_description(description: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(description, str):
        raise TypeError("description must be a string")
    normalized = unicodedata.normalize("NFKC", description).lower().replace("-", " ")
    tokens = tuple(_TOKEN_PATTERN.findall(normalized))
    return " ".join(tokens), tokens


def _find_phrase_matches(
    tokens: tuple[str, ...],
) -> dict[str, list[str]]:
    candidates: list[_PhraseMatch] = []
    order = 0
    for category, phrases in CATEGORY_KEYWORDS.items():
        for phrase in phrases:
            phrase_tokens = _PHRASE_TOKENS[(category, phrase)]
            for start in range(len(tokens) - len(phrase_tokens) + 1):
                if tokens[start : start + len(phrase_tokens)] == phrase_tokens:
                    candidates.append(
                        _PhraseMatch(
                            category=category,
                            phrase=phrase,
                            start=start,
                            end=start + len(phrase_tokens),
                            order=order,
                        )
                    )
                    order += 1

    candidates.sort(
        key=lambda match: (-(match.end - match.start), match.start, match.order)
    )
    occupied: list[tuple[int, int]] = []
    selected: list[_PhraseMatch] = []
    for candidate in candidates:
        if any(
            candidate.start < end and candidate.end > start for start, end in occupied
        ):
            continue
        occupied.append((candidate.start, candidate.end))
        selected.append(candidate)

    selected.sort(
        key=lambda match: (match.start, -(match.end - match.start), match.order)
    )
    matches = {category: [] for category in CATEGORY_KEYWORDS}
    for match in selected:
        if match.phrase not in matches[match.category]:
            matches[match.category].append(match.phrase)
    return matches


def _copy_template(category: str) -> list[str]:
    return list(SCOPE_TEMPLATES[category])


def _matched_phrases(
    matches: Mapping[str, list[str]], category: str, is_mixed: bool
) -> list[str]:
    if not is_mixed:
        return list(matches[category])
    return [phrase for name in CATEGORY_KEYWORDS for phrase in matches[name]]


def classify_description(description: str) -> ScopeSuggestion:
    normalized, tokens = _normalize_description(description)
    matches = _find_phrase_matches(tokens)
    scores = {category: len(phrases) for category, phrases in matches.items()}
    top_score = max(scores.values())
    top_categories = [
        category for category, score in scores.items() if score == top_score
    ]
    is_mixed = top_score == 0 or len(top_categories) != 1
    category = MIXED_CATEGORY if is_mixed else top_categories[0]
    matched_phrases = _matched_phrases(matches, category, is_mixed)
    confidence = (
        CONFIDENCE_HIGH
        if not is_mixed and len(normalized) >= 80 and len(matched_phrases) >= 2
        else CONFIDENCE_MEDIUM
    )

    return ScopeSuggestion(
        category=category,
        matched_phrases=matched_phrases,
        confidence=confidence,
        suggested_items=_copy_template(category),
    )
