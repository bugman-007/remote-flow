#!/usr/bin/env sh
# HW-4: one warm unoserver per Celery process, restarted every
# UNOSERVER_MAX_CONVERSIONS conversions or when its RSS exceeds UNOSERVER_MAX_RSS_MB.
set -eu

CONVERSIONS="${UNOSERVER_MAX_CONVERSIONS:-200}"
MAX_RSS_MB="${UNOSERVER_MAX_RSS_MB:-700}"
PORT="${UNOSERVER_PORT:-2002}"
CONCURRENCY="${RENDER_CONCURRENCY:-1}"

start_unoserver() {
  unoserver --interface 127.0.0.1 --port "$PORT" --conversion-timeout 60 &
  UNO_PID=$!
  # Wait for the socket so the Celery worker never races the first conversion.
  for _ in $(seq 1 100); do
    if python - "$PORT" <<'PY' 2>/dev/null
import socket, sys
sock = socket.socket()
sock.settimeout(0.3)
sock.connect(("127.0.0.1", int(sys.argv[1])))
sock.close()
PY
    then
      return 0
    fi
    sleep 0.3
  done
  return 1
}

stop_unoserver() {
  if [ -n "${UNO_PID:-}" ] && kill -0 "$UNO_PID" 2>/dev/null; then
    kill "$UNO_PID" 2>/dev/null || true
    wait "$UNO_PID" 2>/dev/null || true
  fi
}

rss_mb() {
  if [ -n "${UNO_PID:-}" ] && [ -r "/proc/$UNO_PID/status" ]; then
    awk '/VmRSS/ {printf "%d", $2/1024}' "/proc/$UNO_PID/status"
  else
    echo 0
  fi
}

# Background watchdog: recycle the converter on conversions/RSS/exit.
watch_loop() {
  while true; do
    sleep 30
    if [ "$(rss_mb)" -ge "$MAX_RSS_MB" ]; then
      echo "unoserver: RSS $(rss_mb) MB >= ${MAX_RSS_MB} MB, recycling" >&2
      stop_unoserver
      start_unoserver || true
    fi
    if [ -f /tmp/unoserver.conversions ]; then
      count=$(cat /tmp/unoserver.conversions 2>/dev/null || echo 0)
      if [ "$count" -ge "$CONVERSIONS" ]; then
        echo "unoserver: $count conversions reached, recycling" >&2
        echo 0 > /tmp/unoserver.conversions
        stop_unoserver
        start_unoserver || true
      fi
    fi
  done
}

start_unoserver
watch_loop &
WATCH_PID=$!

shutdown() {
  trap - TERM INT
  kill "$WATCH_PID" 2>/dev/null || true
  stop_unoserver
  exit 0
}
trap shutdown TERM INT

celery -A app.workers.celery_app.celery_app worker \
  -Q render \
  -c "$CONCURRENCY" \
  -P prefork \
  --prefetch-multiplier 1 \
  -l INFO \
  -n "render@%h" &
CELERY_PID=$!

wait "$CELERY_PID"
STATUS=$?
shutdown
exit "$STATUS"
