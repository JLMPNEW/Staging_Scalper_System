"""Canonical financial normalization and common features for Basic Materials."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity, utc_now
from basic_materials.core.financial_ingestion import (
    FinancialIngestionError,
    FinancialIngestionPolicy,
)


@dataclass(frozen=True)
class CanonicalCandidate:
    source_observation_id: str
    filing_key: str
    company_id: int
    security_id: int
    ticker: str
    canonical_metric: str
    period_start: str
    period_end: str
    period_type: str
    accepted_at: str
    accession_number: str
    form_type: str
    taxonomy: str
    concept: str
    reported_value: float
    reported_currency: str
    source_id: str
    source_priority: int
    concept_priority: int
    fiscal_year: str
    fiscal_period: str
    context_id: str
    evidence_url: str
    payload_sha256: str


@dataclass(frozen=True)
class CanonicalFact:
    canonical_fact_id: str
    company_id: int
    security_id: int
    ticker: str
    canonical_metric: str
    period_start: str
    period_end: str
    period_type: str
    accepted_at: str
    filing_key: str
    accession_number: str
    taxonomy: str
    concept: str
    reported_value: float
    reported_currency: str
    usd_value: float | None
    fx_rate_date: str | None
    fx_rate: float | None
    source_observation_id: str
    source_id: str
    selection_rank: int
    amendment_sequence: int
    superseded_by_fact_id: str | None
    quality_status: str
    quality_reasons_json: str
    definition_version: str
    fiscal_year: str
    fiscal_period: str
    context_id: str
    normalization_method: str
    evidence_json: str


@dataclass(frozen=True)
class NormalizationStats:
    snapshot_key: str
    tickers: tuple[str, ...]
    raw_candidates: int
    canonical_facts: int
    usable_facts: int
    conflicted_facts: int
    fx_missing_facts: int
    superseded_facts: int
    issue_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tickers"] = list(self.tickers)
        payload["issue_counts"] = dict(self.issue_counts)
        return payload


@dataclass(frozen=True)
class FeatureBuildStats:
    snapshot_key: str
    market_snapshot_key: str
    as_of_date: str
    current_profiles: int
    feature_rows: int
    coverage_rows: int
    quality_counts: Mapping[str, int]
    rank_ready_count: int
    blocked_count: int
    valuation_ready_count: int
    pilot_results: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["quality_counts"] = dict(self.quality_counts)
        payload["pilot_results"] = [dict(item) for item in self.pilot_results]
        return payload


def _stable_hash(*values: Any) -> str:
    raw = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe_div(numerator: float | None, denominator: float | None, *, positive: bool = False) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    if positive and denominator <= 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def _growth(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior <= 0:
        return None
    value = current / prior - 1.0
    return value if math.isfinite(value) else None


def _apply_sign_policy(value: float, sign_policy: str) -> float:
    if sign_policy in {"positive_expense", "positive_outflow", "positive_liability"}:
        return abs(value)
    return value


def _candidate_currency(metric: str, unit: str) -> tuple[str, str]:
    normalized = unit.strip()
    if metric == "diluted_shares":
        if "share" in normalized.lower():
            return "SHARES", ""
        return "", "invalid_share_unit"
    if len(normalized) == 3 and normalized.isalpha():
        return normalized.upper(), ""
    return "", "non_monetary_or_compound_unit"


def _fx_for_candidate(
    conn: sqlite3.Connection,
    candidate: CanonicalCandidate,
    *,
    snapshot_key: str,
    max_staleness_days: int,
) -> tuple[float | None, str | None, float | None, list[str]]:
    if candidate.reported_currency == "SHARES":
        return None, None, None, []
    if candidate.reported_currency == "USD":
        return candidate.reported_value, candidate.period_end, 1.0, []
    if candidate.period_type == "duration":
        rows = conn.execute(
            """
            SELECT rate_date, rate
            FROM fact_fx_rate
            WHERE snapshot_key = ? AND base_currency = ? AND quote_currency = 'USD'
              AND quality_status = 'usable' AND rate_date BETWEEN ? AND ?
            ORDER BY rate_date
            """,
            (
                snapshot_key,
                candidate.reported_currency,
                candidate.period_start,
                candidate.period_end,
            ),
        ).fetchall()
        if not rows:
            return None, None, None, ["missing_period_average_fx"]
        rate = sum(float(row["rate"]) for row in rows) / len(rows)
        return candidate.reported_value * rate, f"{rows[0]['rate_date']}:{rows[-1]['rate_date']}", rate, []
    row = conn.execute(
        """
        SELECT rate_date, rate
        FROM fact_fx_rate
        WHERE snapshot_key = ? AND base_currency = ? AND quote_currency = 'USD'
          AND quality_status = 'usable' AND rate_date <= ?
        ORDER BY rate_date DESC
        LIMIT 1
        """,
        (snapshot_key, candidate.reported_currency, candidate.period_end),
    ).fetchone()
    if row is None:
        return None, None, None, ["missing_point_in_time_fx"]
    staleness = (date.fromisoformat(candidate.period_end) - date.fromisoformat(str(row["rate_date"]))).days
    if staleness > max_staleness_days:
        return None, str(row["rate_date"]), float(row["rate"]), [f"stale_fx:{staleness}"]
    rate = float(row["rate"])
    return candidate.reported_value * rate, str(row["rate_date"]), rate, []


def _raw_candidates(
    conn: sqlite3.Connection,
    *,
    snapshot_key: str,
    as_of_date: str,
    tickers: set[str] | None,
) -> tuple[list[CanonicalCandidate], list[dict[str, Any]]]:
    params: list[Any] = [snapshot_key, f"{as_of_date}T23:59:59Z"]
    ticker_clause = ""
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        ticker_clause = f" AND x.ticker IN ({placeholders})"
        params.extend(sorted(tickers))
    rows = conn.execute(
        f"""
        SELECT x.*, m.canonical_metric, m.period_type AS expected_period_type,
               m.sign_policy, b.priority AS concept_priority,
               r.effective_reporting_currency
        FROM fact_sec_xbrl_fact_raw AS x
        JOIN bridge_financial_metric_concept AS b
          ON b.taxonomy = x.taxonomy AND b.concept = x.concept
        JOIN dim_financial_metric AS m ON m.metric_id = b.metric_id
        JOIN dim_financial_profile_resolution AS r ON r.security_id = x.security_id
        WHERE x.snapshot_key = ? AND x.quality_status = 'usable'
          AND x.accepted_at <> '' AND x.accepted_at <= ?
          AND x.filing_key IS NOT NULL AND r.canonical_eligible = 1
          {ticker_clause}
        ORDER BY x.ticker, x.accepted_at, x.source_observation_id
        """,
        tuple(params),
    ).fetchall()
    candidates: list[CanonicalCandidate] = []
    issues: list[dict[str, Any]] = []
    source_precedence = {"sec_companyfacts": 1, "sec_inline_xbrl_fallback": 2}
    for row in rows:
        metric = str(row["canonical_metric"])
        currency, unit_error = _candidate_currency(metric, str(row["unit"]))
        actual_period = (
            "duration"
            if str(row["period_start"]) and str(row["period_start"]) != str(row["period_end"])
            else "instant"
        )
        errors: list[str] = []
        if unit_error:
            errors.append(unit_error)
        preferred_currency = str(row["effective_reporting_currency"] or "").upper()
        if (
            metric != "diluted_shares"
            and currency
            and preferred_currency
            and currency != preferred_currency
        ):
            errors.append(f"non_reporting_currency:{currency}")
        if actual_period != str(row["expected_period_type"]):
            errors.append(f"period_type_mismatch:{actual_period}")
        if not str(row["period_end"]):
            errors.append("missing_period_end")
        elif str(row["accepted_at"])[:10] < str(row["period_end"]):
            errors.append("accepted_before_period_end")
        numeric_value = _safe_float(row["numeric_value"])
        if numeric_value is None:
            errors.append("non_finite_numeric_value")
        if errors:
            issues.append(
                {
                    "ticker": str(row["ticker"]),
                    "severity": "warning",
                    "issue_code": "CANONICAL_CANDIDATE_QUARANTINED",
                    "canonical_metric": metric,
                    "accession_number": str(row["accession_number"]),
                    "source_observation_ids": [str(row["source_observation_id"])],
                    "message": ";".join(errors),
                }
            )
            continue
        candidates.append(
            CanonicalCandidate(
                source_observation_id=str(row["source_observation_id"]),
                filing_key=str(row["filing_key"]),
                company_id=int(row["company_id"]),
                security_id=int(row["security_id"]),
                ticker=str(row["ticker"]),
                canonical_metric=metric,
                period_start=str(row["period_start"]),
                period_end=str(row["period_end"]),
                period_type=str(row["expected_period_type"]),
                accepted_at=str(row["accepted_at"]),
                accession_number=str(row["accession_number"]),
                form_type=str(row["form_type"]),
                taxonomy=str(row["taxonomy"]),
                concept=str(row["concept"]),
                reported_value=_apply_sign_policy(float(numeric_value), str(row["sign_policy"])),
                reported_currency=currency,
                source_id=str(row["source_id"]),
                source_priority=source_precedence.get(str(row["source_id"]), 99),
                concept_priority=int(row["concept_priority"]),
                fiscal_year=str(row["fiscal_year"]),
                fiscal_period=str(row["fiscal_period"]),
                context_id=str(row["context_id"]),
                evidence_url=str(row["evidence_url"]),
                payload_sha256=str(row["payload_sha256"]),
            )
        )
    return candidates, issues


def _canonicalize_candidates(
    conn: sqlite3.Connection,
    *,
    candidates: Sequence[CanonicalCandidate],
    snapshot_key: str,
    policy: FinancialIngestionPolicy,
) -> tuple[list[CanonicalFact], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[CanonicalCandidate]] = defaultdict(list)
    for candidate in candidates:
        key = (
            candidate.security_id,
            candidate.canonical_metric,
            candidate.period_start,
            candidate.period_end,
            candidate.period_type,
            candidate.accession_number,
            candidate.reported_currency,
        )
        groups[key].append(candidate)
    normalization = policy.payload["normalization"]
    max_fx_staleness = int(normalization["max_fx_staleness_calendar_days"])
    facts: list[CanonicalFact] = []
    issues: list[dict[str, Any]] = []
    for key, group in sorted(groups.items(), key=lambda item: tuple(str(value) for value in item[0])):
        group.sort(
            key=lambda item: (
                item.source_priority,
                item.concept_priority,
                item.source_observation_id,
            )
        )
        best_rank = (group[0].source_priority, group[0].concept_priority)
        finalists = [
            item
            for item in group
            if (item.source_priority, item.concept_priority) == best_rank
        ]
        values = {round(item.reported_value, 8) for item in finalists}
        conflict = len(values) > 1
        selected = finalists[0]
        reasons: list[str] = []
        if conflict:
            reasons.append("same_priority_conflicting_values")
            issues.append(
                {
                    "ticker": selected.ticker,
                    "severity": "error",
                    "issue_code": "CANONICAL_VALUE_CONFLICT",
                    "canonical_metric": selected.canonical_metric,
                    "accession_number": selected.accession_number,
                    "source_observation_ids": [item.source_observation_id for item in finalists],
                    "message": "Same-precedence facts disagree; group is quarantined rather than averaged.",
                }
            )
        elif len(finalists) > 1:
            reasons.append("equal_duplicate_deterministic_first")
            issues.append(
                {
                    "ticker": selected.ticker,
                    "severity": "info",
                    "issue_code": "EQUAL_DUPLICATE_COLLAPSED",
                    "canonical_metric": selected.canonical_metric,
                    "accession_number": selected.accession_number,
                    "source_observation_ids": [item.source_observation_id for item in finalists],
                    "message": "Equal same-precedence facts were collapsed deterministically.",
                }
            )
        usd_value, fx_date, fx_rate, fx_reasons = _fx_for_candidate(
            conn,
            selected,
            snapshot_key=snapshot_key,
            max_staleness_days=max_fx_staleness,
        )
        reasons.extend(fx_reasons)
        status = "conflicted" if conflict else ("fx_missing" if fx_reasons else "usable")
        fact_id = _stable_hash(
            snapshot_key,
            selected.canonical_metric,
            selected.period_start,
            selected.period_end,
            selected.accession_number,
            selected.reported_currency,
            selected.source_observation_id,
        )
        evidence = {
            "source_observation_id": selected.source_observation_id,
            "source_url": selected.evidence_url,
            "source_payload_sha256": selected.payload_sha256,
            "candidate_count": len(group),
            "same_rank_candidate_count": len(finalists),
            "source_precedence": selected.source_priority,
            "concept_priority": selected.concept_priority,
        }
        facts.append(
            CanonicalFact(
                canonical_fact_id=fact_id,
                company_id=selected.company_id,
                security_id=selected.security_id,
                ticker=selected.ticker,
                canonical_metric=selected.canonical_metric,
                period_start=selected.period_start,
                period_end=selected.period_end,
                period_type=selected.period_type,
                accepted_at=selected.accepted_at,
                filing_key=selected.filing_key,
                accession_number=selected.accession_number,
                taxonomy=selected.taxonomy,
                concept=selected.concept,
                reported_value=selected.reported_value,
                reported_currency=selected.reported_currency,
                usd_value=usd_value,
                fx_rate_date=fx_date,
                fx_rate=fx_rate,
                source_observation_id=selected.source_observation_id,
                source_id=selected.source_id,
                selection_rank=1,
                amendment_sequence=0,
                superseded_by_fact_id=None,
                quality_status=status,
                quality_reasons_json=json.dumps(reasons, sort_keys=True),
                definition_version=str(normalization["definition_version"]),
                fiscal_year=selected.fiscal_year,
                fiscal_period=selected.fiscal_period,
                context_id=selected.context_id,
                normalization_method="source_precedence_then_concept_priority",
                evidence_json=json.dumps(evidence, sort_keys=True),
            )
        )
    return _apply_supersession(facts), issues


def _apply_supersession(facts: Sequence[CanonicalFact]) -> list[CanonicalFact]:
    grouped: dict[tuple[Any, ...], list[CanonicalFact]] = defaultdict(list)
    for fact in facts:
        grouped[
            (
                fact.security_id,
                fact.canonical_metric,
                fact.period_start,
                fact.period_end,
                fact.reported_currency,
            )
        ].append(fact)
    result: list[CanonicalFact] = []
    for group in grouped.values():
        group.sort(key=lambda item: (item.accepted_at, item.accession_number, item.canonical_fact_id))
        usable_indexes = [
            index
            for index, item in enumerate(group)
            if item.quality_status in {"usable", "fx_missing"}
        ]
        newest_index = usable_indexes[-1] if usable_indexes else None
        for index, item in enumerate(group):
            values = asdict(item)
            values["amendment_sequence"] = index
            if newest_index is not None and index in usable_indexes and index != newest_index:
                values["quality_status"] = "superseded"
                values["superseded_by_fact_id"] = group[newest_index].canonical_fact_id
            result.append(CanonicalFact(**values))
    result.sort(
        key=lambda item: (
            item.ticker,
            item.canonical_metric,
            item.period_end,
            item.accepted_at,
            item.canonical_fact_id,
        )
    )
    return result


def _insert_normalization_issue(
    conn: sqlite3.Connection,
    *,
    snapshot_key: str,
    profile: Mapping[str, Any],
    issue: Mapping[str, Any],
    now: str,
) -> None:
    source_ids = list(issue.get("source_observation_ids") or [])
    key = _stable_hash(
        snapshot_key,
        profile["ticker"],
        "normalization",
        issue.get("issue_code"),
        issue.get("canonical_metric"),
        issue.get("accession_number"),
        source_ids,
        issue.get("message"),
    )
    conn.execute(
        """
        INSERT INTO fact_financial_normalization_issue (
            issue_key, snapshot_key, company_id, security_id, ticker, stage,
            severity, issue_code, canonical_metric, accession_number,
            source_observation_ids_json, evidence_json, message, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, 'normalization', ?, ?, ?, ?, ?, '{}', ?, ?)
        ON CONFLICT(issue_key) DO NOTHING
        """,
        (
            key,
            snapshot_key,
            int(profile["company_id"]),
            int(profile["security_id"]),
            str(profile["ticker"]),
            str(issue.get("severity") or "warning"),
            str(issue.get("issue_code") or ""),
            str(issue.get("canonical_metric") or ""),
            str(issue.get("accession_number") or ""),
            json.dumps(source_ids, sort_keys=True),
            str(issue.get("message") or ""),
            now,
        ),
    )


def _insert_canonical_fact(
    conn: sqlite3.Connection,
    fact: CanonicalFact,
    *,
    snapshot_key: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO fact_financial_statement_canonical (
            canonical_fact_id, company_id, security_id, ticker, canonical_metric,
            period_start, period_end, period_type, accepted_at, filing_key,
            accession_number, taxonomy, concept, reported_value, reported_currency,
            usd_value, fx_rate_date, fx_rate, source_observation_id, source_id,
            selection_rank, amendment_sequence, superseded_by_fact_id,
            quality_status, quality_reasons_json, definition_version, snapshot_key,
            created_at_utc, updated_at_utc, fiscal_year, fiscal_period, context_id,
            normalization_method, evidence_json
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            fact.canonical_fact_id,
            fact.company_id,
            fact.security_id,
            fact.ticker,
            fact.canonical_metric,
            fact.period_start,
            fact.period_end,
            fact.period_type,
            fact.accepted_at,
            fact.filing_key,
            fact.accession_number,
            fact.taxonomy,
            fact.concept,
            fact.reported_value,
            fact.reported_currency,
            fact.usd_value,
            fact.fx_rate_date,
            fact.fx_rate,
            fact.source_observation_id,
            fact.source_id,
            fact.selection_rank,
            fact.amendment_sequence,
            fact.quality_status,
            fact.quality_reasons_json,
            fact.definition_version,
            snapshot_key,
            now,
            now,
            fact.fiscal_year,
            fact.fiscal_period,
            fact.context_id,
            fact.normalization_method,
            fact.evidence_json,
        ),
    )


def normalize_financial_facts(
    conn: sqlite3.Connection,
    *,
    policy: FinancialIngestionPolicy,
    snapshot_key: str,
    tickers: Iterable[str] | None = None,
) -> NormalizationStats:
    """Rebuild canonical facts for all or a bounded representative ticker set."""

    assert_database_identity(conn)
    snapshot = conn.execute(
        "SELECT * FROM fact_financial_ingestion_snapshot WHERE snapshot_key = ?",
        (snapshot_key,),
    ).fetchone()
    if snapshot is None:
        raise FinancialIngestionError(f"Financial snapshot does not exist: {snapshot_key}")
    if str(snapshot["policy_sha256"]) != policy.checksum:
        raise FinancialIngestionError("Normalization policy does not match the financial snapshot")
    requested = {str(item).strip().upper() for item in (tickers or []) if str(item).strip()}
    candidates, candidate_issues = _raw_candidates(
        conn,
        snapshot_key=snapshot_key,
        as_of_date=policy.as_of_date,
        tickers=requested or None,
    )
    canonical, selection_issues = _canonicalize_candidates(
        conn,
        candidates=candidates,
        snapshot_key=snapshot_key,
        policy=policy,
    )
    affected = sorted(requested or {item.ticker for item in candidates})
    profile_rows = conn.execute(
        """
        SELECT p.ticker, p.company_id, p.security_id
        FROM dim_issuer_reporting_profile AS p
        ORDER BY p.ticker
        """
    ).fetchall()
    profiles = {str(row["ticker"]): dict(row) for row in profile_rows}
    issues = candidate_issues + selection_issues
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if affected:
            placeholders = ",".join("?" for _ in affected)
            conn.execute(
                f"""
                UPDATE fact_financial_statement_canonical
                SET superseded_by_fact_id = NULL
                WHERE snapshot_key = ? AND ticker IN ({placeholders})
                """,
                (snapshot_key, *affected),
            )
            conn.execute(
                f"""
                DELETE FROM fact_financial_statement_canonical
                WHERE snapshot_key = ? AND ticker IN ({placeholders})
                """,
                (snapshot_key, *affected),
            )
            conn.execute(
                f"""
                DELETE FROM fact_financial_normalization_issue
                WHERE snapshot_key = ? AND stage = 'normalization'
                  AND ticker IN ({placeholders})
                """,
                (snapshot_key, *affected),
            )
        for fact in canonical:
            _insert_canonical_fact(conn, fact, snapshot_key=snapshot_key, now=now)
        for fact in canonical:
            if fact.superseded_by_fact_id:
                conn.execute(
                    """
                    UPDATE fact_financial_statement_canonical
                    SET superseded_by_fact_id = ?
                    WHERE canonical_fact_id = ?
                    """,
                    (fact.superseded_by_fact_id, fact.canonical_fact_id),
                )
        for issue in issues:
            profile = profiles.get(str(issue.get("ticker") or ""))
            if profile is not None:
                _insert_normalization_issue(
                    conn,
                    snapshot_key=snapshot_key,
                    profile=profile,
                    issue=issue,
                    now=now,
                )
        total_canonical = int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()[0]
        )
        details = json.loads(str(snapshot["details_json"]) or "{}")
        details["normalization_definition_version"] = policy.payload["normalization"]["definition_version"]
        details["normalization_last_scope"] = affected
        conn.execute(
            """
            UPDATE fact_financial_ingestion_snapshot
            SET canonical_fact_count = ?, details_json = ?
            WHERE snapshot_key = ?
            """,
            (total_canonical, json.dumps(details, sort_keys=True), snapshot_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    status_counts = Counter(item.quality_status for item in canonical)
    issue_counts = Counter(str(item["issue_code"]) for item in issues)
    return NormalizationStats(
        snapshot_key=snapshot_key,
        tickers=tuple(affected),
        raw_candidates=len(candidates),
        canonical_facts=len(canonical),
        usable_facts=status_counts["usable"],
        conflicted_facts=status_counts["conflicted"],
        fx_missing_facts=status_counts["fx_missing"],
        superseded_facts=status_counts["superseded"],
        issue_counts=dict(sorted(issue_counts.items())),
    )


@dataclass(frozen=True)
class PeriodValue:
    value: float
    period_start: str
    period_end: str
    accepted_at: str
    lineage: tuple[str, ...]
    method: str


def _duration_days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _canonical_rows_for_security(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    snapshot_key: str,
    as_of_timestamp: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM fact_financial_statement_canonical
        WHERE security_id = ? AND snapshot_key = ? AND accepted_at <= ?
          AND quality_status = 'usable'
        ORDER BY canonical_metric, period_end, accepted_at, canonical_fact_id
        """,
        (security_id, snapshot_key, as_of_timestamp),
    ).fetchall()
    return [dict(row) for row in rows]


