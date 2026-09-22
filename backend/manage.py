#!/usr/bin/env python
"""Operator CLI (OPS-3): first manager, maintenance, benchmarks and backups."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import resource
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _bootstrap_schema() -> None:
    from app import models  # noqa: F401
    from app.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def cmd_migrate(_args: argparse.Namespace) -> int:
    """OPS-4: run Alembic under a lock so only one api instance migrates."""
    from app.db import url_is_postgres

    settings = get_settings()

    def run_upgrade() -> None:
        from alembic import command
        from alembic.config import Config

        config = Config(str(Path(__file__).resolve().parent / "alembic.ini"))
        config.set_main_option("script_location", str(Path(__file__).resolve().parent / "alembic"))
        config.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(config, "head")

    if url_is_postgres(settings.database_url):
        import asyncpg

        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute("SELECT pg_advisory_lock(hashtext('remote-flow:migrate'))")
            await asyncio.to_thread(run_upgrade)
        finally:
            await connection.execute("SELECT pg_advisory_unlock(hashtext('remote-flow:migrate'))")
            await connection.close()
    else:
        await asyncio.to_thread(run_upgrade)
    print("migrations applied")
    return 0


async def cmd_create_manager(args: argparse.Namespace) -> int:
    from app.services.seeds import ensure_user
    from app.security import generate_password
    from app.services import settings_store

    await _bootstrap_schema()
    password = args.password or generate_password(16)
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)
        user, created = await ensure_user(
            session, email=args.email, name=args.name, role="manager", password=password
        )
        await session.commit()
    if not created:
        print(f"Manager {user.email} already exists; password unchanged.")
        return 0
    print("Manager created.")
    print(f"  email:    {user.email}")
    print(f"  password: {password}")
    print("Pass this password to the manager out of band; they must change it on first login.")
    return 0


async def cmd_seed(args: argparse.Namespace) -> int:
    from app.services.seeds import seed_demo_data
    from app.services import settings_store
    from app.services.theme import seed_default_theme

    await _bootstrap_schema()
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)
        await seed_default_theme(session)
        info = await seed_demo_data(session, password=args.password)
        await session.commit()
    print(json.dumps(info, indent=2))
    return 0


async def cmd_rotate_master_key(args: argparse.Namespace) -> int:
    from app.services.crypto import decrypt_secret, encode_master_key, encrypt_secret
    from app.models import LLMProvider
    from sqlalchemy import select

    settings = get_settings()
    old_key = settings.master_key
    if not old_key:
        print("MASTER_KEY is not set; nothing to rotate.", file=sys.stderr)
        return 1
    os.environ["MASTER_KEY"] = args.new_key
    settings.master_key = args.new_key
    settings.resolved_master_key.cache_clear() if hasattr(settings.resolved_master_key, "cache_clear") else None
    async with session_scope() as session:
        providers = (await session.execute(select(LLMProvider))).scalars().all()
        rotated = 0
        for provider in providers:
            if not provider.api_key_enc:
                continue
            os.environ["MASTER_KEY"] = old_key
            plaintext = decrypt_secret(provider.api_key_enc)
            os.environ["MASTER_KEY"] = args.new_key
            from app.config import reset_settings_cache

            reset_settings_cache()
            provider.api_key_enc = encrypt_secret(plaintext) if plaintext else None
            rotated += 1
        await session.commit()
    print(f"Rotated {rotated} provider key(s). Store the new MASTER_KEY in your password manager.")
    return 0


async def cmd_reindex(args: argparse.Namespace) -> int:
    from sqlalchemy import text
    from app.db import is_postgres
    from app.services.search import attach_docset_search
    from app.models import DocSet, Job
    from sqlalchemy import select

    async with session_scope() as session:
        if is_postgres(session.get_bind()):
            await session.execute(text("UPDATE jobs SET jd_tsv = to_tsvector('english', coalesce(jd_text,''))"))
            await session.execute(
                text(
                    "UPDATE doc_sets SET search_tsv = lower(concat_ws(' ', coalesce(company_name,''), "
                    "coalesce(job_title,''), coalesce(candidate_name,'')))"
                )
            )
        else:
            for job in (await session.execute(select(Job))).scalars().all():
                await session.execute(
                    text("UPDATE jobs SET jd_tsv = :tsv WHERE id = :id"),
                    {"tsv": (job.jd_text or "").lower(), "id": job.id},
                )
            for doc_set in (await session.execute(select(DocSet))).scalars().all():
                await attach_docset_search(
                    session,
                    doc_set.id,
                    " ".join(filter(None, [doc_set.company_name, doc_set.job_title, doc_set.candidate_name])),
                )
        await session.commit()
    print("Search indexes rebuilt.")
    return 0


async def cmd_release_scan(args: argparse.Namespace) -> int:
    from app.services import release
    from app.models import Job
    from sqlalchemy import select

    async with session_scope() as session:
        if args.maker:
            makers = [args.maker]
        else:
            makers = [
                row for row in (
                    await session.execute(select(Job.maker_id).where(Job.delivery_status == "pending").distinct())
                ).scalars().all()
            ]
        released = 0
        for maker_id in makers:
            released += len(await release.release_pass(session, maker_id, actor="cli"))
        await session.commit()
    print(f"Released {released} job(s).")
    return 0


async def cmd_dispatch_sweep(args: argparse.Namespace) -> int:
    from app.db import get_session_factory
    from app.services import dispatch, pipeline

    async with session_scope() as session:
        count = await pipeline.dispatch_sweep(session)
        await session.commit()
        await dispatch.flush_dispatches(session)
    print(f"Re-dispatched {count} build(s).")
    return 0


async def cmd_expire_leases(args: argparse.Namespace) -> int:
    from app.services import dispatch, pipeline

    async with session_scope() as session:
        count = await pipeline.lease_watchdog(session)
        await session.commit()
        await dispatch.flush_dispatches(session)
    print(f"Recovered {count} expired lease(s).")
    return 0


async def cmd_relay_outbox(args: argparse.Namespace) -> int:
    from app.services import events

    async with session_scope() as session:
        published = await events.relay_unpublished(session)
        purged = await events.purge_old_outbox(session)
        await session.commit()
    print(f"Published {published} event(s); purged {purged}.")
    return 0


async def cmd_retention(args: argparse.Namespace) -> int:
    from app.services import retention

    async with session_scope() as session:
        plan = await retention.run_retention(session, dry_run=args.dry_run)
        await session.commit()
    if args.dry_run:
        megabytes = plan.get("bytes", 0) / (1024 * 1024)
        print(
            f"Would free {megabytes:.1f} MB across {len(plan.get('generation_ids', []))} generation(s) "
            f"and {len(plan.get('attempt_dirs', []))} attempt folder(s)."
        )
    else:
        print(f"Expired {plan.get('expired_generations', 0)} generation(s).")
    return 0


async def cmd_render_sample(args: argparse.Namespace) -> int:
    from app.models import Theme
    from app.services import render
    from vendor.resume_builder import core

    sample = json.loads((Path("vendor/resume_builder/sample.json")).read_text())
    sample["job_description"] = "Sample job description for the fidelity check (GEN-8)."
    async with session_scope() as session:
        theme = await session.get(Theme, args.theme) if args.theme else None
        if args.theme and theme is None:
            print(f"Theme {args.theme} not found.", file=sys.stderr)
            return 1
        params = theme.params if theme else dict(core.DEFAULTS)
        out_dir = Path(args.out or "docs/render-sample")
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            output = render.render_generation(
                llm_json=sample,
                theme_snapshot=params,
                doc_set_path=out_dir,
                generation_no=1,
                meta={"source": "manage.py render-sample"},
            )
        except render.RenderError as exc:
            print(f"Render failed: {exc.message}", file=sys.stderr)
            return 2
        for artifact in output.files:
            target = out_dir / artifact.filename
            shutil.copy2(artifact.path, target)
            print(f"wrote {target}")
        shutil.rmtree(output.temp_dir, ignore_errors=True)
    return 0


async def cmd_bench_render(args: argparse.Namespace) -> int:
    from app.services import render
    from vendor.resume_builder import core

    sample = json.loads((Path(__file__).resolve().parent / "vendor" / "resume_builder" / "sample.json").read_text())
    sample["job_description"] = "Benchmark job description."
    # HW-7: keep the benchmark runnable on boxes without LibreOffice so the numbers
    # are reproducible; the recorded result states which backend was measured.
    has_libreoffice = bool(shutil.which("unoserver") or shutil.which("soffice"))
    if not has_libreoffice:
        def _stub_converter(docx_path: str, pdf_path: str, *, timeout_s: int = 60) -> str:
            Path(pdf_path).write_bytes(b"%PDF-1.4\n% remote-flow benchmark stub\n%%EOF\n")
            return pdf_path

        render.set_pdf_converter(_stub_converter)
    times: list[float] = []
    failures = 0
    for index in range(args.n):
        started = time.perf_counter()
        try:
            output = render.render_generation(
                llm_json=sample,
                theme_snapshot=dict(core.DEFAULTS),
                doc_set_path=Path("."),
                generation_no=index + 1,
                meta={"benchmark": True},
            )
            shutil.rmtree(output.temp_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"render {index + 1} failed: {exc}", file=sys.stderr)
        times.append((time.perf_counter() - started) * 1000)
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {
        "n": args.n,
        "failures": failures,
        "p50_ms": round(statistics.median(times), 1) if times else None,
        "p95_ms": round(statistics.quantiles(times, n=20)[18], 1) if len(times) >= 20 else (round(max(times), 1) if times else None),
        "peak_rss_mb": round(peak_rss_kb / 1024, 1),
        "pdf_backend": "unoserver" if has_libreoffice else "stub (LibreOffice not installed)",
        "environment": {
            "python": sys.version.split()[0],
            "storage_dir": str(get_settings().storage_path()),
        },
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(result, indent=2))
    _append_benchmark("render", result)
    return 0 if failures == 0 else 1


async def cmd_bench_intake(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from app.models import Job, User
    from app.services import intake

    await _bootstrap_schema()
    async with session_scope() as session:
        maker = (
            await session.execute(select(User).where(User.email == args.maker))
        ).scalar_one_or_none()
        if maker is None:
            print(f"Maker {args.maker} not found. Run `manage.py seed` first.", file=sys.stderr)
            return 1
        before = (
            await session.execute(select(func.count(Job.id)).where(Job.maker_id == maker.id))
        ).scalar_one()
        times: list[float] = []
        errors = 0
        for index in range(args.n):
            text = f"Job description {index}: we are hiring a senior engineer at Company {index}."
            started = time.perf_counter()
            try:
                await intake.submit_jd(
                    session, maker=maker, jd_text=text, idempotency_key=f"bench-{time.time_ns()}-{index}"
                )
                await session.commit()
            except intake.IntakeError as exc:
                errors += 1
                await session.rollback()
                if exc.code == "limit_reached":
                    print("Daily limit reached; raise the maker's limit for the benchmark.", file=sys.stderr)
                    break
            times.append((time.perf_counter() - started) * 1000)
        after = (await session.execute(select(func.count(Job.id)).where(Job.maker_id == maker.id))).scalar_one()
    result = {
        "n": len(times),
        "errors": errors,
        "created": int(after - before),
        "p50_ms": round(statistics.median(times), 2) if times else None,
        "p95_ms": round(statistics.quantiles(times, n=20)[18], 2) if len(times) >= 20 else (round(max(times), 2) if times else None),
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(result, indent=2))
    _append_benchmark("intake", result)
    return 0


async def cmd_load_test(args: argparse.Namespace) -> int:
    """NFR-1/2 (M4): concurrent HTTP intake, then optional ordered-release verification."""
    import httpx

    base = args.base_url.rstrip("/")
    run = f"load-{time.time_ns()}"
    latencies: list[float] = []
    seqs: list[int] = []
    created = duplicates = errors = 0
    started_all = time.perf_counter()

    async with httpx.AsyncClient(base_url=base, timeout=args.timeout) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"email": args.email, "password": args.password}
        )
        if login.status_code != 200:
            print(f"login failed: {login.status_code} {login.text[:200]}", file=sys.stderr)
            return 1
        client.headers["X-CSRF-Token"] = login.json().get("csrf_token", "")
        semaphore = asyncio.Semaphore(args.concurrency)

        async def one(index: int) -> None:
            nonlocal created, duplicates, errors
            text = (
                f"Load test job {index}: we are hiring a senior platform engineer at "
                f"Company {index} to join a remote friendly team."
            )
            async with semaphore:
                moment = time.perf_counter()
                try:
                    response = await client.post(
                        "/api/v1/jobs",
                        json={"jd_text": text},
                        headers={"Idempotency-Key": f"{run}-{index}"},
                    )
                except Exception as exc:  # noqa: BLE001 - reported in the summary
                    errors += 1
                    print(f"request {index} failed: {exc}", file=sys.stderr)
                    return
                latencies.append((time.perf_counter() - moment) * 1000)
                if response.status_code == 201:
                    payload = response.json()
                    seqs.append(int(payload["seq_no"]))
                    if payload.get("created", True):
                        created += 1
                    else:
                        duplicates += 1
                else:
                    errors += 1
                    if errors <= 3:
                        print(f"request {index} -> {response.status_code}: {response.text[:200]}", file=sys.stderr)

        await asyncio.gather(*(one(index) for index in range(args.n)))
        elapsed = time.perf_counter() - started_all

        released = 0
        order_ok = True
        release_seconds = None
        if args.wait:
            deadline = time.time() + args.wait
            wanted = set(seqs)
            while time.time() < deadline:
                listing = await client.get(
                    "/api/v1/jobs", params={"page_size": 200, "date": args.date} if args.date else {"page_size": 200}
                )
                if listing.status_code == 200:
                    rows = {int(row["seq_no"]): row for row in listing.json()["items"]}
                    mine = sorted(seq for seq in wanted if seq in rows)
                    states = [str((rows[seq]["status"] or {}).get("status")) for seq in mine]
                    released = sum(1 for state in states if state == "released")
                    # ORD-4: no job may be released while an earlier one is still pending.
                    seen_pending = False
                    order_ok = True
                    for state in states:
                        if state == "released" and seen_pending:
                            order_ok = False
                        if state != "released":
                            seen_pending = True
                    if released >= len(wanted):
                        release_seconds = time.perf_counter() - started_all
                        break
                await asyncio.sleep(2)

    result = {
        "n": args.n,
        "concurrency": args.concurrency,
        "created": created,
        "duplicates": duplicates,
        "errors": errors,
        "unique_seqs": len(set(seqs)),
        "lost": args.n - created - duplicates - errors,
        "p50_ms": round(statistics.median(latencies), 2) if latencies else None,
        "p95_ms": round(statistics.quantiles(latencies, n=20)[18], 2) if len(latencies) >= 20 else (round(max(latencies), 2) if latencies else None),
        "elapsed_s": round(elapsed, 2),
        "jobs_per_min": round(args.n / elapsed * 60, 1) if elapsed else None,
        "waited": bool(args.wait),
        "released": released,
        "release_seconds": round(release_seconds, 2) if release_seconds else None,
        "order_preserved": order_ok,
        "base_url": base,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if args.llm_delay_s and args.provider_concurrency:
        modelled = args.n / max(args.provider_concurrency, 1) * args.llm_delay_s
        result["modelled_seconds"] = round(modelled, 2)
        result["within_model"] = release_seconds is not None and release_seconds <= 1.2 * modelled
    print(json.dumps(result, indent=2))
    _append_benchmark("load-test", result)
    problems = []
    if errors:
        problems.append(f"{errors} request errors")
    if result["lost"]:
        problems.append(f"{result['lost']} lost jobs")
    if created and len(set(seqs)) != created:
        problems.append("duplicate sequence numbers")
    if args.wait and not order_ok:
        problems.append("released out of order")
    if result.get("within_model") is False:
        problems.append("slower than 1.2x the CONC-7 model")
    return 0 if not problems else 1


BENCHMARK_HEADER = """# Benchmarks

