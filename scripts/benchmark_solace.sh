#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RUN_ID="${STL_PERF_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="${STL_PERF_OUT_DIR:-$ROOT_DIR/artifacts/perf/$RUN_ID}"
LOG_DIR="$OUT_DIR/logs"
SDKPERF_CSV="$OUT_DIR/sdkperf_results.csv"

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export STL_TOPIC_PREFIX="${STL_TOPIC_PREFIX:-solace-lab-perf}"
export STL_SYMBOL="${STL_SYMBOL:-FOO}"
export STL_SOLACE_HOST="${STL_SOLACE_HOST:-ws://localhost:${STL_SOLACE_WEB_PORT:-8008}}"

mkdir -p "$LOG_DIR"

pids=()
cleanup() {
  for pid in "${pids[@]+"${pids[@]}"}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
}
trap cleanup EXIT INT TERM

log() {
  printf '[perf] %s\n' "$*" >&2
}

wait_http() {
  local url="$1"
  local label="$2"
  log "waiting for $label at $url"
  for _ in {1..90}; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'Timed out waiting for %s at %s\n' "$label" "$url" >&2
  return 1
}

wait_tcp_port() {
  local host="$1"
  local port="$2"
  local label="$3"
  log "waiting for $label at $host:$port"
  for _ in {1..90}; do
    if command -v nc >/dev/null 2>&1; then
      if nc -z "$host" "$port" >/dev/null 2>&1; then
        return 0
      fi
    elif (echo >/dev/tcp/"$host"/"$port") >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'Timed out waiting for %s at %s:%s\n' "$label" "$host" "$port" >&2
  return 1
}

wait_solace() {
  local mgmt_port="${STL_SOLACE_MGMT_PORT:-8080}"
  local url="http://localhost:${mgmt_port}/SEMP/v2/config/about"
  log "waiting for Solace management at $url"
  for _ in {1..90}; do
    if curl -fsS -u "${STL_SOLACE_USERNAME:-admin}:${STL_SOLACE_PASSWORD:-admin}" \
      "$url" >/dev/null 2>&1; then
      return 0
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' solace-trading-lab-broker 2>/dev/null)" != "true" ]]; then
      docker logs --tail 80 solace-trading-lab-broker >&2 || true
      return 1
    fi
    sleep 1
  done
  docker logs --tail 80 solace-trading-lab-broker >&2 || true
  printf 'Timed out waiting for Solace management on port %s\n' "$mgmt_port" >&2
  return 1
}

start_lab() {
  docker compose up -d solace
  wait_solace
  wait_tcp_port "127.0.0.1" "${STL_SOLACE_WEB_PORT:-8008}" "Solace web transport"

  uv run python -m exchange.main >"$LOG_DIR/venue.log" 2>&1 &
  pids+=("$!")
  uv run python -m market_data.main >"$LOG_DIR/market_data.log" 2>&1 &
  pids+=("$!")

  local strategy_count="${STL_STRATEGY_COUNT:-10}"
  for index in $(seq 1 "$strategy_count"); do
    STL_STRATEGY_ID="$(printf 'mm-%03d' "$index")" \
      STL_STRATEGY_HTTP_PORT=0 \
      uv run python -m strategy.main >"$LOG_DIR/strategy-$index.log" 2>&1 &
    pids+=("$!")
  done

  wait_http "http://localhost:${STL_VENUE_HTTP_PORT:-9001}/health" "venue HTTP"
}

resolve_sdkperf_bin() {
  if [[ -n "${SDKPERF_BIN:-}" && -x "$SDKPERF_BIN" ]]; then
    printf '%s\n' "$SDKPERF_BIN"
    return 0
  fi
  if command -v sdkperf_java >/dev/null 2>&1; then
    command -v sdkperf_java
    return 0
  fi
  if command -v sdkperf_c >/dev/null 2>&1; then
    command -v sdkperf_c
    return 0
  fi
  if command -v sdkperf >/dev/null 2>&1; then
    command -v sdkperf
    return 0
  fi
  if [[ -z "${STL_SDKPERF_URL:-}" ]]; then
    return 1
  fi

  local sdkperf_dir="$ROOT_DIR/.sdkperf"
  local archive="$sdkperf_dir/sdkperf.download"
  mkdir -p "$sdkperf_dir"
  log "downloading SDKPerf from STL_SDKPERF_URL"
  curl -fsSL "$STL_SDKPERF_URL" -o "$archive"
  if file "$archive" | grep -qi 'zip'; then
    uv run python -m zipfile -e "$archive" "$sdkperf_dir"
  else
    tar -xf "$archive" -C "$sdkperf_dir"
  fi
  local found
  found="$(find "$sdkperf_dir" -type f \( -name 'sdkperf_java' -o -name 'sdkperf_c' -o -name 'sdkperf' \) -print -quit)"
  if [[ -z "$found" ]]; then
    return 1
  fi
  chmod +x "$found"
  printf '%s\n' "$found"
}

