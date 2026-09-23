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
from docx.shared import Inches, Pt, RGBColor

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

#: Fixed text colours of the generator (not themeable in v1).
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
    text: str
    bold: bool = False
    color: str | None = None


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

    # Missing optional fields must not print as the literal string "None".
    contact = [
        str(cleaned.get(key) or "").strip()
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

    extra_info = cleaned.get("additional_information") or {}
    if isinstance(extra_info, dict) and extra_info:
        blocks.append(_block("section", "Additional Information"))
        for label, value in extra_info.items():
            blocks.append(Block(kind="skills", label=str(label), runs=parse_highlights(str(value or ""))))

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


def _bottom_border(paragraph, color: str, size: int = 6) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    p_pr.append(borders)


def _google_runs(name: str) -> list[Run]:
    return [Run(char, bold=True, color=GOOGLE_COLORS[index % len(GOOGLE_COLORS)]) for index, char in enumerate(name)]


def build_docx(blocks: Iterable[Block], opts: dict | None = None, out_path: str | Path = "resume.docx") -> str:
    """Render blocks into a DOCX at ``out_path`` and return the path."""
    options = {**DEFAULTS, **(opts or {})}
    base_size = float(options.get("size", DEFAULTS["size"]))
    accent = color_hex(options.get("accent"), "1F4E79")
    font = str(options.get("font") or DEFAULTS["font"])
    line_height = float(options.get("line_height", DEFAULTS["line_height"]))
    section_gap = float(options.get("section_gap", DEFAULTS["section_gap"]))

    document = Document()
    _set_background(document, str(options.get("bg_color", "FFFFFF")))

    style = document.styles["Normal"]
    style.font.name = font
    style.font.size = Pt(base_size)
    style.font.color.rgb = RGBColor.from_string(BODY_COLOR)
    _set_east_asian_font(style.element, font)
    paragraph_format = style.paragraph_format
    paragraph_format.line_spacing = line_height
    paragraph_format.space_before = Pt(0)
    paragraph_format.space_after = Pt(1)

    section = document.sections[0]
    section.top_margin = Inches(float(options.get("margin_top", DEFAULTS["margin_top"])))
    section.bottom_margin = Inches(float(options.get("margin_bottom", DEFAULTS["margin_bottom"])))
    section.left_margin = Inches(float(options.get("margin_left", DEFAULTS["margin_left"])))
    section.right_margin = Inches(float(options.get("margin_right", DEFAULTS["margin_right"])))
    _set_tab_stop(section, Inches(7.0))

    for block in blocks:
        _render_block(document, block, base_size, accent, section_gap)

    out_path = Path(out_path)
    document.save(str(out_path))
    return str(out_path)


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
    from docx.shared import Emu, Inches

    section = document.sections[0]
    page_width = section.page_width or Inches(8.5)
    left = section.left_margin or Emu(0)
    right = section.right_margin or Emu(0)
    return int(Emu(int(page_width) - int(left) - int(right)).twips)


def _add_runs(paragraph, block: Block, *, size: float, color: str, bold: bool) -> None:
    for run in block.runs:
        target = paragraph.add_run(run.text)
        target.font.size = Pt(size)
        target.font.bold = bool(run.bold or bold)
        target.font.color.rgb = RGBColor.from_string(color_hex(run.color, color))


def _render_block(document: Document, block: Block, base_size: float, accent: str, section_gap: float) -> None:
    kind = block.kind
    space_before, space_after = KIND_PROPS.get(kind, (0.0, 1.0))
    paragraph = document.add_paragraph()
    paragraph_format = paragraph.paragraph_format
    paragraph_format.line_spacing = document.styles["Normal"].paragraph_format.line_spacing
    paragraph_format.space_before = Pt(space_before + (section_gap if kind == "section" else 0.0))
    paragraph_format.space_after = Pt(space_after)
    paragraph_format.keep_with_next = kind in {"section", "job"}

    if kind == "name":
        _add_runs(paragraph, block, size=base_size + SIZE_OFFSETS["name"], color=accent, bold=True)
    elif kind == "title":
        _add_runs(paragraph, block, size=base_size + SIZE_OFFSETS["title"], color=accent, bold=True)
    elif kind == "contact":
        _add_runs(paragraph, block, size=base_size + SIZE_OFFSETS["contact"], color=MUTED_COLOR, bold=False)
    elif kind == "section":
        _add_runs(paragraph, block, size=base_size + SIZE_OFFSETS["section"], color=accent, bold=True)
        _bottom_border(paragraph, accent)
    elif kind == "job":
        if block.align_right:
            _add_tabbed_runs(paragraph, block, document, base_size + SIZE_OFFSETS["job"], accent)
        elif block.label and block.label.strip().lower() == "google":
            for run in _google_runs(block.label):
                target = paragraph.add_run(run.text)
                target.font.size = Pt(base_size + SIZE_OFFSETS["job"])
                target.font.bold = True
                target.font.color.rgb = RGBColor.from_string(color_hex(run.color))
        else:
            _add_runs(paragraph, block, size=base_size + SIZE_OFFSETS["job"], color=BODY_COLOR, bold=True)
    elif kind == "meta":
        _add_runs(paragraph, block, size=base_size + SIZE_OFFSETS["meta"], color=MUTED_COLOR, bold=False)
    elif kind == "bullet":
        paragraph_format.left_indent = Inches(0.18)
        paragraph_format.first_line_indent = Inches(-0.18)
        glyph = paragraph.add_run("•  ")
        glyph.font.size = Pt(base_size)
        glyph.font.color.rgb = RGBColor.from_string(accent)
        _add_runs(paragraph, block, size=base_size, color=BODY_COLOR, bold=False)
    elif kind == "skills":
        label = paragraph.add_run(f"{block.label}: ")
        label.font.size = Pt(base_size)
        label.font.bold = True
        label.font.color.rgb = RGBColor.from_string(BODY_COLOR)
        _add_runs(paragraph, block, size=base_size, color=BODY_COLOR, bold=False)
    else:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _add_runs(paragraph, block, size=base_size, color=BODY_COLOR, bold=False)


def _add_tabbed_runs(paragraph, block: Block, document: Document, size: float, accent: str) -> None:
    paragraph.paragraph_format.tab_stops.add_tab_stop(
        _twips_to_length(_resolve_tab_position(document)), WD_TAB_ALIGNMENT.RIGHT
    )
    _add_runs(paragraph, block, size=size, color=BODY_COLOR, bold=True)
    tab = paragraph.add_run("\t")
    tab.font.size = Pt(size)
    dates = paragraph.add_run(block.align_right or "")
    dates.font.size = Pt(size + SIZE_OFFSETS["meta"] - SIZE_OFFSETS["job"])
    dates.font.color.rgb = RGBColor.from_string(MUTED_COLOR)


def _twips_to_length(twips: int):
    from docx.shared import Twips

    return Twips(twips)
