"""Thin long-only mean-variance solver (cvxpy) over an INJECTED covariance.

Mirrors the QP core of the vendored tier1 optimizer (`maximize mu'w - 0.5*gamma*w'Sigma*w`) but stays
minimal for the Stage 3 AQR-only baseline: a single externally-built (Stage 2) covariance, long-only,
fully-invested. The full tier1 Black-Litterman / Pearson+Kendall-scenario / long-short machinery is
deferred to Stage 7.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import cvxpy as cp
import numpy as np


SOLVER_FALLBACK = ("ECOS", "OSQP", "SCS")
CONSTRAINT_CASH_MARGIN = 1e-7


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


def _normalize_group_caps(
    n: int,
    group_caps: list[tuple[list[int], float]],
) -> list[tuple[np.ndarray, float]]:
    """Validate cap groups once and return canonical integer index arrays."""
    normalized: list[tuple[np.ndarray, float]] = []
    for raw_indices, raw_cap in group_caps:
        indices = [int(index) for index in raw_indices]
        if len(indices) != len(set(indices)):
            raise ValueError("group cap contains duplicate indices")
        if any(index < 0 or index >= n for index in indices):
            raise ValueError("group cap contains an invalid index")
        cap = float(raw_cap)
        if not np.isfinite(cap) or cap < 0.0:
            raise ValueError(f"group cap must be finite and non-negative, got {raw_cap!r}")
        normalized.append((np.asarray(indices, dtype=int), cap))
    return normalized


def _normalize_equal_weight_groups(
    n: int,
    equal_weight_groups: list[list[int]] | None,
) -> list[list[int]]:
    normalized_groups: list[list[int]] = []
    members: set[int] = set()
    for raw_indices in equal_weight_groups or []:
        indices = [int(index) for index in raw_indices]
        if len(indices) != len(set(indices)):
            raise ValueError("equal-weight group contains duplicate indices")
        if any(index < 0 or index >= n for index in indices):
            raise ValueError("equal-weight group contains an invalid index")
        overlap = members.intersection(indices)
        if overlap:
            raise ValueError(
                f"equal-weight groups overlap on indices {sorted(overlap)[:5]}"
            )
        members.update(indices)
        normalized_groups.append(indices)
    return normalized_groups


def maximum_investable_gross(
    n: int,
    *,
    group_caps: list[tuple[list[int], float]] | None,
    cap_base_gross: float,
    max_weight: float,
    equal_weight_groups: list[list[int]] | None = None,
) -> tuple[float, list[str]]:
    """Return the maximum NAV fraction investable under all hard caps.

    Group caps are expressed against ``cap_base_gross`` (normally the configured
    1.0 NAV budget), not against the eventually invested amount. This distinction
    lets a fail-closed sector outage de-risk into cash without shrinking every
    surviving sector's absolute cap a second time.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    base_gross = float(cap_base_gross)
    per_name_cap = float(max_weight)
    if not np.isfinite(base_gross) or base_gross < 0.0:
        raise ValueError(
            f"cap_base_gross must be finite and non-negative, got {cap_base_gross!r}"
        )
    if not np.isfinite(per_name_cap) or per_name_cap < 0.0:
        raise ValueError(
            f"max_weight must be finite and non-negative, got {max_weight!r}"
        )
    if n == 0:
        return 0.0, ["analytic:empty_universe"]

    normalized_caps = _normalize_group_caps(n, group_caps or [])
    normalized_equal = _normalize_equal_weight_groups(n, equal_weight_groups)
    if not normalized_caps and not normalized_equal:
        return float(n * per_name_cap), ["analytic:per_name_capacity"]

    probe = cp.Variable(n, nonneg=True)
    constraints: list[Any] = [probe <= per_name_cap]
    for indices, cap in normalized_caps:
        if indices.size:
            constraints.append(cp.sum(probe[indices]) <= cap * base_gross)
    for indices in normalized_equal:
        if len(indices) > 1:
            anchor = indices[0]
            constraints.extend(probe[index] == probe[anchor] for index in indices[1:])
    problem = cp.Problem(cp.Maximize(cp.sum(probe)), constraints)
    attempts: list[str] = []
    for solver in SOLVER_FALLBACK:
        try:
            problem.solve(solver=solver)
        except Exception as exc:  # noqa: BLE001 - use the next installed solver
            attempts.append(f"{solver}:{type(exc).__name__}")
            continue
        attempts.append(f"{solver}:{problem.status}")
        if problem.status in ("optimal", "optimal_inaccurate") and problem.value is not None:
            capacity = max(0.0, min(float(n * per_name_cap), float(str(problem.value))))
            return capacity, attempts
        if problem.status in ("infeasible", "infeasible_inaccurate"):
            return 0.0, attempts
    raise ValueError(f"unable to determine hard-cap investment capacity: {attempts}")


