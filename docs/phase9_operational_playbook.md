# Phase 9 Operational Playbook

This phase is about evidence, not intuition.

## Goals

- Run bounded historical backfills without losing control of the live backlog.
- Measure SQLite writer throughput and queue latency under realistic batch sizes.
- Decide whether SQLite is still sufficient based on observed numbers.

## Commands

```bash
# Historical backfill for a bounded window
python -m stock_triage backfill --source financial_report --start-year 2024 --end-year 2025

# Current per-source processing backlog
python -m stock_triage backlog-status

# SQLite writer throughput and queue-latency sample
python -m stock_triage benchmark-sqlite --batches 20 --rows-per-batch 100
```

## How To Use It

1. Run `backlog-status` before a backfill. Record discovered, downloaded, parsed, extracted, analyzed, and failed counts.
2. Run `benchmark-sqlite` with the batch sizes you expect during the backfill.
3. Run `backfill` on a narrow historical window first, then re-check `backlog-status`.
4. Increase the year range only if backlog remains controlled and failures stay understandable.

## SQLite Decision Criteria

SQLite remains acceptable while these conditions hold:

- bounded backfills complete without sustained backlog growth
- writer queue latency remains low relative to end-to-end stage latency
- failures are dominated by source quality or parsing issues, not database contention

Revisit the storage or queue design when:

- queue latency becomes a material bottleneck during normal runs
- backfills create sustained backlog that cannot be drained predictably
- operational recovery requires invasive manual intervention

## Notes

- `backfill` is intentionally bounded by stage limits per year.
- `benchmark-sqlite` measures the existing single-writer pattern instead of inventing a separate benchmark path.
- Canary failures should be reviewed alongside backlog and benchmark results before making scaling changes.
