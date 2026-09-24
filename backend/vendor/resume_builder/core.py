"""Pure document model + DOCX builder (GEN-1, GEN-5).

Deterministic, process-safe and free of global state: everything is a function
of its arguments, the only file written is ``out_path``. No ``style.json`` and
no import-time side effects.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Mm, Pt, RGBColor

# --------------------------------------------------------------------- theme

#: Appendix C — the themeable keys of ``style.json`` and their defaults.
DEFAULTS: dict[str, Any] = {
    "font": "Calibri",
    "size": 10.5,
    "accent": "#1F4E79",
    "bg_color": "#FFFFFF",
    "line_height": 1.0,
    "section_gap": 9,
    "margin_top": 0.7,
    "margin_bottom": 0.7,
    "margin_left": 0.75,
    "margin_right": 0.75,
    # --- Appendix C.2 (theme schema v2): element-scoped styling -------------
    "body_color": "#262626",
    "muted_color": "#595959",
    "bullet_glyph": "\u2022",
    "page_size": "Letter",
    #: kind -> subset of ELEMENT_FIELDS; only the keys present are overridden.
    "elements": {},
    #: [{"text": "...", "bold": true, "color": "#RRGGBB", ...}] applied to any
    #: run whose text contains ``text`` (case-insensitive). This is what the
    #: editor's "select text in the preview and restyle it" writes.
    "text_rules": [],
}

#: Appendix C — point offsets applied to ``size`` per block kind.
SIZE_OFFSETS: dict[str, float] = {
    "name": 11.0,
    "title": 2.5,
    "section": 1.0,
    "job": 0.5,
    "contact": -0.5,
    "meta": -0.5,
}

#: Fixed text colours (Appendix C.2 lets a theme override them).
BODY_COLOR = "262626"
MUTED_COLOR = "595959"

GOOGLE_COLORS = ("#4285F4", "#EA4335", "#FBBC05", "#4285F4", "#34A853", "#EA4335")

#: Paragraph spacing per block kind: (space_before_pt, space_after_pt).
KIND_PROPS: dict[str, tuple[float, float]] = {
    "name": (0.0, 1.0),
    "title": (0.0, 2.0),
    "contact": (0.0, 1.0),
    "section": (0.0, 1.5),
    "job": (3.0, 0.0),
    "meta": (0.0, 1.0),
    "bullet": (0.0, 1.0),
    "body": (1.0, 2.0),
    "skills": (0.0, 1.0),
}


#: Appendix C.2 — the block kinds a Manager can style on their own.
ELEMENT_KINDS: tuple[str, ...] = (
    "name",
    "title",
    "contact",
    "section",
    "job",
    "meta",
    "bullet",
    "skills",
    "body",
)

#: Element labels used by the editor; the key stays stable for stored themes.
ELEMENT_LABELS: dict[str, str] = {
    "name": "Name",
    "title": "Headline",
    "contact": "Contact line",
    "section": "Section heading",
    "job": "Job / entry line",
    "meta": "Dates & meta",
    "bullet": "Bullets",
    "skills": "Skill rows",
    "body": "Paragraphs",
}

ALIGN_CHOICES: tuple[str, ...] = ("left", "center", "right", "justify")

#: Per-element override key -> value type (drives the API validator and the form).
ELEMENT_FIELDS: dict[str, str] = {
    "font": "font",
    "size": "number",
    "size_delta": "number",
    "weight": "number",
    "bold": "bool",
    "italic": "bool",
    "underline": "bool",
    "uppercase": "bool",
    "align": "align",
    "color": "color",
    "bg": "color",
    "letter_spacing": "number",
    "width_scale": "number",
    "line_height": "number",
    "space_before": "number",
    "space_after": "number",
    "indent_left": "number",
    "indent_right": "number",
    "first_line_indent": "number",
    "rule": "bool",
    "rule_width": "number",
    "hidden": "bool",
}

#: (min, max, step) per numeric element key; ``None`` = no bound.
ELEMENT_BOUNDS: dict[str, tuple[float | None, float | None, float | None]] = {
    "size": (5.0, 48.0, 0.5),
    "size_delta": (-6.0, 26.0, 0.5),
    "weight": (100.0, 900.0, 100.0),
    "letter_spacing": (-2.0, 8.0, 0.1),
    "width_scale": (50.0, 200.0, 5.0),
    "line_height": (0.8, 3.0, 0.05),
    "space_before": (0.0, 60.0, 1.0),
    "space_after": (0.0, 60.0, 1.0),
    "indent_left": (0.0, 3.0, 0.05),
    "indent_right": (0.0, 3.0, 0.05),
    "first_line_indent": (-1.0, 1.0, 0.05),
    "rule_width": (0.0, 24.0, 1.0),
}

#: Appendix C.2 — the generator's built-in style per kind, before overrides.
ELEMENT_BASE: dict[str, dict[str, Any]] = {
    "name": {"size_delta": SIZE_OFFSETS["name"], "tone": "accent", "bold": True},
    "title": {"size_delta": SIZE_OFFSETS["title"], "tone": "accent", "bold": True},
    "contact": {"size_delta": SIZE_OFFSETS["contact"], "tone": "muted", "bold": False},
    "section": {
        "size_delta": SIZE_OFFSETS["section"],
        "tone": "accent",
        "bold": True,
        "rule": True,
        "rule_width": 6,
    },
    "job": {"size_delta": SIZE_OFFSETS["job"], "tone": "body", "bold": True},
    "meta": {"size_delta": SIZE_OFFSETS["meta"], "tone": "muted", "bold": False},
    "bullet": {
        "size_delta": 0.0,
        "tone": "body",
        "bold": False,
        "indent_left": 0.18,
        "first_line_indent": -0.18,
        "glyph": True,
    },
    "skills": {"size_delta": 0.0, "tone": "body", "bold": False},
    "body": {"size_delta": 0.0, "tone": "body", "bold": False, "align": "justify"},
}

#: Keys a text rule may carry; same value types as the element fields.
TEXT_RULE_FIELDS: dict[str, str] = {
    "text": "text",
    "bold": "bool",
    "weight": "number",
    "italic": "bool",
    "underline": "bool",
    "uppercase": "bool",
    "color": "color",
    "bg": "color",
}

#: Editor presets: partial param sets merged over DEFAULTS by the API.
THEME_PRESETS: list[dict[str, Any]] = [
    {
        "key": "classic",
        "name": "Classic (current default)",
        "params": {},
    },
    {
        "key": "modern",
        "name": "Modern — centered header, roomy sections",
        "params": {
            "font": "Calibri",
            "size": 10.5,
            "accent": "#0F766E",
            "body_color": "#1F2937",
            "muted_color": "#6B7280",
            "line_height": 1.15,
            "section_gap": 13,
            "margin_top": 0.6,
            "margin_bottom": 0.6,
            "margin_left": 0.7,
            "margin_right": 0.7,
            "elements": {
                "name": {"align": "center", "size": 24, "letter_spacing": 0.4, "weight": 700},
                "title": {"align": "center", "size": 12, "uppercase": True, "letter_spacing": 1.2, "weight": 600},
                "contact": {"align": "center", "size": 9.5},
                "section": {"uppercase": True, "letter_spacing": 0.8, "space_before": 14, "space_after": 4},
                "job": {"weight": 700, "space_before": 6},
                "bullet": {"indent_left": 0.22, "first_line_indent": -0.22, "space_after": 2},
            },
        },
    },
    {
        "key": "compact",
        "name": "Compact — one page, tighter spacing",
        "params": {
            "font": "Arial",
            "size": 9.5,
            "accent": "#1F4E79",
            "line_height": 1.0,
            "section_gap": 6,
            "margin_top": 0.45,
            "margin_bottom": 0.45,
            "margin_left": 0.55,
            "margin_right": 0.55,
            "elements": {
                "name": {"size": 19},
                "title": {"size": 11},
                "section": {"space_before": 6, "space_after": 2},
                "job": {"space_before": 2, "space_after": 0},
                "bullet": {"space_after": 0.5, "indent_left": 0.16, "first_line_indent": -0.16},
                "meta": {"space_after": 0},
            },
        },
    },
    {
        "key": "elegant",
        "name": "Elegant — serif, wide margins",
        "params": {
            "font": "Georgia",
            "size": 10.5,
            "accent": "#7C2D12",
            "body_color": "#2D2A26",
            "muted_color": "#78716C",
            "line_height": 1.2,
            "section_gap": 15,
            "margin_top": 0.8,
            "margin_bottom": 0.8,
            "margin_left": 0.9,
            "margin_right": 0.9,
            "elements": {
                "name": {"size": 23, "letter_spacing": 0.6, "align": "center"},
                "title": {"size": 11.5, "italic": True, "align": "center"},
                "contact": {"align": "center", "size": 9},
                "section": {"uppercase": True, "letter_spacing": 1.4, "rule_width": 3},
                "bullet": {"indent_left": 0.2, "first_line_indent": -0.2},
            },
        },
    },
]

_HIGHLIGHT_SPAN = re.compile(r"\[h(?:i|)gh?light\](.*?)\[/h(?:i|)gh?light\]", re.IGNORECASE | re.DOTALL)


def color_hex(value: str | None, fallback: str = BODY_COLOR) -> str:
    """Normalise ``#RRGGBB``/``RRGGBB`` to six uppercase hex digits."""
    if not value:
        return fallback
    raw = str(value).strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return raw.upper()
    if re.fullmatch(r"[0-9a-fA-F]{3}", raw):
        return "".join(char * 2 for char in raw).upper()
    return fallback


