# Benchmarks

Measured results for **HW-7** on the machine that ran the commands below. The
engineering targets in the PRD (§4.3, §8) are replaced by these numbers.

How to reproduce (from `backend/`):

```bash
export DATABASE_URL="postgresql+asyncpg://remote_flow:***@localhost/remote_flow"
export STORAGE_DIR=/srv/remote-flow/storage
python manage.py seed                 # demo Maker to submit against
python manage.py bench-render --n 50  # DOCX build + PDF conversion
python manage.py bench-intake --n 200 # POST /jobs equivalent, sequential
```

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

---


## intake — 2026-09-22T11:14:46.447547Z

```json
{
  "n": 60,
  "errors": 0,
  "created": 60,
  "p50_ms": 10.42,
  "p95_ms": 12.38,
  "recorded_at": "2026-09-22T11:14:46.447547Z"
}
```

## render — 2026-09-22T11:14:47.361768Z

```json
{
  "n": 30,
  "failures": 0,
  "p50_ms": 20.7,
  "p95_ms": 25.7,
  "peak_rss_mb": 115.6,
  "pdf_backend": "stub (LibreOffice not installed)",
  "environment": {
    "python": "3.12.3",
    "storage_dir": "/tmp/rf-bench-storage"
  },
  "recorded_at": "2026-09-22T11:14:47.361768Z"
}
```

---

## Environment notes

The numbers above were recorded on the development box that hosts this
repository (Python 3.12.3, 2 vCPU class, SQLite `DATABASE_URL`), where
LibreOffice is **not** installed: the render run measured the DOCX build plus
the deterministic PDF stub, so the `pdf_backend` field says
`stub (LibreOffice not installed)`.

Re-run both benchmarks on the target server after `deploy/host-prep.sh` so the
recorded render p50/p95 and peak RSS reflect the real `unoserver` path (HW-7):

```bash
cd backend
python manage.py bench-render --n 50    # LibreOffice present -> pdf_backend "unoserver"
python manage.py bench-intake --n 200
```

Note that `--n 200` intake submissions trip the Maker's `daily_limit` unless it
is raised first (Settings → Users → daily limit, or seed a benchmark Maker).

The `load-test` run above drove the development box's API directly over HTTP
(SQLite, single-process inline pipeline, mock provider with zero delay): it
verifies NFR-1 correctness (200/200 created, unique gapless sequence numbers,
0 lost, release order preserved) but its p95 reflects SQLite write
serialisation and 25-way client concurrency, not the target 2 vCPU /
PostgreSQL server. Re-run it on the target server with Celery workers and the
real provider before quoting the numbers:

```bash
python manage.py load-test --n 200 --concurrency 25 --wait 900
```


## load-test — 2026-09-22T11:40:02.946050Z

```json
{
  "n": 200,
  "concurrency": 25,
  "created": 200,
  "duplicates": 0,
  "errors": 0,
  "unique_seqs": 200,
  "lost": 0,
  "p50_ms": 306.43,
  "p95_ms": 799.51,
  "elapsed_s": 3.3,
  "jobs_per_min": 3635.4,
  "waited": true,
  "released": 200,
  "release_seconds": 163.85,
  "order_preserved": true,
  "base_url": "http://127.0.0.1:8123",
  "recorded_at": "2026-09-22T11:40:02.946050Z"
}
```
