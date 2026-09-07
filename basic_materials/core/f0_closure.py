"""Read-only F0 closure queues and de-duplicated specialized-source planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
    HistoricalCandidate,
    load_historical_candidate_policy,
    read_and_validate_historical_candidates,
    validate_historical_candidate_manifest,
)
from basic_materials.core.historical_pit_preflight import HistoricalPitPreflightPolicy
from basic_materials.core.specialized_contract import (
    SpecializedMetricRegistry,
    SpecializedSourcePolicy,
)


class F0ClosureError(ValueError):
    """Raised when the F0 closure workbench contract is malformed or unsafe."""


@dataclass(frozen=True)
class ClosureBlock:
    block_id: str
    start_date: str
    end_date: str


@dataclass(frozen=True)
class F0ClosurePolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    blocks: tuple[ClosureBlock, ...]
    queue_order: tuple[str, ...]
    shared_source_families: tuple[str, ...]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class F0ClosureReport:
    structurally_valid: bool
    f0_closure_ready: bool
    database_unchanged: bool
    policy_version: str
    policy_sha256: str
    database_path: str
    counts: Mapping[str, Any]
    blockers: tuple[Mapping[str, Any], ...]
    candidate_rows: tuple[Mapping[str, Any], ...]
    terminal_rows: tuple[Mapping[str, Any], ...]
    membership_rows: tuple[Mapping[str, Any], ...]
    applicability_rows: tuple[Mapping[str, Any], ...]
    acquisition_rows: tuple[Mapping[str, Any], ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "structurally_valid": self.structurally_valid,
            "f0_closure_ready": self.f0_closure_ready,
            "database_unchanged": self.database_unchanged,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "database_path": self.database_path,
            "counts": dict(self.counts),
            "blocker_count": len(self.blockers),
            "blockers": [dict(row) for row in self.blockers],
        }


CANDIDATE_FIELDS = (
    "queue_priority",
    "historical_ticker",
    "provider_symbol",
    "provider_asset_id",
    "company_name",
    "cohort_id",
    "candidate_tier",
    "first_quoted_date",
    "provider_last_quoted_date",
    "expected_terminal_type",
    "expected_event_date",
    "successor_ticker",
    "existing_event_source_url",
    "decision",
    "allowed_decisions",
    "decision_basis",
    "decision_source_id",
    "decision_source_url",
    "reviewed_on",
    "required_evidence",
    "input_row_sha256",
)

TERMINAL_FIELDS = (
    "event_key",
    "ticker",
    "cohort_id",
    "event_date",
    "terminal_event_type",
    "return_treatment",
    "outcome_class",
    "rule_status",
    "bankruptcy_distribution_value",
    "distribution_currency",
    "source_id",
    "evidence_json",
    "reviewed_distribution_value",
    "reviewed_distribution_currency",
    "review_source_id",
    "review_source_url",
    "reviewed_on",
    "required_evidence",
)

MEMBERSHIP_FIELDS = (
    "security_id",
    "ticker",
    "company_name",
    "cohort_id",
    "current_membership_start_date",
    "current_source_only",
    "survivorship_corrected",
    "provider_asset_id",
    "provider_symbol",
    "provider_first_quoted_date",
    "contract_listing_start_date",
    "approved_membership_start_date",
    "membership_basis",
    "membership_source_id",
    "membership_source_url",
    "reviewed_on",
    "required_evidence",
)

APPLICABILITY_FIELDS = (
    "queue_priority",
    "security_id",
    "ticker",
    "company_name",
    "cohort_id",
    "metric_id",
    "coverage_tier",
    "metric_role",
    "unit_family",
    "period_type",
    "dimension_family",
    "definition",
    "definition_variants_json",
    "source_families_json",
    "table_families_json",
    "decision",
    "allowed_decisions",
    "decision_basis",
    "decision_source_id",
    "decision_source_url",
    "reviewed_on",
)

ACQUISITION_FIELDS = (
    "queue_priority",
    "acquisition_key",
    "acquisition_scope",
    "source_family",
    "source_record_key",
    "source_id",
    "discovery_status",
    "census_row_count",
    "security_count",
    "ticker_count",
    "tickers_json",
    "cohorts_json",
    "accession_numbers_json",
    "source_urls_json",
    "resolved_metric_ids_json",
    "review_blocked_metric_ids_json",
    "reuse_strategy",
    "execution_status",
)



def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise F0ClosureError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise F0ClosureError(
            f"{context} keys differ; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _iso_date(value: Any, context: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise F0ClosureError(f"{context} must be an ISO date") from exc


def _strings(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise F0ClosureError(f"{context} must be a list")
    result = tuple(str(item).strip() for item in value)
    if not result or any(not item for item in result) or len(result) != len(set(result)):
        raise F0ClosureError(f"{context} must contain unique non-empty strings")
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}


def load_f0_closure_policy(path: str | Path) -> F0ClosurePolicy:
    policy_path = Path(path).resolve()
    payload = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "F0 closure policy")
    _exact_keys(
        root,
        {
            "policy_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "state",
            "canonical_chronological_blocks",
            "queue_order",
            "decision_controls",
            "source_planning",
            "required_flags",
        },
        "F0 closure policy",
    )
    if root["policy_version"] != "basic_materials_f0_closure_workbench_v1":
        raise F0ClosureError("Unsupported F0 closure policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise F0ClosureError("F0 closure policy identity is invalid")
    if root["state"] != "f0_review_workbench_open_no_automatic_decisions":
        raise F0ClosureError("F0 closure policy state is invalid")
    raw_blocks = root["canonical_chronological_blocks"]
    if not isinstance(raw_blocks, list) or len(raw_blocks) != 3:
        raise F0ClosureError("F0 closure policy requires exactly three chronological blocks")
    blocks: list[ClosureBlock] = []
    for index, raw_block in enumerate(raw_blocks):
        block = _mapping(raw_block, f"canonical_chronological_blocks[{index}]")
        _exact_keys(block, {"block_id", "start_date", "end_date"}, f"chronological block {index}")
        blocks.append(
            ClosureBlock(
                block_id=str(block["block_id"]).strip(),
                start_date=_iso_date(block["start_date"], f"block {index} start"),
                end_date=_iso_date(block["end_date"], f"block {index} end"),
            )
        )
    queue_order = _strings(root["queue_order"], "queue_order")
    expected_order = (
        "historical_candidate_decisions",
        "terminal_distributions",
        "current_membership_history",
        "specialized_metric_applicability",
        "specialized_source_acquisition",
    )
    if queue_order != expected_order:
        raise F0ClosureError("F0 closure queue order differs from the approved implementation order")
    controls = _mapping(root["decision_controls"], "decision_controls")
    _exact_keys(
        controls,
        {
            "query_only_database",
            "automatic_candidate_inclusion_or_exclusion",
            "infer_zero_bankruptcy_distribution",
            "current_snapshot_backfill",
            "automatic_metric_applicability",
            "source_acquisition_before_applicability_seal",
            "production_parser_execution",
        },
        "decision_controls",
    )
    if controls["query_only_database"] is not True or any(
        controls[key] is not False for key in controls if key != "query_only_database"
    ):
        raise F0ClosureError("F0 closure decisions must remain manual, source-backed, and query-only")
    planning = _mapping(root["source_planning"], "source_planning")
    _exact_keys(
        planning,
        {
            "shared_source_families",
            "sec_cache_reuse_first",
            "group_shared_driver_once",
            "content_address_algorithm",
            "execute_acquisition",
        },
        "source_planning",
    )
    shared = _strings(planning["shared_source_families"], "shared_source_families")
    if (
        shared != ("commodity_market", "positioning_market")
        or planning["sec_cache_reuse_first"] is not True
        or planning["group_shared_driver_once"] is not True
        or planning["content_address_algorithm"] != "sha256"
        or planning["execute_acquisition"] is not False
    ):
        raise F0ClosureError("F0 source planning controls are invalid")
    flags = _mapping(root["required_flags"], "required_flags")
    if not flags or any(value is not False for value in flags.values()):
        raise F0ClosureError("F0 promotion flags must all remain false")
    return F0ClosurePolicy(
        path=policy_path,
        checksum=hashlib.sha256(payload).hexdigest(),
        version=str(root["policy_version"]),
        as_of_date=_iso_date(root["contract_as_of_date"], "contract_as_of_date"),
        blocks=tuple(blocks),
        queue_order=queue_order,
        shared_source_families=shared,
        payload=dict(root),
    )



def _candidate_rows(
    candidates: Sequence[HistoricalCandidate],
    *,
    selected_tickers: set[str],
    selected_asset_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        if (
            candidate.historical_ticker in selected_tickers
            or candidate.provider_asset_id in selected_asset_ids
        ):
            continue
        raw = candidate.as_dict()
        rows.append(
            {
                "queue_priority": f"{candidate.candidate_tier:02d}:"
                f"{'0' if candidate.event_source_url else '1'}:{candidate.cohort}:",
                "historical_ticker": candidate.historical_ticker,
                "provider_symbol": candidate.provider_symbol,
                "provider_asset_id": candidate.provider_asset_id,
                "company_name": candidate.company_name,
                "cohort_id": candidate.cohort,
                "candidate_tier": candidate.candidate_tier,
                "first_quoted_date": candidate.first_quoted_date,
                "provider_last_quoted_date": candidate.provider_last_quoted_date,
                "expected_terminal_type": candidate.expected_terminal_type,
                "expected_event_date": candidate.expected_event_date,
                "successor_ticker": candidate.successor_ticker,
                "existing_event_source_url": candidate.event_source_url,
                "decision": "",
                "allowed_decisions": "include|exclude",
                "decision_basis": "",
                "decision_source_id": "",
                "decision_source_url": "",
                "reviewed_on": "",
                "required_evidence": (
                    "stable provider identity; effective membership interval; primary terminal-event evidence"
                ),
                "input_row_sha256": _row_hash(raw),
            }
        )
    return sorted(rows, key=lambda row: (row["queue_priority"], row["historical_ticker"]))


def _terminal_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT r.event_key, COALESCE(s.ticker, c.primary_ticker) AS ticker,
               COALESCE(t.cohort_id, '') AS cohort_id, r.event_date,
               r.terminal_event_type, r.return_treatment,
               COALESCE(rule.outcome_class, '') AS outcome_class,
               COALESCE(rule.rule_status, '') AS rule_status,
               rule.bankruptcy_distribution_value,
               COALESCE(rule.distribution_currency, '') AS distribution_currency,
               r.source_id, r.evidence_json
        FROM fact_terminal_event_reconciliation r
        JOIN dim_company c ON c.company_id = r.company_id
        LEFT JOIN dim_security s ON s.security_id = r.security_id
        LEFT JOIN dim_basic_materials_taxonomy t ON t.security_id = r.security_id
        LEFT JOIN dim_terminal_return_rule rule ON rule.event_key = r.event_key
        WHERE r.resolved = 0
        ORDER BY r.event_date, ticker
    """
    return [
        {
            **dict(row),
            "reviewed_distribution_value": "",
            "reviewed_distribution_currency": "",
            "review_source_id": "",
            "review_source_url": "",
            "reviewed_on": "",
            "required_evidence": (
                "primary court, plan, transfer-agent, or SEC evidence for value delivered to old common equity"
            ),
        }
        for row in conn.execute(query).fetchall()
    ]


