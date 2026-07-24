# Macro Regime H1 Hybrid — Frozen Pre-Registration Spec

**Status: FROZEN as of 2026-07-19, prior to any H1 build, evidence run, or allocation test.**
H1 is a NEW campaign (not V2.4). Its components were selected using the 2001–2026 evaluation
window that the V2.1/V2.2/V2.3 campaign consumed (three candidate looks); H1's legitimacy
therefore rests ENTIRELY on the prospective evidence rules below. No historical result may
promote H1; historical runs are diagnostic only.

## Identity and storage

| Field | Value |
|---|---|
| model_version | `macro_regime_h1_hybrid_v1` |
| config block | `probability_h1:` |
| output_dir | `MacroLayer/out/regime_h1` |
| shadow_only | `true` |
| storage | H1 probability/regime/decision rows live in the v2-family tables under the H1 model_version (PKs are model_version-scoped); H1 writes NO training artifacts (it fits nothing) |
| prospective evidence cutoff | **2026-07-19** — promotion evidence counts ONLY outcomes whose labels resolve from data strictly after this date |
| review cadence | quarterly; first promotion review no earlier than the second quarterly review (~2027-Q1) |

## Components (pinned, with provenance)

| H1 slot | Source | Rationale |
|---|---|---|
| P_G_NOW | **V1** `macro_probabilities_daily` key `P_G_NOW` | production incumbent; V2-family recal degraded growth cells |
| P_G_LEAD | **V1** `macro_probabilities_daily` key `P_G_LEAD` | **inherited, NOT validated** — every candidate family's growth-lead is weak; H1 does not claim to fix it (that is campaign V3) |
| P_PI_NOW | **V2.2** (`macro_regime_v2_2_recalibrated_v1`) key `P_PI_NOW_V2` | VALIDATED: AUC 0.945, Brier skill +0.624, slope 1.30 (raw fails slope 1.79 — recalibrated form required) |
| P_PI_LEAD | **V2.1** (`macro_regime_v2_1_independent_outcomes_v1`) key `P_PI_LEAD_V2` (raw) | VALIDATED at slope 1.35 with the HIGHER discrimination (AUC 0.859, Brier +0.434) vs V2.2's recalibrated 0.767/+0.356 — recalibration is piecewise and cost ranking quality |
| energy_shock_score / energy_shock_flag | **V2.1** `macro_regime_v2_daily` row for the same date | identical unrevised WTI/Brent inputs across v2-family variants |

Quadrant math identical to the v2 family: current regime from (P_G_NOW, P_PI_NOW), next regime
from (P_G_LEAD, P_PI_LEAD) via the shared `regime_probabilities` quadrant composition;
smoothing/decision by the unchanged `regime_layer` machinery under the H1 model_version.

## PIT-safe adapter rules (fail-closed)

For each as_of date D, the adapter joins the five component rows AT date D exactly (no
forward-fill, no nearest-date). A date is COVERED only if every component row (a) exists,
(b) has `coverage_flag = 1` (energy row: exists), (c) has a finite probability, and (d) has
`as_of_date == D` (staleness = any substitute date; future = any row beyond the build end
date is never read). Any failure ⇒ the H1 row is written with `coverage_flag = 0` and null
probabilities. **Fallback rule: consumers of an uncovered H1 date fall back to the V1 label —
never to a partial quadrant.** The adapter seals a manifest with component row counts,
model_versions, coverage stats, and a determinism hash of its full output.

## Validation gates (every build)

1. probability conservation: quadrant probabilities sum to 1 within 1e-8 on every covered row;
2. coverage: reported covered fraction + first/last covered dates;
3. determinism: rebuilding the same window reproduces an identical output hash;
4. PIT lineage: every covered H1 row's components verifiably exist in their source tables with
   matching values (spot-audited per build);
5. growth pass-through byte-equality: H1's P_G_NOW/P_G_LEAD equal V1's values exactly.

## Promotion contract (prospective only; fail-closed)

