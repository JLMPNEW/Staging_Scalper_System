"""Read-only Stage 4D feasibility audit for a single 2019-forward PIT build."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity
from basic_materials.core.historical_candidates import (
    load_historical_candidate_policy,
    read_and_validate_historical_candidates,
    validate_historical_candidate_manifest,
)
from basic_materials.core.specialized_contract import (
    SpecializedMetricRegistry,
    SpecializedSourcePolicy,
)


class PitPreflightError(ValueError):
    """Raised when the Stage 4D policy or input state is unsafe."""


@dataclass(frozen=True)
class ChronologicalBlock:
    block_id: str
    start_date: str
    end_date: str


@dataclass(frozen=True)
class HistoricalPitPreflightPolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    target_history_start_date: str
    calendar_code: str
    cadence: str
    blocks: tuple[ChronologicalBlock, ...]
    minimum_financial_metrics: int
    maximum_period_age_days: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class HistoricalPitPreflightReport:
    feasibility_passed: bool
    pit_materialization_allowed: bool
    database_unchanged: bool
    policy_version: str
    policy_sha256: str
    database_path: str
    schedule_rows: tuple[Mapping[str, Any], ...]
    date_rows: tuple[Mapping[str, Any], ...]
    block_rows: tuple[Mapping[str, Any], ...]
    blockers: tuple[Mapping[str, Any], ...]
    input_seals: Mapping[str, Any]
    counts: Mapping[str, Any]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "feasibility_passed": self.feasibility_passed,
            "pit_materialization_allowed": self.pit_materialization_allowed,
            "database_unchanged": self.database_unchanged,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "database_path": self.database_path,
            "counts": dict(self.counts),
            "input_seals": dict(self.input_seals),
            "blocker_count": len(self.blockers),
            "blockers": [dict(row) for row in self.blockers],
        }


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PitPreflightError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise PitPreflightError(
            f"{context} keys differ; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _iso_date(value: Any, context: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise PitPreflightError(f"{context} must be an ISO date") from exc


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_historical_pit_preflight_policy(path: str | Path) -> HistoricalPitPreflightPolicy:
    policy_path = Path(path).resolve()
    raw = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(raw.decode("utf-8")), "historical PIT preflight policy")
    _exact_keys(
        root,
        {
            "policy_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "state",
            "target_history_start_date",
            "schedule",
            "chronological_blocks",
            "financial_feasibility",
            "market_feasibility",
            "membership_gate",
            "specialized_gate",
            "no_write_contract",
            "required_flags",
        },
        "historical PIT preflight policy",
    )
    if root["policy_version"] != "basic_materials_historical_pit_preflight_policy_v1":
        raise PitPreflightError("Unsupported historical PIT preflight policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise PitPreflightError("Historical PIT preflight identity is invalid")
    if root["state"] != "stage4d_read_only_feasibility":
        raise PitPreflightError("Historical PIT preflight state is invalid")
    schedule = _mapping(root["schedule"], "schedule")
    _exact_keys(
        schedule,
        {"calendar_code", "cadence", "approximate_sessions_per_period", "end_date_rule"},
        "schedule",
    )
    if (
        schedule["calendar_code"] != "XNYS_PROXY_SPY"
        or schedule["cadence"] != "monthly_last_session"
        or int(schedule["approximate_sessions_per_period"]) != 21
        or schedule["end_date_rule"] != "last_available_calendar_session_on_or_before_contract_asof"
    ):
        raise PitPreflightError("Historical PIT schedule contract is invalid")
    blocks_raw = root["chronological_blocks"]
    if not isinstance(blocks_raw, list) or len(blocks_raw) != 3:
        raise PitPreflightError("Exactly three chronological blocks are required")
    blocks: list[ChronologicalBlock] = []
    for index, raw_block in enumerate(blocks_raw):
        block = _mapping(raw_block, f"chronological_blocks[{index}]")
        _exact_keys(block, {"block_id", "start_date", "end_date"}, f"chronological_blocks[{index}]")
        start = _iso_date(block["start_date"], f"block[{index}].start_date")
        end = _iso_date(block["end_date"], f"block[{index}].end_date")
        if start > end:
            raise PitPreflightError(f"Chronological block {block['block_id']} is reversed")
        blocks.append(ChronologicalBlock(str(block["block_id"]), start, end))
    financial = _mapping(root["financial_feasibility"], "financial_feasibility")
    _exact_keys(
        financial,
        {
            "minimum_distinct_canonical_metrics",
            "maximum_latest_period_age_days",
            "accepted_at_is_earliest_availability",
            "period_end_must_not_exceed_asof",
        },
        "financial_feasibility",
    )
    if (
        financial["accepted_at_is_earliest_availability"] is not True
        or financial["period_end_must_not_exceed_asof"] is not True
        or int(financial["minimum_distinct_canonical_metrics"]) < 1
        or int(financial["maximum_latest_period_age_days"]) < 365
    ):
        raise PitPreflightError("Financial PIT feasibility controls are invalid")
    market = _mapping(root["market_feasibility"], "market_feasibility")
    membership = _mapping(root["membership_gate"], "membership_gate")
    specialized = _mapping(root["specialized_gate"], "specialized_gate")
    no_write = _mapping(root["no_write_contract"], "no_write_contract")
    flags = _mapping(root["required_flags"], "required_flags")
    if market.get("require_bar_on_scheduled_session") is not True:
        raise PitPreflightError("Market scheduled-session control must be enabled")
    if (
        membership.get("require_effective_dated_membership_for_every_materialized_row") is not True
        or membership.get("prohibit_current_snapshot_backfill") is not True
        or int(membership.get("open_candidate_decisions_must_equal", -1)) != 0
        or int(membership.get("unresolved_terminal_distributions_must_equal", -1)) != 0
    ):
        raise PitPreflightError("Historical membership gate is invalid")
    expected_specialized = {
        "applicability_accounting_ratio": 1.0,
        "source_terminal_or_cached_ratio": 1.0,
        "parser_work_terminal_ratio": 1.0,
        "current_core_coverage_ratio": 0.80,
        "historical_core_coverage_ratio": 0.70,
        "chronological_block_floor": 0.60,
        "calibration_min_comparable_issuers": 8,
        "calibration_min_dates": 36,
    }
    if {key: float(value) for key, value in specialized.items()} != expected_specialized:
        raise PitPreflightError("Specialized coverage gate is invalid")
    if (
        no_write.get("prohibit_database_mutation") is not True
        or no_write.get("historical_feature_materialization_allowed") is not False
        or list(no_write.get("prohibited_table_prefixes", [])) != ["feature_", "score_", "rank_", "outcome_"]
        or any(value is not False for value in flags.values())
    ):
        raise PitPreflightError("No-write or prohibited promotion flags are invalid")
    return HistoricalPitPreflightPolicy(
        path=policy_path,
        checksum=hashlib.sha256(raw).hexdigest(),
        version=str(root["policy_version"]),
        as_of_date=_iso_date(root["contract_as_of_date"], "contract_as_of_date"),
        target_history_start_date=_iso_date(root["target_history_start_date"], "target_history_start_date"),
        calendar_code=str(schedule["calendar_code"]),
        cadence=str(schedule["cadence"]),
        blocks=tuple(blocks),
        minimum_financial_metrics=int(financial["minimum_distinct_canonical_metrics"]),
        maximum_period_age_days=int(financial["maximum_latest_period_age_days"]),
        payload=dict(root),
    )


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}


def _monthly_schedule(
    conn: sqlite3.Connection,
    policy: HistoricalPitPreflightPolicy,
) -> list[dict[str, str]]:
    sessions = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT session_date FROM dim_trading_calendar_session
            WHERE calendar_code = ? AND session_date >= ? AND session_date <= ?
            ORDER BY session_date
            """,
            (policy.calendar_code, policy.target_history_start_date, policy.as_of_date),
        ).fetchall()
    ]
    by_month: dict[str, str] = {}
    for session in sessions:
        by_month[session[:7]] = session
    rows: list[dict[str, str]] = []
    for sequence, (month, session) in enumerate(sorted(by_month.items()), start=1):
        block = next(
            (item.block_id for item in policy.blocks if item.start_date <= session <= item.end_date),
            "outside_policy_blocks",
        )
        rows.append(
            {
                "schedule_sequence": str(sequence),
                "score_month": month,
                "score_date": session,
                "calendar_code": policy.calendar_code,
                "cadence": policy.cadence,
                "chronological_block": block,
            }
        )
    return rows


