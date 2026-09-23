# Remote Flow — Product Requirements Document

| | |
|---|---|
| **Version** | 0.4 (implementation baseline candidate) |
| **Date** | 2026-09-22 |
| **Product owner** | bugman-007 |
| **Audience** | Engineers building Remote Flow; reviewers of scope |

**How to read this document.** Requirements carry IDs such as `JD-3` so they can be referenced in issues and PRs. Priorities: **P0** = required for v1 launch, **P1** = should ship in v1 or immediately after, **P2** = later. Section 1.5 lists decisions (owner-confirmed ones are marked); §3.4 is the resource budget for the actual server; section 12 lists what still needs an answer. Appendix A is the review of the existing doc generator, Appendix B the LLM output schema, Appendix C the theme schema.

**Changelog**

- **0.4 (2026-09-22)** — Second external review applied. **Two-level state model:** a job's *delivery status* (pending → released | skipped, drives ordering, never moves backwards) is separated from the *processing status of each generation build*, so Regenerate can never re-enter the release queue. **Atomic claims with leases** replace "re-read and exit" (exactly one worker can own an attempt; late results from an expired lease are rejected). Attempt numbers never reset; Manager retries grant a fresh retry *budget*. **Render failures never trigger an automatic LLM call** (owner decision) — a new LLM call is always an explicit Manager action. **Snapshots frozen at build creation:** prompt version, provider/model/params and the **complete theme parameters**; automatic retries reuse them. **One intake transaction** under the Maker's row lock covers idempotency, quota, sequence allocation and job creation; the allocated `(date, seq)` key is the ordering key everywhere. **Transactional event outbox** + periodic client refresh so a crash between commit and publish cannot leave an open page stale. New **`worker-ops`** service (embedded Beat + `ops` queue) so sweeps, watchdog, reconciliation and the outbox relay never queue behind LLM/render work. **Retention** rewritten to actually expire single-generation doc sets, protect interview-pinned generations, and pause intake on critical disk. `MASTER_KEY` recovery added to backups. Capacity example for 5,000 submissions added; Maker-facing ETA (P1).
- **0.3 (2026-09-22)** — First external review + server spec (2 vCPU / 6 GB): stage-aware retries, immutable generations with interview pinning, durable dispatch, static React SPA, resource budget, warm `unoserver` P0, performance numbers marked as targets, milestones as sequence + effort.
- **0.2 (2026-09-21)** — Owner answers applied; `resume_builder.py` reviewed (Word COM → LibreOffice; theme schema; `job_description.txt`; `make_names()` naming). Appendices A–C added.
- **0.1** — Initial draft.

---

## Table of contents

