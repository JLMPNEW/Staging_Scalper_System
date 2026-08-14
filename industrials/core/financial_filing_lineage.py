from __future__ import annotations

import csv
import hashlib
import html
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from industrials.core.reports import write_csv_atomic
from industrials.core.text_norm import normalize_ticker
from orchestration_contracts.financial_lineage import (
    LINEAGE_FIELDS,
    POLICY_CANDIDATE_ONLY,
    evaluate_financial_lineage_rows,
    evaluation_manifest,
    policy_for_model_family,
)


LINEAGE_REPORT_FIELDS = ["ticker", "model_family", *LINEAGE_FIELDS]
PERIODIC_FINANCIAL_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
)
SUPPLEMENTAL_FORMS = frozenset({"6-K", "6-K/A", "8-K", "8-K/A"})
CORE_METRICS = frozenset(
    {
        "assets",
        "cash_and_equivalents",
        "equity",
        "gross_profit",
        "net_income",
        "operating_cash_flow",
        "operating_income",
        "revenue",
    }
)
MAX_DOCUMENT_BYTES = 3 * 1024 * 1024
RESULTS_DISCLOSURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\bitem\s*2[.]02\b",
        (
            r"\b(?:announc(?:e[ds]?|ing)|report(?:s|ed|ing)?|releas(?:e[ds]?|ing)|"
            r"issu(?:e[ds]?|ing)|furnish(?:e[ds]?|ing))\b.{0,220}"
            r"\b(?:financial results|earnings results|results of operations)\b.{0,220}"
            r"\b(?:quarter|year|months? ended|fiscal)\b"
        ),
        r"\bfinancial results for\b.{0,220}\b(?:quarter|year|months? ended|fiscal)\b",
    )
)
STATEMENT_DISCLOSURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\bunaudited (?:interim )?(?:condensed )?consolidated financial statements\b",
        r"\b(?:condensed )?consolidated statements? of (?:operations|income|loss|financial position|cash flows?)\b",
    )
)
FINANCIAL_DISCLOSURE_PATTERNS = (
    *RESULTS_DISCLOSURE_PATTERNS,
    *STATEMENT_DISCLOSURE_PATTERNS,
)


def _availability_date(row: Mapping[str, Any]) -> str:
    return str(row.get("accepted_at") or row.get("filing_date") or "").strip()[:10]


def _valid_asof(raw: str, asof: str) -> bool:
    try:
        return date.fromisoformat(raw) <= date.fromisoformat(asof)
    except ValueError:
        return False


def _quarter_end(raw: str) -> bool:
    return len(raw) >= 10 and raw[5:10] in {"03-31", "06-30", "09-30", "12-31"}


def _normalized_document_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES)
    except OSError:
        return ""
    text = html.unescape(raw.decode("utf-8", errors="ignore"))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _document_has_financial_disclosure(path: Path, *, form_type: str) -> bool:
    text = _normalized_document_text(path)
    patterns = (
        RESULTS_DISCLOSURE_PATTERNS
        if form_type in {"8-K", "8-K/A"}
        else FINANCIAL_DISCLOSURE_PATTERNS
    )
    return any(pattern.search(text) for pattern in patterns)


DISCLOSED_PERIOD_END_PATTERN = re.compile(
    r"\b(?:quarter|fiscal quarter|fiscal year|year|"
    r"(?:three|six|nine|twelve)(?: and (?:three|six|nine|twelve))? months?)"
    r".{0,100}?\bended\s+"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(20\d{2})",
    re.IGNORECASE,
)


