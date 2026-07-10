"""Shared Stage 11 helpers: lockbox enforcement, calibration-row admission, and the pure
cross-sectional estimators — one implementation consumed by 68 (fit) and 69 (validate) so the
two scripts can never drift apart on semantics."""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from portfolio_layer.core.config import cfg_get, resolve_path
from portfolio_layer.core.contracts import sha256_file


def parse_finite(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def manifest_file_errors(
    manifest: dict[str, Any],
    files: dict[str, Path],
    *,
    row_counts: dict[str, int] | None = None,
) -> list[str]:
    """Verify files still match the manifest's sealed sha256/row-count metadata."""
    errors: list[str] = []
    sealed_files = manifest.get("files") or {}
    for name, path in files.items():
        info = sealed_files.get(name)
        if not isinstance(info, dict):
            errors.append(f"{name}:manifest_entry_missing")
            continue
        expected_sha = str(info.get("sha256", "")).strip()
        if not expected_sha:
            errors.append(f"{name}:manifest_sha_missing")
        elif not path.exists():
            errors.append(f"{name}:file_missing")
        else:
            actual_sha = sha256_file(path)
            if actual_sha != expected_sha:
                errors.append(f"{name}:sha_mismatch manifest={expected_sha[:12]} actual={actual_sha[:12]}")
        if row_counts is not None and name in row_counts:
            raw_rows = info.get("rows")
            if raw_rows is None:
                errors.append(f"{name}:manifest_rows_missing")
                continue
            try:
                expected_rows = int(raw_rows)
            except (TypeError, ValueError):
                errors.append(f"{name}:manifest_rows_missing")
            else:
                if int(row_counts[name]) != expected_rows:
                    errors.append(f"{name}:rows_mismatch manifest={expected_rows} actual={row_counts[name]}")
    return errors


def load_lockbox(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Verify the config lockbox mirror against the canonical protocol document, or refuse to run.

    docs/LOCKBOX_PROTOCOL.md is canonical; the config `stage11_lockbox` block is its machine-readable
    mirror. Any divergence (missing doc, missing keys, inconsistent ordering, dates absent from the
    doc text) raises ValueError so Stage 11 scripts fail closed.
    """
    block = cfg_get(config, "stage11_lockbox", None)
    if not isinstance(block, dict):
        raise ValueError("config stage11_lockbox block missing; it must mirror docs/LOCKBOX_PROTOCOL.md")
    required = ("protocol_doc", "declared", "dev_window_start", "dev_window_end", "sealed_start", "lockbox_opened")
    missing = [key for key in required if key not in block]
    if missing:
        raise ValueError(f"config stage11_lockbox missing keys: {missing}")
    dev_start = date.fromisoformat(str(block["dev_window_start"]))
    dev_end = date.fromisoformat(str(block["dev_window_end"]))
    sealed_start = date.fromisoformat(str(block["sealed_start"]))
    if not dev_start <= dev_end < sealed_start:
        raise ValueError(
            f"stage11_lockbox dates inconsistent: need dev_window_start <= dev_window_end < sealed_start, "
            f"got {dev_start} / {dev_end} / {sealed_start}"
        )
    doc = resolve_path(str(block["protocol_doc"]), base_dir=config_path.parent)
    if not doc.exists():
        raise ValueError(f"lockbox protocol document missing: {doc}")
    text = doc.read_text(encoding="utf-8")
    divergent = [
        value for value in (str(block["dev_window_start"]), str(block["dev_window_end"]), str(block["sealed_start"]))
        if value not in text
    ]
    if divergent:
        raise ValueError(f"config stage11_lockbox dates not present in protocol doc (divergence): {divergent}")
    return {
        "dev_window_start": str(block["dev_window_start"]),
        "dev_window_end": str(block["dev_window_end"]),
        "sealed_start": str(block["sealed_start"]),
        "training_label_end_max": str(block.get("training_label_end_max", block["dev_window_end"])),
        "lockbox_opened": bool(block.get("lockbox_opened", False)),
        "protocol_path": doc,
        "protocol_sha256": sha256_file(doc),
    }


# ---------------------------------------------------------------------------
# calibration-row admission (shared by 68 fit and 69 validate — identical semantics)
# ---------------------------------------------------------------------------
def _truthy_flag(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "1.0", "true", "yes"}


VALID_FORWARD_STATUSES = frozenset({"ok", "ok_delisted_terminal"})


def forward_status_is_valid(value: Any) -> bool:
    """Whether a forward label represents a complete investable-horizon outcome."""
    return str(value).strip() in VALID_FORWARD_STATUSES


def admit_calibration_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Admit panel rows usable for promoted training, with full exclusion accounting.

    Requires: research eligibility (contract flag OR tech sidecar stage11 flag), the lockbox purge
    flag, survivorship completeness, and a standardized score. Nothing drops silently.
    """
    exclusions = {
        "not_research_eligible": 0, "not_usable_for_promoted_training": 0,
        "survivorship_incomplete": 0, "missing_score_z": 0,
    }
    admitted: list[dict[str, str]] = []
    for r in rows:
        eligible = str(r.get("calibration_research_eligible", "")).strip() == "1" or _truthy_flag(
            r.get("sidecar_stage11_eligible", "")
        )
        if not eligible:
            exclusions["not_research_eligible"] += 1
            continue
        if str(r.get("usable_for_promoted_training", "")).strip() != "1":
            exclusions["not_usable_for_promoted_training"] += 1
            continue
        if str(r.get("survivorship_complete", "")).strip() != "1":
            exclusions["survivorship_incomplete"] += 1
            continue
        if parse_finite(r.get("score_z_pipeline_date")) is None:
            exclusions["missing_score_z"] += 1
            continue
        admitted.append(r)
    return admitted, exclusions


# ---------------------------------------------------------------------------
# pure cross-sectional estimators (shared by 68 and 69; self-tested in both)
# ---------------------------------------------------------------------------
def pooled_slopes(z: np.ndarray, y: np.ndarray, *, shrinkage: float) -> tuple[float | None, float | None]:
    """(ols, ridge) pooled cross-sectional slopes of y on z; ridge shrinks toward 0."""
    n = len(z)
    if n < 2:
        return None, None
    szz = float(z @ z)
    szy = float(z @ y)
    if szz <= 0:
        return None, None
    return szy / szz, szy / (szz + shrinkage * n)


def per_date_slope(z: np.ndarray, y: np.ndarray) -> float | None:
    """Cross-sectional OLS slope for one date (needs spread in z)."""
    if len(z) < 2:
        return None
    zc = z - z.mean()
    denom = float(zc @ zc)
    if denom <= 0:
        return None
    return float(zc @ (y - y.mean())) / denom


def rank_ic_of(z: np.ndarray, y: np.ndarray) -> float | None:
    """Spearman rank correlation (average ranks for ties), no scipy dependency."""
    if len(z) < 3:
        return None

    def ranks(v: np.ndarray) -> np.ndarray:
        order = v.argsort(kind="mergesort")
        r = np.empty(len(v), dtype=float)
        r[order] = np.arange(len(v), dtype=float)
        for uniq in np.unique(v):
            mask = v == uniq
            if mask.sum() > 1:
                r[mask] = r[mask].mean()
        return r

    rz, ry = ranks(z), ranks(y)
    sz, sy = rz.std(), ry.std()
    if sz <= 0 or sy <= 0:
        return None
    return float(((rz - rz.mean()) * (ry - ry.mean())).mean() / (sz * sy))


def mean_t(values: list[float]) -> tuple[float | None, float | None, float | None]:
    """(mean, se, t) of a series; t needs >= 3 observations and positive spread."""
    if not values:
        return None, None, None
    arr = np.array(values, dtype=float)
    mean = float(arr.mean())
    if len(arr) < 3:
        return mean, None, None
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return mean, 0.0, None
    se = sd / math.sqrt(len(arr))
    return mean, se, mean / se


def mean_t_hac(values: list[float], *, max_lag: int) -> tuple[float | None, float | None, float | None]:
    """Mean and Newey-West HAC t-statistic for serially dependent observations.

    Forward-return labels overlap heavily at multi-month horizons. Treating each daily cross-section
    as independent materially understates uncertainty, so evidence gates use this estimator with a
    lag tied to the target horizon.
    """
    if not values:
        return None, None, None
    arr = np.asarray(values, dtype=float)
    if not np.isfinite(arr).all():
        raise ValueError("HAC t-stat inputs must be finite")
    mean = float(arr.mean())
    n = len(arr)
    if n < 3:
        return mean, None, None
    lag = min(max(0, int(max_lag)), n - 2)
    centered = arr - mean
    gamma0 = float(centered @ centered) / n
    long_run_var = gamma0
    for k in range(1, lag + 1):
        gamma = float(centered[k:] @ centered[:-k]) / n
        long_run_var += 2.0 * (1.0 - k / (lag + 1.0)) * gamma
    # Finite samples can produce a slightly negative estimate from noisy autocovariances. A zero
    # standard error is not evidence, so return no t-stat rather than an infinite one.
    if long_run_var <= 0.0:
        return mean, 0.0, None
    se = math.sqrt(long_run_var / n)
    return mean, se, mean / se if se > 0.0 else None


def independent_windows(dates: list[str], horizon_days: int) -> int:
    """Non-overlapping forward-label windows the snapshot span supports (trading-day horizon,
    converted to calendar days at 7/5). Overlapping windows share outcomes and must not be
    counted as independent evidence."""
    if not dates:
        return 0
    span = (date.fromisoformat(max(dates)) - date.fromisoformat(min(dates))).days
    return 1 + int(span / (horizon_days * 7.0 / 5.0))
