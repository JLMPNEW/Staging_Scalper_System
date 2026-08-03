# Surprise-Factor Candidate Spec (research kickoff, 2026-08-03)

Status: DRAFT research candidate. Shadow-only. First deliverable of the deferred-research
program (surprise factors → joint regime model → empirical betas → Kalman nowcast), unblocked
by the clean vintage backfill of 2026-08-02.

## Motivation

The feature layer contains no actual-vs-expected surprises; regime models consume levels and
transforms of the latest-known vintage only. First prints are now trustworthy for the
true-vintage block (ICSA, IC4WSA, PAYEMS, INDPRO, CFNAI, plus AHETPI backfills), enabling
Citi-style surprise indices with genuine PIT discipline.

## Construction (all expanding-window, PIT-gated)

1. **First print per period**: earliest vintage per (metric, observation_period) from
   `macro_observation_raw`, available at its vintage date.
2. **Expectation**: PIT AR(1) on the first-print series (fallback: random walk / seasonal
   naive for weekly claims), fit expanding-window with labels available <= anchor date.
3. **Surprise** = (first_print − expectation) / expanding first-print std of surprises.
4. **Surprise index per regime block** (growth_now, growth_lead, inflation_now): decay-weighted
   (half-life ~60d) sum of the block's standardized surprises, one daily series each.
5. **Revision factor**: per metric, expanding mean absolute (first_print − latest) as a
   reliability weight candidate for `source_quality_multiplier`.

## Validation gates (before any composite membership)

- Walk-forward correlation of each surprise index with the corresponding composite's forward
  1m/3m change; block-bootstrap CI excluding zero (reuse H1's seeded circular bootstrap).
- No-look-ahead audit: every surprise value's availability = vintage date of its first print;
  assert max(availability) <= as_of for every served row.
- Ablation: v2_2-style candidate with surprise indices as added predictors vs without, same
  walk-forward windows, paired Brier difference with block bootstrap.

## Adoption path

New feature rows (`feature_name = 'surprise_index'`) in a namespaced research table first;
promotion into `macro_feature_daily`/composite policy only via a V2_4-style candidate spec with
sealed evidence, per existing governance. No change to v1.

## Immediate next commands

- Builder scaffold: `build_macro_surprise_factors.py` (reads macro_raw, writes
  `macro_surprise_research` table + dated manifest under `out/surprise_research/`).
- Then: joint-model prototype (multinomial ridge on growth×inflation quadrants) per the
  audit's §4.5, reusing `macro_probability_v2.py` primitives.

## Blockers / notes

- Staging Stage 2 price panel has stale tails for 814/1273 tickers (as of 2026-08-02); this
  blocks meaningful shadow-backtest deltas but NOT surprise-factor research (macro-only).
- EIA/OECD series have no first prints (no vintage archive); surprise factors are
  true-vintage-block only by construction.
