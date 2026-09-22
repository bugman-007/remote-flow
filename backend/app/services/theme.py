"""Theme schema (Appendix C, GEN-6) and validation."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Theme
from vendor.resume_builder import core

DEFAULT_THEME_NAME = "Default"

#: name -> (type, min, max, step) mirroring the desktop GUI's spinboxes.
THEME_FIELDS: dict[str, tuple[str, float | None, float | None, float | None]] = {
    "font": ("font", None, None, None),
    "size": ("number", 8.0, 14.0, 0.5),
    "accent": ("color", None, None, None),
    "bg_color": ("color", None, None, None),
    "line_height": ("number", 1.0, 2.5, 0.05),
    "section_gap": ("number", 0.0, 40.0, 1.0),
    "margin_top": ("number", 0.3, 1.5, 0.05),
    "margin_bottom": ("number", 0.3, 1.5, 0.05),
    "margin_left": ("number", 0.3, 1.5, 0.05),
    "margin_right": ("number", 0.3, 1.5, 0.05),
}

#: Fonts installed in the render image (GEN-4). The DOCX keeps these names;
#: LibreOffice substitutes the metric-compatible family for the PDF.
RENDER_FONTS: list[dict[str, str]] = [
    {"name": "Calibri", "pdf_substitute": "Carlito"},
    {"name": "Cambria", "pdf_substitute": "Caladea"},
    {"name": "Arial", "pdf_substitute": "Liberation Sans"},
    {"name": "Helvetica", "pdf_substitute": "Liberation Sans"},
    {"name": "Times New Roman", "pdf_substitute": "Liberation Serif"},
    {"name": "Courier New", "pdf_substitute": "Liberation Mono"},
    {"name": "Georgia", "pdf_substitute": "Gelasio"},
    {"name": "Segoe UI", "pdf_substitute": "Noto Sans"},
    {"name": "Tahoma", "pdf_substitute": "Noto Sans"},
    {"name": "Verdana", "pdf_substitute": "DejaVu Sans"},
    {"name": "DejaVu Sans", "pdf_substitute": "DejaVu Sans"},
    {"name": "Noto Sans", "pdf_substitute": "Noto Sans"},
]


class ThemeValidationError(ValueError):
    pass


def validate_theme_params(params: dict[str, Any]) -> dict[str, Any]:
    """Validate against Appendix C and return the canonical, snapshot-ready object."""
    if not isinstance(params, dict):
        raise ThemeValidationError("theme params must be an object")
    unknown = set(params) - set(THEME_FIELDS)
    if unknown:
        raise ThemeValidationError(f"unknown theme keys: {', '.join(sorted(unknown))}")
    out = dict(core.DEFAULTS)
    for key, value in params.items():
        kind, minimum, maximum, _step = THEME_FIELDS[key]
        if value is None:
            continue
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ThemeValidationError(f"{key} must be a number")
            number = float(value)
            if minimum is not None and number < minimum:
                raise ThemeValidationError(f"{key} must be >= {minimum}")
            if maximum is not None and number > maximum:
                raise ThemeValidationError(f"{key} must be <= {maximum}")
            out[key] = number if key != "size" and key != "section_gap" else round(number, 2)
        elif kind == "color":
            if not isinstance(value, str):
                raise ThemeValidationError(f"{key} must be a colour string")
            normalised = core.color_hex(value, fallback="")
            if not normalised:
                raise ThemeValidationError(f"{key} must be #RRGGBB")
            out[key] = f"#{normalised}"
        else:
            if not isinstance(value, str) or not value.strip():
                raise ThemeValidationError(f"{key} must be a non-empty string")
            out[key] = value.strip()
    out["size"] = float(out["size"])
    out["section_gap"] = float(out["section_gap"])
    return out


def theme_form_spec() -> list[dict[str, Any]]:
    """Field metadata for `GET /themes/schema`, used to render the Settings form."""
    return [
        {
            "key": key,
            "type": kind,
            "min": minimum,
            "max": maximum,
            "step": step,
            "default": core.DEFAULTS[key],
            "options": [entry["name"] for entry in RENDER_FONTS] if kind == "font" else None,
            "font_labels": RENDER_FONTS if kind == "font" else None,
        }
        for key, (kind, minimum, maximum, step) in THEME_FIELDS.items()
    ]


async def seed_default_theme(session: AsyncSession) -> Theme:
    existing = (await session.execute(select(Theme).where(Theme.name == DEFAULT_THEME_NAME))).scalar_one_or_none()
    if existing:
        return existing
    theme = Theme(
        name=DEFAULT_THEME_NAME,
        description="Generator defaults (Appendix C).",
        params=dict(core.DEFAULTS),
    )
    session.add(theme)
    await session.flush()
    return theme