def _membership_rows(
    conn: sqlite3.Connection,
    *,
    target_history_start_date: str,
) -> list[dict[str, Any]]:
    query = """
        SELECT m.security_id, m.ticker, c.legal_name AS company_name, m.cohort_id,
               m.membership_start_date AS current_membership_start_date,
               m.current_source_only, m.survivorship_corrected,
               COALESCE(i.provider_asset_id, '') AS provider_asset_id,
               COALESCE(i.provider_symbol, '') AS provider_symbol,
               COALESCE(i.provider_first_quoted_date, '') AS provider_first_quoted_date,
               COALESCE(r.expected_start_date, '') AS contract_listing_start_date
        FROM dim_universe_membership m
        JOIN dim_company c ON c.company_id = m.company_id
        LEFT JOIN bridge_market_instrument_role r
          ON r.security_id = m.security_id AND r.role_type = 'current_universe'
        LEFT JOIN dim_market_instrument i ON i.instrument_id = r.instrument_id
        WHERE m.membership_status = 'current'
          AND m.current_source_only = 1
          AND m.membership_start_date > ?
        ORDER BY m.cohort_id, m.ticker
    """
    rows: list[dict[str, Any]] = []
    for item in conn.execute(query, (target_history_start_date,)).fetchall():
        rows.append(
            {
                **dict(item),
                "approved_membership_start_date": "",
                "membership_basis": "",
                "membership_source_id": "",
                "membership_source_url": "",
                "reviewed_on": "",
                "required_evidence": (
                    "effective-dated sector/cohort membership evidence; listing history is a lower bound, not membership proof"
                ),
            }
        )
    return rows


