# Eligible Transportation Positioning and Metric Recovery — 2026-08-01

## Decision

Transportation research is centered on the 24 active, rank-ready members of the
outcome-blind `surface_freight_and_logistics` policy. Membership is not selected
from realized returns. The structural surface-freight universe remains available
for point-in-time membership changes; the 24 names are the current eligible
cross-section, not a survivor-only historical hardcode.

Positioning is now a point-in-time scoring component for these names. Its current
production weight remains `0.00`. It may enter a candidate score only through the
train-only metric-selection gates and may not be activated from the revealed
transportation holdout.

## Current eligible names

`ARCB, CHRW, CNI, CP, CSX, CVLG, EXPD, FDX, FWRD, GXO, HUBG, JBHT, KNX,
LSTR, NSC, ODFL, RLGT, SAIA, SNDR, TFII, UNP, UPS, WERN, XPO`

All 24 are rank ready at 2026-07-30.

## Positioning result

- Positioning score populated: **24/24**.
- Domestic issuers: four applicable inputs — insider net value, insider cluster
  buyers, institutional ownership change, and short-interest change.
- CNI, CP, and TFII: **2/2 applicable inputs observed**. Their configured foreign-
  private-issuer Form 4 exemption is honored; they are not penalized for an
  inapplicable Section 16 channel.
- The component uses the shared `feature_positioning` infrastructure and source
  `industrials_positioning_composite`.
- The historical score builder now calls the family-pinned transportation wrapper,
  so another subsector's override CSV cannot be used accidentally.

## Financial Tier A recovery

One bounded, network-free rebuild was run for the 24 names against facts already
stored in the shared industrials database. The shared concept map now covers the
loaded debt-and-capital-lease and interest concepts, and the financial builder
applies new aliases to the raw store without refetching or reparsing SEC filings.

| Metric | Before | After | Residual classification |
|---|---:|---:|---|
| interest coverage | 18/24 | **22/24** | CHRW and EXPD lack a current reported total interest-expense line |
| net debt / EBITDA | 9/24 | **22/24** | Pre-edge-audit state: EXPD's standard short-term-bank-loan tag and WERN's standard Other D&A tag were absent from the authoritative shared seed |
| FCF conversion | 22/24 | **22/24 usable** | FWRD and WERN are explicit `NOT_APPLICABLE` because net income is nonpositive |

The recovery added no zero-fill. Missing debt is not inferred as zero. CHRW's
`InterestPaidNet` is intentionally not mislabeled as reported interest expense.

## Eligible financial edge audit and materiality gate

The final four-cell draft was audited against the cached filings and shared raw
fact store before another rebuild. One assertion in that draft was wrong:
**EXPD is not debt-free**. Its 2025 10-K reports $30.263 million of subsidiary
credit-line borrowings and its 2026 Q1 filing reports $33 million. The standard
`ShortTermBankLoansAndNotesPayable` tag had not been mapped. It must not be
defaulted to zero.

Three 2026-07-30 shadow repairs are source backed:

| Ticker | Metric | Shadow value | Evidence |
|---|---|---:|---|
| CHRW | interest coverage | 13.1641x | FY2025 interest expense $63.1M, Q1 2026 $14.0M, Q1 2025 $16.8M; TTM $60.3M |
| EXPD | net debt / EBITDA | -1.1283x | Q1 borrowings $33M; cash and aligned TTM EBITDA already loaded |
| WERN | net debt / EBITDA | 2.6012x | `OtherDepreciationAndAmortization`; strict FY + current-Q1 - prior-Q1 TTM |

EXPD interest coverage remains `NOT_DISCLOSED`: the current filings do not
provide a standalone interest-expense amount. Combined other-income lines,
interest paid, tax interest, and lease liabilities are not substitutes.

The authoritative shared concept seed now includes both
`OtherDepreciationAndAmortization` and `ShortTermBankLoansAndNotesPayable`.
This is additive shared infrastructure; the raw-store blast-radius audit found
40 and 17 issuers respectively, so any future materialization must use the
network-free shared backfill and cross-subsector regression tests.

Before authorizing a full financial rebuild, the repairs were applied only in
memory to the governed 24-name current cross-section. The maximum score change
was 1.0721 points, the maximum absolute rank change was three, and top-quintile
membership did not change. A second, train-only upper-bound test then assigned
unrealistically favorable values to every still-missing CHRW/EXPD/WERN cell.
Even that optimistic case changed mean IC by **-0.00197** and top-minus-bottom
spread by only **+0.000217**.

Decision: retain the shared mapping corrections and source-backed evidence, but
do **not** rebuild all 24 financial histories or recalibrate from this repair.
The revealed validation and holdout were not used. The next expensive rebuild
should occur only after the final specialized-metric keep/drop decision, so all
accepted changes can be reconstructed once on a newly frozen point-in-time
panel.

## Specialized metrics: what is worth one final fixture batch

The all-inclusive search has already found the relevant documents. Do **not** run
another broad retrieval or parser sweep. Use only the existing candidate store.

| Metric | Accepted historical coverage | Existing review evidence | Decision |
|---|---|---|---|
| operating ratio | 11 issuers, 93 dates | 263 candidates, 3 issuers, 58 filings | Keep; resolve KNX, CP, and NSC in the bounded batch |
| purchased transportation ratio | 8 issuers, 89 dates | 209 candidates, 3 issuers, 66 filings | Keep as broker/logistics-specific research metric |
| pricing or yield growth | none accepted | 768 candidates, 14 issuers, 245 filings | Parse once; high breadth and economically distinct |
| transport volume growth | none accepted | 366 candidates, 13 issuers, 198 filings | Parse once with pricing to separate price from volume |
| asset utilization | none | no candidates | Drop; no source evidence and the financial proxy overlaps asset turnover |

The pricing/volume candidate batch should be treated as semantic fixture work,
not a new search. Resolve units and denominators per operating archetype, preserve
filing/accession lineage, and reject ambiguous tables rather than accepting a
numeric match.

## Efficient completion sequence

1. Freeze the 24-name current eligibility report and the structural point-in-time
   surface-freight membership policy.
2. Complete one candidate-only fixture batch for operating ratio, purchased
   transportation ratio, pricing/yield growth, and transport volume growth.
3. Rebuild metric availability once and apply the unchanged coverage/history gates.
   Drop metrics that still fail; do not loosen the gates.
4. Freeze the surviving metric registry and positioning definition.
5. Run **one** governed weekly historical reconstruction. The dry run identifies
   396 weekly dates from 2019-01-04 through 2026-07-30. This rebuild must include
   the corrected positioning route and the final metric set, avoiding a second pass.
6. Build a new research panel and run train-only candidate selection. Historical
   validation and holdout results are diagnostic-only because those outcomes have
   already been revealed.
7. Begin clean post-cutoff shadow evidence with the frozen model. Promotion remains
   fail closed until untouched evidence passes the existing return, IC, breadth,
   and governance gates.

## Acceptance gates before the one historical rebuild

- 24/24 current eligible names have a positioning score.
- CNI/CP/TFII show Form 4 `not_applicable`, not `missing`.
- Positioning weight remains zero in the current production/shadow configuration.
- The four selected specialized metrics have a frozen reviewed fixture set and a
  final keep/drop decision.
- No additional broad SEC/EX-99 retrieval is queued.
- Registry, membership policy, concept map, and source definitions are hashed and
  frozen before reconstructing the 396 weekly snapshots.
