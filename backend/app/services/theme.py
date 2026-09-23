"""Theme schema (Appendix C, GEN-6) and validation."""

from __future__ import annotations

import io
from collections import Counter
from pathlib import Path
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


# ------------------------------------------------------- theme from a resume


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def match_render_font(name: str | None) -> str:
    """Map an arbitrary DOCX font to the closest font the renderer ships (GEN-4)."""
    if not name:
        return str(core.DEFAULTS["font"])
    needle = str(name).strip().lower()
    for entry in RENDER_FONTS:
        if entry["name"].lower() == needle:
            return entry["name"]
    for entry in RENDER_FONTS:
        if entry["pdf_substitute"].lower() == needle:
            return entry["name"]
    return str(core.DEFAULTS["font"])


def _is_accentish(rgb_hex: str) -> bool:
    """Reject black/white/near-greys so we only pick a real accent colour."""
    try:
        red, green, blue = (int(rgb_hex[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return False
    spread = max(red, green, blue) - min(red, green, blue)
    return spread >= 24 and 24 <= (red + green + blue) / 3 <= 225


def theme_from_docx(payload: bytes, *, filename: str = "resume.docx") -> dict[str, Any]:
    """SET-11: derive theme params from an existing resume document.

    Only style-level values are read (fonts, sizes, accent colour, margins and
    spacing) — no document text leaves the request. Returns ``params`` ready to
    pre-fill the theme editor plus a ``source`` block for the UI to explain the
    guess.
    """
    from docx import Document

    try:
        document = Document(io.BytesIO(payload))
    except Exception as exc:  # noqa: BLE001 - python-docx raises a mixed bag
        raise ThemeValidationError(f"Could not read that document: {exc}") from exc

    fonts: Counter[str] = Counter()
    sizes: Counter[float] = Counter()
    colours: list[str] = []
    paragraphs = list(document.paragraphs)[:400]
    for paragraph in paragraphs:
        for run in paragraph.runs:
            if run.font.name:
                fonts[str(run.font.name)] += len(run.text or " ") or 1
            if run.font.size is not None:
                sizes[round(float(run.font.size.pt) * 2) / 2] += len(run.text or " ") or 1
            if run.font.color is not None and run.font.color.rgb is not None:
                colours.append(str(run.font.color.rgb).upper())

    normal_font = None
    try:
        normal_font = document.styles["Normal"].font
    except KeyError:  # pragma: no cover - every real template has Normal
        normal_font = None
    source_font = fonts.most_common(1)[0][0] if fonts else (normal_font.name if normal_font else None)
    font = match_render_font(source_font)
    size = sizes.most_common(1)[0][0] if sizes else float(core.DEFAULTS["size"])

    accent = next((value for value in colours if _is_accentish(value)), "")
    if not accent:
        accent = str(core.DEFAULTS["accent"])

    margin_top = margin_bottom = margin_left = margin_right = None
    if document.sections:
        section = document.sections[0]
        margins = (
            section.top_margin,
            section.bottom_margin,
            section.left_margin,
            section.right_margin,
        )
        if all(value is not None for value in margins):
            margin_top, margin_bottom, margin_left, margin_right = (
                _clamp(float(value.inches), 0.3, 1.5) for value in margins
            )

    line_height = core.DEFAULTS["line_height"]
    section_gap = float(core.DEFAULTS["section_gap"])
    for paragraph in paragraphs:
        spacing = paragraph.paragraph_format.line_spacing
        if isinstance(spacing, float) and 1.0 <= spacing <= 2.5:
            line_height = spacing
            break
    gaps = [
        float(paragraph.paragraph_format.space_after.pt)
        for paragraph in paragraphs
        if paragraph.paragraph_format.space_after is not None
        and 0 <= float(paragraph.paragraph_format.space_after.pt) <= 40
    ]
    if gaps:
        gaps.sort()
        section_gap = round(gaps[len(gaps) // 2], 1)

    params = validate_theme_params(
        {
            "font": font,
            "size": _clamp(float(size), 8.0, 14.0),
            "accent": accent,
            "bg_color": core.DEFAULTS["bg_color"],
            "line_height": line_height,
            "section_gap": section_gap,
            "margin_top": margin_top if margin_top is not None else core.DEFAULTS["margin_top"],
            "margin_bottom": margin_bottom if margin_bottom is not None else core.DEFAULTS["margin_bottom"],
            "margin_left": margin_left if margin_left is not None else core.DEFAULTS["margin_left"],
            "margin_right": margin_right if margin_right is not None else core.DEFAULTS["margin_right"],
        }
    )
    warnings: list[str] = []
    if source_font and font.lower() != str(source_font).strip().lower():
        warnings.append(f"“{source_font}” is not installed in the renderer; using {font}.")
    if not sizes:
        warnings.append("No explicit font size found; kept the default.")
    if not accent or accent == str(core.DEFAULTS["accent"]).upper():
        warnings.append("No accent colour found; kept the default.")
    stem = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or "Imported resume"
    return {
        "name": f"{stem[:60]} theme",
        "description": f"Theme derived from {Path(filename).name}.",
        "params": params,
        "source": {
            "font": source_font,
            "size": float(size),
            "accent": accent,
        },
        "warnings": warnings,
    }