def _financial_feasibility(
    conn: sqlite3.Connection,
    *,
    schedule_dates: Sequence[str],
    policy: HistoricalPitPreflightPolicy,
) -> dict[str, set[int]]:
    if not schedule_dates:
        return {}
    facts: defaultdict[int, list[tuple[str, str, str]]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT security_id, canonical_metric, substr(accepted_at, 1, 10) AS accepted_date,
               period_end
        FROM fact_financial_statement_canonical
        WHERE quality_status = 'usable'
          AND substr(accepted_at, 1, 10) <= ?
          AND period_end <= ?
        ORDER BY security_id, accepted_date, period_end, canonical_metric
        """,
        (schedule_dates[-1], schedule_dates[-1]),
    ).fetchall():
        facts[int(row["security_id"])].append(
            (str(row["accepted_date"]), str(row["canonical_metric"]), str(row["period_end"]))
        )
    latest: defaultdict[int, dict[str, str]] = defaultdict(dict)
    positions: Counter[int] = Counter()
    result: dict[str, set[int]] = {}
    security_ids = sorted(facts)
    for asof in schedule_dates:
        asof_date = date.fromisoformat(asof)
        ready: set[int] = set()
        for security_id in security_ids:
            issuer_facts = facts[security_id]
            position = positions[security_id]
            while position < len(issuer_facts) and issuer_facts[position][0] <= asof:
                _, metric, period_end = issuer_facts[position]
                if period_end <= asof and period_end > latest[security_id].get(metric, ""):
                    latest[security_id][metric] = period_end
                position += 1
            positions[security_id] = position
            usable = sum(
                (asof_date - date.fromisoformat(period_end)).days <= policy.maximum_period_age_days
                for period_end in latest[security_id].values()
            )
            if usable >= policy.minimum_financial_metrics:
                ready.add(security_id)
        result[asof] = ready
    return result


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _date_rows(
    conn: sqlite3.Connection,
    *,
    schedule: Sequence[Mapping[str, str]],
    policy: HistoricalPitPreflightPolicy,
) -> list[dict[str, Any]]:
    memberships = [
        dict(row)
        for row in conn.execute(
            """
            SELECT security_id, ticker, cohort_id, membership_status,
                   membership_start_date, COALESCE(membership_end_date, '') AS membership_end_date,
                   current_source_only
            FROM dim_universe_membership ORDER BY security_id
            """
        ).fetchall()
    ]
    roles = {
        int(row["security_id"]): dict(row)
        for row in conn.execute(
            """
            SELECT security_id, instrument_id, role_type, expected_start_date,
                   COALESCE(expected_end_date, '') AS expected_end_date
            FROM bridge_market_instrument_role
            WHERE role_type IN ('current_universe', 'historical_pilot')
            """
        ).fetchall()
    }
    dates = [str(row["score_date"]) for row in schedule]
    financial = _financial_feasibility(conn, schedule_dates=dates, policy=policy)
    market: defaultdict[str, set[int]] = defaultdict(set)
    if dates:
        placeholders = ",".join("?" for _ in dates)
        for row in conn.execute(
            f"""
            SELECT r.security_id, b.bar_date
            FROM fact_adjusted_price_bar b
            JOIN bridge_market_instrument_role r ON r.instrument_id = b.instrument_id
            WHERE r.role_type IN ('current_universe', 'historical_pilot')
              AND b.bar_date IN ({placeholders})
            """,
            tuple(dates),
        ).fetchall():
            market[str(row["bar_date"])].add(int(row["security_id"]))
    rows: list[dict[str, Any]] = []
    for schedule_row in schedule:
        asof = str(schedule_row["score_date"])
        known_members = {
            int(row["security_id"])
            for row in memberships
            if str(row["membership_start_date"]) <= asof
            and (not row["membership_end_date"] or str(row["membership_end_date"]) >= asof)
        }
        unknown_current = {
            int(row["security_id"])
            for row in memberships
            if row["membership_status"] == "current"
            and int(row["current_source_only"]) == 1
            and asof < str(row["membership_start_date"])
        }
        role_eligible = {
            security_id
            for security_id, role in roles.items()
            if str(role["expected_start_date"]) <= asof
            and (not role["expected_end_date"] or str(role["expected_end_date"]) >= asof)
        }
        known_market = len(known_members & market[asof])
        known_financial = len(known_members & financial.get(asof, set()))
        role_market = len(role_eligible & market[asof])
        role_financial = len(role_eligible & financial.get(asof, set()))
        rows.append(
            {
                **dict(schedule_row),
                "known_membership_count": len(known_members),
                "unknown_current_membership_count": len(unknown_current),
                "known_member_market_ready_count": known_market,
                "known_member_financial_ready_count": known_financial,
                "known_member_market_ratio": _ratio(known_market, len(known_members)),
                "known_member_financial_ratio": _ratio(known_financial, len(known_members)),
                "role_eligible_identity_count": len(role_eligible),
                "role_market_ready_count": role_market,
                "role_financial_ready_count": role_financial,
                "role_market_ratio": _ratio(role_market, len(role_eligible)),
                "role_financial_ratio": _ratio(role_financial, len(role_eligible)),
            }
        )
    return rows


def _block_rows(
    date_rows: Sequence[Mapping[str, Any]],
    blocks: Sequence[ChronologicalBlock],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in blocks:
        rows = [row for row in date_rows if row["chronological_block"] == block.block_id]
        known_cells = sum(int(row["known_membership_count"]) for row in rows)
        role_cells = sum(int(row["role_eligible_identity_count"]) for row in rows)
        result.append(
            {
                "block_id": block.block_id,
                "start_date": block.start_date,
                "end_date": block.end_date,
                "scheduled_date_count": len(rows),
                "known_membership_cells": known_cells,
                "unknown_current_membership_cells": sum(int(row["unknown_current_membership_count"]) for row in rows),
                "known_member_market_coverage": _ratio(sum(int(row["known_member_market_ready_count"]) for row in rows), known_cells),
                "known_member_financial_coverage": _ratio(sum(int(row["known_member_financial_ready_count"]) for row in rows), known_cells),
                "role_eligible_cells": role_cells,
                "role_market_coverage": _ratio(sum(int(row["role_market_ready_count"]) for row in rows), role_cells),
                "role_financial_coverage": _ratio(sum(int(row["role_financial_ready_count"]) for row in rows), role_cells),
            }
        )
    return result


def run_historical_pit_preflight(
    conn: sqlite3.Connection,
    *,
    database_path: str | Path,
    policy: HistoricalPitPreflightPolicy,
    registry: SpecializedMetricRegistry,
    source_policy: SpecializedSourcePolicy,
    historical_candidate_policy_path: str | Path,
    historical_candidate_manifest_path: str | Path,
    historical_candidates_path: str | Path,
) -> HistoricalPitPreflightReport:
    """Audit feasibility without creating or mutating any database row."""

    assert_database_identity(conn)
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise PitPreflightError("Stage 4D requires a query-only database connection")
    if not (policy.as_of_date == registry.as_of_date == source_policy.as_of_date):
        raise PitPreflightError("Stage 4D and Stage 5A contract as-of dates differ")
    path = Path(database_path).resolve()
    before_stat = (path.stat().st_size, path.stat().st_mtime_ns)
    before_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    before_counts = _table_counts(conn)

    schedule = _monthly_schedule(conn, policy)
    date_rows = _date_rows(conn, schedule=schedule, policy=policy)
    block_rows = _block_rows(date_rows, policy.blocks)

    candidate_policy = load_historical_candidate_policy(historical_candidate_policy_path)
    candidate_manifest = validate_historical_candidate_manifest(
        historical_candidate_manifest_path,
        historical_candidates_path,
    )
    candidates = read_and_validate_historical_candidates(historical_candidates_path, candidate_policy)
    candidate_tickers = {row.historical_ticker for row in candidates}
    selected_tickers = {
        str(row[0])
        for row in conn.execute(
            "SELECT ticker FROM dim_universe_membership WHERE membership_status='historical'"
        ).fetchall()
    }
    unknown_selected = sorted(selected_tickers - candidate_tickers)
    if unknown_selected:
        raise PitPreflightError(f"Historical members absent from candidate census: {unknown_selected}")
    selected_candidate_count = len(selected_tickers & candidate_tickers)
    open_candidates = len(candidates) - selected_candidate_count
    unresolved_terminal = int(
        conn.execute("SELECT COUNT(*) FROM fact_terminal_event_reconciliation WHERE resolved=0").fetchone()[0]
    )
    current_history_open = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM dim_universe_membership
            WHERE membership_status='current' AND current_source_only=1
              AND membership_start_date > ?
            """,
            (policy.target_history_start_date,),
        ).fetchone()[0]
    )
    identity_count = int(conn.execute("SELECT COUNT(*) FROM dim_security").fetchone()[0])
    metric_count = int(conn.execute("SELECT COUNT(*) FROM dim_specialized_metric").fetchone()[0])
    applicability_rows = int(conn.execute("SELECT COUNT(*) FROM bridge_specialized_metric_applicability").fetchone()[0])
    applicability_reviews = int(
        conn.execute(
            "SELECT COUNT(*) FROM bridge_specialized_metric_applicability WHERE applicability_status='review_required'"
        ).fetchone()[0]
    )
    source_status_counts = {
        str(row["discovery_status"]): int(row["n"])
        for row in conn.execute(
            "SELECT discovery_status, COUNT(*) AS n FROM fact_specialized_source_census GROUP BY discovery_status"
        ).fetchall()
    }
    source_rows = sum(source_status_counts.values())
    source_terminal = source_status_counts.get("cached_hashed", 0) + source_status_counts.get("terminal_unavailable", 0) + source_status_counts.get("not_applicable", 0)
    parser_status_counts = {
        str(row["work_status"]): int(row["n"])
        for row in conn.execute(
            "SELECT work_status, COUNT(*) AS n FROM fact_specialized_parser_work GROUP BY work_status"
        ).fetchall()
    }
    parser_rows = sum(parser_status_counts.values())
    parser_terminal = parser_status_counts.get("completed", 0) + parser_status_counts.get("skipped_terminal", 0)
    accepted_observations = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_specialized_metric_observation WHERE observation_status='accepted'"
        ).fetchone()[0]
    )
    blockers: list[dict[str, Any]] = []

    def block(code: str, message: str, count: int | None = None) -> None:
        blockers.append({"issue_code": code, "severity": "blocker", "count": count, "message": message})

    if open_candidates:
        block("HISTORICAL_CANDIDATE_DECISIONS_OPEN", f"Resolve or reject {open_candidates} remaining deactivated candidates", open_candidates)
    if unresolved_terminal:
        block("TERMINAL_DISTRIBUTIONS_OPEN", f"Resolve {unresolved_terminal} pending terminal-distribution treatments", unresolved_terminal)
    if current_history_open:
        block("CURRENT_MEMBERSHIP_HISTORY_UNRECONSTRUCTED", f"{current_history_open} current-snapshot securities lack pre-snapshot membership history", current_history_open)
    expected_applicability = identity_count * metric_count
    if applicability_rows != expected_applicability:
        block("SPECIALIZED_APPLICABILITY_ACCOUNTING_INCOMPLETE", f"Expected {expected_applicability} identity-metric rows; found {applicability_rows}", expected_applicability - applicability_rows)
    if applicability_reviews:
        block("SPECIALIZED_APPLICABILITY_REVIEW_OPEN", f"Review {applicability_reviews} issuer-selective metric pairs", applicability_reviews)
    if source_rows == 0 or source_terminal != source_rows:
        block("SPECIALIZED_SOURCE_CLOSURE_INCOMPLETE", f"{source_rows - source_terminal} source rows are not cached/hash-sealed or terminally disposed", source_rows - source_terminal)
    if parser_rows == 0 or parser_terminal != parser_rows:
        block("SPECIALIZED_PARSER_PLAN_INCOMPLETE", "No complete all-document parser work ledger exists", parser_rows - parser_terminal if parser_rows else 0)
    if accepted_observations == 0:
        block("SPECIALIZED_COVERAGE_NOT_MEASURED", "No accepted specialized observations exist; coverage gates cannot be evaluated", 0)
    if any(int(row["scheduled_date_count"]) == 0 for row in block_rows):
        block("CHRONOLOGICAL_BLOCK_HAS_NO_DATES", "At least one required chronological block has no scheduled dates")

    market_min = conn.execute("SELECT MIN(bar_date) FROM fact_adjusted_price_bar").fetchone()[0]
    financial_min = conn.execute("SELECT MIN(period_end) FROM fact_financial_statement_canonical").fetchone()[0]
    feasibility_passed = bool(
        schedule
        and str(schedule[0]["score_date"])[:7] == policy.target_history_start_date[:7]
        and market_min is not None
        and str(market_min) <= policy.target_history_start_date
        and financial_min is not None
        and str(financial_min) <= policy.target_history_start_date
        and all(int(row["scheduled_date_count"]) > 0 for row in block_rows)
    )

    after_counts = _table_counts(conn)
    after_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    after_stat = (path.stat().st_size, path.stat().st_mtime_ns)
    database_unchanged = before_counts == after_counts and before_version == after_version and before_stat == after_stat
    if not database_unchanged:
        block("NO_WRITE_CONTRACT_VIOLATION", "Database size, timestamp, version, or row counts changed during Stage 4D")
    pit_allowed = feasibility_passed and database_unchanged and not blockers

    membership_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ticker, cohort_id, membership_start_date,
                   COALESCE(membership_end_date, '') AS membership_end_date,
                   membership_status, current_source_only, survivorship_corrected
            FROM dim_universe_membership ORDER BY ticker
            """
        ).fetchall()
    ]
    market_snapshots = [dict(row) for row in conn.execute("SELECT * FROM fact_market_provider_snapshot ORDER BY snapshot_key").fetchall()]
    financial_snapshots = [dict(row) for row in conn.execute("SELECT * FROM fact_financial_ingestion_snapshot ORDER BY snapshot_key").fetchall()]
    input_seals = {
        "schedule_sha256": _canonical_hash(schedule),
        "membership_sha256": _canonical_hash(membership_rows),
        "market_snapshot_sha256": _canonical_hash(market_snapshots),
        "financial_snapshot_sha256": _canonical_hash(financial_snapshots),
        "specialized_registry_sha256": registry.checksum,
        "specialized_source_policy_sha256": source_policy.checksum,
        "historical_candidate_manifest_sha256": candidate_manifest["sha256"],
        "database_schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
    }
    counts = {
        "scheduled_dates": len(schedule),
        "chronological_blocks": len(block_rows),
        "current_identities": int(conn.execute("SELECT COUNT(*) FROM dim_universe_membership WHERE membership_status='current'").fetchone()[0]),
        "historical_identities_selected": selected_candidate_count,
        "historical_candidate_rows": len(candidates),
        "historical_candidate_decisions_open": open_candidates,
        "unresolved_terminal_distributions": unresolved_terminal,
        "current_membership_histories_open": current_history_open,
        "specialized_metrics": metric_count,
        "specialized_applicability_rows": applicability_rows,
        "specialized_applicability_reviews_open": applicability_reviews,
        "specialized_source_rows": source_rows,
        "specialized_source_terminal_rows": source_terminal,
        "specialized_parser_work_rows": parser_rows,
        "specialized_parser_terminal_rows": parser_terminal,
        "accepted_specialized_observations": accepted_observations,
    }
    return HistoricalPitPreflightReport(
        feasibility_passed=feasibility_passed,
        pit_materialization_allowed=pit_allowed,
        database_unchanged=database_unchanged,
        policy_version=policy.version,
        policy_sha256=policy.checksum,
        database_path=str(path),
        schedule_rows=tuple(schedule),
        date_rows=tuple(date_rows),
        block_rows=tuple(block_rows),
        blockers=tuple(blockers),
        input_seals=input_seals,
        counts=counts,
    )


def write_historical_pit_preflight_reports(
    report: HistoricalPitPreflightReport,
    *,
    report_dir: str | Path,
) -> dict[str, str]:
    output = Path(report_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output / "historical_pit_preflight_summary.json",
        "schedule": output / "historical_pit_candidate_schedule.csv",
        "date_feasibility": output / "historical_pit_date_feasibility.csv",
        "chronological_blocks": output / "historical_pit_chronological_blocks.csv",
        "blockers": output / "historical_pit_blockers.csv",
        "input_seals": output / "historical_pit_input_seals.json",
        "artifact_manifest": output / "artifact_manifest.json",
    }
    atomic_write_json(paths["summary"], report.summary_dict())
    schedule_fields = tuple(report.schedule_rows[0]) if report.schedule_rows else ("score_date",)
    date_fields = tuple(report.date_rows[0]) if report.date_rows else ("score_date",)
    block_fields = tuple(report.block_rows[0]) if report.block_rows else ("block_id",)
    blocker_fields = ("issue_code", "severity", "count", "message")
    atomic_write_csv(paths["schedule"], report.schedule_rows, schedule_fields)
    atomic_write_csv(paths["date_feasibility"], report.date_rows, date_fields)
    atomic_write_csv(paths["chronological_blocks"], report.block_rows, block_fields)
    atomic_write_csv(paths["blockers"], report.blockers, blocker_fields)
    atomic_write_json(paths["input_seals"], report.input_seals)
    manifest = {
        "policy_version": report.policy_version,
        "policy_sha256": report.policy_sha256,
        "feasibility_passed": report.feasibility_passed,
        "pit_materialization_allowed": report.pit_materialization_allowed,
        "database_unchanged": report.database_unchanged,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "artifacts": [
            {
                "name": name,
                "path": str(path),
                "sha256": _file_hash(path),
                "byte_size": path.stat().st_size,
            }
            for name, path in paths.items()
            if name != "artifact_manifest"
        ],
    }
    atomic_write_json(paths["artifact_manifest"], manifest)
    return {name: str(path) for name, path in paths.items()}