def dashify(value: str | None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text.strip())
    return re.sub(r"-{2,}", "-", text).strip("-")


# ------------------------------------------------------------------- data in


def _clean_data(data: Any) -> dict:
    """Tolerant top-level DataFrame-ish cleanup the original tool performed."""
    if not isinstance(data, dict):
        return {}
    cleaned = dict(data)
    for key in ("experience", "education"):
        value = cleaned.get(key)
        if isinstance(value, str):
            cleaned[key] = [{"title": value}] if key == "experience" else [{"degree": value}]
    cleaned["experience"] = [item for item in (cleaned.get("experience") or []) if isinstance(item, dict)]
    cleaned["education"] = [item for item in (cleaned.get("education") or []) if isinstance(item, dict)]
    for key in ("technicalskills", "additional_information"):
        if isinstance(cleaned.get(key), list):
            merged: dict[str, Any] = {}
            for entry in cleaned[key]:
                if isinstance(entry, dict) and entry.get("category"):
                    items = entry.get("items")
                    merged[str(entry["category"])] = (
                        ", ".join(str(i) for i in items) if isinstance(items, list) else (items or "")
                    )
            cleaned[key] = merged
    for item in cleaned["experience"]:
        bullets = item.get("bullets")
        if isinstance(bullets, str):
            item["bullets"] = [bullets]
        elif isinstance(bullets, list):
            item["bullets"] = [str(bullet) for bullet in bullets if str(bullet).strip()]
        else:
            item["bullets"] = []
    if not isinstance(cleaned.get("target_company"), str):
        for alias in ("company_name", "company"):
            if isinstance(cleaned.get(alias), str):
                cleaned["target_company"] = cleaned[alias]
                break
    return cleaned


