# Phase 9 Operational Playbook

This phase is about evidence, not intuition.

## Goals

- Run bounded historical backfills without losing control of the live backlog.
- Measure SQLite writer throughput and queue latency under realistic batch sizes.
- Decide whether SQLite is still sufficient based on observed numbers.

## Commands

```bash
# Historical backfill for a bounded window
python -m stock_triage backfill --source financial_report --start-year 2024 --end-year 2025 --discover-limit 50

# Current per-source processing backlog
python -m stock_triage backlog-status

# SQLite writer throughput and queue-latency sample
python -m stock_triage benchmark-sqlite --batches 20 --rows-per-batch 100
```

## How To Use It

1. Run `backlog-status` before a backfill. Record discovered candidate backlog plus downloaded, parsed, extracted, analyzed, and failed document counts.
2. Run `benchmark-sqlite` with the batch sizes you expect during the backfill.
3. Run `backfill` on a narrow historical window first, using `--discover-limit` unless you intentionally want to widen the queued candidate backlog, then re-check `backlog-status`.
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

- `backfill` is intentionally bounded by stage limits per year, and now defaults to bounding discovery persistence too.
- `benchmark-sqlite` measures the existing single-writer pattern instead of inventing a separate benchmark path.
- Canary failures should be reviewed alongside backlog and benchmark results before making scaling changes.

## Current observations

- Real 2024 financial-report backfill run:
  - discovered from IDX: `8690`
  - downstream bounded work completed: `downloaded=10`, `parsed=8`, `extracted=4`, `analyzed=4`
- Real 2023 financial-report backfill run after bounded discovery default:
  - discovered from IDX: `8987`
  - persisted into active queue: `2`
  - downstream bounded work completed: `downloaded=2`, `parsed=1`, `extracted=1`, `analyzed=1`
  - net queued candidate backlog growth: effectively `0` because both persisted candidates were consumed into documents during the same run
- Synthetic SQLite benchmark on this machine:
  - `25,000` rows: about `338k rows/sec`, `p95 submit ~= 2.26 ms`
  - `100,000` rows: about `324k rows/sec`, `p95 submit ~= 6.79 ms`

## Current decision

SQLite remains sufficient for the current local-first scope.

Reasoning:

- writer throughput is still high relative to the observed bounded stage budgets
- the main scaling risk exposed so far is uncontrolled discovery backlog growth, not SQLite write contention
- bounded discovery persistence is the correct immediate control, while larger storage changes would be premature without stronger contention evidence