def _collapse_period_contexts(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (str(row["period_start"]), str(row["period_end"]))
        existing = latest.get(key)
        if existing is None or (
            str(row["accepted_at"]),
            str(row["accession_number"]),
            str(row["canonical_fact_id"]),
        ) > (
            str(existing["accepted_at"]),
            str(existing["accession_number"]),
            str(existing["canonical_fact_id"]),
        ):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: (str(row["period_end"]), str(row["accepted_at"])))


def _row_value(row: Mapping[str, Any], metric: str) -> float | None:
    if metric == "diluted_shares":
        return _safe_float(row["reported_value"])
    return _safe_float(row["usd_value"])


def _quarter_and_annual_values(
    rows: Sequence[Mapping[str, Any]],
    *,
    metric: str,
    policy: FinancialIngestionPolicy,
) -> tuple[list[PeriodValue], list[PeriodValue]]:
    normalization = policy.payload["normalization"]
    quarter_lo, quarter_hi = (int(value) for value in normalization["quarter_duration_days"])
    annual_lo, annual_hi = (int(value) for value in normalization["annual_duration_days"])
    collapsed = _collapse_period_contexts(rows)
    direct_quarters: dict[str, PeriodValue] = {}
    annuals: list[PeriodValue] = []
    spans_by_start: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in collapsed:
        start = str(row["period_start"])
        end = str(row["period_end"])
        if not start or not end or start == end:
            continue
        value = _row_value(row, metric)
        if value is None:
            continue
        days = _duration_days(start, end)
        item = PeriodValue(
            value=value,
            period_start=start,
            period_end=end,
            accepted_at=str(row["accepted_at"]),
            lineage=(str(row["canonical_fact_id"]),),
            method="direct_duration",
        )
        if quarter_lo <= days <= quarter_hi:
            current = direct_quarters.get(end)
            if current is None or item.accepted_at > current.accepted_at:
                direct_quarters[end] = item
        if annual_lo <= days <= annual_hi:
            annuals.append(item)
        if days <= annual_hi:
            spans_by_start[start].append(row)
    derived_quarters: dict[str, PeriodValue] = {}
    for start, spans in spans_by_start.items():
        spans.sort(key=lambda row: (str(row["period_end"]), str(row["accepted_at"])))
        for prior, current in zip(spans, spans[1:]):
            prior_end = str(prior["period_end"])
            current_end = str(current["period_end"])
            incremental_days = (date.fromisoformat(current_end) - date.fromisoformat(prior_end)).days
            if not (quarter_lo <= incremental_days <= quarter_hi):
                continue
            prior_value = _row_value(prior, metric)
            current_value = _row_value(current, metric)
            if prior_value is None or current_value is None:
                continue
            derived = PeriodValue(
                value=current_value - prior_value,
                period_start=(date.fromisoformat(prior_end) + timedelta(days=1)).isoformat(),
                period_end=current_end,
                accepted_at=max(str(prior["accepted_at"]), str(current["accepted_at"])),
                lineage=(str(prior["canonical_fact_id"]), str(current["canonical_fact_id"])),
                method="same_start_ytd_difference",
            )
            existing = derived_quarters.get(current_end)
            if existing is None or derived.accepted_at > existing.accepted_at:
                derived_quarters[current_end] = derived
    quarters = dict(derived_quarters)
    quarters.update(direct_quarters)
    unique_annuals: dict[str, PeriodValue] = {}
    for item in annuals:
        existing = unique_annuals.get(item.period_end)
        if existing is None or item.accepted_at > existing.accepted_at:
            unique_annuals[item.period_end] = item
    return (
        sorted(quarters.values(), key=lambda item: (item.period_end, item.accepted_at)),
        sorted(unique_annuals.values(), key=lambda item: (item.period_end, item.accepted_at)),
    )


