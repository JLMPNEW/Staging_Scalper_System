"""Stage 8 risk engine - pure functions over the sealed Stage 2 annualized covariance.

All risk lives in the asset covariance `Sigma` (annualized). The factor model falls straight out of
`Sigma` because it already contains the market + sector ETFs, so betas are `B = Cov(A,F) Omega_f^-1`
with no new regression. CASH carries no risk and is excluded from every computation here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RiskContribs:
    total_variance: float
    annual_vol: float
    rc: dict[str, float]          # risk-contribution share per ticker (sums to 1)
    sigma: dict[str, float]       # standalone annual vol per ticker (sqrt(Sigma_ii))


@dataclass(frozen=True)
class RcCapEnforcement:
    weights: dict[str, float]
    converged: bool
    iterations: int
    cash_added: float
    max_rc: float
    trimmed: tuple[dict[str, Any], ...]


def aligned_cov(cov: pd.DataFrame, tickers: list[str]) -> np.ndarray:
    """Return the Sigma sub-block for `tickers`, symmetric and finite, or raise."""
    missing = [t for t in tickers if t not in cov.index or t not in cov.columns]
    if missing:
        raise ValueError(f"covariance missing tickers: {missing[:20]}")
    sub = cov.loc[tickers, tickers].to_numpy(dtype=float)
    if not np.isfinite(sub).all():
        raise ValueError("covariance sub-block has non-finite values")
    return 0.5 * (sub + sub.T)


def risk_contributions(weights: dict[str, float], cov: pd.DataFrame, *, eps: float = 1e-18) -> RiskContribs:
    tickers = [t for t, w in weights.items() if w != 0.0]
    if not tickers:
        raise ValueError("no non-zero risky weights for risk contribution")
    sigma_mat = aligned_cov(cov, tickers)
    w = np.array([float(weights[t]) for t in tickers], dtype=float)
    sigma_w = sigma_mat @ w
    var = float(w @ sigma_w)
    if not math.isfinite(var) or var <= eps:
        raise ValueError(f"portfolio variance w'Sigma w={var} is not strictly positive")
    rc_vals = (w * sigma_w) / var
    if not np.isfinite(rc_vals).all():
        raise ValueError("non-finite risk contributions")
    stand = {t: float(math.sqrt(max(0.0, sigma_mat[i, i]))) for i, t in enumerate(tickers)}
    return RiskContribs(
        total_variance=var,
        annual_vol=float(math.sqrt(max(0.0, var))),
        rc={t: float(rc_vals[i]) for i, t in enumerate(tickers)},
        sigma=stand,
    )


def factor_decomposition(
    weights: dict[str, float],
    cov: pd.DataFrame,
    *,
    market_etf: str,
    sector_etfs: dict[str, str],
) -> dict:
    """Multi-factor variance decomposition from Sigma: systematic / idiosyncratic / per-factor shares.

    Factors = market ETF + the sector ETFs present in Sigma. B = Cov(A,F) Omega_f^-1;
    portfolio factor exposure g = B' w; systematic var = g' Omega_f g; idiosyncratic = w' diag(resid) w.
    """
    assets = [t for t, w in weights.items() if w != 0.0]
    if not assets:
        raise ValueError("no risky weights for factor decomposition")
    factor_labels: list[str] = []
    factor_tickers: list[str] = []
    if market_etf and market_etf in cov.index:
        factor_labels.append("market")
        factor_tickers.append(market_etf)
    for pipe, etf in sector_etfs.items():
        if etf in cov.index and etf not in factor_tickers:
            factor_labels.append(f"sector:{pipe}")
            factor_tickers.append(etf)
    if not factor_tickers:
        raise ValueError("no factor ETFs found in covariance")

    sigma_aa = aligned_cov(cov, assets)
    omega_f = aligned_cov(cov, factor_tickers)
    cross = cov.loc[assets, factor_tickers].to_numpy(dtype=float)  # Cov(A, F)
    w = np.array([float(weights[t]) for t in assets], dtype=float)

    omega_inv = np.linalg.pinv(omega_f)
    betas = cross @ omega_inv                       # A x K
    g = betas.T @ w                                 # K  (portfolio factor exposures)
    omega_g = omega_f @ g
    systematic_var = float(g @ omega_g)
    systematic_cov_diag = np.einsum("ak,kl,al->a", betas, omega_f, betas)  # per-asset systematic var
    resid_var = np.clip(np.diag(sigma_aa) - systematic_cov_diag, 0.0, None)
    idio_var = float(np.sum((w ** 2) * resid_var))
    total_var = float(w @ sigma_aa @ w)
    if total_var <= 0:
        raise ValueError("non-positive total variance in factor decomposition")

    per_factor = {factor_labels[k]: float(g[k] * omega_g[k]) / total_var for k in range(len(factor_labels))}
    market_share = per_factor.get("market", 0.0)
    sector_shares = {lbl.split(":", 1)[1]: share for lbl, share in per_factor.items() if lbl.startswith("sector:")}
    return {
        "total_variance": total_var,
        "systematic_share": systematic_var / total_var,
        "idiosyncratic_share": idio_var / total_var,
        "residual_correlation_share": 1.0 - (systematic_var + idio_var) / total_var,
        "market_share": market_share,
        "sector_shares": sector_shares,
        "max_sector_share": max(sector_shares.values()) if sector_shares else 0.0,
        "per_factor_share": per_factor,
        "factor_exposure": {factor_labels[k]: float(g[k]) for k in range(len(factor_labels))},
    }


def effective_number_of_bets(weights: dict[str, float], cov: pd.DataFrame) -> dict:
    """Meucci/PCA effective number of bets: ENB = exp(-sum p_k ln p_k), p_k = principal-portfolio var share."""
    tickers = [t for t, w in weights.items() if w != 0.0]
    if not tickers:
        raise ValueError("no risky weights for ENB")
    sigma_mat = aligned_cov(cov, tickers)
    w = np.array([float(weights[t]) for t in tickers], dtype=float)
    eigvals, eigvecs = np.linalg.eigh(sigma_mat)
    v = eigvecs.T @ w
    contrib = np.clip((v ** 2) * eigvals, 0.0, None)
    total = float(contrib.sum())
    if total <= 0:
        raise ValueError("non-positive variance in ENB")
    p = contrib / total
    nz = p[p > 1e-15]
    entropy = float(-np.sum(nz * np.log(nz)))
    return {
        "enb": float(math.exp(entropy)),
        "n_names": len(tickers),
        "top_eigen_var_share": float(p.max()),
    }


def information_ratios(alpha: dict[str, float], sigma: dict[str, float], *, eps: float = 1e-12) -> dict[str, float]:
    """IR_i = annual alpha / standalone annual vol."""
    out: dict[str, float] = {}
    for ticker, a in alpha.items():
        s = sigma.get(ticker)
        if s is None or s <= eps or not math.isfinite(a):
            continue
        out[ticker] = float(a) / float(s)
    return out


def target_risk_budget(
    *,
    members: dict[str, list[str]],
    sleeve_budgets: dict[str, float],
    information_ratio: dict[str, float],
    tilt: float,
    rc_cap: float,
) -> dict[str, float]:
    """Create deterministic per-name risk budgets from sleeve budgets and positive IR.

    A zero tilt is meaningful and produces equal risk budgets within each sleeve. Invalid
    policy values fail closed rather than being silently replaced by defaults.
    """
    tilt_value = float(tilt)
    cap = float(rc_cap)
    if not math.isfinite(tilt_value) or not 0.0 <= tilt_value <= 1.0:
        raise ValueError(f"tilt must be finite and in [0,1], got {tilt}")
    if not math.isfinite(cap) or cap <= 0.0:
        raise ValueError(f"rc_cap must be positive and finite, got {rc_cap}")
    present = {sleeve: sorted(set(tickers)) for sleeve, tickers in members.items() if tickers}
    total_budget = sum(max(0.0, float(sleeve_budgets.get(sleeve, 0.0))) for sleeve in present)
    if not math.isfinite(total_budget) or total_budget <= 0.0:
        raise ValueError("present sleeves have no positive finite aggregate risk budget")

    budgets: dict[str, float] = {}
    for sleeve, tickers in sorted(present.items()):
        sleeve_raw = float(sleeve_budgets.get(sleeve, 0.0))
        if not math.isfinite(sleeve_raw) or sleeve_raw < 0.0:
            raise ValueError(f"invalid sleeve risk budget {sleeve}={sleeve_raw}")
        sleeve_share = sleeve_raw / total_budget
        if sleeve_share <= 0.0:
            continue
        positive_ir = {ticker: max(0.0, float(information_ratio.get(ticker, 0.0))) for ticker in tickers}
        if not all(math.isfinite(value) for value in positive_ir.values()):
            raise ValueError(f"non-finite information ratio in sleeve {sleeve}")
        ir_sum = sum(positive_ir.values())
        equal = 1.0 / len(tickers)
        for ticker in tickers:
            tilted = positive_ir[ticker] / ir_sum if ir_sum > 0.0 else equal
            budgets[ticker] = sleeve_share * ((1.0 - tilt_value) * equal + tilt_value * tilted)

    # Normalize and iteratively redistribute capped excess among names with capacity.
    for _ in range(max(16, len(budgets) + 1)):
        total = sum(budgets.values())
        if total <= 0.0:
            raise ValueError("per-name risk budgets collapsed to zero")
        budgets = {ticker: value / total for ticker, value in budgets.items()}
        over = {ticker for ticker, value in budgets.items() if value > cap + 1e-12}
        if not over:
            return {ticker: value for ticker, value in budgets.items() if value > 0.0}
        capped = sum(cap for _ in over)
        free = {ticker: value for ticker, value in budgets.items() if ticker not in over}
        free_total = sum(free.values())
        if not free or free_total <= 0.0 or capped >= 1.0 - 1e-12:
            raise ValueError(
                f"per-name RC cap {cap} is infeasible for {len(budgets)} names"
            )
        residual = 1.0 - capped
        budgets = {
            ticker: cap if ticker in over else residual * value / free_total
            for ticker, value in budgets.items()
        }
    raise ValueError("per-name target risk-budget cap redistribution did not converge")


def trailing_book_drawdown(
    weights: dict[str, float],
    returns: pd.DataFrame,
    *,
    window: int,
    min_complete_fraction: float = 0.80,
) -> float:
    """Current drawdown (<= 0) of the book held statically over the last `window` panel days.

    Shared by the Stage 8 drawdown throttle and the Stage 12 risk governor so both read the same
    number for the same book.
    """
    if window < 2:
        raise ValueError(f"drawdown window must be >=2, got {window}")
    if not 0.0 < min_complete_fraction <= 1.0:
        raise ValueError(f"min_complete_fraction must be in (0,1], got {min_complete_fraction}")
    active = {
        str(t).strip().upper(): float(w)
        for t, w in weights.items()
        if math.isfinite(float(w)) and abs(float(w)) > 1e-15
    }
    if not active:
        raise ValueError("drawdown requires at least one non-zero finite risky weight")
    missing = sorted(t for t in active if t not in returns.columns)
    if missing:
        raise ValueError(f"drawdown return coverage missing held names: {missing[:12]}")
    names = sorted(active)
    w = np.array([active[t] for t in names], dtype=float)
    tail = returns[names].tail(window).apply(pd.to_numeric, errors="coerce")
    complete = tail.dropna(axis=0, how="any")
    required = max(2, int(math.ceil(min(window, len(tail)) * min_complete_fraction)))
    if len(complete) < required:
        raise ValueError(
            f"drawdown has {len(complete)} complete observations, requires {required} "
            f"({min_complete_fraction:.0%} of {min(window, len(tail))})"
        )
    sub = complete.to_numpy(dtype=float)
    port = sub @ w
    equity = np.concatenate(([1.0], np.cumprod(1.0 + port)))
    running = np.maximum.accumulate(equity)
    return float((equity / running - 1.0)[-1])


def throttle_scale(
    drawdown: float,
    dd_limit: float,
    *,
    sigma_target: float | None = None,
    sigma_realized: float | None = None,
) -> float:
    """Continuous drawdown throttle: clip(1 - dd/dd_limit, 0, 1), optionally vol-target adjusted.

    The final scalar is capped at 1.0 so the throttle cannot add gross risk by itself.
    """
    dd = float(drawdown)
    limit = float(dd_limit)
    if not math.isfinite(dd) or not math.isfinite(limit) or limit <= 0.0:
        raise ValueError(f"drawdown throttle needs finite drawdown and positive dd_limit, got {drawdown=}, {dd_limit=}")
    base = max(0.0, min(1.0, 1.0 - max(0.0, dd) / limit))
    if sigma_target is None and sigma_realized is None:
        return base
    if sigma_target is None or sigma_realized is None:
        raise ValueError("sigma_target and sigma_realized must be supplied together")
    target = float(sigma_target)
    realized = float(sigma_realized)
    if not math.isfinite(target) or not math.isfinite(realized) or target < 0.0 or realized <= 0.0:
        raise ValueError(
            f"vol-target throttle needs finite sigma_target>=0 and sigma_realized>0, "
            f"got {sigma_target=}, {sigma_realized=}"
        )
    return max(0.0, min(1.0, base * (target / realized)))


def solve_risk_budget(
    cov: pd.DataFrame,
    budgets: dict[str, float],
    *,
    gross: float,
    max_weight: float,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> dict[str, float]:
    """Long-only risk-budgeting portfolio (Spinu fixed point): RC_i -> budget_i, then cap to max_weight.

    Fixed point w_i <- b_i / (Sigma w)_i (normalized) converges to the portfolio whose risk
    contributions match `budgets`. Then scale to `gross` and project onto the per-name weight cap.
    """
    tickers = [t for t, b in budgets.items() if b > 0.0]
    if not tickers:
        raise ValueError("risk budget needs at least one positive-budget name")
    sigma_mat = aligned_cov(cov, tickers)
    b = np.array([float(budgets[t]) for t in tickers], dtype=float)
    b = b / b.sum()
    w = b.copy()
    for _ in range(max(1, int(max_iter))):
        sigma_w = sigma_mat @ w
        if not np.all(sigma_w > 0):
            break
        w_new = b / sigma_w
        s = w_new.sum()
        if s <= 0 or not np.isfinite(s):
            break
        w_new = w_new / s
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    w = w / w.sum() * float(gross)
    cap = float(max_weight)
    for _ in range(64):
        over = w > cap + 1e-15
        if not over.any():
            break
        w = np.minimum(w, cap)
        deficit = float(gross) - float(w.sum())
        free = ~over
        free_sum = float(w[free].sum())
        if deficit <= 1e-15 or free.sum() == 0 or free_sum <= 0:
            break
        w[free] = w[free] + deficit * (w[free] / free_sum)
    return {t: float(w[i]) for i, t in enumerate(tickers) if w[i] > 0.0}


def build_sleeve_risk_proposal(
    cov: pd.DataFrame,
    target_budgets: dict[str, float],
    *,
    prior_weights: dict[str, float],
    sleeve_of: dict[str, str],
    gross: float,
    max_weight: float,
    max_iter: int = 50,
) -> tuple[dict[str, float], str, tuple[str, ...]]:
    """Build the pre-RC-cap proposal and disclose whether cross-sleeve allocation applies.

    A single active sleeve has no cross-sleeve risk budget to solve. Re-running the full
    risk-parity optimizer in that state would introduce an unsupported within-sleeve book
    rewrite. Preserve the sealed Stage 7 weights instead; the caller still applies the hard
    realized-RC cap and sends any required trim to CASH.
    """
    positive_prior = {
        str(ticker): float(weight)
        for ticker, weight in prior_weights.items()
        if float(weight) > 0.0
    }
    if not positive_prior:
        raise ValueError("sleeve proposal needs at least one positive prior weight")
    missing = sorted(ticker for ticker in positive_prior if not str(sleeve_of.get(ticker, "")).strip())
    if missing:
        raise ValueError(f"positive prior weights missing sleeve assignments: {missing[:20]}")
    active_sleeves = tuple(sorted({str(sleeve_of[ticker]).strip() for ticker in positive_prior}))
    if len(active_sleeves) == 1:
        return positive_prior, "single_sleeve_rc_cap_only", active_sleeves
    proposal = solve_risk_budget(
        cov,
        target_budgets,
        gross=gross,
        max_weight=max_weight,
        max_iter=max_iter,
    )
    return proposal, "multi_sleeve_risk_budget", active_sleeves


def enforce_rc_cap_to_cash(
    weights: dict[str, float],
    cov: pd.DataFrame,
    *,
    rc_cap: float,
    tol: float = 1e-6,
    max_iter: int = 64,
) -> RcCapEnforcement:
    """Trim realized risk-contribution cap breaches to CASH.

    The risk-budget solver caps target RC budgets, but realized RC can drift when the covariance and
    weight caps bind. This function enforces the realized cap directly. Freed weight is not
    redistributed to other risky names, so the operation only de-risks the proposal.
    """
    cap = float(rc_cap)
    if not math.isfinite(cap) or cap <= 0.0:
        raise ValueError(f"rc_cap must be positive and finite, got {rc_cap}")
    current = {str(t): float(w) for t, w in weights.items() if float(w) > 0.0}
    if not current:
        raise ValueError("cannot enforce RC cap on an empty risky book")

    initial_gross = sum(current.values())
    records: list[dict[str, Any]] = []
    max_rc = 0.0
    converged = False
    iterations = 0
    for iterations in range(1, max(1, int(max_iter)) + 1):
        rc = risk_contributions(current, cov).rc
        over = [(ticker, share) for ticker, share in sorted(rc.items()) if share > cap + tol]
        max_rc = max(rc.values()) if rc else 0.0
        if not over:
            converged = True
            break
        for ticker, share in over:
            before = current.get(ticker, 0.0)
            if before <= 0.0:
                continue
            scale = math.sqrt(cap / share)
            scale = min(1.0, max(0.0, scale))
            after = before * scale
            if before - after <= 1e-14:
                after = before * 0.999
            current[ticker] = after
            records.append({
                "iteration": float(iterations),
                "ticker": ticker,
                "rc_before": float(share),
                "weight_before": float(before),
                "weight_after": float(after),
                "weight_trimmed": float(before - after),
            })
        current = {ticker: weight for ticker, weight in current.items() if weight > 1e-12}
        if not current:
            break
    if current:
        rc_final = risk_contributions(current, cov).rc
        max_rc = max(rc_final.values())
        converged = converged or max_rc <= cap + tol

    final_gross = sum(current.values())
    return RcCapEnforcement(
        weights=current,
        converged=converged,
        iterations=iterations,
        cash_added=max(0.0, initial_gross - final_gross),
        max_rc=float(max_rc),
        trimmed=tuple(records),
    )


def sleeve_risk_bounds(
    cov: pd.DataFrame,
    sleeve_of: dict[str, str],
    *,
    gross: float,
    max_weight: float,
    rc_cap: float,
    max_iter: int = 50,
    eps_budget: float = 1e-6,
) -> dict[str, dict[str, float]]:
    """Estimate feasible min/max sleeve RC shares under current names, caps, and Sigma.

    Bounds are solver-based diagnostics: for each sleeve, solve toward an extreme budget that strongly
    favors or disfavors that sleeve, then enforce realized per-name RC caps to CASH and measure the
    resulting sleeve RC share.
    """
    tickers = sorted(t for t, sleeve in sleeve_of.items() if sleeve)
    sleeves = sorted({sleeve for sleeve in sleeve_of.values() if sleeve})
    if not tickers or not sleeves:
        return {}

    out: dict[str, dict[str, float]] = {}
    for sleeve in sleeves:
        shares: dict[str, float] = {}
        for label, favor in (("min", False), ("max", True)):
            budgets = {
                ticker: (1.0 if (sleeve_of[ticker] == sleeve) == favor else eps_budget)
                for ticker in tickers
            }
            weights = solve_risk_budget(cov, budgets, gross=gross, max_weight=max_weight, max_iter=max_iter)
            enforced = enforce_rc_cap_to_cash(weights, cov, rc_cap=rc_cap, max_iter=max_iter)
            rc = risk_contributions(enforced.weights, cov).rc
            shares[label] = float(sum(share for ticker, share in rc.items() if sleeve_of.get(ticker) == sleeve))
        lo = min(shares["min"], shares["max"])
        hi = max(shares["min"], shares["max"])
        out[sleeve] = {"min": lo, "max": hi}
    return out