H1 promotes to `macro.regime_source` ONLY when a sealed H1 evidence run reports acceptance
PROMOTABLE, defined as ALL of the following on post-cutoff data only:

1. **Inflation superiority:** on resolved post-cutoff outcomes, each inflation component's
   Brier improvement vs the corresponding V1 cell > 0, with ≥ 12 resolved monthly PI_NOW
   outcomes and ≥ 4 resolved quarterly PI_LEAD outcomes;
2. **Growth non-inferiority:** byte-equal pass-through of V1 growth probabilities (verified
   per build); no separate statistical test — the growth side is V1;
3. **Decision quality:** H1 current-regime confidence gates on the latest covered date
   (top probability ≥ 0.50, confidence ≥ 0.10) and post-cutoff quadrant Brier
   (H1 current-regime probabilities vs realized quadrant) ≤ the same measure for V1;
4. **Operational:** ≥ 95% of post-cutoff business days covered; determinism and lineage gates
   green on the evidence build.

No gate may be edited after this freeze. The existing 4-cell absolute gate is explicitly NOT
H1's contract (H1's growth-lead is inherited-not-validated by design and disclosed as such);
this contract was defined BEFORE any H1 evidence existed.

## Downstream preconditions (before H1 feeds Stages 7–8, even in shadow comparison)

The regime→gross and regime→sleeve-budget mappings must carry EXPLICIT, intentional entries
for all four regimes — HEATING_UP and EXPANSION_DISINFLATION in particular, since H1's
inflation components will surface them more often than V1 did. The regime-conditional research
corpus (71/72/16c) is V1-labeled and must be re-derived under H1 labels before any Stage 7–8
promotion that leans on regime conditioning.

## Campaign separation

V3 (growth-lead redesign) is a separate future campaign with its own pre-registration and a
fresh evaluation window; it may begin only after this spec is frozen and does not modify H1.

---

# AMENDMENT 1 — 2026-07-19 (issued while prospective outcomes = 0; cutoff re-affirmed)

Issued before any post-cutoff outcome exists; this is the final contract. The prospective
cutoff remains 2026-07-19 and now refers to THIS amended contract. No further amendment is
permitted once the first post-cutoff outcome resolves.

## A1.1 Evidence source: append-only prospective ledger

Each daily H1 build APPENDS that day's rows (as_of_date, capture_date_utc, the four component
probabilities, coverage_flag, current/next regime + quadrant probabilities, per-row digest) to
`out/regime_h1/prospective_ledger.csv`. Rows are never rewritten or deleted; re-captures of an
existing (as_of_date) keep the FIRST capture (first-write-wins) so later rebuilds cannot revise
history. ALL promotion evidence is computed from the ledger, never from rebuildable DB tables.
A ledger row is evidence-eligible only if `capture_date_utc - as_of_date <= 7 calendar days`
(live-captured, not replayed).

## A1.2 Amended sample and uncertainty rules

- FIRST REVIEW (informational, cannot promote): ≥4 resolved post-cutoff PI_LEAD outcomes and
  ≥12 PI_NOW outcomes.
- FINAL PROMOTION requires, per inflation component, on paired post-cutoff ledger dates:
  PI_NOW ≥ 18 resolved outcomes with ≥4 in each class; PI_LEAD ≥ 8 resolved outcomes with
  ≥2 in each class; Brier improvement vs V1 > 0 AND its paired bootstrap 90% confidence
  interval (≥1000 resamples of the paired per-outcome Brier differences, seeded 20260719)
  excluding 0.

## A1.3 Quadrant-Brier gate (implements frozen gate 3 precisely)

Realized CURRENT quadrant at date D = quadrant implied by the resolved (growth-now label,
inflation-now label) whose target windows contain D's period, taken from the sealed v2-family
target tables. H1 and V1 are scored on the IDENTICAL paired date set (only dates where both
quadrant probability vectors are covered in the ledger/V1 tables AND the realized quadrant
resolves post-cutoff). Gate: multiclass Brier(H1 current-quadrant probabilities) ≤
Brier(V1 current-quadrant probabilities). NEXT-regime quadrant Brier is computed and reported
as a diagnostic only.

