#!/usr/bin/env python3
"""Master cross-sector orchestrator for the scalper staging system.

Runs every sector registered in orchestration/registry.yaml (currently eight required
production sectors plus one optional sector) and the Tier-1 portfolio layer from a
single command, reading the registry
for every sector's exact CLI, publish glob, health manifest, and backfill/repair
entry points.

Scheduling model
----------------
* Tier 0 (sectors) runs as one lane per db_group (ThreadPoolExecutor). Sectors
  that share a SQLite file (technology.sqlite -> semis/software/hardware;
  industrials.sqlite -> defense then machinery) are serialized within their lane
  so two refreshes never mutate the same DB at once.
* A global network semaphore (max_concurrent_network_lanes, default 2) caps how
  many network-heavy subprocesses run simultaneously to avoid SEC/Yahoo rate
  collisions across independent lanes.
* Tier 1 (portfolio_layer, script 18) runs only after the Tier-0 gate passes
  (every `required` sector healthy). Excluding a required sector keeps it in the
  gate as UNKNOWN/unhealthy; running the portfolio with a required sector
  excluded needs an explicit --ignore-gate.

Trading calendar
----------------
There is no calendar table in this repo; the authoritative calendar is
portfolio_layer's SPY-from-Yahoo master_calendar, which needs the network. For
offline planning (default target date, catch-up gap math, health windows) we
derive trading dates from valid-session dated publish directories across the
reference sectors, rejecting any weekend/holiday folders, and combine them
with a weekday filter minus a standard NYSE holiday calendar
(observed shifts included; e.g. 2026-07-03 is the observed Independence Day and
therefore NOT tradeable). The default target date is the latest COMPLETED
trading session (weekday + market-close aware, mirroring biotech 24's guard),
never a value derived from published artifacts.

Modes: daily (default) | catch-up | backfill | repair | health-check.
Validate with --selftest (no subprocess) and --dry-run (prints the command matrix).

Catch-up policy (2026-08-29 correction)
-----------------------------------------
An unavailable HISTORICAL date must never prevent producing the CURRENT
portfolio. Catch-up therefore:
  * backfills older missing dates FIRST, oldest-first and best-effort, then
    rebuilds/verifies the CURRENT target last. Stateful sector pipelines cannot
    reliably publish an older partition after their root state has advanced;
  * accepts the sector on the current date alone: failed backfill dates are
    recorded per-date in the manifest (status PASS_WITH_BACKFILL_GAPS) and
    never fail the master; only a current-date failure fails the sector;
  * bounds the auto-run backfill window by the sector's backfill_window_days
    (default: staleness_tolerance_days). Older in-window gaps are surfaced in
    the manifest as historical_gaps, never auto-run;
  * respects an optional per-sector publish_epoch: dates before the epoch are
    exempt from missing-detection, so repointing a publish_glob can never
    retroactively manufacture missing history;
  * honours permanent-gap markers (orchestration/backfill_gap_markers.json):
    a backfill date that keeps failing is auto-marked permanent after
    defaults.permanent_gap_after_failures consecutive failures (operators can
    mark/clear via --mark-gap/--clear-gap) and is skipped by future scans
    while remaining visible in the manifest.

Freshness sentinel
------------------
Optional per-sector `freshness_probes` in the registry are evaluated once per
executed master run (read-only, sqlite opened with mode=ro URIs, hard 10s
timeout per probe; a probe error yields status ERROR, never a crash). Results
land in master_manifest.json per sector as
freshness: [{probe, latest, age_days, threshold, status}] with status
CURRENT | WARN_APPROACHING | STALE | ERROR, so upstream data staleness is
visible days before any fail-closed sector gate trips. Probes are surveillance,
not gates: they never block publication. The single exception is a probe with
required: true that is STALE or ERROR -- it marks the sector with a
FRESHNESS_BLOCKING note and forces the master acceptance to FAIL (other
sectors still run; a required probe that cannot even be evaluated must not be
silently non-blocking). Blocking applies only to sectors in the run's
selection, so a required-stale probe on an unselected sector cannot flip a
partial run. A registry with no probes behaves exactly as before.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import yaml
    from yaml.nodes import MappingNode, Node, SequenceNode
except ImportError as exc:  # pragma: no cover - yaml is a hard dependency here
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

ORCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ORCH_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestration_contracts.financial_lineage import (  # noqa: E402
    DEFAULT_MIN_CORE_METRIC_COUNT,
    POLICY_DISABLED,
    POLICY_STRICT_UNIVERSE,
    evaluate_financial_lineage_rows,
    financial_lineage_sidecar_alignment_errors,
    policy_for_model_family,
)

DEFAULT_REGISTRY = ORCH_DIR / "registry.yaml"
RUNS_ROOT = ORCH_DIR / "runs"  # live master manifests (resume source)
DRYRUN_RUNS_ROOT = ORCH_DIR / "runs_dryrun"  # dry-run manifests (NEVER consulted for resume)
ORCH_LOCK_PATH = ORCH_DIR / ".orchestrator.lock"
PY = sys.executable

# Market-close guard: the latest completed trading session is the previous
# trading day before ~17:00 ET (mirrors biotech 24 + portfolio same_day bar seal).
MARKET_TZ = "America/New_York"
MARKET_CLOSE_ET = dt_time(17, 0)
DEFAULT_LOCK_STALE_SEC = 6 * 3600  # override a lock older than this (crash recovery)

# States a required sector must be in for the gate / overall acceptance to pass.
# PASS_WITH_BACKFILL_GAPS: the CURRENT target date published and verified but one
# or more best-effort historical backfill dates failed -- healthy by policy (an
# unavailable historical date must never prevent the current portfolio).
HEALTHY_STATES = frozenset({"PASS", "DRY_RUN", "SKIPPED_RESUME", "UP_TO_DATE", "PASS_WITH_BACKFILL_GAPS"})

# Persistent permanent-gap marker store (catch-up backfill tombstones).
GAP_MARKER_PATH = ORCH_DIR / "backfill_gap_markers.json"
_GAP_MARKER_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Registry model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RepairSpec:
    date_flag: str
    selection_flag: str
    steps: list[str]
    rebuild_steps: list[str]
    extra_args: list[str]


@dataclass(frozen=True)
class BackfillSpec:
    script: str
    args_template: list[str]
    per_date: bool
    covers_target: bool = False
    note: str = ""


@dataclass(frozen=True)
class HealthSpec:
    manifest: str | None
    status_keys: list[str]
    # Manifest values (case-insensitive) that count as healthy. Default is the
    # strict {"PASS"}; portfolio_layer also accepts PASS_WITH_ADVISORY_WARNINGS
    # (18's whole-run verdict for advisory-only soft failures).
    healthy_values: list[str] = field(default_factory=lambda: ["PASS"])


@dataclass(frozen=True)
class FreshnessProbe:
    """One upstream data-freshness sentinel (surveillance-only unless required).

    kind:
      * sqlite_max_date  -- target {db, sql}: read-only (mode=ro URI) SQL returning
                            one date; age vs tolerance_days.
      * manifest_date    -- target {path, key}: JSON file + (dot-separated) key
                            holding a date; age vs tolerance_days.
      * deadline_schedule-- target {db, sql, cadence, deadline_days?, publication_lag_days?}:
                            for quarterly / bi-monthly datasets. Computes the newest
                            schedule period whose data must already be available at the
                            source (period_end + deadline_days + publication_lag_days
                            + tolerance_days grace <= target date) and compares it with
                            the sqlite max-date value, so it never demands data before
                            the source can publish it.
    """

    name: str
    kind: str
    target: dict[str, Any]
    tolerance_days: int
    warn_lead_days: int
    required: bool
    notes: str


@dataclass(frozen=True)
class Sector:
    name: str
    db_group: str
    dependency_tier: int
    required: bool
    network: bool
    entry_script: str
    date_flag: str
    args_template: list[str]
    force_args: list[str]
    publish_glob: str
    publish_date_format: str
    oos_column: str | None
    gate_column: str | None
    require_oos_valid: bool
    staleness_tolerance_days: int
    # Catch-up policy knobs (2026-08-07 redesign):
    #   backfill_window_days: calendar-day window catch-up may AUTO-RUN missing
    #     historical dates in (None -> staleness_tolerance_days). Older in-window
    #     gaps are reported, never auto-run.
    #   publish_epoch: earliest ISO date the publish_glob convention is valid for;
    #     dates before it are exempt from missing-detection (a repointed glob must
    #     not retroactively manufacture missing history).
    #   native_backfill_min_dates: optional minimum gap size that must use the
    #     sector's PIT-native backfill driver. This is 1 for pipelines whose live
    #     provider watermarks cannot be replayed safely for historical sessions.
    backfill_window_days: int | None
    native_backfill_min_dates: int | None
    publish_epoch: str | None
    # Some native PIT backfills publish a date-sealed rank artifact but do not
    # emit the live daily acceptance manifest. Historical completeness may use
    # that artifact contract only when this is explicitly false; current-date
    # health always remains strict.
    historical_health_manifest_required: bool
    health: HealthSpec
    backfill: BackfillSpec | None
    repair: RepairSpec | None
    weekly_pre_steps: list[dict[str, Any]]
    daily_post_steps: list[dict[str, Any]]
    timeout_sec: int
    retries: int
    retry_args: list[str] = field(default_factory=list)
    freshness_probes: list[FreshnessProbe] = field(default_factory=list)
    financial_lineage_required: bool = False
    financial_lineage_policy: str = POLICY_DISABLED
    financial_lineage_min_core_metric_count: int = DEFAULT_MIN_CORE_METRIC_COUNT
    financial_lineage_artifact: str | None = None


@dataclass(frozen=True)
class Registry:
    sectors: list[Sector]
    group_order: dict[str, list[str]]
    max_concurrent_network_lanes: int
    catch_up_gap_backfill_threshold: int
    catch_up_window_days: int
    repair_days: int
    calendar_reference_sectors: list[str]
    # Consecutive nightly failures after which a backfill date is auto-marked a
    # permanent gap (tombstoned) so it is not retried every night.
    permanent_gap_after_failures: int = 3
    # Ad-hoc full-day market closures (mourning/disaster days) the rule-based
    # NYSE calendar cannot derive. ISO dates.
    market_closures: list[str] = field(default_factory=list)

    def by_name(self, name: str) -> Sector:
        for sector in self.sectors:
            if sector.name == name:
                return sector
        raise KeyError(name)

    @property
    def names(self) -> list[str]:
        return [sector.name for sector in self.sectors]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _assert_unique_yaml_keys(node: Node, *, location: str = "root") -> None:
    """Reject duplicate mapping keys before PyYAML can silently overwrite them."""
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", ""))
            if key in seen:
                line = int(key_node.start_mark.line) + 1
                raise ValueError(
                    f"registry duplicate key {key!r} at {location} (line {line})"
                )
            seen.add(key)
            _assert_unique_yaml_keys(value_node, location=f"{location}.{key}")
    elif isinstance(node, SequenceNode):
        for index, child in enumerate(node.value):
            _assert_unique_yaml_keys(child, location=f"{location}[{index}]")


_PROBE_KINDS = frozenset({"sqlite_max_date", "manifest_date", "deadline_schedule"})
_PROBE_CADENCES = frozenset({"quarterly", "semi_monthly"})


def _parse_freshness_probes(sector_name: str, raw_probes: Any) -> list[FreshnessProbe]:
    probes: list[FreshnessProbe] = []
    for raw in _as_list(raw_probes):
        if not isinstance(raw, dict):
            raise ValueError(f"sector {sector_name}: freshness_probes entries must be mappings")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"sector {sector_name}: freshness probe missing name")
        kind = str(raw.get("kind") or "").strip()
        if kind not in _PROBE_KINDS:
            raise ValueError(f"sector {sector_name}: probe {name}: unknown kind {kind!r}; valid={sorted(_PROBE_KINDS)}")
        target = raw.get("target")
        if not isinstance(target, dict):
            raise ValueError(f"sector {sector_name}: probe {name}: target must be a mapping")
        required_keys = ("db", "sql") if kind in {"sqlite_max_date", "deadline_schedule"} else ("path", "key")
        for key in required_keys:
            if not str(target.get(key) or "").strip():
                raise ValueError(f"sector {sector_name}: probe {name}: target.{key} is required for kind {kind}")
        if kind == "deadline_schedule":
            cadence = str(target.get("cadence") or "").strip()
            if cadence not in _PROBE_CADENCES:
                raise ValueError(
                    f"sector {sector_name}: probe {name}: target.cadence must be one of {sorted(_PROBE_CADENCES)}"
                )
            for lag_key in ("deadline_days", "publication_lag_days"):
                if int(target.get(lag_key, 0)) < 0:
                    raise ValueError(f"sector {sector_name}: probe {name}: target.{lag_key} must be >= 0")
        tolerance_days = int(raw.get("tolerance_days", 0))
        warn_lead_days = int(raw.get("warn_lead_days", 0))
        if tolerance_days < 0 or warn_lead_days < 0:
            raise ValueError(f"sector {sector_name}: probe {name}: tolerance_days and warn_lead_days must be >= 0")
        probes.append(
            FreshnessProbe(
                name=name,
                kind=kind,
                target=dict(target),
                tolerance_days=tolerance_days,
                warn_lead_days=warn_lead_days,
                required=bool(raw.get("required", False)),
                notes=str(raw.get("notes") or ""),
            )
        )
    probe_names = [p.name for p in probes]
    duplicates = sorted({n for n in probe_names if probe_names.count(n) > 1})
    if duplicates:
        raise ValueError(f"sector {sector_name}: duplicate freshness probe names: {duplicates}")
    return probes


def load_registry(path: Path) -> Registry:
    source = Path(path).read_text(encoding="utf-8")
    document = yaml.compose(source, Loader=yaml.SafeLoader)
    if document is None:
        raise ValueError(f"registry {path} is empty")
    _assert_unique_yaml_keys(document)
    raw = yaml.safe_load(source)
    if not isinstance(raw, dict) or "sectors" not in raw:
        raise ValueError(f"registry {path} missing top-level 'sectors'")
    defaults = raw.get("defaults") or {}
    sectors: list[Sector] = []
    for entry in raw["sectors"]:
        sector_name = str(entry["name"])
        lineage_policy = policy_for_model_family(sector_name)
        legacy_lineage_required = entry.get("financial_lineage_required")
        production_lineage_required = lineage_policy.mode_for("production") != POLICY_DISABLED
        if legacy_lineage_required is not None and bool(legacy_lineage_required) != production_lineage_required:
            raise ValueError(
                f"sector {sector_name}: registry financial_lineage_required conflicts "
                "with orchestration/financial_lineage_policy.yaml"
            )
        health_raw = entry.get("health") or {}
        health = HealthSpec(
            manifest=health_raw.get("manifest"),
            status_keys=list(health_raw.get("status_keys") or []),
            healthy_values=[str(v) for v in (health_raw.get("healthy_values") or ["PASS"])],
        )
        backfill_raw = entry.get("backfill")
        backfill = None
        if backfill_raw and backfill_raw.get("script"):
            backfill = BackfillSpec(
                script=str(backfill_raw["script"]),
                args_template=list(backfill_raw.get("args_template") or []),
                per_date=bool(backfill_raw.get("per_date", False)),
                covers_target=bool(backfill_raw.get("covers_target", False)),
                note=str(backfill_raw.get("note") or ""),
            )
        elif backfill_raw and backfill_raw.get("note"):
            backfill = BackfillSpec(
                script="",
                args_template=[],
                per_date=False,
                covers_target=False,
                note=str(backfill_raw["note"]),
            )
        repair_raw = entry.get("repair")
        repair = None
        if repair_raw:
            repair = RepairSpec(
                date_flag=str(repair_raw.get("date_flag") or "--asof"),
                selection_flag=str(repair_raw.get("selection_flag") or ""),
                steps=list(repair_raw.get("steps") or []),
                rebuild_steps=list(repair_raw.get("rebuild_steps") or []),
                extra_args=list(repair_raw.get("extra_args") or []),
            )
        sectors.append(
            Sector(
                name=sector_name,
                db_group=str(entry["db_group"]),
                dependency_tier=int(entry.get("dependency_tier", 0)),
                required=bool(entry.get("required", True)),
                network=bool(entry.get("network", True)),
                entry_script=str(entry["entry_script"]),
                date_flag=str(entry.get("date_flag") or "--asof"),
                args_template=list(entry.get("args_template") or []),
                force_args=list(entry.get("force_args") or []),
                publish_glob=str(entry["publish_glob"]),
                publish_date_format=str(entry.get("publish_date_format") or "%Y-%m-%d"),
                oos_column=entry.get("oos_column"),
                gate_column=entry.get("gate_column"),
                require_oos_valid=bool(entry.get("require_oos_valid", False)),
                staleness_tolerance_days=int(entry.get("staleness_tolerance_days", 3)),
                backfill_window_days=(
                    int(entry["backfill_window_days"]) if entry.get("backfill_window_days") is not None else None
                ),
                native_backfill_min_dates=(
                    int(entry["native_backfill_min_dates"])
                    if entry.get("native_backfill_min_dates") is not None
                    else None
                ),
                publish_epoch=(parse_iso(str(entry["publish_epoch"])) if entry.get("publish_epoch") else None),
                historical_health_manifest_required=bool(
                    entry.get("historical_health_manifest_required", True)
                ),
                health=health,
                backfill=backfill,
                repair=repair,
                weekly_pre_steps=list(entry.get("weekly_pre_steps") or []),
                daily_post_steps=list(entry.get("daily_post_steps") or []),
                timeout_sec=int(entry.get("timeout_sec", defaults.get("timeout_sec", 21600))),
                retries=int(entry.get("retries", defaults.get("retries", 1))),
                retry_args=[str(arg) for arg in _as_list(entry.get("retry_args", defaults.get("retry_args")))],
                freshness_probes=_parse_freshness_probes(sector_name, entry.get("freshness_probes")),
                financial_lineage_required=production_lineage_required,
                financial_lineage_policy=lineage_policy.mode_for("production"),
                financial_lineage_min_core_metric_count=lineage_policy.min_core_metric_count,
                financial_lineage_artifact=(str(entry.get("financial_lineage_artifact") or "").strip() or None),
            )
        )
    names = [sector.name for sector in sectors]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"registry contains duplicate sector names: {duplicates}")
    for sector in sectors:
        if sector.dependency_tier not in {0, 1}:
            raise ValueError(f"sector {sector.name}: dependency_tier must be 0 or 1")
        if sector.timeout_sec <= 0:
            raise ValueError(f"sector {sector.name}: timeout_sec must be > 0")
        if sector.retries < 0:
            raise ValueError(f"sector {sector.name}: retries must be >= 0")
        if any(not arg.strip() for arg in sector.retry_args):
            raise ValueError(f"sector {sector.name}: retry_args cannot contain blank values")
        if sector.staleness_tolerance_days < 0:
            raise ValueError(f"sector {sector.name}: staleness_tolerance_days must be >= 0")
        if sector.backfill_window_days is not None and sector.backfill_window_days < 0:
            raise ValueError(f"sector {sector.name}: backfill_window_days must be >= 0")
        if (
            sector.native_backfill_min_dates is not None
            and sector.native_backfill_min_dates < 1
        ):
            raise ValueError(
                f"sector {sector.name}: native_backfill_min_dates must be >= 1"
            )
        if sector.native_backfill_min_dates is not None and (
            sector.backfill is None or not sector.backfill.script
        ):
            raise ValueError(
                f"sector {sector.name}: native_backfill_min_dates requires a native backfill script"
            )
        if sector.backfill is not None and sector.backfill.covers_target:
            if sector.backfill.per_date:
                raise ValueError(
                    f"sector {sector.name}: backfill.covers_target requires per_date=false"
                )
            if sector.daily_post_steps:
                raise ValueError(
                    f"sector {sector.name}: backfill.covers_target cannot be combined with daily_post_steps"
                )
            if "{to}" not in sector.backfill.args_template:
                raise ValueError(
                    f"sector {sector.name}: backfill.covers_target requires {{to}} in args_template"
                )
        if sector.publish_glob.count("{date}") != 1:
            raise ValueError(f"sector {sector.name}: publish_glob must contain exactly one {{date}}")
    group_order = {str(k): list(v) for k, v in (raw.get("group_order") or {}).items()}
    ordered_names = [str(name) for ordered in group_order.values() for name in ordered]
    unknown_ordered = sorted(set(ordered_names) - set(names))
    duplicate_ordered = sorted({name for name in ordered_names if ordered_names.count(name) > 1})
    if unknown_ordered or duplicate_ordered:
        raise ValueError(f"invalid group_order: unknown={unknown_ordered} duplicated={duplicate_ordered}")
    calendar_refs = list(defaults.get("calendar_reference_sectors") or [])
    unknown_refs = sorted(set(calendar_refs) - set(names))
    if unknown_refs:
        raise ValueError(f"calendar_reference_sectors contains unknown names: {unknown_refs}")

    lanes = int(defaults.get("max_concurrent_network_lanes", 2))
    if lanes < 1:
        raise ValueError(f"max_concurrent_network_lanes must be >= 1, got {lanes}")
    repair_days = int(defaults.get("repair_days", 5))
    if repair_days < 1:
        raise ValueError(f"defaults.repair_days must be >= 1, got {repair_days}")
    catch_up_threshold = int(defaults.get("catch_up_gap_backfill_threshold", 10))
    catch_up_window = int(defaults.get("catch_up_window_days", 45))
    if catch_up_threshold < 1 or catch_up_window < 1:
        raise ValueError("catch_up_gap_backfill_threshold and catch_up_window_days must both be >= 1")
    permanent_after = int(defaults.get("permanent_gap_after_failures", 3))
    if permanent_after < 1:
        raise ValueError(f"permanent_gap_after_failures must be >= 1, got {permanent_after}")
    closures = [parse_iso(str(raw_closure)) for raw_closure in _as_list(defaults.get("market_closures"))]
    # Ad-hoc closures must be visible to the module-level trading calendar
    # (is_trading_day has no registry context at its many call sites).
    global _AD_HOC_CLOSURES
    _AD_HOC_CLOSURES = frozenset(_to_date(c) for c in closures)
    return Registry(
        sectors=sectors,
        group_order=group_order,
        max_concurrent_network_lanes=lanes,
        catch_up_gap_backfill_threshold=catch_up_threshold,
        catch_up_window_days=catch_up_window,
        repair_days=repair_days,
        calendar_reference_sectors=calendar_refs,
        permanent_gap_after_failures=permanent_after,
        market_closures=closures,
    )


def validate_registry_paths(reg: Registry) -> None:
    missing: list[str] = []
    for sector in reg.sectors:
        candidates = [sector.entry_script]
        if sector.backfill and sector.backfill.script:
            candidates.append(sector.backfill.script)
        candidates.extend(
            str(step.get("script") or "") for step in [*sector.weekly_pre_steps, *sector.daily_post_steps]
        )
        for relative in candidates:
            if relative and not (PROJECT_ROOT / relative).is_file():
                missing.append(f"{sector.name}:{relative}")
    if missing:
        raise ValueError(f"registry references missing scripts: {missing}")


# Documented per-step subprocess ceilings: sector runner configs that declare an
# explicit per-step timeout their own step launcher enforces. The registry sector
# timeout_sec must dominate the largest such ceiling, otherwise the master kills
# a sector while one of its steps is still legitimately inside its own budget
# (2026-08-05 biotech post-mortem: registry 21600s default vs biotech's
# biotech_refresh.step_timeout_sec = 28800s). Sectors absent from this map do not
# document a per-step ceiling in config (their runners hard-code or omit one), so
# no relation is asserted for them. Extend this map when a runner gains one.
STEP_CEILING_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "biotech": ("biotech_index/config.yaml", ("biotech_refresh.step_timeout_sec",)),
    "portfolio_layer": (
        "portfolio_layer/config.yaml",
        (
            "orchestration.step_timeout_sec",
            "orchestration.macro_step_timeout_sec",
            "orchestration.monitor_step_timeout_sec",
        ),
    ),
}


def documented_step_ceiling_sec(sector_name: str) -> float | None:
    """Largest per-step timeout the sector's own config documents, else None."""
    source = STEP_CEILING_SOURCES.get(sector_name)
    if source is None:
        return None
    rel_path, dotted_keys = source
    config_path = PROJECT_ROOT / rel_path
    if not config_path.is_file():
        return None
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    values: list[float] = []
    for dotted in dotted_keys:
        node: Any = raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is None:
            continue
        try:
            values.append(float(node))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


# --------------------------------------------------------------------------- #
# Trading calendar (weekday + standard NYSE holiday rules with observed shifts)
# --------------------------------------------------------------------------- #
def parse_iso(raw: str) -> str:
    return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date().isoformat()


def _iso(d: date) -> str:
    return d.isoformat()