csv_header() {
  printf '%s\n' \
    'run_id,case_name,tls,delivery_mode,pattern,message_size_bytes,fanout,expected_messages,transmitted,received,ingress_msg_sec,egress_msg_sec,p99_latency_us,loss_percent,solace_table,solace_target_msg_sec,percent_of_target,status,log_path' \
    >"$SDKPERF_CSV"
}

metric_value() {
  local metrics_file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key {print $2}' "$metrics_file"
}

append_sdkperf_row() {
  local case_name="$1"
  local tls="$2"
  local delivery_mode="$3"
  local pattern="$4"
  local size="$5"
  local fanout="$6"
  local expected="$7"
  local status="$8"
  local log_path="$9"
  local metrics_file="${10}"

  local transmitted received ingress egress p99 target percent loss table
  transmitted="$(metric_value "$metrics_file" transmitted)"
  received="$(metric_value "$metrics_file" received)"
  ingress="$(metric_value "$metrics_file" publish_rate)"
  egress="$(metric_value "$metrics_file" subscriber_rate)"
  p99="$(metric_value "$metrics_file" p99_latency_us)"
  transmitted="${transmitted:-0}"
  received="${received:-0}"
  ingress="${ingress:-0}"
  egress="${egress:-0}"
  p99="${p99:-0}"
  target="$(uv run python -m chaos.benchmark target \
    --delivery-mode "$delivery_mode" \
    --pattern "$pattern" \
    --message-size-bytes "$size" \
    --fanout "$fanout")"
  percent="$(uv run python - "$egress" "$target" <<'PY'
import sys
actual = float(sys.argv[1])
target = float(sys.argv[2])
print(f"{actual / target:.6f}" if target else "0")
PY
)"
  loss="$(uv run python - "$expected" "$received" "$fanout" "$pattern" <<'PY'
import sys
expected = float(sys.argv[1])
received = float(sys.argv[2])
fanout = float(sys.argv[3])
pattern = sys.argv[4]
expected_out = expected * (fanout if pattern == "fanout" else 1)
loss = max(0.0, expected_out - received)
print(f"{(loss / expected_out * 100):.6f}" if expected_out else "0")
PY
)"
  table="$(uv run python - "$delivery_mode" "$pattern" "$size" "$fanout" <<'PY'
import sys
from chaos.benchmark import solace_target
target = solace_target(sys.argv[1], sys.argv[2], int(sys.argv[3]), fanout=int(sys.argv[4]))
print(target.table)
PY
)"

  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$RUN_ID" "$case_name" "$tls" "$delivery_mode" "$pattern" "$size" "$fanout" \
    "$expected" "$transmitted" "$received" "$ingress" "$egress" "$p99" "$loss" \
    "$table" "$target" "$percent" "$status" "$log_path" >>"$SDKPERF_CSV"
}

