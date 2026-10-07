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
python manage.py backup | restore   # see §14
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
   `docs/benchmarks.md` p95 with the LLM p95), add a second render box (§16)
   instead of raising `RENDER_CONCURRENCY` — HW-1 keeps render at 1 per box.
5. A single pathological DOCX that always times out: Skip the job, then ask the
   Maker to resubmit with a leaner theme (or fix the Profile). The JSON is
   available in the doc set drawer for the desktop generator.

## 5. Themes: the document does not look like the editor

The editor preview is HTML; the generated file is DOCX → PDF. They are kept in
step by two implementations of the same rules, so a mismatch means one of them
drifted.

1. Check the version the editor reports (Settings → Themes → *Generator x.y.z*
   badge). It is `GENERATOR_VERSION`; if it lags the deployed image, rebuild.
2. `GET /themes/sample` returns the block list the DOCX writer receives. If the
   preview shows a section the PDF does not, the theme used `hidden` or an
   element override — compare `themes.params` with the generation's
   `theme_snapshot` in the doc-set drawer; snapshots are frozen at build
   creation (PIPE-9) and deliberately do not follow later theme edits.
3. Re-render the theme on its own: **Render PDF proof** in the editor, or
   `python manage.py render-sample --theme <id>`. A slow first call is
   LibreOffice warming up, not a hang.
4. Fonts: only the families in `GET /system/fonts` are installed in the render
   image; anything else is substituted (GEN-4) and the layout shifts.

## 6. Generation processors: static vs dynamic (CONC-2)

Settings → **Processors** chooses how many LLM workers run.

- **Static** - exactly the number the Manager sets. Predictable, always costs
  that much RAM.
- **Dynamic** - the pool grows one processor at a time while memory is
  comfortable, then gives processors back when it is not. Defaults: grow below
  70 % memory, stop admitting work at 85 %, release processors at 90 % (down to
  what is actually running, at most one release per 45 s), 15 s between growth
  steps, 2–8 processors. When nothing is running and nothing is waiting the
  pool falls back to the floor, so a quiet box is a cheap box.

Why memory and not CPU: a provider call is 2-3 minutes of waiting, so the LLM
children sit near 0 % CPU. CPU contention is merely slow; memory exhaustion is
what kills jobs, so memory is the signal the controller watches. CPU can veto a
growth step but never forces a release.

The mechanics:

- The llm worker runs with `--autoscale=MAX,MIN`, so children are created on
  demand and an idle box keeps only MIN. The controller moves that range at
  runtime with `app.control.autoscale()`; `pool_grow`/`pool_shrink` are not
  available on an autoscaled worker.
- The range is re-broadcast at least once a minute even when nothing changed
  (`PUBLISH_KEEPALIVE_S`). A restarted worker comes back on the `--autoscale`
  range from its own command line, so without the keepalive it would keep
  running a range nobody asked for.
- **Releasing is job-safe by construction.** The controller only ever lowers
  the ceiling to the number of calls it can see running, and billiard's
  `Pool.shrink` in turn only terminates children with no active job - it raises
  "Can't shrink pool. All processes busy!" instead of killing a busy one. A
  running generation is never interrupted.
- Admission control sits on top: the controller publishes a *budget* to Redis and
  `dispatch_sweep` refuses to publish more LLM builds than fit, leaving the rest
  `pending` for the next pass. A build that reaches a worker without capacity is
  put back to `pending` **without spending an attempt** - "no capacity" is not a
  failure. The controller's own 10 s tick ends with a sweep, so a slot that has
  just opened is filled immediately instead of waiting for the 30 s sweep.

Sizing: the LLM children cost ~270 MB each (measured), so `worker-llm`'s
`mem_limit` is 3072 MB - enough for the setting's maximum of 10. A container that
hits its own limit gets children OOM-killed and the provider calls in flight are
lost, so the memory-driven controller (host thresholds), not the cgroup, should be
what stops the growth. Two further ceilings apply: the provider's
`max_concurrency` in Settings -> LLM Providers caps *simultaneous calls* (both
providers on this server are 8, so processors past 8 just wait), and
`LLM_POOL_MAX`/`LLM_POOL_MIN` in `deploy/.env` are only the worker's boot range.

Symptoms and fixes:

