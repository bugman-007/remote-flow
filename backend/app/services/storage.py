"""Local filesystem storage layout and ZIP streaming (4.8, STO-1/2/3)."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.config import get_settings
from app.utils import sha256_file, slugify


@dataclass
class StoredFile:
    path: Path
    filename: str
    kind: str
    size_bytes: int
    sha256: str


def storage_root() -> Path:
    return get_settings().storage_path()


def maker_dir(maker_id: str) -> Path:
    return storage_root() / "makers" / maker_id


def doc_set_dir(maker_id: str, submitted_date: date, seq_no: int, company: str | None) -> Path:
    slug = slugify(company)
    return maker_dir(maker_id) / submitted_date.isoformat() / f"{seq_no:03d}-{slug}"


def attempts_dir(doc_set_path: Path, stage: str, attempt_no: int) -> Path:
    return doc_set_path / "attempts" / f"{stage}-{attempt_no:02d}"


def generation_dir(doc_set_path: Path, generation_no: int) -> Path:
    return doc_set_path / f"gen-{generation_no:02d}"


def tmp_dir() -> Path:
    return storage_root() / "tmp"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_text(path: Path, content: str) -> StoredFile:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    return stat_file(path, kind="txt")


def write_bytes(path: Path, content: bytes, *, kind: str = "meta") -> StoredFile:
    ensure_dir(path.parent)
    path.write_bytes(content)
    return stat_file(path, kind=kind)


def stat_file(path: Path, *, kind: str = "meta") -> StoredFile:
    return StoredFile(
        path=path,
        filename=path.name,
        kind=kind,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(str(path)),
    )


def atomic_publish(temp_dir: Path, final_dir: Path) -> None:
    """GEN-2: atomically rename a fully written temp folder into place."""
    if final_dir.exists():
        raise FileExistsError(f"generation directory already exists: {final_dir}")
    ensure_dir(final_dir.parent)
    os.replace(temp_dir, final_dir) if temp_dir.is_dir() else shutil.move(str(temp_dir), str(final_dir))


def safe_relative(path: str | Path, *, root: Path | None = None) -> Path:
    """STO-2: reject any path escaping the storage root."""
    root = (root or storage_root()).resolve()
    candidate = Path(path)
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not str(resolved).startswith(str(root)):
        raise ValueError("path traversal detected")
    return resolved


def zip_entries(entries: list[tuple[Path, str]]) -> io.BytesIO:
    """Build a ZIP in memory (streamed by the API, no temp file - RES-5)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in entries:
            if path.exists() and path.is_file():
                archive.write(path, arcname)
    buffer.seek(0)
    return buffer


async def zip_entries_async(entries: list[tuple[Path, str]]) -> io.BytesIO:
    return await asyncio.to_thread(zip_entries, entries)


def doc_set_zip_folder(submitted_at, company: str | None, role: str | None) -> str:
    """Per-doc-set folder inside a ZIP: ``YYYY-MM-DD_HHMMSS_Company_Job-Title``.

    The timestamp leads so that sorting the extracted folders by name matches the
    Maker's submission (browser-tab) order.
    """
    stamp = submitted_at.strftime("%Y-%m-%d_%H%M%S") if submitted_at is not None else "undated"
    company_part = slugify(company, fallback="Company").replace("-", "_")
    role_part = slugify(role, fallback="Role").replace("-", "_")
    return f"{stamp}_{company_part}_{role_part}"


def download_filename(basename: str, kind: str) -> str:
    suffix = {"pdf": ".pdf", "docx": ".docx", "txt": ".txt"}[kind]
    return f"{basename}{suffix}"


def zip_name_for_date(maker_name: str, day: date | None, suffix: str = "all") -> str:
    safe_maker = slugify(maker_name, fallback="maker")
    stamp = day.isoformat() if day else "range"
    return f"{safe_maker}_{stamp}_{suffix}.zip"


def zip_name_for_range(maker_name: str, date_from: date | None, date_to: date | None) -> str:
    """RES-1: name a range download ``maker_YYYY-MM-DD_YYYY-MM-DD.zip``."""
    safe_maker = slugify(maker_name, fallback="maker")
    start = date_from.isoformat() if date_from else "start"
    end = date_to.isoformat() if date_to else "end"
    return f"{safe_maker}_{start}_{end}.zip"


def disk_usage_pct() -> float:
    root = storage_root()
    ensure_dir(root)
    usage = shutil.disk_usage(str(root))
    return round(usage.used / usage.total * 100, 2)


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def relative_to_root(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(storage_root()))
    except ValueError:
        return str(path)


def parse_date(value: str | date | None, fallback: date | None = None) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    return fallback or date.today()
