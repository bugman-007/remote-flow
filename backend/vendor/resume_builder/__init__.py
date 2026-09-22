"""Vendored `resume_builder` (GEN-1).

Split out of the original single-file desktop tool so the server can import the
pure document model without Tkinter, Word COM or import-time side effects. The
public surface used by Remote Flow:

    from resume_builder import core, pdf
    blocks = core.build_blocks(data)
    core.build_docx(blocks, {**core.DEFAULTS, **theme_snapshot}, out_path)
    pdf.convert_docx_to_pdf(docx_path, pdf_path, backend="unoserver")
"""

from __future__ import annotations

GENERATOR_VERSION = "1.0.0-remote-flow"

from . import core, pdf  # noqa: E402  (kept after GENERATOR_VERSION on purpose)

__all__ = ["core", "pdf", "GENERATOR_VERSION"]