## A1.4 Coverage measurement

The ≥95% post-cutoff coverage gate is measured over business days from cutoff+1 through
(evidence end − 5 business days), excluding the trailing component-publication lag window.

## A1.5 Component drift guard

The FIRST sealed evidence run records a baseline: sha256 of the component builders
(`build_macro_probabilities_v2.py`, `macro_probability_v2.py`, `build_macro_h1_hybrid.py`),
the `probability_h1`/`probability_v2_1`/`probability_v2_2` config blocks, and per-component
historical row digests (pre-cutoff window). Every later evidence run recomputes and FAILS
(reason `component_drift`) on any mismatch. Baseline lives in
`out/regime_h1/h1_prospective_baseline.json` and is itself hash-covered by every manifest.

## A1.6 Sealed promotion manifest

Every evidence run writes `h1_promotion_manifest.json` containing sha256 hashes of: the
evidence JSON, this spec file, `config_macro_raw.yaml`, the hybrid build manifest and
validation JSON, the H1 decision manifest (if present), the ledger file, the baseline file,
and the component builder sources; plus cutoff/evidence dates and component model versions.
`macro/contract.py::h1_promotion_status` must verify EVERY hash and the PROMOTABLE acceptance
before the h1 regime source unlocks; any mismatch is a hard error.

## A1.7 Economic non-inferiority gate (walk-forward)

A standing walk-forward arm pair (V1-regime-driven vs H1-regime-driven, identical machinery,
gross scalars from `regime_to_gross_scalar`) runs in the backtest/16 chain. FINAL promotion
additionally requires, on the latest sealed arm comparison: net_ann_return(H1 arm) ≥
net_ann_return(V1 arm) − 0.005 AND max_drawdown(H1 arm) ≤ max_drawdown(V1 arm) + 0.02
(historical window = diagnostic context; the gate exists to catch gross mis-mapping, not to
prove alpha). Values and thresholds are frozen here.

---

# AMENDMENT 2 - 2026-07-19 (issued while prospective outcomes = 0; audit hardening)

Issued after Amendment 1 and BEFORE any post-cutoff outcome exists. As of this amendment the
prospective ledger, the new outcomes ledger, and every post-cutoff sample are empty - **zero
post-cutoff outcomes exist** - so tightening evidence provenance, tamper-evidence, and
statistical rigor cannot alter, cherry-pick, or advantage any resolved result. Nothing here
weakens a frozen gate; every change only makes promotion harder or its evidence more auditable.
The prospective cutoff remains **2026-07-19**. No further amendment is permitted once the first
post-cutoff outcome resolves.

## A2.0 Review-timeline correction (supersedes the "~2027-Q1" wording)

The identity table's "review cadence" row and Amendment 1's first-review language implied a
first promotion review around 2027-Q1. That is arithmetically wrong: **PI_LEAD (quarterly
inflation-lead) resolves ~1 outcome per quarter.** Starting from the 2026-07-19 cutoff, the
>=4 resolved PI_LEAD outcomes needed for the FIRST (informational) review are not available
until **~ late 2027**, and the >=8 resolved PI_LEAD outcomes needed for FINAL promotion are not
available until **~ late 2028**. All earlier "~2027-Q1" references are superseded by this
timeline. Sample floors themselves (A1.2) are unchanged.

## A2.1 Outcomes ledger + V1 comparators captured at build time (ledger-only evidence)

Two hardening changes remove the last live-DB dependency from evidence computation:

1. The prospective ledger schema gains four **V1 comparator columns captured at build time** -
   `v1_p_g_now`, `v1_p_g_lead`, `v1_p_pi_now`, `v1_p_pi_lead` - the V1
   `macro_probabilities_daily` P_G_NOW/P_G_LEAD/P_PI_NOW/P_PI_LEAD values at each as_of_date.
   They feed BOTH the inflation-component superiority test and the V1 side of the quadrant
   Brier, so V1's comparison probabilities are frozen at capture, not re-read from rebuildable
   tables.
