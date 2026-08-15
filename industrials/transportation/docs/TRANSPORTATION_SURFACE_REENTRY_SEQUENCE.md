# Transportation Surface Re-entry Sequence

Effective evidence date: 2026-08-13

The v4 surface-freight expansion is gated by a single read-only audit of
CVLG, FWRD, HTLD, MRTN, and WERN. The audit consumes already-loaded data and
does not fetch filings, invoke the dedicated parser, rebuild historical
features, use realized returns, or alter the active universe.

The audit requires active operating membership, complete and liquid market
data, current rank-required metrics, two distinct complete financial periods,
an eligible financial reporting policy, explicit leverage/interest/cash-flow
evidence or a governed not-applicable condition, complete positioning inputs,
and an annual consolidated-revenue integrity check. Specialized metrics are
inventoried for the later comparison-domain gate; they are not incorrectly
required issuer by issuer.

The annual-revenue integrity check catches conflicting mapped revenue facts in
one annual accession when the largest value is more than twice the smallest.
That condition indicates that shared concept priority may have selected a
segment or subset fact rather than consolidated revenue. It is a data-integrity
review trigger, not an economic screen.

Only if all five candidates pass does the sequence continue to an expanded
surface comparison-domain audit and a versioned v5 universe. Otherwise v4
remains active and only the exact loaded-fact or metric gaps reported by the
audit are repaired. Historical reconstruction and calibration occur once,
after the final cohort and metric-domain contract are frozen.
