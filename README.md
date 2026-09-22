# Remote Flow

Self-hosted resume-tailoring pipeline for a small recruiting team. Makers paste
job descriptions, an LLM rewrites a Profile's resume against each JD, the
vendored generator renders DOCX + PDF + TXT, and results are released to each
Maker **strictly in submission order**. Managers watch queues, fix builds that
need attention and manage providers/themes/users; reviewers only see the
interview generation they were assigned.

The full product requirements are in `prod-requirements.md`. This repository is
the implementation: FastAPI + Postgres/Redis/Celery backend, React SPA,
Docker Compose deployment.

## Repository layout

```
backend/     FastAPI app, Celery workers, pipeline services, Alembic migrations, operator CLI
  app/routers        HTTP surface (/api/v1)
  app/services       pipeline, release, render, retention, events, storage, LLM adapters
  app/workers        Celery app + beat schedule
  vendor/resume_builder   vendored DOCX/PDF generator (GEN-1)
  tests              pytest suite (unit, integration, end-to-end API)
frontend/    Vite + React 19 + TypeScript SPA (Tailwind, TanStack Query, SSE)
deploy/       Docker Compose stack, Caddyfile, render image, host prep, backup/restore
docs/         runbook.md (OPS-5), benchmarks.md (HW-7)
```

## Quick start (development, no Docker)

Requirements: Python 3.12+, Node 20+. SQLite is enough for day-to-day work; the
mock LLM provider means no API key and no network calls.

```bash
# backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,llm]"
AUTO_CREATE_SCHEMA=true SEED_ON_START=true .venv/bin/uvicorn app.main:app --reload --port 8000

# frontend (second terminal; Vite proxies /api to :8000)
cd frontend
npm install
npm run dev            # http://localhost:5173
```

Seeded accounts (password `remote-flow-demo`):

| Email | Role | Landing page |
| --- | --- | --- |
| `manager@example.com` | manager | Resumes / ops |
| `maker@example.com`, `maker2@example.com` | maker | JD upload |
| `reviewer@example.com` | reviewer | Interviews |

There is no separate admin account: the login page's **Administrator** button sends
`as_admin=true`, which authenticates the same users but only accepts `role = manager`
(and always lands on `/resumes`). Use `manager@example.com` for it.

`make dev` wraps the API + inline pipeline driver; `make dev-web` runs the SPA;
`make dev-workers` runs the three Celery queues against a local Redis. Without
Redis the API still works — the broker falls back to in-process fan-out and
`make dev` drives the pipeline inline.

Useful make targets: `setup`, `migrate`, `seed`, `dev`, `dev-web`, `dev-workers`,
`test`, `lint`, `chaos`, `bench`, `build`, `up`, `down`, `logs`, `backup`.

## How the pipeline works

1. **Intake (PIPE-11).** `POST /api/v1/jobs` validates the JD, enforces the
   Maker's daily limit, allocates the next gapless `seq_no` for the day under a
   per-Maker lock, freezes a Profile/provider/theme snapshot into a `generation`,
   writes the JD to storage and emits an outbox event — all in one transaction.
   Repeating an `Idempotency-Key` returns the same job.
2. **LLM stage.** A worker claims the generation with a lease
   (`FOR UPDATE SKIP LOCKED` on Postgres), calls the provider, validates/repairs
   the JSON and stores the artifacts. Failures are classified
   (provider/JSON/schema) and retried with backoff; three consecutive provider
   errors switch to the fallback provider.
3. **Render stage.** The renderer builds DOCX, converts to PDF with a warm
   `unoserver` (falling back to cold `soffice`), writes TXT + `llm.json` and
   publishes the whole folder with an atomic rename into `gen-{n}/`. The
   generation is immutable from then on.
4. **Ordered release (ORD).** When an *initial* build becomes ready the release
   pass walks from the Maker's cursor (lowest `(date, seq_no)` still pending) and
   releases every ready job behind it in order. A stuck build holds later ready
   jobs with a "Waiting for #003" status; **Skip** removes it and releases the
   rest. Regenerations never touch delivery status.
5. **Realtime.** Every mutation writes an event to the `event_outbox` table in
   the same transaction; a fast path publishes immediately and a relay retries
   anything older than 2 s. Browsers get SSE (`/api/v1/events`) with
   `Last-Event-ID` replay plus a 10 s polling fallback.

States, retry budgets, block codes and the full endpoint list are in
`prod-requirements.md` §4 and §7.

## Configuration

