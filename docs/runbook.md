# Remote Flow — Operations Runbook

Audience: whoever is on call for a self-hosted Remote Flow box. Everything here
is doable from a shell on the host; the Manager UI is the first choice for
content problems, the CLI for anything below the API.

Related documents:

- `docs/benchmarks.md` — HW-7 measurements taken on the launch box.
- `README.md` — architecture, environment variables, first-run setup.
- `deploy/docker-compose.yml` — services, resource limits (HW-1/HW-2) and volumes.

## 1. Quick reference

| Item | Value |
| --- | --- |
| Public entry point | Caddy on `SITE_ADDRESS` (`PUBLIC_URL`), proxies `/api/*` and `/healthz` to `api:8000` |
| Services | `caddy`, `api`, `worker-llm`, `worker-render`, `worker-ops`, `postgres`, `redis` |
| Health | `GET /healthz` (public, via Caddy); `GET /readyz` and `GET /metrics` are internal-only — `docker compose exec api curl -fsS localhost:8000/readyz` |
| Logs | `docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs -f <service>` |
| Operator CLI | `docker compose exec api python manage.py <command>` |
| Manager view | Settings → **System status** card (queues, leases, outbox, disk, versions) |
| Backups | `deploy/backup.sh` nightly via cron; `deploy/restore.sh <dir>` to restore |
| Secrets that must live off-box | `MASTER_KEY` (provider keys), `SECRET_KEY` (sessions), `POSTGRES_PASSWORD` |
| Demo accounts (**dev only**, created by `manage.py seed`) | `manager@example.com`, `maker@example.com`, `maker2@example.com`, `reviewer@example.com` — password `remote-flow-demo`; never seed a production database |

Useful CLI commands:

```bash
python manage.py migrate            # safe on every boot, takes a pg advisory lock
python manage.py relay-outbox       # publish committed-but-unpublished events
python manage.py release-scan       # ORD-8: release ready cursors after a crash
python manage.py dispatch-sweep     # re-enqueue work the queue may have lost
python manage.py expire-leases      # fail expired leases and apply retry policy
python manage.py retention          # expire files per STO-4 (dry run: Settings UI)
python manage.py backup | restore   # see §10
python manage.py rotate-master-key --new-key "$(openssl rand -base64 32)"
python manage.py chaos-run          # OPS-7 smoke test with injected failures
```

Thresholds and defaults that drive the procedures below:

| Setting | Default | Where |
| --- | --- | --- |
| `disk_warn_pct` | 85 | Settings → General |
| `disk_pause_intake_pct` | 90 (auto-pauses intake) | Settings → General |
| `disk_pause_render_pct` | 95 (marks rendering paused) | Settings → General |
| `intake_force_resume` | false | set by "Resume (force)" |
| `attempt_artifacts_retention_days` | 30 | Settings → Retention |
| `superseded_generation_retention_days` | 90 | Settings → Retention |
| `docset_retention_days` | 365 | Settings → Retention |
| `interview_retention_days` | 365 | Settings → Retention |
| `unoserver` recycle | 200 conversions / 700 MB RSS / 60 s conversion | HW-4, `deploy/unoserver-entrypoint.sh` |
| Worker heartbeat freshness | 180 s (`WORKER_STALE_AFTER_S`) | `app.config` |
| Outbox fast path / cap | 2 s, purge after 24 h | `app.services.events` |

## 2. Provider outage (rate limits, 5xx, bad key)

Symptoms: many builds land in `retry_wait` then `needs_attention` with
`provider_rate_limited` / `provider_timeout` / `provider_server_error`; the
System status card shows retries spiking and the LLM queue depth climbing.

1. **Decide whether to keep accepting work.** Intake is independent of the
   provider, so the queue can absorb a short outage. For a long one, pause:
   Settings → General → **Pause intake** (or
   `POST /api/v1/system/intake/pause?reason=Provider outage`). Makers see a
   banner and the pause reason; nothing is lost.
2. **Confirm the blast radius.** Settings → **System status**: "Builds needing
   attention" (blocking vs regeneration), retries in the last hour per stage,
   oldest queued job age. `GET /api/v1/stats/providers` shows per-provider
   attempt/error counts.
