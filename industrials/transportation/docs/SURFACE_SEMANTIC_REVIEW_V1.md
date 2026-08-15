# Surface Freight Semantic Review V1

## Purpose

This gate adjudicates the 119 HIGH-priority surface-freight definitions created
from parser run 105 at the `2026-07-30` point-in-time cutoff. It prevents a
generic parser signature from promoting semantically different values, such as
equipment capex instead of equipment counts or yield levels instead of yield
growth.

## Governed sequence

1. `36q_review_transportation_surface_semantic_definitions.py` verifies the
   frozen queue, recalculates exact fact-store ratios, hashes every referenced
   parser source document, and applies definition and row-level semantic gates.
2. `36r_replay_transportation_surface_semantic_approvals.py` verifies the review
   manifest and replays approved rows into an immutable shadow artifact. It
   does not reparse filings or mutate canonical candidates.
3. `36s_audit_transportation_surface_post_review_coverage.py` consumes the
   sealed replay once and rewrites the domain-coverage artifacts with accepted
   replay periods included and rejected HIGH candidates excluded from the
   unresolved queue.

All stages remain fail-closed for calibration and production promotion.

## Executed result

- 119 HIGH definitions reviewed.
- 87 definitions approved and 32 rejected.
- 7,252 represented candidate rows checked.
- 5,020 rows accepted and 2,232 rejected by policy.
- 544 unique parser source documents physically hash-verified.
- Zero source-document reparses and zero canonical candidate mutations.
- 10 of 36 calibration-candidate metric-domain rules meet both breadth and
  historical-depth gates after replay; 19 additional rules are diagnostic-only.

The ten passing metric-domain rules are:

- `operating_ratio`: `rail_networks`, `ltl_carriers`, and
  `truckload_intermodal`
- `purchased_transportation_ratio`: `truckload_intermodal`
- `fleet_or_equipment_count`: `truckload_intermodal`
- `average_length_of_haul`: `ltl_carriers`
- `freight_weight_per_shipment`: `ltl_carriers`
- `revenue_per_shipment_or_load`: `ltl_carriers`
- `logistics_net_revenue_margin`: `asset_light_logistics`
- `revenue_per_tractor_or_power_unit`: `truckload_intermodal`

The authoritative artifacts are stored under
`output/industrials/transportation/investable_v3/surface_delta/2026-07-30/`.
