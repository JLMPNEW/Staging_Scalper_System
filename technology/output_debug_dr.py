import sqlite3

conn = sqlite3.connect(r"C:/Users/josel/Documents/STAGING/DB/technology.sqlite")
conn.row_factory = sqlite3.Row

print("=== 1. concept map seeded? ===")
for r in conn.execute(
    "SELECT canonical_metric, taxonomy, concept, priority FROM dim_xbrl_concept_map WHERE canonical_metric LIKE 'deferred_revenue%' OR canonical_metric='remaining_performance_obligation' ORDER BY canonical_metric, priority"
):
    print("  ", dict(r))

print("=== 2. raw facts taxonomy/concept for DDOG (sample) ===")
for r in conn.execute(
    """SELECT DISTINCT taxonomy, concept FROM fact_sec_xbrl_fact_raw
       WHERE ticker='DDOG' AND (concept LIKE '%ContractWithCustomerLiability%' OR concept LIKE '%RemainingPerformanceObligation%' OR concept='DeferredRevenueCurrent')
       LIMIT 12"""
):
    print("  ", dict(r))

print("=== 3. raw facts form_type/end_date sample for DDOG ContractWithCustomerLiabilityCurrent ===")
for r in conn.execute(
    """SELECT form_type, period_type, start_date, end_date, value FROM fact_sec_xbrl_fact_raw
       WHERE ticker='DDOG' AND concept='ContractWithCustomerLiabilityCurrent' ORDER BY end_date DESC LIMIT 5"""
):
    print("  ", dict(r))

print("=== 4. canonical rows for DDOG new metrics? ===")
n = conn.execute(
    """SELECT COUNT(*) FROM fact_financial_statement_canonical
       WHERE ticker='DDOG' AND canonical_metric IN ('deferred_revenue_current','deferred_revenue_total','remaining_performance_obligation')"""
).fetchone()[0]
print("  canonical new-metric rows for DDOG:", n)

print("=== 5. does DDOG have ANY canonical rows (was it rebuilt)? ===")
r = conn.execute("SELECT COUNT(*) c, MAX(updated_at) u FROM fact_financial_statement_canonical WHERE ticker='DDOG'").fetchone()
print("  ", dict(r))
conn.close()
