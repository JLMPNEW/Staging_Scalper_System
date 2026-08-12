from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from med_devices.core.config import cfg_get, resolve_path
from med_devices.core.text_norm import normalize_org_name, normalize_ticker


DEFAULT_EXCLUDED_METHODS = {
    "do_not_map",
    "inactive_or_delisted",
    "international_excluded",
    "non_us_traded_parent",
    "not_in_investible_universe",
    "out_of_universe",
    "private_excluded",
}
DEFAULT_MAPPED_OVERRIDE_METHODS = {
    "manual_override",
    "mapped",
}
DEFAULT_ALLOWED_OVERRIDE_METHODS = DEFAULT_EXCLUDED_METHODS | DEFAULT_MAPPED_OVERRIDE_METHODS
FIELDNAMES = [
    "severity",
    "issue_type",
    "source",
    "fda_manufacturer_id",
    "manufacturer_name",
    "mapped_ticker",
    "mapped_company_name",
    "mapping_confidence",
    "mapping_method",
    "total_fda_rows",
    "manual_override_used",
    "high_volume_unmapped",
    "review_reason",
    "expected_ticker",
    "expected_mapping_method",
    "observed",
    "recommended_action",
]


@dataclass(frozen=True)
class FdaMappingGovernanceResult:
    output_csv: Path
    issue_count: int
    critical_count: int
    warning_count: int
    ambiguous_count: int
    high_volume_unmapped_count: int
    low_confidence_mapped_count: int


def _as_float(raw: object, default: float = 0.0) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_int(raw: object, default: int = 0) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _parse_csv_set(raw: object, default: set[str]) -> set[str]:
    if raw is None:
        return set(default)
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").split(",")
    out = {str(item or "").strip().lower() for item in values if str(item or "").strip()}
    return out or set(default)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _active_company_by_ticker(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, UPPER(ticker) AS ticker, company_name, universe_status, is_active
        FROM dim_company
        """
    ).fetchall()
    return {str(row["ticker"] or "").upper(): dict(row) for row in rows if str(row["ticker"] or "").strip()}


def _company_by_id(conn: Any) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, UPPER(ticker) AS ticker, company_name, universe_status, is_active
        FROM dim_company
        """
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows if row["company_id"] is not None}


def _company_is_active(company: dict[str, Any] | None) -> bool:
    if not company:
        return False
    return int(company.get("is_active") or 0) == 1 and str(company.get("universe_status") or "").strip().lower() == "keep"


def _company_is_review_status(company: dict[str, Any] | None) -> bool:
    # universe_status='review' companies are still active and scored, so a mapping to them
    # is a warning (surface for follow-up), not a critical QA-gate blocker
    if not company:
        return False
    return int(company.get("is_active") or 0) == 1 and str(company.get("universe_status") or "").strip().lower() == "review"


def _issue(
    *,
    severity: str,
    issue_type: str,
    source: str,
    row: dict[str, Any] | None = None,
    expected_ticker: str = "",
    expected_mapping_method: str = "",
    observed: str = "",
    recommended_action: str,
) -> dict[str, Any]:
    row = row or {}
    return {
        "severity": severity,
        "issue_type": issue_type,
        "source": source,
        "fda_manufacturer_id": row.get("fda_manufacturer_id", ""),
        "manufacturer_name": row.get("manufacturer_name", ""),
        "mapped_ticker": row.get("mapped_ticker") or row.get("ticker") or "",
        "mapped_company_name": row.get("mapped_company_name", ""),
        "mapping_confidence": row.get("mapping_confidence") or row.get("confidence") or "",
        "mapping_method": row.get("mapping_method", ""),
        "total_fda_rows": row.get("total_fda_rows", ""),
        "manual_override_used": row.get("manual_override_used", ""),
        "high_volume_unmapped": row.get("high_volume_unmapped", ""),
        "review_reason": row.get("review_reason", ""),
        "expected_ticker": expected_ticker,
        "expected_mapping_method": expected_mapping_method,
        "observed": observed,
        "recommended_action": recommended_action,
    }


