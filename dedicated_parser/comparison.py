from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any

from dedicated_parser.storage import utc_now


def _candidate_key(row: sqlite3.Row) -> tuple[Any, ...]:
    value = row["candidate_value"]
    return (
        str(row["metric_name"]),
        round(float(value), 6) if value is not None else None,
        str(row["unit"] or "").upper(),
        str(row["period_start"] or ""),
        str(row["period_end"] or ""),
    )


def _date_distance(first: str, second: str) -> int | None:
    try:
        return abs((date.fromisoformat(first) - date.fromisoformat(second)).days)
    except ValueError:
        return None


def _period_corrections(
    legacy_only: set[tuple[Any, ...]],
    shadow_only: set[tuple[Any, ...]],
) -> tuple[
    list[tuple[tuple[Any, ...], tuple[Any, ...]]],
    set[tuple[Any, ...]],
    set[tuple[Any, ...]],
]:
    corrections: list[tuple[tuple[Any, ...], tuple[Any, ...]]] = []
    remaining_legacy = set(legacy_only)
    remaining_shadow = set(shadow_only)
    for legacy_key in sorted(legacy_only):
        candidates = [
            shadow_key
            for shadow_key in remaining_shadow
            if shadow_key[:4] == legacy_key[:4]
            and (
                distance := _date_distance(
                    str(legacy_key[4]),
                    str(shadow_key[4]),
                )
            )
            is not None
            and distance <= 62
        ]
        if not candidates:
            continue
        # Deterministic tie-break: candidates comes from a set, whose
        # iteration order is hash-randomized; equidistant candidates would
        # otherwise vary between identical runs.
        winner = min(
            candidates,
            key=lambda key: (
                _date_distance(str(legacy_key[4]), str(key[4])) or 0,
                key,
            ),
        )
        corrections.append((legacy_key, winner))
        remaining_legacy.remove(legacy_key)
        remaining_shadow.remove(winner)
    return corrections, remaining_legacy, remaining_shadow


