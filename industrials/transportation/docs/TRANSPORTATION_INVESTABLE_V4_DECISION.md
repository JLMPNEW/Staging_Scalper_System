# Transportation Investable Universe v4 Decision

Effective date: 2026-08-14  
Research-data cutoff: 2026-08-13  
Status: implemented; historical reconstruction, calibration, and promotion remain unauthorized

## Production boundary

The 120-name active research catalogue remains unchanged. Version 4 removes
the ten passenger-airline names from production calibration and portfolio
eligibility while retaining their prices, filings, fundamentals, specialized
evidence, and monitoring history.

The production-calibration universe is now:

- `surface_freight_core`: 19 names
- `oil_tanker_operators`: 11 names
- total: 30 names

The ten passenger airlines are classified as `airline_satellite_research` and appear in
the exact 90-name exclusion complement. The exclusion is based on model and
economic comparability, not on realized returns from a revealed test window.
An airline sleeve may re-enter only through an independent cohort contract,
adequate specialized-metric breadth, and fresh untouched out-of-sample gates.

## Fourth-cohort screen

The screen used the existing 120-name catalogue, the governed v3 exclusion
reasons, and a read-only 2026-08-13 market-feature build from already-loaded
prices. The production liquidity floor remains USD 5 million of 60-session
average dollar volume.

The generic diagnostic builder inherited defense benchmark labels during this
read-only run. The screen therefore used only benchmark-independent trading-day
and dollar-volume fields. Before any v4 historical reconstruction, the
transportation runner must explicitly pin `IYT`, `XTN`, and `SPY` and validate
the benchmark mapping in its manifest.

| Candidate peer set | Names reviewed | Liquidity pass | Decision |
| --- | ---: | ---: | --- |
| Latin American airport concessions | ASR, CAAP, OMAB, PAC | 3 of 4 | Economically coherent, but too small for a stable 20% cross-sectional selection; research only. CAAP was marginally below the liquidity floor at USD 4.96M. |
| Dry-bulk operators | CMDB, DSX, GNK, HSHP, SB, SBLK, SHIP, PANL | 3 of 8 | GNK, SB, and SBLK passed liquidity. The remaining breadth is insufficient; do not mix dry bulk with oil tankers merely to enlarge the cohort. |
| Aviation assets and leasing | AER, FTAI, WLFC | 3 of 3 | Liquid but only three names, with aircraft-leasing, engine-asset, and engine-leasing business-model differences; research only. |
| Surface-freight re-entry pool | CVLG, FWRD, HTLD, MRTN, WERN | 5 of 5 | Economically aligned with existing surface freight. Repair financial-policy and metric gaps, then evaluate re-entry into `surface_freight_core`; do not create an artificial fourth cohort. |

GXO and RXO also passed the liquidity screen, but their contract-warehousing
and recent standalone brokerage histories require separate comparability and
history gates. FDXF remains ineligible because it has only 55 trading sessions
at the cutoff.

## Decision

No fourth production cohort is authorized. The most efficient expansion path
is to repair the five liquid surface-freight re-entry candidates once and test
whether they satisfy the existing surface cohort's financial, point-in-time,
and specialized-domain contracts. Airport, dry-bulk, and aviation-asset peer
sets remain explicit research candidates until they have enough comparable,
liquid issuers for a defensible cross-sectional model.

## Acceptance gates

1. Research catalogue remains exactly 120 active primary listings.
2. Production universe is exactly 30 names: 19 surface freight and 11 tankers.
3. Passenger-airline intersection with production eligibility is empty.
4. Exclusions are the exact 90-name complement of the research catalogue.
5. Passenger-airline overlays use
   `portfolio_role=airline_satellite_research`.
6. Version 3 artifacts remain readable and valid.
7. Version 4 does not authorize reconstruction, calibration, or promotion.