2. A second **append-only outcomes ledger** `out/regime_h1/outcomes_ledger.csv` is added.
   First-write-wins on `(component, predictor_as_of_date)`. Every evidence/build run appends any
   NEWLY-resolved post-cutoff labels - components `growth_now`, `pi_now`, `pi_lead`, taken from
   `macro_probability_v2_target` model `macro_regime_v2_1_independent_outcomes_v1` (keys
   `P_G_NOW_V2`/`P_PI_NOW_V2`/`P_PI_LEAD_V2`) with `label_available_date > cutoff` - recording
   `label_value`, `label_available_date`, `capture_date_utc`, and the row digest.

After this amendment, promotion **evidence computation reads ONLY the two ledgers** for every
probability (H1 and V1 comparator) and every realized label; no live SQLite read of a
probability or label occurs in the gate math. (Two live reads remain and are explicitly NOT
evidence-revisable: the appraisal of H1's own latest decision confidence - frozen gate 3, by
definition "on the latest covered date" - and the label-capture step that writes NEW rows into
the outcomes ledger. Both only ADD to the sealed record; neither can revise a captured value.)
The prospective-ledger schema change is safe because the ledger was verified empty at amendment
time (a non-empty legacy ledger halts the change).

## A2.2 Tamper-evident hash-chained ledgers

BOTH ledgers become hash chains. Each row carries `prev_row_digest`; each `row_digest` now
covers the row's canonical payload (exact CSV cell strings, `capture_date_utc`/`prev_row_digest`
/`row_digest` excluded) **plus the previous row's digest**. The genesis predecessor is the
literal `H1-GENESIS`. Appends take an exclusive `os.O_CREAT|os.O_EXCL` lock file (bounded retry
loop) so concurrent writers cannot interleave. The promotion evaluator verifies EVERY row digest
and full chain continuity on both ledgers, and verifies the chain HEAD against the last sealed
head stored in the baseline: on first run each head is recorded; on later runs the stored head
must still appear in the recomputed chain (append-only continuity, verified by recomputation)
and the baseline head is then advanced monotonically. Any digest mismatch, broken link,
truncation, or head that no longer extends the sealed chain is the hard reason
`ledger_integrity_failure`.

## A2.3 Evidence/decision date parity in the contract

`21_build_macro_contract.py` now mirrors the V2 evidence-date check for H1: the H1 evidence's
`evidence_as_of_date` must equal the selected macro regime row's `macro_as_of_date`, else the
build errors out. `22_validate_macro_contract.py` mirrors the same parity check
(`h1_promotion_decision_date_mismatch`).

## A2.4 Expanded drift/seal graph

The A1.5 baseline and the A1.6 sealed manifest are extended to cover the full decision graph:
`build_macro_probabilities.py` (V1 builder), `build_macro_regime_v2_decision.py`,
`validate_macro_h1_promotion.py` ITSELF, `backtest/16d_run_h1_v1_regime_arms.py`, and the
canonical serializations of the `probability_layer` and `regime_layer` config blocks (in
addition to the previously sealed `probability_h1`/`probability_v2_1`/`probability_v2_2`
blocks). The config blocks are drift-guarded in the baseline (canonical JSON sha) and are also
byte-sealed transitively via `config_macro_raw.yaml` in the manifest; the manifest additionally
records that canonical config-block sha, and `contract.py` cross-checks it against the
hash-verified baseline. The 16d source and the A1.7 gate file are sealed under a new
`portfolio_root` manifest anchor that `contract.py::_verify_h1_manifest` verifies.

## A2.5 A1.7 gate consumption at promotion time

The promotion evaluator reads `output/h1_walkforward/latest_a17_gate.json`. FINAL promotion now
requires `a17_gate_pass == true` AND that the gate file was generated within **400 days** of the
evidence date; otherwise the evaluator adds `a17_gate_missing`, `a17_gate_stale`, or
`a17_gate_fail`. The gate file's hash is sealed in the promotion manifest and re-verified by
`contract.py`.