def _ttm_series(
    quarters: Sequence[PeriodValue],
    annuals: Sequence[PeriodValue],
    *,
    policy: FinancialIngestionPolicy,
) -> list[PeriodValue]:
    max_gap = int(policy.payload["normalization"]["ttm_quarter_gap_max_days"])
    series: dict[str, PeriodValue] = {}
    for index in range(3, len(quarters)):
        window = list(quarters[index - 3 : index + 1])
        gaps = [
            (date.fromisoformat(right.period_end) - date.fromisoformat(left.period_end)).days
            for left, right in zip(window, window[1:])
        ]
        if any(gap < 60 or gap > max_gap for gap in gaps):
            continue
        series[window[-1].period_end] = PeriodValue(
            value=sum(item.value for item in window),
            period_start=window[0].period_start,
            period_end=window[-1].period_end,
            accepted_at=max(item.accepted_at for item in window),
            lineage=tuple(lineage for item in window for lineage in item.lineage),
            method="sum_four_quarters",
        )
    if bool(policy.payload["normalization"]["ttm_annual_fallback_allowed"]):
        for annual in annuals:
            series.setdefault(
                annual.period_end,
                PeriodValue(
                    value=annual.value,
                    period_start=annual.period_start,
                    period_end=annual.period_end,
                    accepted_at=annual.accepted_at,
                    lineage=annual.lineage,
                    method="annual_fallback",
                ),
            )
    return sorted(series.values(), key=lambda item: (item.period_end, item.accepted_at))