def _to_date(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (Mon=0) of `month`."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Computus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date, *, is_new_year: bool = False) -> date:
    """NYSE observed-holiday shift: Sat -> Fri, Sun -> Mon.

    New Year's Day is the documented exception: when Jan 1 falls on a Saturday the
    NYSE does NOT close the preceding Friday (Dec 31), so only the Sunday->Monday
    shift applies.
    """
    if d.weekday() == 5:  # Saturday
        return d if is_new_year else d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def us_market_holidays(year: int) -> set[date]:
    """Full-close NYSE holidays for `year` (observed shifts applied)."""
    holidays: set[date] = set()
    holidays.add(_observed(date(year, 1, 1), is_new_year=True))  # New Year's Day
    holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK (3rd Mon Jan)
    holidays.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday (3rd Mon Feb)
    holidays.add(_easter_sunday(year) - timedelta(days=2))  # Good Friday
    holidays.add(_last_weekday(year, 5, 0))  # Memorial Day (last Mon May)
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    holidays.add(_observed(date(year, 7, 4)))  # Independence Day
    holidays.add(_nth_weekday(year, 9, 0, 1))  # Labor Day (1st Mon Sep)
    holidays.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving (4th Thu Nov)
    holidays.add(_observed(date(year, 12, 25)))  # Christmas
    return holidays


# Ad-hoc full-day closures (mourning/disaster days, e.g. the 2025-01-09 Carter
# closure pattern) that no fixed rule can derive. Populated from the registry's
# defaults.market_closures by load_registry.
_AD_HOC_CLOSURES: frozenset[date] = frozenset()


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in us_market_holidays(d.year) and d not in _AD_HOC_CLOSURES


def latest_completed_trading_session(*, now_utc: datetime | None = None) -> str:
    """Latest COMPLETED trading session as of `now_utc` (default: real now).

    Weekday + market-close aware and holiday-aware: before ~17:00 ET (or on a
    non-trading day) the current session has not completed, so step back. Never
    derived from published artifacts.
    """
    now = now_utc or datetime.now(timezone.utc)
    now_et = now.astimezone(ZoneInfo(MARKET_TZ))
    today = now_et.date()
    if is_trading_day(today) and now_et.time() >= MARKET_CLOSE_ET:
        candidate = today
    else:
        candidate = today - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate.isoformat()


def publish_dir_root(sector: Sector) -> tuple[Path, str]:
    """Return (parent dir that holds dated folders, filename inside a dated folder)."""
    parts = sector.publish_glob.split("/")
    idx = parts.index("{date}")
    parent = PROJECT_ROOT / Path(*parts[:idx])
    filename = "/".join(parts[idx + 1 :])
    return parent, filename


def _publish_folder_name(sector: Sector, iso_date: str) -> str:
    """Dated-folder name for `iso_date` using the sector's publish_date_format
    (e.g. biotech '20260717', others '2026-07-17')."""
    return _to_date(iso_date).strftime(sector.publish_date_format)


def sector_published_dates(sector: Sector, *, require_file: bool = True) -> list[str]:
    """ISO dates of this sector's dated publish folders (optionally requiring the file)."""
    parent, filename = publish_dir_root(sector)
    if not parent.is_dir():
        return []
    out: list[str] = []
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        try:
            parsed = datetime.strptime(child.name, sector.publish_date_format).date()
        except ValueError:
            continue
        if require_file and not (child / filename).exists():
            continue
        out.append(parsed.isoformat())
    return sorted(set(out))


def last_published_date(sector: Sector, on_or_before: str) -> str | None:
    candidates = [d for d in sector_published_dates(sector) if d <= on_or_before]
    return candidates[-1] if candidates else None


def known_trading_dates(reg: Registry) -> list[str]:
    """Union of valid trading-session folders across the reference sectors.

    Dated publisher folders are evidence, not a market calendar. A manual run or
    an upstream bug can create a weekend/holiday folder; retaining that date in
    catch-up would then cause every other sector to manufacture the same invalid
    session. Keep those folders on disk for audit, but never let them seed the
    orchestration calendar.
    """
    refs = reg.calendar_reference_sectors or reg.names
    seen: set[str] = set()
    for name in refs:
        try:
            sector = reg.by_name(name)
        except KeyError:
            continue
        for published in sector_published_dates(sector, require_file=False):
            if is_trading_day(_to_date(published)):
                seen.add(published)
    return sorted(seen)


def trading_dates_in_range(reg: Registry, start: str, end: str) -> list[str]:
    """Trading dates in [start, end], using valid published sessions plus calendar fill.

    The weekday fill applies standard NYSE holiday rules (observed shifts included),
    so e.g. 2026-07-03 (observed Independence Day) is excluded even though it is a
    Friday. Published weekend/holiday folders are ignored rather than propagated.
    """
    if start > end:
        return []
    known = {d for d in known_trading_dates(reg) if start <= d <= end}
    out = set(known)
    cur = _to_date(start)
    last = _to_date(end)
    while cur <= last:
        if is_trading_day(cur):
            out.add(cur.isoformat())
        cur += timedelta(days=1)
    return sorted(out)


def is_first_trading_session_of_week(iso_date: str) -> bool:
    """Return true when ``iso_date`` is the first market session in its ISO week."""
    target = _to_date(iso_date)
    week = target.isocalendar()[:2]
    cursor = target - timedelta(days=1)
    while cursor.isocalendar()[:2] == week:
        if is_trading_day(cursor):
            return False
        cursor -= timedelta(days=1)
    return True


def _missing_from_expected(published: set[str], expected: list[str]) -> list[str]:
    return sorted(d for d in expected if d not in published)


def sector_backfill_window_days(sector: Sector) -> int:
    """Calendar-day window catch-up may auto-run missing historical dates in."""
    if sector.backfill_window_days is not None:
        return sector.backfill_window_days
    return sector.staleness_tolerance_days


# --------------------------------------------------------------------------- #
# Permanent-gap markers (catch-up backfill tombstones)
# --------------------------------------------------------------------------- #
def load_gap_markers(path: Path = GAP_MARKER_PATH) -> dict[str, Any]:
    """{"sectors": {sector: {date: {failures, permanent, reason, ...}}}}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sectors": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sectors"), dict):
        return {"sectors": {}}
    return data


def save_gap_markers(markers: dict[str, Any], path: Path = GAP_MARKER_PATH) -> None:
    write_atomic(Path(path), json.dumps(markers, indent=2, sort_keys=True) + "\n")


def permanent_gap_dates(
    markers: dict[str, Any],
    sector_name: str,
    *,
    include_auto: bool = True,
) -> set[str]:
    """Return permanent gaps, optionally excluding automatic tombstones.

    An explicit operator catch-up is a deliberate retry after code/data repair, so it
    may revisit automatically tombstoned dates. Operator-marked (and legacy records
    without a source) remain permanent until explicitly cleared.
    """
    out: set[str] = set()
    for iso_date, rec in (markers.get("sectors", {}).get(sector_name) or {}).items():
        if (
            isinstance(rec, dict)
            and rec.get("permanent")
            and (include_auto or str(rec.get("source") or "") != "auto")
        ):
            out.add(str(iso_date))
    return out


def record_gap_failure(
    markers: dict[str, Any], sector_name: str, iso_date: str, reason: str, *, auto_permanent_after: int
) -> None:
    """Record one backfill-date failure; auto-tombstone after N consecutive failures.

    A date whose inputs can never exist (e.g. sector scores predating that sector's
    OOS promotion) fails deterministically forever; without a tombstone it would be
    retried every night. Transient failures self-heal: a later success clears the
    record (clear_gap_record) before the threshold is reached.
    """
    sectors = markers.setdefault("sectors", {})
    per_sector = sectors.setdefault(sector_name, {})
    rec = per_sector.get(iso_date)
    if not isinstance(rec, dict):
        rec = {"failures": 0, "permanent": False, "source": "auto"}
    rec["failures"] = int(rec.get("failures", 0)) + 1
    rec["reason"] = reason
    rec["last_failure_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if rec["failures"] >= auto_permanent_after and not rec.get("permanent"):
        rec["permanent"] = True
        rec["source"] = rec.get("source") or "auto"
    per_sector[iso_date] = rec


def clear_gap_record(markers: dict[str, Any], sector_name: str, iso_date: str) -> None:
    per_sector = markers.get("sectors", {}).get(sector_name)
    if isinstance(per_sector, dict):
        per_sector.pop(iso_date, None)


@dataclass(frozen=True)
class GapReport:
    """Per-sector missing-date classification for one catch-up target."""

    target_missing: bool
    backfill_missing: list[str]  # auto-run candidates, OLDEST-first
    historical_gaps: list[str]  # inside the outer window but older than the backfill window
    permanent_gaps: list[str]  # tombstoned by marker; skipped but surfaced


def sector_gap_report(
    reg: Registry,
    sector: Sector,
    target: str,
    *,
    markers: dict[str, Any] | None = None,
    catch_up_from: str | None = None,
) -> GapReport:
    """Classify this sector's unpublished or contract-incomplete dates.

    The CURRENT target date is classified separately (target_missing) and is never
    window-bounded or tombstoned: current production is always attempted. Historical
    dates are auto-run candidates only within [target - backfill_window_days, target),
    unless an operator supplies catch_up_from as an explicit inclusive lower bound;
    older in-(outer-)window gaps are surfaced as historical_gaps for reporting. Dates
    before the sector's publish_epoch (or before its first publish, or beyond
    catch_up_window_days) are exempt entirely -- a repointed publish_glob must not
    retroactively manufacture missing history.
    """
    published = set(sector_published_dates(sector))
    target_missing = target not in published
    published_on_or_before = sorted(d for d in published if d <= target)
    if not published_on_or_before:
        # Never published at/before target: only the current target is actionable.
        return GapReport(target_missing, [], [], [])
    outer_start = (_to_date(target) - timedelta(days=reg.catch_up_window_days)).isoformat()
    floor = max(published_on_or_before[0], outer_start)
    if sector.publish_epoch:
        floor = max(floor, sector.publish_epoch)
    if catch_up_from:
        floor = max(floor, catch_up_from)
    if floor > target:
        return GapReport(target_missing, [], [], [])
    expected = trading_dates_in_range(reg, floor, target)
    expected_set = set(expected)
    # A dated rank file alone is not a complete publication. Historical
    # catch-up must also detect malformed/date-mismatched files and required
    # lineage sidecars that are absent or invalid. Root health manifests are
    # current-state artifacts and cannot certify an older date. A manifest with
    # a {date} partition is different: it is immutable per session and must be
    # healthy before that historical date counts as complete. This distinction
    # prevents an old final artifact from hiding a failed portfolio run.
    verify_historical_manifest = bool(
        sector.historical_health_manifest_required
        and sector.health.manifest
        and "{date}" in sector.health.manifest
    )
    complete_historical = {
        d
        for d in published
        if d == target
        or (
            d in expected_set
            and verify_published_artifact_for_date(
                sector,
                d,
                verify_manifest=verify_historical_manifest,
                policy_context="historical",
            )[0]
        )
    }
    missing_all = [d for d in expected if d not in complete_historical and d != target]
    window_start = (
        floor
        if catch_up_from
        else max(floor, (_to_date(target) - timedelta(days=sector_backfill_window_days(sector))).isoformat())
    )
    tombstoned = permanent_gap_dates(
        markers if markers is not None else load_gap_markers(),
        sector.name,
        # --catch-up-from is an explicit bounded retry. It overrides only
        # auto-tombstones; operator/legacy permanent gaps remain fail-closed.
        include_auto=not bool(catch_up_from),
    )
    backfill = sorted(d for d in missing_all if d >= window_start and d not in tombstoned)
    historical = sorted(d for d in missing_all if d < window_start and d not in tombstoned)
    permanent = sorted(d for d in missing_all if d in tombstoned)
    return GapReport(target_missing, backfill, historical, permanent)


def resolve_target_date(requested: str | None, *, now_utc: datetime | None = None) -> str:
    if requested:
        iso = parse_iso(requested)
        # An explicitly requested target must be a real session: running every
        # sector for a weekend/holiday/closure manufactures phantom dated
        # artifacts that can 'PASS' for a session that never existed.
        if not is_trading_day(_to_date(iso)):
            raise SystemExit(f"--as-of {iso} is not a trading session (weekend/holiday/ad-hoc closure)")
        return iso
    return latest_completed_trading_session(now_utc=now_utc)


# --------------------------------------------------------------------------- #
# Command builders
# --------------------------------------------------------------------------- #
def _sub(tokens: list[str], mapping: dict[str, str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        for key, value in mapping.items():
            tok = tok.replace("{" + key + "}", value)
        out.append(tok)
    return out


def daily_command(sector: Sector, iso_date: str, *, force: bool) -> list[str]:
    cmd = [PY, str(PROJECT_ROOT / sector.entry_script)]
    cmd += _sub(sector.args_template, {"date": iso_date})
    if force and sector.force_args:
        cmd += sector.force_args
    return cmd


def weekly_pre_commands(sector: Sector, iso_date: str) -> list[list[str]]:
    cmds: list[list[str]] = []
    for step in sector.weekly_pre_steps:
        script = str(step["script"])
        cmds.append([PY, str(PROJECT_ROOT / script)] + _sub(list(step.get("args_template") or []), {"date": iso_date}))
    return cmds


def daily_post_commands(
    sector: Sector,
    iso_date: str,
    *,
    historical: bool = False,
) -> list[list[str]]:
    """Post-publish steps that must run (in order) AFTER the sector's daily publish.

    Used by med_devices to run 76_mark_med_device_oos_provenance.py after the refresh
    (finding 1): the daily refresh self-certifies oos_score_valid_flag=1 for replay-window
    rows via --oos-score-valid, and script 76 -- the sole strict-OOS promoter per the
    med-devices PIT policy -- then records the evidence/summary-ledger row and reconciles
    the flag against the database. run_commands executes these sequentially and stops on the
    first non-zero rc, so a post-step failure fails the whole sector.
    """
    cmds: list[list[str]] = []
    for step in sector.daily_post_steps:
        if bool(step.get("historical_only", False)) and not historical:
            continue
        script = str(step["script"])
        raw_template = (
            step.get("historical_args_template")
            if historical and step.get("historical_args_template") is not None
            else step.get("args_template")
        )
        cmds.append(
            [PY, str(PROJECT_ROOT / script)]
            + _sub(list(raw_template or []), {"date": iso_date})
        )
    return cmds


def backfill_commands(
    sector: Sector,
    frm: str,
    to: str,
    reg: Registry,
    *,
    exact_dates: list[str] | None = None,
) -> tuple[list[list[str]], str]:
    """Return (commands, note). Empty commands with a note => nothing native to run."""
    if sector.backfill is None or not sector.backfill.script:
        note = sector.backfill.note if sector.backfill else "no native backfill entry"
        return [], note
    dates = list(exact_dates) if exact_dates is not None else trading_dates_in_range(reg, frm, to)
    dates_csv = ",".join(dates)
    if sector.backfill.per_date:
        cmds = [
            [PY, str(PROJECT_ROOT / sector.backfill.script)]
            + _sub(
                sector.backfill.args_template,
                {"date": d, "dates": d, "from": frm, "to": to},
            )
            for d in dates
        ]
        return cmds, ""
    cmd = [PY, str(PROJECT_ROOT / sector.backfill.script)] + _sub(
        sector.backfill.args_template,
        {"dates": dates_csv, "from": frm, "to": to},
    )
    return [cmd], ""


def _repair_selection_cmd(sector: Sector, iso_date: str, step_ids: list[str]) -> list[str]:
    cmd = [PY, str(PROJECT_ROOT / sector.entry_script), sector.repair.date_flag, iso_date]  # type: ignore[union-attr]
    if sector.repair.selection_flag and step_ids:  # type: ignore[union-attr]
        cmd += [sector.repair.selection_flag, ",".join(step_ids)]  # type: ignore[union-attr]
    cmd += sector.repair.extra_args  # type: ignore[union-attr]
    return cmd


def repair_commands(sector: Sector, dates: list[str]) -> list[list[str]]:
    """Two-stage repair: re-run the source (network/positioning) steps, then rebuild the
    dependent features->scores->publish chain so the published artifact reflects the
    repaired sources. Sectors with no selection_flag (defense) carry the full tail in
    extra_args and emit a single command.
    """
    if sector.repair is None:
        return []
    cmds: list[list[str]] = []
    for iso_date in dates:
        if not sector.repair.selection_flag:
            # No step selector: extra_args (e.g. --positioning-through-publish-only)
            # already rebuilds positioning->eligibility->scores->publish.
            cmds.append(_repair_selection_cmd(sector, iso_date, []))
            continue
        if sector.repair.steps:
            cmds.append(_repair_selection_cmd(sector, iso_date, sector.repair.steps))
        if sector.repair.rebuild_steps:
            cmds.append(_repair_selection_cmd(sector, iso_date, sector.repair.rebuild_steps))
    return cmds


def _catch_up_daily_command(
    sector: Sector,
    iso_date: str,
    *,
    force: bool,
    live_completed_session: str,
) -> list[str]:
    """Build one catch-up command without backdating current-only provider events."""
    command = daily_command(sector, iso_date, force=force)
    if sector.name == "portfolio_layer" and iso_date < live_completed_session:
        command.append("--historical-catchup")
        # Catch-up repairs an incomplete dated run; it is not an implicit
        # historical recalibration. Preserve same-date sealed parents and let
        # Stage 12's manifest gates resume at the first missing/failed child.
        # An operator who intentionally wants a current-code reconstruction can
        # still pass run_all --force explicitly.
    return command


@dataclass(frozen=True)
class CatchUpPlan:
    """Execution plan: strict prerequisites, oldest-first gaps, current target last."""

    target: str
    target_missing: bool
    target_unhealthy: bool
    target_reasons: tuple[str, ...]
    pre_commands: list[list[str]]
    target_commands: list[list[str]]  # empty only when target is published and healthy
    backfill_groups: list[tuple[tuple[str, ...], list[list[str]]]]  # (dates, commands), oldest-first
    historical_gaps: list[str]
    permanent_gaps: list[str]
    used_native_backfill: bool
    native_backfill_covers_target: bool

    @property
    def target_needs_run(self) -> bool:
        return bool(self.target_commands)

    @property
    def all_commands(self) -> list[list[str]]:
        out = list(self.pre_commands)
        for _dates, cmds in self.backfill_groups:
            out.extend(cmds)
        out.extend(self.target_commands)
        return out

    @property
    def backfill_dates(self) -> list[str]:
        return [d for dates, _cmds in self.backfill_groups for d in dates]


def plan_note(plan: CatchUpPlan) -> str:
    note = (
        f"target_missing={plan.target_missing}"
        f" target_unhealthy={plan.target_unhealthy}"
        f" backfill={len(plan.backfill_dates)}"
        f" historical_gaps={len(plan.historical_gaps)}"
        f" permanent_gaps={len(plan.permanent_gaps)}"
    )
    if plan.target_reasons:
        note += " target_reasons=" + "|".join(plan.target_reasons)
    return note


def build_catch_up_plan(
    reg: Registry,
    sector: Sector,
    target: str,
    *,
    force: bool,
    include_weekly_pre: bool = False,
    live_completed_session: str | None = None,
    markers: dict[str, Any] | None = None,
    catch_up_from: str | None = None,
) -> CatchUpPlan:
    """Build a state-safe oldest-first catch-up plan.

    Historical dates are attempted oldest-first because sector databases and root
    manifests advance monotonically. Their failures remain best-effort and never
    block the final current-target rebuild. Gaps larger than
    catch_up_gap_backfill_threshold use the sector's
    native backfill entry, CHUNKED to at most threshold dates per command so one
    subprocess never has to fit N full per-date rebuilds inside the single-command
    timeout ceiling; every chunk still runs the per-date daily_post_steps (e.g.
    med-devices script 76 provenance) so the post-publish contract holds on the
    backfill route too.
    """
    # live_completed_session is sampled ONCE at orchestrator startup (main resolves
    # it next to the target) so a run spanning the 17:00 ET close cannot reclassify
    # the intended current-session command as historical hours later.
    live_session = live_completed_session or latest_completed_trading_session()
    report = sector_gap_report(
        reg,
        sector,
        target,
        markers=markers,
        catch_up_from=catch_up_from,
    )
    missing_weekly_session = any(
        is_first_trading_session_of_week(iso_date)
        for iso_date in report.backfill_missing
    )
    weekly_due = bool(sector.weekly_pre_steps) and (
        include_weekly_pre or missing_weekly_session
    )
    target_unhealthy = False
    target_reasons: list[str] = []
    if not report.target_missing:
        target_ok, target_reasons = verify_published_artifact_for_date(sector, target)
        target_unhealthy = not target_ok
    # If a missed first session skipped a weekly prerequisite, refresh that
    # prerequisite before rebuilding the current target. This keeps a Friday
    # catch-up from silently consuming the previous week's universe screen.
    if weekly_due:
        target_unhealthy = True
        target_reasons.append("weekly prerequisite due for actionable first-session gap")
    pre_cmds = weekly_pre_commands(sector, target) if weekly_due else []
    groups: list[tuple[tuple[str, ...], list[list[str]]]] = []
    missing = report.backfill_missing  # oldest-first
    native_min_dates = (
        sector.native_backfill_min_dates
        if sector.native_backfill_min_dates is not None
        else reg.catch_up_gap_backfill_threshold + 1
    )
    used_native = (
        len(missing) >= native_min_dates
        and sector.backfill is not None
        and bool(sector.backfill.script)
    )
    if used_native:
        ascending = sorted(missing)
        if sector.backfill is not None and sector.backfill.covers_target:
            cmds, _bf_note = backfill_commands(
                sector,
                ascending[0],
                target,
                reg,
                exact_dates=ascending,
            )
            groups.append((tuple(ascending), cmds))
        else:
            chunk_size = reg.catch_up_gap_backfill_threshold
            chunks = [ascending[i : i + chunk_size] for i in range(0, len(ascending), chunk_size)]
            for chunk in chunks:
                cmds, _bf_note = backfill_commands(
                    sector,
                    chunk[0],
                    chunk[-1],
                    reg,
                    exact_dates=chunk,
                )
                for iso_date in chunk:
                    cmds.extend(daily_post_commands(sector, iso_date, historical=True))
                groups.append((tuple(chunk), cmds))
    else:
        for iso_date in missing:
            cmds = [_catch_up_daily_command(sector, iso_date, force=force, live_completed_session=live_session)]
            # Preserve the exact daily post-publish contract for every date.
            cmds.extend(daily_post_commands(sector, iso_date, historical=True))
            groups.append(((iso_date,), cmds))
    # Any historical work can leave global root tables/manifests on the last
    # backfill date. Rebuild the current target last even when its dated file
    # already existed before this run.
    target_cmds: list[list[str]] = []
    native_covers_target = bool(
        used_native
        and sector.backfill is not None
        and sector.backfill.covers_target
    )
    historical_work_requires_target_restore = bool(groups) and (
        sector.name != "portfolio_layer"
    )
    if (
        report.target_missing
        or target_unhealthy
        or historical_work_requires_target_restore
    ) and not native_covers_target:
        # Sector backfills can leave mutable root tables on an older date, so
        # their target restoration remains forced. Portfolio-layer history is
        # date-partitioned (persistent stores are append-only by as-of), and its
        # own manifest DAG safely resumes at the first incomplete group. Its
        # dated artifacts do not need a target replay when the target already
        # verifies healthy.
        target_force = force or (
            bool(groups) and sector.name != "portfolio_layer"
        )
        target_cmds.append(
            _catch_up_daily_command(
                sector,
                target,
                force=target_force,
                live_completed_session=live_session,
            )
        )
        target_cmds.extend(daily_post_commands(sector, target))
    return CatchUpPlan(
        target=target,
        target_missing=report.target_missing,
        target_unhealthy=target_unhealthy,
        target_reasons=tuple(target_reasons),
        pre_commands=pre_cmds,
        target_commands=target_cmds,
        backfill_groups=groups,
        historical_gaps=report.historical_gaps,
        permanent_gaps=report.permanent_gaps,
        used_native_backfill=used_native,
        native_backfill_covers_target=native_covers_target,
    )


# --------------------------------------------------------------------------- #
# Atomic writes (OneDrive-tolerant)
# --------------------------------------------------------------------------- #
def write_atomic(path: Path, text: str, *, retries: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(retries):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # OneDrive can transiently lock the target
            last_exc = exc
            Path(tmp).unlink(missing_ok=True)
            time.sleep(0.4 * (attempt + 1))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    raise RuntimeError(f"write_atomic failed after {retries} attempts: {path}") from last_exc


def sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _command_hash(commands: list[list[str]]) -> str:
    joined = "\n".join(subprocess.list2cmdline(cmd) for cmd in commands)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _sector_source_inputs(sector: Sector) -> list[Path]:
    """Return deterministic source/config inputs that can change a sector command.

    The previous resume key covered only the thin entry wrapper. Most wrappers dispatch
    into shared modules, and med-devices also has a post-publish provenance script. A
    change in either surface could therefore leave the old content hash unchanged and
    incorrectly reuse a stale artifact. Hash the sector's complete top-level source tree
    (Python + YAML policy/config) plus every explicitly registered helper script.
    """
    entry = PROJECT_ROOT / sector.entry_script
    top_level = PROJECT_ROOT / Path(sector.entry_script).parts[0]
    paths: set[Path] = {entry}
    if top_level.is_dir():
        for pattern in ("*.py", "*.yaml", "*.yml"):
            paths.update(
                path
                for path in top_level.rglob(pattern)
                if "__pycache__" not in path.parts and ".git" not in path.parts
            )
    if sector.backfill and sector.backfill.script:
        paths.add(PROJECT_ROOT / sector.backfill.script)
    for step in [*sector.weekly_pre_steps, *sector.daily_post_steps]:
        script = str(step.get("script") or "").strip()
        if script:
            paths.add(PROJECT_ROOT / script)
    return sorted((path.resolve() for path in paths if path.is_file()), key=lambda path: str(path).lower())


def _content_hash(sector: Sector, registry_path: Path) -> str:
    """Hash the effective source/config surface and registry for safe daily resume."""
    h = hashlib.sha256()
    for path in _sector_source_inputs(sector):
        try:
            relative = path.relative_to(PROJECT_ROOT.resolve()).as_posix()
        except ValueError:
            relative = str(path)
        h.update(relative.encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(path).encode("ascii"))
        h.update(b"\n")
    h.update(b"registry\0")
    h.update(sha256_file(Path(registry_path).resolve()).encode("ascii"))
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Global orchestration lock (finding 13)
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int | None) -> bool | None:
    """Is process `pid` currently alive? True/False, or None when undeterminable.

    Cross-platform and dependency-free (psutil is not installed in the staging env):
    Windows uses OpenProcess+GetExitCodeProcess via ctypes; POSIX uses os.kill(pid, 0).
    A None result (query failed) lets callers fall back to age-based staleness.
    """
    if pid is None or pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            ERROR_INVALID_PARAMETER = 87
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                err = ctypes.get_last_error()
                if err == ERROR_ACCESS_DENIED:
                    return True  # exists but not queryable -> alive
                if err == ERROR_INVALID_PARAMETER:
                    return False  # no such process
                return None
            try:
                exit_code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if ok and exit_code.value != STILL_ACTIVE:
                    return False
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return None
    return True


# Slack when comparing a PID's creation time with a recorded start timestamp:
# clock skew + the gap between process start and timestamp write.
_PID_IDENTITY_SLACK_SEC = 120


def _pid_creation_time_utc(pid: int | None) -> datetime | None:
    """Process creation time (UTC), or None when unavailable.

    Windows: GetProcessTimes via ctypes (FILETIME, 100ns ticks since 1601-01-01).
    Elsewhere (not used in this deployment) returns None so callers fall back to
    plain liveness.
    """
    if pid is None or pid <= 0 or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

            creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            )
            if not ok:
                return None
            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            if ticks <= 0:
                return None
            return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _parse_utc_timestamp(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _holder_alive(pid: int | None, started_utc_iso: str) -> bool | None:
    """PID liveness WITH identity: a recycled PID must not impersonate a dead holder.

    Windows PID reuse can make a crashed orchestrator look alive forever (lock never
    overridden, RUNNING manifest never reconciled). When the recorded holder start
    time is known and the live process at that PID was created materially AFTER it,
    the recorded holder is provably dead regardless of raw liveness.
    """
    alive = _pid_alive(pid)
    if alive is not True:
        return alive
    started = _parse_utc_timestamp(started_utc_iso)
    if started is None:
        return alive
    created = _pid_creation_time_utc(pid)
    if created is None:
        return alive
    if created > started + timedelta(seconds=_PID_IDENTITY_SLACK_SEC):
        return False  # PID recycled by an unrelated process
    return True


def make_run_stamp() -> str:
    """Run-dir stamp with sub-second + PID suffix so two masters (or two runs in the same
    second) never collide on the same run directory (finding 9)."""
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%f')}Z_p{os.getpid()}"


class _RecoveryMutex:
    """OS advisory mutex serializing stale path-lock recovery attempts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> _RecoveryMutex:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_args: object) -> None:
        if self.fd is None:
            return
        os.lseek(self.fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


class OrchestrationLock:
    """Exclusive O_CREAT|O_EXCL lock so two masters can't run concurrently, with a
    stale-age override for crash recovery."""

    def __init__(self, path: Path, *, stale_after_sec: int) -> None:
        self.path = path
        self.stale_after_sec = stale_after_sec
        self.fd: int | None = None
        self._children: set[int] = set()
        self._state_lock = threading.Lock()
        self._started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def __enter__(self) -> OrchestrationLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = self._try_create()
        if self.fd is None:
            # Serialize stale-lock inspection. Without this guard, two recovery
            # processes can both classify the old owner as dead and one can remove the
            # other's newly created lock. A normal contender may still win O_EXCL after
            # removal; in that case we simply fail instead of ever deleting its lock.
            with _RecoveryMutex(self.path.with_name(f"{self.path.name}.recovery")):
                self.fd = self._try_create()
                if self.fd is None:
                    # Only override a lock whose recorded holder is provably gone:
                    # PID-liveness (with creation-time identity, so a recycled PID
                    # cannot impersonate the dead holder) beats age, and live
                    # descendants keep ownership closed.
                    recorded_pid = self._recorded_pid()
                    alive = (
                        _holder_alive(recorded_pid, self._recorded_started_utc()) if recorded_pid is not None else None
                    )
                    recorded_children = self._recorded_children()
                    live_children = [pid for pid in recorded_children if _pid_alive(pid) is True]
                    age = self._age()
                    stale_by_age = age is not None and age > self.stale_after_sec
                    override = False
                    reason = ""
                    if alive is False and live_children:
                        reason = f"holder pid={recorded_pid} is dead but child processes remain alive: {live_children}"
                    elif alive is False:
                        override, reason = True, f"holder pid={recorded_pid} is not alive"
                    elif alive is None and stale_by_age:
                        override, reason = True, f"holder pid unknown and age={age:.0f}s > {self.stale_after_sec}s"
                    if override:
                        print(f"orchestration lock {self.path} override ({reason}); overriding", flush=True)
                        self.path.unlink(missing_ok=True)
                        self.fd = self._try_create()
            if self.fd is None:
                recorded_pid = self._recorded_pid()
                alive = _holder_alive(recorded_pid, self._recorded_started_utc()) if recorded_pid is not None else None
                age = self._age()
                reason = "lock remained owned after serialized recovery"
                detail = self.path.read_text(encoding="utf-8", errors="replace") if self.path.exists() else ""
                live = "alive" if alive else ("dead" if alive is False else "unknown")
                raise RuntimeError(
                    f"Another orchestrator holds {self.path} (holder pid={recorded_pid} {live}, "
                    f"age={age if age is None else round(age)}s; {reason}): {detail.strip()}"
                )
        try:
            self._write_state()
        except BaseException:
            # Context-manager __exit__ is not called when __enter__ fails.
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.path.unlink(missing_ok=True)
            raise
        global _ACTIVE_ORCHESTRATION_LOCK
        _ACTIVE_ORCHESTRATION_LOCK = self
        return self

    def _write_state(self) -> None:
        if self.fd is None:
            return
        payload = (
            f"pid={os.getpid()} started_utc={self._started_utc}\n"
            f"children={','.join(str(pid) for pid in sorted(self._children))}\n"
        ).encode()
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, payload)
        os.fsync(self.fd)

    def register_child(self, pid: int) -> None:
        with self._state_lock:
            self._children.add(pid)
            self._write_state()

    def unregister_child(self, pid: int) -> None:
        with self._state_lock:
            self._children.discard(pid)
            self._write_state()

    def _try_create(self) -> int | None:
        try:
            return os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None

    def _age(self) -> float | None:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return None

    def _recorded_pid(self) -> int | None:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        match = re.search(r"pid=(\d+)", text)
        return int(match.group(1)) if match else None

    def _recorded_started_utc(self) -> str:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        match = re.search(r"started_utc=(\S+)", text)
        return match.group(1) if match else ""

    def _recorded_children(self) -> list[int]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        match = re.search(r"^children=([0-9,]*)$", text, flags=re.MULTILINE)
        if not match or not match.group(1):
            return []
        return [int(value) for value in match.group(1).split(",") if value]

    def __exit__(self, *_args: object) -> None:
        global _ACTIVE_ORCHESTRATION_LOCK
        if _ACTIVE_ORCHESTRATION_LOCK is self:
            _ACTIVE_ORCHESTRATION_LOCK = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