def _mapping_row_by_id(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {str(row.get("fda_manufacturer_id") or "").strip(): row for row in rows if str(row.get("fda_manufacturer_id") or "").strip()}


def _audit_mapping_rows(
    rows: list[dict[str, str]],
    *,
    active_companies: dict[str, dict[str, Any]],
    min_mapped_confidence: float,
    low_confidence_review_threshold: float,
) -> tuple[list[dict[str, Any]], int, int, int]:
    issues: list[dict[str, Any]] = []
    ambiguous_count = 0
    high_volume_count = 0
    low_confidence_count = 0
    for row in rows:
        method = str(row.get("mapping_method") or "").strip().lower()
        ticker = normalize_ticker(row.get("mapped_ticker"))
        confidence = _as_float(row.get("mapping_confidence"))
        total_fda_rows = _as_int(row.get("total_fda_rows"), 0)
        # Dimension rows can outlive the facts that introduced them. Preserve
        # them for lineage, but only facts in active use require adjudication.
        if method == "ambiguous" and total_fda_rows > 0:
            ambiguous_count += 1
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="ambiguous_mapping",
                    source="fda_entity_mapping",
                    row=row,
                    observed=str(row.get("candidate_summary") or ""),
                    recommended_action="Add a manual override or explicit out_of_universe exclusion.",
                )
            )
        if _as_int(row.get("high_volume_unmapped")):
            high_volume_count += 1
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="high_volume_unmapped",
                    source="fda_entity_mapping",
                    row=row,
                    observed=f"total_fda_rows={row.get('total_fda_rows')}",
                    recommended_action="Review parent ownership and add a manual override/exclusion.",
                )
            )
        if ticker:
            company = active_companies.get(ticker)
            if _company_is_review_status(company):
                issues.append(
                    _issue(
                        severity="warning",
                        issue_type="mapped_to_review_status_ticker",
                        source="fda_entity_mapping",
                        row=row,
                        observed=f"ticker={ticker}",
                        recommended_action="Resolve the universe review status; the ticker is still active and scored.",
                    )
                )
            elif not _company_is_active(company):
                issues.append(
                    _issue(
                        severity="critical",
                        issue_type="mapped_to_inactive_or_missing_ticker",
                        source="fda_entity_mapping",
                        row=row,
                        observed=f"ticker={ticker}",
                        recommended_action="Map to an active universe ticker or change the override to out_of_universe.",
                    )
                )
            if confidence < min_mapped_confidence:
                issues.append(
                    _issue(
                        severity="critical",
                        issue_type="mapped_below_min_confidence",
                        source="fda_entity_mapping",
                        row=row,
                        observed=f"confidence={confidence:.2f}",
                        recommended_action="Promote only after manual review or add an explicit exclusion.",
                    )
                )
            elif confidence < low_confidence_review_threshold:
                low_confidence_count += 1
                issues.append(
                    _issue(
                        severity="warning",
                        issue_type="mapped_below_high_confidence",
                        source="fda_entity_mapping",
                        row=row,
                        observed=f"confidence={confidence:.2f}",
                        recommended_action="Review for false-positive string matching during the next mapping pass.",
                    )
                )
    return issues, ambiguous_count, high_volume_count, low_confidence_count