3. **Switch the default provider** (Settings → LLM Providers → *Set default*, or
   `POST /api/v1/providers/{id}/set-default`). If the fallback provider is
   configured (`is_fallback`), builds that hit three consecutive provider errors
   switch to it automatically — no action needed.
4. **Drain the queue.** Consumers keep retrying with exponential backoff, so a
   recovered provider drains itself. To force it, Manager → Resumes →
   *Needs attention* filter → **Retry now → New LLM call** per job, or bulk:
   `python manage.py release-scan` does not retry; use the UI/API for retries.
5. **Resume intake** (Settings → General → *Resume*, or
   `POST /api/v1/system/intake/resume`). If disk is above 85 % you must confirm
   with `?force=true`.
6. **Afterwards:** if the same job needed repeated manual retries, consider
   raising `max_concurrency` on the provider or adding a second provider. The
   audit log (Settings → General → Audit) records who did what.

A bad API key shows as `provider_auth_error` and does **not** retry into
fallback forever: fix the key in Settings → LLM Providers → *Edit* (the new key
is re-encrypted with `MASTER_KEY`), then Retry now.

CONC-4 is automatic: after a provider returns HTTP 429 the worker reduces that
provider's effective concurrency by 25 % (compounding, floor 25 %) for 60 s and
honours the response's `Retry-After` in the backoff. No action is needed, but it
explains a temporarily lower `in flight` count on the Resumes strip.

## 3. Builds needing attention

`needs_attention` means a build exhausted its automatic attempts. Blocking
builds hold the Maker's queue (ordered release, ORD-1); regeneration failures do
not (they are labelled *not blocking*).

For each row in Resumes → **Needs attention**:

| Situation | Action |
| --- | --- |
| Transient provider/render blip | **Retry now → Render only** (if the LLM JSON exists) or **New LLM call** |
| Prompt/profile problem | Profiles → edit the Profile, add a new Prompt version, activate it, then **Retry now → New LLM call** (a new LLM call re-resolves the snapshot; PIPE-9) |
| Bad JD (spam, wrong text) | **Skip** — the job leaves the cursor and everything ready behind it releases in order (ORD-5) |
| Regeneration failed | **Retry now**, or **Cancel** the regeneration build; the released files are untouched |
| Maker resubmitted the same JD | Skip, or leave it; identical text inside 7 days is flagged as a duplicate, not blocked |

Retry budgets are per stage and reset explicitly (`retry_budget_reset_at`), so
attempt numbers keep increasing across a Manager reset — useful when reading
the attempt timeline in the doc set drawer.

## 4. `unoserver` hangs and memory growth (HW-4)

Symptoms: renders approach `conversion_timeout_s` (60 s), `render_timeout` in
the attempt timeline, `worker-render` container RSS climbing, PDFs missing.

1. Check the container:
   `docker compose exec worker-render sh -c 'ps aux | grep -E "unoserver|soffice"'`
   and `docker stats worker-render`.
2. The entrypoint already recycles `unoserver` after **200 conversions** or
   **700 MB RSS**, and `render` retries with a cold `soffice --headless` once
   (GEN-3). If it is still wedged, restart just that service:
   `docker compose restart worker-render` — in-flight leases expire and the
   builds retry automatically (leases are 2× the render timeout).
3. If LibreOffice crashed hard, leftover lock files can block the next start:
   the cold fallback uses a private `-env:UserInstallation` profile, so this
   should be self-healing; if not, remove `/tmp/.X*-lock` inside the container.
4. If the render queue is the bottleneck at normal load (compare
   `docs/benchmarks.md` p95 with the LLM p95), add a second render box (§12)
   instead of raising `RENDER_CONCURRENCY` — HW-1 keeps render at 1 per box.
5. A single pathological DOCX that always times out: Skip the job, then ask the
   Maker to resubmit with a leaner theme (or fix the Profile). The JSON is
   available in the doc set drawer for the desktop generator.

## 5. Stuck leases

A lease is a claim with an expiry (`lease_expires_at`). A killed worker never
releases it explicitly, so the watchdog does:

- `worker-ops` beat runs `lease-watchdog` every `LEASE_WATCHDOG_SECONDS`
  (default 60 s) — it marks the attempt `timed_out` and applies the retry policy.
- Manual: `python manage.py expire-leases`.
- Force a re-dispatch if the queue lost the message: `python manage.py dispatch-sweep`.

