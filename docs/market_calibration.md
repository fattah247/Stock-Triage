# Market Risk Calibration

**Date:** 2026-10-21
**Data:** IDX daily summaries and Bank Indonesia's JISDOR, 2020-01-02 to 2026-10-21 (1,638 trading days), synced by `market-sync`.
**Companion to:** `docs/market_risk_plan.md` (phase 5) and `docs/market_risk_factors.md`.

This note records what the phase 5 backtest found, what changed because of it,
and what did not work. Every number below comes from `market-backtest` and
the comparison scripts described here, run point-in-time: each day sees only
data known that day.

## 1. The market state

### What the backtest found

The phase 2 rules put IHSG in a stressed or crisis state on 36% of days and
in calm on only 21%. Four of six episodes were false alarms. One signal
explained most of it: the **worst sector's drawdown** read crisis on 56% of
all days and stressed on another 23%. A maximum over 11 sectors is always
extreme somewhere, and some sectors (technology above all) never regained
their 2021 highs, so the signal was close to permanently on. In practice the
state was "IHSG drawdown plus any one other signal".

### Variants compared

Each variant was replayed over the whole history. "Justified" stressed days
are days in a stressed or crisis state when IHSG was already at least 10%
below its 52-week high, or fell at least 10% further within 63 sessions.
"Missed" deep days are days at least 15% below the high that read below
stressed. Leads are the sessions of unbroken stressed-or-worse state before
each of the three major bottoms (2020-03-24, 2025-04-09, 2026-10-19).

| Variant | Calm | Elevated | Stressed | Crisis | Justified | Missed deep | Golden | Leads |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Worst sector (phase 2) | 21% | 42% | 18% | 18% | 74% | 8% | 4/4 | 18 / 42 / 171 |
| Worst sector, crisis needs IHSG at stressed | 21% | 42% | 19% | 17% | 74% | 8% | 4/4 | 18 / 42 / 171 |
| Worst sector, thresholds 12/20/35, plus gate | 23% | 43% | 18% | 17% | 77% | 8% | 4/4 | 18 / 42 / 171 |
| Share of sectors ≥15% down, 40/60/80% | 40% | 35% | 8% | 17% | 95% | 9% | 4/4 | 18 / 22 / 117 |
| Share ≥20% down, 30/50/70% | 43% | 33% | 7% | 17% | 95% | 10% | 4/4 | 18 / 21 / 117 |
| **Share ≥10% down, 50/70/90%, plus gate (adopted)** | **36%** | **35%** | **13%** | **17%** | **86%** | **8%** | **4/4** | **18 / 40 / 150** |

### What changed (ruleset `market-state-v2`)

- **The sector signal is now `sector_breadth`**: the share of IDX-IC sectors at least 10% below their 52-week high. It reads elevated at 50%, stressed at 70% and crisis at 90% of sectors. The worst-sector version remains available as `market.sector_signal: worst`.
- **A two-signal crisis also needs IHSG itself at least 10% below its high** (`market.regime_crisis_needs_index: stressed`). A 20% fall in IHSG alone still makes a crisis.
- The adopted variant trades some precision (86% justified rather than 95%) for earlier warning: 40 sessions before the April 2025 bottom instead of 22, and 150 before October 2026 instead of 117.

The July 2025 case from phase 2 now reads **stressed**, not crisis. With the full history in the window, IHSG really was 12% below its September 2024 record then. It was not 5%, as the 2025-only backfill had suggested.

### Episodes under the adopted rules

| Opened | Closed | Worst | Max drawdown | Bottom | Recovered the high | Further 10% fall within 63 sessions |
|---|---|---|---:|---|---|---|
| 2020-02-27 | 2020-12-01 | crisis | 37.7% | 2020-03-24 | 2021-01-11 | yes |
| 2022-07-01 | 2022-08-23 | stressed | 8.8% | 2022-07-04 | 2022-09-13 | no |
| 2022-10-17 | 2024-01-19 | stressed | 10.3% | 2023-03-16 | 2024-01-02 | no |
| 2024-04-29 | 2024-10-04 | stressed | 9.5% | 2024-06-19 | 2024-08-14 | no |
| 2024-11-29 | 2025-08-27 | crisis | 24.5% | 2025-04-09 | 2025-08-14 | yes |
| 2026-01-29 | open | crisis | 45.3% | 2026-10-19 so far | not yet | yes |

The three false alarms all stayed at stressed. Every episode that reached
crisis was a real crash.

### Golden dates

| Date | Expected | State | Basis (IDX closes) |
|---|---|---|---|
| 2020-03-24 | at least crisis | crisis | lowest close of 2020, 3,937.6, 37.7% below the January high of 6,325.4 |
| 2024-09-19 | at most elevated | calm | record close of 7,905.4 |
| 2025-04-08 | at least crisis | crisis | first session after Lebaran: 6,510.6 to 5,996.1 (−7.9%), 24% below the record |
| 2026-10-19 | at least crisis | crisis | 4,996.6, 45.3% below the January 2026 high of 9,134.7 |

`intelligence-golden` and `market-backtest` both run these; `market-backtest` exits 1 if one fails.

## 2. The crash-risk estimate