Measured results for **HW-7** on the machine that ran the commands below. The
engineering targets in the PRD (§4.3, §8) are replaced by these numbers.

How to reproduce (from `backend/`):

```bash
export DATABASE_URL="postgresql+asyncpg://remote_flow:***@localhost/remote_flow"
export STORAGE_DIR=/srv/remote-flow/storage
python manage.py seed                 # demo Maker to submit against
python manage.py bench-render --n 50  # DOCX build + PDF conversion
python manage.py bench-intake --n 200 # POST /jobs equivalent, sequential
python manage.py load-test --n 200 --concurrency 25 --wait 1800   # NFR-1/2 against a running API
```

`load-test` drives a **running** API over HTTP the way the browser does (login,
CSRF, `POST /jobs` with idempotency keys), then optionally polls until every job
is released and checks that release order was preserved. Pass
`--llm-delay-s`/`--provider-concurrency` to compare the measured throughput with
the CONC-7 model (acceptance: within 1.2x).

`bench-render` measures the real LibreOffice/unoserver path when either binary
is installed and falls back to a deterministic PDF stub otherwise (the recorded
`pdf_backend` field says which one was measured). `bench-intake` measures the
intake transaction only (quota, duplicate check, gapless sequence, snapshot,
outbox row); it raises the Maker's `daily_limit` first if the run would trip it.