def _audit_overrides(
    rows: list[dict[str, str]],
    *,
    active_companies: dict[str, dict[str, Any]],
    companies_by_id: dict[int, dict[str, Any]],
    allowed_methods: set[str],
    excluded_methods: set[str],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in rows:
        method = str(row.get("mapping_method") or row.get("method") or "manual_override").strip().lower()
        ticker = normalize_ticker(row.get("ticker"))
        company_id_raw = str(row.get("company_id") or "").strip()
        if method not in allowed_methods:
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="override_invalid_method",
                    source="manual_overrides",
                    row=row,
                    observed=method,
                    recommended_action="Use a documented override status.",
                )
            )
        if method in excluded_methods:
            if ticker or company_id_raw:
                issues.append(
                    _issue(
                        severity="warning",
                        issue_type="excluded_override_has_ticker_or_company_id",
                        source="manual_overrides",
                        row=row,
                        observed=f"ticker={ticker};company_id={company_id_raw}",
                        recommended_action="Leave ticker/company_id blank for explicit exclusions.",
                    )
                )
            continue
        company = active_companies.get(ticker) if ticker else None
        if ticker and _company_is_review_status(company):
            issues.append(
                _issue(
                    severity="warning",
                    issue_type="mapped_to_review_status_ticker",
                    source="manual_overrides",
                    row=row,
                    observed=f"ticker={ticker}",
                    recommended_action="Resolve the universe review status; the ticker is still active and scored.",
                )
            )
        elif ticker and not _company_is_active(company):
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="override_ticker_not_active",
                    source="manual_overrides",
                    row=row,
                    observed=f"ticker={ticker}",
                    recommended_action="Use an active ticker or mark the override out_of_universe.",
                )
            )
        if company_id_raw:
            try:
                company_id = int(company_id_raw)
            except ValueError:
                company_id = -1
            company_by_id = companies_by_id.get(company_id)
            company_by_id_ticker = str(company_by_id.get("ticker") or "").upper() if company_by_id is not None else ""
            if _company_is_review_status(company_by_id):
                issues.append(
                    _issue(
                        severity="warning",
                        issue_type="mapped_to_review_status_ticker",
                        source="manual_overrides",
                        row=row,
                        observed=f"company_id={company_id_raw}",
                        recommended_action="Resolve the universe review status; the company is still active and scored.",
                    )
                )
                if ticker and company_by_id_ticker != ticker:
                    issues.append(
                        _issue(
                            severity="critical",
                            issue_type="override_ticker_company_id_mismatch",
                            source="manual_overrides",
                            row=row,
                            observed=f"ticker={ticker};company_id_ticker={company_by_id_ticker}",
                            recommended_action="Correct either ticker or company_id so they reference the same company.",
                        )
                    )
            elif not _company_is_active(company_by_id):
                issues.append(
                    _issue(
                        severity="critical",
                        issue_type="override_company_id_not_active",
                        source="manual_overrides",
                        row=row,
                        observed=f"company_id={company_id_raw}",
                        recommended_action="Use an active company_id or mark the override out_of_universe.",
                    )
                )
            elif ticker and company_by_id_ticker != ticker:
                issues.append(
                    _issue(
                        severity="critical",
                        issue_type="override_ticker_company_id_mismatch",
                        source="manual_overrides",
                        row=row,
                        observed=f"ticker={ticker};company_id_ticker={company_by_id_ticker}",
                        recommended_action="Correct either ticker or company_id so they reference the same company.",
                    )
                )
        if not ticker and not company_id_raw:
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="mapped_override_missing_target",
                    source="manual_overrides",
                    row=row,
                    observed="no ticker/company_id",
                    recommended_action="Supply an active ticker/company_id or mark the override out_of_universe.",
                )
            )
    return issues


def _audit_regression_cases(rows: list[dict[str, str]], cases: list[dict[str, str]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    by_id = _mapping_row_by_id(rows)
    for case in cases:
        case_id = str(case.get("case_id") or case.get("fda_manufacturer_id") or "").strip()
        manufacturer_id = str(case.get("fda_manufacturer_id") or "").strip()
        expected_ticker = normalize_ticker(case.get("expected_ticker"))
        expected_method = str(case.get("expected_mapping_method") or "").strip().lower()
        name_contains = normalize_org_name(case.get("manufacturer_name_contains"))
        row = by_id.get(manufacturer_id) if manufacturer_id else None
        if row is None and name_contains:
            for candidate in rows:
                if name_contains and name_contains in normalize_org_name(candidate.get("manufacturer_name")):
                    row = candidate
                    break
        if row is None:
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="regression_case_missing",
                    source="regression_cases",
                    row={"fda_manufacturer_id": manufacturer_id, "manufacturer_name": case.get("manufacturer_name_contains", "")},
                    expected_ticker=expected_ticker,
                    expected_mapping_method=expected_method,
                    observed="missing mapping row",
                    recommended_action=f"Rebuild FDA core/linking or update regression case {case_id}.",
                )
            )
            continue
        actual_ticker = normalize_ticker(row.get("mapped_ticker"))
        actual_method = str(row.get("mapping_method") or "").strip().lower()
        if expected_ticker != actual_ticker:
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="regression_ticker_mismatch",
                    source="regression_cases",
                    row=row,
                    expected_ticker=expected_ticker,
                    expected_mapping_method=expected_method,
                    observed=f"mapped_ticker={actual_ticker}",
                    recommended_action=f"Restore reviewed FDA mapping regression case {case_id}.",
                )
            )
        if expected_method and actual_method != expected_method:
            issues.append(
                _issue(
                    severity="critical",
                    issue_type="regression_method_mismatch",
                    source="regression_cases",
                    row=row,
                    expected_ticker=expected_ticker,
                    expected_mapping_method=expected_method,
                    observed=f"mapping_method={actual_method}",
                    recommended_action=f"Restore reviewed FDA mapping regression case {case_id}.",
                )
            )
    return issues