def constraint_aware_invested_gross(
    *,
    requested_gross: float,
    capacity: float,
    allow_constraint_cash: bool,
    decimals: int = 10,
) -> tuple[float, bool]:
    """Choose an exact invested gross while routing hard-cap shortfall to cash."""
    requested = float(requested_gross)
    available = float(capacity)
    if not np.isfinite(requested) or requested <= 0.0:
        raise ValueError(f"requested_gross must be finite and positive, got {requested_gross!r}")
    if not np.isfinite(available) or available < 0.0:
        raise ValueError(f"capacity must be finite and non-negative, got {capacity!r}")
    if available >= requested - 1e-8:
        return requested, False
    if not allow_constraint_cash:
        raise ValueError(
            f"hard caps leave capacity {available:.6f} < requested gross {requested:.6f}; "
            "constraint cash is disabled"
        )
    safe_capacity = available - CONSTRAINT_CASH_MARGIN
    if safe_capacity <= 0.0:
        raise ValueError(
            f"hard caps leave no positive investable capacity (capacity={available:.10f})"
        )
    scale = 10**decimals
    invested = float(np.floor(safe_capacity * scale + 1e-9) / scale)
    if invested <= 0.0:
        raise ValueError("constraint-aware invested gross rounded to zero")
    return invested, True


def rescale_group_caps_for_invested_gross(
    group_caps: list[tuple[list[int], float]],
    *,
    cap_base_gross: float,
    invested_gross: float,
) -> list[tuple[list[int], float]]:
    """Preserve absolute cap dollars when the invested gross is below target."""
    invested = float(invested_gross)
    if not np.isfinite(invested) or invested <= 0.0:
        raise ValueError(f"invested_gross must be finite and positive, got {invested_gross!r}")
    multiplier = float(cap_base_gross) / invested
    return [(list(indices), float(cap) * multiplier) for indices, cap in group_caps]


def check_group_cap_feasibility(
    n: int,
    *,
    group_caps: list[tuple[list[int], float]],
    gross: float,
    max_weight: float,
    equal_weight_groups: list[list[int]] | None = None,
) -> None:
    """Verify full investment under arbitrary, including overlapping, cap groups."""
    capacity, attempts = maximum_investable_gross(
        n,
        group_caps=group_caps,
        cap_base_gross=gross,
        max_weight=max_weight,
        equal_weight_groups=equal_weight_groups,
    )
    if capacity < float(gross) - 1e-8:
        raise ValueError(
            f"group caps leave capacity {capacity:.6f} < gross {gross:.6f}: "
            f"full investment infeasible ({attempts})"
        )


