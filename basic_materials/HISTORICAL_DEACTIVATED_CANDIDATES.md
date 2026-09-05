# Basic Materials historical deactivated-company candidate census

As of 2026-09-05, the package contains a 72-security review queue spanning every production cohort. This is the immutable Stage 2B intake list for survivorship-bias remediation. A separate governed pilot now represents 20 of these candidates as effective-dated historical memberships. The candidate file itself remains unchanged, and neither the pilot nor the remaining candidates can enter calibration yet.

## Candidate coverage

| Cohort | Candidates | Proposed historical tickers |
|---|---:|---|
| steel_producers_processors | 8 | AKS, X, RDUS, USAP, HAYN, MUSA, GNA, ZEUS |
| specialty_chemicals_materials | 10 | SIAL, VAL, GRA, KRA, CHMT, CYT, SHLM, FOE, OMN, IPHS |
| mining_royalty_streaming | 4 | MMX, SAND, ROY, NSR |
| precious_metals_producers | 13 | GG, GOLD, KL, AUY, SWC, TAHO, RIC, AUQ, GSS, PVG, KLDX, GATO, ANV |
| industrial_metals_mining | 11 | ACO, RTI, TIE, TRQ, PLM, MCP, GMO, ZINC, TC, ALTM, BRSS |
| commodity_chemicals | 7 | AXLL, TPCG, SEH, TREC, DTRX, BIOA, CCC |
| building_materials | 10 | TXI, HW, FRTA, USCR, SUM, GCP, USG, CBPX, CNR, DOOR |
| agricultural_inputs_crop_science | 9 | MON, POT, AGU, TRA, TNH, RNF, SYT, MBII, AGFS |
| **Total** | **72** | **55 Tier 1 and 17 Tier 2 candidates** |

The census intentionally contains strategic acquisitions, stock mergers, partnership exits, bankruptcies, liquidation cases, foreign issuers, development-stage miners, and ticker-reuse cases. That variety is needed to test terminal returns, successor chains, left-tail outcomes, and identity controls. It should not be interpreted as evidence that every row is equally comparable or ready for model fitting.

## Files

- `system_csvs/basic_materials_deactivated_candidates.csv` is the version-controlled candidate source.
- `data/basic_materials_historical_candidate_policy.yaml` fixes the 72-row and per-cohort contract.
- `core/historical_candidates.py` validates identity fields, dates, statuses, cohort counts, evidence-state semantics, and closed calibration gates.
- `scripts/02b_validate_basic_materials_deactivated_candidates.py` is the command-line gate.
- `review/basic_materials_deactivated_candidate_review.xlsx` is the human review workbook with a control cover, cohort summary, filterable census, and field guide.
- `system_csvs/basic_materials_historical_membership.csv` is the reviewed 20-security pilot membership.
- `system_csvs/basic_materials_ticker_aliases.csv`, `basic_materials_security_events.csv`, and `basic_materials_terminal_events.csv` govern identity and terminal lineage.
- `data/basic_materials_historical_reconciliation_policy.yaml` and `basic_materials_historical_reconciliation_manifest.yaml` seal the pilot contract.
- `core/historical_membership.py` owns strict cross-file validation, atomic loading, database validation, and report publication.

Run the gate from the repository root:

```powershell
C:\Users\josel\Miniconda3\python.exe basic_materials\scripts\02b_validate_basic_materials_deactivated_candidates.py
C:\Users\josel\Miniconda3\python.exe basic_materials\scripts\01b_load_basic_materials_historical_membership.py
C:\Users\josel\Miniconda3\python.exe basic_materials\scripts\02c_validate_basic_materials_historical_membership.py
```

## Current evidence state

- 72 candidate rows are present across all eight cohorts.
- 55 rows are Tier 1 and 17 are Tier 2.
- 71 rows have a resolved Norgate deactivated-security symbol and asset ID.
- 16 rows have an event-source URL in the initial census.
- NSR is deliberately blocked because the Norgate root symbol resolves to an unrelated Neustar security; the Nomad Royalty asset must be mapped independently.
- Every row has `include_in_historical_universe=0` and `calibration_eligible=0`.
- Separate reviewed artifacts promote X, RDUS, HAYN, ZEUS, VAL, MMX, SAND, GOLD (Randgold), GATO, ANV, MCP, GMO, ALTM, BIOA, AXLL, SUM, USCR, MON, POT, and AGU into the Stage 2B engineering pilot.
- The pilot covers nine cash acquisitions, six stock outcomes, one mixed outcome, and four bankruptcy cases.
- All 20 pilot terminal rows remain `survivorship_complete=0` and `calibration_eligible=0`; no zero recovery or successor price is inferred.

Norgate final/current classification and Current & Past watchlists are used for candidate discovery and quote-range evidence. They do not by themselves establish point-in-time cohort membership. Primary SEC or issuer evidence is still required for each lifecycle episode and terminal event.

## Promotion checklist

A candidate can be promoted from this queue only after all of the following are complete:

1. Resolve the legal issuer, security, provider asset ID, exchange, and all ticker aliases.
2. Reconstruct effective-dated listing and cohort-membership episodes from sources available at each date.
3. Verify the terminal event and economic effective date from a primary filing or issuer release.
4. Reconcile cash, stock, unit, CVR, fractional-share, and successor-security consideration as applicable.
5. Validate adjusted prices through the last tradable session and distinguish exchange delisting from later OTC trading.
6. Verify point-in-time financial-statement and specialized-metric coverage.
7. Record a human review decision and immutable evidence hashes.
8. Load the approved row through a separate Stage 2B historical-membership artifact; never edit the current 134-name universe to add it.

The first 20 candidates have completed identity, effective-dated membership, primary terminal-term intake, and Stage 3 adjusted-price reconciliation through separate governed artifacts. Sixteen terminal outcomes are now evidence-backed as calculable; ANV, MCP, GMO, and BIOA remain unresolved pending verified old-equity distribution evidence. The other 52 candidates remain outside the historical pilot. Stage 4 must add point-in-time financial history before the later panel stage can consider calibration. Calibration remains blocked until terminal, financial, and panel gates pass.