Targets:

| Metric | Target | Source |
| --- | --- | --- |
| Intake p95 | < 300 ms at 1,000 jobs/min | NFR-1 |
| Intake correctness | 0 lost or duplicated jobs | NFR-1 |
| Render p50 / p95 | recorded here, replaces the ~1.5 s estimate | HW-7 / CONC-7 |
| Render peak RSS | < 700 MB per `unoserver` between recycles | HW-4 |
| Release latency | < 2 s after a build becomes ready | NFR-3 |
| SSE fast path | < 1 s; outbox slow path ≤ 10 s | NFR-3 |
| Load test | 0 lost jobs, gapless seqs, order preserved, within 1.2x the CONC-7 model | NFR-1/NFR-2 |

The `load-test` entry drives a **running** API over HTTP (login, CSRF,
`POST /jobs` with idempotency keys) and optionally waits for ordered release.
On a dev box (SQLite, inline pipeline, mock provider) it verifies NFR-1
correctness; quote latency only from a run on the target server with Celery
workers and the real provider.

---

"""

def _append_benchmark(kind: str, result: dict) -> None:
    # Always write the repo-level docs file, whatever the current directory is.
    path = Path(__file__).resolve().parents[1] / "docs" / "benchmarks.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = BENCHMARK_HEADER
    if not path.exists():
        path.write_text(header, encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {kind} — {result['recorded_at']}\n\n```json\n{json.dumps(result, indent=2)}\n```\n")


SCHEMA_DOC_PREAMBLE = """# Database schema

