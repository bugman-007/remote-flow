#!/usr/bin/env bash
# NFR-6 / OPS-5: restore drill. Usage: deploy/restore.sh /srv/remote-flow/backups/<stamp>
set -euo pipefail

TARGET="${1:?usage: restore.sh <backup-dir>}"
COMPOSE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose --env-file $COMPOSE_DIR/.env -f $COMPOSE_DIR/docker-compose.yml"

test -f "$TARGET/db.sql" || { echo "missing $TARGET/db.sql" >&2; exit 1; }
test -f "$TARGET/storage.tar.gz" || { echo "missing $TARGET/storage.tar.gz" >&2; exit 1; }

echo "==> This overwrites the current database and storage. Ctrl-C to abort."
sleep 5

echo "==> Stopping writers (api + workers)"
$COMPOSE stop api worker-llm worker-render worker-ops || true

echo "==> Restoring PostgreSQL"
$COMPOSE exec -T postgres psql -U remote_flow -d postgres -c "DROP DATABASE IF EXISTS remote_flow;" -c "CREATE DATABASE remote_flow OWNER remote_flow;"
$COMPOSE exec -T postgres psql -U remote_flow -d remote_flow < "$TARGET/db.sql"

echo "==> Restoring storage volume"
docker run --rm \
  -v remote-flow_storage:/storage \
  -v "$TARGET":/backup:ro \
  alpine:3 sh -c "rm -rf /storage/* && tar xzf /backup/storage.tar.gz -C /storage"

echo "==> Starting services"
$COMPOSE up -d

cat <<'CHECK'

Restore drill checklist (OPS-5):
  1. `docker compose exec api python manage.py migrate` succeeds.
  2. A manager can log in and the Users page lists the restored accounts.
  3. Settings → LLM Providers shows each key as "stored" (decrypts with MASTER_KEY).
  4. `curl -fsS localhost/api/v1/... ` or the System status card shows no needs-attention rows
     that were not there before the backup.
  5. Download one PDF/DOCX/TXT set to confirm /storage is intact.
CHECK