def _applicability_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    priority = {"core": 1, "supporting": 2, "optional": 3}
    query = """
        SELECT a.security_id, a.ticker, c.legal_name AS company_name, a.cohort_id,
               a.metric_id, mc.coverage_tier, m.metric_role, m.unit_family,
               m.period_type, m.dimension_family, m.definition,
               m.definition_variants_json, mc.source_families_json,
               mc.table_families_json
        FROM bridge_specialized_metric_applicability a
        JOIN dim_security s ON s.security_id = a.security_id
        JOIN dim_company c ON c.company_id = s.company_id
        JOIN dim_specialized_metric m ON m.metric_id = a.metric_id
        JOIN bridge_specialized_metric_cohort mc
          ON mc.metric_id = a.metric_id AND mc.cohort_id = a.cohort_id
        WHERE a.applicability_status = 'review_required'
        ORDER BY a.cohort_id, mc.coverage_tier, a.metric_id, a.ticker
    """
    rows: list[dict[str, Any]] = []
    for item in conn.execute(query).fetchall():
        row = dict(item)
        row.update(
            {
                "queue_priority": priority[str(item["coverage_tier"])],
                "decision": "",
                "allowed_decisions": "applicable|not_applicable",
                "decision_basis": "",
                "decision_source_id": "",
                "decision_source_url": "",
                "reviewed_on": "",
            }
        )
        rows.append(row)
    return rows



