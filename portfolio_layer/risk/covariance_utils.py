"""Pure covariance-stabilization utilities.

Adapted from the PROD `tier1_portfolio_optimizer` risk code — **pure functions only** — so Stage 2 and
the Stage 3 optimizer share one risk-math implementation without Stage 2 depending on the full optimizer
or `tier1_common`. Self-contained: numpy + math + logging. No PROD paths, no I/O, no config coupling.

Run `python covariance_utils.py` to execute the self-test.
"""
from __future__ import annotations

import logging
import math

import numpy as np


LOGGER = logging.getLogger("risk.covariance_utils")


def symmetrize(a: np.ndarray) -> np.ndarray:
    return 0.5 * (a + a.T)


def nearest_psd_cov(cov: np.ndarray, eig_floor: float = 1e-8) -> np.ndarray:
    """Practical PSD fix: symmetrize, eigen-decompose, clip eigenvalues to ``eig_floor``, reconstruct.

    Not full Higham alternating projections, but robust and fast (matches tier1).
    """
    cov = symmetrize(cov)
    vals, vecs = np.linalg.eigh(cov)
    vals = np.maximum(vals, eig_floor)
    cov_psd = (vecs * vals) @ vecs.T
    return symmetrize(cov_psd)


def safe_cond(a: np.ndarray) -> float:
    try:
        return float(np.linalg.cond(a))
    except Exception:  # noqa: BLE001
        return float("inf")


def is_well_conditioned(cov: np.ndarray, max_cond: float) -> bool:
    if not np.isfinite(cov).all():
        return False
    cond = safe_cond(cov)
    return bool(math.isfinite(cond) and cond <= float(max_cond))


def kendall_to_pearson(tau: np.ndarray) -> np.ndarray:
    """rho = sin(pi/2 * tau)."""
    return np.sin(0.5 * math.pi * tau)


def stabilize_covariance(cov: np.ndarray, *, eig_floor: float, max_cond: float, name: str = "cov") -> np.ndarray:
    """Symmetrize, zero-fill non-finite, PSD-fix, and jitter if still ill-conditioned (tier1 logic)."""
    cov = symmetrize(cov)
    if not np.isfinite(cov).all():
        LOGGER.warning("%s covariance has non-finite values; zero-filling and PSD-fixing.", name)
        cov = np.nan_to_num(cov, nan=0.0, posinf=0.0, neginf=0.0)
    cov = nearest_psd_cov(cov, eig_floor)
    cond = safe_cond(cov)
    if not math.isfinite(cond) or cond > float(max_cond):
        avg_var = float(np.mean(np.diag(cov)))
        jitter = max(float(eig_floor), avg_var * 1e-4)
        cov = nearest_psd_cov(cov + jitter * np.eye(cov.shape[0]), eig_floor)
        cond2 = safe_cond(cov)
        level = LOGGER.warning if (not math.isfinite(cond2) or cond2 > float(max_cond)) else LOGGER.info
        level("%s covariance jittered (cond=%.2e -> %.2e).", name, cond, cond2)
    return cov


def _selftest() -> None:
    floor = 1e-10
    # 1. nearest_psd_cov makes an indefinite matrix PSD.
    indefinite = np.array([[1.0, 2.0], [2.0, 1.0]])  # eigenvalues +3, -1
    psd = nearest_psd_cov(indefinite, floor)
    assert float(np.linalg.eigvalsh(psd).min()) >= floor - 1e-15, "PSD floor not enforced"
    assert np.allclose(psd, psd.T), "result not symmetric"
    # 2. a valid PSD matrix is (near) unchanged.
    good = np.array([[1.0, 0.5], [0.5, 1.0]])
    assert np.allclose(nearest_psd_cov(good, floor), good, atol=1e-9), "PSD matrix altered"
    # 3. stabilize handles non-finite + ill-conditioning.
    bad = np.array([[1.0, np.nan], [np.nan, 1e-14]])
    stab = stabilize_covariance(bad, eig_floor=floor, max_cond=1e6, name="selftest")
    assert np.isfinite(stab).all() and safe_cond(stab) <= 1e6, "stabilize failed to condition"
    # 4. kendall_to_pearson endpoints.
    kp = kendall_to_pearson(np.array([0.0, 1.0, -1.0]))
    assert abs(kp[0]) < 1e-12 and abs(kp[1] - 1.0) < 1e-12 and abs(kp[2] + 1.0) < 1e-12, "kendall map wrong"
    print("covariance_utils self-test: PASS")


if __name__ == "__main__":
    _selftest()