Verify with Settings → System status → *active leases* / *leases expired (1 h)*.
If a build sits in `llm_running`/`rendering` with an expired lease and the
watchdog is not running, check `worker-ops` logs and that Celery Beat is up
(`-B` on the ops worker).

## 6. Outbox backlog (events not reaching the browser)

The UI polls every 10 s as a fallback, so a backlog is a latency problem, not a
data problem. Symptoms: System status → *outbox backlog* > 0, *relay lag* rising,
live updates only on refresh.

1. `python manage.py relay-outbox` publishes everything committed more than 2 s
   ago. Run it; if it clears, beat was behind.
2. Check `redis` health (`docker compose exec redis redis-cli ping`) — the
   broker degrades to in-process fan-out if Redis is unreachable, which is fine
   for one process but means other processes will not see events.
3. If rows are stuck because delivery keeps raising, look at `api`/`worker-ops`
   logs for `fast-path publish failed` / `relay publish failed` with an
   `outbox_id`. A malformed audience cannot be fixed retroactively — rows older
   than 24 h are purged automatically.
4. Broker memory: Redis runs with `maxmemory-policy noeviction` (HW-6) and SSE
   streams are capped at 500 entries per user, so a backlog cannot grow forever.

## 7. Disk thresholds and forced resume

`worker-ops` evaluates disk usage on every retention sweep and on each System
status refresh:

| Usage | Effect |
| --- | --- |
| ≥ 85 % | warn in the UI, `disk_usage_pct` badge turns amber |
| ≥ 90 % | intake pauses automatically, reason recorded, `intake.paused` event |
| ≥ 95 % | rendering paused flag (System status) |
| < 85 % | intake resumes automatically **unless** someone used force-resume |

Free space safely:

1. Settings → Retention → **Dry run** shows exactly what would be removed
   (`protected` lists Selected / Kept / interview-pinned doc sets that are never
   touched). Check the byte total, then run it — or
   `python manage.py retention`.
2. Retention only ever removes *expired* generations, superseded generations and
   old attempt artifacts. It never touches delivered files that are still the
   current generation of a doc set.
3. `python manage.py backup` first if the numbers are large and you want a
   before-image.
4. Last resort: raise `docset_retention_days` down (e.g. 365 → 180) in
   Settings → Retention, dry-run, then run.
5. **Force resume** (Settings → General → *Resume (force)*, or
   `POST /api/v1/system/intake/resume?force=true`) overrides the automatic pause
   when you have made room another way — e.g. expanded the disk. Until usage
   drops below 85 % the automatic logic will not re-pause, so only do this when
   you accept the risk.

## 8. Database and Redis

- Postgres settings (HW-5) live in `deploy/postgresql.conf`
  (`shared_buffers=768MB`, `effective_cache_size=2GB`, `work_mem=8MB`,
  `max_connections=40`). Changing them needs a Postgres restart.
- `docker compose exec postgres pg_stat_activity` for long-running queries;
  `pg_locks` if a migration seems stuck (it waits on `pg_advisory_lock` held by
  another instance).
- Redis holds broker data only — no results (results live in Postgres). Losing
  Redis loses in-flight queue messages; `dispatch-sweep` re-enqueues them from
  `generations.dispatch_state`. Losing Postgres loses everything, hence
  `mem_limit: 1500m` headroom and backups.
- Migrations: `python manage.py migrate` (Alembic). The API container runs it on
  boot; it is safe to run concurrently because it takes a Postgres advisory lock.
  Downgrade with `alembic downgrade -1` from `backend/` only if you know why.

## 9. Deploys and upgrades

```bash
git pull
make build                       # api, caddy, worker-render images
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d
docker compose exec api python manage.py migrate
```

Zero-ish downtime order: `up -d` recreates workers first (leases expire and
retry), then `api` (two uvicorn workers behind Caddy). The SPA is served from an
immutable-asset build, so open tabs pick up the new bundle on the next reload
(UI-7 chunk-reload guard handles a mid-deploy reload).

Rollback: `git checkout <previous-tag> && make build && docker compose up -d`
plus `alembic downgrade` if the release included a migration.

## 10. Backup and restore (NFR-6)

Backups: `deploy/backup.sh` writes `db.sql`, `storage.tar.gz` and a manifest to
`/srv/remote-flow/backups/<stamp>` and prunes to 30 daily + 12 monthly. Schedule
it nightly (`cron`: `15 3 * * * /srv/remote-flow/deploy/backup.sh`).