def make_names(data: dict) -> tuple[str, str]:
    """Return ``(candidate_name, docx_basename)``.

    ``docx_basename`` follows the desktop tool: ``Luis-Angel-Salazar_Senior-Full-Stack-Developer``.
    """
    cleaned = _clean_data(data)
    name = str(cleaned.get("name") or "").strip()
    title = str(cleaned.get("title") or "").strip()
    if not title:
        subtitle = str(cleaned.get("subtitle") or "")
        title = re.split(r"[·|]", subtitle)[0].strip()
    name_part = dashify(name) or dashify(str(cleaned.get("target_company") or "")) or "Resume"
    title_part = dashify(title) or "Resume"
    return name, f"{name_part}_{title_part}"


def compose_job_info(data: dict) -> str:
    """Build ``job_description.txt`` (LLM-6 guarantees the JD is never empty)."""
    cleaned = _clean_data(data)
    parts: list[str] = []
    description = str(cleaned.get("job_description") or "").strip()
    if description:
        parts.append(description)
    company = cleaned.get("company_information")
    if isinstance(company, dict):
        for label, key in (
            ("Public facts", "public_facts"),
            ("Safe inferences", "safe_inferences"),
            ("Research limitations", "research_limitations"),
        ):
            value = str(company.get(key) or "").strip()
            if value:
                parts.append(f"{label}:\n{value}")
    link = str(cleaned.get("job_link") or "").strip()
    if link:
        parts.append(f"Job link: {link}")
    return "\n\n".join(parts)


# ----------------------------------------------------------------- highlights


@dataclass
class Run:
    """A styled text fragment; the optional flags come from text rules."""

    text: str
    bold: bool = False
    color: str | None = None
    italic: bool | None = None
    underline: bool | None = None
    uppercase: bool | None = None
    bg: str | None = None


def parse_highlights(text: str) -> list[Run]:
    """Parse ``[highlight]…[/highlight]`` plus the legacy suffix/markdown forms.

    Tolerated on input, never produced by the server prompt (Appendix B rule 2/3):
    ``term[highlight]``, the ``hightlight`` typo, ``**bold**`` and ``[text](url)``.
    """
    if not text:
        return []
    text = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", text)
    runs: list[Run] = []
    position = 0
    for match in _HIGHLIGHT_SPAN.finditer(text):
        if match.start() > position:
            runs.extend(_plain_runs(text[position : match.start()]))
        runs.append(Run(match.group(1), bold=True))
        position = match.end()
    if position < len(text):
        runs.extend(_plain_runs(text[position:]))
    return [run for run in runs if run.text]