def compare_shadow_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    model_family: str,
    asof_date: str,
    requested_metrics: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    all_shadow_rows = conn.execute(
        """
        SELECT e.ticker, e.metric_name, e.candidate_value, e.unit,
               e.period_start, e.period_end, e.accession_number,
               e.candidate_status
        FROM sec_parser_metric_evidence_shadow AS e
        JOIN sec_parser_run_metric_evidence AS r
          ON r.evidence_key = e.evidence_key
        WHERE r.run_id = ? AND e.model_family = ?
          AND SUBSTR(COALESCE(NULLIF(e.accepted_at, ''), e.filing_date), 1, 10) <= ?
        """,
        (run_id, model_family, asof_date),
    ).fetchall()
    shadow_rows = [row for row in all_shadow_rows if str(row["candidate_status"]) == "ACCEPTED"]
    rejected_shadow_rows = [
        row for row in all_shadow_rows if str(row["candidate_status"]).startswith(("REJECTED", "SUPPRESSED"))
    ]
    work_scope = [
        (str(row["ticker"]), str(row["accession_number"]))
        for row in conn.execute(
            """
            SELECT DISTINCT ticker, accession_number
            FROM sec_parser_run_work
            WHERE run_id = ?
            """,
            (run_id,),
        )
    ]
    if not work_scope:
        return []
    # Attempted pairs derive from the WORK SCOPE, not from emitted evidence:
    # a filing searched cleanly with zero candidates emits nothing, and
    # deriving attempts from evidence would hide exactly the worst class of
    # regression (legacy-ACCEPTED value that the shadow parser missed).
    work_tickers = {ticker for ticker, _ in work_scope}
    attempted_groups = {(ticker, metric_name) for ticker in work_tickers for metric_name in requested_metrics} | {
        (str(row["ticker"]), str(row["metric_name"])) for row in all_shadow_rows
    }
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS temp_sec_parser_work_scope(
            ticker TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            PRIMARY KEY(ticker, accession_number)
        ) WITHOUT ROWID
        """
    )
    conn.execute("DELETE FROM temp_sec_parser_work_scope")
    conn.executemany(
        """
        INSERT OR IGNORE INTO temp_sec_parser_work_scope(
            ticker, accession_number
        ) VALUES (?, ?)
        """,
        work_scope,
    )
    has_legacy_candidates = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
        "AND name='fact_sec_metric_disclosure_candidate'"
    ).fetchone() is not None
    legacy_rows = conn.execute(
        """
        SELECT candidate.ticker, candidate.metric_name,
               candidate.candidate_value, candidate.unit,
               candidate.period_start, candidate.period_end,
               candidate.accession_number
        FROM fact_sec_metric_disclosure_candidate AS candidate
        JOIN temp_sec_parser_work_scope AS scope
          ON scope.ticker = candidate.ticker
         AND scope.accession_number = candidate.accession_number
        WHERE candidate.model_family = ?
          AND candidate.candidate_status = 'ACCEPTED'
          AND SUBSTR(
              COALESCE(
                  NULLIF(candidate.accepted_at, ''),
                  candidate.filing_date
              ),
              1,
              10
          ) <= ?
        """,
        (model_family, asof_date),
    ).fetchall() if has_legacy_candidates else []
    has_legacy_facts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
        "AND name='fact_sec_xbrl_fact'"
    ).fetchone() is not None
    legacy_fact_rows = conn.execute(
        """
        SELECT fact.ticker, fact.canonical_metric AS metric_name,
               fact.value AS candidate_value, fact.unit,
               fact.period_start, fact.period_end,
               fact.accession_number
        FROM fact_sec_xbrl_fact AS fact
        JOIN temp_sec_parser_work_scope AS scope
          ON scope.ticker = fact.ticker
         AND scope.accession_number = fact.accession_number
        WHERE fact.canonical_metric IN (
              'orders',
              'funded_backlog',
              'reported_backlog',
              'remaining_performance_obligation',
              'rpo_current'
          )
          AND fact.value IS NOT NULL
          AND SUBSTR(
              COALESCE(NULLIF(fact.accepted_at, ''), fact.filing_date),
              1,
              10
          ) <= ?
        """,
        (asof_date,),
    ).fetchall() if has_legacy_facts else []
    conn.execute("DROP TABLE temp_sec_parser_work_scope")
    legacy: dict[tuple[str, str], set[tuple[Any, ...]]] = defaultdict(set)
    legacy_accessions: dict[
        tuple[str, str, tuple[Any, ...]],
        set[str],
    ] = defaultdict(set)
    shadow: dict[tuple[str, str], set[tuple[Any, ...]]] = defaultdict(set)
    for row in legacy_rows:
        group = (str(row["ticker"]), str(row["metric_name"]))
        candidate_key = _candidate_key(row)
        legacy[group].add(candidate_key)
        legacy_accessions[(group[0], group[1], candidate_key)].add(str(row["accession_number"]))
    canonical_facts: dict[tuple[str, str], set[tuple[Any, ...]]] = defaultdict(set)
    for row in legacy_fact_rows:
        group = (str(row["ticker"]), str(row["metric_name"]))
        candidate_key = _candidate_key(row)
        canonical_facts[group].add(candidate_key)
        legacy_accessions[(group[0], group[1], candidate_key)].add(str(row["accession_number"]))
    for row in shadow_rows:
        shadow[(str(row["ticker"]), str(row["metric_name"]))].add(_candidate_key(row))
    rejected_identities = {
        (
            str(row["ticker"]),
            # Metric is part of the identity: reported_backlog and RPO
            # routinely disclose the identical dollar amount in one filing,
            # and a rejected RPO candidate must not absolve a genuine backlog
            # regression.
            str(row["metric_name"]),
            str(row["accession_number"]),
            round(float(row["candidate_value"]), 6) if row["candidate_value"] is not None else None,
            str(row["unit"] or "").upper(),
        )
        for row in rejected_shadow_rows
    }
    # Canonical facts can prove that a shadow result is not genuinely new, but
    # they do not define prose-policy regressions. One accession can contain
    # comparative or dimensional facts that the prose adapter should not emit.
    for group, shadow_keys in shadow.items():
        legacy[group].update(shadow_keys & canonical_facts[group])
    results: list[dict[str, Any]] = []
    conn.execute(
        "DELETE FROM sec_parser_shadow_comparison WHERE run_id = ?",
        (run_id,),
    )
    comparison_groups = (set(legacy) | set(shadow)) & attempted_groups
    for ticker, metric_name in sorted(comparison_groups):
        legacy_keys = legacy[(ticker, metric_name)]
        shadow_keys = shadow[(ticker, metric_name)]
        matched = legacy_keys & shadow_keys
        legacy_only = legacy_keys - shadow_keys
        shadow_only = shadow_keys - legacy_keys
        period_corrections, legacy_only, shadow_only = _period_corrections(
            legacy_only,
            shadow_only,
        )
        duplicate_period_corrections = {
            legacy_key
            for legacy_key in legacy_only
            if any(
                shadow_key[:4] == legacy_key[:4]
                and (
                    distance := _date_distance(
                        str(legacy_key[4]),
                        str(shadow_key[4]),
                    )
                )
                is not None
                and distance <= 75
                for shadow_key in shadow_keys
            )
        }
        legacy_only -= duplicate_period_corrections
        policy_corrections = {
            key
            for key in legacy_only
            if any(
                (
                    ticker,
                    metric_name,
                    accession,
                    key[1],
                    key[2],
                )
                in rejected_identities
                for accession in legacy_accessions[(ticker, metric_name, key)]
            )
        }
        legacy_only -= policy_corrections
        if not legacy_only and not shadow_only and policy_corrections:
            status = "POLICY_CORRECTION"
        elif (
            not legacy_only
            and not shadow_only
            and period_corrections
            or (not legacy_only and not shadow_only and duplicate_period_corrections)
        ):
            status = "PERIOD_CORRECTION"
        elif not legacy_only and not shadow_only:
            status = "MATCH"
        elif shadow_only and not legacy_only:
            status = "SHADOW_ADDITION"
        elif legacy_only and not shadow_only:
            status = "SHADOW_REGRESSION"
        else:
            status = "DIFFERENT"
        details = {
            "legacy_only": sorted(legacy_only),
            "shadow_only": sorted(shadow_only),
            "period_corrections": sorted(period_corrections),
            "duplicate_period_corrections": sorted(duplicate_period_corrections),
            "policy_corrections": sorted(policy_corrections),
        }
        row = {
            "run_id": run_id,
            "model_family": model_family,
            "ticker": ticker,
            "metric_name": metric_name,
            "legacy_accepted_count": len(legacy_keys),
            "shadow_accepted_count": len(shadow_keys),
            "matched_count": (len(matched) + len(period_corrections) + len(duplicate_period_corrections)),
            "legacy_only_count": len(legacy_only),
            "shadow_only_count": len(shadow_only),
            "comparison_status": status,
            "details_json": json.dumps(
                details,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        results.append(row)
        conn.execute(
            """
            INSERT INTO sec_parser_shadow_comparison(
                run_id, model_family, ticker, metric_name,
                legacy_accepted_count, shadow_accepted_count, matched_count,
                legacy_only_count, shadow_only_count, comparison_status,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*row.values(), utc_now()),
        )
    conn.commit()
    return results