def solve_long_only_mv(
    mu: np.ndarray,
    cov: np.ndarray,
    *,
    risk_aversion: float,
    max_weight: float,
    gross: float = 1.0,
    solver: str = "ECOS",
    group_caps: list[tuple[list[int], float]] | None = None,
    equal_weight_groups: list[list[int]] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Return (weights, solve_info). Long-only, sum(w)=gross, 0<=w<=max_weight.

    group_caps: optional [(indices, cap_fraction)] budget caps — sum(w[indices]) <= cap*gross.
    """
    n = len(mu)
    if n == 0:
        return np.zeros(0), {"status": "empty_universe", "solver_used": None}
    if max_weight * n < gross - 1e-9:
        raise ValueError(f"max_weight*{n}={max_weight * n:.3f} < gross={gross}: caps make full investment infeasible")
    normalized_equal_groups = _normalize_equal_weight_groups(n, equal_weight_groups)
    if group_caps or normalized_equal_groups:
        check_group_cap_feasibility(
            n,
            group_caps=group_caps or [],
            gross=gross,
            max_weight=max_weight,
            equal_weight_groups=normalized_equal_groups,
        )
    w = cp.Variable(n, nonneg=True)
    risk = cp.quad_form(w, cp.psd_wrap(cov))
    objective = cp.Maximize(mu @ w - 0.5 * float(risk_aversion) * risk)
    constraints = [cp.sum(w) == gross, w <= max_weight]
    for indices, cap in group_caps or []:
        if indices:
            constraints.append(cp.sum(w[np.asarray(indices, dtype=int)]) <= float(cap) * gross)
    for indices in normalized_equal_groups:
        if len(indices) > 1:
            anchor = int(indices[0])
            constraints.extend(w[int(index)] == w[anchor] for index in indices[1:])
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


def finalize_with_group_caps(
    weights: np.ndarray,
    *,
    group_caps: list[tuple[list[int], float]],
    min_weight: float,
    max_weight: float,
    gross: float,
) -> np.ndarray:
    """Project to a dust-free book while preserving arbitrary overlapping caps.

    Sector and model-scope caps are hierarchical, so a name can legitimately belong to more than
    one capped group. A constrained projection is used instead of independently re-projecting each
    group (which double-counts overlapping budgets). Any sub-minimum positions produced by the
    projection are removed and the remaining active set is re-solved fail closed.
    """
    raw = np.nan_to_num(
        np.asarray(weights, dtype=float).flatten(), nan=0.0, posinf=0.0, neginf=0.0
    )
    raw = np.clip(raw, 0.0, None)
    n = len(raw)
    if n == 0:
        return np.zeros(0)
    gross = float(gross)
    max_weight = float(max_weight)
    min_weight = max(0.0, float(min_weight))
    normalized = _normalize_group_caps(n, group_caps)
    if not normalized:
        return finalize_long_only_weights(
            raw, min_weight=min_weight, max_weight=max_weight, gross=gross
        )
    check_group_cap_feasibility(
        n,
        group_caps=[(indices.tolist(), cap) for indices, cap in normalized],
        gross=gross,
        max_weight=max_weight,
    )

    active = np.ones(n, dtype=bool)
    for _ in range(n + 1):
        active_indices = np.where(active)[0]
        if active_indices.size == 0 or active_indices.size * max_weight < gross - 1e-10:
            raise ValueError("dust removal left insufficient per-name capacity under group caps")
        local_position = {int(index): pos for pos, index in enumerate(active_indices)}
        candidate = cp.Variable(active_indices.size, nonneg=True)
        constraints = [cp.sum(candidate) == gross, candidate <= max_weight]
        for indices, cap in normalized:
            positions = [
                local_position[int(index)]
                for index in indices
                if int(index) in local_position
            ]
            if positions:
                constraints.append(
                    cp.sum(candidate[np.asarray(positions, dtype=int)]) <= cap * gross
                )
        problem = cp.Problem(
            cp.Minimize(cp.sum_squares(candidate - raw[active_indices])), constraints
        )
        solved: np.ndarray | None = None
        attempts: list[str] = []
        for solver in SOLVER_FALLBACK:
            try:
                problem.solve(solver=solver)
            except Exception as exc:  # noqa: BLE001 - use the next installed solver
                attempts.append(f"{solver}:{type(exc).__name__}")
                continue
            attempts.append(f"{solver}:{problem.status}")
            if (
                problem.status in ("optimal", "optimal_inaccurate")
                and candidate.value is not None
            ):
                solved = np.clip(
                    np.asarray(candidate.value, dtype=float).flatten(), 0.0, None
                )
                break
        if solved is None:
            raise ValueError(
                f"group-cap finalization is infeasible after dust removal: {attempts}"
            )
        out = np.zeros(n)
        out[active_indices] = solved
        dust = (
            (out > 1e-10) & (out < min_weight - 1e-10)
            if min_weight > 0.0
            else np.zeros(n, dtype=bool)
        )
        if not dust.any():
            total = float(out.sum())
            if total > gross:
                excess = total - gross
                if excess > 1e-6:
                    raise ValueError(
                        "group-cap solver materially exceeded invested gross: "
                        f"sum={total:.12f}, gross={gross:.12f}"
                    )
                # Solvers satisfy equality constraints to numerical tolerance.
                # Scaling a tiny positive excess downward cannot breach an upper
                # cap and prevents decimal flooring from publishing over-gross.
                out *= gross / total
            elif gross - total > 1e-6:
                raise ValueError(
                    "group-cap solver materially underfilled invested gross: "
                    f"sum={total:.12f}, gross={gross:.12f}"
                )
            finalized = snap_rounded_weights(
                out,
                gross=gross,
                max_weight=max_weight,
                decimals=12,
                group_caps=[(indices.tolist(), cap) for indices, cap in normalized],
            )
            if ((finalized > 0.0) & (finalized < min_weight - 1e-10)).any():
                raise ValueError("rounded group-cap finalization created a dust holding")
            return finalized
        active &= ~dust
    raise ValueError("group-cap dust removal did not converge")


def snap_rounded_weights(
    weights: np.ndarray,
    *,
    gross: float,
    max_weight: float,
    decimals: int = 10,
    group_caps: list[tuple[list[int], float]] | None = None,
) -> np.ndarray:
    """Publish an exact-gross decimal book without breaching overlapping caps.

    This constrained largest-remainder apportionment starts from floor-rounded weights, then places
    residual units only where the per-name cap and every containing group still have room. If the
    requested precision cannot represent a feasible exact-gross book, publication fails closed.
    """
    values = np.asarray(weights, dtype=float).flatten()
    if not np.isfinite(values).all():
        raise ValueError("cannot round non-finite weights")
    if (values < -1e-12).any():
        raise ValueError("cannot round negative long-only weights")
    if decimals < 0 or decimals > 15:
        raise ValueError(f"decimals must be between 0 and 15, got {decimals}")
    values = np.clip(values, 0.0, None)
    n = len(values)
    normalized = _normalize_group_caps(n, group_caps or [])
    step = 10.0 ** -decimals
    gross_float = float(gross)
    if not np.isfinite(gross_float) or gross_float < 0.0:
        raise ValueError(f"gross must be finite and non-negative, got {gross}")
    # Gross is a decimal publication contract. Decimal(str(...)) strips binary
    # float representation noise while preserving operator-supplied precision.
    scale_decimal = Decimal(10) ** decimals
    gross_units = Decimal(str(gross_float)) * scale_decimal
    nearest_units = gross_units.to_integral_value(rounding=ROUND_HALF_UP)
    if abs(gross_units - nearest_units) > Decimal("0.000001"):
        raise ValueError(f"gross={gross} is not representable at {decimals} decimals")
    target_units = int(nearest_units)
    scaled = values / step
    units = np.floor(scaled + 1e-12).astype(np.int64)
    max_units = int(np.floor(float(max_weight) / step + 1e-9))
    if (units > max_units).any():
        raise ValueError("input weight exceeds the per-name cap at publication precision")
    group_limits = [
        int(np.floor(cap * float(gross) / step + 1e-9)) for _, cap in normalized
    ]
    group_totals = [int(units[indices].sum()) for indices, _ in normalized]
    if any(total > limit for total, limit in zip(group_totals, group_limits)):
        raise ValueError("floor-rounded input already breaches a group cap")

    remaining = target_units - int(units.sum())
    if remaining < 0:
        raise ValueError("floor-rounded weights exceed gross exposure")
    fractions = scaled - np.floor(scaled)
    memberships: list[list[int]] = [[] for _ in range(n)]
    for group_number, (indices, _) in enumerate(normalized):
        for index in indices:
            memberships[int(index)].append(group_number)
    order = sorted(
        range(n), key=lambda index: (-fractions[index], -values[index], index)
    )
    for index in order:
        if remaining == 0:
            break
        if int(units[index]) >= max_units:
            continue
        if any(
            group_totals[group_number] >= group_limits[group_number]
            for group_number in memberships[index]
        ):
            continue
        units[index] += 1
        for group_number in memberships[index]:
            group_totals[group_number] += 1
        remaining -= 1
    if remaining:
        raise ValueError(
            "cannot snap rounded weights to exact gross without breaching caps; "
            f"{remaining} decimal units unplaced"
        )
    rounded = np.round(units.astype(float) * step, decimals)
    if int(round(float(rounded.sum()) / step)) != target_units:
        raise ValueError("rounded weights do not close to exact gross")
    return rounded


def weight_sensitivity_band(
    mu: np.ndarray,
    cov: np.ndarray,
    *,
    gammas: list[float],
    max_weight: float,
    min_weight: float = 0.0,
    gross: float,
    solver: str,
    group_caps: list[tuple[list[int], float]] | None = None,
    equal_weight_groups: list[list[int]] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-name [min, max] weight across a set of risk-aversion values (a robustness band)."""
    solutions = []
    for g in gammas:
        w, info = solve_long_only_mv(
            mu, cov, risk_aversion=g, max_weight=max_weight, gross=gross, solver=solver, group_caps=group_caps,
            equal_weight_groups=equal_weight_groups,
        )
        if info["status"] not in ("optimal", "optimal_inaccurate"):
            raise ValueError(f"sensitivity solve failed for gamma={g}: {info}")
        if min_weight > 0:
            if group_caps:
                w = finalize_with_group_caps(
                    w, group_caps=group_caps, min_weight=min_weight, max_weight=max_weight, gross=gross,
                )
            else:
                w = finalize_long_only_weights(w, min_weight=min_weight, max_weight=max_weight, gross=gross)
        solutions.append(w)
    stacked = np.vstack(solutions) if solutions else np.zeros((1, len(mu)))
    return stacked.min(axis=0), stacked.max(axis=0)