run_sdkperf_case() {
  local sdkperf_bin="$1"
  local tls="$2"
  local delivery_mode="$3"
  local pattern="$4"
  local size="$5"
  local fanout="$6"
  local messages="$7"
  local rate="$8"
  local case_name="${delivery_mode}-${pattern}-${size}B-f${fanout}-${tls}"
  local topic="$STL_TOPIC_PREFIX/sdkperf/$case_name"
  local queue="Q_${case_name//[^A-Za-z0-9_]/_}"
  local log_path="$LOG_DIR/$case_name.log"
  local metrics_file="$LOG_DIR/$case_name.metrics"
  local host user vpn password
  host="localhost:${STL_SOLACE_PORT:-55555}"
  if [[ "$tls" == "tls" ]]; then
    host="tcps://localhost:${STL_SOLACE_TLS_PORT:-55443}"
  fi
  user="${STL_SOLACE_USERNAME:-admin}"
  vpn="${STL_SOLACE_VPN:-default}"
  password="${STL_SOLACE_PASSWORD:-admin}"

  local cmd=(
    "$sdkperf_bin"
    "-cip=$host"
    "-cu=$user@$vpn"
    "-cp=$password"
    "-ptl=$topic"
    "-stl=$topic"
    "-mt=$delivery_mode"
    "-mn=$messages"
    "-msa=$size"
    "-mr=$rate"
    "-l"
    "-lwu=${STL_SDKPERF_LATENCY_WARMUP:-1}"
    "-oc"
    "-q"
  )
  if [[ "$pattern" == "fanout" ]]; then
    cmd+=("-cc=$fanout")
  fi
  if [[ "$delivery_mode" == "persistent" ]]; then
    cmd+=("-pe" "-sql=$queue")
  fi
  if [[ "$tls" == "tls" && -n "${STL_SDKPERF_TLS_OPTS:-}" ]]; then
    read -r -a tls_opts <<<"$STL_SDKPERF_TLS_OPTS"
    cmd+=("${tls_opts[@]}")
  fi

  log "running SDKPerf $case_name"
  if timeout "${STL_SDKPERF_TIMEOUT:-180}" "${cmd[@]}" >"$log_path" 2>&1; then
    uv run python -m chaos.benchmark parse-sdkperf --input "$log_path" >"$metrics_file"
    append_sdkperf_row "$case_name" "$tls" "$delivery_mode" "$pattern" "$size" "$fanout" \
      "$messages" "ok" "$log_path" "$metrics_file"
  else
    : >"$metrics_file"
    append_sdkperf_row "$case_name" "$tls" "$delivery_mode" "$pattern" "$size" "$fanout" \
      "$messages" "failed" "$log_path" "$metrics_file"
  fi
}

run_sdkperf_suite() {
  csv_header
  local sdkperf_bin
  if ! sdkperf_bin="$(resolve_sdkperf_bin)"; then
    log "SDKPerf not found. Set SDKPERF_BIN or STL_SDKPERF_URL to run broker benchmarks."
    return 0
  fi
  wait_tcp_port "127.0.0.1" "${STL_SOLACE_PORT:-55555}" "Solace SMF for SDKPerf"

  local messages="${STL_SDKPERF_MESSAGES:-100000}"
  local rate="${STL_SDKPERF_RATE:-50000}"
  for tls in tcp tls; do
    run_sdkperf_case "$sdkperf_bin" "$tls" direct p2p 100 1 "$messages" "$rate"
    run_sdkperf_case "$sdkperf_bin" "$tls" direct fanout 100 10 "$messages" "$rate"
    run_sdkperf_case "$sdkperf_bin" "$tls" persistent p2p 1024 1 "$messages" "$rate"
    run_sdkperf_case "$sdkperf_bin" "$tls" persistent fanout 1024 10 "$messages" "$rate"
  done

  if [[ -s "$SDKPERF_CSV" ]]; then
    uv run python -m chaos.benchmark plot-sdkperf \
      --csv "$SDKPERF_CSV" \
      --output "$OUT_DIR/sdkperf_throughput.png"
  fi
}

run_http_and_chaos() {
  uv run python -m chaos.benchmark http-burst \
    --venue-url "http://localhost:${STL_VENUE_HTTP_PORT:-9001}" \
    --orders "${STL_HTTP_BURST_ORDERS:-1000}" \
    --symbol "$STL_SYMBOL" \
    --output "$OUT_DIR/http_burst.csv"

  uv run python -m chaos.benchmark run \
    --output-dir "$OUT_DIR" \
    --orders "${STL_CHAOS_ORDERS:-2000}" \
    --fanout "${STL_CHAOS_FANOUT:-10}" \
    --symbol "$STL_SYMBOL" \
    --chaos-levels "${STL_CHAOS_LEVELS:-0,0.25,0.5,1.0}" \
    --load-rates "${STL_CHAOS_LOAD_RATES:-100000,1000000}"
}

main() {
  log "writing artifacts to $OUT_DIR"
  start_lab
  run_sdkperf_suite
  run_http_and_chaos
  log "artifacts:"
  find "$OUT_DIR" -maxdepth 2 -type f | sort
}

main "$@"