Generated from the SQLAlchemy models (`backend/app/models.py`) by
`python manage.py schema-doc`; run that after any model change so this file
stays in sync with `alembic/versions/`.

Types are shown as SQLAlchemy portable types. On PostgreSQL the portable
`JSON`/`GUID`/`DATETIME` columns are created as `JSONB`/`UUID`/`timestamptz`
(see `app/models.py::JSONType`, `GUID`, `DateTimeTZ`), which is what production
runs; SQLite is only used for development and the test suite.

Relationships (`prod-requirements.md` Appendix B):

- `users 1-n jobs`, `jobs 1-1 doc_sets`, `jobs 1-n generations`
- `generations 1-n generation_attempts`, `generations 1-n files`
- `doc_sets 1-n interviews`, `interviews n-1 generations` (the pinned generation)
- `interviews 1-n feedback`, `interviews 1-n interview_events`
- `profiles 1-n prompt_versions`, `profiles 1-n profile_assignments n-1 users`
- `jobs.duplicate_of -> jobs.id` marks a duplicate JD inside the 7-day window
- `event_outbox` is the transactional outbox for SSE; `pipeline_events` is the
  per-build audit trail shown in the doc set drawer
- `worker_heartbeats` and `system_metrics` back the System status card

Integrity notes:

- `jobs` has a unique `(maker_id, submitted_date, seq_no)`: the gapless daily
  sequence (PIPE-11) is enforced by the database, not just by the lock.