def _current_and_prior_flow(
    rows: Sequence[Mapping[str, Any]],
    *,
    metric: str,
    policy: FinancialIngestionPolicy,
) -> tuple[PeriodValue | None, PeriodValue | None]:
    quarters, annuals = _quarter_and_annual_values(rows, metric=metric, policy=policy)
    series = _ttm_series(quarters, annuals, policy=policy)
    if not series:
        return None, None
    current = series[-1]
    prior_candidates = [
        item
        for item in series[:-1]
        if 300
        <= (date.fromisoformat(current.period_end) - date.fromisoformat(item.period_end)).days
        <= 430
    ]
    prior = (
        min(
            prior_candidates,
            key=lambda item: abs(
                (date.fromisoformat(current.period_end) - date.fromisoformat(item.period_end)).days - 365
            ),
        )
        if prior_candidates
        else None
    )
    return current, prior


def _current_and_prior_instant(
    rows: Sequence[Mapping[str, Any]],
    *,
    metric: str,
) -> tuple[PeriodValue | None, PeriodValue | None]:
    values: list[PeriodValue] = []
    for row in _collapse_period_contexts(rows):
        value = _row_value(row, metric)
        end = str(row["period_end"])
        if value is None or not end:
            continue
        values.append(
            PeriodValue(
                value=value,
                period_start=end,
                period_end=end,
                accepted_at=str(row["accepted_at"]),
                lineage=(str(row["canonical_fact_id"]),),
                method="latest_instant",
            )
        )
    if not values:
        return None, None
    current = values[-1]
    prior_candidates = [
        item
        for item in values[:-1]
        if 300
        <= (date.fromisoformat(current.period_end) - date.fromisoformat(item.period_end)).days
        <= 430
    ]
    prior = (
        min(
            prior_candidates,
            key=lambda item: abs(
                (date.fromisoformat(current.period_end) - date.fromisoformat(item.period_end)).days - 365
            ),
        )
        if prior_candidates
        else None
    )
    return current, prior


