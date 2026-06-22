"""Thin long-only mean-variance solver (cvxpy) over an INJECTED covariance.

Mirrors the QP core of the vendored tier1 optimizer (`maximize mu'w - 0.5*gamma*w'Sigma*w`) but stays
minimal for the Stage 3 AQR-only baseline: a single externally-built (Stage 2) covariance, long-only,
fully-invested. The full tier1 Black-Litterman / Pearson+Kendall-scenario / long-short machinery is
deferred to Stage 7.
"""
from __future__ import annotations

from typing import Any

import cvxpy as cp
import numpy as np


SOLVER_FALLBACK = ("ECOS", "OSQP", "SCS")


def project_to_capped_simplex(
    values: np.ndarray,
    *,
    gross: float,
    max_weight: float,
    tol: float = 1e-12,
) -> np.ndarray:
    """Euclidean projection onto {w: sum(w)=gross, 0<=w<=max_weight}."""
    v = np.asarray(values, dtype=float).flatten()
    n = len(v)
    if n == 0:
        return np.zeros(0)
    gross = float(gross)
    max_weight = float(max_weight)
    if gross < -tol:
        raise ValueError(f"gross must be non-negative, got {gross}")
    if max_weight < -tol:
        raise ValueError(f"max_weight must be non-negative, got {max_weight}")
    if gross == 0:
        return np.zeros(n)
    if max_weight * n < gross - tol:
        raise ValueError(f"max_weight*{n}={max_weight * n:.6f} < gross={gross:.6f}: infeasible cap")

    v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
    lo = float(np.min(v) - max_weight - abs(np.max(v)) - gross - 1.0)
    hi = float(np.max(v) + abs(np.min(v)) + gross + 1.0)
    for _ in range(200):
        mid = (lo + hi) / 2.0
        w = np.clip(v - mid, 0.0, max_weight)
        if float(w.sum()) > gross:
            lo = mid
        else:
            hi = mid
    w = np.clip(v - hi, 0.0, max_weight)

    residual = gross - float(w.sum())
    if abs(residual) > tol:
        if residual > 0:
            capacity = np.maximum(max_weight - w, 0.0)
            total_capacity = float(capacity.sum())
            if total_capacity < residual - tol:
                raise ValueError("Unable to project weights within cap; insufficient residual capacity")
            if total_capacity > 0:
                w += capacity * (residual / total_capacity)
        else:
            positive = np.maximum(w, 0.0)
            total_positive = float(positive.sum())
            if total_positive <= 0:
                raise ValueError("Unable to reduce projected weights; no positive weights")
            w += positive * (residual / total_positive)
    return np.clip(w, 0.0, max_weight)


def finalize_long_only_weights(
    weights: np.ndarray,
    *,
    min_weight: float,
    max_weight: float,
    gross: float,
) -> np.ndarray:
    """Drop dust weights, then re-project remaining names without violating the cap."""
    raw = np.nan_to_num(np.asarray(weights, dtype=float).flatten(), nan=0.0, posinf=0.0, neginf=0.0)
    raw = np.clip(raw, 0.0, None)
    n = len(raw)
    if n == 0:
        return np.zeros(0)
    gross = float(gross)
    max_weight = float(max_weight)
    min_weight = max(0.0, float(min_weight))
    if max_weight * n < gross - 1e-12:
        raise ValueError(f"max_weight*{n}={max_weight * n:.6f} < gross={gross:.6f}: infeasible cap")

    active = raw >= min_weight if min_weight > 0 else raw > 0
    order = list(np.argsort(-raw))
    if not active.any():
        needed = int(np.ceil(gross / max_weight)) if max_weight > 0 else n
        for idx in order[:needed]:
            active[idx] = True
    while int(active.sum()) * max_weight < gross - 1e-12:
        added = False
        for idx in order:
            if not active[idx]:
                active[idx] = True
                added = True
                break
        if not added:
            raise ValueError("Unable to keep gross exposure after dust removal under the per-name cap")

    # Project, then iteratively drop any sub-min_weight dust the projection created and re-project.
    # The projection of a set summing above gross can starve a marginal name into (0, min_weight);
    # re-projecting the survivors (fewer names => larger shares) removes it. Each pass removes >= 1 name
    # so this converges in <= n passes, leaving a strictly dust-free book that gate #4 can enforce.
    out = np.zeros(n)
    for _ in range(n + 1):
        idxs = np.where(active)[0]
        active_values = raw[idxs]
        if float(active_values.sum()) <= 0:
            active_values = np.ones(len(idxs), dtype=float)
        proj = project_to_capped_simplex(active_values, gross=gross, max_weight=max_weight)
        out = np.zeros(n)
        out[idxs] = proj
        if min_weight <= 0:
            break
        dust = (out > 0.0) & (out < min_weight)
        if not dust.any():
            break
        active = active & ~dust
        if int(active.sum()) * max_weight < gross - 1e-12:
            raise ValueError("dust removal left insufficient capacity for gross under the per-name cap")
    return out