_ACTIVE_ORCHESTRATION_LOCK: OrchestrationLock | None = None


# --------------------------------------------------------------------------- #
# Health / verification
# --------------------------------------------------------------------------- #
def _flag_true(raw: object) -> bool:
    """Truthy flag parser accepting '1', '1.0', 1, 'true', etc. (finding 11)."""
    text = str(raw if raw is not None else "").strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"", "0", "false", "f", "no", "n"}:
        return False
    try:
        return float(text) == 1.0
    except ValueError:
        return False


def _is_json_artifact(path: Path) -> bool:
    return path.suffix.lower() == ".json"


def _load_json_artifact(path: Path) -> Any | None:
    """Parsed JSON artifact content, or None when missing/unreadable/invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _count_oos_valid(path: Path, sector: Sector) -> tuple[int, int]:
    """(oos_valid_or_gate_rows, total_rows) for the published table.

    JSON artifacts (the portfolio's final_manifest.json) are parsed as JSON, never
    through CSV line-count heuristics: a dict counts as one row, a list as its
    length, so serialization style (pretty vs compact) cannot change the verdict.
    """
    if not path.exists():
        return 0, 0
    if _is_json_artifact(path):
        data = _load_json_artifact(path)
        if data is None:
            return 0, 0
        if isinstance(data, list):
            return 0, len(data)
        return 0, 1 if data else 0
    col = sector.oos_column or sector.gate_column
    total = 0
    valid = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                total += 1
                if col and _flag_true(row.get(col, "")):
                    valid += 1
    except OSError:
        return 0, 0
    return valid, total


def _manifest_asof(data: dict[str, Any]) -> str:
    for key in ("asof_date", "asof", "run_as_of", "as_of", "target", "target_date", "asof_iso"):
        value = data.get(key)
        if value:
            return str(value)[:10]
    return ""


def read_manifest(sector: Sector, iso_date: str | None) -> tuple[str, str]:
    """Return (status, asof) from the sector's health manifest.

    status: NO_MANIFEST | MISSING | UNREADABLE | PASS | FAIL | UNKNOWN.
    """
    if not sector.health.manifest:
        return "NO_MANIFEST", ""
    manifest_date = _publish_folder_name(sector, iso_date) if iso_date else ""
    rel = sector.health.manifest.replace("{date}", manifest_date)
    path = PROJECT_ROOT / rel
    if not path.exists():
        return "MISSING", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "UNREADABLE", ""
    healthy = {str(v).upper() for v in (sector.health.healthy_values or ["PASS"])}
    verdicts = [str(data.get(key, "")).upper() for key in sector.health.status_keys]
    status = "PASS" if verdicts and all(v in healthy for v in verdicts) else "FAIL" if verdicts else "UNKNOWN"
    return status, _manifest_asof(data)


def _oos_required(sector: Sector, *, policy_context: str = "production") -> bool:
    """Return whether this artifact must contain a live-valid OOS row.

    A configured oos_column/gate_column merely says WHICH column carries the flag,
    never that valid rows are required: transportation's sealed zero-overlay shadow
    model declares oos_column with require_oos_valid: false (its rank tables carry
    oos_score_valid_flag=0 by design) and must not fail artifact verification.

    Historical sector snapshots are research inputs, not claims that a signal was
    available in live production. Requiring a live OOS flag on those snapshots
    creates an impossible retry loop for pre-lock dates and can tempt a backfill to
    fabricate live provenance. Current-date production health remains strict.
    """
    return sector.require_oos_valid and policy_context == "production"


# Recognized "as-of" date columns in a published table, most-specific first. Every
# sector's published CSV carries exactly one of these (asof_date for the sector rank
# tables / score packs, as_of_date for the portfolio stocks_scores.csv).
_DATE_COLUMN_CANDIDATES = (
    "asof_date",
    "as_of_date",
    "asof",
    "as_of",
    "run_as_of",
    "target_date",
    "trade_date",
    "session_date",
    "date",
)


def _csv_date_column_matches(path: Path, iso_date: str) -> tuple[bool, bool, str]:
    """Does the artifact's internal as-of column equal the folder date `iso_date`?

    Returns (checked, ok, detail):
      * checked=False -> the CSV has no recognized date column (nothing to verify here).
      * checked=True, ok=False -> a recognized column carries value(s) != iso_date.
    Values are normalized to their first 10 chars so a timestamp compares by date.
    """
    if not path.exists():
        return False, False, "missing"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            col = next((c for c in _DATE_COLUMN_CANDIDATES if c in fieldnames), None)
            if col is None:
                return False, True, "no_date_column"
            mismatches: set[str] = set()
            blank_rows = 0
            row_count = 0
            for row in reader:
                row_count += 1
                value = str(row.get(col) or "").strip()[:10]
                if not value:
                    blank_rows += 1
                elif value != iso_date:
                    mismatches.add(value)
            if mismatches:
                return True, False, f"{col}={sorted(mismatches)} != folder {iso_date}"
            if blank_rows:
                return True, False, f"{col} blank on {blank_rows}/{row_count} rows"
            if row_count == 0:
                return True, False, f"{col} present but CSV has 0 data rows"
            return True, True, col
    except OSError:
        return False, False, "unreadable"


def _financial_lineage_errors(
    path: Path,
    iso_date: str,
    *,
    policy_mode: str = POLICY_STRICT_UNIVERSE,
    min_core_metric_count: int = DEFAULT_MIN_CORE_METRIC_COUNT,
) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
    except OSError as exc:
        return [f"financial lineage contract unreadable: {exc}"]
    evaluation = evaluate_financial_lineage_rows(
        rows,
        policy_mode=policy_mode,
        expected_asof=iso_date,
        min_core_metric_count=min_core_metric_count,
    )
    return evaluation.errors


def _financial_lineage_sidecar_errors(
    rank_path: Path,
    lineage_path: Path,
    iso_date: str,
) -> list[str]:
    try:
        with rank_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rank_rows = [dict(row) for row in csv.DictReader(handle)]
        with lineage_path.open("r", encoding="utf-8-sig", newline="") as handle:
            lineage_rows = [dict(row) for row in csv.DictReader(handle)]
    except OSError as exc:
        return [f"financial lineage sidecar unreadable: {exc}"]
    return financial_lineage_sidecar_alignment_errors(
        rank_rows,
        lineage_rows,
        expected_asof=iso_date,
    )


def _financial_lineage_artifact_for_date(
    sector: Sector,
    iso_date: str,
    *,
    published_artifact: Path,
) -> Path:
    template = str(sector.financial_lineage_artifact or "").strip()
    if not template:
        return published_artifact
    if "{date}" not in template:
        raise ValueError(f"sector {sector.name}: financial_lineage_artifact must contain {{date}}")
    rendered = template.replace("{date}", _publish_folder_name(sector, iso_date))
    path = Path(rendered)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def verify_published_artifact_for_date(
    sector: Sector,
    iso_date: str,
    *,
    verify_manifest: bool = True,
    policy_context: str = "production",
) -> tuple[bool, list[str]]:
    """Strict post-run acceptance for one published date (findings 3 & 4):
    artifact exists at that date's folder AND has rows AND (oos/gate-valid rows where
    required) AND the internal as-of column (where present) equals the folder date AND
    the health manifest (where configured) is PASS with a date cross-check.

    Date cross-check (finding 4, fail-closed): a manifest with a NON-EMPTY asof must equal
    the folder date. A manifest whose asof is EMPTY/absent is NOT a date-verifying manifest
    (e.g. the technology refresh manifests stamp asof:""), so the artifact's own internal
    date column MUST verify the date instead; if neither the manifest nor the artifact can
    tie the content to the folder date, the date is unverified and the run fails closed.
    """
    reasons: list[str] = []
    parent, filename = publish_dir_root(sector)
    artifact = parent / _publish_folder_name(sector, iso_date) / filename
    if not artifact.exists():
        return False, [f"{iso_date}: artifact missing: {artifact}"]
    if _is_json_artifact(artifact):
        # JSON artifact (portfolio final_manifest.json): parse it -- never certify
        # JSON through CSV heuristics. Its own run_as_of ties the content to the
        # folder date; a compact serialization must not change the verdict.
        data = _load_json_artifact(artifact)
        if data is None:
            reasons.append(f"artifact unreadable/invalid JSON: {artifact}")
            date_checked, date_ok, date_detail = False, False, "unreadable"
        else:
            if not data:
                reasons.append(f"artifact JSON is empty: {artifact}")
            internal_asof = _manifest_asof(data) if isinstance(data, dict) else ""
            date_checked = bool(internal_asof)
            date_ok = internal_asof == iso_date
            date_detail = f"json asof={internal_asof or 'absent'}"
            if date_checked and not date_ok:
                reasons.append(f"internal as-of mismatch: {date_detail} != folder {iso_date}")
    else:
        valid, total = _count_oos_valid(artifact, sector)
        if total <= 0:
            reasons.append(f"artifact has 0 rows: {artifact}")
        if _oos_required(sector, policy_context=policy_context) and valid <= 0:
            reasons.append("no oos/gate-valid rows where required")
        date_checked, date_ok, date_detail = _csv_date_column_matches(artifact, iso_date)
        if date_checked and not date_ok:
            reasons.append(f"internal as-of column mismatch: {date_detail}")
        lineage_policy_mode = policy_for_model_family(sector.name).mode_for_asof(
            policy_context,
            iso_date,
        )
        if lineage_policy_mode != POLICY_DISABLED:
            lineage_artifact = _financial_lineage_artifact_for_date(
                sector,
                iso_date,
                published_artifact=artifact,
            )
            if not lineage_artifact.is_file():
                reasons.append(f"financial lineage artifact missing: {lineage_artifact}")
            else:
                if lineage_artifact != artifact:
                    reasons.extend(
                        _financial_lineage_sidecar_errors(
                            artifact,
                            lineage_artifact,
                            iso_date,
                        )
                    )
                reasons.extend(
                    _financial_lineage_errors(
                        lineage_artifact,
                        iso_date,
                        policy_mode=lineage_policy_mode,
                        min_core_metric_count=(sector.financial_lineage_min_core_metric_count),
                    )
                )
    if verify_manifest and sector.health.manifest:
        status, asof = read_manifest(sector, iso_date)
        if status != "PASS":
            reasons.append(f"manifest status={status}")
        elif asof:
            if asof != iso_date:
                reasons.append(f"manifest asof={asof} != {iso_date}")
        elif not date_checked:
            # Non-date-verifying manifest AND no internal date column: nothing ties the
            # content to the folder date -> fail closed (finding 4).
            reasons.append("manifest asof empty/absent and artifact has no internal date column (date unverified)")
    return (not reasons), reasons


def verify_published_artifact(sector: Sector, target: str) -> tuple[bool, list[str]]:
    """Strict post-run acceptance for the EXACT target date (daily mode)."""
    return verify_published_artifact_for_date(sector, target)


def clear_resolved_auto_gap_records(
    sector: Sector,
    target: str,
    *,
    markers_path: Path = GAP_MARKER_PATH,
) -> list[str]:
    """Remove obsolete automatic tombstones after their artifacts verify again.

    Operator markers are governance decisions and are never changed here. A
    verification exception also preserves the marker, so cleanup cannot turn an
    unreadable artifact into an accepted date.
    """
    resolved: list[str] = []
    with _GAP_MARKER_LOCK:
        markers = load_gap_markers(markers_path)
        per_sector = markers.get("sectors", {}).get(sector.name)
        if not isinstance(per_sector, dict):
            return resolved
        for iso_date, record in list(per_sector.items()):
            if (
                not isinstance(record, dict)
                or str(record.get("source") or "") != "auto"
                or str(iso_date) > target
            ):
                continue
            try:
                parse_iso(str(iso_date))
                artifact_ok, _ = verify_published_artifact_for_date(
                    sector,
                    str(iso_date),
                    verify_manifest=False,
                    policy_context="historical",
                )
            except (OSError, ValueError, csv.Error):
                artifact_ok = False
            if artifact_ok:
                clear_gap_record(markers, sector.name, str(iso_date))
                resolved.append(str(iso_date))
        if resolved:
            save_gap_markers(markers, markers_path)
    return sorted(resolved)


def health_check(reg: Registry, sectors: list[Sector], target: str) -> dict[str, Any]:
    """Reproduce portfolio Stage-1 readiness: per required Tier-0 sector, a fresh
    oos-valid published table within staleness tolerance of `target` AND (when a
    health manifest is configured) a PASS manifest. Manifest status is wired into
    readiness so a missing/failed manifest fails the sector (findings 3, 10, 11)."""
    rows: list[dict[str, Any]] = []
    required_ok = True
    for sector in sectors:
        last = last_published_date(sector, target)
        parent, filename = publish_dir_root(sector)
        artifact = (parent / _publish_folder_name(sector, last) / filename) if last else None
        oos_valid, total = _count_oos_valid(artifact, sector) if artifact else (0, 0)
        man_status, man_asof = read_manifest(sector, last)
        if last is None:
            status = "MISSING"
            stale_days: int | None = None
        else:
            stale_days = (_to_date(target) - _to_date(last)).days
            fresh = stale_days <= sector.staleness_tolerance_days
            rows_ok = total > 0
            oos_ok = (not _oos_required(sector)) or oos_valid > 0
            artifact_ok, artifact_reasons = verify_published_artifact_for_date(sector, last)
            manifest_ok = (not sector.health.manifest) or man_status == "PASS"
            if not rows_ok:
                status = "NO_ROWS"
            elif not fresh:
                status = "STALE"
            elif not oos_ok:
                status = "NO_OOS"
            elif not manifest_ok:
                status = "MANIFEST_FAIL"
            elif not artifact_ok:
                status = "ARTIFACT_FAIL"
            else:
                status = "PASS"
        lineage_policy_mode = (
            policy_for_model_family(sector.name).mode_for_asof("production", last) if last else POLICY_DISABLED
        )
        lineage_artifact_path = (
            str(
                _financial_lineage_artifact_for_date(
                    sector,
                    last,
                    published_artifact=artifact,
                )
            )
            if (last and artifact is not None and lineage_policy_mode != POLICY_DISABLED)
            else ""
        )
        rows.append(
            {
                "sector": sector.name,
                "required": sector.required,
                "tier": sector.dependency_tier,
                "last_published": last or "",
                "staleness_days": stale_days if stale_days is not None else "",
                "tolerance": sector.staleness_tolerance_days,
                "oos_valid_rows": oos_valid,
                "total_rows": total,
                "manifest_status": man_status,
                "manifest_asof": man_asof,
                "artifact_reasons": artifact_reasons if last else [],
                "financial_lineage_policy": lineage_policy_mode,
                "financial_lineage_artifact": lineage_artifact_path,
                "status": status,
            }
        )
        if sector.required and sector.dependency_tier == 0 and status != "PASS":
            required_ok = False
    # stage1_ready is a full-book claim: it may only be asserted when EVERY required
    # Tier-0 sector was actually evaluated. Under --only-sectors/--skip-sectors that
    # excludes a required sector, we can only report subset_ready and must leave
    # stage1_ready null with an explicit note (finding 8).
    evaluated = {s.name for s in sectors}
    required_tier0 = [s.name for s in reg.sectors if s.dependency_tier == 0 and s.required]
    not_evaluated = [name for name in required_tier0 if name not in evaluated]
    report: dict[str, Any] = {"target": target, "subset_ready": required_ok, "sectors": rows}
    if not_evaluated:
        report["stage1_ready"] = None
        report["note"] = (
            f"partial health-check: {len(not_evaluated)} required Tier-0 sector(s) not evaluated "
            f"({', '.join(not_evaluated)}); stage1_ready is null (cannot assert full-book readiness). "
            f"subset_ready={required_ok} reflects only the evaluated sectors."
        )
    else:
        report["stage1_ready"] = required_ok
    return report


# --------------------------------------------------------------------------- #
# Freshness sentinel (surveillance-only; see module docstring)
# --------------------------------------------------------------------------- #
PROBE_TIMEOUT_SEC = 10.0


def _call_with_timeout(fn, timeout_sec: float):
    """Run fn() on a daemon thread with a hard wall-clock timeout.

    sqlite/file reads cannot be interrupted portably; a daemon thread lets the
    orchestrator abandon a hung probe (status ERROR) without hanging the run.
    """
    out: list[Any] = []
    err: list[BaseException] = []

    def _worker() -> None:
        try:
            out.append(fn())
        except BaseException as exc:  # re-raised on the caller thread below
            err.append(exc)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout_sec)
    if worker.is_alive():
        raise TimeoutError(f"probe timed out after {timeout_sec}s")
    if err:
        raise err[0]
    return out[0] if out else None


def _sqlite_scalar_ro(db: str, sql: str) -> Any:
    """One scalar from a sqlite DB opened STRICTLY read-only (mode=ro URI)."""
    db_path = Path(db)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db
    if not db_path.is_file():
        raise FileNotFoundError(f"sqlite db not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(sql).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _manifest_date_value(path_raw: str, key: str) -> Any:
    path = Path(path_raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path_raw
    data = json.loads(path.read_text(encoding="utf-8"))
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"manifest key {key!r} missing at {part!r} in {path}")
        node = node[part]
    return node


def _coerce_probe_date(value: Any, *, context: str) -> date:
    text = str(value if value is not None else "").strip()[:10]
    if not text:
        raise ValueError(f"{context}: no date value returned")
    return datetime.strptime(text, "%Y-%m-%d").date()


def _month_end_of(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _period_ends_desc(cadence: str, upto: date, count: int) -> list[date]:
    """Most recent `count` schedule period ends at/before `upto`, newest first.

    quarterly    -> calendar quarter ends (last day of Mar/Jun/Sep/Dec), e.g. 13F.
    semi_monthly -> bi-monthly-per-month cycles: the 15th and the last day of each
                    month (FINRA short-interest settlement schedule).
    """
    out: list[date] = []
    year, month = upto.year, upto.month
    while len(out) < count:
        if cadence == "quarterly":
            candidates = [_month_end_of(year, month)] if month in (3, 6, 9, 12) else []
        else:
            candidates = [_month_end_of(year, month), date(year, month, 15)]
        for cand in sorted(candidates, reverse=True):
            if cand <= upto and len(out) < count:
                out.append(cand)
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out


def _probe_raw_value(probe: FreshnessProbe) -> Any:
    if probe.kind in {"sqlite_max_date", "deadline_schedule"}:
        return _sqlite_scalar_ro(str(probe.target["db"]), str(probe.target["sql"]))
    return _manifest_date_value(str(probe.target["path"]), str(probe.target["key"]))


def evaluate_freshness_probe(probe: FreshnessProbe, target: str) -> dict[str, Any]:
    """Evaluate one probe against the run's target date. Never raises: any
    failure (missing db/manifest, bad SQL/key, NULL date, timeout) is status
    ERROR with the exception recorded in `detail`."""
    row: dict[str, Any] = {
        "probe": probe.name,
        "kind": probe.kind,
        "required": probe.required,
        "latest": "",
        "age_days": None,
        "threshold": probe.tolerance_days,
        "status": "ERROR",
        "detail": "",
    }
    target_d = _to_date(target)
    try:
        raw = _call_with_timeout(lambda: _probe_raw_value(probe), PROBE_TIMEOUT_SEC)
        latest_d = _coerce_probe_date(raw, context=f"probe {probe.name}")
    except Exception as exc:
        row["detail"] = f"{type(exc).__name__}: {exc}"
        return row
    row["latest"] = latest_d.isoformat()
    row["age_days"] = (target_d - latest_d).days
    if probe.kind == "deadline_schedule":
        total_lag = (
            int(probe.target.get("deadline_days", 0))
            + int(probe.target.get("publication_lag_days", 0))
            + probe.tolerance_days
        )
        cadence = str(probe.target["cadence"])
        # Generate slightly past the target so a period whose availability date falls
        # inside the warn window is visible even before its period end has lag applied.
        ends = _period_ends_desc(cadence, target_d + timedelta(days=probe.warn_lead_days), 16)
        required = next((e for e in ends if e + timedelta(days=total_lag) <= target_d), None)
        row["threshold"] = f"period>={required.isoformat()}" if required else "no_period_required_yet"
        if required is not None and latest_d < required:
            row["status"] = "STALE"
        else:
            upcoming = [e for e in reversed(ends) if e > latest_d]  # ascending
            if upcoming and (upcoming[0] + timedelta(days=total_lag) - target_d).days <= probe.warn_lead_days:
                row["status"] = "WARN_APPROACHING"
            else:
                row["status"] = "CURRENT"
        return row
    age_days = int(row["age_days"])
    if age_days > probe.tolerance_days:
        row["status"] = "STALE"
    elif age_days + probe.warn_lead_days > probe.tolerance_days:
        row["status"] = "WARN_APPROACHING"
    else:
        row["status"] = "CURRENT"
    return row


def evaluate_freshness(sectors: list[Sector], target: str, log) -> dict[str, list[dict[str, Any]]]:
    """Evaluate every registered probe (cheap, read-only). STALE/ERROR probes are
    logged loudly; one summary line covers the whole sweep."""
    freshness: dict[str, list[dict[str, Any]]] = {}
    for sector in sectors:
        if not sector.freshness_probes:
            continue
        rows = [evaluate_freshness_probe(probe, target) for probe in sector.freshness_probes]
        freshness[sector.name] = rows
        for row in rows:
            if row["status"] == "STALE":
                log(
                    f"FRESHNESS ERROR: [{sector.name}] probe={row['probe']} STALE "
                    f"latest={row['latest'] or 'none'} age_days={row['age_days']} "
                    f"threshold={row['threshold']}{' (required)' if row['required'] else ''}"
                )
            elif row["status"] == "ERROR":
                log(f"FRESHNESS ERROR: [{sector.name}] probe={row['probe']} ERROR: {row['detail']}")
    if freshness:
        summary = "; ".join(
            f"{name} " + ",".join(f"{r['probe']}={r['status']}" for r in rows) for name, rows in freshness.items()
        )
        log(f"freshness: {summary}")
    return freshness


def apply_freshness_consequences(
    freshness: dict[str, list[dict[str, Any]]],
    results: dict[str, RunResult],
    overall: str,
    log,
    *,
    selected_names: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Probes are surveillance, not gates: they never block publication by default.
    The single consequence with teeth: a probe with required:true whose status is
    STALE **or ERROR** marks the sector with a visible FRESHNESS_BLOCKING note and
    forces the master acceptance to FAIL (sectors have already run at this point).
    ERROR blocks because a required probe that cannot even be evaluated (missing
    DB, renamed table, timeout) would otherwise be silently non-blocking forever,
    defeating the fail-closed intent. Blocking is scoped to `selected_names` so a
    required-stale probe on a sector excluded from this run cannot flip the
    acceptance of a run that never touched it (it is still logged loudly)."""
    blocking_required: list[str] = []
    for name, rows in sorted(freshness.items()):
        blocking = [
            (r["probe"], r.get("status")) for r in rows if r.get("required") and r.get("status") in {"STALE", "ERROR"}
        ]
        if not blocking:
            continue
        rendered = [f"{probe}={status}" for probe, status in blocking]
        if selected_names is not None and name not in selected_names:
            log(
                f"FRESHNESS ERROR: [{name}] required probe(s) {rendered} blocking, but the "
                f"sector is not in this run's selection -> surveillance only for this run"
            )
            continue
        blocking_required.extend(f"{name}:{item}" for item in rendered)
        res = results.get(name)
        if res is not None:
            res.note = (res.note + "; " if res.note else "") + f"FRESHNESS_BLOCKING: {','.join(rendered)}"
    if blocking_required and overall == "PASS":
        overall = "FAIL"
        log(f"FRESHNESS ERROR: required freshness probe(s) STALE/ERROR -> master acceptance FAIL: {blocking_required}")
    return overall, blocking_required


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    sector: str
    # PASS | PASS_WITH_BACKFILL_GAPS | FAIL | OPTIONAL_FAIL | DRY_RUN
    #   | SKIPPED_RESUME | SKIPPED_GATE | UP_TO_DATE | NOTE | UNKNOWN | RUNNING
    status: str = "PENDING"
    commands: list[list[str]] = field(default_factory=list)
    return_codes: list[int] = field(default_factory=list)
    elapsed_sec: float = 0.0
    artifact: str = ""
    sha256: str = ""
    command_hash: str = ""
    content_hash: str = ""
    note: str = ""
    # Catch-up per-date backfill accounting (attempted/failed/historical/permanent);
    # None outside catch-up mode.
    backfill: dict[str, Any] | None = None


