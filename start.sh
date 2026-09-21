#!/usr/bin/env bash
# Start the docrot dashboard and open it in the browser once it is serving.
#
#   ./start.sh                 # the dashboard on http://127.0.0.1:8080
#   ./start.sh 8199            # another port
#   ./start.sh 8199 --no-open  # headless
#
# Scans live in data/scans/ and can be added from the page itself. Neo4j is
# optional - without it scans still run; only `docrot blast` loses the graph.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${1:-8080}"
shift $(( $# > 0 ? 1 : 0 ))

OPEN=1
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--no-open" ]]; then OPEN=0; else ARGS+=("$arg"); fi
done

DOCROT=".venv/bin/docrot"
# The query string makes this a URL no browser has cached: the page that used
# to live at / (before the dashboard) may still be in the browser's cache.
URL="http://127.0.0.1:$PORT/?t=$(date +%s)#/"

if [[ ! -x "$DOCROT" ]]; then
  echo "no virtualenv at .venv - create it with: uv venv && uv pip install -e ." >&2
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is already in use - try: ./start.sh $((PORT + 1))" >&2
  exit 1
fi

# The server opens nothing itself; this script opens the dashboard only once
# it answers, so the browser never lands on a connection-refused page.
PYTHONUNBUFFERED=1 "$DOCROT" serve --port "$PORT" --no-open ${ARGS[@]+"${ARGS[@]}"} &
SERVER=$!
trap 'kill "$SERVER" 2>/dev/null; wait "$SERVER" 2>/dev/null' INT TERM

for _ in $(seq 1 150); do                       # up to 30s
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/api/scans" 2>/dev/null; then
    if [[ "$OPEN" == 1 ]]; then
      if command -v open >/dev/null; then open "$URL"
      elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1
      else echo "  open $URL in your browser"
      fi
    fi
    break
  fi
  if ! kill -0 "$SERVER" 2>/dev/null; then
    wait "$SERVER"                              # the server exited; report its status
    exit $?
  fi
  sleep 0.2
done

wait "$SERVER"