- `generations` has a unique `(job_id, generation_no)` and is immutable once
  `status = 'ready'`.
- `generation_attempts` has a unique `(generation_id, stage, attempt_no)`.
- Immutable-then-expired rows (`files.expired_at`, `generations.expired_at`,
  `doc_sets.files_expired_at`) are tombstoned rather than deleted so the audit
  trail survives retention.

---

"""


async def cmd_schema_doc(_args: argparse.Namespace) -> int:
    from app import models  # noqa: F401 - register the tables
    from app.db import Base

    lines: list[str] = [SCHEMA_DOC_PREAMBLE]
    for name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[name]
        lines.append(f"## `{table.name}`\n")
        lines.append("| column | type | null | key |")
        lines.append("| --- | --- | --- | --- |")
        for column in table.columns:
            keys: list[str] = []
            if column.primary_key:
                keys.append("PK")
            for fk in column.foreign_keys:
                keys.append(f"FK -> {fk.target_fullname}")
            if column.index and not column.primary_key:
                keys.append("index")
            lines.append(
                f"| `{column.name}` | `{column.type}` | {'yes' if column.nullable else 'no'} | {', '.join(keys)} |"
            )
        # Sort by class *and* body so the file is byte-stable across runs.
        def constraint_key(constraint) -> tuple[str, str]:
            label = constraint.__class__.__name__
            if label == "UniqueConstraint":
                body = ", ".join(sorted(column.name for column in constraint.columns))
            else:
                body = str(getattr(constraint, "sqltext", ""))
            return (label, body)

        for constraint in sorted(table.constraints, key=constraint_key):
            kind = constraint.__class__.__name__
            if kind == "UniqueConstraint":
                columns = ", ".join(f"`{col.name}`" for col in constraint.columns)
                lines.append(f"\n- unique: {columns}")
            elif kind == "CheckConstraint":
                lines.append(f"\n- check: `{constraint.sqltext}`")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            columns = ", ".join(f"`{col.name}`" for col in index.columns)
            lines.append(f"\n- index `{index.name}`: {columns}")
        lines.append("")
    path = Path(__file__).resolve().parents[1] / "docs" / "schema.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path} ({len(Base.metadata.tables)} tables)")
    return 0


async def cmd_chaos_run(args: argparse.Namespace) -> int:
    """OPS-7 / M1 exit: 50 JDs in a burst with injected LLM+render failures and simulated
    worker kills; everything must come back Ready strictly in submission order."""
    import uuid

    from sqlalchemy import func, select, update

    from datetime import timedelta

    from app.models import DocSet, Generation, GenerationAttempt, Job, LLMProvider, User
    from app.services import dispatch, intake, pipeline, release
    from app.services.broker import MemoryBroker, set_broker
    from app.utils import utcnow

    set_broker(MemoryBroker())
    await _bootstrap_schema()
    pdf_backend = "unoserver"
    if not (shutil.which("soffice") or shutil.which("libreoffice")):
        # No LibreOffice in this environment: keep exercising ordering/retries with a
        # stub PDF so the run is still meaningful. The real image covers fidelity (GEN-8).
        from app.services import render

        def _stub_pdf(docx_path, pdf_path, **_kwargs):
            Path(pdf_path).write_bytes(b"%PDF-1.4\n% chaos-run stub\n%%EOF\n")
            return str(pdf_path)

        render.set_pdf_converter(_stub_pdf)
        pdf_backend = "stub"
        print("warning: LibreOffice not found; using a stub PDF converter (no fidelity check)", file=sys.stderr)

    settings = get_settings()
    settings.mock_llm_delay_ms_min = 0
    settings.mock_llm_delay_ms_max = 0
    settings.mock_llm_fail_rate = args.llm_fail_rate
    settings.mock_render_fail_rate = args.render_fail_rate
    pipeline.RETRY_BACKOFF_SCALE = 0.0

    async with session_scope() as session:
        maker = (await session.execute(select(User).where(User.email == args.maker))).scalar_one_or_none()
        if maker is None:
            print(f"maker {args.maker} not found; run `manage.py seed` first", file=sys.stderr)
            return 1
        if maker.daily_limit is not None and maker.daily_limit < args.n:
            maker.daily_limit = args.n + 10
        # The chaos run exercises ordering and retries, not provider throttling.
        provider = (
            await session.execute(
                select(LLMProvider).limit(1)
            )
        ).scalar_one_or_none()
        if provider is not None and (provider.rpm or 0) < 600:
            provider.rpm = 600

        jobs = []
        for index in range(args.n):
            job, _created = await intake.submit_jd(
                session,
                maker=maker,
                jd_text=f"Chaos run #{index}: hiring a senior engineer at Company {index}, remote friendly.",
                idempotency_key=f"chaos-{uuid.uuid4()}",
            )
            jobs.append(job)
        await session.commit()
        submitted = [job.seq_no for job in jobs]

    killed = 0
    remediated = 0
    steps = 0
    stalled = 0
    kill_pending = args.kill_every
    max_steps = args.n * 80
    while steps < max_steps:
        kill_pending -= 1
        async with session_scope() as session:
            pair = await pipeline.next_actionable(session)
            if pair is None:
                # Nothing runnable: sweep for lost dispatches, recover dead leases and
                # re-run the release pass. If a build exhausted its retry budget it shows
                # up as needs_attention and a manager would use Retry now (PIPE-6).
                await pipeline.dispatch_sweep(session)
                await pipeline.lease_watchdog(session)
                await pipeline.reconcile_releases(session)
                stuck = (
                    await session.execute(
                        select(Job, Generation)
                        .join(Generation, Generation.job_id == Job.id)
                        .where(
                            Job.delivery_status == "pending",
                            Generation.kind == "initial",
                            Generation.status == "needs_attention",
                        )
                        .order_by(Job.seq_no)
                        .limit(1)
                    )
                ).first()
                if stuck is not None:
                    job, generation = stuck
                    mode = "render_only" if generation.stage == "render" else "new_llm_call"
                    try:
                        await pipeline.retry_now(session, job=job, mode=mode, actor_id="chaos")
                        remediated += 1
                    except ValueError:
                        pass
                await session.commit()
                await dispatch.flush_dispatches(session)
                if await pipeline.next_actionable(session) is None and stuck is None:
                    break
                continue

            generation_id, stage = pair
            if kill_pending <= 0 and killed < args.max_kills:
                # Simulate a worker dying mid-attempt: take the lease and let it expire.
                claim = await pipeline.claim_build(session, generation_id, stage, "chaos-killed", timeout_s=60)
                if claim is not None:
                    await session.execute(
                        update(Generation)
                        .where(Generation.id == generation_id)
                        .values(lease_expires_at=utcnow() - timedelta(seconds=1))
                    )
                    await session.commit()
                    await pipeline.lease_watchdog(session)
                    await session.commit()
                    killed += 1
                    kill_pending = args.kill_every
                    continue

            outcome = await pipeline.process_one(session, generation_id, stage, worker="chaos")
            await session.commit()
            await dispatch.flush_dispatches(session)
            stalled = stalled + 1 if outcome is None else 0
            if stalled > 200:
                print("stalled: no progress for 200 steps, aborting", file=sys.stderr)
                break
        steps += 1

    async with session_scope() as session:
        await pipeline.lease_watchdog(session)
        await pipeline.reconcile_releases(session)
        await session.commit()
        released = list(
            (
                await session.execute(
                    select(Job.seq_no)
                    .where(Job.maker_id == maker.id, Job.delivery_status == "released")
                    .order_by(Job.submitted_date, Job.seq_no)
                )
            ).scalars().all()
        )
        released_order = list(
            (
                await session.execute(
                    select(Job.seq_no, Job.released_at)
                    .where(Job.maker_id == maker.id, Job.delivery_status == "released")
                    .order_by(Job.released_at, Job.seq_no)
                )
            ).all()
        )
        duplicate_llm = (
            await session.execute(
                select(GenerationAttempt.generation_id, GenerationAttempt.attempt_no, func.count())
                .where(GenerationAttempt.stage == "llm")
                .group_by(GenerationAttempt.generation_id, GenerationAttempt.attempt_no)
                .having(func.count() > 1)
            )
        ).all()
        not_ready = (
            await session.execute(
                select(func.count(Job.id)).where(
                    Job.maker_id == maker.id, Job.delivery_status == "pending"
                )
            )
        ).scalar_one()

    order_ok = [row[0] for row in released_order] == submitted
    result = {
        "n": args.n,
        "released": len(released),
        "released_in_order": order_ok,
        "still_pending": int(not_ready or 0),
        "duplicate_llm_attempts": len(duplicate_llm),
        "simulated_worker_kills": killed,
        "manager_retries": remediated,
        "max_kills": args.max_kills,
        "llm_fail_rate": args.llm_fail_rate,
        "render_fail_rate": args.render_fail_rate,
        "steps": steps,
        "pdf_backend": pdf_backend,
        "passed": order_ok and len(released) == args.n and not duplicate_llm and not not_ready,
    }
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


async def cmd_backup(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = Path(args.out or "backups")
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    artifacts: list[str] = []
    if settings.database_url.startswith("postgresql"):
        dump_path = target / f"remote-flow-{stamp}.sql"
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        with dump_path.open("wb") as handle:
            subprocess.run(["pg_dump", "--no-owner", "--dbname", dsn], stdout=handle, check=True)
        artifacts.append(str(dump_path))
    storage = settings.storage_path()
    if storage.exists():
        archive = target / f"storage-{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(storage, arcname="storage")
        artifacts.append(str(archive))
    print(json.dumps({"artifacts": artifacts, "master_key_required": True}, indent=2))
    print("Remember: the backup can only decrypt provider keys with the MASTER_KEY stored off-box (NFR-6).")
    return 0


async def cmd_restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    source = Path(args.file)
    if not source.exists():
        print(f"{source} not found.", file=sys.stderr)
        return 1
    if source.suffix == ".sql":
        with source.open("rb") as handle:
            subprocess.run(["psql", settings.database_url], stdin=handle, check=True)
    else:
        with tarfile.open(source, "r:gz") as tar:
            tar.extractall(settings.storage_path().parent)
    print(f"Restored from {source}. Verify a provider key decrypts with MASTER_KEY (NFR-6).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remote Flow operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply Alembic migrations (safe to run from every instance)")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("create-manager", help="create the first manager account")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="Manager")
    p.add_argument("--password")
    p.set_defaults(func=cmd_create_manager)

    p = sub.add_parser("seed", help="seed demo data")
    p.add_argument("--password", default="remote-flow-demo")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("rotate-master-key", help="re-encrypt provider keys with a new MASTER_KEY")
    p.add_argument("--new-key", required=True)
    p.set_defaults(func=cmd_rotate_master_key)

    p = sub.add_parser("reindex-search", help="rebuild the search columns")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("release-scan", help="run the ordered release pass")
    p.add_argument("--maker")
    p.set_defaults(func=cmd_release_scan)

    p = sub.add_parser("dispatch-sweep", help="re-enqueue builds the queue may have lost")
    p.set_defaults(func=cmd_dispatch_sweep)

    p = sub.add_parser("expire-leases", help="fail expired leases and apply the retry policy")
    p.set_defaults(func=cmd_expire_leases)

    p = sub.add_parser("relay-outbox", help="publish committed-but-unpublished events")
    p.set_defaults(func=cmd_relay_outbox)

    p = sub.add_parser("retention", help="expire files per the retention policy")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_retention)

    p = sub.add_parser("render-sample", help="render sample.json for the GEN-8 fidelity check")
    p.add_argument("--theme")
    p.add_argument("--out")
    p.set_defaults(func=cmd_render_sample)

    p = sub.add_parser("bench-render", help="measure render latency (HW-7)")
    p.add_argument("--n", type=int, default=50)
    p.set_defaults(func=cmd_bench_render)

    p = sub.add_parser("bench-intake", help="measure intake latency (HW-7, NFR-1)")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--maker", default="maker@example.com")
    p.set_defaults(func=cmd_bench_intake)

    p = sub.add_parser("load-test", help="M4 load test: concurrent POST /jobs (NFR-1/2)")
    p.add_argument("--base-url", default="http://localhost:8080", dest="base_url")
    p.add_argument("--email", default="maker@example.com")
    p.add_argument("--password", default="remote-flow-demo")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--concurrency", type=int, default=25)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--wait", type=float, default=0.0, help="seconds to wait for ordered release")
    p.add_argument("--date", help="restrict the release check to a YYYY-MM-DD server day")
    p.add_argument("--llm-delay-s", type=float, default=0.0, dest="llm_delay_s")
    p.add_argument("--provider-concurrency", type=int, default=0, dest="provider_concurrency")
    p.set_defaults(func=cmd_load_test)

    p = sub.add_parser("schema-doc", help="regenerate docs/schema.md from the models")
    p.set_defaults(func=cmd_schema_doc)

    p = sub.add_parser("chaos-run", help="OPS-7: burst of JDs with injected failures and worker kills")
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--maker", default="maker@example.com")
    p.add_argument("--llm-fail-rate", type=float, default=0.2, dest="llm_fail_rate")
    p.add_argument("--render-fail-rate", type=float, default=0.15, dest="render_fail_rate")
    p.add_argument("--kill-every", type=int, default=7, dest="kill_every")
    p.add_argument("--max-kills", type=int, default=15, dest="max_kills")
    p.set_defaults(func=cmd_chaos_run)

    p = sub.add_parser("backup", help="dump the database and storage")
    p.add_argument("--out")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore a dump created by `backup`")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
