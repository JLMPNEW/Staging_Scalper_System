from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from io import TextIOWrapper
from pathlib import Path
from typing import Any, TextIO

from industrials.core.reports import write_text_atomic


MODEL_FAMILY = "transportation"
PREFLIGHT_VERSION = "transportation_dp8_historical_impact_preflight_v1"
PANEL_VERSION = "transportation_metrics_v3_discovery_panel_v1"
COMPLETE_PANEL_METRIC_COUNT = 108
DISCOVERY_METRIC_COUNT = 90
GENERIC_METRIC_COUNT = 18

PREFLIGHT_FIELDS = (
    "ticker",
    "universe_role",
    "calibration_cohort",
    "industry",
    "primary_archetype",
    "metric_id",
    "source_lane",
    "applicability_status",
    "final_coverage_status",
    "metric_disposition",
    "calibration_candidate",
    "first_evidence_availability_date",
    "first_accepted_availability_date",
    "first_affected_snapshot_date",
    "affected_snapshot_count",
    "impact_reason",
)

PANEL_FIELDS = (
    "asof_date",
    "ticker",
    "model_family",
    "calibration_cohort",
    "industry",
    "universe_role",
    "primary_archetype",
    "metric_family",
    "metric_id",
    "component",
    "source_lane",
    "metric_disposition",
    "calibration_candidate",
    "applicability_status",
    "availability_status",
    "metric_value",
    "unit",
    "period_start",
    "period_end",
    "availability_date",
    "source_id",
    "source_record_id",
    "confidence",
    "final_coverage_status",
    "status_reason",
)

COVERAGE_FIELDS = (
    "metric_id",
    "metric_pack",
    "source_lane",
    "metric_disposition",
    "calibration_candidate",
    "historical_membership_rows",
    "applicable_membership_rows",
    "value_membership_rows",
    "value_ticker_count",
    "first_value_asof_date",
    "last_value_asof_date",
    "historical_value_coverage_rate",
)


@dataclass(frozen=True)
class Evidence:
    ticker: str
    metric_id: str
    value: float
    unit: str
    period_start: str
    period_end: str
    availability_date: str
    source_record_id: str
    accession_number: str
    scope: str
    confidence: float


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def read_only_connection(path: Path, *, timeout_sec: float = 120.0) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=timeout_sec,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _open_deterministic_gzip_csv(path: Path) -> tuple[Path, TextIO, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".tmp-",
        suffix=".csv.gz",
        dir=str(path.parent),
    )
    raw = os.fdopen(descriptor, "wb")
    compressed = gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=raw,
        mtime=0,
    )
    text = TextIOWrapper(compressed, encoding="utf-8", newline="")
    writer = csv.DictWriter(text, fieldnames=PANEL_FIELDS)
    writer.writeheader()
    return Path(temp_name), text, writer


def _publish_gzip_csv(temp_path: Path, text: TextIO, final_path: Path) -> None:
    try:
        text.flush()
        text.close()
        os.replace(temp_path, final_path)
    except BaseException:
        try:
            text.close()
        finally:
            temp_path.unlink(missing_ok=True)
        raise


