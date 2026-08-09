"""Shadow-only bridge from Technology diagnostics to shared factor validation.

This adapter deliberately has no production integration.  It consumes immutable
Stage 8A diagnostic exports, reconciles the shared per-date Spearman series to
the existing Technology implementation, and only then publishes governed
evidence packages.
"""

from __future__ import annotations

import base64
import binascii
import csv
import json
import math
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from factor_validation import (
    CampaignRegistry,
    FDRFamily,
    FactorObservation,
    FactorValidationConfig,
    ProvenanceFileSet,
    ValidationCellRegistration,
    abandon_incomplete_family,
    anchor_campaign_report,
    campaign_registry_path,
    canonical_json_bytes,
    evidence_package_path,
    load_campaign_registry,
    read_campaign_ledger,
    register_campaign,
    sha256_bytes,
    sha256_file,
    validate_factor,
    verify_campaign_ledger,
    verify_evidence_package,
    write_evidence_family,
)
from factor_validation.core import FactorValidationResult
from factor_validation.evidence import CONTENT_FILE_NAMES, build_evidence_files
from technology.core.config import load_yaml, resolve_path
from technology.core.scoring_features import SUBFEATURE_SPECS
from technology.core.signal_diagnostics import (
    maximum_forward_label_staleness_days,
    spearman as legacy_spearman,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_KEY = "technology_factor_validation_shadow"
TARGET_NAME = "beta_residual_forward_return"
SECTOR_ID = "software_infrastructure"
RECONCILIATION_FILE = "technology_shadow_reconciliation.json"
RECONCILIATION_SCHEMA = "technology_factor_validation_shadow_v3"
CODE_SNAPSHOT_FILE = "sealed_code_snapshot.json"
CODE_SNAPSHOT_SCHEMA = "technology_factor_validation_code_snapshot_v1"
ALLOWED_CONFIG_KEYS = frozenset(
    {
        "mode",
        "production_promotion_enabled",
        "portfolio_write_enabled",
        "model_family",
        "family_id",
        "signal_panel_path",
        "legacy_ic_path",
        "legacy_summary_path",
        "output_root",
        "factor_ids",
        "horizons_trading_days",
        "evaluation_step_trading_days",
        "entry_lag_trading_days",
        "min_cross_section",
        "min_dates",
        "min_independent_windows",
        "min_regime_dates",
        "methodology_amendment_id",
        "quantile_count",
        "min_extreme_bucket_size",
        "round_trip_cost",
        "round_trip_cost_source",
        "fdr_alpha",
        "cross_campaign_familywise_alpha",
        "cross_campaign_max_looks",
        "alpha_spending_method",
        "require_complete_legacy_family",
        "selection_design",
        "prospective_claim_authorized",
        "production_scoring_config_key",
        "holiday_dates",
    }
)


@dataclass(frozen=True)
class TechnologyShadowSettings:
    """Strict, production-disabled configuration for one Technology pilot."""

    config_path: Path
    model_family: str
    family_id: str
    signal_panel_path: Path
    legacy_ic_path: Path
    legacy_summary_path: Path
    output_root: Path
    factor_ids: tuple[str, ...]
    horizons_trading_days: tuple[int, ...]
    evaluation_step_trading_days: int
    entry_lag_trading_days: int = 0
    min_cross_section: int = 30
    min_dates: int = 12
    min_independent_windows: int = 3
    min_regime_dates: int = 3
    methodology_amendment_id: str = "stage3_initial_methodology"
    quantile_count: int = 5
    min_extreme_bucket_size: int = 2
    round_trip_cost: float = 0.003
    round_trip_cost_source: str = "conservative_shadow_stress_30bps"
    fdr_alpha: float = 0.05 / 12.0
    cross_campaign_familywise_alpha: float = 0.05
    cross_campaign_max_looks: int = 12
    alpha_spending_method: str = "bonferroni_equal"
    require_complete_legacy_family: bool = True
    selection_design: str = "retrospective_full_family"
    prospective_claim_authorized: bool = False
    production_scoring_config_key: str = "software_infrastructure_calibrated_scoring"
    holiday_dates: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "config_path",
            "signal_panel_path",
            "legacy_ic_path",
            "legacy_summary_path",
            "output_root",
        ):
            object.__setattr__(self, field_name, Path(getattr(self, field_name)).resolve())
        for field_name in ("model_family", "family_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value)
        methodology_amendment_id = str(self.methodology_amendment_id or "").strip()
        if not methodology_amendment_id:
            raise ValueError("methodology_amendment_id must not be blank")
        object.__setattr__(self, "methodology_amendment_id", methodology_amendment_id)
        _validate_isolated_output_root(self.output_root)
        factors = tuple(str(value or "").strip() for value in self.factor_ids)
        if not factors or any(not value for value in factors):
            raise ValueError("factor_ids must contain nonblank values")
        if len(set(factors)) != len(factors):
            raise ValueError("factor_ids must be unique")
        object.__setattr__(self, "factor_ids", factors)
        horizons = tuple(
            _strict_int(value, field_name="horizons_trading_days", minimum=1)
            for value in self.horizons_trading_days
        )
        if not horizons or len(set(horizons)) != len(horizons):
            raise ValueError("horizons_trading_days must be nonempty and unique")
        object.__setattr__(self, "horizons_trading_days", horizons)
        for field_name, minimum in (
            ("evaluation_step_trading_days", 1),
            ("entry_lag_trading_days", 0),
            ("min_cross_section", 3),
            ("min_dates", 3),
            ("min_independent_windows", 2),
            ("min_regime_dates", 1),
            ("quantile_count", 2),
            ("min_extreme_bucket_size", 1),
            ("cross_campaign_max_looks", 1),
        ):
            object.__setattr__(
                self,
                field_name,
                _strict_int(getattr(self, field_name), field_name=field_name, minimum=minimum),
            )
        for field_name in (
            "round_trip_cost",
            "fdr_alpha",
            "cross_campaign_familywise_alpha",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{field_name} must be a finite number")
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError(f"{field_name} must be finite")
            object.__setattr__(self, field_name, parsed)
        if self.entry_lag_trading_days != 0:
            raise ValueError("Technology Stage 8A forward returns require entry_lag_trading_days=0")
        if self.round_trip_cost <= 0:
            raise ValueError("round_trip_cost must be positive")
        if not 0 < self.fdr_alpha < 1:
            raise ValueError("fdr_alpha must be between zero and one")
        if not 0 < self.cross_campaign_familywise_alpha < 1:
            raise ValueError("cross_campaign_familywise_alpha must be between zero and one")
        expected_alpha = (
            self.cross_campaign_familywise_alpha / self.cross_campaign_max_looks
        )
        if not math.isclose(self.fdr_alpha, expected_alpha, rel_tol=1e-15, abs_tol=0.0):
            raise ValueError(
                "fdr_alpha must equal cross_campaign_familywise_alpha / "
                "cross_campaign_max_looks"
            )
        for field_name in (
            "round_trip_cost_source",
            "production_scoring_config_key",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value)
        if self.alpha_spending_method != "bonferroni_equal":
            raise ValueError("alpha_spending_method must be 'bonferroni_equal'")
        if self.selection_design != "retrospective_full_family":
            raise ValueError("selection_design must be 'retrospective_full_family'")
        if not isinstance(self.require_complete_legacy_family, bool):
            raise TypeError("require_complete_legacy_family must be a boolean")
        if self.require_complete_legacy_family is not True:
            raise ValueError("require_complete_legacy_family must remain true")
        if not isinstance(self.prospective_claim_authorized, bool):
            raise TypeError("prospective_claim_authorized must be a boolean")
        if self.prospective_claim_authorized is not False:
            raise ValueError("prospective_claim_authorized must remain false")
        holidays = tuple(sorted({_strict_date(value, field_name="holiday_dates") for value in self.holiday_dates}))
        object.__setattr__(self, "holiday_dates", holidays)


def _strict_int(value: Any, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _strict_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _validate_isolated_output_root(path: Path) -> None:
    resolved = path.resolve()
    forbidden = {"portfolio", "portfolio_layer", "portfolios"}
    if {part.casefold() for part in resolved.parts} & forbidden:
        raise ValueError("Technology shadow output_root must not be a portfolio path")
    if resolved == PROJECT_ROOT:
        raise ValueError("Technology shadow output_root must not be the project root")


def _strict_date(value: Any, *, field_name: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{field_name} must use canonical YYYY-MM-DD dates")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} contains invalid date {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} contains noncanonical date {value!r}")
    return parsed


def _require_list(value: Any, *, field_name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise TypeError(f"{field_name} must be a nonempty list")
    return value


def settings_from_config(
    config_path: str | Path,
    *,
    output_root: str | Path | None = None,
) -> TechnologyShadowSettings:
    """Load the strict shadow section and enforce both hard safety locks."""

    path = Path(config_path).expanduser().resolve()
    payload = load_yaml(path)
    section = payload.get(CONFIG_KEY)
    if not isinstance(section, dict):
        raise ValueError(f"{CONFIG_KEY} must be a mapping")
    unknown = set(section) - ALLOWED_CONFIG_KEYS
    missing = {
        "mode",
        "production_promotion_enabled",
        "portfolio_write_enabled",
        "model_family",
        "family_id",
        "signal_panel_path",
        "legacy_ic_path",
        "legacy_summary_path",
        "output_root",
        "factor_ids",
        "horizons_trading_days",
        "evaluation_step_trading_days",
        "round_trip_cost_source",
        "cross_campaign_familywise_alpha",
        "cross_campaign_max_looks",
        "alpha_spending_method",
        "require_complete_legacy_family",
        "selection_design",
        "prospective_claim_authorized",
        "production_scoring_config_key",
        "methodology_amendment_id",
    } - set(section)
    if unknown or missing:
        raise ValueError(
            f"{CONFIG_KEY} schema mismatch: missing={sorted(missing)}; extra={sorted(unknown)}"
        )
    if section["mode"] != "shadow":
        raise ValueError("Technology factor validation mode must remain 'shadow'")
    if _strict_bool(
        section["production_promotion_enabled"],
        field_name="production_promotion_enabled",
    ):
        raise ValueError("production_promotion_enabled must remain false in Stage 3")
    if _strict_bool(section["portfolio_write_enabled"], field_name="portfolio_write_enabled"):
        raise ValueError("portfolio_write_enabled must remain false in Stage 3")
    base = path.parent
    configured_output = (
        Path(output_root).expanduser().resolve()
        if output_root is not None
        else resolve_path(section["output_root"], base_dir=base)
    )
    holidays_raw = section.get("holiday_dates", [])
    if not isinstance(holidays_raw, list):
        raise TypeError("holiday_dates must be a list")
    return TechnologyShadowSettings(
        config_path=path,
        model_family=section["model_family"],
        family_id=section["family_id"],
        signal_panel_path=resolve_path(section["signal_panel_path"], base_dir=base),
        legacy_ic_path=resolve_path(section["legacy_ic_path"], base_dir=base),
        legacy_summary_path=resolve_path(section["legacy_summary_path"], base_dir=base),
        output_root=configured_output,
        factor_ids=tuple(_require_list(section["factor_ids"], field_name="factor_ids")),
        horizons_trading_days=tuple(
            _require_list(section["horizons_trading_days"], field_name="horizons_trading_days")
        ),
        evaluation_step_trading_days=section["evaluation_step_trading_days"],
        entry_lag_trading_days=section.get("entry_lag_trading_days", 0),
        min_cross_section=section.get("min_cross_section", 30),
        min_dates=section.get("min_dates", 12),
        min_independent_windows=section.get("min_independent_windows", 3),
        min_regime_dates=section.get("min_regime_dates", 3),
        methodology_amendment_id=section["methodology_amendment_id"],
        quantile_count=section.get("quantile_count", 5),
        min_extreme_bucket_size=section.get("min_extreme_bucket_size", 2),
        round_trip_cost=section.get("round_trip_cost", 0.003),
        round_trip_cost_source=section["round_trip_cost_source"],
        fdr_alpha=section.get("fdr_alpha", 0.05 / 12.0),
        cross_campaign_familywise_alpha=section["cross_campaign_familywise_alpha"],
        cross_campaign_max_looks=section["cross_campaign_max_looks"],
        alpha_spending_method=section["alpha_spending_method"],
        require_complete_legacy_family=_strict_bool(
            section["require_complete_legacy_family"],
            field_name="require_complete_legacy_family",
        ),
        selection_design=section["selection_design"],
        prospective_claim_authorized=_strict_bool(
            section["prospective_claim_authorized"],
            field_name="prospective_claim_authorized",
        ),
        production_scoring_config_key=section["production_scoring_config_key"],
        holiday_dates=tuple(holidays_raw),
    )


def _factor_specs() -> dict[str, tuple[bool, Any]]:
    return {
        raw_key: (higher_is_better, validity)
        for raw_key, _score_key, higher_is_better, validity in SUBFEATURE_SPECS
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"input must be a regular non-symlink file: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f"CSV has missing or duplicate headers: {path}")
        return [dict(row) for row in reader]


def _optional_finite(value: Any, *, field_name: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric or blank, got {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite or blank")
    return parsed


def _load_panel(
    settings: TechnologyShadowSettings,
) -> tuple[list[dict[str, Any]], date, tuple[str, ...]]:
    rows = _read_csv(settings.signal_panel_path)
    required = {"asof_date", "ticker", "market_regime", *settings.factor_ids}
    required.update(f"fwd_resid_{horizon}d" for horizon in settings.horizons_trading_days)
    if not rows:
        raise ValueError("signal panel must contain rows")
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"signal panel is missing columns: {sorted(missing)}")
    specs = _factor_specs()
    unknown = set(settings.factor_ids) - set(specs)
    if unknown:
        raise ValueError(f"factor_ids are absent from canonical Technology specs: {sorted(unknown)}")
    parsed_rows: list[dict[str, Any]] = []
    seen: set[tuple[date, str]] = set()
    dates: set[date] = set()
    for index, row in enumerate(rows, start=2):
        as_of = _strict_date(row.get("asof_date"), field_name=f"signal_panel row {index} asof_date")
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            raise ValueError(f"signal_panel row {index} has blank ticker")
        key = (as_of, ticker)
        if key in seen:
            raise ValueError(f"duplicate signal panel key: {as_of.isoformat()}:{ticker}")
        seen.add(key)
        dates.add(as_of)
        market_regime = str(row.get("market_regime") or "").strip()
        if market_regime not in {"risk_on", "risk_off"}:
            raise ValueError(
                f"signal_panel row {index} has invalid market_regime {market_regime!r}"
            )
        parsed: dict[str, Any] = {
            "asof_date": as_of,
            "ticker": ticker,
            "market_regime": market_regime,
        }
        for factor_id in settings.factor_ids:
            value = _optional_finite(row.get(factor_id), field_name=f"{factor_id} row {index}")
            validity = specs[factor_id][1]
            parsed[factor_id] = value if value is None or validity is None or validity(value) else None
        for horizon in settings.horizons_trading_days:
            column = f"fwd_resid_{horizon}d"
            parsed[column] = _optional_finite(row.get(column), field_name=f"{column} row {index}")
        parsed_rows.append(parsed)
    if len(dates) < settings.min_dates:
        raise ValueError(f"signal panel has only {len(dates)} dates; requires {settings.min_dates}")
    return parsed_rows, max(dates), tuple(sorted(item.isoformat() for item in dates))


def _load_legacy_contract(
    settings: TechnologyShadowSettings,
    *,
    panel_dates: tuple[str, ...],
) -> tuple[dict[tuple[str, int], dict[str, str]], dict[str, Any]]:
    rows = _read_csv(settings.legacy_ic_path)
    required = {"signal", "horizon_days", "n_dates", "mean_ic"}
    if not rows or required - set(rows[0]):
        raise ValueError("legacy IC file is empty or missing required columns")
    selected: dict[tuple[str, int], dict[str, str]] = {}
    complete_rows: dict[tuple[str, int], dict[str, str]] = {}
    for row in rows:
        signal = str(row.get("signal") or "").strip()
        try:
            horizon = int(str(row.get("horizon_days") or ""))
        except ValueError as exc:
            raise ValueError("legacy IC horizon_days must contain integers") from exc
        key = (signal, horizon)
        if horizon in settings.horizons_trading_days:
            if key in complete_rows:
                raise ValueError(f"duplicate legacy IC row: {key}")
            complete_rows[key] = row
        if signal in settings.factor_ids and horizon in settings.horizons_trading_days:
            selected[key] = row
    expected = {
        (factor_id, horizon)
        for factor_id in settings.factor_ids
        for horizon in settings.horizons_trading_days
    }
    if set(selected) != expected:
        raise ValueError(
            f"legacy IC membership mismatch: missing={sorted(expected - set(selected))}; "
            f"extra={sorted(set(selected) - expected)}"
        )
    legacy_signals = {
        signal
        for signal, _horizon in complete_rows
        if all(
            (signal, horizon) in complete_rows
            for horizon in settings.horizons_trading_days
        )
    }
    incomplete_signals = {
        signal for signal, _horizon in complete_rows
    } - legacy_signals
    if incomplete_signals:
        raise ValueError(
            "legacy IC signals are missing registered horizons: "
            f"{sorted(incomplete_signals)}"
        )
    if settings.require_complete_legacy_family and set(settings.factor_ids) != legacy_signals:
        raise ValueError(
            "retrospective full-family membership mismatch: "
            f"missing={sorted(legacy_signals - set(settings.factor_ids))}; "
            f"extra={sorted(set(settings.factor_ids) - legacy_signals)}"
        )
    if not settings.legacy_summary_path.is_file() or settings.legacy_summary_path.is_symlink():
        raise ValueError("legacy summary must be a regular non-symlink file")
    try:
        summary = json.loads(settings.legacy_summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("legacy summary is not valid JSON") from exc
    if not isinstance(summary, dict):
        raise ValueError("legacy summary must be a JSON object")
    if summary.get("model_family") != settings.model_family:
        raise ValueError("legacy summary model_family does not match shadow configuration")
    if summary.get("horizons_trading_days") != list(settings.horizons_trading_days):
        raise ValueError("legacy summary horizons do not match shadow configuration")
    if int(summary.get("panel_dates", -1)) != len(panel_dates):
        raise ValueError("legacy summary panel_dates does not match signal_panel.csv")
    if summary.get("forward_return_filter_mode") != "nonfinite_only":
        raise ValueError(
            "legacy diagnostics must be regenerated without outcome-conditioned filtering"
        )
    if summary.get("regime_method") != "trailing_126d_benchmark_return_sign":
        raise ValueError("legacy diagnostics must declare the canonical PIT regime method")
    summary_end = _strict_date(summary.get("end_date"), field_name="legacy summary end_date")
    panel_end = date.fromisoformat(panel_dates[-1])
    panel_staleness_days = (summary_end - panel_end).days
    maximum_staleness_days = maximum_forward_label_staleness_days(
        settings.horizons_trading_days,
        settings.evaluation_step_trading_days,
    )
    if panel_staleness_days < 0 or panel_staleness_days > maximum_staleness_days:
        raise ValueError(
            "legacy summary end_date is earlier than the panel or the panel is "
            f"more than {maximum_staleness_days} days stale: "
            f"summary={summary_end}, panel={panel_end}"
        )
    return selected, summary


def _validation_config(
    settings: TechnologyShadowSettings,
    *,
    horizon: int,
) -> FactorValidationConfig:
    return FactorValidationConfig(
        horizon_trading_days=horizon,
        entry_lag_trading_days=settings.entry_lag_trading_days,
        min_cross_section=settings.min_cross_section,
        min_dates=settings.min_dates,
        min_independent_windows=settings.min_independent_windows,
        min_regime_dates=settings.min_regime_dates,
        quantile_count=settings.quantile_count,
        min_extreme_bucket_size=settings.min_extreme_bucket_size,
        round_trip_cost=settings.round_trip_cost,
        primary_inference="independent_window",
        target_name=TARGET_NAME,
        holiday_dates=settings.holiday_dates,
        transition_cadence_trading_days=settings.evaluation_step_trading_days,
    )


def _cell_id(factor_id: str, horizon: int) -> str:
    factor_digest = sha256_bytes(factor_id.encode("utf-8"))[:10]
    return f"fv_{factor_digest}_{horizon}d"


def _build_cell_evidence(
    settings: TechnologyShadowSettings,
    *,
    panel_rows: list[dict[str, Any]],
    legacy_row: Mapping[str, str],
    factor_id: str,
    horizon: int,
) -> tuple[Any, FactorValidationConfig, dict[str, Any], tuple[FactorObservation, ...]]:
    specs = _factor_specs()
    higher_is_better = specs[factor_id][0]
    direction_multiplier = 1.0 if higher_is_better else -1.0
    return_column = f"fwd_resid_{horizon}d"
    observations = tuple(
        FactorObservation(
            as_of_date=row["asof_date"],
            entity_id=row["ticker"],
            factor_value=row[factor_id],
            forward_return=row[return_column],
            regime=row["market_regime"],
        )
        for row in panel_rows
    )
    config = _validation_config(settings, horizon=horizon)
    result = validate_factor(observations, factor_id=factor_id, config=config)

    by_date: dict[date, list[tuple[float, float]]] = defaultdict(list)
    for observation in observations:
        if observation.factor_value is None or observation.forward_return is None:
            continue
        by_date[observation.as_of_date].append(
            (
                direction_multiplier * float(observation.factor_value),
                float(observation.forward_return),
            )
        )
    legacy_per_date: dict[date, float] = {}
    for as_of, pairs in sorted(by_date.items()):
        if len(pairs) < settings.min_cross_section:
            continue
        value = legacy_spearman(
            [item[0] for item in pairs],
            [item[1] for item in pairs],
        )
        if value is not None:
            legacy_per_date[as_of] = value
    raw_shared_per_date = {
        item.as_of_date: float(item.spearman_ic)
        for item in result.per_date
        if item.spearman_ic is not None
    }
    shared_per_date = {
        as_of: direction_multiplier * value
        for as_of, value in raw_shared_per_date.items()
    }
    if set(shared_per_date) != set(legacy_per_date):
        raise ValueError(
            f"per-date IC date mismatch for {factor_id}/{horizon}d: "
            f"shared_only={sorted(set(shared_per_date) - set(legacy_per_date))[:5]}; "
            f"legacy_only={sorted(set(legacy_per_date) - set(shared_per_date))[:5]}"
        )
    differences = {
        as_of: abs(shared_per_date[as_of] - legacy_per_date[as_of])
        for as_of in shared_per_date
    }
    max_difference = max(differences.values(), default=0.0)
    if max_difference > 1e-12:
        raise ValueError(
            f"per-date Spearman reconciliation failed for {factor_id}/{horizon}d: "
            f"max_abs_difference={max_difference}"
        )
    try:
        legacy_n_dates = int(str(legacy_row["n_dates"]))
        legacy_mean_ic = float(str(legacy_row["mean_ic"]))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"legacy IC row is malformed for {factor_id}/{horizon}d") from exc
    if legacy_n_dates != result.ic_date_count:
        raise ValueError(
            f"legacy n_dates mismatch for {factor_id}/{horizon}d: "
            f"legacy={legacy_n_dates}, shared={result.ic_date_count}"
        )
    direction_adjusted_mean_ic = (
        None if result.mean_ic is None else direction_multiplier * result.mean_ic
    )
    if (
        direction_adjusted_mean_ic is None
        or legacy_mean_ic != round(direction_adjusted_mean_ic, 4)
    ):
        raise ValueError(
            f"legacy mean_ic mismatch for {factor_id}/{horizon}d: "
            f"legacy={legacy_mean_ic}, shared_rounded="
            f"{None if direction_adjusted_mean_ic is None else round(direction_adjusted_mean_ic, 4)}"
        )
    oriented_gross = (
        None
        if result.mean_gross_top_minus_bottom_matched is None
        else direction_multiplier * result.mean_gross_top_minus_bottom_matched
    )
    oriented_net = (
        None
        if oriented_gross is None or result.mean_two_leg_turnover is None
        else oriented_gross - settings.round_trip_cost * result.mean_two_leg_turnover
    )
    reconciliation = {
        "factor_direction": "higher_is_better" if higher_is_better else "lower_is_better",
        "factor_id": factor_id,
        "horizon_trading_days": horizon,
        "legacy_mean_ic": legacy_mean_ic,
        "legacy_n_dates": legacy_n_dates,
        "max_abs_per_date_ic_difference": max_difference,
        "shared_evidence_eligible": result.evidence_eligible,
        "shared_mean_ic": result.mean_ic,
        "shared_direction_adjusted_mean_ic": direction_adjusted_mean_ic,
        "shared_direction_adjusted_gross_spread": oriented_gross,
        "shared_direction_adjusted_net_spread": oriented_net,
        "shared_mean_two_leg_turnover": result.mean_two_leg_turnover,
        "shared_primary_p_value": result.primary_p_value,
    }
    return result, config, reconciliation, observations


def _provenance_files(settings: TechnologyShadowSettings) -> ProvenanceFileSet:
    return ProvenanceFileSet(
        config_path=settings.config_path,
        source_paths={
            "technology/signal_panel.csv": settings.signal_panel_path,
            "technology/subfeature_ic.csv": settings.legacy_ic_path,
            "technology/stage8a_summary.json": settings.legacy_summary_path,
        },
        code_paths={
            "factor_validation/acceptance.py": PROJECT_ROOT / "factor_validation" / "acceptance.py",
            "factor_validation/artifacts.py": PROJECT_ROOT / "factor_validation" / "artifacts.py",
            "factor_validation/core.py": PROJECT_ROOT / "factor_validation" / "core.py",
            "factor_validation/evidence.py": PROJECT_ROOT / "factor_validation" / "evidence.py",
            "factor_validation/fdr.py": PROJECT_ROOT / "factor_validation" / "fdr.py",
            "factor_validation/ledger.py": PROJECT_ROOT / "factor_validation" / "ledger.py",
            "factor_validation/registry.py": PROJECT_ROOT / "factor_validation" / "registry.py",
            "technology/adapters/factor_validation_shadow.py": Path(__file__).resolve(),
            "technology/core/scoring_features.py": (
                PROJECT_ROOT / "technology" / "core" / "scoring_features.py"
            ),
            "technology/core/signal_diagnostics.py": (
                PROJECT_ROOT / "technology" / "core" / "signal_diagnostics.py"
            ),
        },
    )


def technology_shadow_provenance_files(
    settings: TechnologyShadowSettings,
) -> ProvenanceFileSet:
    """Return the exact runtime files required for independent drift verification."""

    return _provenance_files(settings)


def _sealed_code_snapshot(
    provenance: ProvenanceFileSet,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture exact executable bytes so dirty-worktree pilots remain reproducible."""

    files: list[dict[str, Any]] = []
    for logical_path, raw_path in provenance.code_paths:
        path = raw_path.resolve()
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"sealed code must be a regular non-symlink file: {path}")
        content = path.read_bytes()
        files.append(
            {
                "content_base64": base64.b64encode(content).decode("ascii"),
                "logical_path": logical_path,
                "sha256": sha256_bytes(content),
                "size_bytes": len(content),
            }
        )
    payload = {
        "files": files,
        "schema_version": CODE_SNAPSHOT_SCHEMA,
    }
    return payload, {
        "file_count": len(files),
        "path": CODE_SNAPSHOT_FILE,
        "sha256": sha256_bytes(canonical_json_bytes(payload)),
    }


def _sealed_code_vcs_status(provenance: ProvenanceFileSet) -> dict[str, Any]:
    """Disclose whether Git HEAD alone can reproduce every sealed code file."""

    relative_paths: list[str] = []
    status: list[str] = []
    for logical_path, raw_path in provenance.code_paths:
        path = raw_path.resolve()
        if path.is_relative_to(PROJECT_ROOT):
            relative_paths.append(path.relative_to(PROJECT_ROOT).as_posix())
        else:
            status.append(f"external_to_repository:{logical_path}")
    try:
        head = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if relative_paths:
            observed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(PROJECT_ROOT),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--",
                    *sorted(relative_paths),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            status.extend(item.rstrip() for item in observed if item.strip())
    except (OSError, subprocess.CalledProcessError):
        head = None
        status.append("git_observation_unavailable")
    status = sorted(set(status))
    clean = head is not None and not status
    return {
        "head_commit": head,
        "reproducibility_status": (
            "git_head_reproduces_sealed_code"
            if clean
            else "exact_bytes_recoverable_from_anchored_snapshot"
        ),
        "sealed_code_worktree_clean": clean,
        "status": status,
    }


def _production_factor_weights(
    settings: TechnologyShadowSettings,
) -> dict[str, dict[str, Any]]:
    payload = load_yaml(settings.config_path)
    block = payload.get(settings.production_scoring_config_key)
    if not isinstance(block, dict):
        raise ValueError("production scoring configuration must be a mapping")
    component_weights = block.get("component_weights")
    subfeature_weights = block.get("subfeature_weights")
    if not isinstance(component_weights, dict) or not isinstance(subfeature_weights, dict):
        raise ValueError("production component_weights and subfeature_weights must be mappings")
    output: dict[str, dict[str, Any]] = {}
    for factor_id in settings.factor_ids:
        score_key = f"{factor_id}_score"
        allocations: list[dict[str, Any]] = []
        for component, raw_weights in sorted(subfeature_weights.items()):
            if not isinstance(raw_weights, dict) or score_key not in raw_weights:
                continue
            component_weight = _optional_finite(
                component_weights.get(component),
                field_name=f"production component weight {component}",
            )
            subfeature_weight = _optional_finite(
                raw_weights.get(score_key),
                field_name=f"production subfeature weight {component}.{score_key}",
            )
            if component_weight is None or subfeature_weight is None:
                raise ValueError("production weights must not be blank")
            if component_weight < 0 or subfeature_weight < 0:
                raise ValueError("production weights must be nonnegative")
            allocations.append(
                {
                    "component": str(component),
                    "component_weight": component_weight,
                    "effective_weight": component_weight * subfeature_weight,
                    "subfeature_weight": subfeature_weight,
                }
            )
        effective = sum(float(item["effective_weight"]) for item in allocations)
        output[factor_id] = {
            "production_active": effective > 0.0,
            "production_allocations": allocations,
            "production_effective_weight": effective,
            "production_scoring_config_key": settings.production_scoring_config_key,
        }
    return output


def _campaign_id(
    settings: TechnologyShadowSettings,
    *,
    max_panel_date: date,
    provenance_sha256: str,
) -> str:
    policy = {
        "entry_lag_trading_days": settings.entry_lag_trading_days,
        "evaluation_step_trading_days": settings.evaluation_step_trading_days,
        "factor_ids": list(settings.factor_ids),
        "alpha_spending_method": settings.alpha_spending_method,
        "cross_campaign_familywise_alpha": settings.cross_campaign_familywise_alpha,
        "cross_campaign_max_looks": settings.cross_campaign_max_looks,
        "fdr_alpha": settings.fdr_alpha,
        "family_id": settings.family_id,
        "horizons_trading_days": list(settings.horizons_trading_days),
        "min_cross_section": settings.min_cross_section,
        "min_dates": settings.min_dates,
        "min_extreme_bucket_size": settings.min_extreme_bucket_size,
        "min_independent_windows": settings.min_independent_windows,
        "min_regime_dates": settings.min_regime_dates,
        "methodology_amendment_id": settings.methodology_amendment_id,
        "model_family": settings.model_family,
        "provenance_sha256": provenance_sha256,
        "prospective_claim_authorized": settings.prospective_claim_authorized,
        "quantile_count": settings.quantile_count,
        "round_trip_cost": settings.round_trip_cost,
        "round_trip_cost_source": settings.round_trip_cost_source,
        "selection_design": settings.selection_design,
    }
    digest = sha256_bytes(canonical_json_bytes(policy))[:12]
    return f"tech_shadow_{digest}"


def _sequential_look_state(
    settings: TechnologyShadowSettings,
    *,
    registry: CampaignRegistry,
    max_panel_date: date,
    results: Mapping[str, FactorValidationResult],
) -> dict[str, Any]:
    entries = [
        entry
        for entry in read_campaign_ledger(settings.output_root)
        if entry.get("event_type") == "publication_succeeded"
        and entry.get("family_id") == settings.family_id
    ]
    abandoned_campaigns = {
        str(entry.get("campaign_id"))
        for entry in read_campaign_ledger(settings.output_root)
        if entry.get("event_type") == "family_abandoned"
        and entry.get("family_id") == settings.family_id
    }
    entries = [
        entry for entry in entries if str(entry.get("campaign_id")) not in abandoned_campaigns
    ]
    ordered_campaigns: list[str] = []
    for entry in entries:
        prior_campaign = str(entry.get("campaign_id") or "")
        if prior_campaign and prior_campaign not in ordered_campaigns:
            ordered_campaigns.append(prior_campaign)
    amendment_of_campaign_id: str | None = None
    methodology_amendment = False
    if registry.campaign_id in ordered_campaigns:
        report_path = settings.output_root / registry.campaign_id / RECONCILIATION_FILE
        if report_path.is_file() and not report_path.is_symlink():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                sequential = report["sequential_testing"]
                if not isinstance(sequential, dict):
                    raise TypeError("sequential_testing must be a mapping")
                look_index = _strict_int(
                    sequential.get("look_index"),
                    field_name="existing campaign look_index",
                    minimum=1,
                )
                prior_amendment = sequential.get("amends_campaign_id")
                if prior_amendment is not None and not isinstance(prior_amendment, str):
                    raise TypeError("amends_campaign_id must be a string or null")
                amendment_of_campaign_id = prior_amendment
                methodology_amendment = bool(
                    sequential.get("methodology_amendment", False)
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("existing sequential campaign report is invalid") from exc
        else:
            successful_cells = {
                str(entry.get("cell_id"))
                for entry in entries
                if entry.get("campaign_id") == registry.campaign_id
                and entry.get("event_type") == "publication_succeeded"
            }
            expected_cells = {cell.cell_id for cell in registry.cells}
            is_partial_tail = (
                ordered_campaigns[-1] == registry.campaign_id
                and bool(successful_cells)
                and successful_cells < expected_cells
                and len(ordered_campaigns) >= 2
            )
            prior_campaign_id = ordered_campaigns[-2] if is_partial_tail else ""
            prior_report_path = (
                settings.output_root / prior_campaign_id / RECONCILIATION_FILE
            )
            try:
                prior_report = json.loads(prior_report_path.read_text(encoding="utf-8"))
                prior_panel_date = _strict_date(
                    prior_report.get("max_panel_date"),
                    field_name="prior campaign max_panel_date",
                )
                prior_sequential = prior_report["sequential_testing"]
                if not isinstance(prior_sequential, dict):
                    raise TypeError("prior sequential_testing must be a mapping")
                prior_look_index = _strict_int(
                    prior_sequential.get("look_index"),
                    field_name="prior campaign look_index",
                    minimum=1,
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("partial campaign cannot establish its prior look") from exc
            if (
                max_panel_date != prior_panel_date
                or not _is_exact_code_only_amendment(
                    settings.output_root,
                    prior_campaign_id=prior_campaign_id,
                    registry=registry,
                    results=results,
                )
            ):
                raise ValueError("partial campaign is not an exact code-only amendment")
            look_index = prior_look_index
            amendment_of_campaign_id = prior_campaign_id
    else:
        look_index = 1
        if ordered_campaigns:
            prior_campaign_id = ordered_campaigns[-1]
            prior_report_path = (
                settings.output_root
                / prior_campaign_id
                / RECONCILIATION_FILE
            )
            try:
                prior_report = json.loads(prior_report_path.read_text(encoding="utf-8"))
                prior_panel_date = _strict_date(
                    prior_report.get("max_panel_date"),
                    field_name="prior campaign max_panel_date",
                )
                prior_sequential = prior_report["sequential_testing"]
                if not isinstance(prior_sequential, dict):
                    raise TypeError("prior sequential_testing must be a mapping")
                prior_look_index = _strict_int(
                    prior_sequential.get("look_index"),
                    field_name="prior campaign look_index",
                    minimum=1,
                )
            except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    "prior sequential campaign report is missing or invalid"
                ) from exc
            if max_panel_date > prior_panel_date:
                look_index = prior_look_index + 1
            elif max_panel_date == prior_panel_date and _is_exact_code_only_amendment(
                settings.output_root,
                prior_campaign_id=prior_campaign_id,
                registry=registry,
                results=results,
            ):
                look_index = prior_look_index
                amendment_of_campaign_id = prior_campaign_id
            elif (
                max_panel_date == prior_panel_date
                and settings.methodology_amendment_id
                != str(prior_report.get("methodology_amendment_id") or "")
            ):
                look_index = prior_look_index + 1
                methodology_amendment = True
                amendment_of_campaign_id = prior_campaign_id
            else:
                raise ValueError(
                    "a new sequential look requires a strictly newer panel date"
                )
    if look_index > settings.cross_campaign_max_looks:
        raise ValueError("cross-campaign alpha-spending look budget is exhausted")
    return {
        "alpha_spending_method": settings.alpha_spending_method,
        "familywise_alpha": settings.cross_campaign_familywise_alpha,
        "look_index": look_index,
        "maximum_looks": settings.cross_campaign_max_looks,
        "per_look_fdr_alpha": settings.fdr_alpha,
        "amends_campaign_id": amendment_of_campaign_id,
        "code_only_amendment": (
            amendment_of_campaign_id is not None and not methodology_amendment
        ),
        "methodology_amendment": methodology_amendment,
        "statistical_result_identity": (
            "byte_exact_prior_evidence"
            if amendment_of_campaign_id is not None and not methodology_amendment
            else None
        ),
    }


def _is_exact_code_only_amendment(
    output_root: Path,
    *,
    prior_campaign_id: str,
    registry: CampaignRegistry,
    results: Mapping[str, FactorValidationResult],
) -> bool:
    """Accept a same-panel replacement only when prior evidence regenerates byte-for-byte.

    Data files, configuration bytes, registration semantics, and the FDR family must
    be unchanged. At least one sealed code digest must differ. The newly computed
    complete result family is then serialized against the prior registration and
    required to match every prior evidence content file exactly.
    """

    try:
        prior_registry = load_campaign_registry(
            output_root / prior_campaign_id / "campaign_registry.json"
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if prior_registry.fdr_families != registry.fdr_families:
        return False
    if {cell.cell_id for cell in prior_registry.cells} != {
        cell.cell_id for cell in registry.cells
    }:
        return False
    code_changed = False
    semantic_fields = (
        "cell_id",
        "sector_id",
        "factor_id",
        "target_name",
        "horizon_trading_days",
        "entry_lag_trading_days",
        "factor_direction",
        "evaluation_step_trading_days",
        "fdr_family_id",
        "fdr_member_id",
        "validation_config",
    )
    for current in registry.cells:
        prior = prior_registry.cell(current.cell_id)
        if any(getattr(prior, name) != getattr(current, name) for name in semantic_fields):
            return False
        if prior.config_sha256 != current.config_sha256:
            return False
        if prior.source_files != current.source_files:
            return False
        if tuple(item.logical_path for item in prior.code_files) != tuple(
            item.logical_path for item in current.code_files
        ):
            return False
        code_changed = code_changed or prior.code_files != current.code_files
    if not code_changed or set(results) != {cell.cell_id for cell in registry.cells}:
        return False

    for prior_cell in prior_registry.cells:
        package_path = evidence_package_path(
            output_root,
            prior_registry,
            cell_id=prior_cell.cell_id,
        )
        try:
            prior_acceptance = json.loads(
                (package_path / "acceptance.json").read_text(encoding="utf-8")
            )
            supersedes = prior_acceptance.get("supersedes_manifest_sha256")
            if supersedes is not None and not isinstance(supersedes, str):
                return False
            regenerated = build_evidence_files(
                prior_registry,
                cell_id=prior_cell.cell_id,
                result=results[prior_cell.cell_id],
                family_results=results,
                supersedes_manifest_sha256=supersedes,
            ).by_name()
            if set(regenerated) != set(CONTENT_FILE_NAMES):
                return False
            if any(
                (package_path / name).read_bytes() != regenerated[name]
                for name in CONTENT_FILE_NAMES
            ):
                return False
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
    return True


def _abandon_irrecoverable_partial_amendment(
    settings: TechnologyShadowSettings,
    *,
    registry: CampaignRegistry,
    max_panel_date: date,
    results: Mapping[str, FactorValidationResult],
) -> str | None:
    """Close a prior partial code version only when a completed baseline proves identity."""

    entries = read_campaign_ledger(settings.output_root)
    abandoned = {
        str(entry.get("campaign_id"))
        for entry in entries
        if entry.get("event_type") == "family_abandoned"
        and entry.get("family_id") == settings.family_id
    }
    ordered: list[str] = []
    for entry in entries:
        if (
            entry.get("event_type") == "publication_succeeded"
            and entry.get("family_id") == settings.family_id
        ):
            campaign_id = str(entry.get("campaign_id") or "")
            if campaign_id and campaign_id not in ordered and campaign_id not in abandoned:
                ordered.append(campaign_id)
    if not ordered or ordered[-1] == registry.campaign_id:
        return None
    partial_campaign_id = ordered[-1]
    partial_report = settings.output_root / partial_campaign_id / RECONCILIATION_FILE
    if partial_report.exists():
        return None
    try:
        partial_registry = load_campaign_registry(
            settings.output_root / partial_campaign_id / "campaign_registry.json"
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    successful_members = {
        str(entry.get("fdr_member_id"))
        for entry in entries
        if entry.get("event_type") == "publication_succeeded"
        and entry.get("campaign_id") == partial_campaign_id
        and entry.get("family_id") == settings.family_id
    }
    expected_members = set(partial_registry.family(settings.family_id).member_ids)
    if not successful_members or not successful_members < expected_members:
        return None
    completed_campaign_id = next(
        (
            campaign_id
            for campaign_id in reversed(ordered[:-1])
            if (settings.output_root / campaign_id / RECONCILIATION_FILE).is_file()
        ),
        None,
    )
    if completed_campaign_id is None:
        return None
    try:
        completed_report = json.loads(
            (
                settings.output_root / completed_campaign_id / RECONCILIATION_FILE
            ).read_text(encoding="utf-8")
        )
        completed_panel_date = _strict_date(
            completed_report.get("max_panel_date"),
            field_name="completed campaign max_panel_date",
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if completed_panel_date != max_panel_date:
        return None
    if not _is_exact_code_only_amendment(
        settings.output_root,
        prior_campaign_id=completed_campaign_id,
        registry=partial_registry,
        results=results,
    ):
        return None
    if not _is_exact_code_only_amendment(
        settings.output_root,
        prior_campaign_id=completed_campaign_id,
        registry=registry,
        results=results,
    ):
        return None
    abandon_incomplete_family(
        settings.output_root,
        partial_registry,
        family_id=settings.family_id,
        reason_code="code_provenance_changed_after_interruption",
    )
    return partial_campaign_id


def _logical_cell_sha256(cell: ValidationCellRegistration) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "factor_id": cell.factor_id.casefold(),
                "horizon_trading_days": cell.horizon_trading_days,
                "sector_id": cell.sector_id.casefold(),
                "target_name": cell.target_name.casefold(),
            }
        )
    )


def _supersession_map(
    output_root: Path,
    registry: CampaignRegistry,
) -> dict[str, str | None]:
    entries = read_campaign_ledger(output_root)
    abandoned = {
        (str(entry.get("campaign_id")), str(entry.get("family_id")))
        for entry in entries
        if entry.get("event_type") == "family_abandoned"
    }
    non_abandoned_successes = [
        entry
        for entry in entries
        if entry.get("event_type") == "publication_succeeded"
        and (str(entry.get("campaign_id")), str(entry.get("family_id")))
        not in abandoned
    ]
    superseded = {
        str(entry["supersedes_manifest_sha256"])
        for entry in non_abandoned_successes
        if entry.get("supersedes_manifest_sha256") is not None
    }
    current_by_logical = {
        str(entry["logical_cell_sha256"]): entry
        for entry in non_abandoned_successes
        if entry.get("campaign_id") == registry.campaign_id
        and isinstance(entry.get("logical_cell_sha256"), str)
    }
    successes = [
        entry
        for entry in non_abandoned_successes
        if entry.get("manifest_sha256") not in superseded
        and entry.get("campaign_id") != registry.campaign_id
    ]
    by_logical: dict[str, dict[str, Any]] = {}
    for entry in successes:
        logical = entry.get("logical_cell_sha256")
        if isinstance(logical, str):
            by_logical[logical] = entry
    return {
        cell.cell_id: (
            (
                str(
                    current_by_logical[_logical_cell_sha256(cell)].get(
                        "supersedes_manifest_sha256"
                    )
                )
                if current_by_logical[_logical_cell_sha256(cell)].get(
                    "supersedes_manifest_sha256"
                )
                is not None
                else None
            )
            if _logical_cell_sha256(cell) in current_by_logical
            else (
                str(by_logical[_logical_cell_sha256(cell)]["manifest_sha256"])
                if _logical_cell_sha256(cell) in by_logical
                else None
            )
        )
        for cell in registry.cells
    }


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    content = canonical_json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != content:
            raise FileExistsError(f"immutable reconciliation artifact differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()


def _build_reconciliation_report(
    *,
    registry: CampaignRegistry,
    settings: TechnologyShadowSettings,
    max_panel_date: date,
    cells: list[dict[str, Any]],
    package_states: Mapping[str, str],
    sequential_look: Mapping[str, Any],
    sealed_code_snapshot: Mapping[str, Any],
    sealed_code_vcs: Mapping[str, Any],
) -> dict[str, Any]:
    enriched_cells = []
    production_contradictions = []
    for item in cells:
        cell = dict(item)
        cell_id = _cell_id(str(cell["factor_id"]), int(cell["horizon_trading_days"]))
        state = package_states[cell_id]
        cell["cell_id"] = cell_id
        cell["statistical_evidence_state"] = state
        contradiction = bool(cell.get("production_active")) and state == "rejected"
        cell["production_reconciliation_status"] = (
            "ACTIVE_PRODUCTION_FACTOR_REJECTED_BY_SHADOW"
            if contradiction
            else "NO_ACTIVE_REJECTION_CONTRADICTION"
        )
        if contradiction:
            production_contradictions.append(
                {
                    "cell_id": cell_id,
                    "factor_id": cell["factor_id"],
                    "horizon_trading_days": cell["horizon_trading_days"],
                    "production_effective_weight": cell["production_effective_weight"],
                }
            )
        enriched_cells.append(cell)
    return {
        "campaign_id": registry.campaign_id,
        "cells": sorted(
            enriched_cells,
            key=lambda item: (item["factor_id"], item["horizon_trading_days"]),
        ),
        "entry_lag_trading_days": settings.entry_lag_trading_days,
        "legacy_authoritative": True,
        "max_panel_date": max_panel_date.isoformat(),
        "methodology_amendment_id": settings.methodology_amendment_id,
        "mode": "shadow",
        "model_family": settings.model_family,
        "package_states": dict(sorted(package_states.items())),
        "portfolio_impact": False,
        "portfolio_write_enabled": False,
        "production_reconciliation": {
            "active_factor_rejection_count": len(production_contradictions),
            "active_factor_rejections": production_contradictions,
            "production_scoring_config_key": settings.production_scoring_config_key,
        },
        "production_promotion_enabled": False,
        "prospective_claim_authorized": settings.prospective_claim_authorized,
        "reconciliation_scope": (
            "algorithmic_spearman_consistency_and_legacy_aggregate_match;"
            "not_independent_source_data_validation"
        ),
        "registry_sha256": registry.registration_sha256,
        "round_trip_cost": settings.round_trip_cost,
        "round_trip_cost_source": settings.round_trip_cost_source,
        "schema_version": RECONCILIATION_SCHEMA,
        "sealed_code_snapshot": dict(sealed_code_snapshot),
        "sealed_code_vcs": dict(sealed_code_vcs),
        "sector_id": SECTOR_ID,
        "sector_promotion_authorized": False,
        "selection_design": settings.selection_design,
        "sequential_testing": dict(sequential_look),
        "shared_gate_active": False,
        "statistical_acceptance_only": True,
    }


def run_technology_factor_validation_shadow(
    settings: TechnologyShadowSettings,
) -> dict[str, Any]:
    """Reconcile first, then publish the prevalidated FDR family resumably."""

    provenance = _provenance_files(settings)
    initial_observation = provenance.observe()
    panel_rows, max_panel_date, panel_dates = _load_panel(settings)
    legacy_rows, legacy_summary = _load_legacy_contract(settings, panel_dates=panel_dates)
    production_weights = _production_factor_weights(settings)

    configs: dict[str, FactorValidationConfig] = {}
    observations_by_cell: dict[str, tuple[FactorObservation, ...]] = {}
    results_by_cell: dict[str, FactorValidationResult] = {}
    reconciliation_cells: list[dict[str, Any]] = []
    for factor_id in settings.factor_ids:
        for horizon in settings.horizons_trading_days:
            cell_id = _cell_id(factor_id, horizon)
            result, config, reconciliation, observations = _build_cell_evidence(
                settings,
                panel_rows=panel_rows,
                legacy_row=legacy_rows[(factor_id, horizon)],
                factor_id=factor_id,
                horizon=horizon,
            )
            configs[cell_id] = config
            observations_by_cell[cell_id] = observations
            results_by_cell[cell_id] = result
            reconciliation.update(production_weights[factor_id])
            reconciliation_cells.append(reconciliation)

    observed = provenance.observe()
    if observed.observed_sha256 != initial_observation.observed_sha256:
        raise RuntimeError("Technology inputs or code changed during shadow validation")
    campaign_id = _campaign_id(
        settings,
        max_panel_date=max_panel_date,
        provenance_sha256=observed.observed_sha256,
    )
    specs = _factor_specs()
    cells = tuple(
        ValidationCellRegistration(
            cell_id=_cell_id(factor_id, horizon),
            sector_id=SECTOR_ID,
            factor_id=factor_id,
            target_name=TARGET_NAME,
            horizon_trading_days=horizon,
            entry_lag_trading_days=settings.entry_lag_trading_days,
            factor_direction=(
                "higher_is_better" if specs[factor_id][0] else "lower_is_better"
            ),
            evaluation_step_trading_days=settings.evaluation_step_trading_days,
            fdr_family_id=settings.family_id,
            fdr_member_id=_cell_id(factor_id, horizon),
            config_sha256=observed.config_sha256,
            source_files=observed.source_files,
            code_files=observed.code_files,
            validation_config=configs[_cell_id(factor_id, horizon)],
        )
        for factor_id in settings.factor_ids
        for horizon in settings.horizons_trading_days
    )
    registry = CampaignRegistry(
        campaign_id=campaign_id,
        cells=cells,
        fdr_families=(
            FDRFamily(
                family_id=settings.family_id,
                member_ids=tuple(cell.cell_id for cell in cells),
                alpha=settings.fdr_alpha,
            ),
        ),
    )
    _abandon_irrecoverable_partial_amendment(
        settings,
        registry=registry,
        max_panel_date=max_panel_date,
        results=results_by_cell,
    )
    sequential_look = _sequential_look_state(
        settings,
        registry=registry,
        max_panel_date=max_panel_date,
        results=results_by_cell,
    )
    provenance_by_cell = {cell.cell_id: provenance for cell in cells}
    register_campaign(
        settings.output_root,
        registry,
        provenance_files=provenance_by_cell,
    )
    package_paths = {
        cell.cell_id: evidence_package_path(settings.output_root, registry, cell_id=cell.cell_id)
        for cell in cells
    }
    existing = {cell_id: path.exists() for cell_id, path in package_paths.items()}
    reused = all(existing.values())
    packages = write_evidence_family(
        settings.output_root,
        registry,
        family_id=settings.family_id,
        observations=observations_by_cell,
        configs=configs,
        provenance_files=provenance_by_cell,
        supersedes_manifest_sha256=_supersession_map(settings.output_root, registry),
    )
    package_by_cell = {package.path.name: package for package in packages}
    package_states = {
        cell.cell_id: package_by_cell[cell.cell_id].state for cell in cells
    }

    ledger_report = verify_campaign_ledger(settings.output_root)
    if not ledger_report.ok:
        raise RuntimeError(f"Technology shadow campaign ledger failed: {ledger_report.errors}")
    campaign_dir = (settings.output_root / registry.campaign_id).resolve()
    snapshot_path = campaign_dir / CODE_SNAPSHOT_FILE
    snapshot_payload, snapshot_metadata = _sealed_code_snapshot(provenance)
    snapshot_seals = {
        (item["logical_path"], item["sha256"], item["size_bytes"])
        for item in snapshot_payload["files"]
    }
    registered_code_seals = {
        (seal.logical_path, seal.sha256, seal.size_bytes)
        for seal in registry.cells[0].code_files
    }
    if snapshot_seals != registered_code_seals:
        raise RuntimeError("sealed code changed before campaign snapshot publication")
    _write_immutable_json(snapshot_path, snapshot_payload)
    report_path = campaign_dir / RECONCILIATION_FILE
    if report_path.is_file() and not report_path.is_symlink():
        existing_report = json.loads(report_path.read_text(encoding="utf-8"))
        sealed_code_vcs = existing_report.get("sealed_code_vcs")
        if not isinstance(sealed_code_vcs, dict):
            raise ValueError("existing reconciliation has invalid sealed_code_vcs")
    else:
        sealed_code_vcs = _sealed_code_vcs_status(provenance)
    report_payload = _build_reconciliation_report(
        registry=registry,
        settings=settings,
        max_panel_date=max_panel_date,
        cells=reconciliation_cells,
        package_states=package_states,
        sequential_look=sequential_look,
        sealed_code_snapshot=snapshot_metadata,
        sealed_code_vcs=sealed_code_vcs,
    )
    report_payload["forward_return_filter_mode"] = legacy_summary[
        "forward_return_filter_mode"
    ]
    report_payload["forward_return_outlier_observation_counts"] = legacy_summary.get(
        "forward_return_outlier_observation_counts",
        {},
    )
    _write_immutable_json(report_path, report_payload)
    anchor_campaign_report(
        settings.output_root,
        registry,
        family_id=settings.family_id,
        report_path=report_path,
    )
    ledger_report = verify_campaign_ledger(settings.output_root)
    if not ledger_report.ok:
        raise RuntimeError(
            f"Technology shadow campaign report anchor failed: {ledger_report.errors}"
        )
    return {
        **report_payload,
        "evidence_package_count": len(package_paths),
        "ledger_entry_count": ledger_report.entry_count,
        "output_root": str(settings.output_root),
        "reconciliation_path": str(report_path),
        "reused_existing_packages": reused,
    }


def validate_technology_factor_validation_shadow(
    output_root: str | Path,
    *,
    campaign_id: str,
    provenance_files: ProvenanceFileSet | None = None,
) -> dict[str, Any]:
    """Verify one pilot campaign, every package, and the root trust ledger."""

    root = Path(output_root).expanduser().resolve()
    registry_file = root / campaign_id / "campaign_registry.json"
    registry = load_campaign_registry(registry_file)
    if registry.campaign_id != campaign_id:
        raise ValueError("campaign registry ID does not match requested campaign")
    errors: list[str] = []
    states: dict[str, str | None] = {}
    for cell in registry.cells:
        report = verify_evidence_package(
            evidence_package_path(root, registry, cell_id=cell.cell_id),
            expected_registry=registry,
            expected_cell_id=cell.cell_id,
            ledger_root=root,
            provenance_files=provenance_files,
        )
        states[cell.cell_id] = report.state
        errors.extend(f"{cell.cell_id}:{error}" for error in report.errors)
    ledger_report = verify_campaign_ledger(root)
    errors.extend(f"ledger:{error}" for error in ledger_report.errors)
    reconciliation_path = root / campaign_id / RECONCILIATION_FILE
    try:
        reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"reconciliation_unreadable:{exc.__class__.__name__}")
        reconciliation = {}
    required_safety = {
        "legacy_authoritative": True,
        "mode": "shadow",
        "portfolio_impact": False,
        "portfolio_write_enabled": False,
        "production_promotion_enabled": False,
        "prospective_claim_authorized": False,
        "sector_promotion_authorized": False,
        "shared_gate_active": False,
        "statistical_acceptance_only": True,
    }
    for key, expected in required_safety.items():
        if reconciliation.get(key) != expected:
            errors.append(f"reconciliation_safety_lock_mismatch:{key}")
    if reconciliation.get("campaign_id") != campaign_id:
        errors.append("reconciliation_campaign_id_mismatch")
    if reconciliation.get("registry_sha256") != registry.registration_sha256:
        errors.append("reconciliation_registry_sha256_mismatch")
    if reconciliation.get("schema_version") != RECONCILIATION_SCHEMA:
        errors.append("reconciliation_schema_version_mismatch")
    if reconciliation.get("sector_id") != SECTOR_ID:
        errors.append("reconciliation_sector_id_mismatch")
    if any(cell.sector_id != SECTOR_ID for cell in registry.cells):
        errors.append("registry_sector_id_mismatch")
    if reconciliation.get("selection_design") != "retrospective_full_family":
        errors.append("reconciliation_selection_design_mismatch")
    if reconciliation.get("entry_lag_trading_days") != 0:
        errors.append("reconciliation_entry_lag_mismatch")
    methodology_amendment_id = reconciliation.get("methodology_amendment_id")
    if not isinstance(methodology_amendment_id, str) or not methodology_amendment_id.strip():
        errors.append("reconciliation_methodology_amendment_id_invalid")
    vcs = reconciliation.get("sealed_code_vcs")
    expected_vcs_keys = {
        "head_commit",
        "reproducibility_status",
        "sealed_code_worktree_clean",
        "status",
    }
    if not isinstance(vcs, dict) or set(vcs) != expected_vcs_keys:
        errors.append("reconciliation_sealed_code_vcs_invalid")
    else:
        clean = vcs.get("sealed_code_worktree_clean")
        status = vcs.get("status")
        head = vcs.get("head_commit")
        reproducibility = vcs.get("reproducibility_status")
        if (
            type(clean) is not bool
            or not isinstance(status, list)
            or any(not isinstance(item, str) or not item for item in status)
            or (
                head is not None
                and (
                    not isinstance(head, str)
                    or len(head) not in {40, 64}
                    or any(character not in "0123456789abcdef" for character in head)
                )
            )
            or clean is not (head is not None and not status)
            or reproducibility
            not in {
                "git_head_reproduces_sealed_code",
                "exact_bytes_recoverable_from_anchored_snapshot",
            }
            or (clean and reproducibility != "git_head_reproduces_sealed_code")
            or (not clean and reproducibility != "exact_bytes_recoverable_from_anchored_snapshot")
        ):
            errors.append("reconciliation_sealed_code_vcs_invalid")
    snapshot = reconciliation.get("sealed_code_snapshot")
    expected_snapshot_keys = {"file_count", "path", "sha256"}
    if not isinstance(snapshot, dict) or set(snapshot) != expected_snapshot_keys:
        errors.append("reconciliation_code_snapshot_invalid")
    else:
        snapshot_relative = snapshot.get("path")
        snapshot_sha = snapshot.get("sha256")
        snapshot_count = snapshot.get("file_count")
        snapshot_path = root / campaign_id / str(snapshot_relative)
        try:
            snapshot_value = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append("reconciliation_code_snapshot_unreadable")
            snapshot_value = None
        snapshot_is_regular = snapshot_path.is_file() and not snapshot_path.is_symlink()
        observed_snapshot_sha = sha256_file(snapshot_path) if snapshot_is_regular else None
        if (
            snapshot_relative != CODE_SNAPSHOT_FILE
            or not isinstance(snapshot_sha, str)
            or snapshot_sha != observed_snapshot_sha
        ):
            errors.append("reconciliation_code_snapshot_hash_mismatch")
        if not isinstance(snapshot_value, dict) or set(snapshot_value) != {
            "files",
            "schema_version",
        } or snapshot_value.get("schema_version") != CODE_SNAPSHOT_SCHEMA:
            errors.append("reconciliation_code_snapshot_schema_mismatch")
        else:
            snapshot_files = snapshot_value.get("files")
            decoded_seals: set[tuple[str, str, int]] = set()
            if not isinstance(snapshot_files, list):
                errors.append("reconciliation_code_snapshot_files_invalid")
            else:
                for item in snapshot_files:
                    if not isinstance(item, dict) or set(item) != {
                        "content_base64",
                        "logical_path",
                        "sha256",
                        "size_bytes",
                    }:
                        errors.append("reconciliation_code_snapshot_files_invalid")
                        continue
                    try:
                        content = base64.b64decode(item["content_base64"], validate=True)
                    except (TypeError, ValueError, binascii.Error):
                        errors.append("reconciliation_code_snapshot_content_invalid")
                        continue
                    logical = item.get("logical_path")
                    digest = item.get("sha256")
                    size = item.get("size_bytes")
                    if (
                        not isinstance(logical, str)
                        or not logical
                        or not isinstance(digest, str)
                        or digest != sha256_bytes(content)
                        or type(size) is not int
                        or size != len(content)
                    ):
                        errors.append("reconciliation_code_snapshot_content_invalid")
                        continue
                    decoded_seals.add((logical, digest, size))
                registered_seals = {
                    (seal.logical_path, seal.sha256, seal.size_bytes)
                    for cell in registry.cells
                    for seal in cell.code_files
                }
                if decoded_seals != registered_seals:
                    errors.append("reconciliation_code_snapshot_registry_mismatch")
                if type(snapshot_count) is not int or snapshot_count != len(snapshot_files):
                    errors.append("reconciliation_code_snapshot_count_mismatch")
    round_trip_cost = reconciliation.get("round_trip_cost")
    if (
        isinstance(round_trip_cost, bool)
        or not isinstance(round_trip_cost, (int, float))
        or not math.isfinite(round_trip_cost)
        or round_trip_cost <= 0
    ):
        errors.append("reconciliation_round_trip_cost_invalid")
    cell_rows = reconciliation.get("cells")
    if not isinstance(cell_rows, list):
        errors.append("reconciliation_cells_invalid")
    else:
        by_id = {
            str(item.get("cell_id")): item
            for item in cell_rows
            if isinstance(item, dict)
        }
        expected_ids = {cell.cell_id for cell in registry.cells}
        if set(by_id) != expected_ids:
            errors.append("reconciliation_cell_membership_mismatch")
        else:
            for cell_id, state in states.items():
                if by_id[cell_id].get("statistical_evidence_state") != state:
                    errors.append(f"reconciliation_cell_state_mismatch:{cell_id}")
    return {
        "campaign_id": campaign_id,
        "errors": errors,
        "ledger_entry_count": ledger_report.entry_count,
        "ok": not errors,
        "package_count": len(registry.cells),
        "package_states": dict(sorted(states.items())),
        "reconciliation_path": str(reconciliation_path),
        "registry_path": str(campaign_registry_path(root, registry)),
    }