1. [Overview](#1-overview)
2. [Roles and permissions](#2-roles-and-permissions)
3. [System architecture](#3-system-architecture)
4. [Generation pipeline](#4-generation-pipeline)
5. [Functional requirements by page](#5-functional-requirements-by-page)
6. [Data model](#6-data-model)
7. [API surface](#7-api-surface)
8. [Non-functional requirements](#8-non-functional-requirements)
9. [Security and privacy](#9-security-and-privacy)
10. [Deployment and operations](#10-deployment-and-operations)
11. [Milestones](#11-milestones)
12. [Open questions](#12-open-questions)
- [Appendix A — Review of `resume_builder.py`](#appendix-a--review-of-resume_builderpy)
- [Appendix B — LLM output JSON schema](#appendix-b--llm-output-json-schema)
- [Appendix C — Theme schema](#appendix-c--theme-schema)

---

## 1. Overview

### 1.1 What Remote Flow does

Remote Flow turns pasted job descriptions (JDs) into tailored resume document sets (PDF + DOCX + TXT) using an LLM and the existing Python document generator (`resume_builder`), delivers them to the person who submitted the JD in real time and in submission order, and routes the best results into an interview workflow.

End-to-end flow:

1. A **Manager** creates products ("Profiles"), each with its own prompt, theme and optional LLM settings, and assigns a Profile to one or many **Makers**. Each Maker has a daily submission limit.
2. A **Maker** pastes JD text into the **JD Upload** page, many times in quick succession (100–200 per day is normal).
3. For every JD the backend creates a job and its first **generation build**. An **LLM worker** sends the JD plus the Profile's prompt to the configured LLM provider and receives a resume JSON.
4. A **Render worker** runs the **doc generator** on that JSON, producing `<Name>_<Role>.docx`, `<Name>_<Role>.pdf` and `job_description.txt` as an immutable **generation**, stored on the server's local disk.
5. Finished document sets are **released** to the Maker's **Resumes** page in strict submission order, pushed live over Server-Sent Events. Makers download single files or ZIPs.
6. The Manager reviews document sets, marks good ones **Selected**, and on the **Interviews** page assigns them to **Reviewers** (interviewers) with meeting details. Reviewers see only their assigned interviews and record feedback afterwards.

### 1.2 Goals

- **G1 — Burst-safe.** Hundreds of JDs may arrive within minutes from up to 50 Makers. Nothing is lost and nothing is processed twice — not on an API crash, a worker crash, or a Redis restart; throughput is limited only by the LLM provider's rate limits and the server's rendering capacity.
- **G2 — Ordered delivery.** A Maker's document sets become available strictly in the order the JDs were submitted, even though LLM latency varies and individual calls fail. The only exceptions are listed in ORD-9.
- **G3 — Manager-configurable without code changes.** Prompts, LLM providers and API keys, document themes, user accounts, limits and interview forms are all edited in the UI — and changes never alter work already in flight.
- **G4 — Real-time.** Makers see progress and results as they happen, without refreshing.
- **G5 — Fully self-hosted on modest hardware.** Everything (web, API, workers, database, files, PDF conversion) runs on the owner's single Ubuntu server: 2 vCPU, 6 GB RAM (§3.4).

### 1.3 Non-goals for v1

- Public sign-up, or self-service password reset by email (no outbound email in v1).
- Editing generated documents in the browser (the desktop GUI's "edit the preview before saving" feature is not reproduced; see 12).
- Multi-tenancy (one organisation per deployment).
- Native mobile apps (the web UI must be usable on a tablet, desktop is primary).
- Applying to jobs or integrating with ATS/job boards.

### 1.4 Glossary

| Term | Meaning |
|---|---|
| **JD** | Job description text pasted by a Maker. One JD → one Job → one Doc set. |
| **Job** | One submission. Owns the JD text, the per-Maker **ordering key** `(submitted_date, seq_no)` and the **delivery status** (`pending` → `released` \| `skipped`). Delivery status drives ordering and never moves backwards. |
| **Generation build / Generation** | One attempt to produce a content version for a job: an LLM stage followed by a render stage, with its own processing status, retry counters and frozen configuration snapshot. Once `ready` it is an immutable generation (PDF + DOCX + TXT + the JSON that produced it). A job's first build is its **initial generation**; Regenerate and retry-after-skip create further builds. |
| **Attempt** | One execution of one stage of one build. Attempt numbers only ever increase. |
| **Lease** | Ownership of an attempt by one worker, with an expiry. Only the lease holder may write results. |
| **Doc set** | The user-facing face of a job: names (company, title, candidate), current generation, selection flag. Created together with the job; names are filled when the initial generation is ready. |
| **Profile** | A product configuration: name, URL, prompt, theme, dates, LLM override, reviewer-visible fields. Assigned to one or many Makers. |
| **Prompt version** | An immutable snapshot of a Profile's prompt. Every build records which version it used. |
| **Theme** | A named styling configuration (font, size, colours, spacing, margins — Appendix C). Defined in Settings, assigned to Profiles; **copied in full onto every build** at creation. |
| **Provider** | An LLM API account (Anthropic, OpenAI, Google, OpenRouter, custom OpenAI-compatible endpoint…) with its API key, configured in Settings. |
| **Release** | The moment a job's delivery status becomes `released` and its initial generation becomes visible/downloadable to its Maker. Gated by ordering. |
| **Blocker** | The Maker's oldest job with delivery status `pending`. Nothing behind it is released until its initial generation is `ready` (or a Manager skips it). |
| **Selected** | A Manager flag on a Doc set meaning "good enough to use for an interview". Selected Doc sets appear on the Interviews page. |
| **Interview** | A Selected Doc set — pinned to one generation — assigned to a Reviewer with meeting details, built from an **Interview form template**. |
| **Reviewer** | Also called *interviewer*. Sees only assigned interviews; writes feedback. |
| **Seq no** | Per-Maker, per-day sequence number of a JD (`#001`, `#002`…), allocated inside the intake transaction. Human-readable identifier used in the UI, on disk, and as the ordering key. |
| **Doc generator** | The existing `resume_builder.py` (python-docx). Its pure core is reused by the render worker; its GUI and Word-COM PDF path are not (Appendix A). |

### 1.5 Decisions

Rows marked **✔ confirmed** were approved by the owner. Others are proposals that can be changed by editing this table.

| Topic | Decision | Why | Status |
|---|---|---|---|
| Multi-file download | Single file → direct download. Whole doc set, multiple doc sets, or "download all for date" → **one ZIP streamed on the fly**. Directory choice is left to the browser's "Ask where to save" setting. | Browsers cannot save several files to a chosen folder in one go. A Chromium-only "Save to folder…" (File System Access API) is P2. | ✔ confirmed 2026-09-21 |
| Ordering and failures | Strict per-Maker ordering. **A failed build is retried automatically and everything behind it waits**; no job is ever released ahead of an earlier one. After the per-stage retry budget the build enters **Needs attention** (still blocking) and the Manager is alerted; only a Manager can **Skip** it. The two documented exceptions are in ORD-9. | Owner requirement: order matters more than latency. The budget and Skip exist so one permanently broken JD cannot freeze a Maker forever without anyone noticing. | ✔ confirmed 2026-09-21 |
| Retry granularity | **Stage-aware, and render failures never trigger an LLM call.** A render failure retries only the render using the stored JSON; after the render budget the build needs attention. A new LLM call is always an explicit Manager action (*Retry now → new LLM call*, or Regenerate). | Repeating a paid LLM call for a converter problem wastes money, time and changes content; the owner does not want automatic re-calls. | ✔ confirmed 2026-09-22 |
| Two-level state | **Delivery status on the job** (`pending` → `released` \| `skipped`) is separate from the **processing status of each generation build**. Ordering reads only delivery status; builds never write it except through the release pass (initial build) or the documented late-release path. | Regenerate and retries can never put a delivered job back into the queue, by construction. | proposed (0.4) |
| Ownership of work | A worker takes an attempt with an **atomic compare-and-set claim** that issues a **lease token**; all result writes are conditional on the token; expired leases are failed by the watchdog and late writers are rejected. | Celery is at-least-once; two workers can otherwise both run the same paid call. | proposed (0.4) |
| Frozen configuration | Prompt version, provider/model/params and the **full theme parameters** are copied onto the build when it is created. **Automatic retries reuse the snapshot**; only a Manager's *new LLM call* or Regenerate re-resolves from current configuration. Provider fallback (PIPE-7) is the single automatic exception and is logged. | A Manager editing a theme or prompt must not change work already queued or a render retry. | proposed (0.4) |
| Intake transaction | Idempotency check, daily quota, `seq_no` allocation, and creation of job + doc set + initial build happen in **one transaction holding the Maker's user row lock**. No Redis counters in the intake path. | Gapless, race-free sequence numbers and quota; the allocated key is what ordering uses. | proposed (0.4) |
| Event delivery | State changes write an **outbox row in the same transaction**; a fast path publishes after commit, an `ops` relay publishes anything still unpublished; clients also refresh visible lists every 60 s. | A crash between commit and publish must not leave an open page stale. | proposed (0.4) |
| Maintenance execution | Sweeps, watchdog, reconciliation, outbox relay, retention and stats run on a dedicated **`ops` queue** consumed by **`worker-ops`** (which also embeds Beat). | Recovery work must never wait behind a generation backlog. | proposed (0.4) |
| Immutable generations | Every `ready` build is immutable, in its own folder. A doc set points at its **current** generation; **interviews pin the generation they were created with**; moving an interview to a newer generation is an explicit Manager action. | Regenerate must never silently change the documents an interviewer has already read. | proposed (0.3) |
| Submission limit | **Daily limit stored per Maker** (`users.daily_limit`), editable on the Users page and from the Profiles assignment panel. Resets at midnight in the configured server timezone. `null` = unlimited, `0` = blocked. | Owner: limit is a Maker property, not a Profile property. | ✔ confirmed 2026-09-21 |
| Profile ↔ Maker | **One Profile → many Makers.** Each Maker has **at most one active Profile** at a time; assigning a new one ends the previous assignment. Makers without an assignment cannot submit. | The prompt used for a JD must be unambiguous; several Makers can work the same product. | ✔ confirmed 2026-09-21 |
| Company name / job title | Doc set company = `target_company`; job title = `title` (or the part of `subtitle` before `·`/`|`); candidate name = `name` — the same fields the generator's `make_names()` uses. Manager can rename a doc set. | Fields already exist in the JSON. | ✔ confirmed 2026-09-21 |
| "Login as admin" | Same email/password form; the button asserts the account must have the **manager** role, otherwise shows an error. Plain "Login" works for every role and routes by role. | | ✔ confirmed 2026-09-21 |
| Makers see "Selected" badge; Reviewers don't see Maker identity | As stated. | | ✔ confirmed 2026-09-21 |
| Frontend | **React SPA (Vite + TypeScript, TanStack Router/Query/Table, Tailwind, shadcn/ui) built to static files and served by Caddy.** | An authenticated dashboard needs no SSR or SEO; a static SPA is one fewer process on a 2-core server. | proposed (0.3) |
| Which LLM is used | Settings defines Providers and a **system default provider + model**. A Profile may **override** provider/model/parameters. The build snapshots the final choice. | Manager control at both levels; reproducibility per build. | proposed |
| Provider abstraction | Thin internal `LLMClient` interface implemented with the **LiteLLM SDK** (library, not proxy), keys loaded per call from the database. | One code path for 100+ providers; keys stay in our DB; replaceable if LiteLLM disappoints. | proposed |
| Job queue | **Celery** with **Redis** broker, queues `llm`, `render`, `ops`; priority enabled so **retries jump ahead** of fresh submissions; `acks_late`, `prefetch_multiplier=1`. The database, not the queue, is the source of truth. | A retried blocker must not queue behind 500 new JDs. | proposed |
| PDF conversion | **LibreOffice headless** through a **warm `unoserver` per render process** (P0 on this hardware). The generator's Word-COM path stays available for the Windows GUI only. | Word COM cannot run on Ubuntu; DOCX stays the single source of truth; a cold `soffice` start per document costs CPU a 2-core box cannot spare. | proposed — needs owner sign-off on visual fidelity (12 #1) |
| TXT file | `job_description.txt`, produced by the generator's `compose_job_info()` from `job_description`, `company_information`, `job_link`. The **system injects the original JD text** as `job_description`. | Matches the existing generator; saves output tokens and avoids the model truncating a long JD. | proposed |
| Hardware | Target is the owner's **2 vCPU / 6 GB** Ubuntu server. Render concurrency starts at **1**; all numbers in §4.3/§8 are targets until the M0 benchmark replaces them. Scale-out path: a second render box. | | ✔ spec confirmed 2026-09-22; budget proposed |
| First manager account | Created by a CLI command (`manage.py create-manager`) during deployment. | There is no sign-up. | proposed |

---

## 2. Roles and permissions

Three fixed roles: **maker**, **manager**, **reviewer**. A user has exactly one role. Every API endpoint enforces role checks server-side; the UI only hides what the role cannot do.

| Capability | Maker | Manager | Reviewer |
|---|:-:|:-:|:-:|
| Log in with email + password | ✓ | ✓ | ✓ |
| Submit JDs (JD Upload page) | ✓ | – | – |
| See own daily limit/usage and estimated completion | ✓ | ✓ (all) | – |
| View / download **own** doc sets (current generation) | ✓ | – | – |
| View / download **all** doc sets, all generations, JD text, LLM JSON, attempt logs | – | ✓ | – |
| View / download the **pinned generation** of assigned interviews | – | ✓ | ✓ |
| See retry status of own blocked jobs | ✓ | ✓ | – |
| **Retry now** (render only / new LLM call), **Skip** a blocking build, **Cancel** a regeneration build | – | ✓ | – |
| **Regenerate** a doc set (new generation), move an interview to another generation, **Keep** a doc set beyond retention | – | ✓ | – |
| Mark / unmark doc set as Selected | – | ✓ | – |
| Search & filter doc sets | own | all | – |
| Create / edit interview form templates | – | ✓ | – |
| Create / edit / cancel interviews | – | ✓ | – |
| View interviews | – | all | assigned only |
| Write feedback on an interview | – | – (read) | ✓ (own) |
| Profiles: create / edit / archive, prompt versions | – | ✓ | – |
| Assign Profiles to Makers | – | ✓ | – |
| Set a Maker's daily limit | – | ✓ | – |
| View Profile details | never | full | shared fields only, via an interview |
| Settings: providers, API keys, themes, general, retention | – | ✓ | – |
| Users: create, edit role, deactivate, reset password | – | ✓ | – |
| System status (queues, workers, leases, outbox, disk, builds needing attention) | – | ✓ | – |

Navigation per role (sidebar):

- **Maker:** JD Upload · Resumes
- **Manager:** Resumes · Interviews · Profiles · Settings · Users
- **Reviewer:** Interviews

---

## 3. System architecture

### 3.1 Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend | **React 19 + TypeScript SPA built with Vite**; TanStack Router, Query and Table; Tailwind CSS; shadcn/ui | Built to static assets and served by Caddy. No Node process at runtime. SSE client with reconnect and periodic refresh. |
| Backend API | **Python 3.12 + FastAPI**, SQLAlchemy 2 (async), Alembic migrations, Pydantic v2 | Same language as the doc generator, imported directly. |
| Background jobs | **Celery 5** + **Redis 7** (priority queues, `acks_late`, `prefetch_multiplier=1`) | Queues `llm` (I/O bound), `render` (CPU bound), `ops` (maintenance). Beat embedded in `worker-ops`. |
| Real-time | **Server-Sent Events** from FastAPI, fan-out through Redis Pub/Sub + Streams, fed by a **transactional outbox** | One SSE stream per logged-in browser tab; falls back to polling if SSE is blocked. |
| Database | **PostgreSQL 16** | Source of truth for all state; full-text search (`tsvector`); row locks for intake, advisory locks for the release pass, compare-and-set claims for workers. |
| File storage | **Local filesystem** on the VPS, mounted as a Docker volume (e.g. `/srv/remote-flow/storage`) | Files served only through authenticated API endpoints, never as static files. |
| LLM access | **LiteLLM SDK** behind an internal `LLMClient` interface | Provider-native structured output where available; Pydantic validation always. |
| Doc generator | `resume_builder` core (python-docx) as a vendored package + **LibreOffice via `unoserver`** for DOCX→PDF | See 4.6 and Appendix A for the required refactor. |
| Auth | Email + password (Argon2id), JWT access token (15 min) + rotating refresh token (7 days) in `HttpOnly; Secure; SameSite=Lax` cookies | No third-party IdP in v1. |
| Reverse proxy / TLS / static | **Caddy** (automatic Let's Encrypt), serves the SPA and proxies `/api/*` | SSE needs `flush_interval -1`. |
| Packaging | **Docker Compose** with per-service memory limits and CPU shares (§3.4) | One `docker compose up -d` on the Ubuntu server. |
| Observability | Structured JSON logs to stdout, `/healthz` endpoints, Prometheus-style `/metrics`, optional self-hosted GlitchTip | Manager-visible "System status" card in Settings. |

### 3.2 Services (Docker Compose)

| Service | Image / build | Role | Concurrency on 2 vCPU | Memory limit |
|---|---|---|---|---|
| `caddy` | `deploy/Dockerfile.caddy` (multi-stage: node builds `frontend/`, assets copied into `caddy:2`) | TLS, static SPA, reverse proxy to `api` | — | 128 MB |
| `api` | `backend/` | FastAPI (uvicorn) | 2 workers | 512 MB |
| `worker-llm` | `backend/` | Celery worker, queue `llm`, gevent pool | 1 process × 16 greenlets | 512 MB |
| `worker-render` | `deploy/Dockerfile.render` (`backend/` + LibreOffice + fonts + `unoserver`) | Celery worker, queue `render`, prefork, `nice 10`; one warm `unoserver` per process | **1** process (raise to 2 only after the M0 benchmark shows API p95 unaffected) | 1.5 GB |
| `worker-ops` | `backend/` | `celery worker -Q ops -B -c 2 -P gevent`: embedded Beat + executor for dispatch sweep, lease watchdog, release reconciliation, outbox relay, retention, disk check, stats | 2 greenlets, reserved for short tasks only | 192 MB |
| `postgres` | postgres:16 | Database (`shared_buffers=768MB`, `max_connections=40`, `work_mem=8MB`) | — | 1.5 GB |
| `redis` | redis:7 | Broker, pub/sub, streams, rate-limit counters (`maxmemory 200mb`, `noeviction`, AOF on) | — | 256 MB |

Volumes: `pgdata`, `redisdata`, `storage` (documents), `caddy_data`. Sum of limits ≈ 4.6 GB, leaving ≈ 1.4 GB for the OS and page cache.

### 3.3 High-level diagram

```
 Browser (Maker / Manager / Reviewer)
    │  HTTPS: static SPA + /api/* (REST + SSE)
    ▼
 ┌──────────┐  /api/*   ┌──────────┐   SQL (intake txn, claims, outbox)   ┌────────────┐
 │  caddy   │──────────▶│   api    │─────────────────────────────────────▶│  postgres  │
 │ (SPA+TLS)│           │ (FastAPI)│                                      └────────────┘
 └──────────┘           └──────────┘                 ▲ claims / release pass / outbox
                          │ enqueue after commit     │
                          ▼                          │
                     ┌──────────┐         ┌──────────┴──────┐
                     │  redis   │◀───────▶│  worker-llm     │──▶ LLM provider(s)
                     └──────────┘         └─────────────────┘
                       ▲  │ enqueue (render)
                       │  ▼
                       │ ┌────────────────────────────┐   writes   ┌─────────────────────┐
                       │ │ worker-render              │───────────▶│ /storage (local FS) │
                       │ │ resume_builder.core (docx) │            └─────────────────────┘
                       │ │ + unoserver/LibreOffice    │
                       │ └────────────────────────────┘
                       │ ┌────────────────────────────┐
                       └─│ worker-ops (Beat + ops q)  │ dispatch sweep · lease watchdog · release
                         └────────────────────────────┘ reconciliation · outbox relay · retention
```

### 3.4 Target hardware and resource budget

Owner's server (confirmed 2026-09-22): **2 vCPU, 6 GB RAM**, Ubuntu. Disk size unknown (12 #11).

| Rule | Detail |
|---|---|
| **HW-1 (P0)** CPU priority | Interactive paths win: `cpu_shares` api 1024, postgres 1024, worker-llm 512, worker-ops 512, worker-render 256; the render process additionally runs under `nice -n 10`. Render concurrency defaults to 1. |
| **HW-2 (P0)** Memory limits | Per-service limits as in §3.2; OOM-killing a worker is recoverable (leases expire, attempts are retried), OOM-killing Postgres is not, hence the headroom. |
| **HW-3 (P0)** Swap | 2 GB swapfile, `vm.swappiness=10`, as a safety net for LibreOffice spikes — not as working memory. |
| **HW-4 (P0)** LibreOffice hygiene | `unoserver` restarted every 200 conversions or when its RSS exceeds 700 MB (LibreOffice leaks); a conversion that exceeds 60 s kills and restarts it. |
| **HW-5 (P0)** Postgres | `shared_buffers=768MB`, `effective_cache_size=2GB`, `work_mem=8MB`, `max_connections=40` (api pool 10, each worker 5). |
| **HW-6 (P0)** Redis | Broker data must never be evicted: `maxmemory-policy noeviction`; SSE replay streams capped at 500 entries per user; no result backend (results live in Postgres). |
| **HW-7 (P0)** Benchmark first | M0 runs `manage.py bench-render --n 50` and `bench-intake` on this server and records p50/p95 render time, peak RSS and API p95 under load. The numbers in §4.3 and §8 are replaced by the measurements. |
| **HW-8 (P1)** Scale-out | When measured render throughput is below the LLM throughput at peak, add a second box running only `worker-render` (shares Redis/Postgres/storage over the private network) rather than upgrading the main server. |

Expected steady-state footprint: caddy 30 MB · api 250 MB · worker-llm 200 MB · worker-render 500–800 MB (Python + soffice) · worker-ops 100 MB · postgres ~1 GB · redis 100 MB ≈ **2.5–3 GB**, comfortable within 6 GB with the limits above.

---

## 4. Generation pipeline

This section is the heart of the product; the page specs in section 5 depend on it.

### 4.1 State model

Two independent levels:

```
 JOB — delivery status (ordering; never moves backwards)

   pending ─────────────────── release pass (initial build ready & job is the cursor) ──▶ released
      │                                                                                     ▲
      └── Manager: Skip ──▶ skipped ── Manager: Retry → new build → ready ── late release ──┘ (ORD-9 #1)


 GENERATION BUILD — processing status (one per content version; kind = initial | retry_after_skip | regenerate)

   queued ──atomic claim──▶ llm_running ──▶ rendering ──atomic claim──▶ ready
     ▲                          │ fail             │ fail
     │                          ▼                  ▼
     │                 retry_wait (llm)    retry_wait (render)      automatic, backoff, next_retry_at,
     │                          │                  │                re-enqueued with HIGH priority
     └──────────────────────────┘                  └──────────▶ rendering   (same llm.json — NO new LLM call)
                                │ retry budget exhausted (per stage)
                                ▼
                         needs_attention ── Manager: Retry now (render only | new LLM call)
                                         ── Manager: Skip (initial build → job skipped)
                                         ── Manager: Cancel (regenerate builds only) ──▶ cancelled
```

| Level | Status | Meaning | Blocks later jobs? |
|---|---|---|---|
| job | `pending` | Not yet delivered. Its initial build is somewhere in the build states. | yes, if it is the cursor |
| job | `released` | Delivered. Terminal. | no |
| job | `skipped` | A Manager removed it from the order. Terminal for ordering; a later build may still deliver it late (ORD-9). | no |
| build | `queued` | Created; waiting for an LLM worker's claim. | via job |
| build | `llm_running` | An LLM attempt holds a lease. | via job |
| build | `rendering` | A render attempt holds a lease. `llm.json` is stored. | via job |
| build | `retry_wait` | An attempt of `stage` failed; the next automatic attempt is scheduled at `next_retry_at`. | via job |
| build | `needs_attention` | Retry budget of the current stage exhausted; a Manager must act. | via job |
| build | `ready` | Files written and verified; immutable from here on. | no |
| build | `cancelled` | Skipped initial build or cancelled regeneration. | no |

A **Maker-facing status** is derived: `released` → *Ready* (with a generation badge if > 1); `skipped` → *Skipped*; `pending` → from the initial build: *Queued*, *Generating*, *Rendering*, *Retrying (n)*, *Waiting for a manager*, or *Waiting for #NNN* when the build is `ready` but the job is not the cursor.

### 4.2 Pipeline rules

- **PIPE-1 (P0) — Events through an outbox.** Every job or build state change writes a `pipeline_events` row and an `event_outbox` row **in the same transaction**. After commit the writer publishes the outbox row to Redis (fast path) and marks it published; the `ops` relay (every 5 s) publishes any row still unpublished after 2 s (slow path). Rows are deleted after 24 h.
- **PIPE-2 (P0) — Atomic claim and lease.** A worker never trusts the queue message. It takes work with a single conditional update:
  `UPDATE generations SET status=<running>, claimed_by=:worker, lease_token=gen_random_uuid(), lease_expires_at=now()+:lease WHERE id=:id AND stage=:stage AND status IN ('queued','retry_wait') AND (next_retry_at IS NULL OR next_retry_at<=now()) RETURNING lease_token`,
  and inserts the `generation_attempts` row (next `attempt_no`) in the same transaction. Zero rows → another worker owns it → the task exits without side effects. **Every later write** (store `llm.json`, move to `rendering`, mark `ready`, record failure) is conditional on `lease_token`; zero rows → the lease expired → the worker discards its result (raw output is still saved under `attempts/` for debugging) and stops. Lease length = stage timeout + 30 s. Provider slots (CONC-3) are acquired **before** the claim so waiting for a slot cannot burn lease time; if the claim then fails the slot is released.
- **PIPE-3 (P0) — Durable dispatch.** Builds are created with `dispatch_state=pending`; the creator enqueues after commit and sets `sent`. The `ops` **dispatch sweep** (every 30 s) re-enqueues builds that are `pending`, `queued`/`retry_wait` and due but not claimed within 2 minutes of dispatch, or whose stage advanced without a dispatch. Duplicate enqueues are harmless because claims are atomic. Celery runs `acks_late=True`, `reject_on_worker_lost=True`, `prefetch_multiplier=1`. The database is the source of truth; the queue is a hint.
- **PIPE-4 (P0) — Stage-aware automatic retry policy (no automatic LLM re-calls).**
  - **LLM stage** failures (provider 429/5xx/network/timeout, or JSON still invalid after 2 in-call *repair* rounds): backoff 5 s, 15 s, 45 s, 2 min, 5 min, then 10 min, until the LLM retry budget (`max_llm_attempts`, default **10**, ≈ 1 hour) is exhausted → `needs_attention`. `Retry-After` is honoured.
  - **Render stage** failures (generator exception, `unoserver`/LibreOffice error or timeout, missing/empty output file): retry **the render only**, re-using the stored `llm.json`, backoff 5 s, 15 s, 45 s, until the render retry budget (`max_render_attempts`, default **3**) is exhausted → `needs_attention`. **The pipeline never calls the LLM again on its own because rendering failed.**
  - Every retry is enqueued with **high priority** so it runs before fresh submissions.
- **PIPE-5 (P0) — Lease watchdog.** The `ops` watchdog (every 30 s) finds attempts whose `lease_expires_at` has passed, marks them `timed_out`, and applies PIPE-4 to the build. Stage timeouts: `llm_timeout_s` default 600, `render_timeout_s` default 180 (GEN-7 caps a single conversion at 60 s).
- **PIPE-6 (P0) — Needs attention.** Managers get a banner on Resumes and a counter in System status; the Maker sees *Waiting for a manager* (for initial builds). Manager actions:
  - **Retry now → render only**: same `llm.json`, same snapshot; grants a fresh render budget.
  - **Retry now → new LLM call**: re-resolves prompt version, provider/model and theme from *current* configuration onto the same build (logged as a snapshot change), grants a fresh LLM budget.
  - **Skip** (initial builds): job → `skipped`, build → `cancelled`, release pass runs.
  - **Cancel** (regenerate builds): build → `cancelled`; nothing else changes.
  Attempt numbers are never reset; a budget is "attempts of that stage since `retry_budget_reset_at`".
- **PIPE-7 (P1) — Fallback provider.** After 3 consecutive *provider* errors on the same build, remaining LLM attempts use the fallback provider/model if one is configured. This is the only automatic change to a build's snapshot and is recorded on the attempt. Invalid-JSON failures do not switch provider.
- **PIPE-8 (P0) — Generations and Regenerate.** A `ready` build is an immutable generation in `gen-{n:02d}/`. When a job's **initial** build becomes ready, the doc set's names are filled and the release pass runs (§4.3). **Regenerate** (Manager, only on `released` jobs) creates a build of kind `regenerate` with a fresh snapshot from current configuration; when it becomes ready the doc set's `current_generation_id` moves to it. Regenerate builds **never touch delivery status**, never participate in the cursor, and their failures show in the Manager's needs-attention list as *regeneration — not blocking*. Interviews keep their pinned generation (INT-3); *Update to generation n* is an explicit, logged action.
- **PIPE-9 (P0) — Frozen snapshots.** At build creation the system resolves and copies: `prompt_version_id` (Profile's active version), `provider_id`/`model`/`llm_params` (Profile override → system default), `theme_id` **and the complete theme parameters** (`theme_snapshot`). Automatic retries of either stage use the snapshot unchanged. Editing a Profile, prompt, theme or provider afterwards affects only builds created later.
- **PIPE-10 (P0) — Attempt artifacts.** Every LLM attempt stores `llm_request.json`, `llm_response.raw`, validation errors and error code under `attempts/llm-{n}/`; failed render attempts store `error.log` under `attempts/render-{n}/`.
- **PIPE-11 (P0) — Intake transaction.** `POST /jobs` runs one transaction: `SELECT … FROM users WHERE id=:maker FOR UPDATE` (serialises the Maker) → look up `idempotency_key`; if found, return the existing job (HTTP 200, same `seq_no`) → check active Profile assignment → count today's jobs `< daily_limit` else HTTP 429 → `seq_no = coalesce(max(seq_no),0)+1` for `(maker, today)` → insert `jobs` (delivery `pending`), `doc_sets` (placeholder), the initial `generations` build (`queued`, snapshots per PIPE-9), outbox row → commit → enqueue (PIPE-3). Redis is not involved in intake.

### 4.3 Ordered release

- **ORD-1 (P0)** Ordering key = `(submitted_date, seq_no)` allocated in PIPE-11; `submitted_at` is informational. Each Maker's **cursor** is the smallest key with delivery status `pending`. That job is the **blocker**. A job is released only when it is the cursor **and** its initial build is `ready`.
- **ORD-2 (P0) — Transactional release pass.** When an initial build becomes `ready`, or a job is skipped, the worker opens one transaction, takes `pg_advisory_xact_lock(hashtext(maker_id))`, walks from the cursor forward setting `released` while each job's initial build is `ready` (writing `released_at`, events and outbox rows), and commits. The lock serialises concurrent passes for the same Maker and cannot be left dangling by a crash.
- **ORD-3 (P0)** **Failures block.** A cursor job whose initial build is `retry_wait` or `needs_attention` holds everything behind it; later ready builds show *Waiting for #NNN*.
- **ORD-4 (P0)** Ordering is per Maker only. Makers never wait on each other.
- **ORD-5 (P0)** **Skip** (Manager) sets the cursor job to `skipped` and runs the release pass, so everything ready behind it releases immediately, in order.
- **ORD-6 (P0)** Maker-facing status of held jobs names the blocker and its state: *Waiting for #012 (retrying, attempt 3)* / *Waiting for #012 (needs a manager)*.
- **ORD-7 (P0)** Release latency after the blocker's build is ready must be < 2 s.
- **ORD-8 (P0) — Reconciliation.** The `ops` task (every 60 s) runs the release pass for every Maker whose cursor job has a `ready` initial build older than 10 s. This catches a crashed pass. `manage.py release-scan` runs the same logic on demand.
- **ORD-9 (P0) — The only exceptions to "in submission order":**
  1. **A skipped job that a Manager later retries** creates a `retry_after_skip` build; when it is ready the job is set directly to `released` with `released_late=true` — after its neighbours, by the Manager's explicit decision. The row keeps its seq no and is labelled *released late*.
  2. **Regenerate** changes which generation is *current* for an already released position. Delivery status is not involved.
  Delivery status never moves from `released`/`skipped` back to `pending`; there is no setting that disables ordering.

Worked example: a Maker submits #001…#100 five seconds apart. #001's LLM call fails twice (429) and succeeds on the third attempt at ~80 s; #002's build was ready at ~35 s but the job stayed *Waiting for #001 (retrying, attempt 2)*. At ~85 s #001's build is ready; the release pass releases #001, #002 and every other ready job in order within one transaction. If #007's PDF conversion fails, #007 retries the render three times with the same JSON and no LLM call; if that still fails the Manager sees *#007 needs attention* and chooses *Retry now → render only*, *Retry now → new LLM call* (e.g. after fixing the prompt), or *Skip*. #008…#100 wait throughout.

### 4.4 Concurrency and throughput

- **CONC-1 (P0)** Fresh builds are enqueued FIFO by ordering key at normal priority; retries and Manager "Retry now" use high priority (Celery `priority` on the Redis broker, `queue_order_strategy = priority`).
- **CONC-2 (P0)** `worker-llm` runs many concurrent tasks (default 16 greenlets in one process; I/O bound). `worker-render` runs **1** process on the target server (HW-1) with a warm `unoserver`. `worker-ops` runs only short maintenance tasks. Queues are independent so a backlog in one never starves another.
- **CONC-3 (P0)** Each Provider has `max_concurrency` (default 8) and `requests_per_minute` (default 60) enforced with a Redis semaphore and token bucket. A task acquires its slot **before** claiming the build (PIPE-2) and waits in place (bounded, with jitter) rather than re-queuing.
- **CONC-4 (P0)** HTTP 429 responses respect `Retry-After` and additionally reduce the provider's effective concurrency by 25% for 60 s (simple adaptive backoff).
- **CONC-5 (P1)** Prompt caching is enabled when the provider supports it (e.g. Anthropic `cache_control` on the system prompt, OpenAI automatic). The long Profile prompt is placed first and unchanged; the JD goes last.
- **CONC-6 (P0)** Token usage (input, cached input, output) and provider latency are stored per attempt for cost reporting.
- **CONC-7 (P0) — Capacity model (targets until HW-7 measures them).** Wall-clock for a burst of *N* jobs ≈ `max(N·L/C_llm, N·R/C_render)` where *L* = mean LLM latency, *C_llm* = effective provider concurrency, *R* = mean render time, *C_render* = render concurrency. On the target server with R ≈ 1.5 s (to be measured) and C_render = 1, rendering handles ≈ 2,400 docs/hour, so bursts are **LLM-bound**: 200 jobs at L = 30 s, C_llm = 8 → ≈ 12.5 min; **5,000 jobs at the same rates → ≈ 5.2 hours** (≈ 1.3 h at C_llm = 32). Fast acceptance therefore does not mean fast completion: the provider tier and `max_concurrency` are the levers, and Makers are shown an estimate (JD-11). Intake target: ≥ 1,000 `POST /jobs`/min without errors, verified by `bench-intake`.

### 4.5 LLM request contract

- **LLM-1 (P0)** Request composition: `system` = Profile prompt (snapshotted version) + a system-appended, developer-maintained **output contract** (Appendix B schema, the `[highlight]…[/highlight]` bold-span convention, "no Markdown links", "do not include `job_description`"); `user` = the JD text. Provider-native structured output (OpenAI `response_format: json_schema`, Anthropic tool/structured output, Gemini `response_schema`) is used when available; otherwise JSON mode + strict validation. Strict-mode quirks (e.g. OpenAI requiring every property in `required`) are handled inside the LiteLLM adapter, not in the prompt.
- **LLM-2 (P0)** The JSON must validate against Appendix B. Required: `name`, `title`, `target_company`, `summary`, `experience` (≥ 1 item with `title`, `company`, `bullets`). Missing or empty required fields trigger a repair round (PIPE-4).
- **LLM-3 (P0)** Provider/model/params, prompt version and theme come from the build's snapshot (PIPE-9), never from live configuration.
- **LLM-4 (P0)** The exact prompt sent, the raw provider response and the parsed JSON are stored per attempt (PIPE-10). Managers can view them; Makers cannot.
- **LLM-5 (P1)** Profile page has **Test prompt**: paste a sample JD, run once, see JSON + rendered files, without creating a Maker-visible doc set.
- **LLM-6 (P0)** Before the JSON is stored as the build's `llm.json`, the system sets `job_description` to the original JD text (the model is told not to produce it) and normalises legacy aliases the generator tolerates (`contact{}`, `skills[]`, `role`, `duration`, `university`, `field_of_study`, `company`/`company_name`) into the canonical field names. Every render attempt of the build uses that stored file.

### 4.6 `LLMClient` interface

```python
class LLMClient(Protocol):
    async def generate_json(
        self, *, provider: ProviderConfig, model: str,
        system: str, user: str, json_schema: dict,
        temperature: float, max_tokens: int, timeout_s: int,
    ) -> LLMResult:  # .json (dict) .raw (str) .usage (tokens) .latency_ms .finish_reason
```

Implemented by `LiteLLMClient`. Supported provider types in v1: `anthropic`, `openai`, `azure_openai`, `google_gemini`, `openrouter`, `openai_compatible` (custom `base_url`), plus `mock` for development. Adding a type = adding a mapping entry, no UI change.

### 4.7 Doc generator integration (`resume_builder`)

The generator exists as a single 1,000-line file combining a pure document model, DOCX building, Word-COM PDF export, a Tkinter GUI and a CLI (full review in Appendix A). The server uses only the pure parts. **Before anything else in the project, the generator is run on the Ubuntu server through LibreOffice and its output approved (GEN-8) — this is the first task of M0.**

- **GEN-1 (P0) — Refactor into a package** (done once, in the generator's own repo, keeping the desktop GUI working):
  - `resume_builder/core.py`: `build_blocks(data)`, `build_docx(blocks, opts, out_path)`, `make_names(data)`, `compose_job_info(data)`, `DEFAULTS`, highlight/link helpers. **No I/O other than the given `out_path`, no `style.json`, no import-time side effects.**
  - `resume_builder/pdf.py`: `convert_docx_to_pdf(docx_path, pdf_path, *, backend)` with backends `unoserver` (Linux server), `soffice` (cold-start fallback) and `word` (existing `WordWorker`, Windows GUI). The `WordWorker` singleton and `atexit` hook move behind the `word` backend and are created lazily.
  - `resume_builder/gui.py`, `resume_builder/cli.py`: unchanged behaviour; CLI gains `--theme theme.json`.
  - Vendored into `backend/vendor/resume_builder` (git subtree or pinned wheel) with a version string recorded in every `meta.json`.
- **GEN-2 (P0) — Render attempt flow:** claim (PIPE-2) → load the build's stored `llm.json` and `theme_snapshot` → `blocks = build_blocks(data)` → `build_docx(blocks, {**DEFAULTS, **theme_snapshot}, tmp/<base>.docx)` → `convert_docx_to_pdf(... backend="unoserver")` → `job_description.txt` from `compose_job_info(data)` (never empty because of LLM-6) → verify three non-empty files, SHA-256, write `meta.json` → atomic rename of the temp folder to `gen-{n:02d}/` → conditional update to `ready` (lease-checked) → release pass if this is an initial build.
- **GEN-3 (P0) — PDF backend on the server.** Each render process starts one `unoserver` (LibreOffice kept warm) on start-up and converts through `unoconvert`. A conversion exceeding 60 s kills and restarts the instance; HW-4 recycling applies. If `unoserver` is unavailable the attempt falls back once to a cold `soffice --headless --convert-to pdf` with a private `-env:UserInstallation` profile, then fails the attempt.
- **GEN-4 (P0) — Fonts.** The generator's defaults are Windows fonts (Calibri…). The render image installs metric-compatible substitutes and fontconfig aliases: Calibri→Carlito, Cambria→Caladea, Arial/Helvetica→Liberation Sans, Times New Roman→Liberation Serif, Courier New→Liberation Mono, Georgia→Gelasio, plus DejaVu and Noto Sans as fallbacks. The DOCX keeps the Windows font name (so it renders natively when opened in Word); the PDF uses the substitute. The Theme font picker lists only fonts present in the render image (`GET /system/fonts`), labelled *Calibri (rendered as Carlito in PDF)*.
- **GEN-5 (P0)** The generator core must be deterministic and process-safe: no shared temp files, no global mutable state, no writes outside `out_path`. Verified by running the golden test (GEN-8) under parallel processes.
- **GEN-6 (P0)** Theme schema = Appendix C (the keys of `DEFAULTS` with the GUI's ranges). Settings → Themes renders a form from it; values are validated on save and the validated object is what gets snapshotted onto builds.
- **GEN-7 (P0)** Time budget 60 s per conversion, `render_timeout_s` (180) per attempt; exceeding either fails the attempt with `error_code=render_timeout` (retried per PIPE-4, render stage).
- **GEN-8 (P0) — Fidelity sign-off.** `sample.json` rendered through the LibreOffice path on the actual server is compared side by side with a Word-exported reference; the owner approves before M0 exits. Known risks to check: full-page background colour (`w:background`), right-aligned tab stops, section bottom borders, justified body text, font metrics/page count.
- **GEN-9 (P0)** The Windows GUI remains a supported manual tool (Manager can paste a JSON from the detail drawer into it); it is not part of the server deployment.

### 4.8 Storage layout

```
/storage/
  makers/{maker_id}/{YYYY-MM-DD}/{seq:03d}-{company_slug}/
      jd.txt                             # original pasted text
      attempts/llm-{n}/llm_request.json  # every LLM attempt of every build, including failed ones
      attempts/llm-{n}/llm_response.raw
      attempts/llm-{n}/error.json
      attempts/render-{n}/error.log      # failed render attempts only
      gen-01/                            # immutable generation 1 (initial build)
          llm.json                       #   validated + normalised JSON used for this generation
          meta.json                      #   snapshot (prompt version, provider, model, theme params),
                                         #   generator + LibreOffice versions, timings, hashes
          {Name}_{Role}.docx             #   from make_names(), e.g. Luis-Angel-Salazar_Senior-Full-Stack-Developer.docx
          {Name}_{Role}.pdf
          job_description.txt            #   compose_job_info()
      gen-02/                            # created by Regenerate or retry-after-skip; gen-01 is never modified
  tmp/                                   # in-progress renders (cleaned by the ops watchdog)
```

- **STO-1 (P0)** Folder names are system-generated (`{seq:03d}-{company_slug}`) so they sort by submission order; file names inside follow the generator's `make_names()` so downloads look exactly like the desktop tool's output. ZIP downloads use one folder per doc set named `{seq:03d}_{Company}_{Role}` and contain the requested generation (current by default; the pinned one for interview downloads).
- **STO-2 (P0)** Slugs are sanitised (`dashify()` + length cap 80 chars + reserved-name guard); user or model text never forms a path segment directly (path traversal guard).
- **STO-3 (P0)** The `files` table stores path, size and SHA-256 for each artifact per generation; downloads verify the row belongs to a doc set/generation the caller may access.
- **STO-4 (P0) — Retention (files only; database rows are never deleted).** Run daily by `ops`; every deletion is logged; the UI shows *expired* where files are gone.
  - `attempt_artifacts_retention_days` (default **30**): `attempts/` folders (raw requests/responses, error logs).
  - `superseded_generation_retention_days` (default **90**): generations that are neither current nor pinned by any interview.
  - `docset_retention_days` (default **365**, counted from submission): **all** generations of a doc set — including a single, current one — unless the doc set is **Selected**, has the Manager's **Keep** flag, or has an interview pinning a generation (then only pinned generations are kept).
  - `interview_retention_days` (default **365**, counted from the meeting time): pinned generations of completed/cancelled interviews.
  - Selected/Keep doc sets never expire automatically; System status lists their count and size so the Manager can prune deliberately.
- **STO-5 (P0) — Disk thresholds** (checked every 5 min by `ops`): ≥ 85 % → warning banner for Managers; ≥ 90 % → **intake paused automatically** (JD Upload shows a maintenance message, in-flight builds finish); ≥ 95 % → rendering paused as well so partial writes cannot fill the disk. The Manager can resume once usage drops below 85 %, or force-resume after acknowledging the risk.

### 4.9 Real-time sync

- **RT-1 (P0)** `GET /api/events` is an authenticated SSE stream. Events carry `id` (monotonic per user), `event` type and JSON `data`. Clients reconnect with `Last-Event-ID`; the server replays up to the last 500 events per user from a Redis stream.
- **RT-2 (P0)** Event types: `job.status` (id, seq, derived status, stage, attempt, blocker info), `job.released`, `build.needs_attention`, `docset.regenerated`, `docset.selected`, `limit.updated`, `intake.paused`/`intake.resumed`, `interview.assigned`, `interview.updated`, `feedback.added`, `system.notice`.
- **RT-3 (P0)** Makers receive events only for their own jobs; Managers receive all job, build and interview events; Reviewers receive events for their interviews.
- **RT-4 (P0)** Fallback: if the SSE connection fails twice in a row, the client polls the relevant list endpoint every 10 s and shows a "Live updates unavailable" indicator.
- **RT-5 (P0)** End-to-end latency from state change to UI update < 1 s under normal load (fast path); ≤ 10 s after a crash between commit and publish (outbox relay).
- **RT-6 (P0)** Events are **notifications, not state**: the UI re-fetches the affected list on reconnect and on any event whose payload is insufficient. A lost event can delay a UI update, never corrupt it.
- **RT-7 (P0)** Events originate from the transactional outbox (PIPE-1); the fast path and the relay are idempotent (same event id), so a client may see a duplicate but never a gap.
- **RT-8 (P0)** Even with a healthy SSE connection, every list view silently re-fetches its current page every 60 s and whenever the tab regains focus.

---

## 5. Functional requirements by page

### 5.1 Login page

**Who:** everyone. **Route:** `/login`.

UI: email input, password input, buttons **Login** and **Login as admin**, inline error area. No sign-up link, no "forgot password" link (v1 shows "Contact your manager to reset your password").

- **AUTH-1 (P0)** Credentials are verified against active users only; deactivated users get the same generic error as wrong passwords.
- **AUTH-2 (P0)** **Login** succeeds for any role and redirects: maker → `/jd-upload`, manager → `/resumes`, reviewer → `/interviews`.
- **AUTH-3 (P0)** **Login as admin** performs the same authentication but rejects the session with "This account is not a manager" unless `role = manager`; on success redirects to `/resumes`.
- **AUTH-4 (P0)** Rate limiting: after 5 failed attempts for an email or IP within 15 minutes, further attempts are blocked for 15 minutes (HTTP 429 with a friendly message).
- **AUTH-5 (P0)** Sessions: access JWT 15 min + refresh token 7 days (sliding), both in `HttpOnly` cookies; refresh tokens are rotated and revocable; "Log out" and Manager-initiated "Revoke sessions" invalidate them.
- **AUTH-6 (P1)** `must_change_password` flag forces a password change screen on first login with a Manager-issued password.
- **AUTH-7 (P0)** Passwords hashed with Argon2id; minimum 10 characters; no other composition rules.

### 5.2 JD Upload page (Maker)

**Who:** maker only. **Route:** `/jd-upload`.

UI: a large paste box (monospace, auto-grow, character counter), **Submit** button (also `Ctrl/Cmd+Enter`), a usage indicator "Today: 37 / 100", an estimate line (JD-11), and a **Recent submissions** panel (today's last 20, with seq no, company name once known, status chip).

- **JD-1 (P0)** Submitting sends the text with a fresh `Idempotency-Key` (PIPE-11), returns `{job_id, seq_no}` within 300 ms p95, and the client **clears the box** and focuses it again so the next paste can start immediately. If the network drops after the request was sent, the client retries with the **same** key, so a double submission cannot occur.
- **JD-2 (P0)** Submit is disabled while a request is in flight and re-enabled on response; rapid successive submissions (every 1–5 s) must never be dropped or merged.
- **JD-3 (P0)** Validation: trimmed length between `min_jd_chars` (default 50) and `max_jd_chars` (default 200,000, effectively unlimited for a pasted JD); whitespace-only is rejected; error shown inline without clearing the box. The client reads both bounds from `GET /me/limit` so a Manager can widen them in Settings (SET-12).
- **JD-4 (P0)** **Daily limit** (`users.daily_limit`) is enforced inside the intake transaction (PIPE-11). On reaching the limit the button is disabled, the indicator turns red, and the server returns HTTP 429 `limit_reached`. Limit changes by the Manager take effect immediately and are pushed via `limit.updated`. Skipped and retried jobs still count once each (one submission = one count).
- **JD-5 (P0)** If the Maker has no active Profile assignment the page shows "Your account is not yet configured. Contact your manager." and submission is rejected with `no_profile_assigned`. If intake is paused (STO-5 / SET-14) the page shows the maintenance message and submission is rejected with `intake_paused`.
- **JD-6 (P0)** The Maker never sees the Profile's name, prompt or any Profile field. The API response for makers contains no Profile data.
- **JD-7 (P0)** Seq no is allocated inside the intake transaction (PIPE-11): gapless per Maker per day and identical to submission order.
- **JD-8 (P1)** Duplicate detection: if the SHA-256 of the normalised text equals an earlier submission by the same Maker within 7 days, the submission is accepted but flagged `duplicate_of` and the Recent panel shows a "Duplicate of #012" badge. Managers can filter duplicates.
- **JD-9 (P0)** Recent submissions update live through SSE (`job.status`, `job.released`).
- **JD-10 (P2)** Bulk paste: detect a delimiter (`-----` on its own line) and offer "Split into N submissions".
- **JD-11 (P1)** **Estimated completion**: "≈ 18 min until your latest submission is ready" computed from the Maker's queue position, current global queue depth and the rolling mean LLM latency / effective provider concurrency (CONC-7). Shown on JD Upload and Resumes; labelled as an estimate.

### 5.3 Resumes page

**Who:** maker (own), manager (all). Reviewers have no access. **Route:** `/resumes`.

#### 5.3.1 Common

- **RES-1 (P0)** **Date selector** (calendar + "Today" / "Yesterday" quick buttons; Managers additionally get a date range). Default = today. The list shows doc sets whose JD was submitted on that date (server timezone day).
- **RES-2 (P0)** List columns: seq no, company name, job title, status chip, submitted time, ready time, three file chips (PDF · DOCX · TXT) for the **current generation**, each with **Preview** (PDF/TXT inline viewer, DOCX = download only) and **Download**, checkbox for multi-select, row menu. A small `gen 2` badge appears when a doc set has more than one generation.
- **RES-3 (P0)** Default sort: seq no ascending (submission order). Sort toggles: seq, company, status, ready time.
- **RES-4 (P0)** Rows appear the moment a JD is submitted and show the derived Maker-facing status (§4.1): `Queued` → `Generating` → `Rendering` → `Waiting for #NNN` → `Ready`; failure states `Retrying (n)`, `Waiting for a manager` (Manager wording: *Needs attention*), `Skipped`, `Released late`. Only `Ready` rows have file chips enabled.
- **RES-5 (P0)** Downloads: a single file chip downloads that file directly. Row menu **Download set** and the toolbar **Download selected (n)** / **Download all for this date** produce one ZIP streamed by the server (no temp file), named `{maker}_{YYYY-MM-DD}_{seq}_{Company}.zip` or `{maker}_{YYYY-MM-DD}_selection.zip`, containing one folder per doc set (STO-1) with the three files of the current generation. Only `Ready` sets are included; the UI says how many were skipped.
- **RES-6 (P0)** **Search** box: matches company name, job title, candidate name, seq no (`#042` or `42`) and JD text (Postgres full-text). Makers search within their own sets; Managers across all.
- **RES-7 (P0)** Status filter chips: All · Ready · Processing · Retrying · Needs attention · Skipped · Expired · Selected (Selected visible to Makers as a read-only badge; see RES-12).
- **RES-8 (P0)** Live updates: new rows, status changes, releases and selection changes arrive over SSE without refresh, plus the RT-8 periodic refresh. A subtle "n new" indicator appears if the user has scrolled away from the insertion point.
- **RES-9 (P0)** A retrying or needs-attention row shows the last error in plain words ("Model returned invalid JSON", "Provider rate-limited", "PDF conversion failed") and, for Managers, the actions **Retry now → Render only / New LLM call**, **Skip**, **View last response**. Makers see the status only.
- **RES-10 (P1)** Doc set detail drawer: JD text (read-only), **generations list** (number, kind, created, by whom, which interviews pin it, current marker, expiry date) with per-generation files, timeline of events and attempts per stage with lease/claim info, and (Manager only) the LLM JSON (copyable — usable in the desktop GUI), the snapshot (prompt version, provider/model, theme params), token usage and latency per attempt.
- **RES-11 (P2)** Keyboard navigation (j/k, space to select, d to download) for power users going through hundreds of rows.

#### 5.3.2 Maker specifics

- **RES-12 (P0)** Makers can see whether one of their doc sets was marked **Selected** by the Manager (badge only). Settings flag `show_selection_to_makers` (default ON) can hide it.
- **RES-13 (P0)** Makers see only their own doc sets, enforced by the API (`maker_id = current_user`), and only the current generation.

#### 5.3.3 Manager specifics

- **RES-14 (P0)** Additional filters: **Maker** (multi-select), **Profile**, **date range**, **Selected only**, **Duplicates only**, **Provider/model**, **Needs attention only** (initial / regeneration), **Has multiple generations**, **Kept**.
- **RES-15 (P0)** **Select / Unselect** toggle per row and bulk on the current selection. Selected doc sets appear on the Interviews page (5.4) and are exempt from retention. Unselecting a doc set that already has an interview requires confirmation and does not delete the interview.
- **RES-16 (P0)** **Per-Maker stats bar** for the chosen date range: submitted, ready, skipped, selected, **success rate = selected ÷ ready** (shown as % with counts), average time to ready, average LLM attempts, tokens used. Sortable table of makers; clicking a maker applies the filter.
- **RES-17 (P0)** Row menu (Manager): Rename company/job title, Retry now (render only / new LLM call), Skip, **Regenerate** (creates the next generation with a fresh snapshot; shows "k interview(s) keep generation n" before confirming), **Keep** (exempt from retention), View JD, View LLM JSON, Download set (choose generation).
- **RES-18 (P1)** Export the current filtered list as CSV (seq, maker, date, company, title, status, attempts, generations, selected, times, tokens).
- **RES-19 (P0)** **Needs-attention banner** at the top for Managers: "3 builds are blocking 2 makers · 1 regeneration failed" with one-click filters. Plus a "Processing now" strip: queue depths, in-flight LLM calls per provider, in-flight renders, oldest waiting job, intake paused indicator.

### 5.4 Interviews page

**Who:** manager (all), reviewer (assigned). Makers have no access. **Route:** `/interviews`.

#### 5.4.1 Manager view

Two tabs: **Selected doc sets** (Selected, no interview yet) and **Interviews** (scheduled/completed/cancelled).

- **INT-1 (P0)** Selected doc sets tab lists company, job title, maker, profile, selected date, with filters (maker, profile, date range) and search (company/title). Action: **Schedule interview**.
- **INT-2 (P0)** **Interview form templates** (managed from a "Form templates" button on this page): a template has a name and an ordered list of fields. Field types: `text`, `textarea`, `datetime`, `date`, `time`, `url`, `email`, `phone`, `number`, `select` (options), `multiselect`, `checkbox`. Each field: label, key (auto-slug), required, help text, default, **visible to reviewer** (default yes). Managers can add, edit, reorder, delete fields and create several templates; one is the default. Built-in, non-deletable fields: Reviewer (user picker, reviewers only), Meeting time (datetime with timezone), Meeting link (url), Location (text), Notes (textarea).
- **INT-3 (P0)** **Schedule interview** opens the default template (switchable); on save the interview stores the **template snapshot + values** and **pins the doc set's current generation** (`interviews.generation_id`), so later template edits or regenerations never change what the interviewer sees. Multiple interviews can be created from the same doc set (rounds, different reviewers).
- **INT-4 (P0)** Interviews tab: columns company, job title, maker, reviewer, meeting time, status (`scheduled`, `completed`, `cancelled`, `no_show`), pinned generation (with a "newer available" marker), feedback indicator. Filters: reviewer, maker, profile, status, date range. Search: company, job title, reviewer. Default sort: meeting time ascending, upcoming first.
- **INT-5 (P0)** Manager can edit any field, reassign the reviewer, cancel, mark no-show, or **Update to generation n** (explicit, logged in `interview_events`, notifies the reviewer). Reviewer is notified by SSE (`interview.assigned` / `interview.updated`).
- **INT-6 (P0)** Interview detail (Manager): all fields, the pinned generation's three files (preview/download) with the generation number, full Profile details, event history, and the reviewer's feedback when present.
- **INT-7 (P1)** Duplicate an interview to another reviewer with one click ("reuse form for many interviewers"); the duplicate pins the same generation.
- **INT-8 (P2)** iCal (.ics) download per interview.

#### 5.4.2 Reviewer view

- **INT-9 (P0)** Two tabs: **Upcoming** (meeting time ≥ now, sorted ascending) and **Past** (descending). Only interviews where `reviewer_id = current_user`. A badge shows the count of new/unseen assignments.
- **INT-10 (P0)** Interview detail: company name, job title, candidate name from the resume, meeting time (in the reviewer's browser timezone with the server timezone in a tooltip), meeting link, location, notes, all template fields marked *visible to reviewer*, the **pinned generation's** three files (preview PDF/TXT inline, download all as ZIP), and **Profile details limited to the fields the Manager marked shareable** (PRO-8). The Maker's identity is **not** shown to reviewers. If the Manager updates the pinned generation, the reviewer sees an "updated documents" notice with the change time.
- **INT-11 (P0)** **Feedback** form, available once the meeting time has passed (and any time for `completed` ones): outcome (`pass` / `fail` / `hold` / `no_show`), rating 1–5, strengths (textarea), concerns (textarea), free notes. Submitting sets status `completed` (or `no_show`) and notifies Managers (`feedback.added`). The author can edit their feedback; edits are versioned and the Manager sees the latest with an "edited" marker.
- **INT-12 (P0)** Reviewers never see: other reviewers' interviews, Makers, the Resumes page, prompts, un-shared Profile fields, LLM JSON, other generations.
- **INT-13 (P1)** Reviewer dashboard card on the Interviews page: next interview countdown, interviews this week, pending feedback count.

### 5.5 Profiles page (Manager)

**Who:** manager only. **Route:** `/profiles`. Makers never see Profile data; Reviewers see only shared fields, inside an interview.

- **PRO-1 (P0)** List of Profiles with name, URL, status (`active` / `archived`), theme, provider override, number of assigned Makers, doc sets generated, last edited. Search by name.
- **PRO-2 (P0)** Create/edit form fields: **Name** (unique), **Product URL**, **Description / notes** (markdown), **Start date**, **End date** (optional; outside the window the profile is flagged "inactive" and new assignments warn), **Theme** (from Settings), **LLM override** (provider, model, temperature, max tokens — all optional), **Tags**, and a **Custom fields** area (key/value pairs the Manager can add freely, e.g. "Contact", "Pricing tier"). A note under the form states that changes apply to builds created from now on (PIPE-9).
- **PRO-3 (P0)** **Prompt editor**: large monospace editor with character/token estimate, **Save as new version** (with a change note) and a **Version history** list showing author, timestamp, note, and diff against the previous version; any past version can be re-activated. Only the **active version** is snapshotted onto new builds.
- **PRO-4 (P0)** The output contract (LLM-1) is appended automatically and shown read-only below the editor so the Manager knows what the model receives.
- **PRO-5 (P1)** **Test prompt** (LLM-5): sample JD box → run → show JSON, validation result, rendered files, tokens and latency. Uses the Profile's provider resolution. Nothing is stored in a Maker's view.
- **PRO-6 (P0)** **Assigned makers** panel on each Profile: table of Makers currently on this Profile with their **daily limit** (editable inline — it is the Maker's own `users.daily_limit`), assigned date, today's usage, and **Unassign**. **Assign makers** opens a multi-select of Makers; those already on another Profile show "Will move from *Profile X*" and, on confirm, their previous assignment ends. One Profile can have any number of Makers.
- **PRO-7 (P0)** Assignment history (who/when/which profile) is kept for auditing; a Maker has at most one active assignment (partial unique index).
- **PRO-8 (P0)** **Share with reviewers**: a checklist of Profile fields (Name, URL, Description, Start/End dates, Tags, each Custom field). Checked fields are shown to Reviewers in interview details for doc sets generated under this Profile. The **prompt, LLM settings and theme are never shareable**. Default: only Name and URL.
- **PRO-9 (P0)** Archive (not delete) a Profile: no new assignments, existing assignments end, historical builds keep their snapshot. Unarchive allowed.
- **PRO-10 (P1)** Usage summary per Profile: doc sets by day, success rate, average attempts, tokens/cost estimate.

### 5.6 Settings page (Manager)

**Who:** manager only. **Route:** `/settings`. Sub-tabs: **LLM Providers**, **Doc Themes**, **General**, **Retention & disk**, **System status**.

#### LLM Providers

- **SET-1 (P0)** Table of providers: display name, type, default model, status (OK / error / disabled), max concurrency, RPM, last used, **System default** and **Fallback** markers.
- **SET-2 (P0)** Add/edit provider: **Type** (anthropic, openai, azure_openai, google_gemini, openrouter, openai_compatible), **Display name**, **API key** (write-only: never returned by the API; UI shows `••••…ab12` last 4), **Base URL** (required for openai_compatible / azure), **Default model** (free text, with a **Fetch models** button where the provider supports listing), **Max concurrency**, **Requests per minute**, **Timeout (s)**, **Enabled** toggle.
- **SET-3 (P0)** **Test connection** sends a tiny request and shows latency and the model that answered, or the provider's error message.
- **SET-4 (P0)** **Set as system default**: exactly one enabled provider is the default; deleting/disabling it requires choosing another first. Builds already created keep their snapshot; Profiles referencing a deleted provider fall back to the default for new builds and the Profile list shows a warning. **Set as fallback** (P1, PIPE-7) is optional and must differ from the default.
- **SET-5 (P0)** API keys are encrypted at rest (AES-256-GCM via a `MASTER_KEY` env var, key rotation command provided). Keys never appear in logs, SSE events or error messages.
- **SET-6 (P1)** Per-provider usage card: requests, tokens, error rate, average latency (last 24 h / 7 d).

#### Doc Themes

- **SET-7 (P0)** Table of themes: name, description, assigned Profiles, preview thumbnail (P1).
- **SET-8 (P0)** Add/edit theme: **Name**, **Description**, and a form generated from Appendix C: font (picker limited to fonts installed in the render image, GEN-4), base size, accent colour, page background colour ("None" = white), line height, section gap, four margins. Values validated against the ranges in Appendix C. Saving shows "applies to builds created from now on".
- **SET-9 (P0)** **Assign to Profiles** multi-select on the theme (mirrors the Theme dropdown on the Profile form).
- **SET-10 (P1)** **Preview** renders the bundled `sample.json` with the theme through the real pipeline and shows the PDF inline.
- **SET-11 (P0)** A theme in use cannot be deleted; it can be archived. A default theme (the generator's `DEFAULTS`) always exists (seeded).

#### General

- **SET-12 (P0)** Timezone (used for "day" boundaries, seq numbering and limits; default from the host), `default_daily_limit` (100, applied to newly created Makers), `min_jd_chars`, `max_jd_chars`, `llm_timeout_s`, `render_timeout_s`, `max_llm_attempts` (10), `max_render_attempts` (3), `show_selection_to_makers` (RES-12), default interview form template.
- **SET-13 (P0)** Every Settings change is written to the audit log with before/after values (keys redacted).

#### Retention & disk

- **SET-15 (P0)** The four retention windows of STO-4, the disk thresholds of STO-5, a **dry-run** button ("this would free 12.3 GB across 1,842 generations") and the list of Selected/Kept doc sets with their sizes.

#### System status

- **SET-14 (P0)** Live card (SSE): queue depths (`llm`, `render`, `ops`), in-flight per provider, workers alive with last heartbeat, active leases and leases expired in the last hour, outbox backlog and relay lag, builds re-dispatched by the sweep (last 24 h), `unoserver` health and conversions since restart, **builds needing attention** (blocking vs regeneration, with links), retries in the last hour per stage, oldest queued job age, disk usage of `/storage` with threshold state and **Pause / Resume intake** switch, database size, memory per service, LibreOffice version, generator version, app version.

### 5.7 Users page (Manager)

**Who:** manager only. **Route:** `/users`.

- **USR-1 (P0)** Table: name, email, role, status (active / deactivated), Profile and **daily limit** (for makers), today's usage, last login, created. Filter by role/status; search by name/email.
- **USR-2 (P0)** **Create user**: name, email (unique, case-insensitive), role; for makers: **daily limit** (prefilled from `default_daily_limit`) and optional Profile; **initial password** (auto-generated 16 chars, shown once with Copy; Manager passes it to the person out of band), `must_change_password` on by default (AUTH-6).
- **USR-3 (P0)** Edit name/email/role/daily limit. Changing a maker's role ends their Profile assignment; changing a reviewer's role requires no open scheduled interviews (or reassigning them first).
- **USR-4 (P0)** **Deactivate / Reactivate**: deactivated users cannot log in and their sessions are revoked immediately; their data remains. Hard delete is not offered in v1.
- **USR-5 (P0)** **Reset password**: generates a new temporary password (shown once) and sets `must_change_password`.
- **USR-6 (P0)** Guards: a manager cannot deactivate or demote themselves, and the last active manager cannot be demoted or deactivated.
- **USR-7 (P1)** **Revoke sessions** per user; show active session count.
- **USR-8 (P1)** Bulk create from CSV (name, email, role, profile, limit) with a preview and per-row errors.

### 5.8 Application shell

- **UI-1 (P0)** Left sidebar with role-specific navigation (section 2), current user menu (name, role, Change password, Log out), and a global connection indicator (Live / Reconnecting / Offline).
- **UI-2 (P0)** All lists are server-paginated (default 50, up to 200 per page) and keep filters in the URL query string so views are shareable and survive refresh.
- **UI-3 (P0)** Responsive down to ~768 px (tablet). Tables collapse to cards below that width.
- **UI-4 (P1)** Light and dark themes following the OS preference, with a manual toggle.
- **UI-5 (P0)** Every destructive or bulk action confirms and reports the outcome (toast with counts and a link to the affected view).
- **UI-6 (P0)** English UI strings only in v1, but all strings pass through an i18n layer so translation is possible later.
- **UI-7 (P0)** The SPA is served with hashed asset filenames and an `index.html` that is never cached, so a deploy never leaves a browser on a broken mix of old and new bundles; on a chunk-load error the app reloads once.

---

## 6. Data model

PostgreSQL. All tables have `id` (UUIDv7), `created_at`, `updated_at`. Soft state is expressed with status columns and `*_at` timestamps rather than deletes.

| Table | Key columns | Notes |
|---|---|---|
| `users` | `email` (citext unique), `name`, `role` (enum), `password_hash`, `is_active`, `must_change_password`, **`daily_limit`** (int, nullable; makers only), `last_login_at` | The row is the Maker's intake lock (PIPE-11). |
| `refresh_tokens` | `user_id`, `token_hash`, `expires_at`, `revoked_at`, `user_agent`, `ip` | Rotated on use. |
| `profiles` | `name` (unique), `url`, `description`, `start_date`, `end_date`, `theme_id`, `provider_id` (nullable), `model`, `temperature`, `max_tokens`, `tags[]`, `custom_fields jsonb`, `shared_fields text[]`, `status`, `active_prompt_version_id` | |
| `prompt_versions` | `profile_id`, `version_no`, `body`, `change_note`, `created_by` | Immutable. |
| `profile_assignments` | `profile_id`, `maker_id`, `assigned_by`, `started_at`, `ended_at` | Partial unique index on `(maker_id) where ended_at is null`. |
| `llm_providers` | `type`, `display_name`, `api_key_enc` (bytea), `base_url`, `default_model`, `max_concurrency`, `rpm`, `timeout_s`, `is_enabled`, `is_default`, `is_fallback` | Exactly one `is_default` among enabled. |
| `themes` | `name`, `description`, `params jsonb` (Appendix C, validated), `status` | Live configuration; builds carry their own copy. |
| `jobs` | `maker_id`, `profile_id`, `seq_no`, `submitted_date`, `submitted_at`, `idempotency_key`, `jd_text`, `jd_hash`, `duplicate_of`, **`delivery_status`** (enum: pending, released, skipped), `released_at`, `released_late` (bool), `skipped_at`, `skipped_by`, `initial_generation_id`, `jd_tsv tsvector` | Unique `(maker_id, submitted_date, seq_no)` — the ordering key; unique `(maker_id, idempotency_key)`. Partial index on `delivery_status='pending'` for the cursor. |
| `doc_sets` | `job_id` (unique), `candidate_name`, `company_name`, `job_title`, `slug`, `storage_dir`, `docx_basename`, **`current_generation_id`**, `is_selected`, `selected_by`, `selected_at`, `keep` (bool), `renamed_by`, `files_expired_at`, `search_tsv` | Created with the job; names filled when the initial build is ready. |
| `generations` | `job_id`, `generation_no`, `kind` (enum: initial, retry_after_skip, regenerate), **`status`** (enum: queued, llm_running, rendering, retry_wait, needs_attention, ready, cancelled), `stage` (enum: llm, render), `llm_attempts`, `render_attempts`, `retry_budget_reset_at`, `next_retry_at`, `dispatch_state` (pending/sent), `dispatched_at`, `claimed_by`, `lease_token` (uuid), `lease_expires_at`, `consecutive_provider_errors`, `last_error_code`, `last_error_message`, **snapshot:** `prompt_version_id`, `provider_id`, `model`, `llm_params jsonb`, `theme_id`, `theme_snapshot jsonb`; `storage_dir`, `ready_at`, `created_by` (null = pipeline), `expired_at` | Unique `(job_id, generation_no)`. Partial indexes on `dispatch_state='pending'`, on `next_retry_at`, on `lease_expires_at`, and on `status='ready' and kind='initial'` for reconciliation. Immutable once `ready`. |
| `generation_attempts` | `generation_id`, `stage`, `attempt_no`, `lease_token`, `worker`, `provider_id`, `model`, `started_at`, `finished_at`, `outcome` (enum: succeeded, failed, timed_out, superseded), `error_code`, `error_message`, `tokens_in`, `tokens_cached`, `tokens_out`, `latency_ms`, `artifacts_dir` | Unique `(generation_id, stage, attempt_no)`; `attempt_no` only increases. |
| `pipeline_events` | `job_id`, `generation_id` (nullable), `from_state`, `to_state`, `stage`, `attempt_no`, `actor` (worker or user id), `details jsonb`, `at` | Timeline for the detail drawer. |
| `event_outbox` | `audience jsonb` (user ids / roles), `event_type`, `payload jsonb`, `created_at`, `published_at` | Written in the same transaction as the state change (PIPE-1); rows older than 24 h are purged. Partial index on `published_at is null`. |
| `files` | `generation_id`, `kind` (`pdf`/`docx`/`txt`/`llm_json`/`meta`), `path`, `size_bytes`, `sha256`, `expired_at` | `jd` at doc-set level (`generation_id` null). |
| `interview_templates` | `name`, `fields jsonb`, `is_default`, `created_by` | |
| `interviews` | `doc_set_id`, **`generation_id`** (pinned), `reviewer_id`, `template_id`, `template_snapshot jsonb`, `values jsonb`, `meeting_at` (timestamptz), `meeting_tz`, `status` (enum), `created_by`, `cancelled_at`, `seen_by_reviewer_at` | |
| `interview_events` | `interview_id`, `type` (incl. `generation_changed`), `actor_id`, `details jsonb`, `at` | |
| `feedback` | `interview_id`, `author_id`, `outcome`, `rating`, `strengths`, `concerns`, `notes`, `version_no` | New row per edit; latest wins. |
| `settings` | `key` (pk), `value jsonb`, `updated_by` | General, retention and disk settings; `intake_paused` with reason. |
| `audit_log` | `actor_id`, `action`, `entity_type`, `entity_id`, `before jsonb`, `after jsonb`, `ip`, `at` | Managers' actions, logins, key changes (redacted), retention deletions. |
| `daily_maker_stats` (materialised) | `maker_id`, `date`, `submitted`, `ready`, `skipped`, `selected`, `avg_ready_ms`, `avg_llm_attempts`, `tokens` | Refreshed by `ops` every 5 min; used by RES-16. |

Key relationships: `users 1—n jobs`, `jobs 1—1 doc_sets`, `jobs 1—n generations`, `generations 1—n generation_attempts`, `generations 1—n files`, `doc_sets 1—n interviews`, `interviews n—1 generations` (pinned), `interviews 1—n feedback`, `profiles 1—n prompt_versions`, `profiles 1—n profile_assignments n—1 users`.

---

## 7. API surface

REST under `/api/v1`, JSON, cookie auth, CSRF token on mutating requests. OpenAPI docs served at `/api/docs` for managers only. Errors follow `{ "error": { "code": "limit_reached", "message": "…", "details": {} } }`.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login` (`{email,password,as_admin}`) · `POST /auth/logout` · `POST /auth/refresh` · `GET /auth/me` · `POST /auth/change-password` |
| Events | `GET /events` (SSE) |
| JDs / jobs | `POST /jobs` (maker; header `Idempotency-Key`) · `GET /jobs?date=&status=&q=&maker_id=&page=` · `GET /jobs/{id}` · `GET /jobs/{id}/generations` · `GET /jobs/{id}/attempts` · `POST /jobs/{id}/retry` (manager; `{mode: "render_only" \| "new_llm_call"}`; on a `pending` job acts on the initial build, on a `skipped` job creates a `retry_after_skip` build) · `POST /jobs/{id}/skip` (manager) · `GET /me/limit` · `GET /me/eta` |
| Generations | `POST /generations/{id}/cancel` (manager; regenerate builds only) |
| Doc sets | `GET /doc-sets?...` (filters incl. `selected`, `profile_id`, `provider_id`, `multi_generation`, `kept`, `attention`) · `GET /doc-sets/{id}` · `PATCH /doc-sets/{id}` (rename, keep; manager) · `POST /doc-sets/{id}/regenerate` (manager) · `POST /doc-sets/{id}/select` · `DELETE /doc-sets/{id}/select` · `POST /doc-sets/bulk-select` |
| Files | `GET /files/{file_id}` (download, `?inline=1` for preview) · `GET /doc-sets/{id}/zip?generation=` · `POST /doc-sets/zip` (`{ids:[…]}`, streamed) · `GET /doc-sets/zip?date=&maker_id=` |
| Stats | `GET /stats/makers?from=&to=` · `GET /stats/profiles` · `GET /stats/providers` |
| Interviews | `GET /interview-templates` · `POST/PUT/DELETE /interview-templates/{id}` · `GET /interviews?...` · `POST /interviews` · `GET /interviews/{id}` · `PATCH /interviews/{id}` · `POST /interviews/{id}/use-generation` (`{generation_id}`) · `POST /interviews/{id}/cancel` · `POST /interviews/{id}/duplicate` · `POST /interviews/{id}/feedback` · `GET /interviews/{id}/feedback` · `POST /interviews/{id}/seen` |
| Profiles | `GET /profiles` · `POST /profiles` · `GET /profiles/{id}` · `PATCH /profiles/{id}` · `POST /profiles/{id}/archive` · `GET /profiles/{id}/prompt-versions` · `POST /profiles/{id}/prompt-versions` · `POST /profiles/{id}/prompt-versions/{v}/activate` · `POST /profiles/{id}/test` · `GET /profiles/{id}/assignments` · `POST /profiles/{id}/assignments` (`{maker_ids:[…]}`) · `POST /assignments/{id}/end` |
| Settings | `GET /providers` · `POST /providers` · `PATCH /providers/{id}` · `DELETE /providers/{id}` · `POST /providers/{id}/test` · `POST /providers/{id}/set-default` · `POST /providers/{id}/set-fallback` · `GET /providers/{id}/models` · `GET /themes` · `POST /themes` · `PATCH /themes/{id}` · `POST /themes/{id}/preview` · `GET /system/fonts` · `GET /settings` · `PATCH /settings` · `POST /retention/dry-run` · `POST /system/intake/pause` · `POST /system/intake/resume` · `GET /system/status` |
| Users | `GET /users` · `POST /users` · `PATCH /users/{id}` (incl. `daily_limit`) · `POST /users/{id}/deactivate` · `POST /users/{id}/reactivate` · `POST /users/{id}/reset-password` · `POST /users/{id}/revoke-sessions` · `POST /users/import` |
| Ops | `GET /healthz` · `GET /readyz` · `GET /metrics` (internal network only) |

---

## 8. Non-functional requirements

Numbers marked **(target)** are engineering targets to be replaced by measurements from HW-7 (M0) and the M4 load test on the actual 2 vCPU / 6 GB server.

| ID | Requirement |
|---|---|
| **NFR-1** | Intake (target): 1,000 `POST /jobs` per minute sustained for 10 minutes with p95 < 300 ms and zero lost or duplicated jobs, verified by `bench-intake` in M0 and the M4 load test. |
| **NFR-2** | Pipeline: throughput follows the CONC-7 model. Acceptance in M4: a 200-JD burst with the mock provider at L = 30 s, C_llm = 8 completes within 1.2 × the modelled time, and rendering is never the bottleneck at C_llm = 8 (measured R / C_render < L / C_llm). |
| **NFR-3** | Release latency (ORD-7) < 2 s; SSE fast path (RT-5) < 1 s; outbox slow path ≤ 10 s (target). |
| **NFR-4** | Storage estimate: ~1–2 MB per generation including debug artifacts → 5,000 doc sets ≈ 5–10 GB, 100,000 ≈ 100–200 GB before retention. Disk thresholds per STO-5. Disk size to be confirmed (12 #11). |
| **NFR-5** | Availability: single-node; all services restart automatically (`restart: unless-stopped`); planned downtime only for upgrades; accepted jobs survive API, worker and Redis restarts without loss or duplication (PIPE-2/3); a Postgres restart pauses the system but loses nothing. |
| **NFR-6** | Data durability: nightly `pg_dump` + incremental backup of `/storage` (restic or rsync) to a second disk or off-box target; retention 30 daily + 12 monthly. **`MASTER_KEY` is stored outside the server (owner's password manager) and the restore drill verifies that a restored database can decrypt a provider key with it**; without the key, provider API keys must be re-entered (nothing else is encrypted). Restore procedure documented and tested once before launch. |
| **NFR-7** | Browser support: latest two versions of Chrome, Edge, Firefox, Safari. |
| **NFR-8** | Accessibility: keyboard-operable forms and tables, visible focus, WCAG AA contrast. |
| **NFR-9** | Observability: every job traceable from JD to files via `job_id`/`generation_id`; logs are JSON with `job_id`, `generation_id`, `stage`, `attempt_no`, `lease_token`, `user_id`, `request_id`; metrics for queue depths, in-flight, per-stage latency histograms, retry / needs-attention / re-dispatch / expired-lease / outbox-lag counts, memory per service, disk usage. |
| **NFR-10** | Test coverage: unit/integration tests for **ordering** (ORD-1…5, 8, 9: a retrying blocker holds later ready jobs; Skip releases them in order; reconciliation releases after a simulated crash; a regenerate build on a released job never changes delivery status or the cursor), **claims** (two workers claim the same build concurrently → exactly one wins; a write with an expired lease is rejected and the late result is discarded), **retries** (a render failure never creates an LLM attempt; attempt numbers keep increasing across a Manager reset), **snapshots** (editing the theme/prompt/provider after build creation does not change the build's output; *new LLM call* does re-resolve), **intake** (concurrent submissions from one Maker yield gapless seq numbers and respect the limit; a repeated idempotency key returns the same job), **outbox** (an event committed but not published is delivered by the relay), **retention** (a single-generation doc set expires after `docset_retention_days`; Selected/Kept/pinned ones do not), limits, permission checks, JSON validation/repair, alias normalisation, generation pinning; a golden test running the real generator on `sample.json` through `unoserver` under parallel processes (GEN-5/8); an end-to-end test that submits 50 JDs with the mock provider returning in random order with injected LLM and render failures and worker kills, and asserts strictly ordered release with no duplicate LLM attempts. |
| **NFR-11** | Maintainability: Alembic migrations for every schema change; `make dev` boots the full stack locally with the mock LLM provider; seed script creates a manager, two makers, one reviewer, one profile, the default theme. |
| **NFR-12** | Benchmarks are code: `manage.py bench-render`, `bench-intake` and the load-test script live in the repo and their latest results are committed to `docs/benchmarks.md` with the server spec and date. |

---

## 9. Security and privacy

- **SEC-1 (P0)** All traffic over HTTPS (Caddy, HSTS). Cookies `HttpOnly; Secure; SameSite=Lax`; CSRF double-submit token for mutating requests.
- **SEC-2 (P0)** Role checks and ownership checks in a single dependency layer on every endpoint; object-level authorization tests for makers (own jobs only), reviewers (assigned interviews and their pinned generation only).
- **SEC-3 (P0)** Files are never served statically; every download passes through an authorization check and sets `Content-Disposition` with a sanitised filename; `X-Content-Type-Options: nosniff`.
- **SEC-4 (P0)** Provider API keys encrypted at rest (SET-5), decrypted only inside the LLM worker process, redacted from logs, never in SSE payloads or snapshots (snapshots store `provider_id`, not the key).
- **SEC-5 (P0)** Passwords Argon2id; login rate limiting (AUTH-4); refresh token rotation with reuse detection (revoke the family on reuse).
- **SEC-6 (P0)** Input handling: JD text stored as plain text and rendered as text only (no HTML interpretation); LLM JSON validated by schema before it is stored for rendering; the generator and LibreOffice run with a per-job temp dir, no outbound network (container network policy) and CPU/time limits.
- **SEC-7 (P0)** JDs and generated documents may contain personal data. Access to doc sets and downloads is recorded in `audit_log`; the retention policy (STO-4) provides deletion; retention deletions are logged.
- **SEC-8 (P1)** Security headers (CSP without inline scripts, Referrer-Policy, Permissions-Policy); dependency scanning in CI (`pip-audit`, `npm audit`).
- **SEC-9 (P1)** Optional IP allow-list for the Manager role, configurable in `.env`.

---

## 10. Deployment and operations

- **OPS-1 (P0)** Repository layout: `frontend/` (Vite React SPA), `backend/` (FastAPI app, Celery workers, `vendor/resume_builder`, Alembic, `manage.py`), `deploy/` (`docker-compose.yml`, `Caddyfile`, `Dockerfile.caddy`, `Dockerfile.render`, `.env.example`, `postgresql.conf`, backup scripts), `docs/` (this PRD, runbook, `benchmarks.md`, schema docs).
- **OPS-2 (P0)** Configuration exclusively via environment variables (`DATABASE_URL`, `REDIS_URL`, `MASTER_KEY`, `STORAGE_DIR`, `PUBLIC_URL`, `RENDER_CONCURRENCY`, `LLM_CONCURRENCY`, timeouts). No secrets in images.
- **OPS-3 (P0)** `manage.py` commands: `create-manager`, `rotate-master-key`, `reindex-search`, `release-scan [--maker <id>]`, `dispatch-sweep`, `expire-leases`, `relay-outbox`, `retention --dry-run`, `render-sample --theme <id>` (fidelity check), `bench-render --n N`, `bench-intake`, `backup`, `restore`.
- **OPS-4 (P0)** Upgrade path: `git pull && docker compose build && docker compose up -d`; migrations run automatically on `api` start with a lock so only one instance migrates; the SPA is rebuilt inside the Caddy image (UI-7). Workers finish their current attempt before stopping (`stop_grace_period` ≥ `render_timeout_s`); anything cut off is recovered by lease expiry.
- **OPS-5 (P0)** Runbook covers: provider outage (pause intake, drain, switch default provider), builds needing attention (Retry now modes / Skip / Cancel / fix prompt), `unoserver` hangs and memory growth (HW-4), stuck leases, outbox backlog, disk thresholds and forced resume, restoring from backup incl. `MASTER_KEY`, rotating the master key, adding a provider type, updating the vendored generator, adding a second render box (HW-8).
- **OPS-6 (P1)** Health checks wired to a simple uptime monitor; disk, memory, needs-attention, expired-lease and outbox-lag alerts to the Manager via the in-app System status banner (email/Slack alerts are P2).
- **OPS-7 (P0)** Local development uses the `mock` provider type that returns fixture JSON after a random 1–10 s delay with configurable LLM and render failure rates, so ordering, retry and UI behaviour can be tested without API spend; `make chaos` kills random workers during a run.
- **OPS-8 (P0)** Host preparation script: Docker + Compose, 2 GB swapfile and `vm.swappiness=10` (HW-3), unattended security updates, firewall allowing 80/443/SSH only, storage directory on the largest disk.

---

## 11. Milestones

Milestones are a **sequence with rough effort for one full-time developer familiar with the stack**, not calendar commitments. Re-estimate after M0, when the generator has been proven on the server and the team's real velocity is known.

| Milestone | Scope | Exit criteria | Rough effort |
|---|---|---|---|
| **M0 — Feasibility & foundations** | **First:** generator refactor (GEN-1), render image with LibreOffice + `unoserver` + fonts, `sample.json` rendered on the server, fidelity sign-off (GEN-8), `bench-render` (HW-7). **Then:** repo layout, Docker Compose with limits (§3.2/3.4), Postgres/Redis, FastAPI skeleton, Vite SPA shell, auth (login/refresh/logout, roles), Users page incl. daily limit, seed script, CI. | Owner approves the LibreOffice PDF. `docs/benchmarks.md` holds measured render p50/p95, peak RSS and intake p95 on this server. Manager can log in and create users of each role. | 1.5–2 weeks |
| **M1 — Pipeline** | Intake transaction (PIPE-11) with limits and idempotency, jobs/doc sets/builds, priority queues, durable dispatch, atomic claims and leases, lease watchdog, LLM worker with LiteLLM + one real provider + mock provider, JSON validation/repair/normalisation, stage-aware retries and needs-attention, render worker producing immutable generations, storage layout, transactional ordered release + reconciliation, outbox + SSE + periodic refresh, `worker-ops`, Maker Resumes page with downloads/ZIP. | 50 JDs submitted in a burst with injected LLM and render failures **and random worker kills** come back `Ready` strictly in submission order; `generation_attempts` shows no duplicate or extra LLM attempts; killing the API, a worker and Redis mid-run loses nothing; an open browser tab is correct within 10 s after a crash between commit and publish. | 3–3.5 weeks |
| **M2 — Manager tooling** | Profiles (CRUD, prompt versions, multi-maker assignments, share fields), Settings (providers, keys, test, default/fallback, themes with live preview, general, retention & disk, system status), Manager Resumes view (filters, stats, select, Retry now modes / Skip / Cancel / Regenerate with generation list, Keep, rename, needs-attention banner). | Manager configures a new provider, theme and Profile without touching the server; editing a theme does not change a queued build; Regenerate creates gen 2 and gen 1 remains downloadable; stats match the database. | 2 weeks |
| **M3 — Interviews** | Form templates, scheduling with generation pinning, reviewer view, feedback, notifications, shared Profile fields, "update to generation n". | End-to-end: select → schedule → regenerate → reviewer still sees gen 1 → Manager updates → reviewer sees gen 2 with a notice → feedback visible to Manager. | 2 weeks |
| **M4 — Hardening & launch** | Load test (NFR-1/2 on this server), chaos run, backups and restore drill incl. `MASTER_KEY`, retention dry-run and first real run, audit log review, security headers, runbook, dark mode, CSV export, ETA (JD-11). | Load test, chaos run and restore drill pass; `benchmarks.md` updated; all P0 requirements verified. | 1–1.5 weeks |

---

## 12. Open questions

| # | Question | Impact | Proposed default |
|---|---|---|---|
| 1 | **PDF fidelity:** is LibreOffice's rendering of the DOCX acceptable compared with Word's? Decided by the GEN-8 side-by-side, first task of M0. If not: (a) a direct-PDF renderer (second code path, risk of drift), or (b) a Windows render node (breaks G5). | GEN-3/8, M0 exit | LibreOffice; tune fonts/aliases until approved. |
| 2 | **Fonts:** which fonts must the theme picker offer? Windows fonts cannot be shipped; metric-compatible substitutes are used. Should the owner supply licensed font files to install in the image? | GEN-4, SET-8 | Substitutes only. |
| 3 | **`company_information`:** the model has no web access, so this section is written from the model's own knowledge. Keep it in the TXT as today, or drop it from the schema and prompt? | Appendix B, TXT content | Keep for now; drop if the owner prefers not to rely on model knowledge. |
| 4 | **Timezone for "day" boundaries** when makers are in different countries: server timezone for everyone, or per-user? | JD-7, RES-1 | Single server timezone (Settings). |
| 5 | **Retention windows:** are the STO-4 defaults (attempt artifacts 30 d, superseded generations 90 d, doc sets 365 d, interview-pinned 365 d after the meeting) right? Any legal requirement to delete personal data on request? | STO-4, SEC-7 | Defaults as stated; manual purge command. |
| 6 | **Which providers must work on day one?** (Anthropic, OpenAI, Gemini, OpenRouter, other?) | M1 scope, testing | Anthropic + OpenAI + mock in M1; others in M2. |
| 7 | **Multiple reviewers per interview** (panel), or always one? | INT-2/3 | One reviewer per interview; use Duplicate (INT-7) for panels. |
| 8 | **Prompt placeholders:** should the prompt support variables such as `{{product_url}}` or `{{today}}`? | PRO-3 | Support a small fixed set: `{{product_name}}`, `{{product_url}}`, `{{today}}`. |
| 9 | **Retry budgets** (`max_llm_attempts` 10 ≈ 1 h, `max_render_attempts` 3) and whether a Maker may Skip their own stuck job or must always wait for a Manager. | PIPE-4/6 | 10 / 3; Manager only. |
| 10 | **Manual edits after generation:** the desktop GUI lets a person edit the preview before saving. Is a web equivalent (edit JSON → regenerate as a new generation) needed, and for whom? | Non-goals, P2 backlog | Not in v1; Managers can copy the JSON into the desktop GUI. |
| 11 | **Server disk size and other tenants:** how much disk is available for `/storage` and backups, and does this server run anything else that competes for the 2 cores / 6 GB? | §3.4, NFR-4/6 | Assume ≥ 100 GB free and a dedicated box. |
| 12 | **Peak shape and provider tier:** is the realistic worst case 50 makers × 100 JDs within one hour (5,000 jobs → ≈ 5 h at 8 concurrent calls, CONC-7) or spread over a day? Which provider concurrency tier will be purchased? | CONC-7, HW-8, JD-11 | Spread over the day; single box at launch; `max_concurrency` raised to the provider's real limit. |

---

## Appendix A — Review of `resume_builder.py`

Reviewed version: the file attached on 2026-09-21 (≈1,050 lines; python-docx + Tkinter + Word COM). Overall: the document model (`build_blocks` → blocks/runs → `build_docx`) is clean, deterministic and easy to reuse; the surrounding process code assumes a single Windows desktop and has to be separated from the core before it can run on the server. **Nothing here has been executed on Ubuntu yet — that is the first task of M0.**

### A.1 Findings

| # | Severity | Finding | Impact on Remote Flow | Required change |
|---|---|---|---|---|
| 1 | **Blocker** | PDF export uses Microsoft Word through COM (`win32com`, `pythoncom`, `WordWorker`). | Cannot run on Ubuntu; there is no Word. | Pluggable PDF backend (`resume_builder/pdf.py`); server uses LibreOffice via `unoserver` (GEN-3). Keep `word` backend for the Windows GUI. |
| 2 | **Blocker** | `_word_worker = WordWorker()` and `atexit.register(...)` execute at **import time**. | Importing the module in a worker registers a Windows-only shutdown hook and a hidden thread factory. | Create the worker lazily inside the `word` backend only. |
| 3 | High | Style comes from a global `style.json` next to the script (`load_style`, `save_settings`); `run_cli` ignores per-call style. | Themes must be per build and the image filesystem is read-only; concurrent jobs would race on one file. | Core functions take `opts` explicitly (they already do); server passes the build's `theme_snapshot`; CLI gains `--theme`. |
| 4 | High | The JSON shape is implicit in `build_blocks` (two generations of schema: `contact{}`/`skills[]` vs flat fields/`technicalskills{}`, `role`/`duration` vs `title`/`dates`, `university`/`field_of_study`, `company`/`company_name`/`target_company`). | The LLM must be told one exact shape; malformed shapes crash (`item.get` on a string in `education`, `job.get` on a non-dict in `experience`). | Formal JSON Schema (Appendix B) validated **before** the generator; alias normalisation (LLM-6). The generator keeps its tolerant reading for desktop use. |
| 5 | High | Windows-only font defaults (`Calibri`, `FONT_CHOICES` list of Office fonts). | PDF rendered on Linux would silently fall back to DejaVu and change layout/page count. | Install metric-compatible substitutes + fontconfig aliases; restrict the theme font picker to installed fonts (GEN-4). |
| 6 | Medium | `compose_job_info()` returns `""` when `job_description`/`company_information`/`job_link` are all absent → GUI writes no TXT. | The pipeline requires three files. | LLM-6 injects the original JD as `job_description`, so the TXT is never empty. |
| 7 | Medium | `make_names()`/`dashify()` do not cap length or guard reserved names; a long `target_company` + role can exceed filesystem limits (255 bytes) or produce `.` / empty segments. | Storage and ZIP entries. | Server-side slug: `dashify` + 80-char cap + fallback `Resume`. Folder names are system-generated (STO-1). |
| 8 | Medium | Page background uses `w:background` + `w:displayBackgroundShape` (Word-specific settings). | LibreOffice may or may not honour it in PDF export. | Verify in GEN-8; if unsupported, emulate with page-style background via a post-processing step or document the limitation for the `bg_color` theme field. |
| 9 | Medium | The GUI's "edit the preview, then save" step (`widget_to_blocks`) is part of the desktop workflow. | No human-in-the-loop step exists server-side. | Out of scope for v1 (12 #10); Regenerate (as a new generation) + prompt versions cover most needs. |
| 10 | Low | `_deliver()`/`_replace_into_place()` retry loops target Windows file locks (cloud sync, open viewers). | Harmless on Linux. | Leave as is; server uses its own atomic-rename step (GEN-2). |
| 11 | Low | `add()` in `build_blocks` re-parses highlight markers on runs already produced by `_hl_runs`; double parsing is harmless but redundant. | None. | Optional cleanup during the refactor. |
| 12 | Low | `normal.font.name` is set but not the East Asian font (`w:eastAsia`); CJK text in a JD-derived field would render in the default CJK font. | Cosmetic, only if resumes contain CJK. | Optional: set `rFonts` for all scripts. |
| 13 | Info | `[highlight]` handling accepts both `term[highlight]` (suffix, heuristic span) and `[highlight]…[/highlight]` (explicit), plus the `hightlight` typo; Markdown links are unwrapped. | Shows real-world LLM output variance. | The output contract asks for the explicit span form only; the generator's tolerance stays as a safety net. |
| 14 | Info | Company "Google" is rendered in logo colours (`google_*`). | Deliberate feature; keep. | None. |

### A.2 What the server reuses unchanged

`build_blocks`, `build_docx`, `make_names`, `dashify`, `compose_job_info`, `parse_highlights`, `_clean_data`, `DEFAULTS`, `SIZE_OFFSETS`, `KIND_PROPS`, `COLOR_HEX`. These are pure functions over their inputs and are safe to run in parallel processes.

### A.3 What the server does not use

Tkinter GUI (`run_gui`, `App`, `_gui_selftest`), `WordWorker`, `docx_to_pdf` (Word), `save_resume`, `generate` (extension-dispatch wrapper), `style.json` helpers, `run_cli`.

### A.4 Render image (Dockerfile.render) essentials

```
python:3.12-slim
apt: libreoffice-writer libreoffice-core-nogui fontconfig
     fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation fonts-liberation2
     fonts-dejavu fonts-noto-core fonts-gelasio
/etc/fonts/local.conf: aliases Calibri→Carlito, Cambria→Caladea, Arial→Liberation Sans,
     Times New Roman→Liberation Serif, Courier New→Liberation Mono, Georgia→Gelasio,
     Segoe UI/Tahoma/Verdana→Noto Sans (or DejaVu Sans)
pip: python-docx, unoserver, celery[redis,gevent], the backend package, vendored resume_builder
entrypoint: start unoserver (one per Celery process, port 2002+index), then celery worker -Q render
env: RB_PDF_BACKEND=unoserver  RENDER_CONCURRENCY=1  UNOSERVER_MAX_CONVERSIONS=200  UNOSERVER_MAX_RSS_MB=700
```

---

## Appendix B — LLM output JSON schema

Canonical shape requested from the model (`schemas/resume.schema.json`). The generator also tolerates the legacy aliases listed at the end; LLM-6 normalises them so the pipeline only ever stores the canonical form.

```jsonc
{
  "name": "string, required — candidate name (used in file names)",
  "title": "string, required — role title (file names, doc set label)",
  "subtitle": "string — line under the name, e.g. 'Senior Full Stack Developer · Remote'",

  "email": "string", "phone": "string", "location": "string", "citizenship": "string",
  "work_authorization": "string", "linkedin": "string",

  "summary": "string, required — may contain [highlight]…[/highlight] spans",

  "technicalskills": { "<Category>": "comma-separated text, may contain highlight spans" },
  "additional_information": { "<Label>": "text" },

  "experience": [                              // required, ≥ 1
    { "title": "string, required", "company": "string, required",
      "location": "string", "dates": "string",
      "bullets": ["string, required ≥ 1 — may contain highlight spans"] }
  ],
  "education": [
    { "degree": "string", "school": "string", "location": "string",
      "dates": "string", "gpa": "string" }
  ],

  "target_company": "string, required — the company in the JD (doc set company name)",
  "job_link": "string — URL if present in the JD",
  "company_information": {                     // optional; goes to job_description.txt (see 12 #3)
    "public_facts": "text", "safe_inferences": "text", "research_limitations": "text"
  }
  // "job_description" is NOT requested from the model; the system injects the original JD (LLM-6)
}
```

Rules included in the output contract appended to every prompt (LLM-1):

1. Return **only** a JSON object matching this schema; no Markdown fences, no prose.
2. Bold terms with explicit spans: `[highlight]Node.js[/highlight]`. Do not use the suffix form or Markdown `**bold**`.
3. No Markdown links; write plain URLs or plain text.
4. `education` is always an array (the generator also accepts a single object).
5. Do not include `job_description`.

Legacy aliases normalised by LLM-6 (canonical ← alias): `email/phone/location/citizenship/work_authorization/linkedin` ← `contact.{…}`; `technicalskills{}` ← `skills[{category, items[]}]` (joined with ", "); `experience[].title` ← `role`; `experience[].dates` ← `duration`; `education[].degree` ← `field_of_study`; `education[].school` ← `university`; `target_company` ← `company_name` ← `company` (top level only).

Derived doc set fields: `company_name = target_company`; `job_title = title` (fallback: `subtitle` split on `·`/`|`, first part); `candidate_name = name`; `docx_basename = make_names(data)[1]`.

---

## Appendix C — Theme schema

Mirrors the generator's `DEFAULTS` (the keys of `style.json`) with the ranges used by the desktop GUI's spinboxes. Stored in `themes.params` as live configuration and **copied verbatim into `generations.theme_snapshot`** when a build is created; the render worker passes `{**DEFAULTS, **theme_snapshot}` to `build_docx`.

| Key | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `font` | string | `"Calibri"` | one of `GET /system/fonts` (installed in the render image) | Body and heading font. The DOCX stores this name; the PDF uses the metric-compatible substitute. |
| `size` | number (pt) | `10.5` | 8 – 14, step 0.5 | Base body size; other sizes are `size + SIZE_OFFSETS[kind]` (name +11, title +2.5, section +1, job +0.5, contact/meta −0.5). |
| `accent` | hex colour | `"#1F4E79"` | `#RRGGBB` | Name, section headings, rules, company names, bullet glyphs. |
| `bg_color` | hex colour | `"#FFFFFF"` | `#RRGGBB`; white = none | Full-page background fill (see Appendix A #8). |
| `line_height` | number | `1.0` | 1.0 – 2.5, step 0.05 | Line spacing multiple. |
| `section_gap` | number (pt) | `9` | 0 – 40 | Space before each section header. |
| `margin_top` | inches | `0.7` | 0.3 – 1.5 | |
| `margin_bottom` | inches | `0.7` | 0.3 – 1.5 | |
| `margin_left` | inches | `0.75` | 0.3 – 1.5 | |
| `margin_right` | inches | `0.75` | 0.3 – 1.5 | |

Fixed by the generator (not themeable in v1): text colours (`body #262626`, `muted #595959`), Google logo colours, paragraph spacing per block kind (`KIND_PROPS`), bullet glyph and indent, contact separators, section title casing. Exposing any of these is a generator change plus a new schema version.