Everything comes from environment variables (`backend/app/config.py`, sample in
`deploy/.env.example`). The important ones:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./remote_flow.db` | use `postgresql+asyncpg://…` in production |
| `REDIS_URL` | unset | broker/SSE/semaphores; in-process fallback when unset |
| `STORAGE_DIR` | `./storage` | JD files, generation folders, attempt artifacts |
| `MASTER_KEY` | unset | base64 32 bytes; **required** in production (AES-256-GCM provider keys) |
| `SECRET_KEY` | dev value | JWT signing; set a long random value in production |
| `PUBLIC_URL` / `SITE_ADDRESS` / `COOKIE_SECURE` | localhost | CORS, Caddy site, cookie flags |
| `LLM_CONCURRENCY` / `RENDER_CONCURRENCY` | 16 / 1 | worker sizing (CONC-2, HW-1) |
| `MAX_LLM_ATTEMPTS` / `MAX_RENDER_ATTEMPTS` | 10 / 3 | retry budgets before `needs_attention` |
| `MOCK_LLM_FAIL_RATE` / `MOCK_RENDER_FAIL_RATE` | 0 | failure injection for chaos runs (OPS-7) |
| `AUTO_CREATE_SCHEMA` / `SEED_ON_START` | false | dev conveniences; production uses Alembic |
| `MANAGER_IP_ALLOWLIST` | empty | SEC-9: comma-separated IPs/CIDRs allowed to use manager tools |

Settings that operators change at runtime (disk thresholds, retention windows,
daily limits, pause/resume, LLM providers, themes) live in the database and are
edited in Settings → General/Retention/Providers/Themes.

## API notes

- Base path `/api/v1`; prefix-less `GET /healthz`, `GET /readyz`, `GET /metrics`.
- Auth uses httpOnly cookies: `rf_access` (15 min), `rf_refresh` (7 days) and
  `rf_csrf`. Mutations must send `X-CSRF-Token` matching the CSRF cookie;
  refresh is single-flight and rotates the refresh token.
- Errors always use `{"error": {"code", "message", "details"}}`.
- Roles: `maker` (own jobs/doc sets), `manager` (everything except reviewer
  feedback authoring), `reviewer` (assigned interviews and the pinned
  generation only). Object-level checks live in the routers and are covered by
  `backend/tests/test_api.py`.

## Testing

```bash
make test              # pytest + SPA typecheck/build
make test-backend      # cd backend && .venv/bin/python -m pytest -q
make test-frontend     # cd frontend && npm run build

cd backend
.venv/bin/python manage.py chaos-run --n 50   # OPS-7 burst with failures + worker kills
.venv/bin/python manage.py bench-render --n 30
.venv/bin/python manage.py bench-intake --n 60
```

```bash
make load-test        # M4 load test against a running API (NFR-1/2)
```

`manage.py load-test` is the M4 load test: it logs in over HTTP, fires 200
concurrent `POST /jobs` with idempotency keys, verifies that no job was lost or
duplicated, then optionally waits and checks ordered release. CI
(`.github/workflows/ci.yml`) runs the suites plus `pip-audit`/`npm audit`.

The suite covers ordering (ORD-1…5, 8, 9), claims/leases, retries, snapshot
freezing, intake quotas and gapless sequences, the outbox relay, retention
protection, permissions, generation pinning, a golden test of the real DOCX
generator, and an end-to-end API flow (submit → pipeline → ordered release →
ZIP download). LibreOffice is not required: `render.set_pdf_converter` is
swapped for a deterministic stub in tests.

## Deployment

```bash
cp deploy/.env.example deploy/.env     # then fill in MASTER_KEY, SECRET_KEY, POSTGRES_PASSWORD
sudo deploy/host-prep.sh               # OPS-8: docker, 2 GB swap, firewall, storage dir
make build && make up                  # docker compose up -d
make logs
```

The stack runs Caddy (TLS + SPA + `/api` proxy), FastAPI (2 uvicorn workers),
three Celery workers (`llm`, `render`, `ops` + beat), Postgres 16 and Redis 7,
with the CPU/memory limits from HW-1/HW-2 and the Linux tuning from HW-3…HW-6.
Nightly backups: schedule `deploy/backup.sh`; restore with
`deploy/restore.sh <backup-dir>` and keep `MASTER_KEY` off-box.

Operations, incident procedures and the restore drill are in
`docs/runbook.md`; measured render/intake numbers are in `docs/benchmarks.md`.