def _acquisition_key(row: Mapping[str, Any], shared_families: set[str]) -> tuple[str, str, str]:
    family = str(row["source_family"])
    record_key = str(row["source_record_key"])
    if family in shared_families:
        return (family, record_key, "shared_driver")
    if family == "sec_filing" and row["accession_number"]:
        return (family, str(row["accession_number"]), "document")
    return (family, f"{row['security_id']}:{record_key}", "issuer_discovery")


def _acquisition_rows(
    conn: sqlite3.Connection,
    *,
    source_policy: SpecializedSourcePolicy,
    shared_source_families: Sequence[str],
    contract_blocked: bool,
) -> list[dict[str, Any]]:
    applicability = {
        (int(row["security_id"]), str(row["metric_id"])): str(row["applicability_status"])
        for row in conn.execute(
            "SELECT security_id, metric_id, applicability_status "
            "FROM bridge_specialized_metric_applicability"
        ).fetchall()
    }
    source_rows = conn.execute(
        """
        SELECT census_key, security_id, ticker, cohort_id, source_family,
               source_record_key, accession_number, source_url, source_id,
               discovery_status, requested_metric_ids_json
        FROM fact_specialized_source_census
        WHERE discovery_status IN ('identified_unhydrated', 'discovery_required')
        ORDER BY source_family, source_record_key, security_id
        """
    ).fetchall()
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    shared = set(shared_source_families)
    for source in source_rows:
        source_dict = dict(source)
        natural_key = _acquisition_key(source_dict, shared)
        group = groups.setdefault(
            natural_key,
            {
                "source_family": str(source["source_family"]),
                "source_record_key": str(source["source_record_key"]),
                "source_id": str(source["source_id"]),
                "scope": natural_key[2],
                "census_keys": set(),
                "security_ids": set(),
                "tickers": set(),
                "cohorts": set(),
                "accessions": set(),
                "urls": set(),
                "statuses": set(),
                "resolved_metrics": set(),
                "review_metrics": set(),
            },
        )
        group["census_keys"].add(str(source["census_key"]))
        group["security_ids"].add(int(source["security_id"]))
        group["tickers"].add(str(source["ticker"]))
        group["cohorts"].add(str(source["cohort_id"]))
        group["statuses"].add(str(source["discovery_status"]))
        if source["accession_number"]:
            group["accessions"].add(str(source["accession_number"]))
        if source["source_url"]:
            group["urls"].add(str(source["source_url"]))
        requested = json.loads(str(source["requested_metric_ids_json"]))
        if not isinstance(requested, list):
            raise F0ClosureError(f"Invalid requested metric list for census row {source['census_key']}")
        for metric_id in requested:
            status = applicability.get((int(source["security_id"]), str(metric_id)))
            if status == "applicable":
                group["resolved_metrics"].add(str(metric_id))
            elif status == "review_required":
                group["review_metrics"].add(str(metric_id))

    priority = {family: index + 1 for index, family in enumerate(source_policy.source_family_priority)}
    rows: list[dict[str, Any]] = []
    for natural_key, group in groups.items():
        statuses = set(group["statuses"])
        status = "discovery_required" if "discovery_required" in statuses else "identified_unhydrated"
        if group["scope"] == "shared_driver":
            reuse = "hydrate shared driver once and fan out through the census bridge"
        elif group["source_family"] == "sec_filing":
            reuse = "link an existing content hash first; fetch only a cache miss"
        else:
            reuse = "discover issuer source, hash content, and collapse duplicate documents"
        acquisition_key = f"acq_{_row_hash(natural_key)[:24]}"
        rows.append(
            {
                "queue_priority": priority[str(group["source_family"])],
                "acquisition_key": acquisition_key,
                "acquisition_scope": group["scope"],
                "source_family": group["source_family"],
                "source_record_key": group["source_record_key"],
                "source_id": group["source_id"],
                "discovery_status": status,
                "census_row_count": len(group["census_keys"]),
                "security_count": len(group["security_ids"]),
                "ticker_count": len(group["tickers"]),
                "tickers_json": _canonical_json(sorted(group["tickers"])),
                "cohorts_json": _canonical_json(sorted(group["cohorts"])),
                "accession_numbers_json": _canonical_json(sorted(group["accessions"])),
                "source_urls_json": _canonical_json(sorted(group["urls"])),
                "resolved_metric_ids_json": _canonical_json(sorted(group["resolved_metrics"])),
                "review_blocked_metric_ids_json": _canonical_json(sorted(group["review_metrics"])),
                "reuse_strategy": reuse,
                "execution_status": (
                    "blocked_until_f0_contract_seals" if contract_blocked else "planned_not_executed"
                ),
            }
        )
    return sorted(rows, key=lambda row: (row["queue_priority"], row["source_family"], row["acquisition_key"]))


