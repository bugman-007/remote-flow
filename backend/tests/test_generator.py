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
