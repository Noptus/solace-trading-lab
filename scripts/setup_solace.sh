#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export STL_SOLACE_PORT="${STL_SOLACE_PORT:-55555}"

docker compose up -d solace

MGMT_PORT=8080
printf "Waiting for Solace PubSub+ management port"
for _ in {1..60}; do
  if curl -fsS "http://localhost:$MGMT_PORT" >/dev/null 2>&1; then
    printf "\nSolace is reachable at http://localhost:%s (admin/admin).\n" "$MGMT_PORT"
    exit 0
  fi
  printf "."
  sleep 2
done

printf "\nSolace did not become reachable on port %s within 120 seconds.\n" "$MGMT_PORT" >&2
exit 1