def _plain_runs(text: str) -> list[Run]:
    out: list[Run] = []
    position = 0
    for match in re.finditer(r"\*\*(.+?)\*\*|(\S+)\[hight?light\]", text):
        if match.start() > position:
            out.append(Run(text[position : match.start()]))
        if match.group(1) is not None:
            out.append(Run(match.group(1), bold=True))
        else:
            out.append(Run(match.group(2), bold=True))
        position = match.end()
    if position < len(text):
        out.append(Run(text[position:]))
    return out


# ------------------------------------------------------- resolved element style


def _palette(options: dict) -> dict[str, str]:
    return {
        "accent": color_hex(options.get("accent"), "1F4E79"),
        "body": color_hex(options.get("body_color"), BODY_COLOR),
        "muted": color_hex(options.get("muted_color"), MUTED_COLOR),
    }


def resolve_element(opts: dict | None, kind: str) -> dict[str, Any]:
    """Appendix C.2 — the generator default for ``kind`` with the theme applied.

    Pure function of its arguments: the API validator, the DOCX writer and the
    browser preview all agree because they all start from this object.
    """
    options = {**DEFAULTS, **(opts or {})}
    base_size = float(options.get("size") or DEFAULTS["size"])
    palette = _palette(options)
    base = ELEMENT_BASE.get(kind, ELEMENT_BASE["body"])
    overrides = options.get("elements") or {}
    override = dict(overrides.get(kind) or {}) if isinstance(overrides, dict) else {}

    space_before, space_after = KIND_PROPS.get(kind, (0.0, 1.0))
    if kind == "section":
        space_before += float(options.get("section_gap") or 0.0)

    default_weight = 700.0 if base.get("bold") else 400.0
    resolved: dict[str, Any] = {
        "kind": kind,
        "font": str(options.get("font") or DEFAULTS["font"]),
        "size": base_size + float(base.get("size_delta", 0.0)),
        "weight": default_weight,
        "bold": bool(base.get("bold")),
        "italic": False,
        "underline": False,
        "uppercase": False,
        "align": base.get("align"),
        "color": palette[str(base.get("tone", "body"))],
        "color_set": False,
        "bg": None,
        "letter_spacing": 0.0,
        "width_scale": 100.0,
        "line_height": float(options.get("line_height") or DEFAULTS["line_height"]),
        "space_before": float(space_before),
        "space_after": float(space_after),
        "indent_left": base.get("indent_left"),
        "indent_right": None,
        "first_line_indent": base.get("first_line_indent"),
        "rule": bool(base.get("rule", False)),
        "rule_width": float(base.get("rule_width", 6)),
        "hidden": False,
        "accent": palette["accent"],
        "muted": palette["muted"],
    }

    if override.get("bold") is not None:
        resolved["weight"] = 700.0 if override["bold"] else 400.0
    if override.get("weight") is not None:
        resolved["weight"] = float(override["weight"])
    resolved["bold"] = float(resolved["weight"]) >= 600

    for key, value in override.items():
        if value is None or key not in ELEMENT_FIELDS or key in {"bold", "weight"}:
            continue
        if key == "size":
            resolved["size"] = float(value)
        elif key == "size_delta":
            resolved["size"] = base_size + float(base.get("size_delta", 0.0)) + float(value)
        elif key == "color":
            resolved["color"] = f"#{color_hex(value, palette['body'])}"
            resolved["color_set"] = True
        elif key == "bg":
            resolved["bg"] = f"#{color_hex(value, 'FFFFFF')}"
        elif key == "align":
            if str(value) in ALIGN_CHOICES:
                resolved["align"] = str(value)
        elif key in {"italic", "underline", "uppercase", "rule", "hidden"}:
            resolved[key] = bool(value)
        else:
            resolved[key] = float(value)
    return resolved


def element_form_spec() -> list[dict[str, Any]]:
    """Per-element field metadata for the editor (mirrors the validator)."""
    return [
        {
            "key": kind,
            "label": ELEMENT_LABELS.get(kind, kind),
            "fields": [
                {
                    "key": key,
                    "type": field_type,
                    "min": ELEMENT_BOUNDS.get(key, (None, None, None))[0],
                    "max": ELEMENT_BOUNDS.get(key, (None, None, None))[1],
                    "step": ELEMENT_BOUNDS.get(key, (None, None, None))[2],
                }
                for key, field_type in ELEMENT_FIELDS.items()
            ],
            "aligns": list(ALIGN_CHOICES),
        }
        for kind in ELEMENT_KINDS
    ]


# ----------------------------------------------------------------- text rules


def _rule_modifiers(run: Run, rules: list[dict]) -> Run:
    for rule in rules:
        if rule.get("weight") is not None:
            run.bold = float(rule["weight"]) >= 600
        if rule.get("bold") is not None:
            run.bold = bool(rule["bold"])
        if rule.get("italic") is not None:
            run.italic = bool(rule["italic"])
        if rule.get("underline") is not None:
            run.underline = bool(rule["underline"])
        if rule.get("uppercase") is not None:
            run.uppercase = bool(rule["uppercase"])
        if rule.get("color"):
            run.color = str(rule["color"])
        if rule.get("bg"):
            run.bg = str(rule["bg"])
    return run


