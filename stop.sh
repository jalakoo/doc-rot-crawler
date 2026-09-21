#!/usr/bin/env bash
# Stop the docrot dashboard and clean up after it.
#
#   ./stop.sh              # stop this project's servers and tidy up
#   ./stop.sh --all-cache  # ...and delete every cached clone, not just partial ones
#
# Stops each `docrot serve` started from this directory (and the start.sh
# wrapping it), kills git left behind by a scan, removes partial clones, and
# marks interrupted scans failed so the dashboard does not show a scan running
# forever.
#
# Deliberately not `set -e`: every step is best-effort cleanup, and one failure
# must not leave the rest undone.
set -uo pipefail

cd "$(dirname "$0")" || exit 1
ROOT="$PWD"
ALL_CACHE=0
for arg in "$@"; do
  case "$arg" in
    --all-cache) ALL_CACHE=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Only this checkout's processes: a docrot running elsewhere is not ours to stop.
servers=$(pgrep -f "$ROOT/\.venv/bin/.*docrot serve" 2>/dev/null | tr '\n' ' ')
scans=$(pgrep -f "$ROOT/data/cache" 2>/dev/null | tr '\n' ' ')

# the start.sh wrapping one of those servers, found by parentage so another
# project's start.sh is never matched
wrappers=""
for pid in $servers; do
  parent=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
  if [[ -n "$parent" ]] && ps -o command= -p "$parent" 2>/dev/null | grep -q "start\.sh"; then
    wrappers+="$parent "
  fi
done

stop() {                       # SIGTERM, then SIGKILL whatever is still there
  local pids="$1" label="$2" alive pid
  [[ -z "${pids// /}" ]] && return 0
  echo "  stopping $label: ${pids% }"
  for pid in $pids; do kill "$pid" 2>/dev/null; done
  for _ in $(seq 1 25); do
    alive=""
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then alive+="$pid "; fi
    done
    [[ -z "$alive" ]] && return 0
    sleep 0.2
  done
  for pid in $alive; do
    echo "    $pid did not stop; killing"
    kill -9 "$pid" 2>/dev/null
  done
  return 0
}

# A hung clone's ssh sits in git's process group, so stop the group, not just git.
if [[ -n "${scans// /}" ]]; then
  echo "  stopping git left over from a scan: ${scans% }"
  for pid in $scans; do
    group=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [[ -n "$group" ]]; then kill -- "-$group" 2>/dev/null; fi
  done
fi

stop "$wrappers" "start.sh"
stop "$servers" "docrot serve"

if [[ -z "${servers// /}${wrappers// /}${scans// /}" ]]; then
  echo "  nothing running for $ROOT"
fi

shopt -s nullglob
partial=(data/cache/*.partial)
shopt -u nullglob
if (( ${#partial[@]} )); then
  echo "  removing ${#partial[@]} partial clone(s): $(du -sh "${partial[@]}" 2>/dev/null | awk '{s=$1} END {print s}')"
  rm -rf "${partial[@]}"
fi

if [[ "$ALL_CACHE" == 1 && -d data/cache ]]; then
  echo "  removing the clone and page cache ($(du -sh data/cache | awk '{print $1}'))"
  rm -rf data/cache
fi

# A scan that was running when the server stopped is not running now.
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python - <<'PY'
from docrot import config
from docrot.store import Store

for scan_id in Store(config.DATA).recover():
    print(f"  marked interrupted: {scan_id}")
PY
fi

# Anything still talking to a git remote cannot be attributed to this checkout -
# report it rather than killing someone else's ssh.
strays=$(pgrep -fl "ssh .*git-upload-pack" 2>/dev/null)
if [[ -n "$strays" ]]; then
  echo "  note: ssh is still talking to a git remote. If these are leftovers, kill them:"
  echo "$strays" | sed 's/^/    /'
fi

echo "  stopped."