def iter_gzip_csv(path: Path) -> Iterator[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            yield {str(key): str(value or "") for key, value in row.items()}


def verify_artifact(reference: Mapping[str, Any], *, label: str) -> Path:
    path = Path(str(reference.get("path") or "")).expanduser().resolve()
    expected = str(reference.get("sha256") or "")
    if not path.is_file():
        raise FileNotFoundError(f"{label}: {path}")
    actual = sha256(path)
    if not expected or actual != expected:
        raise ValueError(
            f"{label}: sha256 mismatch expected={expected} actual={actual}"
        )
    return path


def snapshot_dates(build_manifest: Mapping[str, Any]) -> list[str]:
    dates = sorted(str(value) for value in build_manifest.get("completed_dates", []))
    if not dates or len(dates) != len(set(dates)):
        raise ValueError("Frozen v2 completed dates must be nonempty and unique")
    return dates


def verify_v2_snapshots(
    *,
    historical_root: Path,
    validation_manifest: Mapping[str, Any],
) -> dict[str, str]:
    if (
        validation_manifest.get("acceptance") != "PASS"
        or validation_manifest.get("panel_status") != "FROZEN"
    ):
        raise ValueError("The v2 PIT panel is not passing and frozen")
    expected = validation_manifest.get("snapshot_sha256")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("The v2 validation manifest has no snapshot hashes")
    aggregate: dict[str, str] = {}
    for asof, files in sorted(expected.items()):
        if not isinstance(files, dict):
            raise ValueError(f"{asof}: invalid snapshot hash mapping")
        for name, expected_hash in sorted(files.items()):
            path = historical_root / str(asof) / str(name)
            if not path.is_file():
                raise FileNotFoundError(path)
            actual = sha256(path)
            if actual != str(expected_hash):
                raise ValueError(
                    f"{asof}/{name}: frozen v2 hash changed "
                    f"expected={expected_hash} actual={actual}"
                )
            aggregate[f"{asof}/{name}"] = actual
    return aggregate


def evidence_lineage(
    *,
    connection: sqlite3.Connection,
    evaluation_ids: Sequence[int],
    supplemental_run_ids: Sequence[int] = (),
) -> list[dict[str, Any]]:
    requested_evaluations = sorted({int(value) for value in evaluation_ids})
    if not requested_evaluations or any(value <= 0 for value in requested_evaluations):
        raise ValueError("Reviewed evidence lineage requires positive evaluation IDs")
    evaluation_placeholders = ",".join("?" for _ in requested_evaluations)
    evaluation_rows = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT evaluation_id, base_run_id, model_family, status,
                   evaluated_evidence_count
            FROM sec_parser_review_evaluation
            WHERE evaluation_id IN ({evaluation_placeholders})
            """,
            requested_evaluations,
        )
    ]
    found_evaluations = {
        int(row["evaluation_id"]): row for row in evaluation_rows
    }
    missing_evaluations = sorted(
        set(requested_evaluations) - set(found_evaluations)
    )
    if missing_evaluations:
        raise ValueError(
            f"Review evaluations do not exist={missing_evaluations}"
        )
    invalid_evaluations = [
        evaluation_id
        for evaluation_id, row in sorted(found_evaluations.items())
        if str(row.get("model_family") or "") != MODEL_FAMILY
        or str(row.get("status") or "") != "COMPLETED"
    ]
    if invalid_evaluations:
        raise ValueError(
            "Review evaluations must be completed transportation evaluations="
            f"{invalid_evaluations}"
        )
    reviewed_run_ids = {
        int(row["base_run_id"]) for row in evaluation_rows
    }
    supplemental_runs = sorted({int(value) for value in supplemental_run_ids})
    if any(value <= 0 for value in supplemental_runs):
        raise ValueError("Supplemental evidence run IDs must be positive")
    overlap = sorted(reviewed_run_ids.intersection(supplemental_runs))
    if overlap:
        raise ValueError(
            "Reviewed runs cannot be loaded again as raw supplemental evidence="
            f"{overlap}"
        )
    output: list[dict[str, Any]] = []
    review_rows = connection.execute(
        f"""
        SELECT evaluation_id, ticker, metric_name, candidate_value, unit, period_start,
               period_end, accepted_at, filing_date, accession_number,
               evaluated_evidence_key AS evidence_key, scope, confidence,
               candidate_status
        FROM sec_parser_review_evidence
        WHERE evaluation_id IN ({evaluation_placeholders})
        """,
        requested_evaluations,
    )
    output.extend(dict(row) for row in review_rows)
    actual_review_counts: dict[int, int] = defaultdict(int)
    for row in output:
        actual_review_counts[int(row["evaluation_id"])] += 1
    count_mismatches = [
        (
            evaluation_id,
            int(found_evaluations[evaluation_id]["evaluated_evidence_count"]),
            actual_review_counts.get(evaluation_id, 0),
        )
        for evaluation_id in requested_evaluations
        if int(found_evaluations[evaluation_id]["evaluated_evidence_count"])
        != actual_review_counts.get(evaluation_id, 0)
    ]
    if count_mismatches:
        raise ValueError(
            "Review evaluation evidence counts changed="
            f"{count_mismatches}"
        )
    if supplemental_runs:
        placeholders = ",".join("?" for _ in supplemental_runs)
        run_rows = connection.execute(
            f"""
            SELECT NULL AS evaluation_id, evidence.ticker, evidence.metric_name,
                   evidence.candidate_value, evidence.unit,
                   evidence.period_start, evidence.period_end,
                   evidence.accepted_at, evidence.filing_date,
                   evidence.accession_number,
                   evidence.evidence_key, evidence.scope,
                   evidence.confidence, evidence.candidate_status
            FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS evidence
              ON evidence.evidence_key=relation.evidence_key
            WHERE relation.run_id IN ({placeholders})
              AND evidence.model_family=?
            """,
            (*supplemental_runs, MODEL_FAMILY),
        )
        output.extend(dict(row) for row in run_rows)
    deduplicated: dict[str, dict[str, Any]] = {}
    for row in output:
        key = str(row.get("evidence_key") or "")
        if not key:
            raise ValueError("Evidence lineage contains a blank evidence key")
        prior = deduplicated.get(key)
        if prior is None:
            deduplicated[key] = row
            continue
        comparable_fields = (
            "ticker",
            "metric_name",
            "candidate_value",
            "unit",
            "period_start",
            "period_end",
            "accepted_at",
            "filing_date",
            "accession_number",
            "scope",
            "confidence",
            "candidate_status",
        )
        if any(prior.get(field) != row.get(field) for field in comparable_fields):
            raise ValueError(
                "Conflicting reviewed evidence lineage for evidence_key="
                f"{key}"
            )
    return [deduplicated[key] for key in sorted(deduplicated)]


def first_evidence_dates(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, str]]:
    output: dict[tuple[str, str], dict[str, str]] = defaultdict(
        lambda: {"first_evidence": "", "first_accepted": ""}
    )
    for row in rows:
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("metric_name") or ""),
        )
        available = str(
            row.get("accepted_at") or row.get("filing_date") or ""
        )[:10]
        if not available:
            continue
        current = output[key]["first_evidence"]
        if not current or available < current:
            output[key]["first_evidence"] = available
        if str(row.get("candidate_status") or "") == "ACCEPTED":
            current = output[key]["first_accepted"]
            if not current or available < current:
                output[key]["first_accepted"] = available
    return dict(output)


def build_preflight_rows(
    *,
    scope_rows: Sequence[Mapping[str, str]],
    coverage_rows: Sequence[Mapping[str, str]],
    disposition_rows: Sequence[Mapping[str, str]],
    dates: Sequence[str],
    first_dates: Mapping[tuple[str, str], Mapping[str, str]],
) -> list[dict[str, object]]:
    coverage = {
        (row["ticker"], row["metric_id"]): row for row in coverage_rows
    }
    dispositions = {row["metric_id"]: row for row in disposition_rows}
    output: list[dict[str, object]] = []
    for scope in sorted(
        scope_rows,
        key=lambda row: (row["ticker"], row["metric_id"]),
    ):
        key = (scope["ticker"], scope["metric_id"])
        final = coverage.get(key)
        disposition = dispositions.get(scope["metric_id"])
        if final is None or disposition is None:
            raise ValueError(f"Missing final contract for {key}")
        evidence_dates = first_dates.get(
            key,
            {"first_evidence": "", "first_accepted": ""},
        )
        first_accepted = str(evidence_dates.get("first_accepted") or "")
        affected = [
            asof
            for asof in dates
            if not first_accepted or asof >= first_accepted
        ]
        output.append(
            {
                "ticker": scope["ticker"],
                "universe_role": scope["universe_role"],
                "calibration_cohort": scope["calibration_cohort"],
                "industry": scope["industry"],
                "primary_archetype": scope["primary_archetype"],
                "metric_id": scope["metric_id"],
                "source_lane": scope["source_lane"],
                "applicability_status": scope["applicability_status"],
                "final_coverage_status": final["coverage_status"],
                "metric_disposition": disposition["metric_disposition"],
                "calibration_candidate": disposition["calibration_candidate"],
                "first_evidence_availability_date": evidence_dates.get(
                    "first_evidence", ""
                ),
                "first_accepted_availability_date": first_accepted,
                "first_affected_snapshot_date": affected[0] if affected else "",
                "affected_snapshot_count": len(affected),
                "impact_reason": (
                    "new_v3_registry_requires_explicit_state_all_dates"
                    if not first_accepted
                    else "accepted_evidence_available_point_in_time"
                ),
            }
        )
    return output


def normalized_accepted_evidence(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[Evidence]]:
    exact: dict[tuple[str, str, str, str], Evidence] = {}
    scope_priority = {"consolidated": 3, "segment": 2, "unknown": 1}
    for row in rows:
        if (
            str(row.get("candidate_status") or "") != "ACCEPTED"
            or row.get("candidate_value") is None
        ):
            continue
        try:
            value = float(row["candidate_value"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        period_end = str(row.get("period_end") or "")[:10]
        available = str(
            row.get("accepted_at") or row.get("filing_date") or ""
        )[:10]
        try:
            date.fromisoformat(period_end)
            date.fromisoformat(available)
        except ValueError:
            continue
        if period_end > available:
            continue
        evidence = Evidence(
            ticker=str(row.get("ticker") or "").upper(),
            metric_id=str(row.get("metric_name") or ""),
            value=value,
            unit=str(row.get("unit") or ""),
            period_start=str(row.get("period_start") or "")[:10],
            period_end=period_end,
            availability_date=available,
            source_record_id=str(row.get("evidence_key") or ""),
            accession_number=str(row.get("accession_number") or ""),
            scope=str(row.get("scope") or ""),
            confidence=float(row.get("confidence") or 0.0),
        )
        exact_key = (
            evidence.ticker,
            evidence.metric_id,
            evidence.period_end,
            f"{evidence.value:.12g}",
        )
        prior = exact.get(exact_key)
        if prior is None or (
            scope_priority.get(evidence.scope, 0),
            evidence.confidence,
            evidence.availability_date,
            evidence.source_record_id,
        ) > (
            scope_priority.get(prior.scope, 0),
            prior.confidence,
            prior.availability_date,
            prior.source_record_id,
        ):
            exact[exact_key] = evidence
    conflicts: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for evidence in exact.values():
        conflicts[
            (evidence.ticker, evidence.metric_id, evidence.period_end)
        ].add(f"{evidence.value:.12g}")
    bad = {key: values for key, values in conflicts.items() if len(values) > 1}
    if bad:
        raise ValueError(f"Conflicting accepted evidence={list(bad.items())[:10]}")
    output: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for evidence in exact.values():
        output[(evidence.ticker, evidence.metric_id)].append(evidence)
    for key in output:
        output[key].sort(
            key=lambda item: (
                item.period_end,
                item.availability_date,
                item.source_record_id,
            )
        )
    return dict(output)


def choose_point_in_time_evidence(
    evidence: Sequence[Evidence],
    *,
    asof_date: str,
    max_staleness_days: int,
) -> Evidence | None:
    asof = date.fromisoformat(asof_date)
    eligible = [
        row
        for row in evidence
        if row.availability_date <= asof_date
        and row.period_end <= asof_date
        and (asof - date.fromisoformat(row.period_end)).days
        <= max_staleness_days
    ]
    return eligible[-1] if eligible else None


def _generic_panel_row(
    row: Mapping[str, str],
    *,
    scope: Mapping[str, str],
) -> dict[str, object]:
    status = row["availability_status"]
    return {
        "asof_date": row["asof_date"],
        "ticker": row["ticker"],
        "model_family": MODEL_FAMILY,
        "calibration_cohort": row["calibration_cohort"],
        "industry": row["industry"],
        "universe_role": scope["universe_role"],
        "primary_archetype": scope["primary_archetype"],
        "metric_family": "generic",
        "metric_id": row["metric_name"],
        "component": row["component"],
        "source_lane": "V2_GENERIC",
        "metric_disposition": "BASE_GENERIC",
        "calibration_candidate": 0,
        "applicability_status": (
            "NOT_APPLICABLE" if status == "NOT_APPLICABLE" else "APPLICABLE"
        ),
        "availability_status": status,
        "metric_value": row["metric_value"],
        "unit": row["unit"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "availability_date": row["filing_date"],
        "source_id": row["source_id"],
        "source_record_id": row["accession_number"],
        "confidence": row["confidence"],
        "final_coverage_status": "",
        "status_reason": row["status_reason"],
    }


def _specialized_panel_row(
    *,
    asof_date: str,
    scope: Mapping[str, str],
    coverage: Mapping[str, str],
    disposition: Mapping[str, str],
    registry: Mapping[str, str],
    accepted: Mapping[tuple[str, str], Sequence[Evidence]],
    generic_fallback: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    ticker = scope["ticker"]
    metric_id = scope["metric_id"]
    applicable = scope["applicability_status"]
    selected: Evidence | None = None
    fallback = None
    if applicable == "APPLICABLE" and scope["source_lane"] == "DP":
        selected = choose_point_in_time_evidence(
            accepted.get((ticker, metric_id), ()),
            asof_date=asof_date,
            max_staleness_days=int(scope["max_staleness_days"]),
        )
    elif applicable == "APPLICABLE" and scope["source_lane"] == "FIN-D":
        candidate = generic_fallback.get(metric_id)
        if candidate and candidate.get("metric_value", "") != "":
            fallback = candidate
    if applicable != "APPLICABLE":
        status = "NOT_APPLICABLE"
        reason = scope["applicability_reason"]
    elif selected is not None:
        status = "REPORTED"
        reason = "reviewed_accepted_evidence_available_point_in_time"
    elif fallback is not None:
        status = "DERIVED"
        reason = "reused_frozen_v2_financial_derived_value"
    else:
        status = "NOT_AVAILABLE_ASOF"
        reason = (
            "no_reviewed_accepted_value_available_by_snapshot"
            if scope["source_lane"] == "DP"
            else "formula_or_financial_value_not_available_in_frozen_inputs"
        )
    return {
        "asof_date": asof_date,
        "ticker": ticker,
        "model_family": MODEL_FAMILY,
        "calibration_cohort": scope["calibration_cohort"],
        "industry": scope["industry"],
        "universe_role": scope["universe_role"],
        "primary_archetype": scope["primary_archetype"],
        "metric_family": "specialized_discovery",
        "metric_id": metric_id,
        "component": registry["component"],
        "source_lane": scope["source_lane"],
        "metric_disposition": disposition["metric_disposition"],
        "calibration_candidate": disposition["calibration_candidate"],
        "applicability_status": applicable,
        "availability_status": status,
        "metric_value": (
            selected.value
            if selected is not None
            else fallback["metric_value"]
            if fallback is not None
            else ""
        ),
        "unit": (
            selected.unit
            if selected is not None
            else fallback["unit"]
            if fallback is not None
            else registry["unit_contract"]
        ),
        "period_start": (
            selected.period_start
            if selected is not None
            else fallback["period_start"]
            if fallback is not None
            else ""
        ),
        "period_end": (
            selected.period_end
            if selected is not None
            else fallback["period_end"]
            if fallback is not None
            else ""
        ),
        "availability_date": (
            selected.availability_date
            if selected is not None
            else fallback["filing_date"]
            if fallback is not None
            else ""
        ),
        "source_id": (
            "dedicated_parser_transportation_reviewed_shadow"
            if selected is not None
            else fallback["source_id"]
            if fallback is not None
            else ""
        ),
        "source_record_id": (
            selected.source_record_id
            if selected is not None
            else fallback["accession_number"]
            if fallback is not None
            else ""
        ),
        "confidence": (
            selected.confidence
            if selected is not None
            else fallback["confidence"]
            if fallback is not None
            else ""
        ),
        "final_coverage_status": coverage["coverage_status"],
        "status_reason": reason,
    }


def materialize_panels(
    *,
    historical_root: Path,
    dates: Sequence[str],
    scope_rows: Sequence[Mapping[str, str]],
    coverage_rows: Sequence[Mapping[str, str]],
    disposition_rows: Sequence[Mapping[str, str]],
    discovery_registry_rows: Sequence[Mapping[str, str]],
    generic_metric_ids: Sequence[str],
    accepted: Mapping[tuple[str, str], Sequence[Evidence]],
    discovery_path: Path,
    complete_path: Path,
) -> dict[str, Any]:
    scope = {(row["ticker"], row["metric_id"]): row for row in scope_rows}
    scope_by_ticker: dict[str, Mapping[str, str]] = {}
    for row in scope_rows:
        scope_by_ticker.setdefault(row["ticker"], row)
    coverage = {
        (row["ticker"], row["metric_id"]): row for row in coverage_rows
    }
    dispositions = {row["metric_id"]: row for row in disposition_rows}
    registry = {row["metric_id"]: row for row in discovery_registry_rows}
    specialized_ids = sorted(registry)
    if len(specialized_ids) != DISCOVERY_METRIC_COUNT:
        raise ValueError(
            f"Discovery metric count={len(specialized_ids)} "
            f"expected={DISCOVERY_METRIC_COUNT}"
        )
    generic_ids = sorted(generic_metric_ids)
    if len(generic_ids) != GENERIC_METRIC_COUNT:
        raise ValueError(
            f"Generic metric count={len(generic_ids)} expected={GENERIC_METRIC_COUNT}"
        )
    discovery_temp, discovery_text, discovery_writer = (
        _open_deterministic_gzip_csv(discovery_path)
    )
    complete_temp, complete_text, complete_writer = (
        _open_deterministic_gzip_csv(complete_path)
    )
    discovery_count = 0
    complete_count = 0
    membership_count = 0
    stats: dict[str, dict[str, Any]] = {
        metric_id: {
            "historical": 0,
            "applicable": 0,
            "value_rows": 0,
            "tickers": set(),
            "first": "",
            "last": "",
        }
        for metric_id in specialized_ids
    }
    try:
        for asof in dates:
            availability_rows = read_csv(
                historical_root / asof / "metric_availability.csv"
            )
            by_ticker: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
            for row in availability_rows:
                by_ticker[row["ticker"]][row["metric_name"]] = row
            for ticker in sorted(by_ticker):
                membership_count += 1
                generic_by_metric = by_ticker[ticker]
                ticker_scope = scope_by_ticker.get(ticker)
                if ticker_scope is None:
                    raise ValueError(f"{asof}: ticker absent from v3 scope={ticker}")
                for metric_id in generic_ids:
                    generic = generic_by_metric.get(metric_id)
                    if generic is None:
                        raise ValueError(
                            f"{asof}:{ticker}: missing generic metric={metric_id}"
                        )
                    row = _generic_panel_row(generic, scope=ticker_scope)
                    complete_writer.writerow(row)
                    complete_count += 1
                for metric_id in specialized_ids:
                    key = (ticker, metric_id)
                    scoped = scope.get(key)
                    final = coverage.get(key)
                    disposition = dispositions.get(metric_id)
                    if scoped is None or final is None or disposition is None:
                        raise ValueError(f"{asof}: incomplete v3 contract={key}")
                    row = _specialized_panel_row(
                        asof_date=asof,
                        scope=scoped,
                        coverage=final,
                        disposition=disposition,
                        registry=registry[metric_id],
                        accepted=accepted,
                        generic_fallback=generic_by_metric,
                    )
                    discovery_writer.writerow(row)
                    complete_writer.writerow(row)
                    discovery_count += 1
                    complete_count += 1
                    metric_stats = stats[metric_id]
                    metric_stats["historical"] += 1
                    if scoped["applicability_status"] == "APPLICABLE":
                        metric_stats["applicable"] += 1
                    if row["metric_value"] != "":
                        metric_stats["value_rows"] += 1
                        metric_stats["tickers"].add(ticker)
                        metric_stats["first"] = (
                            min(metric_stats["first"], asof)
                            if metric_stats["first"]
                            else asof
                        )
                        metric_stats["last"] = max(metric_stats["last"], asof)
        expected_discovery = membership_count * DISCOVERY_METRIC_COUNT
        expected_complete = membership_count * COMPLETE_PANEL_METRIC_COUNT
        if discovery_count != expected_discovery:
            raise ValueError(
                f"Discovery rows={discovery_count} expected={expected_discovery}"
            )
        if complete_count != expected_complete:
            raise ValueError(
                f"Complete rows={complete_count} expected={expected_complete}"
            )
        _publish_gzip_csv(discovery_temp, discovery_text, discovery_path)
        _publish_gzip_csv(complete_temp, complete_text, complete_path)
    except BaseException:
        try:
            discovery_text.close()
        finally:
            discovery_temp.unlink(missing_ok=True)
        try:
            complete_text.close()
        finally:
            complete_temp.unlink(missing_ok=True)
        raise
    coverage_rows_out: list[dict[str, object]] = []
    for metric_id in specialized_ids:
        metric_stats = stats[metric_id]
        disposition = dispositions[metric_id]
        registry_row = registry[metric_id]
        applicable = int(metric_stats["applicable"])
        value_rows = int(metric_stats["value_rows"])
        coverage_rows_out.append(
            {
                "metric_id": metric_id,
                "metric_pack": registry_row["metric_pack"],
                "source_lane": registry_row["source_lane"],
                "metric_disposition": disposition["metric_disposition"],
                "calibration_candidate": disposition["calibration_candidate"],
                "historical_membership_rows": metric_stats["historical"],
                "applicable_membership_rows": applicable,
                "value_membership_rows": value_rows,
                "value_ticker_count": len(metric_stats["tickers"]),
                "first_value_asof_date": metric_stats["first"],
                "last_value_asof_date": metric_stats["last"],
                "historical_value_coverage_rate": (
                    value_rows / applicable if applicable else 0.0
                ),
            }
        )
    return {
        "membership_row_count": membership_count,
        "discovery_row_count": discovery_count,
        "complete_row_count": complete_count,
        "coverage_rows": coverage_rows_out,
    }


def validate_panel_stream(
    *,
    path: Path,
    historical_root: Path,
    dates: Sequence[str],
    expected_metric_keys: Sequence[tuple[str, str]],
) -> tuple[int, list[str]]:
    errors: list[str] = []
    row_count = 0
    iterator = iter_gzip_csv(path)
    for asof in dates:
        snapshot = read_csv(historical_root / asof / "metric_availability.csv")
        tickers = sorted({row["ticker"] for row in snapshot})
        for ticker in tickers:
            for expected_family, expected_metric in expected_metric_keys:
                try:
                    row = next(iterator)
                except StopIteration:
                    errors.append(
                        f"early EOF at {asof}:{ticker}:{expected_family}:{expected_metric}"
                    )
                    return row_count, errors
                row_count += 1
                actual = (
                    row["asof_date"],
                    row["ticker"],
                    row["metric_family"],
                    row["metric_id"],
                )
                expected = (
                    asof,
                    ticker,
                    expected_family,
                    expected_metric,
                )
                if actual != expected:
                    errors.append(f"order/contract mismatch actual={actual} expected={expected}")
                    if len(errors) >= 100:
                        return row_count, errors
                if row["availability_date"] and row["availability_date"] > asof:
                    errors.append(
                        f"future availability={asof}:{ticker}:{expected_metric}:"
                        f"{row['availability_date']}"
                    )
                if row["period_end"] and row["period_end"] > asof:
                    errors.append(
                        f"future period={asof}:{ticker}:{expected_metric}:"
                        f"{row['period_end']}"
                    )
                if row["metric_value"]:
                    try:
                        if not math.isfinite(float(row["metric_value"])):
                            raise ValueError
                    except ValueError:
                        errors.append(
                            f"nonfinite value={asof}:{ticker}:{expected_metric}"
                        )
                if len(errors) >= 100:
                    return row_count, errors
    try:
        extra = next(iterator)
    except StopIteration:
        return row_count, errors
    errors.append(
        "unexpected trailing row="
        f"{extra.get('asof_date')}:{extra.get('ticker')}:{extra.get('metric_id')}"
    )
    return row_count + 1, errors