def run_commands(
    sector: Sector,
    commands: list[list[str]],
    *,
    run_dir: Path,
    net_sem: threading.Semaphore | None,
    dry_run: bool,
    empty_status: str,
    log,
) -> RunResult:
    result = RunResult(sector=sector.name, commands=commands)
    if dry_run:
        result.status = "DRY_RUN"
        return result
    if not commands:
        result.status = empty_status
        return result
    started = time.perf_counter()
    log_path = run_dir / "logs" / f"{sector.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    overall_ok = True
    with log_path.open("w", encoding="utf-8", newline="") as logfile:
        for command in commands:
            rc = _run_one(sector, command, net_sem=net_sem, logfile=logfile)
            result.return_codes.append(rc)
            if rc != 0:
                overall_ok = False
                break
    result.elapsed_sec = round(time.perf_counter() - started, 2)
    result.status = "PASS" if overall_ok else "FAIL"
    log(f"  [{sector.name}] {result.status} rcs={result.return_codes} elapsed={result.elapsed_sec}s")
    return result


def run_catch_up_sector(
    reg: Registry,
    sector: Sector,
    plan: CatchUpPlan,
    *,
    run_dir: Path,
    net_sem: threading.Semaphore | None,
    log,
    markers_path: Path = GAP_MARKER_PATH,
    runner=None,
) -> RunResult:
    """Execute strict prerequisites, best-effort history, then the current target.

    Acceptance is the CURRENT date alone: a current-date execution/verification
    failure fails the sector exactly like daily mode; failed backfill dates are
    recorded per-date (status PASS_WITH_BACKFILL_GAPS) and never fail the master.
    Verification runs IMMEDIATELY after each date's commands so a sector whose
    health manifest is a global latest-run file is checked while that manifest
    still describes the date just built. The final current-target rebuild leaves
    every global latest-state artifact on the requested target. `runner` is
    injectable for the no-subprocess selftest.
    """
    result = RunResult(sector=sector.name, commands=plan.all_commands, backfill=None)
    started = time.perf_counter()
    log_path = run_dir / "logs" / f"{sector.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    backfill_failed: dict[str, str] = {}
    date_partitioned_manifest = bool(sector.health.manifest and "{date}" in sector.health.manifest)
    with log_path.open("w", encoding="utf-8", newline="") as logfile:

        def _exec(command: list[str]) -> int:
            if runner is not None:
                return int(runner(command))
            return _run_one(sector, command, net_sem=net_sem, logfile=logfile)

        # ---- 1) strict shared prerequisites ------------------------------- #
        prerequisites_ok = True
        for command in plan.pre_commands:
            rc = _exec(command)
            result.return_codes.append(rc)
            if rc != 0:
                prerequisites_ok = False
                break
        if not prerequisites_ok:
            result.status = "FAIL"
            result.note = "catch-up prerequisite failed"

        # ---- 2) historical backfill, best-effort, oldest-first ------------ #
        embedded_target_failed = False
        if prerequisites_ok and plan.backfill_groups:
            for dates, cmds in plan.backfill_groups:
                group_rc = 0
                for command in cmds:
                    group_rc = _exec(command)
                    result.return_codes.append(group_rc)
                    if group_rc != 0:
                        break
                if group_rc != 0:
                    for iso_date in dates:
                        backfill_failed[iso_date] = f"rc={group_rc}"
                    if plan.native_backfill_covers_target:
                        embedded_target_failed = True
                        break
                    continue
                for iso_date in dates:
                    # A native multi-date backfill writes a global manifest only
                    # once, so only date-partitioned manifests can be checked for
                    # each member of such a chunk.
                    verify_backfill_manifest = bool(
                        sector.historical_health_manifest_required
                        and ((not plan.used_native_backfill) or date_partitioned_manifest)
                    )
                    ok_d, reasons_d = verify_published_artifact_for_date(
                        sector,
                        iso_date,
                        verify_manifest=verify_backfill_manifest,
                        policy_context="historical",
                    )
                    if not ok_d:
                        backfill_failed[iso_date] = "verify: " + "; ".join(reasons_d)

        # ---- 3) CURRENT TARGET LAST (strict) ------------------------------ #
        if prerequisites_ok and embedded_target_failed:
            result.status = "FAIL"
            result.note = f"native backfill/current target {plan.target} failed"
        elif prerequisites_ok and plan.target_needs_run:
            target_ok = True
            for command in plan.target_commands:
                rc = _exec(command)
                result.return_codes.append(rc)
                if rc != 0:
                    target_ok = False
                    break
            if not target_ok:
                result.status = "FAIL"
                result.note = f"current target {plan.target} failed"
            else:
                ok, reasons = verify_published_artifact_for_date(sector, plan.target)
                if ok:
                    result.status = "PASS"
                else:
                    result.status = "FAIL"
                    result.note = "artifact_verify_failed: " + "; ".join(reasons)
        elif prerequisites_ok:
            # With no backfill groups, an already-current target needs only a
            # strict artifact verification.
            ok, reasons = verify_published_artifact_for_date(sector, plan.target)
            if ok:
                result.status = "UP_TO_DATE"
            else:
                result.status = "FAIL"
                result.note = f"target {plan.target} published but unverifiable: " + "; ".join(reasons)
    # ---- 4) marker bookkeeping + status ---------------------------------- #
    if plan.backfill_dates:
        with _GAP_MARKER_LOCK:
            markers = load_gap_markers(markers_path)
            for iso_date, reason in backfill_failed.items():
                record_gap_failure(
                    markers,
                    sector.name,
                    iso_date,
                    reason,
                    auto_permanent_after=reg.permanent_gap_after_failures,
                )
            for iso_date in plan.backfill_dates:
                if iso_date not in backfill_failed:
                    clear_gap_record(markers, sector.name, iso_date)
            save_gap_markers(markers, markers_path)
    resolved_markers = clear_resolved_auto_gap_records(
        sector,
        plan.target,
        markers_path=markers_path,
    )
    if resolved_markers:
        log(
            f"  [{sector.name}] cleared resolved automatic gap markers: "
            + ",".join(resolved_markers)
        )
    if result.status in {"PASS", "UP_TO_DATE"}:
        if backfill_failed:
            result.status = "PASS_WITH_BACKFILL_GAPS"
            result.note = (result.note + "; " if result.note else "") + (
                "backfill_gaps=" + ",".join(sorted(backfill_failed))
            )
        elif result.status == "UP_TO_DATE" and plan.backfill_groups:
            result.status = "PASS"  # backfill work executed and verified
    result.backfill = {
        "attempted": list(plan.backfill_dates),
        "failed": dict(sorted(backfill_failed.items())),
        "historical_gaps": list(plan.historical_gaps),
        "permanent_gaps": list(plan.permanent_gaps),
        "native_backfill": plan.used_native_backfill,
    }
    result.elapsed_sec = round(time.perf_counter() - started, 2)
    log(
        f"  [{sector.name}] {result.status} rcs={result.return_codes} elapsed={result.elapsed_sec}s ({plan_note(plan)})"
    )
    return result


def _should_retry_rc(rc: int) -> bool:
    """Return false for success, timeout, and deterministic policy failures."""
    return rc not in (0, 78, 124)


def _is_sector_entry_command(sector: Sector, command: list[str]) -> bool:
    entry = sector.entry_script.replace("\\", "/").lower()
    return any(str(part).replace("\\", "/").lower().endswith(entry) for part in command)


def _command_for_attempt(sector: Sector, command: list[str], attempt: int) -> list[str]:
    if attempt <= 1 or not sector.retry_args:
        return list(command)
    if not _is_sector_entry_command(sector, command):
        return list(command)
    width = len(sector.retry_args)
    if any(command[idx : idx + width] == sector.retry_args for idx in range(len(command) - width + 1)):
        return list(command)
    return [*command, *sector.retry_args]


def _commands_for_master_resume(sector: Sector, commands: list[list[str]]) -> list[list[str]]:
    """Apply native resume args only to the sector entry command."""
    resumed: list[list[str]] = []
    for command in commands:
        resumed.append(_command_for_attempt(sector, command, 2))
    return resumed


