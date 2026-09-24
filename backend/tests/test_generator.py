"""Golden test for the vendored generator (GEN-5, GEN-8, NFR-10)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from vendor.resume_builder import core, pdf

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "vendor" / "resume_builder" / "sample.json"


def _render(index: int) -> tuple[int, int]:
    data = json.loads(SAMPLE.read_text())
    out = Path(f"/tmp/rf-generator-{index}")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "out.docx"
    core.build_docx(core.build_blocks(data), {**core.DEFAULTS, "accent": "#123456"}, path)
    return index, path.stat().st_size


def test_make_names_matches_desktop_tool():
    data = json.loads(SAMPLE.read_text())
    name, basename = core.make_names(data)
    assert name == "Luis Angel Salazar"
    assert basename == "Luis-Angel-Salazar_Senior-Full-Stack-Developer"


def test_make_names_falls_back_to_subtitle_and_slugifies():
    name, basename = core.make_names({"name": "A/B", "subtitle": "DevOps Lead · Remote"})
    assert name == "A/B"
    assert basename == "AB_DevOps-Lead"


def test_highlight_parsing_supports_span_suffix_and_markdown():
    runs = core.parse_highlights("a [highlight]b[/highlight] c**d** e[highlight]")
    assert [run.text for run in runs] == ["a ", "b", " c", "d", " ", "e"]
    assert [run.bold for run in runs] == [False, True, False, True, False, True]


def test_placeholder_contact_values_are_dropped():
    """A model that answers "None"/"N/A" instead of omitting the key must not leak it."""
    blocks = core.build_blocks(
        {
            "name": "Ada Lovelace",
            "title": "Engineer",
            "email": "ada@example.com",
            "location": "London",
            "citizenship": "None",
            "work_authorization": "N/A",
            "linkedin": "linkedin.com/in/ada",
            "summary": "s",
            "experience": [{"title": "Engineer", "company": "Acme", "bullets": ["b"]}],
        }
    )
    text = "".join(run.text for run in next(b for b in blocks if b.kind == "contact").runs)
    assert text == "ada@example.com  ·  London  ·  linkedin.com/in/ada"


def test_additional_information_is_never_rendered():
    """The generator skips the section even when the model returns it (GEN-4)."""
    data = json.loads(SAMPLE.read_text())
    assert data["additional_information"]  # the fixture does carry the section
    blocks = core.build_blocks(data)
    rendered = "\n".join("".join(run.text for run in block.runs) for block in blocks)
    assert "Additional Information" not in rendered
    assert "English (native)" not in rendered
    assert all(block.label != "Interests" for block in blocks)


def test_missing_optional_fields_do_not_render_as_none():
    """A null citizenship/work_authorization must be skipped, not printed as "None"."""
    blocks = core.build_blocks(
        {
            "name": "Ada Lovelace",
            "title": "Engineer",
            "email": "ada@example.com",
            "phone": None,
            "location": "London",
            "citizenship": None,
            "work_authorization": None,
            "summary": "s",
            "experience": [{"title": "Engineer", "company": "Acme", "location": None, "bullets": ["b"]}],
        }
    )
    contact = next(block for block in blocks if block.kind == "contact")
    text = "".join(run.text for run in contact.runs)
    assert "None" not in text
    assert text == "ada@example.com  ·  London"


def test_compose_job_info_uses_injected_description():
    data = json.loads(SAMPLE.read_text())
    data["job_description"] = "Original JD text"
    text = core.compose_job_info(data)
    assert "Original JD text" in text
    assert "Job link" in text
    assert core.compose_job_info({}) == ""


def test_generator_is_parallel_safe():
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_render, range(8)))
    assert len(results) == 8
    assert all(size > 10_000 for _index, size in results)


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed")
def test_golden_render_through_libreoffice_is_deterministic(tmp_path):
    data = json.loads(SAMPLE.read_text())
    outputs = []
    for index in range(2):
        docx = tmp_path / f"{index}.docx"
        core.build_docx(core.build_blocks(data), core.DEFAULTS, docx)
        pdf_path = tmp_path / f"{index}.pdf"
        pdf.convert_docx_to_pdf(docx, pdf_path, backend="soffice", timeout_s=120)
        outputs.append(pdf_path.read_bytes())
    assert outputs[0][:4] == b"%PDF"
    assert abs(len(outputs[0]) - len(outputs[1])) < 5000


def test_generated_docx_contains_expected_text(tmp_path):
    from docx import Document

    data = json.loads(SAMPLE.read_text())
    path = tmp_path / "resume.docx"
    core.build_docx(core.build_blocks(data), core.DEFAULTS, path)
    document = Document(str(path))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Luis Angel Salazar" in text
    assert "Nimbus Logistics" in text
    assert "Technical Skills" in text
    assert path.read_bytes()[:2] == b"PK"


# ------------------------------------------- Appendix C.2 element styling (v2)


def _rich_theme(**extra):
    theme = {
        **core.DEFAULTS,
        "accent": "#0F766E",
        "body_color": "#111827",
        "elements": {
            "name": {"align": "center", "size": 26, "letter_spacing": 1.0, "width_scale": 110},
            "section": {"uppercase": True, "rule_width": 12, "space_before": 16, "bg": "#F0FDFA"},
            "meta": {"hidden": True},
        },
        "text_rules": [{"text": "Node.js", "bold": True, "color": "#B91C1C", "underline": True}],
    }
    theme.update(extra)
    return theme


def test_theme_v1_snapshot_still_renders(tmp_path):
    """Back-compat: a snapshot saved before Appendix C.2 renders unchanged."""
    from docx import Document

    data = json.loads(SAMPLE.read_text())
    old_snapshot = {
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
    path = tmp_path / "v1.docx"
    core.build_docx(core.build_blocks(data), old_snapshot, path)
    document = Document(str(path))
    assert "Luis Angel Salazar" in "\n".join(p.text for p in document.paragraphs)


def test_element_overrides_only_touch_their_own_kind(tmp_path):
    from docx import Document

    data = json.loads(SAMPLE.read_text())
    path = tmp_path / "styled.docx"
    core.build_docx(core.build_blocks(data), _rich_theme(), path)
    document = Document(str(path))
    paragraphs = document.paragraphs

    name = paragraphs[0]
    assert name.text == "Luis Angel Salazar"
    assert str(name.alignment) == "CENTER (1)"
    assert name.runs[0].font.size.pt == 26.0
    assert 'w:w w:val="110"' in name._p.xml  # character width scaling

    title = paragraphs[1]
    assert str(title.alignment) != "CENTER (1)"

    sections = [p for p in paragraphs if p.text == "SUMMARY"]
    assert sections, "section heading should be upper-cased"
    assert "w:shd" in sections[0]._p.xml  # element background


def test_hidden_element_is_not_rendered(tmp_path):
    from docx import Document

    data = json.loads(SAMPLE.read_text())
    path = tmp_path / "hidden.docx"
    core.build_docx(core.build_blocks(data), _rich_theme(), path)
    document = Document(str(path))
    assert not any(p.text.strip() == "Austin, TX" for p in document.paragraphs)


def test_text_rules_style_every_occurrence(tmp_path):
    from docx import Document

    data = json.loads(SAMPLE.read_text())
    path = tmp_path / "rules.docx"
    core.build_docx(core.build_blocks(data), _rich_theme(), path)
    document = Document(str(path))
    hits = [run for paragraph in document.paragraphs for run in paragraph.runs if "Node.js" in run.text]
    assert hits, "the sample mentions Node.js several times"
    assert all(run.font.bold and run.font.underline for run in hits)
    assert all(str(run.font.color.rgb) == "B91C1C" for run in hits)
    assert all(run.text == "Node.js" for run in hits), "runs are split exactly on the rule"


def test_apply_text_rules_is_case_insensitive_and_skips_absent_text():
    runs = [core.Run("Senior Full Stack Developer"), core.Run("Nothing here")]
    out = core.apply_text_rules(runs, [{"text": "full stack", "bold": True}])
    assert [(run.text, run.bold) for run in out] == [
        ("Senior ", False),
        ("Full Stack", True),
        (" Developer", False),
        ("Nothing here", False),
    ]


def test_resolve_element_defaults_match_the_v1_style():
    style = core.resolve_element(core.DEFAULTS, "name")
    assert style["size"] == core.DEFAULTS["size"] + core.SIZE_OFFSETS["name"]
    assert style["bold"] is True
    assert style["color"] == "1F4E79"
    body = core.resolve_element(core.DEFAULTS, "body")
    assert body["align"] == "justify"
    assert body["indent_left"] is None
