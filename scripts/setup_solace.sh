#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

docker compose up -d solace

printf "Waiting for Solace PubSub+ management port"
for _ in {1..60}; do
  if curl -fsS "http://localhost:8080" >/dev/null 2>&1; then
    printf "\nSolace is reachable at http://localhost:8080 (admin/admin).\n"
    exit 0
  fi
  printf "."
  sleep 2
done

printf "\nSolace did not become reachable on port 8080 within 120 seconds.\n" >&2
exit 1