A crash is a fall of 10% or more from a day's close within 63 sessions
(`market.crash_risk_*`). The model is a ridge-penalised logistic regression,
refitted every 21 sessions on days whose outcome was known by then. It is
scored on 1,242 days, 200 of which a crash followed.

| Model | Crash definition | Brier | Base rate's Brier | Skill | Warnings that caught a crash | Warnings that were false alarms |
|---|---|---:|---:|---:|---:|---:|
| 9 features (adopted, `crash-risk-v1`) | 10% in 63 sessions | 0.153 | 0.145 | −6% | 6% | 73% |
| 9 features | 20% in 63 sessions | 0.068 | 0.065 | −4% | 4% | 89% |
| 3 features (drawdown, volatility, trend) | 10% in 63 sessions | 0.153 | 0.145 | −6% | 0% | 100% |
| 3 features | 20% in 63 sessions | 0.077 | 0.065 | −18% | 0% | 100% |

**No version beats the plain base rate**, so the estimate is withheld: the
dashboard, `crash-risk` and alerts show the past episodes today resembles,
and no probability. This is the design working as intended (plan, "Non-goals:
certainty"). It is also an honest answer to "can it predict a crash": on
2020–2026 IDX data, these market features do not forecast a 10% fall better
than its base rate.

Why, as far as the data shows:

- **Few independent events.** Two hundred crash days sound like a lot, but they come from about four separate falls, and overlapping 63-session windows make neighbouring days near-copies.
- **Regime change.** The model learns from 2020 that a deep drawdown precedes recovery, while 2025 and 2026 kept falling.
- **What moved these crashes was not in prices beforehand:** a pandemic, tariff and fiscal news, an index-provider consultation. That is the case for the news layer (phase 4) and for the analogs, which describe what followed similar days rather than predicting.

The estimate keeps being refitted and scored daily. It will be shown the day
its walk-forward record beats the base rate by `market.crash_risk_min_skill`
(5%), and alerts (`crash_risk`) fire only then.

## 3. Signals added in phases 2b and 4

- **`exchange_notices`** (suspensions plus UMA notices over five sessions, against the past year's usual): data starts on 2023-10-23, and the signal reads only from about April 2024. It has been elevated on 3% of days and stressed on 1%. Speculative frenzies drive UMA more than crashes do, so it is a weak crash signal; the backtest should keep judging it.
- **`active_themes`** (high-severity risk themes active at once): it has no history before watch-sync started, so it cannot be backtested yet. It needs news archives from past episodes (plan, risk 3).

## 4. Re-run on IDX's history from 2009 (2026-10-22)

`market-sync` now also fetches IHSG daily closes for 2009–2019 from IDX's own
statistics reports, and JISDOR from May 2013 (plan, "History before 2020").
That gives 4,304 trading days, 2009-01-05 to 2026-10-21. The days before 2020
carry only the close and the rupiah, so on them the state rests on drawdown,
volatility and the rupiah.

- **Golden dates:** all four still pass.
- **States:** calm 60%, elevated 22%, stressed 9%, crisis 9% of days.
- **Episodes:** 11, five of them before 2020.

| Opened | Closed | Worst | Max drawdown | Bottom | Further 10% fall within 63 sessions |
|---|---|---|---:|---|---|
| 2011-09-22 | 2011-12-16 | crisis | 22.0% | 2011-10-04 | no |
| 2013-06-13 | 2014-02-18 | crisis | 23.9% | 2013-08-27 | yes |
| 2015-08-12 | 2016-02-25 | crisis | 25.4% | 2015-09-28 | no |
| 2016-05-20 | 2016-07-14 | stressed | 11.5% | 2016-05-24 | no |
| 2018-05-07 | 2018-12-03 | stressed | 15.8% | 2018-07-03 | no |

The bottoms match the known lows of the 2011 euro crisis, the 2013 taper
tantrum, the 2015 yuan devaluation and the 2018 rupiah slide. The finding in
section 1, that every episode reaching crisis was a real crash, no longer
holds. In 2011 and 2015 the state reached crisis only after most of the fall,
so less than 10% followed.

**The crash-risk estimate is still withheld.** On 3,871 scored days, 645 of
which a crash followed, its Brier score is 0.163 against the base rate's
0.144: skill −13%. Its warnings caught a crash 7% of the time, and 83% of them
were false alarms. Five more crashes did not make the price features
predictive. Before 2020 the model sees only drawdown, volatility, trend,
momentum and the rupiah, and counts its other inputs as average.

## 5. Caveats

- **The rules were tuned on the same history they are judged on,** which holds three crashes. The structural change (share instead of worst) is motivated by how a maximum over 11 sectors behaves, not by the data alone. The 10% cutoff and the 50/70/90 levels were picked from six candidates.
- **Re-run `market-backtest` after each new episode closes,** and compare against this table before changing a threshold.
- **Foreign flow is an estimate** valued at average traded prices (docs/phase2_market_recon.md).

## How to reproduce

```bash
python -m stock_triage market-sync --backfill-from 2020-01-02   # the first run also fetches 2009-2019
python -m stock_triage market-signals
python -m stock_triage crash-risk
python -m stock_triage market-backtest      # writes <data_dir>/reports/market_backtest.md
```