def audit_fda_mapping_governance(
    conn: Any,
    *,
    config: dict[str, Any],
    base_dir: Path,
    mapping_csv: Path | None = None,
    output_csv: Path | None = None,
    overrides_csv: Path | None = None,
    regression_cases_csv: Path | None = None,
) -> FdaMappingGovernanceResult:
    mapping_path = mapping_csv or resolve_path(
        cfg_get(
            config,
            "fda_mapping_governance.source_mapping_csv",
            cfg_get(config, "fda_entity_linking.output_csv", "../output/med_devices_reports/med_device_fda_entity_mapping.csv"),
        ),
        base_dir=base_dir,
    )
    output_path = output_csv or resolve_path(
        cfg_get(config, "fda_mapping_governance.output_csv", "../output/med_devices_reports/fda_mapping_review_queue.csv"),
        base_dir=base_dir,
    )
    overrides_raw = str(
        cfg_get(config, "fda_mapping_governance.manual_overrides_csv", cfg_get(config, "fda_entity_linking.manual_overrides_csv", ""))
        or ""
    ).strip()
    overrides_path = overrides_csv or (resolve_path(overrides_raw, base_dir=base_dir) if overrides_raw else None)
    regression_raw = str(cfg_get(config, "fda_mapping_governance.regression_cases_csv", "") or "").strip()
    regression_path = regression_cases_csv or (resolve_path(regression_raw, base_dir=base_dir) if regression_raw else None)
    min_mapped_confidence = _as_float(
        cfg_get(config, "fda_mapping_governance.min_mapped_confidence", cfg_get(config, "fda_entity_linking.min_auto_confidence", 75.0)),
        75.0,
    )
    low_confidence_review_threshold = _as_float(
        cfg_get(config, "fda_mapping_governance.low_confidence_review_threshold", cfg_get(config, "fda_entity_linking.high_confidence_threshold", 90.0)),
        90.0,
    )
    max_ambiguous = _as_int(cfg_get(config, "fda_mapping_governance.max_ambiguous", 0), 0)
    max_high_volume_unmapped = _as_int(cfg_get(config, "fda_mapping_governance.max_high_volume_unmapped", 0), 0)
    allowed_methods = _parse_csv_set(
        cfg_get(config, "fda_mapping_governance.allowed_override_methods", sorted(DEFAULT_ALLOWED_OVERRIDE_METHODS)),
        DEFAULT_ALLOWED_OVERRIDE_METHODS,
    )
    excluded_methods = _parse_csv_set(
        cfg_get(config, "fda_mapping_governance.excluded_override_methods", sorted(DEFAULT_EXCLUDED_METHODS)),
        DEFAULT_EXCLUDED_METHODS,
    )
    mapping_rows = _read_csv(mapping_path)
    active_companies = _active_company_by_ticker(conn)
    companies_by_id = _company_by_id(conn)
    issues, ambiguous_count, high_volume_count, low_confidence_count = _audit_mapping_rows(
        mapping_rows,
        active_companies=active_companies,
        min_mapped_confidence=min_mapped_confidence,
        low_confidence_review_threshold=low_confidence_review_threshold,
    )
    if ambiguous_count <= max_ambiguous or high_volume_count <= max_high_volume_unmapped:
        for issue in issues:
            if issue["issue_type"] == "ambiguous_mapping" and ambiguous_count <= max_ambiguous:
                issue["severity"] = "warning"
            if issue["issue_type"] == "high_volume_unmapped" and high_volume_count <= max_high_volume_unmapped:
                issue["severity"] = "warning"
    if overrides_path is not None and overrides_path.exists():
        issues.extend(
            _audit_overrides(
                _read_csv(overrides_path),
                active_companies=active_companies,
                companies_by_id=companies_by_id,
                allowed_methods=allowed_methods,
                excluded_methods=excluded_methods,
            )
        )
    if regression_path is not None and regression_path.exists():
        issues.extend(_audit_regression_cases(mapping_rows, _read_csv(regression_path)))
    severity_order = {"critical": 0, "warning": 1}
    issues = sorted(
        issues,
        key=lambda item: (
            severity_order.get(str(item.get("severity") or ""), 9),
            str(item.get("issue_type") or ""),
            _as_int(item.get("total_fda_rows"), 0) * -1,
            str(item.get("manufacturer_name") or ""),
        ),
    )
    _write_csv(output_path, issues)
    critical_count = sum(1 for issue in issues if issue["severity"] == "critical")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    return FdaMappingGovernanceResult(
        output_csv=output_path,
        issue_count=len(issues),
        critical_count=critical_count,
        warning_count=warning_count,
        ambiguous_count=ambiguous_count,
        high_volume_unmapped_count=high_volume_count,
        low_confidence_mapped_count=low_confidence_count,
    )