def apply_text_rules(runs: list[Run], rules: list[dict] | None) -> list[Run]:
    """Split runs so every occurrence of a rule's text carries the rule's style.

    Rules match case-insensitively on the exact substring, so a Manager can
    select a phrase in the preview and have the same phrase styled in every
    resume generated from this theme — independent of the model's output.
    """
    active_rules = [
        rule for rule in (rules or []) if isinstance(rule, dict) and str(rule.get("text") or "").strip()
    ]
    if not active_rules:
        return list(runs)

    out: list[Run] = []
    for run in runs:
        text = run.text
        lowered = text.lower()
        spans: list[tuple[int, int, dict]] = []
        for rule in active_rules:
            needle = str(rule["text"]).strip().lower()
            start = 0
            while needle:
                index = lowered.find(needle, start)
                if index < 0:
                    break
                spans.append((index, index + len(needle), rule))
                start = index + len(needle)
        if not spans:
            out.append(run)
            continue
        boundaries = sorted({0, len(text)} | {edge for start, end, _ in spans for edge in (start, end)})
        for left, right in zip(boundaries, boundaries[1:]):
            if left >= right:
                continue
            covering = [rule for start, end, rule in spans if start <= left and right <= end]
            fragment = Run(
                text[left:right],
                bold=run.bold,
                color=run.color,
                italic=run.italic,
                underline=run.underline,
                uppercase=run.uppercase,
                bg=run.bg,
            )
            out.append(_rule_modifiers(fragment, covering) if covering else fragment)
    return out


def highlight_runs_to_dicts(runs: list[Run]) -> list[dict[str, Any]]:
    """Serialise runs for ``GET /themes/sample`` (the browser preview)."""
    return [
        {
            "text": run.text,
            "bold": bool(run.bold),
            "color": run.color,
            "italic": run.italic,
            "underline": run.underline,
            "uppercase": run.uppercase,
            "bg": run.bg,
        }
        for run in runs
    ]


def blocks_to_dicts(blocks: list[Block]) -> list[dict[str, Any]]:
    """Serialise the block model so the SPA can render the live preview."""
    return [
        {
            "kind": block.kind,
            "label": block.label,
            "align_right": block.align_right,
            "runs": highlight_runs_to_dicts(block.runs),
            "text": "".join(run.text for run in block.runs),
        }
        for block in blocks
    ]


# ---------------------------------------------------------------- block model


@dataclass
class Block:
    kind: str
    runs: list[Run] = field(default_factory=list)
    label: str | None = None       # skills category, section case-insensitive label
    align_right: str | None = None  # right-aligned tab content (dates, location)
    align: str | None = None


def _block(kind: str, text: str = "", **kwargs: Any) -> Block:
    return Block(kind=kind, runs=parse_highlights(text), **kwargs)


#: Values the model emits for "unknown" that must not be printed.
CONTACT_PLACEHOLDERS = frozenset({"none", "null", "n/a", "na", "not specified", "unspecified", "unknown", "-", "--"})


def _contact_value(value: Any) -> str:
    """Header value, or ``""`` when the model left it out or answered with a placeholder."""
    text = str(value or "").strip()
    return "" if text.lower() in CONTACT_PLACEHOLDERS else text