## A2.6 Deeper H1 build validation

`validate_macro_h1_hybrid.py` now compares EVERY actual DB row in BOTH tables
(`macro_probability_v2_daily` and `macro_regime_v2_daily`, all value columns) against the
freshly recomputed adapter rows - not just the aggregate digest - and enforces probability
bounds in [0, 1] on every probability and quadrant value. The adapter's energy coverage rule
adopts the **stricter** interpretation: a date is covered only if `energy_shock_flag` is in
{0, 1} AND, when `energy_shock_flag == 1`, `energy_shock_score` is finite (a flagged shock with
a null score is NOT coverage). `energy_shock_flag == 0` with a null score remains explicitly
allowed.

## A2.7 Statistical hardening

The IID bootstrap is replaced by a seeded **circular block bootstrap** (block length 3 for the
monthly PI_NOW cell and the current-quadrant Brier; block length 2 for the quarterly PI_LEAD
cell; seed 20260719; 1000 resamples) to respect serial dependence in overlapping monthly/
quarterly outcomes. The quadrant-Brier gate additionally requires **>= 12 paired current-quadrant
outcomes** AND a paired block-bootstrap 90% CI whose upper bound on the (H1 - V1) multiclass
Brier difference is **<= +0.005**. Post-cutoff coverage is now measured over the system's own
sealed calendar - the distinct as_of_dates actually present in the H1 ledger inside the
[cutoff+1, evidence_end - 5 business days] window - rather than numpy weekday arithmetic.

## A2.8 Contract verification

`contract.py` verification is extended to cover every new manifest entry (the `portfolio_root`
anchor and its 16d/A1.7-gate files, the outcomes ledger, the expanded builder/config seal set,
and the recorded config-block sha cross-check against the baseline). All additions are
fail-closed: any missing, extra, or mismatched artifact is a hard error and the H1 regime source
stays locked.

---

# AMENDMENT 3 - 2026-07-19 (issued while prospective ledgers and outcomes = 0)

Issued before any post-cutoff capture or resolved outcome exists. Two execution defects found
during the final 2026-07-17 dry run are corrected without changing any predictor, coefficient,
threshold, gate, cutoff, or promotion requirement:

1. V2.2/V2.3 trailing recalibration now copies the temporary NumPy probability vector before
   mutation. This only restores compatibility with pandas copy-on-write; recalibration math is
   unchanged.
2. The A1.5 component baseline now records `component_baseline_end_date`, equal to the evidence
   date on which the baseline is created (bounded by the prospective cutoff). Later drift checks
   recompute historical-row digests only through that fixed boundary. The former implementation
   hashed through the future cutoff, so ordinary rows arriving between baseline creation and the
   cutoff falsely appeared as component drift. Builder/config hashes and all rows that actually
   existed at baseline creation remain frozen and fail-closed.

The pre-Amendment-3 baseline is archived before replacement. Re-baselining is permitted once for
this amendment only after verifying both prospective ledgers have zero rows and no post-cutoff
outcome exists.

---

# AMENDMENT 4 - 2026-07-19 (issued while prospective ledgers and outcomes = 0)

Issued before the first post-cutoff business-day capture. The complete four-provider raw-data
refresh for the 2026-07-17 evidence boundary finished after the Amendment-3 dry run. It advanced
the EIA energy series from 2026-07-06 to 2026-07-13 and rebuilt all PIT component histories from
one consistent FRED/ALFRED, EIA, OECD, and Philadelphia Fed snapshot. The drift guard correctly
reported changed historical-row digests for all four H1 components.

This amendment permits one final baseline replacement against that complete raw snapshot. It does
not change any H1 predictor, coefficient, component source, gate, threshold, cutoff, bootstrap,
minimum sample, or promotion requirement. Before replacement, both hash-chained ledgers must be
verified at `H1-GENESIS` with zero rows and no post-cutoff outcome. The Amendment-3 baseline must
be archived with its SHA-256 identity. After replacement, all component and code/config drift
checks remain fail-closed; no further re-baseline is permitted once prospective capture begins.