def solve_long_only_mv(
    mu: np.ndarray,
    cov: np.ndarray,
    *,
    risk_aversion: float,
    max_weight: float,
    gross: float = 1.0,
    solver: str = "ECOS",
) -> tuple[np.ndarray, dict[str, Any]]:
    """Return (weights, solve_info). Long-only, sum(w)=gross, 0<=w<=max_weight."""
    n = len(mu)
    if n == 0:
        return np.zeros(0), {"status": "empty_universe", "solver_used": None}
    if max_weight * n < gross - 1e-9:
        raise ValueError(f"max_weight*{n}={max_weight * n:.3f} < gross={gross}: caps make full investment infeasible")
    w = cp.Variable(n, nonneg=True)
    risk = cp.quad_form(w, cp.psd_wrap(cov))
    objective = cp.Maximize(mu @ w - 0.5 * float(risk_aversion) * risk)
    constraints = [cp.sum(w) == gross, w <= max_weight]
    problem = cp.Problem(objective, constraints)

    order = [solver] + [s for s in SOLVER_FALLBACK if s != solver]
    status, used, obj_val, attempts = "no_solution", None, None, []
    for candidate in order:
        try:
            problem.solve(solver=candidate)
        except Exception as exc:  # noqa: BLE001 - try next solver
            attempts.append(f"{candidate}:{type(exc).__name__}")
            continue
        attempts.append(f"{candidate}:{problem.status}")
        if problem.status in ("optimal", "optimal_inaccurate") and w.value is not None:
            value = problem.value
            status, used = problem.status, candidate
            obj_val = float(str(value)) if value is not None else None
            break

    weights = np.clip(np.asarray(w.value, dtype=float).flatten(), 0.0, None) if w.value is not None else np.zeros(n)
    total = float(weights.sum())
    if total > 0:
        weights = weights * (gross / total)  # tidy tiny numerical drift back to exact gross
    return weights, {
        "status": status,
        "solver_requested": solver,
        "solver_used": used,
        "objective_value": obj_val,
        "solver_attempts": attempts,
        "max_weight": max_weight,
        "gross": gross,
        "risk_aversion": float(risk_aversion),
    }


def weight_sensitivity_band(
    mu: np.ndarray,
    cov: np.ndarray,
    *,
    gammas: list[float],
    max_weight: float,
    min_weight: float = 0.0,
    gross: float,
    solver: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-name [min, max] weight across a set of risk-aversion values (a robustness band)."""
    solutions = []
    for g in gammas:
        w, info = solve_long_only_mv(mu, cov, risk_aversion=g, max_weight=max_weight, gross=gross, solver=solver)
        if info["status"] not in ("optimal", "optimal_inaccurate"):
            raise ValueError(f"sensitivity solve failed for gamma={g}: {info}")
        if min_weight > 0:
            w = finalize_long_only_weights(w, min_weight=min_weight, max_weight=max_weight, gross=gross)
        solutions.append(w)
    stacked = np.vstack(solutions) if solutions else np.zeros((1, len(mu)))
    return stacked.min(axis=0), stacked.max(axis=0)