def _current_and_prior_shares(
    rows: Sequence[Mapping[str, Any]],
    *,
    policy: FinancialIngestionPolicy,
) -> tuple[PeriodValue | None, PeriodValue | None]:
    quarters, annuals = _quarter_and_annual_values(rows, metric="diluted_shares", policy=policy)
    series = quarters if quarters else annuals
    if not series:
        return None, None
    current = series[-1]
    prior_candidates = [
        item
        for item in series[:-1]
        if 300
        <= (date.fromisoformat(current.period_end) - date.fromisoformat(item.period_end)).days
        <= 430
    ]
    prior = (
        min(
            prior_candidates,
            key=lambda item: abs(
                (date.fromisoformat(current.period_end) - date.fromisoformat(item.period_end)).days - 365
            ),
        )
        if prior_candidates
        else None
    )
    return current, prior


def _metric_period_values(
    canonical_rows: Sequence[Mapping[str, Any]],
    *,
    policy: FinancialIngestionPolicy,
) -> tuple[dict[str, PeriodValue | None], dict[str, PeriodValue | None], dict[str, Any]]:
    by_metric: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in canonical_rows:
        by_metric[str(row["canonical_metric"])].append(row)
    current: dict[str, PeriodValue | None] = {}
    prior: dict[str, PeriodValue | None] = {}
    lineage: dict[str, Any] = {}
    for metric, rows in by_metric.items():
        period_type = str(rows[0]["period_type"])
        if metric == "diluted_shares":
            present, previous = _current_and_prior_shares(rows, policy=policy)
        elif period_type == "duration":
            present, previous = _current_and_prior_flow(rows, metric=metric, policy=policy)
        else:
            present, previous = _current_and_prior_instant(rows, metric=metric)
        current[metric] = present
        prior[metric] = previous
        lineage[metric] = {
            "current": asdict(present) if present else None,
            "prior": asdict(previous) if previous else None,
        }
    return current, prior, lineage


def _period_number(values: Mapping[str, PeriodValue | None], metric: str) -> float | None:
    item = values.get(metric)
    return item.value if item is not None else None


