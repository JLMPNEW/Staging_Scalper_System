#!/usr/bin/env python3
"""Master cross-sector orchestrator for the scalper staging system.

Runs the seven sector refreshes (biotech, med_devices, semiconductors,
software_infrastructure, technology_hardware, defense, machinery) and the
Tier-1 portfolio layer from a single command, reading orchestration/registry.yaml
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
except ImportError as exc:  # pragma: no cover - yaml is a hard dependency here
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc


ORCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ORCH_DIR.parent
DEFAULT_REGISTRY = ORCH_DIR / "registry.yaml"
RUNS_ROOT = ORCH_DIR / "runs"                 # live master manifests (resume source)
DRYRUN_RUNS_ROOT = ORCH_DIR / "runs_dryrun"   # dry-run manifests (NEVER consulted for resume)
ORCH_LOCK_PATH = ORCH_DIR / ".orchestrator.lock"
PY = sys.executable

# Market-close guard: the latest completed trading session is the previous
# trading day before ~17:00 ET (mirrors biotech 24 + portfolio same_day bar seal).
MARKET_TZ = "America/New_York"
MARKET_CLOSE_ET = dt_time(17, 0)
DEFAULT_LOCK_STALE_SEC = 6 * 3600  # override a lock older than this (crash recovery)

# States a required sector must be in for the gate / overall acceptance to pass.
HEALTHY_STATES = frozenset({"PASS", "DRY_RUN", "SKIPPED_RESUME", "UP_TO_DATE"})


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
    note: str = ""


@dataclass(frozen=True)
class HealthSpec:
    manifest: str | None
    status_keys: list[str]


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
    health: HealthSpec
    backfill: BackfillSpec | None
    repair: RepairSpec | None
    weekly_pre_steps: list[dict[str, Any]]
    daily_post_steps: list[dict[str, Any]]
    timeout_sec: int
    retries: int


@dataclass(frozen=True)
class Registry:
    sectors: list[Sector]
    group_order: dict[str, list[str]]
    max_concurrent_network_lanes: int
    catch_up_gap_backfill_threshold: int
    catch_up_window_days: int
    repair_days: int
    calendar_reference_sectors: list[str]

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


def load_registry(path: Path) -> Registry:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "sectors" not in raw:
        raise ValueError(f"registry {path} missing top-level 'sectors'")
    defaults = raw.get("defaults") or {}
    sectors: list[Sector] = []
    for entry in raw["sectors"]:
        health_raw = entry.get("health") or {}
        health = HealthSpec(
            manifest=health_raw.get("manifest"),
            status_keys=list(health_raw.get("status_keys") or []),
        )
        backfill_raw = entry.get("backfill")
        backfill = None
        if backfill_raw and backfill_raw.get("script"):
            backfill = BackfillSpec(
                script=str(backfill_raw["script"]),
                args_template=list(backfill_raw.get("args_template") or []),
                per_date=bool(backfill_raw.get("per_date", False)),
                note=str(backfill_raw.get("note") or ""),
            )
        elif backfill_raw and backfill_raw.get("note"):
            backfill = BackfillSpec(script="", args_template=[], per_date=False, note=str(backfill_raw["note"]))
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
                name=str(entry["name"]),
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
                health=health,
                backfill=backfill,
                repair=repair,
                weekly_pre_steps=list(entry.get("weekly_pre_steps") or []),
                daily_post_steps=list(entry.get("daily_post_steps") or []),
                timeout_sec=int(entry.get("timeout_sec", defaults.get("timeout_sec", 21600))),
                retries=int(entry.get("retries", defaults.get("retries", 1))),
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
        if sector.staleness_tolerance_days < 0:
            raise ValueError(f"sector {sector.name}: staleness_tolerance_days must be >= 0")
        if sector.publish_glob.count("{date}") != 1:
            raise ValueError(f"sector {sector.name}: publish_glob must contain exactly one {{date}}")
    group_order = {str(k): list(v) for k, v in (raw.get("group_order") or {}).items()}
    ordered_names = [str(name) for ordered in group_order.values() for name in ordered]
    unknown_ordered = sorted(set(ordered_names) - set(names))
    duplicate_ordered = sorted({name for name in ordered_names if ordered_names.count(name) > 1})
    if unknown_ordered or duplicate_ordered:
        raise ValueError(
            f"invalid group_order: unknown={unknown_ordered} duplicated={duplicate_ordered}"
        )
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
        raise ValueError(
            "catch_up_gap_backfill_threshold and catch_up_window_days must both be >= 1"
        )
    return Registry(
        sectors=sectors,
        group_order=group_order,
        max_concurrent_network_lanes=lanes,
        catch_up_gap_backfill_threshold=catch_up_threshold,
        catch_up_window_days=catch_up_window,
        repair_days=repair_days,
        calendar_reference_sectors=calendar_refs,
    )


def validate_registry_paths(reg: Registry) -> None:
    missing: list[str] = []
    for sector in reg.sectors:
        candidates = [sector.entry_script]
        if sector.backfill and sector.backfill.script:
            candidates.append(sector.backfill.script)
        candidates.extend(
            str(step.get("script") or "")
            for step in [*sector.weekly_pre_steps, *sector.daily_post_steps]
        )
        for relative in candidates:
            if relative and not (PROJECT_ROOT / relative).is_file():
                missing.append(f"{sector.name}:{relative}")
    if missing:
        raise ValueError(f"registry references missing scripts: {missing}")


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
    holidays.add(_observed(date(year, 1, 1), is_new_year=True))         # New Year's Day
    holidays.add(_nth_weekday(year, 1, 0, 3))                            # MLK (3rd Mon Jan)
    holidays.add(_nth_weekday(year, 2, 0, 3))                           # Washington's Birthday (3rd Mon Feb)
    holidays.add(_easter_sunday(year) - timedelta(days=2))              # Good Friday
    holidays.add(_last_weekday(year, 5, 0))                             # Memorial Day (last Mon May)
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))                     # Juneteenth
    holidays.add(_observed(date(year, 7, 4)))                          # Independence Day
    holidays.add(_nth_weekday(year, 9, 0, 1))                          # Labor Day (1st Mon Sep)
    holidays.add(_nth_weekday(year, 11, 3, 4))                         # Thanksgiving (4th Thu Nov)
    holidays.add(_observed(date(year, 12, 25)))                        # Christmas
    return holidays


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in us_market_holidays(d.year)


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


def _missing_from_expected(published: set[str], expected: list[str]) -> list[str]:
    return sorted(d for d in expected if d not in published)


def missing_trading_dates(reg: Registry, sector: Sector, target: str) -> list[str]:
    """Trading dates this sector has not published within the catch-up window, up to
    `target` -- including internal gaps, not just dates after the last published one.

    The window is [max(first_published<=target, target - catch_up_window_days),
    target] so a hole a few sessions back is surfaced while ancient sparse
    calibration history is not dragged into a routine catch-up.
    """
    published = set(sector_published_dates(sector))
    published_on_or_before = sorted(d for d in published if d <= target)
    window_start_lookback = (_to_date(target) - timedelta(days=reg.catch_up_window_days)).isoformat()
    if not published_on_or_before:
        window_start = target
    else:
        window_start = max(published_on_or_before[0], window_start_lookback)
    if window_start > target:
        return []
    expected = trading_dates_in_range(reg, window_start, target)
    return _missing_from_expected(published, expected)


def resolve_target_date(requested: str | None, *, now_utc: datetime | None = None) -> str:
    if requested:
        return parse_iso(requested)
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


def daily_post_commands(sector: Sector, iso_date: str) -> list[list[str]]:
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
        script = str(step["script"])
        cmds.append([PY, str(PROJECT_ROOT / script)] + _sub(list(step.get("args_template") or []), {"date": iso_date}))
    return cmds


def backfill_commands(sector: Sector, frm: str, to: str, reg: Registry) -> tuple[list[list[str]], str]:
    """Return (commands, note). Empty commands with a note => nothing native to run."""
    if sector.backfill is None or not sector.backfill.script:
        note = sector.backfill.note if sector.backfill else "no native backfill entry"
        return [], note
    if sector.backfill.per_date:
        dates = trading_dates_in_range(reg, frm, to)
        cmds = [
            [PY, str(PROJECT_ROOT / sector.backfill.script)] + _sub(sector.backfill.args_template, {"date": d, "from": frm, "to": to})
            for d in dates
        ]
        return cmds, ""
    cmd = [PY, str(PROJECT_ROOT / sector.backfill.script)] + _sub(sector.backfill.args_template, {"from": frm, "to": to})
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
    return command


def catch_up_commands(
    reg: Registry,
    sector: Sector,
    target: str,
    *,
    force: bool,
    live_completed_session: str | None = None,
) -> tuple[list[list[str]], list[str]]:
    missing = missing_trading_dates(reg, sector, target)
    if not missing:
        return [], []
    if len(missing) > reg.catch_up_gap_backfill_threshold and sector.backfill and sector.backfill.script:
        cmds, _ = backfill_commands(sector, missing[0], missing[-1], reg)
        return cmds, missing
    commands: list[list[str]] = []
    live_session = live_completed_session or latest_completed_trading_session()
    for iso_date in missing:
        commands.append(
            _catch_up_daily_command(
                sector,
                iso_date,
                force=force,
                live_completed_session=live_session,
            )
        )
        # Catch-up is a sequence of ordinary dated publishes. Preserve the exact daily
        # post-publish contract (notably med-devices script 76 provenance) for every date.
        commands.extend(daily_post_commands(sector, iso_date))
    return commands, missing


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
                    # PID-liveness beats age, and live descendants keep ownership closed.
                    recorded_pid = self._recorded_pid()
                    alive = _pid_alive(recorded_pid) if recorded_pid is not None else None
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
                alive = _pid_alive(recorded_pid) if recorded_pid is not None else None
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


def _count_oos_valid(path: Path, sector: Sector) -> tuple[int, int]:
    """(oos_valid_or_gate_rows, total_rows) for the published table."""
    if not path.exists():
        return 0, 0
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
    rel = sector.health.manifest.replace("{date}", iso_date or "")
    path = PROJECT_ROOT / rel
    if not path.exists():
        return "MISSING", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "UNREADABLE", ""
    verdicts = [str(data.get(key, "")).upper() for key in sector.health.status_keys]
    status = "PASS" if verdicts and all(v == "PASS" for v in verdicts) else "FAIL" if verdicts else "UNKNOWN"
    return status, _manifest_asof(data)


def _oos_required(sector: Sector) -> bool:
    return sector.require_oos_valid or bool(sector.oos_column) or bool(sector.gate_column)


# Recognized "as-of" date columns in a published table, most-specific first. Every
# sector's published CSV carries exactly one of these (asof_date for the sector rank
# tables / score packs, as_of_date for the portfolio stocks_scores.csv).
_DATE_COLUMN_CANDIDATES = (
    "asof_date", "as_of_date", "asof", "as_of", "run_as_of", "target_date", "trade_date", "session_date", "date",
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


def verify_published_artifact_for_date(
    sector: Sector,
    iso_date: str,
    *,
    verify_manifest: bool = True,
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
    valid, total = _count_oos_valid(artifact, sector)
    if total <= 0:
        reasons.append(f"artifact has 0 rows: {artifact}")
    if _oos_required(sector) and valid <= 0:
        reasons.append("no oos/gate-valid rows where required")
    date_checked, date_ok, date_detail = _csv_date_column_matches(artifact, iso_date)
    if date_checked and not date_ok:
        reasons.append(f"internal as-of column mismatch: {date_detail}")
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
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    sector: str
    # PASS | FAIL | OPTIONAL_FAIL | DRY_RUN | SKIPPED_RESUME | SKIPPED_GATE
    #   | UP_TO_DATE | NOTE | UNKNOWN | RUNNING
    status: str = "PENDING"
    commands: list[list[str]] = field(default_factory=list)
    return_codes: list[int] = field(default_factory=list)
    elapsed_sec: float = 0.0
    artifact: str = ""
    sha256: str = ""
    command_hash: str = ""
    content_hash: str = ""
    note: str = ""


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


def _run_one(sector: Sector, command: list[str], *, net_sem: threading.Semaphore | None, logfile) -> int:
    attempts = sector.retries + 1
    rc = 1
    for attempt in range(1, attempts + 1):
        acquired = False
        if sector.network and net_sem is not None:
            net_sem.acquire()
            acquired = True
        proc: subprocess.Popen[bytes] | None = None
        tracker = _ACTIVE_ORCHESTRATION_LOCK
        try:
            logfile.write(f"\n=== attempt {attempt}/{attempts}: {subprocess.list2cmdline(command)}\n")
            logfile.flush()
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            child_env = os.environ.copy()
            # Portfolio Tier-1 uses the same global lock as this master. Pass a
            # verifiable owner token so the child may borrow the master's lock;
            # direct portfolio invocations without this token must acquire the
            # lock themselves and cannot race a sector refresh.
            child_env["STAGING_ORCHESTRATOR_PID"] = str(os.getpid())
            proc = subprocess.Popen(
                command,
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
        if rc == 0:
            return 0
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


def _result_rows(results: dict[str, RunResult]) -> list[dict[str, Any]]:
    return [
        {
            "sector": r.sector, "status": r.status, "note": r.note, "elapsed_sec": r.elapsed_sec,
            "return_codes": r.return_codes, "artifact": r.artifact, "sha256": r.sha256,
            "command_hash": r.command_hash, "content_hash": r.content_hash,
            "commands": [subprocess.list2cmdline(c) for c in r.commands],
        }
        for r in results.values()
    ]


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
        if _pid_alive(pid) is not False:
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
        cmds, missing = catch_up_commands(reg, sector, target, force=args.force)
        note = f"missing_dates={len(missing)}" if missing else "up_to_date"
        return cmds, note
    # daily
    daily_cmds: list[list[str]] = []
    if args.cadence == "weekly":
        daily_cmds += weekly_pre_commands(sector, target)
    daily_cmds.append(daily_command(sector, target, force=args.force))
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
    if n < 1:
        raise ValueError(f"last_n_dates requires n >= 1, got {n}")
    known = [d for d in known_trading_dates(reg) if d <= target]
    if len(known) >= n:
        return known[-n:]
    out = set(known)
    cur = _to_date(target)
    while len(out) < n and cur > _to_date("2000-01-01"):
        if is_trading_day(cur):
            out.add(cur.isoformat())
        cur -= timedelta(days=1)
    return sorted(out)[-n:]


def finalize_result(
    sector: Sector,
    res: RunResult,
    *,
    target: str,
    mode: str,
    dry_run: bool,
    catch_up_dates: list[str] | None = None,
) -> RunResult:
    """Apply strict artifact verification (daily/catch-up) and optional-sector downgrade.

    Daily verifies the single target date. Catch-up verifies EVERY date it attempted
    (finding 3): each filled gap must have an artifact that exists, has rows, carries
    oos/gate-valid rows where required, and whose internal as-of column matches that
    folder date. A single unverified filled date fails the sector -- catch-up is not
    'done' just because the newest date landed.
    """
    if not dry_run and res.status in {"PASS", "UP_TO_DATE"}:
        reasons: list[str] = []
        if mode == "daily":
            _, reasons = verify_published_artifact(sector, target)
        elif mode == "catch-up":
            dates_to_verify = catch_up_dates if catch_up_dates else [target]
            # Most sector health manifests are global "latest run" files rather than
            # date-partitioned artifacts. After a multi-date catch-up they describe only
            # the final command, so comparing that one manifest date against every earlier
            # gap creates false failures. Verify every CSV semantically and apply the global
            # manifest exactly once, to the final attempted date.
            manifest_date = dates_to_verify[-1]
            for iso_date in dates_to_verify:
                ok_date, date_reasons = verify_published_artifact_for_date(
                    sector,
                    iso_date,
                    verify_manifest=iso_date == manifest_date,
                )
                if not ok_date:
                    reasons.extend(date_reasons)
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
    empty_status = "UP_TO_DATE" if args.mode == "catch-up" else "NOTE"

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
            # Capture the missing dates BEFORE running so catch-up can verify every date it
            # attempts (finding 3); recomputing post-run would show them as no-longer-missing.
            catch_up_dates = missing_trading_dates(reg, sector, target) if args.mode == "catch-up" else None
            commands, note = build_sector_commands(reg, sector, args, target, repair_map)
            cmd_hash = _command_hash(commands)
            content_hash = _content_hash(sector, args.registry)
            if (
                args.resume
                and not args.dry_run
                and resume_match(resume_records, sector, target, args.mode, cmd_hash, content_hash)
            ):
                res = RunResult(sector=sector.name, status="SKIPPED_RESUME", commands=commands,
                                command_hash=cmd_hash, content_hash=content_hash,
                                note="resume: prior PASS matched target/mode/command/content/artifact-sha")
                _set_artifact(sector, res, target, dry_run=False)
                record_progress(res)
                log(f"  [{sector.name}] SKIPPED_RESUME (prior PASS matched)")
                continue
            res = run_commands(sector, commands, run_dir=run_dir, net_sem=net_sem,
                               dry_run=args.dry_run, empty_status=empty_status, log=log)
            res.note = note
            res.command_hash = cmd_hash
            res.content_hash = content_hash
            _set_artifact(sector, res, target, dry_run=args.dry_run)
            finalize_result(sector, res, target=target, mode=args.mode, dry_run=args.dry_run,
                            catch_up_dates=catch_up_dates)
            record_progress(res)

    workers = max(1, len(lanes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_lane, members) for members in lanes]
        for fut in concurrent.futures.as_completed(futures):
            fut.result()
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
    p.add_argument("--as-of", dest="as_of", default=None,
                   help="Target trading date YYYY-MM-DD (default: latest COMPLETED trading session, close-aware).")
    p.add_argument("--catch-up", action="store_true", help="Run every missing trading date per sector through the target.")
    p.add_argument("--repair", default="", help="Repair spec: 'sector[:step],sector2,...' (re-run IB/network steps + rebuild dependents).")
    p.add_argument("--repair-days", type=int, default=None, help="Trailing dates a --repair pass re-runs (>=1; default from registry).")
    p.add_argument("--from", dest="from_date", default="", help="Backfill start date YYYY-MM-DD.")
    p.add_argument("--to", dest="to_date", default="", help="Backfill end date YYYY-MM-DD.")
    p.add_argument("--cadence", choices=["daily", "weekly"], default="daily", help="weekly runs each sector's weekly_pre_steps before the daily publish (none by default).")
    p.add_argument("--only-sectors", default="", help="Comma-separated registry sector names to include.")
    p.add_argument("--skip-sectors", default="", help="Comma-separated registry sector names to exclude.")
    p.add_argument("--force", action="store_true", help="Forward each sector's force flag.")
    p.add_argument("--resume", action="store_true", help="Skip sectors whose prior PASS matches (target, mode, command-hash) with a live artifact.")
    p.add_argument("--dry-run", action="store_true", help="Print the command matrix; execute nothing (manifest isolated under runs_dryrun/).")
    p.add_argument("--ignore-gate", action="store_true", help="Run portfolio even if the Tier-0 gate fails (required for partial runs that exclude required sectors).")
    p.add_argument("--lock-stale-sec", type=int, default=DEFAULT_LOCK_STALE_SEC, help="Override a global orchestration lock older than this many seconds.")
    p.add_argument("--selftest", action="store_true", help="In-process validation of registry/date-math/scheduling/repair; no subprocess.")
    return p.parse_args(argv)


def resolve_mode(args: argparse.Namespace) -> None:
    """Resolve the effective mode and reject conflicting mode flags (finding 14)."""
    explicit_backfill = args.mode == "backfill"
    conflicts = [name for name, on in (("--repair", bool(args.repair)), ("--catch-up", args.catch_up), ("--mode backfill", explicit_backfill)) if on]
    if len(conflicts) > 1:
        raise SystemExit(f"conflicting mode flags: {conflicts}; choose exactly one of --repair / --catch-up / --mode backfill")
    if args.cadence == "weekly" and (args.repair or args.catch_up or explicit_backfill):
        raise SystemExit("--cadence weekly only applies to daily mode")
    if args.repair:
        args.mode = "repair"
    elif args.catch_up:
        args.mode = "catch-up"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return run_selftest()
    reg = load_registry(args.registry)
    validate_registry_paths(reg)
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

    log(f"orchestrator mode={args.mode} cadence={args.cadence} target={target} "
        f"sectors={[s.name for s in selected]} dry_run={args.dry_run}")

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
            {"sector": name, "status": "DRY_RUN", "command_hash": _command_hash(cmds),
             "commands": [subprocess.list2cmdline(c) for c in cmds], "note": note}
            for name, (cmds, note) in matrix.items()
        ]
        write_master_manifest(
            run_dir,
            {"run_stamp": run_stamp, "mode": args.mode, "cadence": args.cadence, "target": target,
             "dry_run": True, "sectors": rows},
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
            {"run_stamp": run_stamp, "mode": args.mode, "cadence": args.cadence, "target": target,
             "master_pid": os.getpid(), "dry_run": False, "acceptance": "RUNNING",
             "tier0_gate": "RUNNING", "sectors": []},
        )
        results: dict[str, RunResult] = {}
        overall = "FAIL"
        gate_ok = False
        failing: list[str] = []
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
                    if sector.dependency_tier == 0 and sector.required and sector.name not in results and sector.name not in selected_names:
                        results[sector.name] = RunResult(sector=sector.name, status="UNKNOWN",
                                                         note="excluded from selection; required for the Tier-0 gate")
            log(f"tier0 gate: {'PASS' if gate_ok else 'FAIL'} (failing_required={failing})")

            # Tier 1 (portfolio) only after the gate. It never runs in repair mode: the repair
            # selection excludes portfolio (repair: null), so tier1_selected is empty there.
            if tier1_selected and args.mode in {"daily", "catch-up"}:
                if gate_ok or args.ignore_gate:
                    for sector in tier1_selected:
                        catch_up_dates = missing_trading_dates(reg, sector, target) if args.mode == "catch-up" else None
                        commands, note = build_sector_commands(reg, sector, args, target, repair_map)
                        res = run_commands(sector, commands, run_dir=run_dir, net_sem=net_sem,
                                           dry_run=False, empty_status="NOTE", log=log)
                        res.note = note
                        res.command_hash = _command_hash(commands)
                        res.content_hash = _content_hash(sector, args.registry)
                        _set_artifact(sector, res, target, dry_run=False)
                        finalize_result(sector, res, target=target, mode=args.mode, dry_run=False,
                                        catch_up_dates=catch_up_dates)
                        results[sector.name] = res
                        write_master_manifest(
                            run_dir,
                            {"run_stamp": run_stamp, "mode": args.mode, "cadence": args.cadence,
                             "target": target, "master_pid": os.getpid(), "dry_run": False,
                             "acceptance": "RUNNING", "tier0_gate": "PASS" if gate_ok else "BYPASSED",
                             "tier0_failing_required": failing, "sectors": _result_rows(results)},
                        )
                else:
                    for sector in tier1_selected:
                        results[sector.name] = RunResult(sector=sector.name, status="SKIPPED_GATE",
                                                         note=f"tier0 gate failed: {failing}")
                        log(f"  [{sector.name}] SKIPPED_GATE (tier0 failing_required={failing})")
            elif tier1_selected and args.mode == "backfill":
                for sector in tier1_selected:
                    note = sector.backfill.note if sector.backfill else "no backfill"
                    results[sector.name] = RunResult(sector=sector.name, status="NOTE", note=note)
                    log(f"  [{sector.name}] NOTE: {note}")

            overall = compute_overall(selected, results, mode=args.mode, gate_ok=gate_ok, ignore_gate=args.ignore_gate)
        finally:
            manifest_path = write_master_manifest(
                run_dir,
                {"run_stamp": run_stamp, "mode": args.mode, "cadence": args.cadence, "target": target,
                 "master_pid": os.getpid(), "dry_run": False, "acceptance": overall,
                 "tier0_gate": "PASS" if gate_ok else "FAIL",
                 "tier0_failing_required": failing, "ignore_gate": bool(args.ignore_gate),
                 "sectors": _result_rows(results)},
            )
    log(f"master_manifest: {manifest_path}")
    log(f"OVERALL: {overall}")
    return 0 if overall == "PASS" else 1


# --------------------------------------------------------------------------- #
# Selftest (no subprocess; fake registry)
# --------------------------------------------------------------------------- #
FAKE_REGISTRY = {
    "defaults": {
        "timeout_sec": 60, "retries": 0, "max_concurrent_network_lanes": 2,
        "catch_up_gap_backfill_threshold": 3, "catch_up_window_days": 45, "repair_days": 5,
        "calendar_reference_sectors": ["alpha", "defense_x"],
    },
    "group_order": {"grp_tech": ["alpha", "beta"], "grp_ind": ["defense_x", "mach_x"]},
    "sectors": [
        {"name": "alpha", "db_group": "grp_tech", "dependency_tier": 0, "required": True, "network": True,
         "entry_script": "a/run.py", "date_flag": "--asof", "args_template": ["--asof", "{date}"],
         "force_args": ["--force-refresh"], "publish_glob": "output/a/{date}/a.csv", "publish_date_format": "%Y-%m-%d",
         "oos_column": "oos_score_valid_flag", "require_oos_valid": True, "staleness_tolerance_days": 3,
         "health": {"manifest": "output/a/m.json", "status_keys": ["status"]},
         "daily_post_steps": [{"script": "a/promote.py", "args_template": ["--asof", "{date}"]}],
         "backfill": {"script": "a/bf.py", "args_template": ["--start", "{from}", "--end", "{to}"], "per_date": False},
         "repair": {"date_flag": "--asof", "selection_flag": "--only", "steps": ["s1", "s2"],
                    "rebuild_steps": ["r1", "r2"], "extra_args": []}},
        {"name": "beta", "db_group": "grp_tech", "dependency_tier": 0, "required": True, "network": True,
         "entry_script": "b/run.py", "date_flag": "--asof", "args_template": ["--asof", "{date}"],
         "force_args": [], "publish_glob": "output/b/{date}/b.csv", "publish_date_format": "%Y-%m-%d",
         "oos_column": None, "staleness_tolerance_days": 3,
         "health": {"manifest": None, "status_keys": []},
         "backfill": {"script": "b/bf.py", "args_template": ["--from", "{from}", "--to", "{to}"], "per_date": True},
         "repair": {"date_flag": "--asof", "selection_flag": "--steps", "steps": ["x1"], "rebuild_steps": ["x9"], "extra_args": []}},
        {"name": "defense_x", "db_group": "grp_ind", "dependency_tier": 0, "required": True, "network": True,
         "entry_script": "d/run.py", "date_flag": "--asof", "args_template": ["--asof", "{date}"],
         "force_args": [], "publish_glob": "output/d/{date}/d.csv", "publish_date_format": "%Y-%m-%d",
         "oos_column": "oos_score_valid_flag", "require_oos_valid": True, "staleness_tolerance_days": 3,
         "health": {"manifest": "output/d/m.json", "status_keys": ["acceptance"]},
         "weekly_pre_steps": [{"script": "d/snap.py", "args_template": ["--end-date", "{date}"]}],
         "backfill": {"script": "d/19.py", "args_template": ["--start-date", "{from}", "--end-date", "{to}", "--membership-mode", "pit"], "per_date": False},
         "repair": {"date_flag": "--asof", "selection_flag": "", "steps": ["13", "17"], "rebuild_steps": [],
                    "extra_args": ["--positioning-through-publish-only"]}},
        {"name": "mach_x", "db_group": "grp_ind", "dependency_tier": 0, "required": False, "network": True,
         "entry_script": "m/run.py", "date_flag": "--asof", "args_template": ["--asof", "{date}"],
         "force_args": ["--force"], "publish_glob": "output/m/{date}/m.csv", "publish_date_format": "%Y-%m-%d",
         "oos_column": "oos_score_valid_flag", "require_oos_valid": True, "staleness_tolerance_days": 3,
         "health": {"manifest": "output/m/m.json", "status_keys": ["acceptance"]},
         "backfill": {"script": "m/run.py", "args_template": ["--asof", "{to}", "--include-historical-backfill", "--history-start-date", "{from}"], "per_date": False},
         "repair": {"date_flag": "--asof", "selection_flag": "--only", "steps": ["12", "13"], "rebuild_steps": ["06a", "10b"], "extra_args": []}},
        {"name": "port_x", "db_group": "portfolio", "dependency_tier": 1, "required": True, "network": True,
         "entry_script": "p/18.py", "date_flag": "--as-of", "args_template": ["--as-of", "{date}"],
         "force_args": ["--force"], "publish_glob": "p/output/runs/{date}/stocks_scores.csv", "publish_date_format": "%Y-%m-%d",
         "oos_column": None, "require_oos_valid": False, "staleness_tolerance_days": 10,
         "health": {"manifest": "p/output/runs/{date}/manifest.json", "status_keys": ["hard_gate_acceptance"]},
         "backfill": {"script": "", "args_template": [], "per_date": False, "note": "stage11 not auto-run"},
         "repair": None},
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
    ok("registry_group_order", reg.group_order["grp_ind"] == ["defense_x", "mach_x"])
    port = reg.by_name("port_x")
    ok("registry_tier1", port.dependency_tier == 1 and port.date_flag == "--as-of")
    ok("registry_backfill_note", port.backfill is not None and port.backfill.script == "" and "stage11" in port.backfill.note)
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
    ok("catch_up_historical_portfolio_suppresses_event_cycle",
       "--historical-catchup" in historical_portfolio)
    ok("catch_up_live_portfolio_keeps_event_cycle",
       "--historical-catchup" not in live_portfolio)

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
    ok("repair_defense_single_tail", len(rcmds_def) == 1 and "--positioning-through-publish-only" in rcmds_def[0] and "--only" not in rcmds_def[0])

    # --- date / gap math + NYSE holidays ---
    ok("parse_iso", parse_iso(" 2026-07-17 ") == "2026-07-17")
    ok("holiday_july3_2026_observed", not is_trading_day(date(2026, 7, 3)))     # observed July 4 (Sat)
    ok("holiday_july6_2026_trading", is_trading_day(date(2026, 7, 6)))
    ok("holiday_new_year_2026", not is_trading_day(date(2026, 1, 1)))
    ok("holiday_mlk_2026", not is_trading_day(date(2026, 1, 19)))
    ok("holiday_good_friday_2026", not is_trading_day(date(2026, 4, 3)))
    ok("holiday_christmas_2026", not is_trading_day(date(2026, 12, 25)))
    ok("holiday_new_year_2022_no_dec31", is_trading_day(date(2021, 12, 31)))     # Jan 1 2022 was Sat; Fri stays open
    span = trading_dates_in_range(reg, "2026-06-29", "2026-07-07")
    ok("trading_span_excludes_july3_and_weekend",
       span == ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07"])
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
    ok("target_after_close_same_day",
       resolve_target_date(None, now_utc=datetime(2026, 7, 21, 22, 0, tzinfo=utc)) == "2026-07-21")   # Tue 18:00 ET
    ok("target_before_close_prev_day",
       resolve_target_date(None, now_utc=datetime(2026, 7, 21, 12, 0, tzinfo=utc)) == "2026-07-20")   # Tue 08:00 ET
    ok("target_monday_before_close_skips_holiday",
       resolve_target_date(None, now_utc=datetime(2026, 7, 6, 12, 0, tzinfo=utc)) == "2026-07-02")    # Mon 08:00 ET -> skip Jul3 + weekend
    ok("target_saturday_prev_friday",
       resolve_target_date(None, now_utc=datetime(2026, 7, 18, 22, 0, tzinfo=utc)) == "2026-07-17")
    ok("target_requested_passthrough", resolve_target_date("2026-05-01") == "2026-05-01")

    # --- missing dates: internal gaps surfaced ---
    published = {"2026-07-13", "2026-07-14", "2026-07-16", "2026-07-17"}   # 07-15 internal gap
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
    base = {"alpha": RunResult("alpha", "PASS"), "beta": RunResult("beta", "UP_TO_DATE"),
            "defense_x": RunResult("defense_x", "PASS"), "mach_x": RunResult("mach_x", "OPTIONAL_FAIL")}
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
    ok("overall_up_to_date_pass",
       compute_overall(sel, {**base, "port_x": RunResult("port_x", "PASS")}, mode="catch-up", gate_ok=True, ignore_gate=False) == "PASS")
    ok("overall_gate_fail_no_bypass",
       compute_overall(sel, base, mode="daily", gate_ok=False, ignore_gate=False) == "FAIL")
    note_sel = [reg.by_name("port_x")]
    ok("overall_backfill_note_ok",
       compute_overall(note_sel, {"port_x": RunResult("port_x", "NOTE")}, mode="backfill", gate_ok=True, ignore_gate=True) == "PASS")
    ok("overall_daily_note_required_fail",
       compute_overall(note_sel, {"port_x": RunResult("port_x", "NOTE")}, mode="daily", gate_ok=True, ignore_gate=True) == "FAIL")

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
        recs = [{"sector": "alpha", "status": "PASS", "target": "2026-07-17", "mode": "daily",
                 "command_hash": h, "content_hash": ch, "sha256": art_sha, "artifact": str(art)}]
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

    # --- conflicting mode flags / cadence ---
    for flags in (["--repair", "alpha", "--catch-up"], ["--mode", "backfill", "--catch-up"], ["--cadence", "weekly", "--catch-up"]):
        a = parse_args(flags)
        try:
            resolve_mode(a)
            ok(f"reject_conflict_{'_'.join(flags)}", False)
        except SystemExit:
            ok(f"reject_conflict_{'_'.join(flags)}", True)

    # --- finding 1: daily post-steps appended after the publish command ---
    args_daily = parse_args([])
    dcmds, dnote = build_sector_commands(reg, reg.by_name("alpha"), args_daily, "2026-07-17", {})
    ok("finding1_daily_post_step_appended", any("promote.py" in " ".join(c) for c in dcmds))
    ok("finding1_post_step_after_publish",
       "run.py" in " ".join(dcmds[0]) and "promote.py" in " ".join(dcmds[-1]) and "--asof" in dcmds[-1] and "2026-07-17" in dcmds[-1])
    ok("finding1_post_step_note", dnote == "daily+post_steps")
    dcmds_np, dnote_np = build_sector_commands(reg, reg.by_name("beta"), args_daily, "2026-07-17", {})
    ok("finding1_no_post_step_when_none", all("promote.py" not in " ".join(c) for c in dcmds_np) and dnote_np == "")

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

    def _mk_artifact(folder_parent: str, folder: str, filename: str, rows: int,
                     *, date_col: str | None = "asof_date", date_val: str | None = None) -> None:
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
            name="fsec", db_group="g", dependency_tier=0, required=True, network=False,
            entry_script="x/run.py", date_flag="--asof", args_template=["--asof", "{date}"],
            force_args=[], publish_glob="pub/{date}/t.csv", publish_date_format="%Y-%m-%d",
            oos_column="oos_score_valid_flag", gate_column=None, require_oos_valid=True,
            staleness_tolerance_days=3, health=HealthSpec(manifest=None, status_keys=[]),
            backfill=None, repair=None, weekly_pre_steps=[], daily_post_steps=[], timeout_sec=60, retries=0,
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
        # catch-up verifies EVERY attempted date: a single bad date fails the whole sector
        res_cu = RunResult("fsec", "PASS")
        finalize_result(fsec, res_cu, target="2026-07-17", mode="catch-up", dry_run=False,
                        catch_up_dates=["2026-07-15", "2026-07-16", "2026-07-14"])
        ok("finding3_catchup_all_dates_checked", res_cu.status == "FAIL" and "2026-07-14" in res_cu.note)
        res_ok = RunResult("fsec", "PASS")
        finalize_result(fsec, res_ok, target="2026-07-17", mode="catch-up", dry_run=False,
                        catch_up_dates=["2026-07-15", "2026-07-16", "2026-07-17"])
        ok("finding3_catchup_all_present_pass", res_ok.status == "PASS")

        # One global latest-run manifest must be checked against the final catch-up date,
        # not falsely compared with every earlier artifact.
        man_dir = tmp_root / "mani"
        man_dir.mkdir(parents=True, exist_ok=True)
        (man_dir / "global.json").write_text(
            json.dumps({"status": "PASS", "asof": "2026-07-17"}), encoding="utf-8"
        )
        fsec_global = _sector(health=HealthSpec(manifest="mani/global.json", status_keys=["status"]))
        res_global = RunResult("fsec", "PASS")
        finalize_result(
            fsec_global,
            res_global,
            target="2026-07-17",
            mode="catch-up",
            dry_run=False,
            catch_up_dates=["2026-07-15", "2026-07-16", "2026-07-17"],
        )
        ok("finding3_global_manifest_checked_once", res_global.status == "PASS")

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
        fsec_nodate = _sector(publish_glob="pub2/{date}/t.csv",
                              health=HealthSpec(manifest="mani/empty_{date}.json", status_keys=["status"]))
        vfc = verify_published_artifact_for_date(fsec_nodate, "2026-07-16")
        ok("finding4_empty_asof_no_datecol_failclosed", not vfc[0] and any("date unverified" in r for r in vfc[1]))
        # populated manifest asof that disagrees with folder -> fail
        (man_dir / "wrong_2026-07-16.json").write_text(json.dumps({"status": "PASS", "asof": "2026-07-10"}), encoding="utf-8")
        fsec_wrong = _sector(health=HealthSpec(manifest="mani/wrong_{date}.json", status_keys=["status"]))
        vwrong = verify_published_artifact_for_date(fsec_wrong, "2026-07-16")
        ok("finding4_manifest_asof_mismatch_fails", not vwrong[0] and any("manifest asof=2026-07-10" in r for r in vwrong[1]))
    finally:
        _globals["PROJECT_ROOT"] = _saved_root
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)

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
    with OrchestrationLock(dead_lock, stale_after_sec=10 ** 9):  # dead PID -> override even though fresh
        ok("finding9_dead_pid_override", dead_lock.exists())
    live_lock = _lockdir / "live.lock"
    live_lock.write_text(f"pid={os.getpid()} started_utc=2000-01-01T00:00:00+00:00\n", encoding="utf-8")
    try:
        with OrchestrationLock(live_lock, stale_after_sec=1):  # alive PID + old -> must NOT override
            ok("finding9_live_pid_not_overridden", False)
    except RuntimeError:
        ok("finding9_live_pid_not_overridden", True)
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

    # --- real registry loads and every entry_script exists ---
    real = load_registry(DEFAULT_REGISTRY)
    ok("real_registry_sectors", len(real.sectors) == 9)
    try:
        validate_registry_paths(real)
        ok("real_registry_all_script_paths_exist", True)
    except ValueError:
        ok("real_registry_all_script_paths_exist", False)
    ok(
        "real_industrials_order",
        real.group_order.get("industrials")
        == ["defense", "machinery", "transportation"],
    )
    ok("real_portfolio_tier1", real.by_name("portfolio_layer").dependency_tier == 1)
    rdef = real.by_name("defense")
    ok("real_defense_weekly_no_promotion", not rdef.weekly_pre_steps)
    rbio = real.by_name("biotech")
    ok("real_biotech_oos_column", rbio.oos_column == "oos_score_valid_flag" and rbio.require_oos_valid)
    rmed = real.by_name("med_devices")
    ok("real_med_devices_require_oos", rmed.require_oos_valid)
    # finding 1: daily self-certifies within the replay window, then script 76 records provenance.
    ok("real_med_oos_score_valid_flag", "--oos-score-valid" in rmed.args_template)
    ok("real_med_post_step_76",
       any("76_mark_med_device_oos_provenance" in str(step.get("script", "")) for step in rmed.daily_post_steps))
    # finding 7: 63 rebuild sits in the repair rebuild chain, before the institutional-flow rebuild it feeds.
    ok("real_med_repair_has_63",
       rmed.repair is not None and "63_rebuild_sec13f_common_shares" in rmed.repair.rebuild_steps)
    ok("real_med_63_before_flow",
       rmed.repair is not None
       and rmed.repair.rebuild_steps.index("63_rebuild_sec13f_common_shares")
       < rmed.repair.rebuild_steps.index("58_build_institutional_flow_features"))
    rsemi = real.by_name("semiconductors")
    ok("real_semi_backfill_daily_survivorship",
       rsemi.backfill is not None
       and "daily" in rsemi.backfill.args_template
       and "--include-stage11-survivorship-panel" in rsemi.backfill.args_template)
    ok("real_semi_repair_rebuild", rsemi.repair is not None and bool(rsemi.repair.rebuild_steps))

    print(f"SELFTEST PASS: {len(checks)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