def _run_one(sector: Sector, command: list[str], *, net_sem: threading.Semaphore | None, logfile) -> int:
    attempts = sector.retries + 1
    rc = 1
    for attempt in range(1, attempts + 1):
        attempt_command = _command_for_attempt(sector, command, attempt)
        acquired = False
        if sector.network and net_sem is not None:
            net_sem.acquire()
            acquired = True
        proc: subprocess.Popen[bytes] | None = None
        tracker = _ACTIVE_ORCHESTRATION_LOCK
        try:
            logfile.write(f"\n=== attempt {attempt}/{attempts}: {subprocess.list2cmdline(attempt_command)}\n")
            logfile.flush()
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            child_env = os.environ.copy()
            # Portfolio Tier-1 uses the same global lock as this master. Pass a
            # verifiable owner token so the child may borrow the master's lock;
            # direct portfolio invocations without this token must acquire the
            # lock themselves and cannot race a sector refresh.
            child_env["STAGING_ORCHESTRATOR_PID"] = str(os.getpid())
            proc = subprocess.Popen(
                attempt_command,
                cwd=str(PROJECT_ROOT),
                stdout=logfile,
                stderr=subprocess.STDOUT,
                env=child_env,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            if tracker is not None:
                tracker.register_child(proc.pid)
            rc = proc.wait(timeout=sector.timeout_sec)
        except subprocess.TimeoutExpired:
            logfile.write(f"\n=== TIMEOUT after {sector.timeout_sec}s\n")
            if proc is not None:
                _terminate_process_tree(proc, logfile)
            rc = 124
        except BaseException:
            if proc is not None and proc.poll() is None:
                _terminate_process_tree(proc, logfile)
            raise
        finally:
            if proc is not None and tracker is not None:
                tracker.unregister_child(proc.pid)
            if acquired and net_sem is not None:
                net_sem.release()
        if not _should_retry_rc(rc):
            return rc
    return rc


def _terminate_process_tree(proc: subprocess.Popen[bytes], logfile) -> None:
    """Terminate a timed-out/interrupted command and every descendant it spawned."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 and proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            proc.kill()
    logfile.write(f"=== terminated process tree pid={proc.pid}\n")
    logfile.flush()


def plan_lanes(reg: Registry, selected: list[Sector]) -> list[list[Sector]]:
    """Group selected Tier-0 sectors into serialized lanes by db_group, ordered by group_order."""
    by_group: dict[str, list[Sector]] = {}
    for sector in selected:
        if sector.dependency_tier != 0:
            continue
        by_group.setdefault(sector.db_group, []).append(sector)
    lanes: list[list[Sector]] = []
    for group, members in by_group.items():
        order = reg.group_order.get(group, [s.name for s in members])
        members.sort(key=lambda s: order.index(s.name) if s.name in order else 99)
        lanes.append(members)
    return lanes


def select_sectors(reg: Registry, only: list[str], skip: list[str]) -> list[Sector]:
    names = only if only else reg.names
    unknown = [n for n in (only + skip) if n not in reg.names]
    if unknown:
        raise ValueError(f"unknown sector names: {unknown}; valid={reg.names}")
    selected = [reg.by_name(n) for n in names if n not in skip]
    if not selected:
        raise ValueError("selection is empty after applying --only-sectors/--skip-sectors")
    return selected


def parse_repair_arg(reg: Registry, raw: str) -> dict[str, list[str]]:
    """'biotech:ib_market,med_devices' -> {biotech:[ib_market], med_devices:[<all source steps>]}."""
    out: dict[str, list[str]] = {}
    for token in [t.strip() for t in raw.split(",") if t.strip()]:
        if ":" in token:
            name, step = token.split(":", 1)
            name, step = name.strip(), step.strip()
        else:
            name, step = token, ""
        if name not in reg.names:
            raise ValueError(f"unknown repair sector: {name}")
        sector = reg.by_name(name)
        if sector.repair is None:
            raise ValueError(f"sector {name} has no repair steps defined")
        if step:
            if not sector.repair.selection_flag:
                raise ValueError(
                    f"sector {name} does not support selecting an individual repair step; "
                    f"use --repair {name!s} without a suffix"
                )
            if step not in sector.repair.steps:
                raise ValueError(f"unknown repair step {step!r} for {name}; valid={sector.repair.steps}")
            out.setdefault(name, [])
            if step not in out[name]:
                out[name].append(step)
        else:
            out[name] = list(sector.repair.steps)
    return out


# --------------------------------------------------------------------------- #
# Master manifest / resume
# --------------------------------------------------------------------------- #
def write_master_manifest(run_dir: Path, payload: dict[str, Any]) -> Path:
    path = run_dir / "master_manifest.json"
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _result_rows(
    results: dict[str, RunResult], freshness: dict[str, list[dict[str, Any]]] | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in results.values():
        row: dict[str, Any] = {
            "sector": r.sector,
            "status": r.status,
            "note": r.note,
            "elapsed_sec": r.elapsed_sec,
            "return_codes": r.return_codes,
            "artifact": r.artifact,
            "sha256": r.sha256,
            "command_hash": r.command_hash,
            "content_hash": r.content_hash,
            "commands": [subprocess.list2cmdline(c) for c in r.commands],
        }
        if r.backfill is not None:
            row["backfill"] = r.backfill
        probes = (freshness or {}).get(r.sector)
        if probes is not None:
            row["freshness"] = probes
        rows.append(row)
    return rows


def load_resume_records(reg: Registry) -> list[dict[str, str]]:
    """Per-sector PASS records from LIVE master manifests only (dry-run manifests live
    under DRYRUN_RUNS_ROOT and are never consulted)."""
    records: list[dict[str, str]] = []
    if not RUNS_ROOT.is_dir():
        return records
    for manifest in sorted(RUNS_ROOT.glob("*/master_manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("dry_run"):
            continue
        top_target = str(data.get("target") or "")
        top_mode = str(data.get("mode") or "")
        for row in data.get("sectors", []):
            records.append(
                {
                    "sector": str(row.get("sector") or ""),
                    "status": str(row.get("status") or ""),
                    "target": top_target,
                    "mode": top_mode,
                    "command_hash": str(row.get("command_hash") or ""),
                    "content_hash": str(row.get("content_hash") or ""),
                    "sha256": str(row.get("sha256") or ""),
                    "artifact": str(row.get("artifact") or ""),
                }
            )
    return records


def reconcile_abandoned_runs() -> list[Path]:
    """Fail-close RUNNING manifests whose recorded master process is gone.

    A forced host/tool shutdown can bypass Python's `finally`. Leaving such a manifest
    at RUNNING forever is operationally misleading and obscures the last completed
    verdict. The run stamp carries the master PID; only a provably dead PID is amended.
    """
    reconciled: list[Path] = []
    if not RUNS_ROOT.is_dir():
        return reconciled
    for manifest in sorted(RUNS_ROOT.glob("*/master_manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("acceptance") or "").upper() != "RUNNING":
            continue
        run_stamp = str(data.get("run_stamp") or manifest.parent.name)
        match = re.search(r"_p(\d+)$", run_stamp)
        pid = int(match.group(1)) if match else None
        # The stamp encodes the master's UTC start time; with it, a recycled PID
        # (created after the run started) cannot keep the manifest RUNNING forever.
        stamp_match = re.match(r"^(\d{8}T\d{6}_\d+)Z", run_stamp)
        started_iso = ""
        if stamp_match:
            try:
                started_iso = (
                    datetime.strptime(stamp_match.group(1), "%Y%m%dT%H%M%S_%f").replace(tzinfo=timezone.utc).isoformat()
                )
            except ValueError:
                started_iso = ""
        if _holder_alive(pid, started_iso) is not False:
            continue
        data["acceptance"] = "ABORTED"
        data["tier0_gate"] = "FAIL"
        data["aborted_reason"] = f"master process pid={pid} is no longer alive; prior run did not seal"
        data["reconciled_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_master_manifest(manifest.parent, data)
        reconciled.append(manifest)
    return reconciled


def resume_match(
    records: list[dict[str, str]], sector: Sector, target: str, mode: str, cmd_hash: str, content_hash: str
) -> bool:
    """Skip only on FULL identity (finding 5): a prior PASS whose (target, mode, sector,
    command-hash, script+registry content-hash) all match AND whose recorded artifact still
    exists at the exact date with a byte-identical SHA-256. A changed script/registry
    (content-hash) or a modified/replaced artifact (SHA mismatch) forces a re-run."""
    # A catch-up/backfill/repair can touch many dated artifacts, while historical master
    # manifests record only the target artifact SHA. Until the manifest stores the full
    # touched-date hash set, resuming those modes would be false assurance. Daily has a
    # single artifact and can be verified completely.
    if mode != "daily":
        return False
    for rec in records:
        if (
            rec["sector"] == sector.name
            and rec["status"] == "PASS"
            and rec["target"] == target
            and rec["mode"] == mode
            and rec["command_hash"] == cmd_hash
            and rec["command_hash"]
            and rec.get("content_hash") == content_hash
            and rec.get("content_hash")
        ):
            artifact = rec["artifact"]
            stored_sha = rec.get("sha256") or ""
            if artifact and stored_sha and Path(artifact).exists() and sha256_file(Path(artifact)) == stored_sha:
                artifact_ok, _ = verify_published_artifact_for_date(sector, target)
                if artifact_ok:
                    return True
    return False


# --------------------------------------------------------------------------- #
# Orchestration driver
# --------------------------------------------------------------------------- #
def build_sector_commands(
    reg: Registry, sector: Sector, args: argparse.Namespace, target: str, repair_map: dict[str, list[str]]
) -> tuple[list[list[str]], str]:
    """Return (commands, note) for one sector under the resolved mode."""
    if args.mode == "backfill":
        cmds, note = backfill_commands(sector, args.from_date, args.to_date, reg)
        if cmds and sector.daily_post_steps:
            for iso_date in trading_dates_in_range(
                reg,
                args.from_date,
                args.to_date,
            ):
                cmds.extend(daily_post_commands(sector, iso_date, historical=True))
            note = "backfill+post_steps"
        return cmds, note
    if args.mode == "repair":
        steps = repair_map.get(sector.name, [])
        if not steps and sector.repair is not None:
            steps = list(sector.repair.steps)
        dates = last_n_dates(reg, target, args.repair_days)
        if sector.repair and steps != sector.repair.steps:
            sector = _with_repair_steps(sector, steps)
        return repair_commands(sector, dates), ""
    if args.mode == "catch-up":
        plan = build_catch_up_plan(
            reg,
            sector,
            target,
            force=args.force,
            include_weekly_pre=args.cadence == "weekly",
            live_completed_session=getattr(args, "live_session", None),
            catch_up_from=getattr(args, "catch_up_from", None),
        )
        return plan.all_commands, plan_note(plan)
    # daily
    daily_cmds: list[list[str]] = []
    if args.cadence == "weekly":
        daily_cmds += weekly_pre_commands(sector, target)
    daily_cmds.append(
        _catch_up_daily_command(
            sector,
            target,
            force=args.force,
            live_completed_session=(
                getattr(args, "live_session", None) or latest_completed_trading_session()
            ),
        )
    )
    daily_cmds += daily_post_commands(sector, target)  # finding 1: e.g. med runs script 76 after publish
    note = "daily+post_steps" if sector.daily_post_steps else ""
    return daily_cmds, note


def _with_repair_steps(sector: Sector, steps: list[str]) -> Sector:
    assert sector.repair is not None
    new_repair = RepairSpec(
        date_flag=sector.repair.date_flag,
        selection_flag=sector.repair.selection_flag,
        steps=steps,
        rebuild_steps=sector.repair.rebuild_steps,
        extra_args=sector.repair.extra_args,
    )
    return Sector(**{**sector.__dict__, "repair": new_repair})


def last_n_dates(reg: Registry, target: str, n: int) -> list[str]:
    """The most recent `n` calendar trading sessions ending at `target`.

    Pure calendar walk-back (never published-evidence dates): after a multi-day
    publishing outage, published dirs would point --repair at stale pre-outage
    dates instead of the sessions that actually need attention. `reg` is kept in
    the signature for call-site symmetry; the rule-based calendar (plus the
    registry-declared ad-hoc closures already loaded) is authoritative here.
    """
    del reg  # calendar-only by design (see docstring)
    if n < 1:
        raise ValueError(f"last_n_dates requires n >= 1, got {n}")
    out: list[str] = []
    cur = _to_date(target)
    while len(out) < n and cur > _to_date("2000-01-01"):
        if is_trading_day(cur):
            out.append(cur.isoformat())
        cur -= timedelta(days=1)
    return sorted(out)


def finalize_result(
    sector: Sector,
    res: RunResult,
    *,
    target: str,
    mode: str,
    dry_run: bool,
) -> RunResult:
    """Apply strict artifact verification (daily) and optional-sector downgrade.

    Daily verifies the single target date. Catch-up verification happens inline in
    run_catch_up_sector (each backfill immediately, target strictly after all gaps),
    so only the optional-sector downgrade applies here.
    """
    if not dry_run and mode == "daily" and res.status in {"PASS", "UP_TO_DATE"}:
        _, reasons = verify_published_artifact(sector, target)
        if reasons:
            res.status = "FAIL"
            res.note = (res.note + "; " if res.note else "") + "artifact_verify_failed: " + "; ".join(reasons)
    if res.status == "FAIL" and not sector.required:
        res.status = "OPTIONAL_FAIL"
    return res


def _set_artifact(sector: Sector, res: RunResult, target: str, *, dry_run: bool) -> None:
    parent, filename = publish_dir_root(sector)
    artifact = parent / _publish_folder_name(sector, target) / filename
    res.artifact = str(artifact)
    if not dry_run:
        res.sha256 = sha256_file(artifact)


def _unexpected_lane_failure(sector: Sector, exc: Exception) -> RunResult:
    """Convert an escaped lane exception into a fail-visible sector result.

    Command failures normally become ``RunResult`` instances inside ``run_commands``.
    This guard covers orchestration/finalization defects (for example an artifact or
    checkpoint read raising after the child process succeeded).  One broken worker
    must not discard the results already checkpointed by every other lane.
    """
    status = "FAIL" if sector.required else "OPTIONAL_FAIL"
    return RunResult(
        sector=sector.name,
        status=status,
        note=f"unexpected_lane_exception:{type(exc).__name__}:{exc}",
    )


def run_tier0(
    reg: Registry,
    lanes: list[list[Sector]],
    args: argparse.Namespace,
    target: str,
    repair_map: dict[str, list[str]],
    run_dir: Path,
    resume_records: list[dict[str, str]],
    net_sem: threading.Semaphore,
    log,
) -> dict[str, RunResult]:
    results: dict[str, RunResult] = {}
    lock = threading.Lock()

    def record_progress(res: RunResult) -> None:
        """Persist each completed sector while the master is still RUNNING.

        This is the load-bearing crash-resume record. If the master is terminated after
        one lane finishes, a later --resume can recover the exact PASS + artifact SHA
        instead of finding the startup manifest's old empty sector list.
        """
        with lock:
            results[res.sector] = res
            write_master_manifest(
                run_dir,
                {
                    "run_stamp": run_dir.name,
                    "mode": args.mode,
                    "catch_up_from": getattr(args, "catch_up_from", None) or "",
                    "cadence": args.cadence,
                    "target": target,
                    "master_pid": os.getpid(),
                    "dry_run": False,
                    "acceptance": "RUNNING",
                    "tier0_gate": "RUNNING",
                    "sectors": _result_rows(results),
                },
            )

    def run_lane(members: list[Sector]) -> None:
        for sector in members:
            plan: CatchUpPlan | None = None
            if args.mode == "catch-up":
                # Stateful catch-up plan: oldest gap first, current target last;
                # verification happens inline immediately after each date.
                plan = build_catch_up_plan(
                    reg,
                    sector,
                    target,
                    force=args.force,
                    include_weekly_pre=args.cadence == "weekly",
                    live_completed_session=getattr(args, "live_session", None),
                    catch_up_from=getattr(args, "catch_up_from", None),
                )
                commands, note = plan.all_commands, plan_note(plan)
            else:
                commands, note = build_sector_commands(reg, sector, args, target, repair_map)
            cmd_hash = _command_hash(commands)
            content_hash = _content_hash(sector, args.registry)
            resume_matched = False
            if args.resume and not args.dry_run:
                resume_matched = resume_match(
                    resume_records,
                    sector,
                    target,
                    args.mode,
                    cmd_hash,
                    content_hash,
                )
                if not resume_matched:
                    resume_commands = _commands_for_master_resume(sector, commands)
                    resume_cmd_hash = _command_hash(resume_commands)
                    if resume_cmd_hash != cmd_hash:
                        commands = resume_commands
                        cmd_hash = resume_cmd_hash
                        resume_matched = resume_match(
                            resume_records,
                            sector,
                            target,
                            args.mode,
                            cmd_hash,
                            content_hash,
                        )
            if resume_matched:
                res = RunResult(
                    sector=sector.name,
                    status="SKIPPED_RESUME",
                    commands=commands,
                    command_hash=cmd_hash,
                    content_hash=content_hash,
                    note="resume: prior PASS matched target/mode/command/content/artifact-sha",
                )
                _set_artifact(sector, res, target, dry_run=False)
                record_progress(res)
                log(f"  [{sector.name}] SKIPPED_RESUME (prior PASS matched)")
                continue
            if plan is not None and not args.dry_run:
                res = run_catch_up_sector(reg, sector, plan, run_dir=run_dir, net_sem=net_sem, log=log)
            else:
                res = run_commands(
                    sector,
                    commands,
                    run_dir=run_dir,
                    net_sem=net_sem,
                    dry_run=args.dry_run,
                    empty_status="NOTE",
                    log=log,
                )
            res.note = "; ".join(part for part in (note, res.note) if part)
            res.command_hash = cmd_hash
            res.content_hash = content_hash
            _set_artifact(sector, res, target, dry_run=args.dry_run)
            finalize_result(sector, res, target=target, mode=args.mode, dry_run=args.dry_run)
            record_progress(res)

    workers = max(1, len(lanes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_lane, members): members for members in lanes}
        for fut in concurrent.futures.as_completed(futures):
            members = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                names = [sector.name for sector in members]
                log(
                    "  [lane] FAIL unexpected exception "
                    f"members={names} error={type(exc).__name__}:{exc}"
                )
                # Completed members were checkpointed before the exception and must
                # remain authoritative. Mark only the failed/not-started tail.
                for sector in members:
                    with lock:
                        already_recorded = sector.name in results
                    if already_recorded:
                        continue
                    res = _unexpected_lane_failure(sector, exc)
                    parent, filename = publish_dir_root(sector)
                    res.artifact = str(parent / _publish_folder_name(sector, target) / filename)
                    record_progress(res)
    return results


def tier0_gate(
    reg: Registry, results: dict[str, RunResult], *, repair_scope: list[str] | None = None
) -> tuple[bool, list[str]]:
    """Tier-0 gate.

    Full-book modes (daily/catch-up/backfill): gate over EVERY required Tier-0 sector in
    the registry. A required sector excluded from the selection has no result -> counts as
    UNKNOWN/unhealthy so the portfolio cannot run ungated.

    Repair mode (finding 2): a repair is a targeted re-run, NOT a full-book run, so the
    gate evaluates ONLY the repaired sectors' outcomes (`repair_scope`). Non-repaired
    sectors are irrelevant to a repair's success, so their absence must not fail the gate
    and no --ignore-gate is needed.
    """
    failing: list[str] = []
    if repair_scope is not None:
        for name in repair_scope:
            state = results.get(name)
            if state is None or state.status not in HEALTHY_STATES:
                failing.append(name)
        return (not failing), failing
    for sector in reg.sectors:
        if sector.dependency_tier != 0 or not sector.required:
            continue
        state = results.get(sector.name)
        if state is None or state.status not in HEALTHY_STATES:
            failing.append(sector.name)
    return (not failing), failing


def _gate_manifest_value(gate_ok: bool, ignore_gate: bool) -> str:
    """Sealed-manifest tier0_gate value: PASS | BYPASSED | FAIL.

    BYPASSED (gate failed but --ignore-gate deliberately ran the portfolio anyway)
    must survive into the FINAL manifest -- rewriting it to FAIL made 'gate failed,
    portfolio skipped' indistinguishable from 'gate failed, run anyway' for any
    consumer not also joining on the ignore_gate flag.
    """
    if gate_ok:
        return "PASS"
    return "BYPASSED" if ignore_gate else "FAIL"


def compute_overall(
    selected: list[Sector], results: dict[str, RunResult], *, mode: str, gate_ok: bool, ignore_gate: bool
) -> str:
    """Overall acceptance, consistent with the gate (finding 2):
    - a failed/ignored gate that was NOT explicitly bypassed -> FAIL;
    - otherwise every selected required sector must be healthy (backfill NOTE for the
      portfolio is acceptable); optional-sector failures never sink the run (finding 14).
    """
    if not gate_ok and not ignore_gate:
        return "FAIL"
    for sector in selected:
        res = results.get(sector.name)
        if res is None:
            return "FAIL"
        if res.status in HEALTHY_STATES:
            continue
        if not sector.required:
            continue
        if mode == "backfill" and res.status == "NOTE":
            continue
        return "FAIL"
    return "PASS"


def print_matrix(sector_cmds: dict[str, tuple[list[list[str]], str]]) -> None:
    print("\n=== COMMAND MATRIX ===")
    for name, (cmds, note) in sector_cmds.items():
        if not cmds:
            print(f"[{name}] (no commands) {('- ' + note) if note else ''}")
            continue
        for cmd in cmds:
            print(f"[{name}] {subprocess.list2cmdline(cmd)}")
        if note:
            print(f"[{name}]   note: {note}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Master cross-sector orchestrator.")
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--mode", choices=["daily", "backfill", "health-check"], default="daily")
    p.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Target trading date YYYY-MM-DD (default: latest COMPLETED trading session, close-aware).",
    )
    p.add_argument(
        "--catch-up", action="store_true", help="Run every missing trading date per sector through the target."
    )
    p.add_argument(
        "--catch-up-from",
        default="",
        help="Operator override: in catch-up mode, fill missing sessions no earlier than YYYY-MM-DD.",
    )
    p.add_argument(
        "--repair",
        default="",
        help="Repair spec: 'sector[:step],sector2,...' (re-run IB/network steps + rebuild dependents).",
    )
    p.add_argument(
        "--repair-days",
        type=int,
        default=None,
        help="Trailing dates a --repair pass re-runs (>=1; default from registry).",
    )
    p.add_argument("--from", dest="from_date", default="", help="Backfill start date YYYY-MM-DD.")
    p.add_argument("--to", dest="to_date", default="", help="Backfill end date YYYY-MM-DD.")
    p.add_argument(
        "--cadence",
        choices=["daily", "weekly"],
        default="daily",
        help="weekly runs each sector's weekly_pre_steps before the daily publish (none by default).",
    )
    p.add_argument("--only-sectors", default="", help="Comma-separated registry sector names to include.")
    p.add_argument("--skip-sectors", default="", help="Comma-separated registry sector names to exclude.")
    p.add_argument("--force", action="store_true", help="Forward each sector's force flag.")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip sectors whose prior PASS matches (target, mode, command-hash) with a live artifact.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command matrix; execute nothing (manifest isolated under runs_dryrun/).",
    )
    p.add_argument(
        "--ignore-gate",
        action="store_true",
        help="Run portfolio even if the Tier-0 gate fails (required for partial runs that exclude required sectors).",
    )
    p.add_argument(
        "--lock-stale-sec",
        type=int,
        default=DEFAULT_LOCK_STALE_SEC,
        help="Override a global orchestration lock older than this many seconds.",
    )
    p.add_argument(
        "--mark-gap",
        default="",
        metavar="SECTOR:DATE[:REASON]",
        help="Mark one backfill date as a permanent gap (tombstone) and exit; catch-up will stop retrying it.",
    )
    p.add_argument(
        "--clear-gap", default="", metavar="SECTOR:DATE", help="Clear a permanent-gap marker / failure record and exit."
    )
    p.add_argument(
        "--selftest",
        action="store_true",
        help="In-process validation of registry/date-math/scheduling/repair; no subprocess.",
    )
    return p.parse_args(argv)


def resolve_mode(args: argparse.Namespace) -> None:
    """Resolve the effective mode and reject conflicting mode flags (finding 14).

    --mode health-check participates in the conflict check: silently rewriting
    'health-check --catch-up' into an EXECUTING catch-up run would launch real
    sector subprocesses under a read-only intent.
    """
    explicit_backfill = args.mode == "backfill"
    explicit_health = args.mode == "health-check"
    conflicts = [
        name
        for name, on in (
            ("--repair", bool(args.repair)),
            ("--catch-up", args.catch_up),
            ("--mode backfill", explicit_backfill),
            ("--mode health-check", explicit_health),
        )
        if on
    ]
    if len(conflicts) > 1:
        raise SystemExit(
            f"conflicting mode flags: {conflicts}; choose exactly one of "
            f"--repair / --catch-up / --mode backfill / --mode health-check"
        )
    if args.cadence == "weekly" and (args.repair or explicit_backfill or explicit_health):
        raise SystemExit("--cadence weekly only applies to daily or catch-up mode")
    if args.repair:
        args.mode = "repair"
    elif args.catch_up:
        args.mode = "catch-up"


def handle_gap_marker_cli(reg: Registry, args: argparse.Namespace) -> int:
    """--mark-gap / --clear-gap: operator tombstone management; no run happens."""
    if args.mark_gap and args.clear_gap:
        raise SystemExit("--mark-gap and --clear-gap are mutually exclusive")
    spec = args.mark_gap or args.clear_gap
    parts = [p.strip() for p in spec.split(":")]
    if len(parts) < 2 or (args.clear_gap and len(parts) != 2):
        raise SystemExit(f"invalid gap spec {spec!r}; expected SECTOR:DATE[:REASON]")
    sector_name, iso_date = parts[0], parse_iso(parts[1])
    if sector_name not in reg.names:
        raise SystemExit(f"unknown sector {sector_name!r}; valid={reg.names}")
    with _GAP_MARKER_LOCK:
        markers = load_gap_markers()
        if args.mark_gap:
            reason = ":".join(parts[2:]).strip() or "operator-marked permanent gap"
            sectors = markers.setdefault("sectors", {})
            sectors.setdefault(sector_name, {})[iso_date] = {
                "failures": int((sectors.get(sector_name, {}).get(iso_date) or {}).get("failures", 0)),
                "permanent": True,
                "reason": reason,
                "source": "operator",
                "marked_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            print(f"marked permanent gap: {sector_name} {iso_date} ({reason})")
        else:
            clear_gap_record(markers, sector_name, iso_date)
            print(f"cleared gap record: {sector_name} {iso_date}")
        save_gap_markers(markers)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return run_selftest()
    reg = load_registry(args.registry)
    validate_registry_paths(reg)
    if args.mark_gap or args.clear_gap:
        return handle_gap_marker_cli(reg, args)
    if args.repair_days is None:
        args.repair_days = reg.repair_days
    if args.repair_days < 1:
        raise SystemExit(f"--repair-days must be >= 1, got {args.repair_days}")
    if args.lock_stale_sec < 1:
        raise SystemExit(f"--lock-stale-sec must be >= 1, got {args.lock_stale_sec}")
    resolve_mode(args)

    only = [s.strip() for s in args.only_sectors.split(",") if s.strip()]
    skip = [s.strip() for s in args.skip_sectors.split(",") if s.strip()]
    selected = select_sectors(reg, only, skip)
    target = resolve_target_date(args.as_of)
    if args.catch_up_from:
        if args.mode != "catch-up":
            raise SystemExit("--catch-up-from requires --catch-up")
        args.catch_up_from = parse_iso(args.catch_up_from)
        if args.catch_up_from > target:
            raise SystemExit(
                f"--catch-up-from ({args.catch_up_from}) must not be after target ({target})"
            )
    else:
        args.catch_up_from = None
    # Sample the live completed session ONCE, next to target resolution, so a run
    # spanning the 17:00 ET close cannot reclassify the intended current-session
    # command as historical at command-build time hours later.
    args.live_session = latest_completed_trading_session()

    def log(msg: str) -> None:
        print(msg, flush=True)

    if args.mode == "backfill":
        if not args.from_date or not args.to_date:
            raise SystemExit("--mode backfill requires --from and --to")
        args.from_date = parse_iso(args.from_date)
        args.to_date = parse_iso(args.to_date)
        if args.from_date > args.to_date:
            raise SystemExit(f"--from ({args.from_date}) must not be after --to ({args.to_date})")

    repair_map: dict[str, list[str]] = {}
    if args.mode == "repair":
        repair_map = parse_repair_arg(reg, args.repair)
        selected = [s for s in selected if s.name in repair_map]
        if not selected:
            raise SystemExit("--repair selection is empty")

    log(
        f"orchestrator mode={args.mode} cadence={args.cadence} target={target} "
        f"sectors={[s.name for s in selected]} dry_run={args.dry_run}"
    )

    if args.mode == "health-check":
        report = health_check(reg, list(selected), target)
        print(json.dumps(report, indent=2))
        return 0 if report["stage1_ready"] else 1

    run_stamp = make_run_stamp()
    tier0_selected = [s for s in selected if s.dependency_tier == 0]
    tier1_selected = [s for s in selected if s.dependency_tier == 1]

    # --dry-run: assemble & print the whole matrix, write an ISOLATED dry-run manifest.
    if args.dry_run:
        run_dir = DRYRUN_RUNS_ROOT / run_stamp
        matrix: dict[str, tuple[list[list[str]], str]] = {}
        for sector in tier0_selected + tier1_selected:
            matrix[sector.name] = build_sector_commands(reg, sector, args, target, repair_map)
        print_matrix(matrix)
        rows = [
            {
                "sector": name,
                "status": "DRY_RUN",
                "command_hash": _command_hash(cmds),
                "commands": [subprocess.list2cmdline(c) for c in cmds],
                "note": note,
            }
            for name, (cmds, note) in matrix.items()
        ]
        write_master_manifest(
            run_dir,
            {
                "run_stamp": run_stamp,
                "mode": args.mode,
                "catch_up_from": getattr(args, "catch_up_from", None) or "",
                "cadence": args.cadence,
                "target": target,
                "dry_run": True,
                "sectors": rows,
            },
        )
        return 0

    run_dir = RUNS_ROOT / run_stamp
    resume_records = load_resume_records(reg) if args.resume else []
    net_sem = threading.Semaphore(reg.max_concurrent_network_lanes)

    with OrchestrationLock(ORCH_LOCK_PATH, stale_after_sec=args.lock_stale_sec):
        for abandoned in reconcile_abandoned_runs():
            log(f"reconciled abandoned RUNNING manifest -> ABORTED: {abandoned}")
        # Crash-resumable: write an in-progress RUNNING manifest BEFORE execution.
        write_master_manifest(
            run_dir,
            {
                "run_stamp": run_stamp,
                "mode": args.mode,
                "catch_up_from": getattr(args, "catch_up_from", None) or "",
                "cadence": args.cadence,
                "target": target,
                "master_pid": os.getpid(),
                "dry_run": False,
                "acceptance": "RUNNING",
                "tier0_gate": "RUNNING",
                "sectors": [],
            },
        )
        results: dict[str, RunResult] = {}
        overall = "FAIL"
        gate_ok = False
        failing: list[str] = []
        freshness: dict[str, list[dict[str, Any]]] = {}
        freshness_blocking_required: list[str] = []
        try:
            lanes = plan_lanes(reg, tier0_selected)
            results = run_tier0(reg, lanes, args, target, repair_map, run_dir, resume_records, net_sem, log)

            if args.mode == "repair":
                # Repair-scoped gate: only the repaired sectors' outcomes decide it (finding 2).
                # No UNKNOWN injection for non-repaired sectors -- they are not part of this run.
                gate_ok, failing = tier0_gate(reg, results, repair_scope=[s.name for s in tier0_selected])
            else:
                gate_ok, failing = tier0_gate(reg, results)
                # Record excluded required Tier-0 sectors as UNKNOWN for transparency (finding 4).
                selected_names = {s.name for s in selected}
                for sector in reg.sectors:
                    if (
                        sector.dependency_tier == 0
                        and sector.required
                        and sector.name not in results
                        and sector.name not in selected_names
                    ):
                        results[sector.name] = RunResult(
                            sector=sector.name,
                            status="UNKNOWN",
                            note="excluded from selection; required for the Tier-0 gate",
                        )
            log(f"tier0 gate: {'PASS' if gate_ok else 'FAIL'} (failing_required={failing})")

            # Tier 1 (portfolio) only after the gate. It never runs in repair mode: the repair
            # selection excludes portfolio (repair: null), so tier1_selected is empty there.
            if tier1_selected and args.mode in {"daily", "catch-up"}:
                if gate_ok or args.ignore_gate:
                    for sector in tier1_selected:
                        if args.mode == "catch-up":
                            # Historical books are built oldest-first and the
                            # current book is rebuilt strictly at the end.
                            plan = build_catch_up_plan(
                                reg,
                                sector,
                                target,
                                force=args.force,
                                include_weekly_pre=args.cadence == "weekly",
                                live_completed_session=getattr(args, "live_session", None),
                                catch_up_from=getattr(args, "catch_up_from", None),
                            )
                            commands, note = plan.all_commands, plan_note(plan)
                            res = run_catch_up_sector(reg, sector, plan, run_dir=run_dir, net_sem=net_sem, log=log)
                        else:
                            commands, note = build_sector_commands(reg, sector, args, target, repair_map)
                            res = run_commands(
                                sector,
                                commands,
                                run_dir=run_dir,
                                net_sem=net_sem,
                                dry_run=False,
                                empty_status="NOTE",
                                log=log,
                            )
                        res.note = "; ".join(part for part in (note, res.note) if part)
                        res.command_hash = _command_hash(commands)
                        res.content_hash = _content_hash(sector, args.registry)
                        _set_artifact(sector, res, target, dry_run=False)
                        finalize_result(sector, res, target=target, mode=args.mode, dry_run=False)
                        results[sector.name] = res
                        write_master_manifest(
                            run_dir,
                            {
                                "run_stamp": run_stamp,
                                "mode": args.mode,
                                "catch_up_from": getattr(args, "catch_up_from", None) or "",
                                "cadence": args.cadence,
                                "target": target,
                                "master_pid": os.getpid(),
                                "dry_run": False,
                                "acceptance": "RUNNING",
                                "tier0_gate": _gate_manifest_value(gate_ok, args.ignore_gate),
                                "tier0_failing_required": failing,
                                "sectors": _result_rows(results),
                            },
                        )
                else:
                    for sector in tier1_selected:
                        results[sector.name] = RunResult(
                            sector=sector.name, status="SKIPPED_GATE", note=f"tier0 gate failed: {failing}"
                        )
                        log(f"  [{sector.name}] SKIPPED_GATE (tier0 failing_required={failing})")
            elif tier1_selected and args.mode == "backfill":
                for sector in tier1_selected:
                    note = sector.backfill.note if sector.backfill else "no backfill"
                    results[sector.name] = RunResult(sector=sector.name, status="NOTE", note=note)
                    log(f"  [{sector.name}] NOTE: {note}")

            overall = compute_overall(selected, results, mode=args.mode, gate_ok=gate_ok, ignore_gate=args.ignore_gate)
            # Data-freshness sentinel: read-only surveillance of upstream feeds, run
            # after the sectors so it reports the post-run state. Probes never block
            # publication; only a required probe that is STALE flips the acceptance.
            freshness = evaluate_freshness(reg.sectors, target, log)
            overall, freshness_blocking_required = apply_freshness_consequences(
                freshness,
                results,
                overall,
                log,
                selected_names={s.name for s in selected},
            )
        finally:
            payload: dict[str, Any] = {
                "run_stamp": run_stamp,
                "mode": args.mode,
                "catch_up_from": getattr(args, "catch_up_from", None) or "",
                "cadence": args.cadence,
                "target": target,
                "master_pid": os.getpid(),
                "dry_run": False,
                "acceptance": overall,
                "tier0_gate": _gate_manifest_value(gate_ok, bool(args.ignore_gate)),
                "tier0_failing_required": failing,
                "ignore_gate": bool(args.ignore_gate),
                "sectors": _result_rows(results, freshness=freshness),
            }
            if freshness:
                payload["freshness"] = freshness
            if freshness_blocking_required:
                payload["freshness_blocking_required"] = freshness_blocking_required
            manifest_path = write_master_manifest(run_dir, payload)
    log(f"master_manifest: {manifest_path}")
    log(f"OVERALL: {overall}")
    return 0 if overall == "PASS" else 1


# --------------------------------------------------------------------------- #
# Selftest (no subprocess; fake registry)
# --------------------------------------------------------------------------- #
FAKE_REGISTRY = {
    "defaults": {
        "timeout_sec": 60,
        "retries": 0,
        "max_concurrent_network_lanes": 2,
        "catch_up_gap_backfill_threshold": 3,
        "catch_up_window_days": 45,
        "repair_days": 5,
        "permanent_gap_after_failures": 2,
        "market_closures": [],
        "calendar_reference_sectors": ["alpha", "defense_x"],
    },
    "group_order": {"grp_tech": ["alpha", "beta"], "grp_ind": ["defense_x", "mach_x"]},
    "sectors": [
        {
            "name": "alpha",
            "db_group": "grp_tech",
            "dependency_tier": 0,
            "required": True,
            "network": True,
            "entry_script": "a/run.py",
            "date_flag": "--asof",
            "args_template": ["--asof", "{date}"],
            "force_args": ["--force-refresh"],
            "publish_glob": "output/a/{date}/a.csv",
            "publish_date_format": "%Y-%m-%d",
            "oos_column": "oos_score_valid_flag",
            "require_oos_valid": True,
            "staleness_tolerance_days": 3,
            "backfill_window_days": 5,
            "health": {"manifest": "output/a/m.json", "status_keys": ["status"]},
            "daily_post_steps": [{"script": "a/promote.py", "args_template": ["--asof", "{date}"]}],
            "backfill": {
                "script": "a/bf.py",
                "args_template": ["--start", "{from}", "--end", "{to}"],
                "per_date": False,
            },
            "repair": {
                "date_flag": "--asof",
                "selection_flag": "--only",
                "steps": ["s1", "s2"],
                "rebuild_steps": ["r1", "r2"],
                "extra_args": [],
            },
        },
        {
            "name": "beta",
            "db_group": "grp_tech",
            "dependency_tier": 0,
            "required": True,
            "network": True,
            "entry_script": "b/run.py",
            "date_flag": "--asof",
            "args_template": ["--asof", "{date}"],
            "force_args": [],
            "publish_glob": "output/b/{date}/b.csv",
            "publish_date_format": "%Y-%m-%d",
            "oos_column": None,
            "staleness_tolerance_days": 3,
            "health": {"manifest": None, "status_keys": []},
            "backfill": {"script": "b/bf.py", "args_template": ["--from", "{from}", "--to", "{to}"], "per_date": True},
            "repair": {
                "date_flag": "--asof",
                "selection_flag": "--steps",
                "steps": ["x1"],
                "rebuild_steps": ["x9"],
                "extra_args": [],
            },
        },
        {
            "name": "defense_x",
            "db_group": "grp_ind",
            "dependency_tier": 0,
            "required": True,
            "network": True,
            "entry_script": "d/run.py",
            "date_flag": "--asof",
            "args_template": ["--asof", "{date}"],
            "force_args": [],
            "publish_glob": "output/d/{date}/d.csv",
            "publish_date_format": "%Y-%m-%d",
            "oos_column": "oos_score_valid_flag",
            "require_oos_valid": True,
            "staleness_tolerance_days": 3,
            "health": {"manifest": "output/d/m.json", "status_keys": ["acceptance"]},
            "weekly_pre_steps": [{"script": "d/snap.py", "args_template": ["--end-date", "{date}"]}],
            "backfill": {
                "script": "d/19.py",
                "args_template": ["--start-date", "{from}", "--end-date", "{to}", "--membership-mode", "pit"],
                "per_date": False,
            },
            "repair": {
                "date_flag": "--asof",
                "selection_flag": "",
                "steps": ["13", "17"],
                "rebuild_steps": [],
                "extra_args": ["--positioning-through-publish-only"],
            },
            "freshness_probes": [
                {
                    "name": "borrow_age",
                    "kind": "sqlite_max_date",
                    "target": {"db": "fixture/f.sqlite", "sql": "SELECT MAX(asof_date) FROM t"},
                    "tolerance_days": 10,
                    "warn_lead_days": 3,
                },
                {
                    "name": "13f_period",
                    "kind": "deadline_schedule",
                    "target": {
                        "db": "fixture/f.sqlite",
                        "sql": "SELECT MAX(period_of_report) FROM t",
                        "cadence": "quarterly",
                        "deadline_days": 45,
                        "publication_lag_days": 35,
                    },
                    "tolerance_days": 0,
                    "warn_lead_days": 7,
                    "required": True,
                },
            ],
        },
        {
            "name": "mach_x",
            "db_group": "grp_ind",
            "dependency_tier": 0,
            "required": False,
            "network": True,
            "entry_script": "m/run.py",
            "date_flag": "--asof",
            "args_template": ["--asof", "{date}"],
            "force_args": ["--force"],
            "publish_glob": "output/m/{date}/m.csv",
            "publish_date_format": "%Y-%m-%d",
            "oos_column": "oos_score_valid_flag",
            "require_oos_valid": True,
            "staleness_tolerance_days": 3,
            "health": {"manifest": "output/m/m.json", "status_keys": ["acceptance"]},
            "backfill": {
                "script": "m/run.py",
                "args_template": ["--asof", "{to}", "--include-historical-backfill", "--history-start-date", "{from}"],
                "per_date": False,
            },
            "repair": {
                "date_flag": "--asof",
                "selection_flag": "--only",
                "steps": ["12", "13"],
                "rebuild_steps": ["06a", "10b"],
                "extra_args": [],
            },
        },
        {
            "name": "port_x",
            "db_group": "portfolio",
            "dependency_tier": 1,
            "required": True,
            "network": True,
            "entry_script": "p/18.py",
            "date_flag": "--as-of",
            "args_template": ["--as-of", "{date}"],
            "force_args": ["--force"],
            "publish_glob": "p/output/runs/{date}/final/final_manifest.json",
            "publish_date_format": "%Y-%m-%d",
            "oos_column": None,
            "require_oos_valid": False,
            "staleness_tolerance_days": 10,
            "publish_epoch": "2026-07-02",
            "health": {
                "manifest": "p/output/runs/{date}/orchestration_meta.json",
                "status_keys": ["acceptance"],
                "healthy_values": ["PASS", "PASS_WITH_ADVISORY_WARNINGS"],
            },
            "backfill": {"script": "", "args_template": [], "per_date": False, "note": "stage11 not auto-run"},
            "repair": None,
        },
    ],
}


def run_selftest() -> int:
    checks: list[str] = []

    def ok(name: str, cond: bool) -> None:
        assert cond, f"SELFTEST FAIL: {name}"
        checks.append(name)

    # --- registry parse ---
    tmp = Path(tempfile.mkdtemp()) / "fake_registry.yaml"
    tmp.write_text(yaml.safe_dump(FAKE_REGISTRY), encoding="utf-8")
    reg = load_registry(tmp)
    ok("registry_parse_sector_count", len(reg.sectors) == 5)
    ok("registry_defaults", reg.max_concurrent_network_lanes == 2 and reg.catch_up_gap_backfill_threshold == 3)
    ok("registry_catch_up_window", reg.catch_up_window_days == 45)
    ok("registry_permanent_gap_threshold", reg.permanent_gap_after_failures == 2)
    ok("registry_publish_epoch_parsed", reg.by_name("port_x").publish_epoch == "2026-07-02")
    ok(
        "registry_backfill_window_parsed",
        reg.by_name("alpha").backfill_window_days == 5 and reg.by_name("beta").backfill_window_days is None,
    )
    ok(
        "registry_backfill_window_default_tolerance",
        sector_backfill_window_days(reg.by_name("beta")) == 3
        and sector_backfill_window_days(reg.by_name("alpha")) == 5,
    )
    ok("registry_group_order", reg.group_order["grp_ind"] == ["defense_x", "mach_x"])
    port = reg.by_name("port_x")
    ok("registry_tier1", port.dependency_tier == 1 and port.date_flag == "--as-of")
    ok(
        "registry_backfill_note",
        port.backfill is not None and port.backfill.script == "" and "stage11" in port.backfill.note,
    )
    ok("registry_require_oos", reg.by_name("alpha").require_oos_valid and not port.require_oos_valid)

    # --- registry validation: bad lanes / repair_days ---
    for bad_key, bad_val in (("max_concurrent_network_lanes", 0), ("repair_days", 0)):
        bad = {**FAKE_REGISTRY, "defaults": {**FAKE_REGISTRY["defaults"], bad_key: bad_val}}
        btmp = Path(tempfile.mkdtemp()) / "bad.yaml"
        btmp.write_text(yaml.safe_dump(bad), encoding="utf-8")
        try:
            load_registry(btmp)
            ok(f"registry_reject_{bad_key}", False)
        except ValueError:
            ok(f"registry_reject_{bad_key}", True)

    # --- command building ---
    alpha = reg.by_name("alpha")
    dcmd = daily_command(alpha, "2026-07-17", force=True)
    ok("daily_cmd_asof", "--asof" in dcmd and "2026-07-17" in dcmd)
    ok("daily_cmd_force", "--force-refresh" in dcmd)
    dcmd_nf = daily_command(reg.by_name("beta"), "2026-07-17", force=True)
    ok("daily_cmd_no_force_when_empty", "--force" not in " ".join(dcmd_nf))
    defense = reg.by_name("defense_x")
    wpre = weekly_pre_commands(defense, "2026-07-17")
    ok("weekly_pre_generic", len(wpre) == 1 and "--end-date" in wpre[0])
    ok("weekly_pre_no_promotion", all("27_promote" not in " ".join(c) for c in wpre))
    bf, _ = backfill_commands(alpha, "2026-01-01", "2026-01-10", reg)
    ok("backfill_range_single", len(bf) == 1 and "--start" in bf[0])
    bf_pit, _ = backfill_commands(defense, "2026-01-01", "2026-01-10", reg)
    ok("backfill_defense_pit", "--membership-mode" in bf_pit[0] and "pit" in bf_pit[0])
    portfolio = Sector(**{**port.__dict__, "name": "portfolio_layer"})
    historical_portfolio = _catch_up_daily_command(
        portfolio,
        "2026-07-16",
        force=False,
        live_completed_session="2026-07-17",
    )
    live_portfolio = _catch_up_daily_command(
        portfolio,
        "2026-07-17",
        force=False,
        live_completed_session="2026-07-17",
    )
    ok("catch_up_historical_portfolio_suppresses_event_cycle", "--historical-catchup" in historical_portfolio)
    ok("catch_up_historical_portfolio_keeps_resume", "--force" not in historical_portfolio)
    ok("catch_up_live_portfolio_keeps_event_cycle", "--historical-catchup" not in live_portfolio)
    ok("catch_up_live_portfolio_keeps_resume", "--force" not in live_portfolio)

    # --- repair selection (two-stage) ---
    rmap = parse_repair_arg(reg, "alpha:s1,mach_x")
    ok("repair_parse_specific", rmap["alpha"] == ["s1"])
    ok("repair_parse_all_steps", rmap["mach_x"] == ["12", "13"])
    try:
        parse_repair_arg(reg, "alpha:bogus")
        ok("repair_reject_bad_step", False)
    except ValueError:
        ok("repair_reject_bad_step", True)
    try:
        parse_repair_arg(reg, "defense_x:13")
        ok("repair_reject_selector_for_nonselectable_runner", False)
    except ValueError:
        ok("repair_reject_selector_for_nonselectable_runner", True)
    rcmds = repair_commands(_with_repair_steps(alpha, ["s1"]), ["2026-07-16", "2026-07-17"])
    ok("repair_two_stage_per_date", len(rcmds) == 4)  # 2 dates x (source + rebuild)
    ok("repair_cmd_source_only", "--only" in rcmds[0] and "s1,s2".split(",")[0] in " ".join(rcmds[0]))
    ok("repair_cmd_rebuild", "r1,r2" in " ".join(rcmds[1]) and "--only" in rcmds[1])
    rcmds_def = repair_commands(defense, ["2026-07-17"])
    ok(
        "repair_defense_single_tail",
        len(rcmds_def) == 1 and "--positioning-through-publish-only" in rcmds_def[0] and "--only" not in rcmds_def[0],
    )

    # --- date / gap math + NYSE holidays ---
    ok("parse_iso", parse_iso(" 2026-07-17 ") == "2026-07-17")
    ok("holiday_july3_2026_observed", not is_trading_day(date(2026, 7, 3)))  # observed July 4 (Sat)
    ok("holiday_july6_2026_trading", is_trading_day(date(2026, 7, 6)))
    ok("holiday_new_year_2026", not is_trading_day(date(2026, 1, 1)))
    ok("holiday_mlk_2026", not is_trading_day(date(2026, 1, 19)))
    ok("holiday_good_friday_2026", not is_trading_day(date(2026, 4, 3)))
    ok("holiday_christmas_2026", not is_trading_day(date(2026, 12, 25)))
    ok("holiday_new_year_2022_no_dec31", is_trading_day(date(2021, 12, 31)))  # Jan 1 2022 was Sat; Fri stays open
    span = trading_dates_in_range(reg, "2026-06-29", "2026-07-07")
    ok(
        "trading_span_excludes_july3_and_weekend",
        span == ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07"],
    )
    span2 = trading_dates_in_range(reg, "2026-07-18", "2026-07-19")  # Sat..Sun
    ok("trading_span_weekend_empty", span2 == [])
    saved_project_root = globals()["PROJECT_ROOT"]
    calendar_root = Path(tempfile.mkdtemp())
    try:
        globals()["PROJECT_ROOT"] = calendar_root
        for folder in ("2026-07-03", "2026-07-17", "2026-07-18"):
            (calendar_root / "output" / "a" / folder).mkdir(parents=True)
        known = known_trading_dates(reg)
        ok("published_valid_session_seeds_calendar", "2026-07-17" in known)
        ok("published_holiday_does_not_seed_calendar", "2026-07-03" not in known)
        ok("published_weekend_does_not_seed_calendar", "2026-07-18" not in known)
    finally:
        globals()["PROJECT_ROOT"] = saved_project_root
    lastn = last_n_dates(reg, "2026-07-17", 3)
    ok("last_n_dates", lastn == ["2026-07-15", "2026-07-16", "2026-07-17"])
    try:
        last_n_dates(reg, "2026-07-17", 0)
        ok("last_n_dates_reject_zero", False)
    except ValueError:
        ok("last_n_dates_reject_zero", True)

    # --- default target = latest COMPLETED trading session (close-aware, never artifacts) ---
    utc = timezone.utc
    ok(
        "target_after_close_same_day",
        resolve_target_date(None, now_utc=datetime(2026, 7, 21, 22, 0, tzinfo=utc)) == "2026-07-21",
    )  # Tue 18:00 ET
    ok(
        "target_before_close_prev_day",
        resolve_target_date(None, now_utc=datetime(2026, 7, 21, 12, 0, tzinfo=utc)) == "2026-07-20",
    )  # Tue 08:00 ET
    ok(
        "target_monday_before_close_skips_holiday",
        resolve_target_date(None, now_utc=datetime(2026, 7, 6, 12, 0, tzinfo=utc)) == "2026-07-02",
    )  # Mon 08:00 ET -> skip Jul3 + weekend
    ok(
        "target_saturday_prev_friday",
        resolve_target_date(None, now_utc=datetime(2026, 7, 18, 22, 0, tzinfo=utc)) == "2026-07-17",
    )
    ok("target_requested_passthrough", resolve_target_date("2026-05-01") == "2026-05-01")

    # --- missing dates: internal gaps surfaced ---
    published = {"2026-07-13", "2026-07-14", "2026-07-16", "2026-07-17"}  # 07-15 internal gap
    expected = trading_dates_in_range(reg, "2026-07-13", "2026-07-17")
    ok("missing_internal_gap", _missing_from_expected(published, expected) == ["2026-07-15"])

    # --- oos flag parser variants ---
    ok("flag_true_int", _flag_true(1) and _flag_true("1"))
    ok("flag_true_float", _flag_true("1.0") and _flag_true(1.0))
    ok("flag_true_words", _flag_true("true") and _flag_true("YES"))
    ok("flag_false", not _flag_true("0") and not _flag_true("0.0") and not _flag_true("") and not _flag_true("no"))

    # --- lane scheduling ---
    lanes = plan_lanes(reg, [s for s in reg.sectors if s.dependency_tier == 0])
    lane_map = {tuple(s.name for s in lane) for lane in lanes}
    ok("lane_grouping", ("defense_x", "mach_x") in lane_map)
    ok("lane_tech_group", any(set(t) == {"alpha", "beta"} for t in lane_map))
    ok("lane_industrials_order", ("defense_x", "mach_x") in lane_map)  # defense before machinery
    ok("lane_excludes_tier1", all("port_x" not in t for t in lane_map))

    # --- gate logic (UP_TO_DATE healthy; UNKNOWN/excluded unhealthy; optional ignored) ---
    base = {
        "alpha": RunResult("alpha", "PASS"),
        "beta": RunResult("beta", "UP_TO_DATE"),
        "defense_x": RunResult("defense_x", "PASS"),
        "mach_x": RunResult("mach_x", "OPTIONAL_FAIL"),
    }
    gate_ok, failing = tier0_gate(reg, base)
    ok("gate_up_to_date_healthy_optional_ignored", gate_ok and failing == [])
    base2 = dict(base)
    base2["defense_x"] = RunResult("defense_x", "FAIL")
    gate_ok2, failing2 = tier0_gate(reg, base2)
    ok("gate_required_fail", not gate_ok2 and "defense_x" in failing2)
    excluded = {"alpha": RunResult("alpha", "PASS"), "beta": RunResult("beta", "PASS")}  # defense_x excluded
    gate_ok3, failing3 = tier0_gate(reg, excluded)
    ok("gate_excluded_required_unknown", not gate_ok3 and "defense_x" in failing3)

    # --- overall consistency ---
    sel = [s for s in reg.sectors]
    ok(
        "overall_up_to_date_pass",
        compute_overall(
            sel, {**base, "port_x": RunResult("port_x", "PASS")}, mode="catch-up", gate_ok=True, ignore_gate=False
        )
        == "PASS",
    )
    ok(
        "overall_gate_fail_no_bypass",
        compute_overall(sel, base, mode="daily", gate_ok=False, ignore_gate=False) == "FAIL",
    )
    note_sel = [reg.by_name("port_x")]
    ok(
        "overall_backfill_note_ok",
        compute_overall(
            note_sel, {"port_x": RunResult("port_x", "NOTE")}, mode="backfill", gate_ok=True, ignore_gate=True
        )
        == "PASS",
    )
    ok(
        "overall_daily_note_required_fail",
        compute_overall(note_sel, {"port_x": RunResult("port_x", "NOTE")}, mode="daily", gate_ok=True, ignore_gate=True)
        == "FAIL",
    )

    # --- resume keying (finding 5: command + script/registry content hashes + artifact SHA) ---
    cmds_a = [daily_command(alpha, "2026-07-17", force=False)]
    h = _command_hash(cmds_a)
    ok("command_hash_stable", h == _command_hash(cmds_a))
    ok("command_hash_differs", h != _command_hash([daily_command(alpha, "2026-07-16", force=False)]))
    _saved_resume_root = globals()["PROJECT_ROOT"]
    tmp_resume = Path(tempfile.mkdtemp())
    try:
        globals()["PROJECT_ROOT"] = tmp_resume
        ch = _content_hash(alpha, DEFAULT_REGISTRY)
        ok("content_hash_stable", ch == _content_hash(alpha, DEFAULT_REGISTRY))
        art = tmp_resume / "output" / "a" / "2026-07-17" / "a.csv"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_text("asof_date,oos_score_valid_flag,ticker\n2026-07-17,1,AAA\n", encoding="utf-8")
        manifest = tmp_resume / "output" / "a" / "m.json"
        manifest.write_text(json.dumps({"status": "PASS", "asof_date": "2026-07-17"}), encoding="utf-8")
        art_sha = sha256_file(art)
        recs = [
            {
                "sector": "alpha",
                "status": "PASS",
                "target": "2026-07-17",
                "mode": "daily",
                "command_hash": h,
                "content_hash": ch,
                "sha256": art_sha,
                "artifact": str(art),
            }
        ]
        ok("resume_match_full_identity", resume_match(recs, alpha, "2026-07-17", "daily", h, ch))
        ok("resume_reject_wrong_target", not resume_match(recs, alpha, "2026-07-16", "daily", h, ch))
        ok("resume_reject_wrong_mode", not resume_match(recs, alpha, "2026-07-17", "catch-up", h, ch))
        ok("resume_reject_wrong_cmd_hash", not resume_match(recs, alpha, "2026-07-17", "daily", "deadbeef", ch))
        ok("resume_reject_wrong_content_hash", not resume_match(recs, alpha, "2026-07-17", "daily", h, "beadfeed"))
        recs_missing = [{**recs[0], "artifact": str(art) + ".nope"}]
        ok("resume_reject_missing_artifact", not resume_match(recs_missing, alpha, "2026-07-17", "daily", h, ch))
        recs_nosha = [{**recs[0], "sha256": ""}]
        ok("resume_reject_empty_stored_sha", not resume_match(recs_nosha, alpha, "2026-07-17", "daily", h, ch))
        art.write_text("MUTATED since prior PASS", encoding="utf-8")
        ok("resume_reject_sha_mismatch", not resume_match(recs, alpha, "2026-07-17", "daily", h, ch))
    finally:
        globals()["PROJECT_ROOT"] = _saved_resume_root
        import shutil

        shutil.rmtree(tmp_resume, ignore_errors=True)

    # --- dry-run isolation ---
    ok("dryrun_runs_root_isolated", DRYRUN_RUNS_ROOT != RUNS_ROOT and DRYRUN_RUNS_ROOT.name == "runs_dryrun")

    # --- selection ---
    selsec = select_sectors(reg, [], ["mach_x"])
    ok("select_skip", "mach_x" not in [s.name for s in selsec] and "alpha" in [s.name for s in selsec])
    for only_names, skip_names, label in (("nope", "", "unknown"),):
        try:
            select_sectors(reg, [only_names], [n for n in skip_names.split(",") if n])
            ok(f"select_reject_{label}", False)
        except ValueError:
            ok(f"select_reject_{label}", True)
    try:
        select_sectors(reg, [], ["alpha", "beta", "defense_x", "mach_x", "port_x"])
        ok("select_reject_empty", False)
    except ValueError:
        ok("select_reject_empty", True)

    # --- conflicting mode flags / cadence (health-check participates: it must never
    # silently become an executing catch-up run) ---
    for flags in (
        ["--repair", "alpha", "--catch-up"],
        ["--mode", "backfill", "--catch-up"],
        ["--mode", "health-check", "--catch-up"],
        ["--mode", "health-check", "--repair", "alpha"],
    ):
        a = parse_args(flags)
        try:
            resolve_mode(a)
            ok(f"reject_conflict_{'_'.join(flags)}", False)
        except SystemExit:
            ok(f"reject_conflict_{'_'.join(flags)}", True)

    weekly_catchup = parse_args(["--cadence", "weekly", "--catch-up"])
    resolve_mode(weekly_catchup)
    ok(
        "weekly_catchup_allowed",
        weekly_catchup.mode == "catch-up" and weekly_catchup.cadence == "weekly",
    )

    # --- finding 1: daily post-steps appended after the publish command ---
    args_daily = parse_args([])
    dcmds, dnote = build_sector_commands(reg, reg.by_name("alpha"), args_daily, "2026-07-17", {})
    ok("finding1_daily_post_step_appended", any("promote.py" in " ".join(c) for c in dcmds))
    ok(
        "finding1_post_step_after_publish",
        "run.py" in " ".join(dcmds[0])
        and "promote.py" in " ".join(dcmds[-1])
        and "--asof" in dcmds[-1]
        and "2026-07-17" in dcmds[-1],
    )
    ok("finding1_post_step_note", dnote == "daily+post_steps")
    dcmds_np, dnote_np = build_sector_commands(reg, reg.by_name("beta"), args_daily, "2026-07-17", {})
    ok("finding1_no_post_step_when_none", all("promote.py" not in " ".join(c) for c in dcmds_np) and dnote_np == "")
    args_daily.live_session = "2026-07-18"
    historical_daily_cmds, _ = build_sector_commands(reg, portfolio, args_daily, "2026-07-17", {})
    ok(
        "daily_historical_portfolio_suppresses_event_cycle",
        "--historical-catchup" in historical_daily_cmds[0],
    )
    ok(
        "daily_historical_portfolio_keeps_resume",
        "--force" not in historical_daily_cmds[0],
    )
    args_daily.live_session = "2026-07-17"
    live_daily_cmds, _ = build_sector_commands(reg, portfolio, args_daily, "2026-07-17", {})
    ok("daily_live_portfolio_keeps_event_cycle", "--historical-catchup" not in live_daily_cmds[0])

    # --- finding 2: repair-scoped gate (only repaired sectors decide it) ---
    gate_r, fail_r = tier0_gate(reg, {"alpha": RunResult("alpha", "PASS")}, repair_scope=["alpha"])
    ok("finding2_repair_gate_scoped_pass", gate_r and fail_r == [])
    gate_rf, fail_rf = tier0_gate(reg, {"alpha": RunResult("alpha", "FAIL")}, repair_scope=["alpha"])
    ok("finding2_repair_gate_fail_on_repaired", not gate_rf and fail_rf == ["alpha"])
    gate_opt, fail_opt = tier0_gate(reg, {"mach_x": RunResult("mach_x", "OPTIONAL_FAIL")}, repair_scope=["mach_x"])
    ok("finding2_repair_optional_fail_visible", not gate_opt and fail_opt == ["mach_x"])
    gate_full, _ff = tier0_gate(reg, {"alpha": RunResult("alpha", "PASS")})  # full-book gate still strict
    ok("finding2_fullbook_gate_still_strict", not gate_full)

    # --- findings 3 & 4: per-date artifact verification on real temp artifacts ---
    # Redirect the module-global PROJECT_ROOT (that publish_dir_root/read_manifest read) to a
    # temp tree for the duration of these checks, then restore it in the finally.
    _globals = globals()
    _saved_root = _globals["PROJECT_ROOT"]
    tmp_root = Path(tempfile.mkdtemp())

    def _mk_artifact(
        folder_parent: str,
        folder: str,
        filename: str,
        rows: int,
        *,
        date_col: str | None = "asof_date",
        date_val: str | None = None,
    ) -> None:
        d = tmp_root / folder_parent / folder
        d.mkdir(parents=True, exist_ok=True)
        with (d / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            header = ([date_col] if date_col else []) + ["oos_score_valid_flag", "ticker"]
            writer.writerow(header)
            for i in range(rows):
                writer.writerow(([date_val or folder] if date_col else []) + ["1", f"T{i}"])

    def _sector(**over: Any) -> Sector:
        base = dict(
            name="fsec",
            db_group="g",
            dependency_tier=0,
            required=True,
            network=False,
            entry_script="x/run.py",
            date_flag="--asof",
            args_template=["--asof", "{date}"],
            force_args=[],
            publish_glob="pub/{date}/t.csv",
            publish_date_format="%Y-%m-%d",
            oos_column="oos_score_valid_flag",
            gate_column=None,
            require_oos_valid=True,
            staleness_tolerance_days=3,
            backfill_window_days=None,
            native_backfill_min_dates=None,
            publish_epoch=None,
            historical_health_manifest_required=True,
            health=HealthSpec(manifest=None, status_keys=[]),
            backfill=None,
            repair=None,
            weekly_pre_steps=[],
            daily_post_steps=[],
            timeout_sec=60,
            retries=0,
        )
        base.update(over)
        return Sector(**base)  # type: ignore[arg-type]

    try:
        _globals["PROJECT_ROOT"] = tmp_root
        fsec = _sector()
        for d in ("2026-07-15", "2026-07-16", "2026-07-17"):
            _mk_artifact("pub", d, "t.csv", 2)
        ok("finding3_perdate_ok", verify_published_artifact_for_date(fsec, "2026-07-16")[0])
        ok("finding3_missing_date_fails", not verify_published_artifact_for_date(fsec, "2026-07-14")[0])
        _mk_artifact("pub", "2026-07-13", "t.csv", 2, date_val="2026-07-16")  # folder!=internal
        vmis = verify_published_artifact_for_date(fsec, "2026-07-13")
        ok("finding3_internal_date_mismatch_fails", not vmis[0] and any("as-of column" in r for r in vmis[1]))
        _mk_artifact("pub", "2026-07-12", "t.csv", 1, date_val="")
        blank_path = tmp_root / "pub" / "2026-07-12" / "t.csv"
        blank_path.write_text("asof_date,oos_score_valid_flag,ticker\n,1,T0\n", encoding="utf-8")
        vblank = verify_published_artifact_for_date(fsec, "2026-07-12")
        ok("finding4_blank_row_date_fails", not vblank[0] and any("blank" in r for r in vblank[1]))
        zero_oos_dir = tmp_root / "pub" / "2026-07-11"
        zero_oos_dir.mkdir(parents=True, exist_ok=True)
        (zero_oos_dir / "t.csv").write_text(
            "asof_date,oos_score_valid_flag,ticker\n2026-07-11,0,T0\n",
            encoding="utf-8",
        )
        ok(
            "historical_zero_oos_snapshot_is_complete",
            verify_published_artifact_for_date(
                fsec,
                "2026-07-11",
                policy_context="historical",
            )[0],
        )
        prod_zero_oos = verify_published_artifact_for_date(fsec, "2026-07-11")
        ok(
            "production_zero_oos_snapshot_still_fails",
            not prod_zero_oos[0]
            and any("no oos/gate-valid rows" in reason for reason in prod_zero_oos[1]),
        )
        # finalize_result (daily) verifies the single target; catch-up verification is
        # inline in run_catch_up_sector (covered by the catch-up policy block below).
        res_daily_bad = RunResult("fsec", "PASS")
        finalize_result(fsec, res_daily_bad, target="2026-07-14", mode="daily", dry_run=False)
        ok("daily_finalize_unverified_target_fails", res_daily_bad.status == "FAIL")
        res_daily_ok = RunResult("fsec", "PASS")
        finalize_result(fsec, res_daily_ok, target="2026-07-16", mode="daily", dry_run=False)
        ok("daily_finalize_verified_target_pass", res_daily_ok.status == "PASS")
        man_dir = tmp_root / "mani"
        man_dir.mkdir(parents=True, exist_ok=True)
        compact_manifest = tmp_root / "mani_compact" / "20260716" / "manifest.json"
        compact_manifest.parent.mkdir(parents=True, exist_ok=True)
        compact_manifest.write_text(
            json.dumps({"status": "PASS", "asof": "2026-07-16"}),
            encoding="utf-8",
        )
        fsec_compact = _sector(
            publish_date_format="%Y%m%d",
            health=HealthSpec(
                manifest="mani_compact/{date}/manifest.json",
                status_keys=["status"],
            ),
        )
        ok(
            "health_manifest_uses_sector_date_format",
            read_manifest(fsec_compact, "2026-07-16") == ("PASS", "2026-07-16"),
        )

        # finding 4: empty-asof manifest is NOT date-verifying -> artifact date column must verify
        (man_dir / "empty_2026-07-16.json").write_text(json.dumps({"status": "PASS", "asof": ""}), encoding="utf-8")
        fsec_empty = _sector(health=HealthSpec(manifest="mani/empty_{date}.json", status_keys=["status"]))
        ok("finding4_empty_asof_artifact_verifies", verify_published_artifact_for_date(fsec_empty, "2026-07-16")[0])
        # empty-asof manifest AND no internal date column -> fail closed
        d2 = tmp_root / "pub2" / "2026-07-16"
        d2.mkdir(parents=True, exist_ok=True)
        with (d2 / "t.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["oos_score_valid_flag", "ticker"])
            writer.writerow(["1", "T0"])
        fsec_nodate = _sector(
            publish_glob="pub2/{date}/t.csv",
            health=HealthSpec(manifest="mani/empty_{date}.json", status_keys=["status"]),
        )
        vfc = verify_published_artifact_for_date(fsec_nodate, "2026-07-16")
        ok("finding4_empty_asof_no_datecol_failclosed", not vfc[0] and any("date unverified" in r for r in vfc[1]))
        # populated manifest asof that disagrees with folder -> fail
        (man_dir / "wrong_2026-07-16.json").write_text(
            json.dumps({"status": "PASS", "asof": "2026-07-10"}), encoding="utf-8"
        )
        fsec_wrong = _sector(health=HealthSpec(manifest="mani/wrong_{date}.json", status_keys=["status"]))
        vwrong = verify_published_artifact_for_date(fsec_wrong, "2026-07-16")
        ok(
            "finding4_manifest_asof_mismatch_fails",
            not vwrong[0] and any("manifest asof=2026-07-10" in r for r in vwrong[1]),
        )
        # M10: healthy_values set -- PASS_WITH_ADVISORY_WARNINGS healthy only when declared
        (man_dir / "advisory_2026-07-16.json").write_text(
            json.dumps({"acceptance": "PASS_WITH_ADVISORY_WARNINGS", "run_as_of": "2026-07-16"}),
            encoding="utf-8",
        )
        fsec_adv = _sector(
            health=HealthSpec(
                manifest="mani/advisory_{date}.json",
                status_keys=["acceptance"],
                healthy_values=["PASS", "PASS_WITH_ADVISORY_WARNINGS"],
            )
        )
        st_adv, asof_adv = read_manifest(fsec_adv, "2026-07-16")
        ok("m10_advisory_value_set_healthy", st_adv == "PASS" and asof_adv == "2026-07-16")
        fsec_strict = _sector(health=HealthSpec(manifest="mani/advisory_{date}.json", status_keys=["acceptance"]))
        ok("m10_default_value_set_strict", read_manifest(fsec_strict, "2026-07-16")[0] == "FAIL")
    finally:
        _globals["PROJECT_ROOT"] = _saved_root
        import shutil

        shutil.rmtree(tmp_root, ignore_errors=True)

    # --- catch-up policy: bounded window, epoch exemption, permanent-gap
    # --- markers, oldest-first best-effort history, strict current target last ---
    cu_root = Path(tempfile.mkdtemp())
    _saved_cu_root = _globals["PROJECT_ROOT"]
    try:
        _globals["PROJECT_ROOT"] = cu_root
        markers_path = cu_root / "markers.json"
        run_dir_cu = cu_root / "rundir"
        target_cu = "2026-07-17"

        def _publish(sec: Sector, iso: str, rows: int = 2) -> None:
            parent, filename = publish_dir_root(sec)
            d = parent / iso
            d.mkdir(parents=True, exist_ok=True)
            with (d / filename).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["asof_date", "oos_score_valid_flag", "ticker"])
                for i in range(rows):
                    writer.writerow([iso, "1", f"T{i}"])

        def _mk_runner(fail_dates: set[str], publish_sec: Sector):
            """No-subprocess command runner: publishes the date's artifact on success."""

            def _runner(command: list[str]) -> int:
                date_tok = next((t for t in command if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)), None)
                if date_tok is None:
                    return 0
                if date_tok in fail_dates:
                    return 1
                if "post.py" not in " ".join(command):
                    _publish(publish_sec, date_tok)
                return 0

            return _runner

        # gap report: window bounded by tolerance; historical gaps surfaced not run
        cusec = _sector(
            name="cusec",
            publish_glob="cu/{date}/t.csv",
            daily_post_steps=[{"script": "x/post.py", "args_template": ["--asof", "{date}"]}],
        )
        for d in ("2026-07-06", "2026-07-13"):
            _publish(cusec, d)
        rep = sector_gap_report(reg, cusec, target_cu, markers={"sectors": {}})
        ok("cu_target_missing_detected", rep.target_missing)
        ok("cu_window_bounded_by_tolerance", rep.backfill_missing == ["2026-07-14", "2026-07-15", "2026-07-16"])
        ok("cu_oldest_first_ordering", rep.backfill_missing == sorted(rep.backfill_missing))
        ok(
            "cu_historical_gaps_surfaced_not_run",
            rep.historical_gaps == ["2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"],
        )
        # A present file with a broken dated contract is still a catch-up gap.
        incomplete = _sector(name="incomplete", publish_glob="cu_incomplete/{date}/t.csv")
        for d in ("2026-07-13", "2026-07-14", "2026-07-16"):
            _publish(incomplete, d)
        bad_historical = cu_root / "cu_incomplete" / "2026-07-15"
        bad_historical.mkdir(parents=True, exist_ok=True)
        (bad_historical / "t.csv").write_text(
            "asof_date,oos_score_valid_flag,ticker\n2026-07-14,1,T0\n",
            encoding="utf-8",
        )
        rep_incomplete = sector_gap_report(
            reg,
            incomplete,
            target_cu,
            markers={"sectors": {}},
            catch_up_from="2026-07-13",
        )
        ok(
            "cu_present_but_incomplete_historical_is_gap",
            rep_incomplete.backfill_missing == ["2026-07-15"],
        )
        # A date-partitioned terminal manifest is historical evidence, not a
        # mutable latest-state file. Its failed verdict must make an otherwise
        # valid dated artifact actionable again.
        dated_health = _sector(
            name="dated_health",
            publish_glob="cu_dated_health/{date}/t.csv",
            health=HealthSpec(
                manifest="cu_dated_health/{date}/health.json",
                status_keys=["acceptance"],
            ),
        )
        for d in ("2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16"):
            _publish(dated_health, d)
            health_path = cu_root / "cu_dated_health" / d / "health.json"
            health_path.write_text(
                json.dumps({
                    "acceptance": "FAIL" if d == "2026-07-15" else "PASS",
                    "asof_date": d,
                }),
                encoding="utf-8",
            )
        rep_dated_health = sector_gap_report(
            reg,
            dated_health,
            target_cu,
            markers={"sectors": {}},
            catch_up_from="2026-07-13",
        )
        ok(
            "cu_failed_dated_health_manifest_is_gap",
            rep_dated_health.backfill_missing == ["2026-07-15"],
        )
        historical_artifact_health = _sector(
            name="historical_artifact_health",
            publish_glob="cu_dated_health/{date}/t.csv",
            historical_health_manifest_required=False,
            health=HealthSpec(
                manifest="cu_dated_health/{date}/health.json",
                status_keys=["acceptance"],
            ),
        )
        rep_artifact_health = sector_gap_report(
            reg,
            historical_artifact_health,
            target_cu,
            markers={"sectors": {}},
            catch_up_from="2026-07-13",
        )
        ok(
            "cu_explicit_artifact_only_history_ignores_live_manifest",
            rep_artifact_health.backfill_missing == [],
        )
        ok(
            "cu_artifact_only_history_keeps_current_health_strict",
            not verify_published_artifact_for_date(
                historical_artifact_health,
                "2026-07-15",
            )[0],
        )

        # A root/latest manifest cannot be applied retroactively. Historical
        # completeness therefore remains tied to the dated artifact itself.
        root_health = _sector(
            name="root_health",
            publish_glob="cu_root_health/{date}/t.csv",
            health=HealthSpec(
                manifest="cu_root_health/latest_health.json",
                status_keys=["acceptance"],
            ),
        )
        for d in ("2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16"):
            _publish(root_health, d)
        root_health_path = cu_root / "cu_root_health" / "latest_health.json"
        root_health_path.write_text(
            json.dumps({"acceptance": "FAIL", "asof_date": "2026-07-16"}),
            encoding="utf-8",
        )
        rep_root_health = sector_gap_report(
            reg,
            root_health,
            target_cu,
            markers={"sectors": {}},
            catch_up_from="2026-07-13",
        )
        ok(
            "cu_root_health_manifest_not_retroactive",
            rep_root_health.backfill_missing == [],
        )
        cusec_w = _sector(name="cusec", publish_glob="cu/{date}/t.csv", backfill_window_days=7)
        rep_w = sector_gap_report(reg, cusec_w, target_cu, markers={"sectors": {}})
        ok(
            "cu_backfill_window_days_override",
            "2026-07-10" in rep_w.backfill_missing and "2026-07-09" not in rep_w.backfill_missing,
        )
        rep_explicit = sector_gap_report(
            reg,
            cusec_w,
            target_cu,
            markers={"sectors": {}},
            catch_up_from="2026-07-07",
        )
        ok(
            "cu_explicit_lower_bound_overrides_auto_window",
            "2026-07-07" in rep_explicit.backfill_missing
            and rep_explicit.historical_gaps == [],
        )

        # Missing the first session of the week must rerun the weekly prerequisite
        # before the current target. Otherwise a Friday catch-up can publish a fresh
        # file against the previous week's universe/configuration.
        cusec_weekly = _sector(
            name="cusec_weekly",
            publish_glob="cu_weekly/{date}/t.csv",
            weekly_pre_steps=[{"script": "x/weekly.py", "args_template": ["--asof", "{date}"]}],
        )
        _publish(cusec_weekly, "2026-07-10")
        plan_weekly = build_catch_up_plan(
            reg,
            cusec_weekly,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers={"sectors": {}},
            catch_up_from="2026-07-13",
        )
        ok(
            "cu_missed_weekly_session_refreshes_current_target",
            plan_weekly.target_unhealthy
            and len(plan_weekly.pre_commands) == 1
            and "weekly.py" in " ".join(plan_weekly.pre_commands[0])
            and bool(plan_weekly.target_commands)
            and target_cu in " ".join(plan_weekly.target_commands[0]),
        )

        # Old gaps outside the actionable catch-up window are diagnostic only.
        # They must not make every current no-op launch an expensive weekly job.
        plan_inert_historical_weekly = build_catch_up_plan(
            reg,
            cusec_weekly,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers={"sectors": {}},
        )
        ok(
            "cu_inert_historical_gap_does_not_repeat_weekly_prerequisite",
            bool(plan_inert_historical_weekly.historical_gaps)
            and plan_inert_historical_weekly.pre_commands == [],
        )

        # publish-convention epoch: pre-epoch dates exempt from missing-detection
        cusec_e = _sector(name="cusec", publish_glob="cu/{date}/t.csv", publish_epoch="2026-07-13")
        rep_e = sector_gap_report(reg, cusec_e, target_cu, markers={"sectors": {}})
        ok(
            "cu_epoch_exempts_preepoch_dates",
            rep_e.historical_gaps == [] and rep_e.backfill_missing == ["2026-07-14", "2026-07-15", "2026-07-16"],
        )

        # permanent-gap markers: auto-tombstone after N failures; respected by scans
        mk = {"sectors": {}}
        record_gap_failure(mk, "cusec", "2026-07-15", "rc=1", auto_permanent_after=2)
        ok("cu_marker_first_failure_not_permanent", permanent_gap_dates(mk, "cusec") == set())
        record_gap_failure(mk, "cusec", "2026-07-15", "rc=1", auto_permanent_after=2)
        ok("cu_marker_auto_permanent_after_threshold", permanent_gap_dates(mk, "cusec") == {"2026-07-15"})
        rep_m = sector_gap_report(reg, cusec, target_cu, markers=mk)
        ok(
            "cu_permanent_gap_not_retried",
            "2026-07-15" not in rep_m.backfill_missing and rep_m.permanent_gaps == ["2026-07-15"],
        )
        rep_auto_retry = sector_gap_report(
            reg,
            cusec,
            target_cu,
            markers=mk,
            catch_up_from="2026-07-13",
        )
        ok(
            "cu_explicit_range_retries_auto_tombstone",
            "2026-07-15" in rep_auto_retry.backfill_missing
            and rep_auto_retry.permanent_gaps == [],
        )
        weekly_mk = {"sectors": {"cusec_weekly": {"2026-07-13": {"permanent": True}}}}
        for published_date in ("2026-07-14", "2026-07-15", "2026-07-16", target_cu):
            _publish(cusec_weekly, published_date)
        plan_permanent_weekly = build_catch_up_plan(
            reg,
            cusec_weekly,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers=weekly_mk,
            catch_up_from="2026-07-13",
        )
        ok(
            "cu_permanent_weekly_gap_does_not_repeat_prerequisite",
            plan_permanent_weekly.permanent_gaps == ["2026-07-13"]
            and plan_permanent_weekly.pre_commands == []
            and plan_permanent_weekly.target_commands == []
            and not plan_permanent_weekly.target_unhealthy,
        )
        clear_gap_record(mk, "cusec", "2026-07-15")
        ok("cu_marker_cleared", permanent_gap_dates(mk, "cusec") == set())

        # plan: oldest-first backfill, then CURRENT TARGET LAST; post steps per date
        plan_cu = build_catch_up_plan(
            reg, cusec, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        ok("cu_plan_current_target_last", plan_cu.target_missing and target_cu in " ".join(plan_cu.all_commands[-2]))
        ok("cu_plan_target_post_step", "post.py" in " ".join(plan_cu.all_commands[-1]))
        ok("cu_plan_backfill_oldest_first", plan_cu.backfill_dates == ["2026-07-14", "2026-07-15", "2026-07-16"])
        ok(
            "cu_plan_backfill_before_target",
            all(target_cu in " ".join(c) for c in plan_cu.target_commands)
            and plan_cu.all_commands[-len(plan_cu.target_commands) :] == plan_cu.target_commands,
        )
        ok(
            "cu_plan_historical_flag_on_backfill_only",
            all("--historical-catchup" not in c for c in plan_cu.target_commands),
        )
        forceable_cusec = Sector(
            **{**cusec.__dict__, "force_args": ["--force"]}
        )
        plan_forceable_cu = build_catch_up_plan(
            reg,
            forceable_cusec,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers={"sectors": {}},
        )
        ok(
            "cu_sector_target_restore_is_forced",
            "--force" in plan_forceable_cu.target_commands[0],
        )
        portfolio_cu = Sector(
            **{
                **cusec.__dict__,
                "name": "portfolio_layer",
                "force_args": ["--force"],
            }
        )
        plan_portfolio_cu = build_catch_up_plan(
            reg,
            portfolio_cu,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers={"sectors": {}},
        )
        ok(
            "cu_portfolio_missing_target_is_rebuilt_incrementally",
            "--force" not in plan_portfolio_cu.target_commands[0],
        )
        _publish(portfolio_cu, target_cu)
        plan_portfolio_healthy_target = build_catch_up_plan(
            reg,
            portfolio_cu,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers={"sectors": {}},
        )
        ok(
            "cu_portfolio_historical_repair_preserves_healthy_target",
            bool(plan_portfolio_healthy_target.backfill_groups)
            and plan_portfolio_healthy_target.target_commands == [],
        )

        # native backfill route: chunked to <= threshold dates per command,
        # oldest chunk first, per-date post steps preserved too
        cusec_bf = _sector(
            name="cusec_bf",
            publish_glob="cubf/{date}/t.csv",
            backfill_window_days=10,
            backfill=BackfillSpec(
                script="a/bf.py", args_template=["--start", "{from}", "--end", "{to}"], per_date=False
            ),
            daily_post_steps=[
                {"script": "x/post.py", "args_template": ["--asof", "{date}"]},
                {
                    "script": "x/historical.py",
                    "args_template": ["--asof", "{date}", "--context", "production"],
                    "historical_args_template": [
                        "--asof",
                        "{date}",
                        "--context",
                        "historical",
                    ],
                    "historical_only": True,
                },
            ],
        )
        _publish(cusec_bf, "2026-07-06")
        plan_bf = build_catch_up_plan(
            reg, cusec_bf, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        ok(
            "cu_native_backfill_chunked",
            plan_bf.used_native_backfill
            and len(plan_bf.backfill_groups) == 3
            and all(len(dates) <= reg.catch_up_gap_backfill_threshold for dates, _c in plan_bf.backfill_groups),
        )
        ok(
            "cu_native_chunks_oldest_first",
            plan_bf.backfill_groups[0][0][0] == "2026-07-07"
            and plan_bf.backfill_groups[-1][0] == ("2026-07-15", "2026-07-16"),
        )
        first_chunk_dates, first_chunk_commands = plan_bf.backfill_groups[0]
        ok(
            "cu_native_chunk_post_steps",
            sum("post.py" in " ".join(c) for c in first_chunk_commands)
            == len(first_chunk_dates),
        )
        ok(
            "cu_native_historical_only_post_context",
            sum(
                "historical.py" in " ".join(c)
                and "--context historical" in " ".join(c)
                for c in first_chunk_commands
            )
            == len(first_chunk_dates)
            and all("historical.py" not in " ".join(c) for c in plan_bf.target_commands),
        )
        exact_dates_sector = _sector(
            name="cu_exact_dates",
            publish_glob="cuexact/{date}/t.csv",
            backfill_window_days=10,
            native_backfill_min_dates=1,
            backfill=BackfillSpec(
                script="a/bf.py",
                args_template=["--history-dates", "{dates}"],
                per_date=False,
            ),
        )
        _publish(exact_dates_sector, "2026-07-13")
        _publish(exact_dates_sector, "2026-07-15")
        exact_plan = build_catch_up_plan(
            reg,
            exact_dates_sector,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers={"sectors": {}},
        )
        exact_command = " ".join(exact_plan.backfill_groups[0][1][0])
        ok(
            "cu_native_receives_exact_missing_dates_not_range",
            "--history-dates 2026-07-14,2026-07-16" in exact_command,
        )
        # Some native range runners (machinery) rebuild the current target and
        # all historical gaps in one monotone command. They must not be split
        # into regressive chunks or followed by a duplicate current rebuild.
        cusec_cover = _sector(
            name="cusec_cover",
            publish_glob="cucover/{date}/t.csv",
            backfill_window_days=10,
            native_backfill_min_dates=1,
            backfill=BackfillSpec(
                script="a/bf.py",
                args_template=["--start", "{from}", "--asof", "{to}"],
                per_date=False,
                covers_target=True,
            ),
        )
        _publish(cusec_cover, "2026-07-06")
        plan_cover = build_catch_up_plan(
            reg,
            cusec_cover,
            target_cu,
            force=False,
            live_completed_session=target_cu,
            markers={"sectors": {}},
        )
        cover_command = " ".join(plan_cover.backfill_groups[0][1][0])
        ok(
            "cu_native_covers_target_single_monotone_command",
            plan_cover.native_backfill_covers_target
            and len(plan_cover.backfill_groups) == 1
            and "2026-07-07" in cover_command
            and target_cu in cover_command
            and plan_cover.target_commands == [],
        )
        direct_bf_args = argparse.Namespace(
            mode="backfill",
            from_date="2026-07-14",
            to_date="2026-07-16",
        )
        direct_bf_cmds, direct_bf_note = build_sector_commands(
            reg,
            cusec_bf,
            direct_bf_args,
            target_cu,
            {},
        )
        ok(
            "direct_backfill_preserves_daily_post_steps",
            direct_bf_note == "backfill+post_steps"
            and sum("post.py" in " ".join(c) for c in direct_bf_cmds) == 3,
        )
        ok(
            "direct_backfill_uses_historical_post_context",
            sum(
                "historical.py" in " ".join(c)
                and "--context historical" in " ".join(c)
                for c in direct_bf_cmds
            )
            == 3,
        )

        # execution: a failed backfill date NEVER fails the sector/master; the failed
        # date is recorded per-date and its marker failure count persisted
        cus_a = _sector(name="cus_a", publish_glob="cua/{date}/t.csv")
        for d in ("2026-07-06", "2026-07-13"):
            _publish(cus_a, d)
        plan_a = build_catch_up_plan(
            reg, cus_a, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        res_a = run_catch_up_sector(
            reg,
            cus_a,
            plan_a,
            run_dir=run_dir_cu,
            net_sem=None,
            log=lambda _m: None,
            markers_path=markers_path,
            runner=_mk_runner({"2026-07-15"}, cus_a),
        )
        ok("cu_exec_backfill_failure_not_fatal", res_a.status == "PASS_WITH_BACKFILL_GAPS")
        ok("cu_exec_current_book_published", (cu_root / "cua" / target_cu / "t.csv").exists())
        ok(
            "cu_exec_failed_date_recorded",
            res_a.backfill is not None and set(res_a.backfill["failed"]) == {"2026-07-15"},
        )
        ok(
            "cu_exec_backfill_gap_master_pass",
            compute_overall([cus_a], {"cus_a": res_a}, mode="catch-up", gate_ok=True, ignore_gate=False) == "PASS",
        )
        ok("cu_exec_backfill_gap_gate_healthy", "PASS_WITH_BACKFILL_GAPS" in HEALTHY_STATES)
        ok(
            "cu_exec_marker_failure_persisted",
            load_gap_markers(markers_path)["sectors"]["cus_a"]["2026-07-15"]["failures"] == 1,
        )

        # second failing night: up-to-date target + same gap -> auto-tombstone at the
        # fake registry threshold (2), and the next scan skips the tombstoned date
        plan_a2 = build_catch_up_plan(
            reg, cus_a, target_cu, force=False, live_completed_session=target_cu, markers=load_gap_markers(markers_path)
        )
        ok("cu_second_night_only_gap_remains", not plan_a2.target_missing and plan_a2.backfill_dates == ["2026-07-15"])
        res_a2 = run_catch_up_sector(
            reg,
            cus_a,
            plan_a2,
            run_dir=run_dir_cu,
            net_sem=None,
            log=lambda _m: None,
            markers_path=markers_path,
            runner=_mk_runner({"2026-07-15"}, cus_a),
        )
        ok("cu_exec_up_to_date_target_with_gap", res_a2.status == "PASS_WITH_BACKFILL_GAPS")
        ok(
            "cu_exec_marker_auto_permanent",
            load_gap_markers(markers_path)["sectors"]["cus_a"]["2026-07-15"]["permanent"] is True,
        )
        plan_a3 = build_catch_up_plan(
            reg, cus_a, target_cu, force=False, live_completed_session=target_cu, markers=load_gap_markers(markers_path)
        )
        ok(
            "cu_exec_tombstone_respected_next_scan",
            plan_a3.backfill_dates == [] and plan_a3.permanent_gaps == ["2026-07-15"],
        )

        # execution: CURRENT-date failure fails the sector exactly like daily,
        # after best-effort history has already been recovered oldest-first
        cus_b = _sector(name="cus_b", publish_glob="cub/{date}/t.csv")
        for d in ("2026-07-06", "2026-07-13"):
            _publish(cus_b, d)
        plan_b = build_catch_up_plan(
            reg, cus_b, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        res_b = run_catch_up_sector(
            reg,
            cus_b,
            plan_b,
            run_dir=run_dir_cu,
            net_sem=None,
            log=lambda _m: None,
            markers_path=markers_path,
            runner=_mk_runner({target_cu}, cus_b),
        )
        ok("cu_exec_current_failure_fails_sector", res_b.status == "FAIL")
        ok(
            "cu_exec_current_failure_follows_backfill",
            res_b.return_codes[-1] == 1
            and all((cu_root / "cub" / d / "t.csv").exists() for d in ("2026-07-14", "2026-07-15", "2026-07-16")),
        )

        # fully current catch-up -> UP_TO_DATE (healthy), never NOTE/FAIL
        cus_c = _sector(name="cus_c", publish_glob="cuc/{date}/t.csv")
        _publish(cus_c, target_cu)
        plan_c = build_catch_up_plan(
            reg, cus_c, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        ok(
            "cu_plan_empty_when_current",
            plan_c.all_commands == [] and not plan_c.target_missing and not plan_c.target_unhealthy,
        )
        res_c = run_catch_up_sector(
            reg,
            cus_c,
            plan_c,
            run_dir=run_dir_cu,
            net_sem=None,
            log=lambda _m: None,
            markers_path=markers_path,
            runner=lambda _c: 1,
        )  # must never be invoked
        ok("cu_exec_up_to_date_healthy", res_c.status == "UP_TO_DATE" and res_c.return_codes == [])
        ok(
            "cu_up_to_date_master_pass",
            compute_overall([cus_c], {"cus_c": res_c}, mode="catch-up", gate_ok=True, ignore_gate=False) == "PASS",
        )

        # published-but-unverifiable target fails closed
        bad_dir = cu_root / "cud" / target_cu
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "t.csv").write_text("asof_date,oos_score_valid_flag,ticker\n2026-07-10,1,T0\n", encoding="utf-8")
        cus_d = _sector(name="cus_d", publish_glob="cud/{date}/t.csv")
        plan_d = build_catch_up_plan(
            reg, cus_d, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        ok(
            "cu_plan_unhealthy_target_is_rerun",
            plan_d.target_unhealthy and not plan_d.target_missing and bool(plan_d.target_commands),
        )
        res_d = run_catch_up_sector(
            reg,
            cus_d,
            plan_d,
            run_dir=run_dir_cu,
            net_sem=None,
            log=lambda _m: None,
            markers_path=markers_path,
            runner=lambda _c: 0,
        )
        ok("cu_exec_unverifiable_target_fails", res_d.status == "FAIL")

        # The same present-but-unhealthy state passes only after the rerun repairs
        # the current artifact and the normal verification observes that repair.
        cus_d2 = _sector(name="cus_d2", publish_glob="cud2/{date}/t.csv")
        bad_dir2 = cu_root / "cud2" / target_cu
        bad_dir2.mkdir(parents=True, exist_ok=True)
        (bad_dir2 / "t.csv").write_text("asof_date,oos_score_valid_flag,ticker\n2026-07-10,1,T0\n", encoding="utf-8")
        plan_d2 = build_catch_up_plan(
            reg, cus_d2, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        res_d2 = run_catch_up_sector(
            reg,
            cus_d2,
            plan_d2,
            run_dir=run_dir_cu,
            net_sem=None,
            log=lambda _m: None,
            markers_path=markers_path,
            runner=_mk_runner(set(), cus_d2),
        )
        ok(
            "cu_exec_unhealthy_target_repaired",
            res_d2.status == "PASS" and res_d2.return_codes == [0],
        )

        # {date}-partitioned health manifests are verified PER backfill date (a FAIL
        # per-date manifest turns that date into a recorded gap, not a sector FAIL)
        cus_e = _sector(
            name="cus_e",
            publish_glob="cue/{date}/t.csv",
            health=HealthSpec(manifest="cuman/{date}/m.json", status_keys=["acceptance"]),
        )
        _publish(cus_e, "2026-07-13")
        for iso, acc in (
            ("2026-07-17", "PASS"),
            ("2026-07-16", "PASS"),
            ("2026-07-15", "PASS"),
            ("2026-07-14", "FAIL"),
        ):
            meta_dir = cu_root / "cuman" / iso
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / "m.json").write_text(json.dumps({"acceptance": acc, "run_as_of": iso}), encoding="utf-8")
        plan_e = build_catch_up_plan(
            reg, cus_e, target_cu, force=False, live_completed_session=target_cu, markers={"sectors": {}}
        )
        res_e = run_catch_up_sector(
            reg,
            cus_e,
            plan_e,
            run_dir=run_dir_cu,
            net_sem=None,
            log=lambda _m: None,
            markers_path=markers_path,
            runner=_mk_runner(set(), cus_e),
        )
        ok(
            "cu_perdate_manifest_checked",
            res_e.status == "PASS_WITH_BACKFILL_GAPS"
            and res_e.backfill is not None
            and set(res_e.backfill["failed"]) == {"2026-07-14"}
            and "manifest" in res_e.backfill["failed"]["2026-07-14"],
        )

        # JSON publish artifacts (portfolio final_manifest.json) are verified as JSON:
        # compact serialization passes, internal run_as_of mismatch / invalid JSON fail
        jsec = _sector(
            name="jsec",
            publish_glob="jout/{date}/final_manifest.json",
            oos_column=None,
            require_oos_valid=False,
            health=HealthSpec(manifest=None, status_keys=[]),
        )
        for iso, payload in (
            ("2026-07-16", json.dumps({"acceptance": "PASS", "run_as_of": "2026-07-16"}, separators=(",", ":"))),
            ("2026-07-15", json.dumps({"acceptance": "PASS", "run_as_of": "2026-07-10"})),
            ("2026-07-14", "not-json{"),
        ):
            jdir = cu_root / "jout" / iso
            jdir.mkdir(parents=True, exist_ok=True)
            (jdir / "final_manifest.json").write_text(payload, encoding="utf-8")
        ok("json_artifact_compact_verifies", verify_published_artifact_for_date(jsec, "2026-07-16")[0])
        ok("json_artifact_internal_date_mismatch_fails", not verify_published_artifact_for_date(jsec, "2026-07-15")[0])
        ok("json_artifact_invalid_fails", not verify_published_artifact_for_date(jsec, "2026-07-14")[0])

        # require_oos_valid is authoritative: a declared oos_column with the knob
        # false (transportation's sealed zero-overlay shadow) must verify with flag=0
        shadow = _sector(name="shadow", publish_glob="sh/{date}/t.csv", require_oos_valid=False)
        sh_dir = cu_root / "sh" / "2026-07-16"
        sh_dir.mkdir(parents=True, exist_ok=True)
        (sh_dir / "t.csv").write_text("asof_date,oos_score_valid_flag,ticker\n2026-07-16,0,T0\n", encoding="utf-8")
        ok("require_oos_valid_knob_authoritative", not _oos_required(shadow))
        ok("shadow_zero_flag_artifact_verifies", verify_published_artifact_for_date(shadow, "2026-07-16")[0])
    finally:
        _globals["PROJECT_ROOT"] = _saved_cu_root
        import shutil

        shutil.rmtree(cu_root, ignore_errors=True)

    # --- ad-hoc market closures: registry-declared, excluded from the calendar ---
    closure_raw = {**FAKE_REGISTRY, "defaults": {**FAKE_REGISTRY["defaults"], "market_closures": ["2026-07-15"]}}
    ctmp = Path(tempfile.mkdtemp()) / "closure.yaml"
    ctmp.write_text(yaml.safe_dump(closure_raw), encoding="utf-8")
    try:
        creg = load_registry(ctmp)
        ok("closure_parsed", creg.market_closures == ["2026-07-15"])
        ok("closure_not_trading_day", not is_trading_day(date(2026, 7, 15)))
        ok(
            "closure_excluded_from_expected_sessions",
            "2026-07-15" not in trading_dates_in_range(creg, "2026-07-13", "2026-07-17"),
        )
    finally:
        load_registry(tmp)  # restore the plain fake registry's (empty) closure set
    ok("closure_restored_after_reload", is_trading_day(date(2026, 7, 15)))
    duplicate_registry = ctmp.parent / "duplicate_registry.yaml"
    duplicate_registry.write_text(
        "defaults:\n  retries: 1\n  retries: 2\nsectors: []\n",
        encoding="utf-8",
    )
    try:
        load_registry(duplicate_registry)
        ok("registry_duplicate_keys_fail_closed", False)
    except ValueError as exc:
        ok(
            "registry_duplicate_keys_fail_closed",
            "duplicate key 'retries'" in str(exc),
        )
    finally:
        load_registry(tmp)

    # --- explicit --as-of must be a real session ---
    try:
        resolve_target_date("2026-08-08")  # Saturday
        ok("asof_reject_non_trading_day", False)
    except SystemExit:
        ok("asof_reject_non_trading_day", True)

    # --- retry policy: success and TIMEOUT are terminal, other failures retry ---
    ok("timeout_not_retried", not _should_retry_rc(124) and not _should_retry_rc(78) and not _should_retry_rc(0) and _should_retry_rc(1))
    retry_sector = Sector(
        **{
            **reg.by_name("alpha").__dict__,
            "entry_script": "runner.py",
            "retry_args": ["--resume"],
        }
    )
    retry_base = ["python", "runner.py", "--asof", "2026-07-17"]
    ok("retry_args_absent_first_attempt", _command_for_attempt(retry_sector, retry_base, 1) == retry_base)
    ok(
        "retry_args_added_after_failure",
        _command_for_attempt(retry_sector, retry_base, 2) == [*retry_base, "--resume"],
    )
    ok(
        "retry_args_not_duplicated",
        _command_for_attempt(retry_sector, [*retry_base, "--resume"], 2) == [*retry_base, "--resume"],
    )
    post_command = ["python", "post_publish.py", "--asof", "2026-07-17"]
    ok(
        "retry_args_never_added_to_post_step",
        _command_for_attempt(retry_sector, post_command, 2) == post_command,
    )
    ok(
        "master_resume_only_updates_entry_command",
        _commands_for_master_resume(retry_sector, [retry_base, post_command])
        == [[*retry_base, "--resume"], post_command],
    )
    req_lane_failure = _unexpected_lane_failure(_sector(name="req_lane", required=True), RuntimeError("boom"))
    opt_lane_failure = _unexpected_lane_failure(_sector(name="opt_lane", required=False), OSError("disk"))
    ok(
        "unexpected_required_lane_exception_is_fail_visible",
        req_lane_failure.status == "FAIL"
        and req_lane_failure.sector == "req_lane"
        and "RuntimeError:boom" in req_lane_failure.note,
    )
    ok(
        "unexpected_optional_lane_exception_is_nonblocking_visible",
        opt_lane_failure.status == "OPTIONAL_FAIL"
        and opt_lane_failure.sector == "opt_lane"
        and "OSError:disk" in opt_lane_failure.note,
    )

    # --- sealed-manifest gate value preserves the BYPASSED distinction ---
    ok(
        "gate_value_bypassed_preserved",
        _gate_manifest_value(True, False) == "PASS"
        and _gate_manifest_value(False, True) == "BYPASSED"
        and _gate_manifest_value(False, False) == "FAIL",
    )

    # --- PID identity: a recycled PID cannot impersonate a dead holder ---
    ok("holder_identity_no_timestamp_falls_back", _holder_alive(os.getpid(), "") is True)
    if _pid_creation_time_utc(os.getpid()) is not None:
        ok("holder_identity_recycled_pid_dead", _holder_alive(os.getpid(), "2000-01-01T00:00:00+00:00") is False)
        ok(
            "holder_identity_current_pid_alive",
            _holder_alive(os.getpid(), datetime.now(timezone.utc).isoformat(timespec="seconds")) is True,
        )

    # --- finding 8: health-check subset semantics ---
    all_t0 = [s for s in reg.sectors if s.dependency_tier == 0]
    hc_full = health_check(reg, all_t0, "2026-07-17")
    ok("finding8_full_stage1_is_bool", isinstance(hc_full["stage1_ready"], bool) and "subset_ready" in hc_full)
    hc_sub = health_check(reg, [reg.by_name("alpha")], "2026-07-17")  # excludes required beta/defense_x
    ok("finding8_subset_stage1_null", hc_sub["stage1_ready"] is None)
    ok("finding8_subset_note_present", "partial" in hc_sub.get("note", "") and "subset_ready" in hc_sub)

    # --- finding 9: PID-liveness + run-dir uniqueness ---
    ok("finding9_self_pid_alive", _pid_alive(os.getpid()) is True)
    ok("finding9_bogus_pid_dead", _pid_alive(2_000_000_000) is False)
    ok("finding9_none_pid_undeterminable", _pid_alive(None) is None and _pid_alive(0) is None)
    stamp = make_run_stamp()
    ok("finding9_run_stamp_has_pid", f"_p{os.getpid()}" in stamp)
    ok("finding9_run_stamp_subsecond", re.match(r"^\d{8}T\d{6}_\d+Z_p\d+$", stamp) is not None)
    _lockdir = Path(tempfile.mkdtemp())
    dead_lock = _lockdir / "dead.lock"
    dead_lock.write_text("pid=2000000000 started_utc=2000-01-01T00:00:00+00:00\n", encoding="utf-8")
    with OrchestrationLock(dead_lock, stale_after_sec=10**9):  # dead PID -> override even though fresh
        ok("finding9_dead_pid_override", dead_lock.exists())
    live_lock = _lockdir / "live.lock"
    # A genuinely-live holder (matching start time) must NOT be overridden even when old.
    live_started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    live_lock.write_text(f"pid={os.getpid()} started_utc={live_started}\n", encoding="utf-8")
    try:
        with OrchestrationLock(live_lock, stale_after_sec=1):  # alive PID -> must NOT override
            ok("finding9_live_pid_not_overridden", False)
    except RuntimeError:
        ok("finding9_live_pid_not_overridden", True)
    # A RECYCLED pid (live process created long after the recorded holder started)
    # is provably not the holder -> override even with a huge stale threshold.
    if _pid_creation_time_utc(os.getpid()) is not None:
        recycled_lock = _lockdir / "recycled.lock"
        recycled_lock.write_text(f"pid={os.getpid()} started_utc=2000-01-01T00:00:00+00:00\n", encoding="utf-8")
        with OrchestrationLock(recycled_lock, stale_after_sec=10**9):
            ok("finding9_recycled_pid_overridden", True)
    orphan_lock = _lockdir / "orphan.lock"
    orphan_lock.write_text(
        f"pid=2000000000 started_utc=2000-01-01T00:00:00+00:00\nchildren={os.getpid()}\n",
        encoding="utf-8",
    )
    try:
        with OrchestrationLock(orphan_lock, stale_after_sec=1):
            ok("finding9_live_orphan_child_blocks_override", False)
    except RuntimeError:
        ok("finding9_live_orphan_child_blocks_override", True)

    # --- freshness sentinel: parse + validation ---
    fr_def = reg.by_name("defense_x")
    ok("freshness_parse_count", len(fr_def.freshness_probes) == 2)
    fp_borrow = fr_def.freshness_probes[0]
    ok(
        "freshness_parse_fields",
        fp_borrow.name == "borrow_age"
        and fp_borrow.kind == "sqlite_max_date"
        and fp_borrow.tolerance_days == 10
        and fp_borrow.warn_lead_days == 3
        and not fp_borrow.required,
    )
    ok(
        "freshness_parse_required_flag",
        fr_def.freshness_probes[1].required and fr_def.freshness_probes[1].kind == "deadline_schedule",
    )
    ok("freshness_no_probes_default", reg.by_name("alpha").freshness_probes == [])
    for bad_probe, label in (
        ({"name": "x", "kind": "bogus", "target": {"db": "d", "sql": "s"}}, "bad_kind"),
        ({"name": "x", "kind": "sqlite_max_date", "target": {"db": "d"}}, "missing_sql"),
        ({"name": "x", "kind": "manifest_date", "target": {"path": "p"}}, "missing_key"),
        (
            {"name": "x", "kind": "deadline_schedule", "target": {"db": "d", "sql": "s", "cadence": "weekly"}},
            "bad_cadence",
        ),
        (
            {"name": "x", "kind": "sqlite_max_date", "target": {"db": "d", "sql": "s"}, "tolerance_days": -1},
            "neg_tolerance",
        ),
        ({"name": "", "kind": "sqlite_max_date", "target": {"db": "d", "sql": "s"}}, "empty_name"),
    ):
        try:
            _parse_freshness_probes("t", [bad_probe])
            ok(f"freshness_reject_{label}", False)
        except ValueError:
            ok(f"freshness_reject_{label}", True)
    try:
        _parse_freshness_probes(
            "t",
            [
                {"name": "dup", "kind": "manifest_date", "target": {"path": "p", "key": "k"}},
                {"name": "dup", "kind": "manifest_date", "target": {"path": "p", "key": "k"}},
            ],
        )
        ok("freshness_reject_duplicate_names", False)
    except ValueError:
        ok("freshness_reject_duplicate_names", True)

    # --- freshness sentinel: every kind + WARN/STALE/ERROR paths on real fixtures ---
    fr_tmp = Path(tempfile.mkdtemp())
    fdb = fr_tmp / "f.sqlite"
    fcon = sqlite3.connect(fdb)
    fcon.execute("CREATE TABLE obs (d TEXT)")
    fcon.executemany("INSERT INTO obs VALUES (?)", [("2026-08-01",), ("2026-07-20",)])
    fcon.execute("CREATE TABLE empty_obs (d TEXT)")
    fcon.execute("CREATE TABLE q (p TEXT)")
    fcon.execute("INSERT INTO q VALUES ('2026-03-31')")
    fcon.execute("CREATE TABLE sm (s TEXT)")
    fcon.execute("INSERT INTO sm VALUES ('2026-06-15')")
    fcon.execute("CREATE TABLE sm2 (s TEXT)")
    fcon.execute("INSERT INTO sm2 VALUES ('2026-07-15')")
    fcon.commit()
    fcon.close()

    def _mk_probe(**over: Any) -> FreshnessProbe:
        base: dict[str, Any] = dict(
            name="p",
            kind="sqlite_max_date",
            target={"db": str(fdb), "sql": "SELECT MAX(d) FROM obs"},
            tolerance_days=10,
            warn_lead_days=3,
            required=False,
            notes="",
        )
        base.update(over)
        return FreshnessProbe(**base)

    try:
        r_cur = evaluate_freshness_probe(_mk_probe(), "2026-08-05")
        ok(
            "freshness_sqlite_current",
            r_cur["status"] == "CURRENT"
            and r_cur["latest"] == "2026-08-01"
            and r_cur["age_days"] == 4
            and r_cur["threshold"] == 10,
        )
        ok(
            "freshness_sqlite_warn_approaching",
            evaluate_freshness_probe(_mk_probe(), "2026-08-09")["status"] == "WARN_APPROACHING",
        )  # age 8, breach at 11
        ok(
            "freshness_sqlite_stale", evaluate_freshness_probe(_mk_probe(), "2026-08-17")["status"] == "STALE"
        )  # age 16 > 10
        r_badsql = evaluate_freshness_probe(
            _mk_probe(target={"db": str(fdb), "sql": "SELECT MAX(d) FROM missing_table"}), "2026-08-05"
        )
        ok("freshness_sqlite_error_bad_sql", r_badsql["status"] == "ERROR" and bool(r_badsql["detail"]))
        ok(
            "freshness_sqlite_error_missing_db",
            evaluate_freshness_probe(
                _mk_probe(target={"db": str(fr_tmp / "nope.sqlite"), "sql": "SELECT 1"}),
                "2026-08-05",
            )["status"]
            == "ERROR",
        )
        ok(
            "freshness_sqlite_error_null_date",
            evaluate_freshness_probe(
                _mk_probe(target={"db": str(fdb), "sql": "SELECT MAX(d) FROM empty_obs"}),
                "2026-08-05",
            )["status"]
            == "ERROR",
        )

        fman = fr_tmp / "m.json"
        fman.write_text(json.dumps({"meta": {"asof_date": "2026-08-01T00:00:00"}}), encoding="utf-8")
        r_man = evaluate_freshness_probe(
            _mk_probe(kind="manifest_date", target={"path": str(fman), "key": "meta.asof_date"}), "2026-08-05"
        )
        ok("freshness_manifest_current_dotted_key", r_man["status"] == "CURRENT" and r_man["latest"] == "2026-08-01")
        ok(
            "freshness_manifest_error_missing_key",
            evaluate_freshness_probe(
                _mk_probe(kind="manifest_date", target={"path": str(fman), "key": "meta.nope"}),
                "2026-08-05",
            )["status"]
            == "ERROR",
        )
        ok(
            "freshness_manifest_error_missing_file",
            evaluate_freshness_probe(
                _mk_probe(kind="manifest_date", target={"path": str(fr_tmp / "no.json"), "key": "k"}),
                "2026-08-05",
            )["status"]
            == "ERROR",
        )

        # deadline_schedule quarterly (13F shape): Q1 data satisfies until Q2's
        # availability date quarter_end+45+35 = 2026-09-18; warn from 7 days before.
        q_target = {
            "db": str(fdb),
            "sql": "SELECT MAX(p) FROM q",
            "cadence": "quarterly",
            "deadline_days": 45,
            "publication_lag_days": 35,
        }

        def _q_eval(t: str) -> dict[str, Any]:
            return evaluate_freshness_probe(
                _mk_probe(kind="deadline_schedule", target=q_target, tolerance_days=0, warn_lead_days=7), t
            )

        r_q_cur = _q_eval("2026-08-05")
        ok(
            "freshness_deadline_quarterly_current",
            r_q_cur["status"] == "CURRENT" and r_q_cur["threshold"] == "period>=2026-03-31",
        )
        ok("freshness_deadline_quarterly_warn", _q_eval("2026-09-12")["status"] == "WARN_APPROACHING")
        r_q_stale = _q_eval("2026-09-20")
        ok(
            "freshness_deadline_quarterly_stale",
            r_q_stale["status"] == "STALE" and r_q_stale["threshold"] == "period>=2026-06-30",
        )

        # deadline_schedule semi_monthly (FINRA shape): cycle 15th/EOM, pub lag 12
        # + 7d grace -> the 07-15 cycle is required from 08-03.
        def _sm_eval(table: str, t: str) -> dict[str, Any]:
            return evaluate_freshness_probe(
                _mk_probe(
                    kind="deadline_schedule",
                    target={
                        "db": str(fdb),
                        "sql": f"SELECT MAX(s) FROM {table}",
                        "cadence": "semi_monthly",
                        "publication_lag_days": 12,
                    },
                    tolerance_days=7,
                    warn_lead_days=3,
                ),
                t,
            )

        r_sm_stale = _sm_eval("sm", "2026-08-05")  # latest 06-15 < required 07-15
        ok(
            "freshness_deadline_semimonthly_stale",
            r_sm_stale["status"] == "STALE" and r_sm_stale["threshold"] == "period>=2026-07-15",
        )
        ok("freshness_deadline_semimonthly_current", _sm_eval("sm2", "2026-08-05")["status"] == "CURRENT")
        ok(
            "freshness_deadline_semimonthly_warn",  # 07-31 cycle due 08-19; 2 days out
            _sm_eval("sm2", "2026-08-17")["status"] == "WARN_APPROACHING",
        )

        # hard probe timeout -> TimeoutError -> ERROR (exercised with a tiny budget)
        try:
            _call_with_timeout(lambda: time.sleep(0.5), 0.05)
            ok("freshness_timeout_raises", False)
        except TimeoutError:
            ok("freshness_timeout_raises", True)

        # sweep + summary line + per-sector scoping
        fr_logs: list[str] = []
        fsen = _sector(name="fsen", freshness_probes=[_mk_probe()])
        fmap = evaluate_freshness([fsen, _sector(name="noprobe")], "2026-08-05", fr_logs.append)
        ok("freshness_eval_map_scoped", list(fmap) == ["fsen"] and fmap["fsen"][0]["status"] == "CURRENT")
        ok("freshness_summary_line_logged", any(msg.startswith("freshness: ") for msg in fr_logs))
        no_probe_logs: list[str] = []
        ok(
            "freshness_eval_empty_without_probes",
            evaluate_freshness([_sector(name="noprobe")], "2026-08-05", no_probe_logs.append) == {}
            and no_probe_logs == [],
        )

        # consequences: a required probe that is STALE **or ERROR** forces FAIL with a
        # FRESHNESS_BLOCKING note; everything else (non-required STALE/ERROR, WARN) is
        # surveillance-only, and blocking is scoped to the run's selected sectors.
        fr_state = {
            "defense_x": [{"probe": "13f_period", "required": True, "status": "STALE"}],
            "mach_x": [
                {"probe": "finra_cycle", "required": False, "status": "STALE"},
                {"probe": "borrow_age", "required": False, "status": "ERROR"},
            ],
        }
        fr_results = {"defense_x": RunResult("defense_x", "PASS")}
        cons_logs: list[str] = []
        overall_fr, blocking_req = apply_freshness_consequences(fr_state, fr_results, "PASS", cons_logs.append)
        ok(
            "freshness_required_stale_forces_fail",
            overall_fr == "FAIL" and blocking_req == ["defense_x:13f_period=STALE"],
        )
        ok(
            "freshness_required_stale_note_marked",
            "FRESHNESS_BLOCKING: 13f_period=STALE" in fr_results["defense_x"].note,
        )
        ok("freshness_required_stale_logged_loudly", any("FRESHNESS ERROR" in m for m in cons_logs))
        overall_err, err_req = apply_freshness_consequences(
            {"defense_x": [{"probe": "13f_period", "required": True, "status": "ERROR"}]}, {}, "PASS", cons_logs.append
        )
        ok("freshness_required_error_blocks", overall_err == "FAIL" and err_req == ["defense_x:13f_period=ERROR"])
        overall_nr, blocking_nr = apply_freshness_consequences(
            {"mach_x": fr_state["mach_x"]}, {}, "PASS", cons_logs.append
        )
        ok("freshness_nonrequired_stale_never_blocks", overall_nr == "PASS" and blocking_nr == [])
        overall_scoped, scoped_req = apply_freshness_consequences(
            fr_state, {}, "PASS", cons_logs.append, selected_names={"mach_x"}
        )
        ok("freshness_unselected_sector_never_blocks", overall_scoped == "PASS" and scoped_req == [])
        overall_keep, _ = apply_freshness_consequences(fr_state, {}, "FAIL", cons_logs.append)
        ok("freshness_existing_fail_kept", overall_keep == "FAIL")

        # no-probes backward compatibility: manifest rows are byte-identical
        rows_plain = _result_rows({"a": RunResult("a", "PASS")})
        ok("freshness_rows_no_key_without_probes", "freshness" not in rows_plain[0])
        rows_fr = _result_rows({"a": RunResult("a", "PASS")}, freshness={"a": [{"probe": "p", "status": "CURRENT"}]})
        ok("freshness_rows_attached_when_present", rows_fr[0]["freshness"][0]["probe"] == "p")
    finally:
        import shutil

        shutil.rmtree(fr_tmp, ignore_errors=True)

    # --- real registry loads and every entry_script exists ---
    real = load_registry(DEFAULT_REGISTRY)
    ok("real_registry_sectors", len(real.sectors) == 10)
    try:
        validate_registry_paths(real)
        ok("real_registry_all_script_paths_exist", True)
    except ValueError:
        ok("real_registry_all_script_paths_exist", False)
    ok(
        "real_industrials_order",
        real.group_order.get("industrials") == ["defense", "machinery", "transportation"],
    )
    ok("real_portfolio_tier1", real.by_name("portfolio_layer").dependency_tier == 1)
    # M10: publish/health gate on end-of-run artifacts, with the advisory verdict healthy.
    rport = real.by_name("portfolio_layer")
    ok(
        "real_portfolio_retry_reuses_risk_price_data",
        rport.retry_args == ["--reuse-risk-price-data"],
    )
    ok("m10_real_portfolio_publish_final_manifest", rport.publish_glob.endswith("/final/final_manifest.json"))
    ok(
        "m10_real_portfolio_health_orchestration_meta",
        rport.health.manifest is not None
        and rport.health.manifest.endswith("orchestration_meta.json")
        and rport.health.status_keys == ["acceptance"]
        and "PASS_WITH_ADVISORY_WARNINGS" in rport.health.healthy_values
        and "PASS_WITH_DEFERRED" in rport.health.healthy_values,
    )
    rdef = real.by_name("defense")
    ok("real_defense_weekly_no_promotion", not rdef.weekly_pre_steps)
    rbio = real.by_name("biotech")
    ok("real_biotech_oos_column", rbio.oos_column == "oos_score_valid_flag" and rbio.require_oos_valid)
    ok(
        "real_biotech_backfill_uses_exact_master_dates",
        rbio.backfill is not None
        and "--history-dates" in rbio.backfill.args_template
        and "{dates}" in rbio.backfill.args_template,
    )
    rmed = real.by_name("med_devices")
    ok("real_med_devices_require_oos", rmed.require_oos_valid)
    ok("real_med_current_restore_is_incremental", "--force-refresh" not in rmed.force_args)
    # finding 1: daily self-certifies within the replay window, then script 76 records provenance.
    ok("real_med_oos_score_valid_flag", "--oos-score-valid" in rmed.args_template)
    ok(
        "real_med_post_step_76",
        any("76_mark_med_device_oos_provenance" in str(step.get("script", "")) for step in rmed.daily_post_steps),
    )
    med_source_step = next(
        step
        for step in rmed.daily_post_steps
        if "81_build_med_device_source_incorporation" in str(step.get("script", ""))
    )
    ok(
        "real_med_source_incorporation_historical_only",
        med_source_step.get("historical_only") is True
        and "historical" in list(med_source_step.get("historical_args_template") or [])
        and all(
            "81_build_med_device_source_incorporation" not in " ".join(command)
            for command in daily_post_commands(rmed, "2026-08-28")
        )
        and any(
            "--policy-context historical" in " ".join(command)
            for command in daily_post_commands(
                rmed,
                "2026-08-27",
                historical=True,
            )
        ),
    )
    # finding 7: 63 rebuild sits in the repair rebuild chain, before the institutional-flow rebuild it feeds.
    ok(
        "real_med_repair_has_63",
        rmed.repair is not None and "63_rebuild_sec13f_common_shares" in rmed.repair.rebuild_steps,
    )
    ok(
        "real_med_63_before_flow",
        rmed.repair is not None
        and rmed.repair.rebuild_steps.index("63_rebuild_sec13f_common_shares")
        < rmed.repair.rebuild_steps.index("58_build_institutional_flow_features"),
    )
    rsemi = real.by_name("semiconductors")
    ok(
        "real_semi_backfill_daily_survivorship",
        rsemi.backfill is not None
        and "daily" in rsemi.backfill.args_template
        and "--include-stage11-survivorship-panel" in rsemi.backfill.args_template,
    )
    ok("real_semi_repair_rebuild", rsemi.repair is not None and bool(rsemi.repair.rebuild_steps))
    rmach = real.by_name("machinery")
    ok(
        "real_machinery_uses_native_gap_backfill",
        rmach.native_backfill_min_dates == 1
        and rmach.backfill is not None
        and bool(rmach.backfill.script)
        and rmach.backfill.covers_target
        and "--overwrite-outputs" in rmach.backfill.args_template,
    )
    # freshness sentinel seeds: probes parse and carry the intended shape.
    ok(
        "real_defense_freshness_probe_names",
        {p.name for p in rdef.freshness_probes}
        == {"ibkr_borrow_age", "finra_short_interest_cycle", "institutional_13f_period"},
    )
    p13f = next(p for p in rdef.freshness_probes if p.name == "institutional_13f_period")
    ok(
        "real_defense_13f_probe_required_deadline_aware",
        p13f.required
        and p13f.kind == "deadline_schedule"
        and p13f.target.get("cadence") == "quarterly"
        and int(p13f.target.get("deadline_days", 0)) == 45
        and int(p13f.target.get("publication_lag_days", 0)) >= 30
        and p13f.warn_lead_days == 7,
    )
    rborrow = next(p for p in rdef.freshness_probes if p.name == "ibkr_borrow_age")
    ok(
        "real_defense_borrow_probe_tolerance",
        rborrow.tolerance_days == 10 and rborrow.warn_lead_days == 3 and not rborrow.required,
    )
    rmach = real.by_name("machinery")
    ok(
        "real_machinery_freshness_probes_not_required",
        len(rmach.freshness_probes) == 3 and not any(p.required for p in rmach.freshness_probes),
    )
    rtrans = real.by_name("transportation")
    # require_oos_valid: false is authoritative for the sealed zero-overlay shadow
    # lane -- transportation must never be marked OPTIONAL_FAIL for flag=0 tables.
    ok(
        "real_transportation_oos_not_required",
        rtrans.oos_column == "oos_score_valid_flag" and not rtrans.require_oos_valid and not _oos_required(rtrans),
    )
    # Publish-convention epoch: the portfolio's final-manifest convention starts at the
    # first date a final_manifest.json exists on disk (2026-07-02); the repointed glob
    # must not manufacture missing history before it.
    ok("real_portfolio_publish_epoch", rport.publish_epoch == "2026-07-02")
    ok("real_permanent_gap_threshold", real.permanent_gap_after_failures == 3)
    ok("real_no_adhoc_closures_declared", real.market_closures == [])
    ok(
        "real_transportation_borrow_probe",
        any(p.name == "ibkr_borrow_age" and p.tolerance_days == 10 for p in rtrans.freshness_probes),
    )
    rfinra = next(p for p in rtrans.freshness_probes if p.name == "finra_short_interest_cycle")
    ok(
        "real_transportation_finra_bimonthly",
        rfinra.kind == "deadline_schedule"
        and rfinra.target.get("cadence") == "semi_monthly"
        and rfinra.warn_lead_days == 3,
    )
    ok(
        "real_portfolio_freshness_probes",
        {(p.name, p.kind, p.tolerance_days, p.warn_lead_days) for p in rport.freshness_probes}
        == {("macro_serving_age", "sqlite_max_date", 5, 2), ("holdings_ledger_age", "sqlite_max_date", 7, 2)},
    )
    ok(
        "real_freshness_probes_never_required_except_defense_13f",
        all(
            not p.required
            for s in real.sectors
            for p in s.freshness_probes
            if not (s.name == "defense" and p.name == "institutional_13f_period")
        ),
    )

    # --- timeout consistency: every sector ceiling must dominate its documented
    # per-step ceiling (2026-08-05 biotech post-mortem: the 21600s default killed
    # an attempt whose single-step budget was 28800s). Sectors without a
    # documented per-step ceiling (not in STEP_CEILING_SOURCES) assert nothing.
    for rsec in real.sectors:
        ceiling = documented_step_ceiling_sec(rsec.name)
        if ceiling is not None:
            ok(
                f"timeout_covers_documented_step_ceiling_{rsec.name}",
                rsec.timeout_sec >= ceiling,
            )
    # The two known declarers must stay resolvable so the assertion above cannot
    # silently degrade into a no-op if a config key is renamed/moved.
    ok("timeout_ceiling_resolvable_biotech", (documented_step_ceiling_sec("biotech") or 0) >= 28800)
    ok("timeout_ceiling_resolvable_portfolio", (documented_step_ceiling_sec("portfolio_layer") or 0) >= 7200)
    ok("timeout_ceiling_unknown_sector_none", documented_step_ceiling_sec("nope") is None)

    print(f"SELFTEST PASS: {len(checks)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