def build_blocks(data: dict) -> list[Block]:
    """Turn resume JSON into the ordered block list that ``build_docx`` renders."""
    cleaned = _clean_data(data)
    blocks: list[Block] = []

    name = str(cleaned.get("name") or "").strip()
    if name:
        blocks.append(_block("name", name))
    title = str(cleaned.get("title") or "").strip()
    subtitle = str(cleaned.get("subtitle") or "").strip()
    if title:
        blocks.append(_block("title", title))
    elif subtitle:
        blocks.append(_block("title", subtitle))

    # A field the model left out (or filled with a placeholder) must never reach
    # the header - it used to print as the literal string "None".
    contact = [
        _contact_value(cleaned.get(key))
        for key in ("email", "phone", "location", "citizenship", "work_authorization")
    ]
    contact = [item for item in contact if item]
    linkedin = str(cleaned.get("linkedin") or "").strip()
    if linkedin:
        contact.append(linkedin)
    if contact:
        blocks.append(_block("contact", "  ·  ".join(contact)))

    summary = str(cleaned.get("summary") or "").strip()
    if summary:
        blocks.append(_block("section", "Summary"))
        blocks.append(_block("body", summary))

    skills = cleaned.get("technicalskills") or {}
    if isinstance(skills, dict) and skills:
        blocks.append(_block("section", "Technical Skills"))
        for category, items in skills.items():
            blocks.append(Block(kind="skills", label=str(category), runs=parse_highlights(str(items or ""))))

    experience = cleaned.get("experience") or []
    if experience:
        blocks.append(_block("section", "Experience"))
        for job in experience:
            company = str(job.get("company") or "").strip()
            role = str(job.get("title") or "").strip()
            header = f"{company} — {role}" if company and role else (company or role)
            blocks.append(
                Block(
                    kind="job",
                    runs=parse_highlights(header),
                    label=company,
                    align_right=str(job.get("dates") or "").strip() or None,
                )
            )
            meta = [str(job.get(key) or "").strip() for key in ("location",)]
            meta = [item for item in meta if item]
            if meta:
                blocks.append(_block("meta", " · ".join(meta)))
            for bullet in job.get("bullets") or []:
                blocks.append(_block("bullet", str(bullet)))

    education = cleaned.get("education") or []
    if education:
        blocks.append(_block("section", "Education"))
        for entry in education:
            degree = str(entry.get("degree") or "").strip()
            school = str(entry.get("school") or "").strip()
            header = f"{degree} — {school}" if degree and school else (degree or school)
            extra = " · ".join(
                part
                for part in (
                    str(entry.get("location") or "").strip(),
                    f"GPA {entry['gpa']}" if entry.get("gpa") else "",
                )
                if part
            )
            blocks.append(
                Block(
                    kind="job",
                    runs=parse_highlights(header),
                    label=school,
                    align_right=str(entry.get("dates") or "").strip() or None,
                )
            )
            if extra:
                blocks.append(_block("meta", extra))

    # "additional_information" is deliberately not rendered: the section is not part
    # of the resume the tool is meant to produce, so the generator always skips it
    # no matter what the model returns.
    return blocks


# ------------------------------------------------------------------ rendering


def _set_background(document: Document, hex_color: str) -> None:
    """Word-style full-page background (Appendix A #8); verified in GEN-8."""
    color = color_hex(hex_color, "FFFFFF")
    if color == "FFFFFF":
        return
    settings = document.settings.element
    background = OxmlElement("w:background")
    background.set(qn("w:color"), color)
    settings.insert(0, background)
    display = OxmlElement("w:displayBackgroundShape")
    settings.append(display)


_PPR_AFTER_BORDER = (
    "w:shd",
    "w:tabs",
    "w:suppressAutoHyphens",
    "w:kinsoku",
    "w:wordWrap",
    "w:overflowPunct",
    "w:topLinePunct",
    "w:autoSpaceDE",
    "w:autoSpaceDN",
    "w:bidi",
    "w:adjustRightInd",
    "w:snapToGrid",
    "w:spacing",
    "w:ind",
    "w:contextualSpacing",
    "w:mirrorIndents",
    "w:suppressOverlap",
    "w:jc",
    "w:textDirection",
    "w:textAlignment",
    "w:textboxTightWrap",
    "w:outlineLvl",
    "w:divId",
    "w:cnfStyle",
    "w:rPr",
    "w:sectPr",
    "w:pPrChange",
)

_PPR_AFTER_SHADING = _PPR_AFTER_BORDER[1:]

_RPR_AFTER_SPACING = (
    "w:w",
    "w:kern",
    "w:position",
    "w:sz",
    "w:szCs",
    "w:highlight",
    "w:u",
    "w:effect",
    "w:bdr",
    "w:shd",
    "w:fitText",
    "w:vertAlign",
    "w:rtl",
    "w:cs",
    "w:em",
    "w:lang",
    "w:eastAsianLayout",
    "w:specVanish",
    "w:oMath",
)

_RPR_AFTER_WIDTH = _RPR_AFTER_SPACING[1:]

_RPR_AFTER_SHADING = (
    "w:fitText",
    "w:vertAlign",
    "w:rtl",
    "w:cs",
    "w:em",
    "w:lang",
    "w:eastAsianLayout",
    "w:specVanish",
    "w:oMath",
)


def _bottom_border(paragraph, color: str, size: int = 6) -> None:
    """Section rule. ``size`` is in eighths of a point (Word's ``w:sz``)."""
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(int(size)))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color_hex(color))
    borders.append(bottom)
    p_pr.insert_element_before(borders, *_PPR_AFTER_BORDER)


def _shade_paragraph(paragraph, hex_color: str) -> None:
    colour = color_hex(hex_color, "FFFFFF")
    if colour == "FFFFFF":
        return
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), colour)
    paragraph._p.get_or_add_pPr().insert_element_before(shading, *_PPR_AFTER_SHADING)


