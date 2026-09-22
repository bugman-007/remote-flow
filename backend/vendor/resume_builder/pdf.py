"""DOCX → PDF conversion backends (GEN-1, GEN-3)."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

SUPPORTED_BACKENDS = ("unoserver", "soffice", "word", "stub")

_word_worker = None
_word_lock = threading.Lock()


class PDFConversionError(RuntimeError):
    """Raised when every configured backend failed to produce a PDF."""


def convert_docx_to_pdf(docx_path: str | Path, pdf_path: str | Path, *, backend: str = "unoserver",
                        timeout_s: int = 60, port: int | None = None) -> str:
    docx_path, pdf_path = str(docx_path), str(pdf_path)
    if not os.path.exists(docx_path):
        raise PDFConversionError(f"source document not found: {docx_path}")

    if backend == "unoserver":
        try:
            _convert_unoserver(docx_path, pdf_path, timeout_s=timeout_s, port=port)
            return pdf_path
        except Exception as exc:  # noqa: BLE001 - GEN-3 single cold-start fallback
            fallback_reason = exc
        try:
            _convert_soffice(docx_path, pdf_path, timeout_s=timeout_s)
            return pdf_path
        except Exception as exc:  # noqa: BLE001
            raise PDFConversionError(
                f"unoserver failed ({fallback_reason}); soffice fallback failed ({exc})"
            ) from exc

    if backend == "soffice":
        _convert_soffice(docx_path, pdf_path, timeout_s=timeout_s)
        return pdf_path

    if backend == "word":
        _get_word_worker().convert(docx_path, pdf_path)
        return pdf_path

    if backend == "stub":
        # Development only (boxes without LibreOffice, e.g. `make dev` and the
        # benchmark): writes a valid but contentless PDF so the pipeline and the
        # download/ZIP paths can be exercised. Never use in production.
        Path(pdf_path).write_bytes(
            b"%PDF-1.4\n% remote-flow stub backend - not a real conversion\n%%EOF\n"
        )
        return pdf_path

    raise PDFConversionError(f"unknown PDF backend: {backend!r} (expected one of {SUPPORTED_BACKENDS})")


# ------------------------------------------------------------------- backends


def unoserver_port(index: int = 0) -> int:
    return 2002 + index


def _convert_unoserver(docx_path: str, pdf_path: str, *, timeout_s: int, port: int | None) -> None:
    import urllib.error
    import urllib.request

    port = port or int(os.environ.get("UNOSERVER_PORT") or unoserver_port())
    payload = _multipart(docx_path, outdir=str(Path(pdf_path).parent))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/request",
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PDFConversionError(f"unoserver request failed: {exc}") from exc
    _wait_for_file(pdf_path, timeout_s)


_BOUNDARY = "----RemoteFlowBoundary7MA4YWxkTrZu0gW"


def _multipart(docx_path: str, *, outdir: str) -> bytes:
    with open(docx_path, "rb") as handle:
        content = handle.read()
    filename = os.path.basename(docx_path)
    parts = [
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
        "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n" % filename,
        content,
        f"\r\n--{_BOUNDARY}\r\nContent-Disposition: form-data; name=\"convert-to\"\r\n\r\npdf\r\n",
        f"--{_BOUNDARY}\r\nContent-Disposition: form-data; name=\"outdir\"\r\n\r\n{outdir}\r\n",
        f"--{_BOUNDARY}--\r\n",
    ]
    return b"".join(part if isinstance(part, bytes) else part.encode("utf-8") for part in parts)


def _convert_soffice(docx_path: str, pdf_path: str, *, timeout_s: int) -> None:
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise PDFConversionError("LibreOffice (soffice) is not installed")
    profile = tempfile.mkdtemp(prefix="rb-soffice-")
    outdir = str(Path(pdf_path).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    command = [
        binary,
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        outdir,
        docx_path,
    ]
    result = subprocess.run(command, capture_output=True, timeout=timeout_s, check=False)
    if result.returncode != 0 and not os.path.exists(pdf_path):
        raise PDFConversionError(result.stderr.decode("utf-8", "replace")[:500])
    _wait_for_file(pdf_path, timeout_s)
    shutil.rmtree(profile, ignore_errors=True)


def _wait_for_file(path: str, timeout_s: int) -> None:
    deadline = time.time() + max(timeout_s, 1)
    while time.time() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        time.sleep(0.1)
    raise PDFConversionError(f"conversion produced no output: {path}")


# --------------------------------------------------------------- word backend


class WordWorker:
    """Windows-only Word COM export; lazily created behind the ``word`` backend."""

    def __init__(self) -> None:
        import win32com.client  # type: ignore[import-not-found]

        self._app = win32com.client.DispatchEx("Word.Application")
        self._app.Visible = False
        self._app.DisplayAlerts = 0

    def convert(self, docx_path: str, pdf_path: str) -> None:
        document = self._app.Documents.Open(os.path.abspath(docx_path), ReadOnly=True)
        try:
            document.SaveAs(os.path.abspath(pdf_path), FileFormat=17)
        finally:
            document.Close(False)

    def quit(self) -> None:  # pragma: no cover - Windows only
        try:
            self._app.Quit()
        except Exception:  # noqa: BLE001
            pass


def _get_word_worker() -> WordWorker:  # pragma: no cover - Windows only
    global _word_worker
    with _word_lock:
        if _word_worker is None:
            _word_worker = WordWorker()
            atexit.register(_shutdown_word_worker)
        return _word_worker


def _shutdown_word_worker() -> None:  # pragma: no cover - Windows only
    global _word_worker
    if _word_worker is not None:
        _word_worker.quit()
        _word_worker = None