def _latest_market_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT *
        FROM fact_market_provider_snapshot
        WHERE status = 'loaded' AND extraction_asof_date <= ?
        ORDER BY extraction_asof_date DESC, created_at_utc DESC, snapshot_key DESC
        LIMIT 1
        """,
        (as_of_date,),
    ).fetchone()
    if row is None:
        raise FinancialIngestionError("A loaded Stage 3 market snapshot is required for financial features")
    return row


def _market_price(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    market_snapshot_key: str,
    as_of_date: str,
) -> tuple[float | None, str, str]:
    row = conn.execute(
        """
        SELECT p.close, p.bar_date, s.trading_currency
        FROM bridge_market_instrument_role AS r
        JOIN dim_security AS s ON s.security_id = r.security_id
        JOIN fact_adjusted_price_bar AS p
          ON p.instrument_id = r.instrument_id
         AND p.provider_source_id = 'norgate_us_equities_total_return'
         AND p.snapshot_key = ?
        WHERE r.security_id = ? AND r.role_type = 'current_universe'
          AND p.bar_date <= ?
        ORDER BY p.bar_date DESC
        LIMIT 1
        """,
        (market_snapshot_key, security_id, as_of_date),
    ).fetchone()
    if row is None:
        return None, "", ""
    return float(row["close"]), str(row["bar_date"]), str(row["trading_currency"]).upper()


def _feature_row_for_profile(
    conn: sqlite3.Connection,
    *,
    profile: Mapping[str, Any],
    policy: FinancialIngestionPolicy,
    snapshot_key: str,
    market_snapshot_key: str,
    expected_metrics: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    as_of_date = policy.as_of_date
    as_of_timestamp = f"{as_of_date}T23:59:59Z"
    security_id = int(profile["security_id"])
    ticker = str(profile["ticker"])
    canonical_rows = _canonical_rows_for_security(
        conn,
        security_id=security_id,
        snapshot_key=snapshot_key,
        as_of_timestamp=as_of_timestamp,
    )
    current, prior, lineage = _metric_period_values(canonical_rows, policy=policy)
    available_metrics = sorted({str(row["canonical_metric"]) for row in canonical_rows})
    missing_metrics = sorted(set(expected_metrics) - set(available_metrics))
    conflict_metrics = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT canonical_metric
            FROM fact_financial_statement_canonical
            WHERE security_id = ? AND snapshot_key = ? AND accepted_at <= ?
              AND quality_status = 'conflicted'
            """,
            (security_id, snapshot_key, as_of_timestamp),
        ).fetchall()
    }
    revenue = _period_number(current, "revenue")
    cost = _period_number(current, "cost_of_revenue")
    gross_profit = _period_number(current, "gross_profit")
    if gross_profit is None and revenue is not None and cost is not None:
        gross_profit = revenue - cost
        lineage["gross_profit_derived"] = "revenue_minus_cost_of_revenue"
    operating_income = _period_number(current, "operating_income")
    net_income = _period_number(current, "net_income")
    operating_cash_flow = _period_number(current, "operating_cash_flow")
    capex = _period_number(current, "capital_expenditures")
    free_cash_flow = (
        operating_cash_flow - capex
        if operating_cash_flow is not None and capex is not None
        else None
    )
    cash = _period_number(current, "cash_and_equivalents")
    debt_current = _period_number(current, "debt_current")
    debt_noncurrent = _period_number(current, "debt_noncurrent")
    debt = (
        debt_current + debt_noncurrent
        if debt_current is not None and debt_noncurrent is not None
        else None
    )
    assets = _period_number(current, "assets")
    equity = _period_number(current, "equity")
    inventory = _period_number(current, "inventory")
    receivables = _period_number(current, "accounts_receivable")
    payables = _period_number(current, "accounts_payable")
    diluted_shares = _period_number(current, "diluted_shares")
    depreciation = _period_number(current, "depreciation_depletion_amortization")
    ebitda = (
        operating_income + depreciation
        if operating_income is not None and depreciation is not None
        else None
    )
    interest = _period_number(current, "interest_expense")
    tax = _period_number(current, "income_tax_expense")
    pretax = _period_number(current, "pretax_income")
    tax_rate = _safe_div(tax, pretax, positive=True)
    if tax_rate is not None and not 0 <= tax_rate <= 0.5:
        tax_rate = None
    prior_revenue = _period_number(prior, "revenue")
    prior_operating_income = _period_number(prior, "operating_income")
    prior_operating_cash_flow = _period_number(prior, "operating_cash_flow")
    prior_capex = _period_number(prior, "capital_expenditures")
    prior_fcf = (
        prior_operating_cash_flow - prior_capex
        if prior_operating_cash_flow is not None and prior_capex is not None
        else None
    )
    prior_inventory = _period_number(prior, "inventory")
    prior_assets = _period_number(prior, "assets")
    prior_shares = _period_number(prior, "diluted_shares")
    prior_cash = _period_number(prior, "cash_and_equivalents")
    prior_debt_current = _period_number(prior, "debt_current")
    prior_debt_noncurrent = _period_number(prior, "debt_noncurrent")
    prior_debt = (
        prior_debt_current + prior_debt_noncurrent
        if prior_debt_current is not None and prior_debt_noncurrent is not None
        else None
    )
    prior_equity = _period_number(prior, "equity")
    average_assets = (
        (assets + prior_assets) / 2.0
        if assets is not None and prior_assets is not None
        else assets
    )
    invested_capital = (
        debt + equity - cash
        if debt is not None and equity is not None and cash is not None
        else None
    )
    prior_invested = (
        prior_debt + prior_equity - prior_cash
        if prior_debt is not None and prior_equity is not None and prior_cash is not None
        else None
    )
    average_invested = (
        (invested_capital + prior_invested) / 2.0
        if invested_capital is not None and prior_invested is not None
        else invested_capital
    )
    nopat = (
        operating_income * (1.0 - tax_rate)
        if operating_income is not None and tax_rate is not None
        else None
    )
    gross_margin = _safe_div(gross_profit, revenue, positive=True)
    operating_margin = _safe_div(operating_income, revenue, positive=True)
    fcf_margin = _safe_div(free_cash_flow, revenue, positive=True)
    roic = _safe_div(nopat, average_invested, positive=True)
    asset_turnover = _safe_div(revenue, average_assets, positive=True)
    net_debt = debt - cash if debt is not None and cash is not None else None
    net_debt_to_ebitda = _safe_div(net_debt, ebitda, positive=True)
    interest_coverage = _safe_div(operating_income, interest, positive=True)
    inventory_days = _safe_div(
        inventory * 365.0 if inventory is not None else None,
        cost,
        positive=True,
    )
    inventory_turnover = _safe_div(cost, inventory, positive=True)
    dso = _safe_div(
        receivables * 365.0 if receivables is not None else None,
        revenue,
        positive=True,
    )
    dpo = _safe_div(
        payables * 365.0 if payables is not None else None,
        cost,
        positive=True,
    )
    cash_conversion_cycle = (
        inventory_days + dso - dpo
        if inventory_days is not None and dso is not None and dpo is not None
        else None
    )
    revenue_growth = _growth(revenue, prior_revenue)
    operating_income_growth = _growth(operating_income, prior_operating_income)
    free_cash_flow_growth = _growth(free_cash_flow, prior_fcf)
    revenue_delta = (
        revenue - prior_revenue
        if revenue is not None and prior_revenue is not None
        else None
    )
    operating_delta = (
        operating_income - prior_operating_income
        if operating_income is not None and prior_operating_income is not None
        else None
    )
    incremental_margin = _safe_div(operating_delta, revenue_delta, positive=True)
    inventory_growth = _growth(inventory, prior_inventory)
    inventory_sales_spread = (
        inventory_growth - revenue_growth
        if inventory_growth is not None and revenue_growth is not None
        else None
    )
    capex_to_revenue = _safe_div(capex, revenue, positive=True)
    price, price_date, trading_currency = _market_price(
        conn,
        security_id=security_id,
        market_snapshot_key=market_snapshot_key,
        as_of_date=as_of_date,
    )
    feature_policy = policy.payload["features"]
    reasons: list[str] = []
    if price_date:
        price_staleness = (date.fromisoformat(as_of_date) - date.fromisoformat(price_date)).days
        if price_staleness > int(feature_policy["maximum_price_staleness_days"]):
            reasons.append(f"stale_market_price:{price_staleness}")
            price = None
    else:
        reasons.append("missing_stage3_market_price")
    share_basis_safe = (
        str(profile["filing_regime"]) == "domestic_sec"
        or str(profile["ingestion_route"]) == "domestic_interim_companyfacts"
    )
    if not share_basis_safe:
        reasons.append("foreign_security_share_ratio_unresolved")
    if trading_currency and trading_currency != "USD":
        reasons.append(f"non_usd_trading_price_unconverted:{trading_currency}")
        share_basis_safe = False
    market_cap = (
        price * diluted_shares
        if price is not None and diluted_shares is not None and diluted_shares > 0 and share_basis_safe
        else None
    )
    if diluted_shares is None:
        reasons.append("missing_diluted_shares_for_market_cap")
    enterprise_value = (
        market_cap + debt - cash
        if market_cap is not None and debt is not None and cash is not None
        else None
    )
    fcf_yield = _safe_div(free_cash_flow, market_cap, positive=True)
    ev_to_ebitda = _safe_div(enterprise_value, ebitda, positive=True)
    ev_to_ebit = _safe_div(enterprise_value, operating_income, positive=True)
    ev_to_gross_profit = _safe_div(enterprise_value, gross_profit, positive=True)
    dilution = _growth(diluted_shares, prior_shares)
    latest_period_end = max(
        (item.period_end for item in current.values() if item is not None),
        default="",
    )
    latest_accepted_at = max(
        (str(row["accepted_at"]) for row in canonical_rows),
        default="",
    )
    stale_days = (
        (date.fromisoformat(as_of_date) - date.fromisoformat(latest_accepted_at[:10])).days
        if latest_accepted_at
        else 10**9
    )
    stale_metrics = {
        metric
        for metric, item in current.items()
        if item is not None
        and (date.fromisoformat(as_of_date) - date.fromisoformat(item.accepted_at[:10])).days
        > int(feature_policy["maximum_current_staleness_days"])
    }
    core_ready = all(
        value is not None
        for value in (revenue, operating_income, operating_cash_flow, assets)
    )
    canonical_eligible = bool(profile["canonical_eligible"])
    if not canonical_eligible or not canonical_rows:
        quality = "blocked"
        reasons.append("profile_canonical_blocked" if not canonical_eligible else "no_usable_canonical_facts")
    elif stale_days > int(feature_policy["maximum_current_staleness_days"]):
        quality = "stale"
        reasons.append(f"stale_financial_acceptance:{stale_days}")
    elif len(available_metrics) >= int(feature_policy["minimum_metrics_full"]) and core_ready:
        quality = "full"
    elif len(available_metrics) >= int(feature_policy["minimum_metrics_partial"]) and revenue is not None:
        quality = "partial"
    else:
        quality = "insufficient"
        reasons.append("insufficient_common_metric_coverage")
    if debt is None and (debt_current is not None or debt_noncurrent is not None):
        reasons.append("incomplete_debt_components")
    if conflict_metrics:
        reasons.append("conflicted_metrics:" + ",".join(sorted(conflict_metrics)))
    confidence = 0.0 if quality == "blocked" else len(available_metrics) / len(expected_metrics)
    if quality == "stale":
        confidence *= 0.5
    rank_ready = int(
        canonical_eligible
        and quality in {"full", "partial"}
        and revenue is not None
        and not stale_metrics
    )
    input_payload = {
        "metric_lineage": lineage,
        "derived": {
            "free_cash_flow": "operating_cash_flow_minus_positive_capex",
            "debt": "debt_current_plus_debt_noncurrent_requires_both",
            "ebitda": "operating_income_plus_depreciation_depletion_amortization",
            "gross_profit": "direct_or_revenue_minus_cost_of_revenue",
        },
        "valuation": {
            "share_basis_safe": share_basis_safe,
            "price_source": "stage3_norgate_contract",
            "price_field": "close",
            "trading_currency": trading_currency,
        },
        "missing_metrics": missing_metrics,
        "conflict_metrics": sorted(conflict_metrics),
        "tax_rate": tax_rate,
    }
    now = utc_now()
    feature = {
        "security_id": security_id,
        "ticker": ticker,
        "asof_date": as_of_date,
        "reporting_currency": str(profile["effective_reporting_currency"]),
        "latest_period_end": latest_period_end,
        "latest_accepted_at": latest_accepted_at,
        "revenue_ttm_usd": revenue,
        "gross_profit_ttm_usd": gross_profit,
        "operating_income_ttm_usd": operating_income,
        "net_income_ttm_usd": net_income,
        "operating_cash_flow_ttm_usd": operating_cash_flow,
        "capital_expenditures_ttm_usd": capex,
        "free_cash_flow_ttm_usd": free_cash_flow,
        "cash_usd": cash,
        "debt_usd": debt,
        "assets_usd": assets,
        "equity_usd": equity,
        "inventory_usd": inventory,
        "diluted_shares": diluted_shares,
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
        "free_cash_flow_margin": fcf_margin,
        "return_on_invested_capital": roic,
        "asset_turnover": asset_turnover,
        "net_debt_to_ebitda": net_debt_to_ebitda,
        "interest_coverage": interest_coverage,
        "inventory_days": inventory_days,
        "inventory_turnover": inventory_turnover,
        "cash_conversion_cycle": cash_conversion_cycle,
        "revenue_growth": revenue_growth,
        "operating_income_growth": operating_income_growth,
        "free_cash_flow_growth": free_cash_flow_growth,
        "incremental_operating_margin": incremental_margin,
        "inventory_sales_growth_spread": inventory_sales_spread,
        "capex_to_revenue": capex_to_revenue,
        "free_cash_flow_yield": fcf_yield,
        "enterprise_value_to_ebitda": ev_to_ebitda,
        "enterprise_value_to_ebit": ev_to_ebit,
        "enterprise_value_to_gross_profit": ev_to_gross_profit,
        "dilution": dilution,
        "data_confidence": confidence,
        "quality_status": quality,
        "quality_reasons_json": json.dumps(sorted(set(reasons)), sort_keys=True),
        "feature_definition_version": str(feature_policy["definition_version"]),
        "market_snapshot_key": market_snapshot_key,
        "financial_snapshot_key": snapshot_key,
        "created_at_utc": now,
        "updated_at_utc": now,
        "market_price": price,
        "market_price_date": price_date,
        "market_cap_usd": market_cap,
        "enterprise_value_usd": enterprise_value,
        "ebitda_ttm_usd": ebitda,
        "feature_inputs_json": json.dumps(input_payload, sort_keys=True),
        "available_metric_count": len(available_metrics),
        "expected_metric_count": len(expected_metrics),
    }
    coverage = {
        "audit_asof_date": as_of_date,
        "security_id": security_id,
        "ticker": ticker,
        "profile_status": str(profile["original_profile_status"]),
        "expected_metric_count": len(expected_metrics),
        "available_metric_count": len(available_metrics),
        "stale_metric_count": len(stale_metrics),
        "conflict_metric_count": len(conflict_metrics),
        "coverage_ratio": len(available_metrics) / len(expected_metrics),
        "rank_ready": rank_ready,
        "issue_detail": ";".join(sorted(set(reasons))),
        "snapshot_key": snapshot_key,
        "created_at_utc": now,
        "role_type": str(profile["role_type"]),
        "filing_regime": str(profile["filing_regime"]),
        "ingestion_route": str(profile["ingestion_route"]),
        "latest_accepted_at": latest_accepted_at,
        "missing_metrics_json": json.dumps(missing_metrics),
    }
    return feature, coverage