def _shade_run(run, hex_color: str) -> None:
    colour = color_hex(hex_color, "FFFFFF")
    if colour == "FFFFFF":
        return
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), colour)
    run._r.get_or_add_rPr().insert_element_before(shading, *_RPR_AFTER_SHADING)


def _set_run_fonts(run, font: str) -> None:
    r_pr = run._r.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert_element_before(r_fonts, "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:color", "w:sz")
    for attribute in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        r_fonts.set(qn(attribute), font)


def _set_char_spacing(run, points: float) -> None:
    """``w:spacing`` — letter tracking in twentieths of a point."""
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:val"), str(int(round(float(points) * 20))))
    run._r.get_or_add_rPr().insert_element_before(spacing, *_RPR_AFTER_SPACING)


def _set_char_scale(run, percent: float) -> None:
    """``w:w`` — character width scaling (Word's ``w:w``, percent)."""
    scale = OxmlElement("w:w")
    scale.set(qn("w:val"), str(int(round(float(percent)))))
    run._r.get_or_add_rPr().insert_element_before(scale, *_RPR_AFTER_WIDTH)


def _google_runs(name: str) -> list[Run]:
    return [Run(char, bold=True, color=GOOGLE_COLORS[index % len(GOOGLE_COLORS)]) for index, char in enumerate(name)]


def build_docx(blocks: Iterable[Block], opts: dict | None = None, out_path: str | Path = "resume.docx") -> str:
    """Render blocks into a DOCX at ``out_path`` and return the path (GEN-1)."""
    options = {**DEFAULTS, **(opts or {})}
    base_size = float(options.get("size") or DEFAULTS["size"])
    font = str(options.get("font") or DEFAULTS["font"])
    body_color = color_hex(options.get("body_color"), BODY_COLOR)
    line_height = float(options.get("line_height") or DEFAULTS["line_height"])

    document = Document()
    _set_background(document, str(options.get("bg_color", "FFFFFF")))

    style = document.styles["Normal"]
    style.font.name = font
    style.font.size = Pt(base_size)
    style.font.color.rgb = RGBColor.from_string(body_color)
    _set_east_asian_font(style.element, font)
    paragraph_format = style.paragraph_format
    paragraph_format.line_spacing = line_height
    paragraph_format.space_before = Pt(0)
    paragraph_format.space_after = Pt(1)

    section = document.sections[0]
    if str(options.get("page_size") or "Letter").strip().upper().startswith("A4"):
        section.page_width = Mm(210)
        section.page_height = Mm(297)
    else:
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
    margin_left = float(options.get("margin_left", DEFAULTS["margin_left"]))
    margin_right = float(options.get("margin_right", DEFAULTS["margin_right"]))
    section.top_margin = Inches(float(options.get("margin_top", DEFAULTS["margin_top"])))
    section.bottom_margin = Inches(float(options.get("margin_bottom", DEFAULTS["margin_bottom"])))
    section.left_margin = Inches(margin_left)
    section.right_margin = Inches(margin_right)
    text_width = float(section.page_width.inches) - margin_left - margin_right
    _set_tab_stop(section, Inches(max(1.0, text_width)))

    for block in blocks:
        _render_block(document, block, options)

    out_path = Path(out_path)
    document.save(str(out_path))
    return str(out_path)


def _style_paragraph(paragraph, style: dict[str, Any], *, keep_with_next: bool = False) -> None:
    paragraph_format = paragraph.paragraph_format
    paragraph_format.line_spacing = float(style["line_height"])
    paragraph_format.space_before = Pt(float(style["space_before"]))
    paragraph_format.space_after = Pt(float(style["space_after"]))
    paragraph_format.keep_with_next = keep_with_next
    if style.get("align"):
        paragraph.alignment = WD_ALIGN_PARAGRAPH[str(style["align"]).upper()]
    if style.get("indent_left") is not None:
        paragraph_format.left_indent = Inches(float(style["indent_left"]))
    if style.get("indent_right") is not None:
        paragraph_format.right_indent = Inches(float(style["indent_right"]))
    if style.get("first_line_indent") is not None:
        paragraph_format.first_line_indent = Inches(float(style["first_line_indent"]))
    if style.get("bg"):
        _shade_paragraph(paragraph, str(style["bg"]))


def _add_styled_run(
    paragraph,
    text: str,
    *,
    style: dict[str, Any],
    size: float,
    color: str,
    bold: bool,
    italic: bool | None = None,
    underline: bool | None = None,
    bg: str | None = None,
):
    if not text:
        return None
    run = paragraph.add_run(text)
    font = str(style.get("font") or DEFAULTS["font"])
    run.font.size = Pt(float(size))
    run.font.bold = bool(bold)
    run.font.name = font
    if italic:
        run.font.italic = True
    if underline:
        run.font.underline = True
    run.font.color.rgb = RGBColor.from_string(color_hex(color, BODY_COLOR))
    _set_run_fonts(run, font)
    letter_spacing = float(style.get("letter_spacing") or 0.0)
    if letter_spacing:
        _set_char_spacing(run, letter_spacing)
    width_scale = float(style.get("width_scale") or 100.0)
    if width_scale != 100.0:
        _set_char_scale(run, width_scale)
    if bg:
        _shade_run(run, bg)
    return run