def build_f0_closure_workbench(
    conn: sqlite3.Connection,
    *,
    database_path: str | Path,
    policy: F0ClosurePolicy,
    pit_policy: HistoricalPitPreflightPolicy,
    registry: SpecializedMetricRegistry,
    source_policy: SpecializedSourcePolicy,
    historical_candidate_policy_path: str | Path,
    historical_candidate_manifest_path: str | Path,
    historical_candidates_path: str | Path,
) -> F0ClosureReport:
    """Build review queues and an acquisition plan without mutating SQLite."""

    assert_database_identity(conn)
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise F0ClosureError("F0 closure workbench requires a query-only database connection")
    if not (policy.as_of_date == pit_policy.as_of_date == registry.as_of_date == source_policy.as_of_date):
        raise F0ClosureError("F0 closure, PIT, metric, and source policies must share one as-of date")
    expected_blocks = tuple((block.block_id, block.start_date, block.end_date) for block in policy.blocks)
    actual_blocks = tuple((block.block_id, block.start_date, block.end_date) for block in pit_policy.blocks)
    if actual_blocks != expected_blocks:
        raise F0ClosureError("F0 closure chronological blocks differ from the executable PIT policy")

    path = Path(database_path).resolve()
    before_stat = (path.stat().st_size, path.stat().st_mtime_ns)
    before_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    before_counts = _table_counts(conn)

    candidate_policy = load_historical_candidate_policy(historical_candidate_policy_path)
    candidate_manifest = validate_historical_candidate_manifest(
        historical_candidate_manifest_path,
        historical_candidates_path,
    )
    candidates = read_and_validate_historical_candidates(historical_candidates_path, candidate_policy)
    selected_tickers = {
        str(row[0])
        for row in conn.execute(
            "SELECT ticker FROM dim_universe_membership WHERE membership_status='historical'"
        ).fetchall()
    }
    selected_asset_ids = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT i.provider_asset_id
            FROM bridge_market_instrument_role r
            JOIN dim_market_instrument i ON i.instrument_id = r.instrument_id
            WHERE r.role_type = 'historical_pilot'
            """
        ).fetchall()
    }
    candidate_rows = _candidate_rows(
        candidates,
        selected_tickers=selected_tickers,
        selected_asset_ids=selected_asset_ids,
    )
    terminal_rows = _terminal_rows(conn)
    membership_rows = _membership_rows(
        conn,
        target_history_start_date=pit_policy.target_history_start_date,
    )
    applicability_rows = _applicability_rows(conn)
    open_source_rows = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM fact_specialized_source_census
            WHERE discovery_status IN ('identified_unhydrated', 'discovery_required')
            """
        ).fetchone()[0]
    )
    contract_blocked = bool(candidate_rows or terminal_rows or membership_rows or applicability_rows)
    acquisition_rows = _acquisition_rows(
        conn,
        source_policy=source_policy,
        shared_source_families=policy.shared_source_families,
        contract_blocked=contract_blocked,
    )

    blockers: list[dict[str, Any]] = []
    for code, rows, message in (
        ("HISTORICAL_CANDIDATE_DECISIONS_OPEN", candidate_rows, "historical candidate decisions"),
        ("TERMINAL_DISTRIBUTIONS_OPEN", terminal_rows, "terminal distributions"),
        ("CURRENT_MEMBERSHIP_HISTORY_OPEN", membership_rows, "current membership histories"),
        ("SPECIALIZED_APPLICABILITY_REVIEWS_OPEN", applicability_rows, "metric applicability reviews"),
    ):
        if rows:
            blockers.append(
                {
                    "issue_code": code,
                    "severity": "blocker",
                    "count": len(rows),
                    "message": f"Resolve {len(rows)} {message}",
                }
            )
    if open_source_rows:
        blockers.append(
            {
                "issue_code": "SPECIALIZED_SOURCE_ROWS_OPEN",
                "severity": "blocker",
                "count": open_source_rows,
                "message": f"Hydrate or terminally dispose {open_source_rows} specialized source rows",
            }
        )

    after_counts = _table_counts(conn)
    after_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    after_stat = (path.stat().st_size, path.stat().st_mtime_ns)
    database_unchanged = before_counts == after_counts and before_version == after_version and before_stat == after_stat
    if not database_unchanged:
        blockers.append(
            {
                "issue_code": "F0_WORKBENCH_WRITE_VIOLATION",
                "severity": "error",
                "count": 1,
                "message": "Database changed during the query-only F0 workbench",
            }
        )
    structurally_valid = database_unchanged
    acquisition_units = len(acquisition_rows)
    counts = {
        "candidate_census_rows": len(candidates),
        "historical_candidates_selected": len(candidates) - len(candidate_rows),
        "historical_candidate_decisions_open": len(candidate_rows),
        "terminal_distributions_open": len(terminal_rows),
        "current_membership_histories_open": len(membership_rows),
        "specialized_applicability_reviews_open": len(applicability_rows),
        "specialized_source_rows_open": open_source_rows,
        "source_acquisition_units": acquisition_units,
        "source_rows_avoided_before_content_hashing": open_source_rows - acquisition_units,
        "shared_driver_acquisition_units": sum(
            row["acquisition_scope"] == "shared_driver" for row in acquisition_rows
        ),
        "candidate_manifest_sha256": candidate_manifest["sha256"],
    }
    return F0ClosureReport(
        structurally_valid=structurally_valid,
        f0_closure_ready=structurally_valid and not blockers,
        database_unchanged=database_unchanged,
        policy_version=policy.version,
        policy_sha256=policy.checksum,
        database_path=str(path),
        counts=counts,
        blockers=tuple(blockers),
        candidate_rows=tuple(candidate_rows),
        terminal_rows=tuple(terminal_rows),
        membership_rows=tuple(membership_rows),
        applicability_rows=tuple(applicability_rows),
        acquisition_rows=tuple(acquisition_rows),
    )



