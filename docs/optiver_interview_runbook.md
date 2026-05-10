# Optiver-Style Walkthrough Runbook

This project is a teaching lab, not a production exchange. The interview value is in showing how
you reason about correctness, latency, loss, and tradeoffs in a system that can be tested end to
end.

Optiver's public engineering material emphasizes pricing, risk, execution, low-latency systems,
reliability under pressure, and learning by building:

- [Tech at Optiver](https://optiver.com/working-at-optiver/tech-at-optiver/)
- [Software Engineer - Automated Trading](https://optiver.com/working-at-optiver/career-opportunities/8521302002/)
- [Software Engineer - Retail Execution](https://optiver.com/working-at-optiver/career-opportunities/8523840002/)

## Demo Flow

1. Run deterministic correctness tests:

   ```bash
   uv run pytest
   ```

2. Run the deterministic chaos benchmark:

   ```bash
   uv run python -m chaos.benchmark run \
     --output-dir artifacts/perf/interview-demo \
     --orders 2000 \
     --fanout 10 \
     --chaos-levels 0,0.25,0.5,1.0 \
     --load-rates 100000,1000000
   ```

3. If SDKPerf is installed, run the broker benchmark:

   ```bash
   SDKPERF_BIN=/path/to/sdkperf_java ./scripts/benchmark_solace.sh
   ```

4. Explain the generated artifacts:

   - `chaos_latency.csv`: p50/p99/p99.9 order-to-fill latency by load and chaos level
   - `chaos_tail_latency.png`: visual tail-latency regression signal
   - `sdkperf_results.csv`: direct vs. persistent throughput against Solace ESXi targets
   - `*.prom`: metrics that can be scraped or diffed in CI

## Order Book Correctness Points

- Price-time priority is deterministic and covered by tests.
- Order ids are single-use, including after full fills.
- Cancels preserve queue position for remaining orders.
- Amend is cancel-replace and intentionally resets queue priority.
- `OrderBook.validate_invariants()` audits internal indexes against queued liquidity:
  active order ids, FIFO sequence order, level side/price consistency, non-crossed books, and
  duplicate-guard coverage.

Use this when teaching:

```python
report = book.validate_invariants()
print(report)
```

It is opt-in so it does not add hot-path latency, but it is suitable for replay audits, property
tests, and debugging during incident-style exercises.

## Low-Latency Talking Points

- Python is the pedagogical control plane here. The production analog for colocated execution would
  be C++ with pinned cores, preallocated objects, kernel-bypass networking, and deterministic
  failure handling.
- The `src/execution/low_latency.py` path models the shape of a real execution engine:
  risk gate -> submit -> match -> publish accounting.
- Direct messaging is modeled for market data fan-out; persistent messaging is modeled for fills
  and execution reports.
- The benchmark harness keeps absolute broker targets separate from deterministic tests because
  Docker, TLS, CPU, NIC, and storage IOPS dominate the real result.

## Reliability Questions To Be Ready For

- What happens if a fill is published but an ACK is delayed?
- Which streams require persistence and which tolerate direct delivery?
- How do you prove no book state was lost during replay?
- What metrics would alert on a tail-latency regression before average latency moves?
- How would you move the Python hot path into C++ while preserving the same test vectors?