_FEATURE_COLUMNS = (
    "security_id",
    "ticker",
    "asof_date",
    "reporting_currency",
    "latest_period_end",
    "latest_accepted_at",
    "revenue_ttm_usd",
    "gross_profit_ttm_usd",
    "operating_income_ttm_usd",
    "net_income_ttm_usd",
    "operating_cash_flow_ttm_usd",
    "capital_expenditures_ttm_usd",
    "free_cash_flow_ttm_usd",
    "cash_usd",
    "debt_usd",
    "assets_usd",
    "equity_usd",
    "inventory_usd",
    "diluted_shares",
    "gross_margin",
    "operating_margin",
    "free_cash_flow_margin",
    "return_on_invested_capital",
    "asset_turnover",
    "net_debt_to_ebitda",
    "interest_coverage",
    "inventory_days",
    "inventory_turnover",
    "cash_conversion_cycle",
    "revenue_growth",
    "operating_income_growth",
    "free_cash_flow_growth",
    "incremental_operating_margin",
    "inventory_sales_growth_spread",
    "capex_to_revenue",
    "free_cash_flow_yield",
    "enterprise_value_to_ebitda",
    "enterprise_value_to_ebit",
    "enterprise_value_to_gross_profit",
    "dilution",
    "data_confidence",
    "quality_status",
    "quality_reasons_json",
    "feature_definition_version",
    "market_snapshot_key",
    "financial_snapshot_key",
    "created_at_utc",
    "updated_at_utc",
    "market_price",
    "market_price_date",
    "market_cap_usd",
    "enterprise_value_usd",
    "ebitda_ttm_usd",
    "feature_inputs_json",
    "available_metric_count",
    "expected_metric_count",
)

_COVERAGE_COLUMNS = (
    "audit_asof_date",
    "security_id",
    "ticker",
    "profile_status",
    "expected_metric_count",
    "available_metric_count",
    "stale_metric_count",
    "conflict_metric_count",
    "coverage_ratio",
    "rank_ready",
    "issue_detail",
    "snapshot_key",
    "created_at_utc",
    "role_type",
    "filing_regime",
    "ingestion_route",
    "latest_accepted_at",
    "missing_metrics_json",
)