def _supplemental_disclosed_period_ends(
    conn: sqlite3.Connection,
    *,
    accession: str,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT source_path, is_full_submission
        FROM sec_parser_document_catalog
        WHERE accession_number = ?
        ORDER BY is_full_submission ASC, is_primary DESC, file_size ASC
        """,
        (accession,),
    ).fetchall()
    periods: set[str] = set()
    for row in rows:
        if bool(row["is_full_submission"]):
            continue
        text = _normalized_document_text(Path(str(row["source_path"] or "")))
        for match in DISCLOSED_PERIOD_END_PATTERN.finditer(text):
            raw = f"{match.group(1)} {match.group(2)}, {match.group(3)}"
            try:
                periods.add(datetime.strptime(raw, "%B %d, %Y").date().isoformat())
            except ValueError:
                continue
    return periods


def _supplemental_financial_evidence(
    conn: sqlite3.Connection,
    *,
    accession: str,
    form_type: str,
    report_date: str,
    filing_date: str,
) -> tuple[bool, str]:
    documents = conn.execute(
        """
        SELECT source_path, is_full_submission
        FROM sec_parser_document_catalog
        WHERE accession_number = ?
        ORDER BY is_full_submission ASC, is_primary DESC, file_size ASC
        """,
        (accession,),
    ).fetchall()
    for document in documents:
        # The SEC full-submission wrapper concatenates contracts and exhibits;
        # financial covenant boilerplate there is not an earnings disclosure.
        if bool(document["is_full_submission"]):
            continue
        source_path = str(document["source_path"] or "").strip()
        if source_path and _document_has_financial_disclosure(
            Path(source_path), form_type=form_type
        ):
            return True, "cached_document_financial_disclosure"
    if (
        form_type in {"6-K", "6-K/A"}
        and report_date
        and report_date != filing_date
        and _quarter_end(report_date)
        and _valid_asof(report_date, filing_date)
    ):
        return True, "quarter_end_report_date_metadata"
    return False, "no_financial_disclosure_evidence"


def _canonical_core_metrics(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    model_family: str,
    asof: str,
) -> dict[str, set[str]]:
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT accession_number, canonical_metric
        FROM fact_financial_statement_canonical
        WHERE model_family = ?
          AND ticker IN ({placeholders})
          AND COALESCE(NULLIF(SUBSTR(accepted_at, 1, 10), ''), filing_date) <= ?
        """,
        (model_family, *tickers, asof),
    ).fetchall()
    metrics: dict[str, set[str]] = {}
    for row in rows:
        metric = str(row["canonical_metric"] or "").strip()
        if metric in CORE_METRICS:
            metrics.setdefault(str(row["accession_number"] or ""), set()).add(metric)
    return metrics