**The `MASTER_KEY` is not in the backup.** Store it in the team password
manager; without it provider API keys in a restored database are unreadable
(everything else still works — the keys must be re-entered).

Restore drill (do this in M4 and after any infra change):

1. `deploy/restore.sh /srv/remote-flow/backups/<stamp>` — stops writers,
   recreates the database, restores the storage volume, restarts the stack.
2. Export the `MASTER_KEY` used by the *backed-up* instance before starting.
3. Walk the checklist printed by the script: migrations, Manager login, Users
   list, provider keys decrypt to "stored", System status, and one PDF/DOCX/TXT
   download per Maker.
4. Record the drill (date, who, duration, issues) in the ops log.

Point-in-time recovery is out of scope for v1; if it is needed, enable Postgres
WAL archiving on the volume in addition to the nightly dump.

## 11. Rotating the master key

```bash
# 1. New key, generated and stored off-box first.
NEW_KEY="$(openssl rand -base64 32)"
# 2. Re-encrypt every provider key.
docker compose exec api python manage.py rotate-master-key --new-key "$NEW_KEY"
# 3. Put NEW_KEY in deploy/.env, then recreate the services.
sed -i "s|^MASTER_KEY=.*|MASTER_KEY=$NEW_KEY|" deploy/.env
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d
```

Verify afterwards: Settings → LLM Providers → *Test* on each provider returns
OK. If a provider key was corrupted before the rotation, re-enter it.

## 12. Adding a provider type / a second render box

**Provider type.** The provider list is data-driven (`providers.type`);
supported types come from the LLM client adapter (`app/services/llm.py`). To add
one: add the type to the allowed set, make sure the adapter can stream/return
JSON for it, then create the provider in Settings → LLM Providers with its API
key and model list. Run *Test* before setting it as default. `bench-intake` and
the chaos run both work with the `mock` type, which is handy for a dry run.

**Second render box (HW-8).** `worker-render` is stateless: it reads/writes
Postgres, Redis and the shared storage directory.

1. Provision the box with `deploy/host-prep.sh` (swap, docker, firewall).
2. Mount the **same storage** (NFS or a replicated volume) at `/storage`; the
   renderer writes into `gen-{n}/` via an atomic rename, so two boxes never
   write the same file.
3. Point `DATABASE_URL` and `REDIS_URL` at the primary box over the private
   network and run only the render worker:
   `docker compose up -d worker-render` with `RENDER_CONCURRENCY=1`.
4. Watch `SELECT count(*) FROM generations WHERE status='rendering'` and the
   oldest queued age in System status; both boxes draw from the same `render`
   queue, so no configuration change is needed on the API side.

## 13. Routine checks

Daily (or let the nightly backup cron mail you):

- `GET /healthz` and `GET /readyz` return 200/`ok`.
- System status: no unknown/red worker heartbeats, no `needs_attention` rows
  older than a day, outbox backlog 0, disk < 85 %.
- Backups exist for last night (`ls -l /srv/remote-flow/backups/latest`).

Weekly:

- Review Settings → General → Audit for unexpected role/settings changes.
- CI (`.github/workflows/ci.yml`) runs the backend suite, the SPA typecheck/build
  and the dependency scans (`pip-audit`, `npm audit`). Triage its findings with
  the dependency update.
- If the server is exposed beyond a trusted network, set
  `MANAGER_IP_ALLOWLIST` (SEC-9) and confirm manager pages return
  `ip_not_allowed` from outside. SEC-8 headers are set by Caddy for the SPA and
  by the API for JSON responses.
- Retention dry run; run it if the byte total is growing.
- `python manage.py chaos-run --n 50` on a staging box after dependency updates.

## 14. Incident checklist (first 5 minutes)

1. What is broken — API, one worker, PDFs only, or the UI? `docker compose ps`.
2. `docker compose logs --since 15m <service>` for the failing service.
3. Is Postgres healthy (`pg_isready`) and is Redis answering (`redis-cli ping`)?
4. If intake is failing, pause it with a clear reason so Makers see why.
5. If a Maker's queue is blocked, use Skip / Retry now rather than editing rows.
6. Write down the timeline; the audit log + `pipeline_events` are the source of
   truth for what the pipeline actually did.