def write_f0_closure_workbench(
    report: F0ClosureReport,
    *,
    report_dir: str | Path,
) -> dict[str, str]:
    output = Path(report_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output / "f0_closure_summary.json",
        "blockers": output / "f0_closure_blockers.csv",
        "candidate_decisions": output / "historical_candidate_decisions.csv",
        "terminal_distributions": output / "terminal_distribution_reviews.csv",
        "membership_history": output / "current_membership_history_reviews.csv",
        "applicability": output / "specialized_metric_applicability_reviews.csv",
        "source_acquisition": output / "specialized_source_acquisition_plan.csv",
        "artifact_manifest": output / "artifact_manifest.json",
    }
    atomic_write_json(paths["summary"], report.summary_dict())
    atomic_write_csv(
        paths["blockers"],
        report.blockers,
        ("issue_code", "severity", "count", "message"),
    )
    atomic_write_csv(paths["candidate_decisions"], report.candidate_rows, CANDIDATE_FIELDS)
    atomic_write_csv(paths["terminal_distributions"], report.terminal_rows, TERMINAL_FIELDS)
    atomic_write_csv(paths["membership_history"], report.membership_rows, MEMBERSHIP_FIELDS)
    atomic_write_csv(paths["applicability"], report.applicability_rows, APPLICABILITY_FIELDS)
    atomic_write_csv(paths["source_acquisition"], report.acquisition_rows, ACQUISITION_FIELDS)
    manifest = {
        "policy_version": report.policy_version,
        "policy_sha256": report.policy_sha256,
        "database_unchanged": report.database_unchanged,
        "f0_closure_ready": report.f0_closure_ready,
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
