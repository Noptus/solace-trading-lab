#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

"$ROOT_DIR/scripts/setup_solace.sh"

export STL_IN_MEMORY_BUS="${STL_IN_MEMORY_BUS:-false}"
export STL_TOPIC_PREFIX="${STL_TOPIC_PREFIX:-solace-lab}"
export STL_SYMBOL="${STL_SYMBOL:-FOO}"
export STL_SOLACE_HOST="${STL_SOLACE_HOST:-localhost:${STL_SOLACE_PORT:-55555}}"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid"
    fi
  done
}
trap cleanup EXIT INT TERM

uv run python -m exchange.main &
pids+=("$!")

uv run python -m market_data.main &
pids+=("$!")

uv run python -m strategy.main &
pids+=("$!")

printf "Solace Trading Lab is running.\n"
printf "Venue HTTP: http://localhost:${STL_VENUE_HTTP_PORT:-9001}\n"
printf "Broker UI:      http://localhost:8080 (admin/admin)\n"
wait