def _filings(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    asof: str,
) -> list[dict[str, Any]]:
    if not tickers:
        return []
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, accession_number, form_type, filing_date, accepted_at,
               report_date, primary_document, filing_url
        FROM fact_sec_filing
        WHERE ticker IN ({placeholders})
          AND form_type IN ({','.join('?' for _ in PERIODIC_FINANCIAL_FORMS | SUPPLEMENTAL_FORMS)})
        ORDER BY ticker, COALESCE(NULLIF(accepted_at, ''), filing_date), accession_number
        """,
        (*tickers, *sorted(PERIODIC_FINANCIAL_FORMS | SUPPLEMENTAL_FORMS)),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if _valid_asof(_availability_date(dict(row)), asof)
    ]


def _filings_describe_same_financial_event(
    *,
    latest: dict[str, Any],
    candidate: dict[str, Any],
    disclosed_period_ends: set[str],
) -> bool:
    latest_accession = str(latest.get("accession_number") or "").strip()
    candidate_accession = str(candidate.get("accession_number") or "").strip()
    if candidate_accession == latest_accession:
        return True

    latest_report = str(latest.get("report_date") or "").strip()[:10]
    candidate_report = str(candidate.get("report_date") or "").strip()[:10]
    if latest_report and candidate_report == latest_report:
        return True

    candidate_form = str(candidate.get("form_type") or "").strip().upper()
    if candidate_form not in PERIODIC_FINANCIAL_FORMS:
        return False

    latest_filing = str(latest.get("filing_date") or "").strip()[:10]
    candidate_filing = str(candidate.get("filing_date") or "").strip()[:10]
    if latest_filing and candidate_filing == latest_filing:
        return True
    if candidate_report and candidate_report in disclosed_period_ends:
        return True

    # Explicit period text is authoritative. Never use proximity to override it.
    if disclosed_period_ends:
        return False

    latest_form = str(latest.get("form_type") or "").strip().upper()
    if latest_form not in {"8-K", "8-K/A"}:
        return False
    if latest_report and candidate_filing == latest_report:
        return True
    try:
        filing_gap = (
            date.fromisoformat(latest_filing) - date.fromisoformat(candidate_filing)
        ).days
    except ValueError:
        return False
    return 0 <= filing_gap <= 2


def build_financial_filing_lineage(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof: str,
    tickers: Iterable[str],
) -> dict[str, dict[str, str]]:
    """Reconcile each ticker's latest material filing to canonical financial facts.

    A 6-K/8-K is material only when cached filing evidence identifies an earnings or
    financial-statement disclosure. Paired cover/data filings are resolved by report
    period or exact filing date, so an untagged press-release cover does not mask
    its same-day tagged periodic filing when the cover uses the SEC event date.
    """
    normalized = sorted({normalize_ticker(ticker) for ticker in tickers if normalize_ticker(ticker)})
    policy = policy_for_model_family(model_family)
    min_core_metric_count = policy.min_core_metric_count
    filings = _filings(conn, tickers=normalized, asof=asof)
    core_metrics = _canonical_core_metrics(
        conn,
        tickers=normalized,
        model_family=model_family,
        asof=asof,
    )
    by_ticker: dict[str, list[dict[str, Any]]] = {ticker: [] for ticker in normalized}
    for filing in filings:
        ticker = normalize_ticker(filing.get("ticker"))
        accession = str(filing.get("accession_number") or "").strip()
        filing["availability_date"] = _availability_date(filing)
        filing["core_metric_count"] = len(core_metrics.get(accession, set()))
        by_ticker.setdefault(ticker, []).append(filing)

    feature_tickers = {
        normalize_ticker(row[0])
        for row in conn.execute(
            """
            SELECT ticker
            FROM feature_financial_statement
            WHERE model_family = ? AND asof_date = ?
            """,
            (model_family, asof),
        )
    }
    output: dict[str, dict[str, str]] = {}
    for ticker in normalized:
        filings_desc = sorted(
            by_ticker.get(ticker, []),
            key=lambda row: (
                str(row.get("availability_date") or ""),
                str(row.get("accepted_at") or ""),
                str(row.get("accession_number") or ""),
            ),
            reverse=True,
        )
        latest: dict[str, Any] | None = None
        for filing in filings_desc:
            form_type = str(filing.get("form_type") or "").strip().upper()
            report_date = str(filing.get("report_date") or "").strip()[:10]
            filing_date = str(filing.get("filing_date") or "").strip()[:10]
            metric_count = int(filing.get("core_metric_count") or 0)
            if form_type in PERIODIC_FINANCIAL_FORMS:
                material = True
                evidence = "periodic_financial_form"
            elif metric_count >= min_core_metric_count:
                material = True
                evidence = "canonical_core_financial_facts"
            else:
                material, evidence = _supplemental_financial_evidence(
                    conn,
                    accession=str(filing.get("accession_number") or ""),
                    form_type=form_type,
                    report_date=report_date,
                    filing_date=filing_date,
                )
            if material:
                latest = filing
                latest["material_evidence"] = evidence
                break
        if latest is None:
            has_feature = ticker in feature_tickers
            status = "NO_MATERIAL_FINANCIAL_FILING" if has_feature else "REVIEW_REQUIRED"
            output[ticker] = {
                "ticker": ticker,
                "model_family": model_family,
                "financial_lineage_checked_asof_date": asof,
                "financial_lineage_status": status,
                "financial_lineage_gate": "0",
                "financial_lineage_classification": (
                    "NO_MATERIAL_FILING_IDENTIFIED" if has_feature else "PARSING_GAP"
                ),
                "latest_material_financial_filing_date": "",
                "latest_material_financial_form": "",
                "latest_material_financial_accession": "",
                "latest_material_financial_report_date": "",
                "incorporated_financial_filing_date": "",
                "incorporated_financial_accession": "",
                "incorporated_financial_report_date": "",
                "incorporated_financial_core_metric_count": "0",
                "financial_lineage_reason": (
                    "no_material_filing_identified_feature_snapshot_available"
                    if has_feature
                    else "no_material_filing_and_no_financial_feature_snapshot"
                ),
            }
            continue

        latest_report = str(latest.get("report_date") or "").strip()[:10]
        disclosed_period_ends = (
            _supplemental_disclosed_period_ends(
                conn,
                accession=str(latest.get("accession_number") or ""),
            )
            if str(latest.get("form_type") or "").strip().upper() in SUPPLEMENTAL_FORMS
            else set()
        )
        incorporated_candidates = [
            row
            for row in filings_desc
            if int(row.get("core_metric_count") or 0) >= min_core_metric_count
            and _filings_describe_same_financial_event(
                latest=latest,
                candidate=row,
                disclosed_period_ends=disclosed_period_ends,
            )
        ]
        incorporated = incorporated_candidates[0] if incorporated_candidates else None
        status = "INCORPORATED" if incorporated is not None else "REVIEW_REQUIRED"
        output[ticker] = {
            "ticker": ticker,
            "model_family": model_family,
            "financial_lineage_checked_asof_date": asof,
            "financial_lineage_status": status,
            "financial_lineage_gate": "1" if incorporated is not None else "0",
            "financial_lineage_classification": (
                "INCORPORATED" if incorporated is not None else "CANONICALIZATION_GAP"
            ),
            "latest_material_financial_filing_date": str(latest.get("filing_date") or "")[:10],
            "latest_material_financial_form": str(latest.get("form_type") or ""),
            "latest_material_financial_accession": str(latest.get("accession_number") or ""),
            "latest_material_financial_report_date": latest_report,
            "incorporated_financial_filing_date": (
                str(incorporated.get("filing_date") or "")[:10] if incorporated else ""
            ),
            "incorporated_financial_accession": (
                str(incorporated.get("accession_number") or "") if incorporated else ""
            ),
            "incorporated_financial_report_date": (
                str(incorporated.get("report_date") or "")[:10] if incorporated else ""
            ),
            "incorporated_financial_core_metric_count": (
                str(int(incorporated.get("core_metric_count") or 0)) if incorporated else "0"
            ),
            "financial_lineage_reason": (
                f"latest_material_filing_incorporated:{latest.get('material_evidence')}"
                if incorporated
                else f"latest_material_filing_has_no_canonical_core_facts:{latest.get('material_evidence')}"
            ),
        }
    return output


def apply_financial_lineage_gate(
    rows: list[dict[str, str]],
    lineage: Mapping[str, Mapping[str, str]],
) -> list[dict[str, str]]:
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        evidence = dict(lineage.get(ticker, {}))
        if not evidence:
            evidence = {
                field: "" for field in LINEAGE_REPORT_FIELDS
            }
            evidence.update(
                {
                    "ticker": ticker,
                    "financial_lineage_status": "REVIEW_REQUIRED",
                    "financial_lineage_gate": "0",
                    "financial_lineage_reason": "missing_ticker_lineage_record",
                }
            )
        for field in LINEAGE_FIELDS:
            row[field] = str(evidence.get(field) or "")
        if row["financial_lineage_gate"] == "1":
            continue
        reason = (
            "unresolved_material_financial_filing:"
            f"{row.get('latest_material_financial_accession') or 'unknown'}"
        )
        row["portfolio_candidate_gate"] = "0"
        row["portfolio_candidate_status"] = "data_review_required"
        row["portfolio_candidate_reason"] = reason
        if "portfolio_sleeve_selected_flag" in row:
            row["portfolio_sleeve_selected_flag"] = "0"
        if "portfolio_sleeve_target_weight" in row:
            row["portfolio_sleeve_target_weight"] = "0"
        if "eligibility_reason" in row:
            row["eligibility_reason"] = reason
        if "review_reason" in row:
            prior = str(row.get("review_reason") or "").strip()
            row["review_reason"] = ";".join(part for part in (prior, reason) if part)
    return rows


def lineage_rows_from_rank_rows(
    rows: Iterable[Mapping[str, Any]], *, model_family: str
) -> list[dict[str, str]]:
    return [
        {
            "ticker": normalize_ticker(row.get("ticker")),
            "model_family": model_family,
            **{field: str(row.get(field) or "") for field in LINEAGE_FIELDS},
        }
        for row in rows
    ]


def write_financial_lineage_report(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    model_family: str,
    asof: str | None = None,
    policy_context: str = "production",
) -> dict[str, Any]:
    source_rows = [dict(row) for row in rows]
    report_rows = lineage_rows_from_rank_rows(source_rows, model_family=model_family)
    write_csv_atomic(path, LINEAGE_REPORT_FIELDS, report_rows)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    checked_dates = {
        str(row.get("financial_lineage_checked_asof_date") or "").strip()
        for row in source_rows
        if str(row.get("financial_lineage_checked_asof_date") or "").strip()
    }
    expected_asof = str(asof or "").strip()
    if not expected_asof and len(checked_dates) == 1:
        expected_asof = next(iter(checked_dates))
    policy = policy_for_model_family(model_family)
    evaluation = evaluate_financial_lineage_rows(
        source_rows,
        policy_mode=policy.mode_for(policy_context),
        expected_asof=expected_asof,
        min_core_metric_count=policy.min_core_metric_count,
    )
    manifest = {
        "path": str(path),
        "sha256": digest,
        **evaluation_manifest(evaluation, policy=policy, context=policy_context),
        "review_required_count": sum(
            row["financial_lineage_gate"] != "1" for row in report_rows
        ),
        "investable_unresolved_count": sum(
            str(row.get("portfolio_candidate_gate") or "") == "1"
            and str(row.get("financial_lineage_gate") or "") != "1"
            for row in source_rows
        ),
    }
    return manifest


def validate_financial_lineage_rank_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_asof: str | None = None,
    policy_mode: str = POLICY_CANDIDATE_ONLY,
) -> list[str]:
    materialized = [dict(row) for row in rows]
    if expected_asof is None:
        checked_dates = {
            str(row.get("financial_lineage_checked_asof_date") or "").strip()
            for row in materialized
            if str(row.get("financial_lineage_checked_asof_date") or "").strip()
        }
        expected_asof = next(iter(checked_dates)) if len(checked_dates) == 1 else ""
    evaluation = evaluate_financial_lineage_rows(
        materialized,
        policy_mode=policy_mode,
        expected_asof=expected_asof,
    )
    return evaluation.errors


def read_financial_lineage_report(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]