def _add_runs(paragraph, block: Block, *, style: dict[str, Any], size: float, color: str, bold: bool, rules=None) -> None:
    for run in apply_text_rules(block.runs, rules):
        text = run.text.upper() if (style.get("uppercase") or run.uppercase) else run.text
        _add_styled_run(
            paragraph,
            text,
            style=style,
            size=size,
            color=run.color or color,
            bold=bool(run.bold or bold),
            italic=bool(run.italic) or bool(style.get("italic")),
            underline=bool(run.underline) or bool(style.get("underline")),
            bg=run.bg,
        )


def _add_tabbed_runs(paragraph, block: Block, document: Document, style: dict[str, Any], options: dict) -> None:
    paragraph.paragraph_format.tab_stops.add_tab_stop(
        _twips_to_length(_resolve_tab_position(document)), WD_TAB_ALIGNMENT.RIGHT
    )
    _add_runs(
        paragraph,
        block,
        style=style,
        size=float(style["size"]),
        color=str(style["color"]),
        bold=bool(style["bold"]),
        rules=options.get("text_rules"),
    )
    tab = paragraph.add_run("\t")
    tab.font.size = Pt(float(style["size"]))
    dates = paragraph.add_run(block.align_right or "")
    dates.font.size = Pt(float(style["size"]) + SIZE_OFFSETS["meta"] - SIZE_OFFSETS["job"])
    dates.font.color.rgb = RGBColor.from_string(str(style["muted"]))
    dates.font.name = str(style["font"])


def _render_block(document: Document, block: Block, options: dict) -> None:
    kind = block.kind
    style = resolve_element(options, kind)
    if style["hidden"]:
        return
    rules = options.get("text_rules")
    size = float(style["size"])
    paragraph = document.add_paragraph()
    _style_paragraph(paragraph, style, keep_with_next=kind in {"section", "job"})

    if kind == "job" and block.align_right:
        _add_tabbed_runs(paragraph, block, document, style, options)
    elif kind == "job" and (block.label or "").strip().lower() == "google":
        for run in _google_runs(block.label or ""):
            _add_styled_run(
                paragraph,
                run.text,
                style=style,
                size=size,
                color=run.color or str(style["accent"]),
                bold=True,
            )
    elif kind == "bullet":
        glyph = paragraph.add_run(f"{options.get('bullet_glyph') or DEFAULTS['bullet_glyph']}  ")
        glyph.font.size = Pt(size)
        glyph.font.color.rgb = RGBColor.from_string(
            str(style["color"]) if style["color_set"] else str(style["accent"])
        )
        _add_runs(paragraph, block, style=style, size=size, color=str(style["color"]), bold=False, rules=rules)
    elif kind == "skills":
        label = paragraph.add_run(f"{block.label}: ")
        label.font.size = Pt(size)
        label.font.bold = True
        label.font.color.rgb = RGBColor.from_string(str(style["color"]))
        _add_runs(
            paragraph,
            block,
            style={**style, "uppercase": False},
            size=size,
            color=str(style["color"]),
            bold=False,
            rules=rules,
        )
    else:
        _add_runs(
            paragraph,
            block,
            style=style,
            size=size,
            color=str(style["color"]),
            bold=bool(style["bold"]),
            rules=rules,
        )

    if style["rule"] and float(style["rule_width"]) > 0:
        _bottom_border(paragraph, str(style["color"]), size=int(style["rule_width"]))


def _set_east_asian_font(style_element, font: str) -> None:
    r_pr = style_element.get_or_add_rPr() if hasattr(style_element, "get_or_add_rPr") else None
    if r_pr is None:
        return
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.append(r_fonts)
    for attribute in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        r_fonts.set(qn(attribute), font)


def _set_tab_stop(section, position) -> None:
    paragraph_format = section._sectPr
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")
    tab.set(qn("w:pos"), str(int(position.twips)))
    tabs.append(tab)
    paragraph_format.append(tabs)


def _resolve_tab_position(document: Document) -> int:
    from docx.shared import Emu

    section = document.sections[0]
    page_width = section.page_width or Inches(8.5)
    left = section.left_margin or Emu(0)
    right = section.right_margin or Emu(0)
    return int(Emu(int(page_width) - int(left) - int(right)).twips)


def _twips_to_length(twips: int):
    from docx.shared import Twips

    return Twips(twips)
