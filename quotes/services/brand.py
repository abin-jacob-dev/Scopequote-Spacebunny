from __future__ import annotations

import base64
import re
from decimal import Decimal

from quotes.services.storage import StoredLogo, inspect_stored_logo

_ACCENT_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalize_accent_color(value: str) -> str:
    if not isinstance(value, str) or not _ACCENT_COLOR.fullmatch(value):
        raise ValueError("Accent color must use #RRGGBB format.")
    return value.upper()


def _channel(value: int) -> Decimal:
    normalized = Decimal(value) / Decimal("255")
    if normalized <= Decimal("0.03928"):
        return normalized / Decimal("12.92")
    return ((normalized + Decimal("0.055")) / Decimal("1.055")) ** Decimal("2.4")


def _luminance(color: str) -> Decimal:
    value = normalize_accent_color(color).lstrip("#")
    channels = [int(value[index : index + 2], 16) for index in (0, 2, 4)]
    red, green, blue = (_channel(channel) for channel in channels)
    return (
        Decimal("0.2126") * red + Decimal("0.7152") * green + Decimal("0.0722") * blue
    )


def contrast_ratio(first: str, second: str) -> Decimal:
    first_luminance = _luminance(first)
    second_luminance = _luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + Decimal("0.05")) / (darker + Decimal("0.05"))


def readable_foreground(accent_color: str) -> str:
    dark = "#20231F"
    light = "#FFFFFF"
    return (
        dark
        if contrast_ratio(accent_color, dark) >= contrast_ratio(accent_color, light)
        else light
    )


def authorized_logo_data_uri(
    *,
    storage_key: str,
    owner_id,
) -> str | None:
    if not storage_key:
        return None
    stored = StoredLogo(
        storage_key=storage_key,
        content_type="",
        byte_size=0,
        owner_id=owner_id,
    )
    inspected = inspect_stored_logo(stored)
    from django.core.files.storage import default_storage

    with default_storage.open(inspected.storage_key, "rb") as source:
        data = source.read()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{inspected.content_type};base64,{encoded}"
