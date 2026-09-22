"""Render stage (GEN-2, GEN-3, GEN-7): DOCX + PDF + TXT into an immutable generation."""

from __future__ import annotations

import json
import logging
import random
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import get_settings
from app.services import storage
from app.services.llm import canonicalise
from vendor.resume_builder import GENERATOR_VERSION, core, pdf

logger = logging.getLogger(__name__)


class RenderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RenderOutput:
    temp_dir: Path
    files: list[storage.StoredFile] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


_pdf_converter: Callable[..., str] | None = None


def set_pdf_converter(converter: Callable[..., str] | None) -> None:
    """Test/dev hook: replace the LibreOffice call (GEN-3 backend selection stays)."""
    global _pdf_converter
    _pdf_converter = converter


def configured_backend() -> str:
    """GEN-3: the PDF backend for this process (``RB_PDF_BACKEND``)."""
    return (get_settings().pdf_backend or "unoserver").strip().lower()


def default_pdf_converter(docx_path: str, pdf_path: str, *, timeout_s: int) -> str:
    return pdf.convert_docx_to_pdf(docx_path, pdf_path, backend=configured_backend(), timeout_s=timeout_s)


def render_generation(
    *,
    llm_json: dict,
    theme_snapshot: dict | None,
    doc_set_path: Path,
    generation_no: int,
    meta: dict[str, Any] | None = None,
) -> RenderOutput:
    """Build the three files in a temp folder; the caller publishes it atomically (GEN-2)."""
    settings = get_settings()
    if settings.mock_render_fail_rate and random.random() < settings.mock_render_fail_rate:
        raise RenderError("render_failed", "mock renderer: injected failure")

    data = canonicalise(llm_json)
    options = {**core.DEFAULTS, **(theme_snapshot or {})}
    name, basename = core.make_names(data)
    temp_dir = storage.tmp_dir() / f"gen-{uuid.uuid4().hex}"
    storage.ensure_dir(temp_dir)
    started = time.perf_counter()
    try:
        docx_path = temp_dir / f"{basename}.docx"
        core.build_docx(core.build_blocks(data), options, docx_path)

        pdf_path = temp_dir / f"{basename}.pdf"
        converter = _pdf_converter or default_pdf_converter
        try:
            converter(str(docx_path), str(pdf_path), timeout_s=settings.conversion_timeout_s)
        except RenderError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to the render stage error codes
            text = str(exc).lower()
            if "timeout" in text or "timed out" in text:
                raise RenderError("render_timeout", str(exc)[:500]) from exc
            raise RenderError("render_failed", str(exc)[:500]) from exc

        txt_path = temp_dir / "job_description.txt"
        txt_path.write_text(core.compose_job_info(data), encoding="utf-8")

        llm_json_path = temp_dir / "llm.json"
        llm_json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        for path in (docx_path, pdf_path, txt_path):
            if not path.exists() or path.stat().st_size == 0:
                raise RenderError("render_output_missing", f"missing or empty output: {path.name}")

        artifacts = [
            storage.stat_file(docx_path, kind="docx"),
            storage.stat_file(pdf_path, kind="pdf"),
            storage.stat_file(txt_path, kind="txt"),
            storage.stat_file(llm_json_path, kind="llm_json"),
        ]
        meta_payload = {
            "generation_no": generation_no,
            "candidate_name": name,
            "docx_basename": basename,
            "engine": {
                "generator_version": GENERATOR_VERSION,
                "pdf_backend": "test-hook" if _pdf_converter else configured_backend(),
                "theme": options,
            },
            "timings": {"render_ms": int((time.perf_counter() - started) * 1000)},
            "hashes": {artifact.filename: artifact.sha256 for artifact in artifacts},
            **(meta or {}),
        }
        meta_path = temp_dir / "meta.json"
        meta_path.write_text(json.dumps(meta_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        artifacts.append(storage.stat_file(meta_path, kind="meta"))
        return RenderOutput(temp_dir=temp_dir, files=artifacts, meta=meta_payload)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def discard(output: RenderOutput) -> None:
    """PIPE-2: a worker whose lease expired discards its result."""
    shutil.rmtree(output.temp_dir, ignore_errors=True)
