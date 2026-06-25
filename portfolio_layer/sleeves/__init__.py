"""Stage 8 - multi-horizon sleeves + risk-allocation engine (shadow-only).

Re-allocates the RISK of the sealed Stage 7 fused book (factor-neutralized, diversified into many
low-correlation bets, risk placed where the information ratio is highest), partitioned into horizon
sleeves with regime-conditional risk budgets. Emits a proposal; never mutates the Stage 7 book.
"""
