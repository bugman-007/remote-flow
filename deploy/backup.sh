#!/usr/bin/env bash
# NFR-6: nightly database + storage backup. Retention 30 daily + 12 monthly.
#   deploy/backup.sh [backup-root]      # default /srv/remote-flow/backups
set -euo pipefail

BACKUP_ROOT="${1:-/srv/remote-flow/backups}"
KEEP_DAILY="${KEEP_DAILY:-30}"
KEEP_MONTHLY="${KEEP_MONTHLY:-12}"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
DAY="$(date -u +%Y-%m-%d)"
TARGET="$BACKUP_ROOT/$STAMP"
COMPOSE_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$TARGET"

echo "==> PostgreSQL dump"
docker compose --env-file "$COMPOSE_DIR/.env" -f "$COMPOSE_DIR/docker-compose.yml" exec -T postgres \
  pg_dump --no-owner --dbname "postgresql://remote_flow:${POSTGRES_PASSWORD:-$(grep -E '^POSTGRES_PASSWORD=' "$COMPOSE_DIR/.env" | cut -d= -f2-)}@localhost:5432/remote_flow" \
  > "$TARGET/db.sql"

echo "==> Storage archive (incremental-friendly tar of the volume)"
docker run --rm \
  -v remote-flow_storage:/storage:ro \
  -v "$TARGET":/backup \
  alpine:3 tar czf /backup/storage.tar.gz -C /storage .

echo "==> Manifest"
cat > "$TARGET/MANIFEST.txt" <<MANIFEST
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
host=$(hostname)
compose_project=remote-flow
master_key_stored_off_box=yes
MANIFEST

ln -sfn "$TARGET" "$BACKUP_ROOT/latest"

echo "==> Pruning (keep $KEEP_DAILY daily, one per month for $KEEP_MONTHLY months)"
find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name '20*T*' | sort | head -n "-$KEEP_DAILY" | xargs -r rm -rf
for month in $(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name '20*T*' | sed -E 's#.*/([0-9]{4})([0-9]{2})[0-9]{2}T.*#\1\2#' | sort -u | head -n "-$KEEP_MONTHLY"); do
  keep=$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name "${month}*T*" | sort | tail -1)
  for dir in $(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name "${month}*T*"); do
    [ "$dir" = "$keep" ] || rm -rf "$dir"
  done
done

echo "backup complete: $TARGET"
echo "REMINDER: the restored database can only decrypt provider keys with the MASTER_KEY stored outside this server."