def _upsert_feature(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    placeholders = ",".join("?" for _ in _FEATURE_COLUMNS)
    updates = ",".join(
        f"{column}=excluded.{column}"
        for column in _FEATURE_COLUMNS
        if column not in {"security_id", "asof_date", "created_at_utc"}
    )
    conn.execute(
        f"""
        INSERT INTO feature_financial_statement ({','.join(_FEATURE_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(security_id, asof_date) DO UPDATE SET {updates}
        """,
        tuple(row[column] for column in _FEATURE_COLUMNS),
    )


def _upsert_coverage(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    placeholders = ",".join("?" for _ in _COVERAGE_COLUMNS)
    updates = ",".join(
        f"{column}=excluded.{column}"
        for column in _COVERAGE_COLUMNS
        if column not in {"audit_asof_date", "security_id", "created_at_utc"}
    )
    conn.execute(
        f"""
        INSERT INTO fact_financial_data_coverage ({','.join(_COVERAGE_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(audit_asof_date, security_id) DO UPDATE SET {updates}
        """,
        tuple(row[column] for column in _COVERAGE_COLUMNS),
    )


def build_financial_features(
    conn: sqlite3.Connection,
    *,
    policy: FinancialIngestionPolicy,
    snapshot_key: str,
) -> FeatureBuildStats:
    """Publish one visible common-feature and coverage row for all 134 current names."""

    assert_database_identity(conn)
    snapshot = conn.execute(
        "SELECT * FROM fact_financial_ingestion_snapshot WHERE snapshot_key = ?",
        (snapshot_key,),
    ).fetchone()
    if snapshot is None or str(snapshot["policy_sha256"]) != policy.checksum:
        raise FinancialIngestionError("Financial feature build has no matching Stage 4B snapshot")
    market_snapshot = _latest_market_snapshot(conn, as_of_date=policy.as_of_date)
    market_snapshot_key = str(market_snapshot["snapshot_key"])
    expected_metrics = [
        str(row[0])
        for row in conn.execute(
            "SELECT canonical_metric FROM dim_financial_metric ORDER BY canonical_metric"
        ).fetchall()
    ]
    expected_count = int(policy.payload["features"]["expected_canonical_metrics"])
    if len(expected_metrics) != expected_count or expected_count != 22:
        raise FinancialIngestionError("Feature build requires the exact 22-metric Stage 4A contract")
    profile_rows = conn.execute(
        """
        SELECT p.profile_key, p.company_id, p.security_id, p.ticker, p.role_type,
               p.filing_regime, p.domicile_country,
               r.original_profile_status, r.ingestion_route, r.resolution_status,
               r.effective_reporting_currency, r.canonical_eligible
        FROM dim_issuer_reporting_profile AS p
        JOIN dim_financial_profile_resolution AS r ON r.profile_key = p.profile_key
        WHERE p.role_type = 'current_universe'
        ORDER BY p.ticker
        """
    ).fetchall()
    if len(profile_rows) != 134:
        raise FinancialIngestionError(f"Expected 134 current financial profiles, found {len(profile_rows)}")
    features: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for raw_profile in profile_rows:
        feature, audit = _feature_row_for_profile(
            conn,
            profile=dict(raw_profile),
            policy=policy,
            snapshot_key=snapshot_key,
            market_snapshot_key=market_snapshot_key,
            expected_metrics=expected_metrics,
        )
        features.append(feature)
        coverage.append(audit)
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM feature_financial_statement WHERE asof_date = ?",
            (policy.as_of_date,),
        )
        conn.execute(
            "DELETE FROM fact_financial_data_coverage WHERE audit_asof_date = ?",
            (policy.as_of_date,),
        )
        for row in features:
            row["created_at_utc"] = now
            row["updated_at_utc"] = now
            _upsert_feature(conn, row)
        for row in coverage:
            row["created_at_utc"] = now
            _upsert_coverage(conn, row)
        details = json.loads(str(snapshot["details_json"]) or "{}")
        details.update(
            {
                "feature_definition_version": policy.payload["features"]["definition_version"],
                "market_snapshot_key": market_snapshot_key,
                "feature_as_of_date": policy.as_of_date,
                "feature_rows": len(features),
                "valuation_share_basis_policy": "foreign_ratio_required",
            }
        )
        blocked = sum(row["quality_status"] == "blocked" for row in features)
        conn.execute(
            """
            UPDATE fact_financial_ingestion_snapshot
            SET feature_count = ?, status = ?, details_json = ?
            WHERE snapshot_key = ?
            """,
            (
                len(features),
                "partial" if blocked else "loaded",
                json.dumps(details, sort_keys=True),
                snapshot_key,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    quality_counts = Counter(str(row["quality_status"]) for row in features)
    pilots = set(str(item) for item in policy.payload["snapshot"]["representative_pilot_tickers"])
    pilot_results = tuple(
        {
            "ticker": row["ticker"],
            "quality_status": row["quality_status"],
            "available_metric_count": row["available_metric_count"],
            "revenue_ttm_usd": row["revenue_ttm_usd"],
            "operating_income_ttm_usd": row["operating_income_ttm_usd"],
            "free_cash_flow_ttm_usd": row["free_cash_flow_ttm_usd"],
            "latest_accepted_at": row["latest_accepted_at"],
        }
        for row in features
        if row["ticker"] in pilots
    )
    return FeatureBuildStats(
        snapshot_key=snapshot_key,
        market_snapshot_key=market_snapshot_key,
        as_of_date=policy.as_of_date,
        current_profiles=len(profile_rows),
        feature_rows=len(features),
        coverage_rows=len(coverage),
        quality_counts=dict(sorted(quality_counts.items())),
        rank_ready_count=sum(int(row["rank_ready"]) for row in coverage),
        blocked_count=quality_counts["blocked"],
        valuation_ready_count=sum(row["market_cap_usd"] is not None for row in features),
        pilot_results=pilot_results,
    )


def write_financial_feature_reports(
    conn: sqlite3.Connection,
    *,
    stats: FeatureBuildStats,
    report_dir: str | Path,
) -> dict[str, str]:
    target = Path(report_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    summary_path = atomic_write_json(target / "financial_feature_summary.json", stats.as_dict())
    features = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM feature_financial_statement
            WHERE asof_date = ?
            ORDER BY ticker
            """,
            (stats.as_of_date,),
        ).fetchall()
    ]
    feature_path = atomic_write_csv(
        target / "financial_features.csv",
        features,
        tuple(features[0]) if features else _FEATURE_COLUMNS,
    )
    coverage = [
        dict(row)
        for row in conn.execute(
            """
            SELECT c.*, t.cohort_id
            FROM fact_financial_data_coverage AS c
            JOIN dim_basic_materials_taxonomy AS t ON t.security_id = c.security_id
            WHERE c.audit_asof_date = ?
            ORDER BY c.ticker
            """,
            (stats.as_of_date,),
        ).fetchall()
    ]
    coverage_path = atomic_write_csv(
        target / "financial_coverage.csv",
        coverage,
        tuple(coverage[0]) if coverage else _COVERAGE_COLUMNS,
    )
    cohort_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT t.cohort_id, COUNT(*) AS profile_count,
                   SUM(c.rank_ready) AS rank_ready_count,
                   SUM(CASE WHEN f.quality_status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
                   AVG(c.coverage_ratio) AS average_coverage_ratio,
                   SUM(CASE WHEN f.market_cap_usd IS NOT NULL THEN 1 ELSE 0 END) AS valuation_ready_count
            FROM fact_financial_data_coverage AS c
            JOIN feature_financial_statement AS f
              ON f.security_id = c.security_id AND f.asof_date = c.audit_asof_date
            JOIN dim_basic_materials_taxonomy AS t ON t.security_id = c.security_id
            WHERE c.audit_asof_date = ?
            GROUP BY t.cohort_id
            ORDER BY t.cohort_id
            """,
            (stats.as_of_date,),
        ).fetchall()
    ]
    cohort_path = atomic_write_csv(
        target / "financial_coverage_by_cohort.csv",
        cohort_rows,
        tuple(cohort_rows[0]) if cohort_rows else ("cohort_id",),
    )
    regime_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT c.filing_regime, c.ingestion_route, COUNT(*) AS profile_count,
                   SUM(c.rank_ready) AS rank_ready_count,
                   AVG(c.coverage_ratio) AS average_coverage_ratio
            FROM fact_financial_data_coverage AS c
            WHERE c.audit_asof_date = ?
            GROUP BY c.filing_regime, c.ingestion_route
            ORDER BY c.filing_regime, c.ingestion_route
            """,
            (stats.as_of_date,),
        ).fetchall()
    ]
    regime_path = atomic_write_csv(
        target / "financial_coverage_by_regime.csv",
        regime_rows,
        tuple(regime_rows[0]) if regime_rows else ("filing_regime",),
    )
    pilot_path = atomic_write_csv(
        target / "representative_normalization_pilot.csv",
        [dict(item) for item in stats.pilot_results],
        (
            "ticker",
            "quality_status",
            "available_metric_count",
            "revenue_ttm_usd",
            "operating_income_ttm_usd",
            "free_cash_flow_ttm_usd",
            "latest_accepted_at",
        ),
    )
    artifacts = {
        "summary": str(summary_path),
        "features": str(feature_path),
        "coverage": str(coverage_path),
        "cohort_coverage": str(cohort_path),
        "regime_coverage": str(regime_path),
        "representative_pilot": str(pilot_path),
    }
    manifest_path = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "stage": "stage4b_financial_features",
            "snapshot_key": stats.snapshot_key,
            "artifacts": [
                {
                    "artifact": name,
                    "path": path,
                    "sha256": _sha256_path(Path(path)),
                    "byte_size": Path(path).stat().st_size,
                }
                for name, path in sorted(artifacts.items())
            ],
        },
    )
    artifacts["artifact_manifest"] = str(manifest_path)
    return artifacts


def write_normalization_report(
    *,
    stats: NormalizationStats,
    report_dir: str | Path,
    filename: str = "financial_normalization_summary.json",
) -> str:
    target = Path(report_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    return str(atomic_write_json(target / filename, stats.as_dict()))