| Symptom | Check | Action |
|---|---|---|
| Pool stuck at 2 processors with a backlog | Settings → Processors → *Controller* state and reason | `memory %` above 70 %: freeing RAM is the fix, not raising the max |
| Pool smaller than expected in Static mode | `docker compose exec worker-ops celery -A app.workers.celery_app.celery_app inspect stats` | is `worker-ops` (Beat) running? the controller lives there |
| *Waiting for the controller's first reading* | `docker logs remote-flow-worker-ops-1` | the `pool-tick` beat entry runs every 10 s; restart `worker-ops` |
| Jobs queue but nothing starts | `LLM_POOL_MAX`, provider `max_concurrency`/`rpm` | the provider tier is the real ceiling above 8 |

`python manage.py` has no pool command: the controller is a Beat task
(`app.workers.tasks.pool_tick`) and Settings → Processors → **Apply now** runs
one cycle immediately.

## 7. Cancelling a Maker's submission

A Maker can withdraw a submission from the JD Upload page while it is still in
flight (the cancel action on each *Recent submissions* row, `POST
/jobs/{id}/cancel`).

- The job becomes `skipped` so the Maker's delivery cursor moves past it - a
  withdrawn job must never block the submissions behind it - and the row reads
  **Cancelled** (a Manager's skip keeps saying **Skipped**).
- Every unfinished build of that job is marked `cancelled` and loses its lease.
  A provider call that is already on the wire cannot be aborted, so its result is
  thrown away when it returns (`lease_update` no longer matches) and a *failing*
  attempt afterwards cannot resurrect the build (`record_failure` re-reads the
  status and supersedes). The slot a cancelled call holds stays busy until the
  provider answers.
- A delivered doc set cannot be withdrawn (409) - that is what permanent delete
  (Managers, Resumes page) is for. The submission keeps its daily-limit count.
- `job.cancel` is audited, and `pipeline_event` rows show `to_state=cancelled`.

Two identities can be told apart later: `jobs.skipped_by` equal to `jobs.maker_id`
means the Maker withdrew it; anything else is a Manager skip.

## 8. Render concurrency (doc generator)

`RENDER_CONCURRENCY` (default **2**) is how many DOCX-to-PDF conversions run at
once on `worker-render`. Measured on the target box: p50 0.68 s, p95 0.78 s, one
busy core and ~215 MB per conversion (docs/benchmarks.md).

- Each conversion spawns its own `soffice` with a private `-env:UserInstallation`
  profile, so concurrent conversions never share state.
- Two children already saturate both cores, which is why the deploy ships 2 and
  `worker-render` is nice-d at 10 with the lowest CPU share: a bulk render must
  never starve the API. Raise the knob only with free cores; also raise the
  service `mem_limit` (~215 MB per extra child).
- The render stage is not what makes a batch slow: a complete doc set costs
  ~0.7 s of render against 0.5-15 min of provider time, so the LLM pool
  (see 6) is the throughput lever.

## 9. Stuck leases

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

## 10. Outbox backlog (events not reaching the browser)

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

## 11. Disk thresholds and forced resume

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

### Deleting a resume for good

Retention (above) only *expires files* and keeps every row. When a resume must
disappear completely — database rows **and** the PDF/DOCX/JD on disk — a Manager
selects the rows on the Resumes page and uses **Delete permanently**, or calls:

| Call | Effect |
| --- | --- |
| `DELETE /api/v1/doc-sets/{id}?confirm=true` | one resume |
| `POST /api/v1/doc-sets/bulk-delete` `{"ids": [...]}` | the whole selection |

Both remove the doc set folder, every generation folder, `files` rows, attempts,
`pipeline_events`, the doc set and the job (with its `seq_no`), and write a
`docset.purge` audit row plus a `docset.purged` event. Two refusals are by
design: a resume an interview still pins (`docset_has_interviews`) and one whose
build is still running (`docset_in_flight`). Neither the retention window nor the
daily stats are recalculated, so a purged submission leaves a gap in the Maker's
`seq_no` sequence and stays counted in `daily_maker_stats`.

## 12. Database and Redis

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

## 13. Deploys and upgrades

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

## 14. Backup and restore (NFR-6)

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

## 15. Rotating the master key

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

## 16. Adding a provider type / a second render box

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

## 17. Routine checks

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

## 18. Incident checklist (first 5 minutes)

1. What is broken — API, one worker, PDFs only, or the UI? `docker compose ps`.
2. `docker compose logs --since 15m <service>` for the failing service.
3. Is Postgres healthy (`pg_isready`) and is Redis answering (`redis-cli ping`)?
4. If intake is failing, pause it with a clear reason so Makers see why.
5. If a Maker's queue is blocked, use Skip / Retry now rather than editing rows.
6. Write down the timeline; the audit log + `pipeline_events` are the source of
   truth for what the pipeline actually did.
