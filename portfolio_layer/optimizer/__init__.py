"""Vendored institutional optimizer (tier1) + Stage 3 thin mean-variance baseline wrapper.

`tier1_portfolio_optimizer.py` and `tier1_common.py` are a self-contained, re-rooted copy of the PROD
optimizer (Black-Litterman + Pearson/Kendall covariance scenarios + long/short) — independent of
PROD_Scalper_System. Stage 3 uses only a thin long-only mean-variance solver that injects the Stage 2
covariance directly; the full tier1 BL/scenario machinery is wired in at Stage 7.
"""