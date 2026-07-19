from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from industrials.core.db import utc_now
from industrials.core.text_norm import as_bool, normalize_cik, normalize_ticker
from industrials.machinery.contracts import cohort_metadata, read_csv_rows, validate_seed_contracts


MODEL_FAMILY = "machinery"
CURRENT_MEMBERSHIP_BASIS = "current_source_of_truth"
HISTORICAL_MEMBERSHIP_BASIS = "survivorship_corrected_pit_contract"


def _parse_date(value: object, *, field: str, ticker: str, allow_blank: bool = False) -> str:
    text = str(value or "").strip()[:10]
    if allow_blank and not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{ticker}: invalid {field}={value!r}; expected YYYY-MM-DD") from exc


def _company_id(conn: sqlite3.Connection, ticker: str) -> int:
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        raise RuntimeError(f"Company upsert failed for {ticker}")
    return int(row["company_id"])


def _insert_company_alias(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    alias: str,
    source_id: str,
) -> None:
    alias_clean = str(alias or "").strip()
    if not alias_clean:
        return
    alias_norm = " ".join(alias_clean.upper().split())
    exists = conn.execute(
        """
        SELECT 1
        FROM dim_company_alias
        WHERE company_id = ? AND alias_norm = ? AND source_id = ?
        LIMIT 1
        """,
        (company_id, alias_norm, source_id),
    ).fetchone()
    if exists is not None:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_company_alias(
            company_id, alias_raw, alias_norm, source_id, confidence, is_manual, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 1.0, 0, ?, ?)
        """,
        (company_id, alias_clean, alias_norm, source_id, now, now),
    )


def _insert_identifier(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    identifier_type: str,
    identifier_value: str,
    source_id: str,
    confidence: float = 1.0,
) -> None:
    value = str(identifier_value or "").strip()
    if not value:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_identifier(
            company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, identifier_type, identifier_value) DO UPDATE SET
            source_id = excluded.source_id,
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        (company_id, identifier_type, value, source_id, confidence, now, now),
    )


def _upsert_taxonomy(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ticker: str,
    sector: str,
    industry: str,
    subsector: str,
    cohort_id: str,
    cohort: dict[str, str],
    taxonomy_source: str,
    historical_only: bool = False,
) -> None:
    now = utc_now()
    calibration_use = "historical_research" if historical_only else cohort.get("calibration_use", "core")
    development_stage = "historical_delisted" if historical_only else cohort.get("development_stage", "operating")
    conn.execute(
        """
        INSERT INTO dim_industrials_taxonomy(
            company_id, ticker, model_family, sector, industry, subsector,
            calibration_cohort_id, calibration_cohort, calibration_use, development_stage,
            taxonomy_confidence, taxonomy_source, analyst_reviewed, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, 1, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            company_id = excluded.company_id,
            sector = excluded.sector,
            industry = excluded.industry,
            subsector = excluded.subsector,
            calibration_cohort_id = excluded.calibration_cohort_id,
            calibration_cohort = excluded.calibration_cohort,
            calibration_use = excluded.calibration_use,
            development_stage = excluded.development_stage,
            taxonomy_confidence = excluded.taxonomy_confidence,
            taxonomy_source = excluded.taxonomy_source,
            analyst_reviewed = excluded.analyst_reviewed,
            updated_at = excluded.updated_at
        """,
        (
            company_id,
            ticker,
            MODEL_FAMILY,
            sector,
            industry,
            subsector,
            cohort_id,
            cohort.get("cohort_name", cohort_id),
            calibration_use,
            development_stage,
            taxonomy_source,
            now,
        ),
    )


def _upsert_membership(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ticker: str,
    source_id: str,
    basis: str,
    start_date: str,
    end_date: str,
    status: str,
    is_current: bool,
    confidence: float,
    reason: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_source_id, membership_basis,
            start_date, end_date, membership_status, is_current_member,
            point_in_time_flag, confidence, reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family, membership_source_id, start_date) DO UPDATE SET
            company_id = excluded.company_id,
            membership_basis = excluded.membership_basis,
            end_date = excluded.end_date,
            membership_status = excluded.membership_status,
            is_current_member = excluded.is_current_member,
            point_in_time_flag = excluded.point_in_time_flag,
            confidence = excluded.confidence,
            reason = excluded.reason,
            updated_at = excluded.updated_at
        """,
        (
            company_id,
            ticker,
            MODEL_FAMILY,
            source_id,
            basis,
            start_date,
            end_date,
            status,
            int(is_current),
            confidence,
            reason,
            now,
            now,
        ),
    )


def load_listing_dates(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    return {
        normalize_ticker(row.get("ticker")): row
        for row in read_csv_rows(path)
        if normalize_ticker(row.get("ticker"))
    }


def load_active_universe(
    conn: sqlite3.Connection,
    *,
    active_path: Path,
    delisted_path: Path,
    cohort_path: Path,
    listing_path: Path,
    seed_source_id: str,
    cohort_source_id: str,
    optimization_start: str,
    expected_active: int,
    expected_delisted: int,
) -> int:
    active_rows = read_csv_rows(active_path)
    delisted_rows = read_csv_rows(delisted_path)
    cohorts = cohort_metadata(cohort_path)
    errors = validate_seed_contracts(
        active_rows,
        delisted_rows,
        cohorts,
        expected_active=expected_active,
        expected_delisted=expected_delisted,
    )
    if errors:
        raise ValueError("; ".join(errors[:20]))
    listing_dates = load_listing_dates(listing_path)
    active_tickers = {normalize_ticker(row.get("ticker")) for row in active_rows}
    stale_rows = conn.execute(
        "SELECT ticker FROM dim_industrials_taxonomy WHERE model_family = ?",
        (MODEL_FAMILY,),
    ).fetchall()
    stale_tickers = {normalize_ticker(row["ticker"]) for row in stale_rows} - active_tickers

    conn.execute(
        "DELETE FROM dim_universe_membership WHERE model_family = ? AND membership_source_id = ?",
        (MODEL_FAMILY, seed_source_id),
    )
    for row in active_rows:
        ticker = normalize_ticker(row.get("ticker"))
        cik = normalize_cik(row.get("cik"))
        company_name = str(row.get("company_name") or ticker).strip()
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_company(
                ticker, cik, company_name, sector, industry, subsector, country, currency,
                universe_status, is_active, data_quality_status, first_seen_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'investable', 1, 'seed_validated', ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                cik = excluded.cik,
                company_name = excluded.company_name,
                sector = excluded.sector,
                industry = excluded.industry,
                subsector = excluded.subsector,
                country = excluded.country,
                currency = excluded.currency,
                universe_status = excluded.universe_status,
                is_active = 1,
                data_quality_status = excluded.data_quality_status,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                cik,
                company_name,
                row["sector"],
                row["industry"],
                row["subsector"],
                row["country"],
                row["currency"],
                now,
                now,
            ),
        )
        company_id = _company_id(conn, ticker)
        conn.execute(
            """
            INSERT INTO dim_security(
                company_id, ticker, exchange, security_type, listing_status,
                is_primary_listing, currency, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, exchange) DO UPDATE SET
                company_id = excluded.company_id,
                security_type = excluded.security_type,
                listing_status = excluded.listing_status,
                is_primary_listing = excluded.is_primary_listing,
                currency = excluded.currency,
                updated_at = excluded.updated_at
            """,
            (
                company_id,
                ticker,
                row["exchange"],
                row["security_type"],
                row["listing_status"],
                int(as_bool(row["is_primary_listing"], default=False)),
                row["currency"],
                now,
                now,
            ),
        )
        _insert_identifier(
            conn,
            company_id=company_id,
            identifier_type="CIK",
            identifier_value=cik,
            source_id=seed_source_id,
        )
        _insert_company_alias(conn, company_id=company_id, alias=ticker, source_id=seed_source_id)
        _insert_company_alias(conn, company_id=company_id, alias=company_name, source_id=seed_source_id)
        cohort_id = row["calibration_cohort"]
        _upsert_taxonomy(
            conn,
            company_id=company_id,
            ticker=ticker,
            sector=row["sector"],
            industry=row["industry"],
            subsector=row["subsector"],
            cohort_id=cohort_id,
            cohort=cohorts[cohort_id],
            taxonomy_source=cohort_source_id,
        )
        listing = listing_dates.get(ticker, {})
        start_date = str(listing.get("first_eligible_date") or optimization_start)
        _upsert_membership(
            conn,
            company_id=company_id,
            ticker=ticker,
            source_id=seed_source_id,
            basis=CURRENT_MEMBERSHIP_BASIS,
            start_date=_parse_date(start_date, field="start_date", ticker=ticker),
            end_date="",
            status="active",
            is_current=True,
            confidence=float(listing.get("confidence") or 1.0),
            reason="Canonical machinery active seed bounded by the reviewed listing-date contract.",
        )

    for ticker in stale_tickers:
        current_elsewhere = conn.execute(
            """
            SELECT 1 FROM dim_universe_membership
            WHERE ticker = ? AND is_current_member = 1 AND model_family <> ?
            LIMIT 1
            """,
            (ticker, MODEL_FAMILY),
        ).fetchone()
        if current_elsewhere is not None:
            continue
        machinery_history = conn.execute(
            "SELECT 1 FROM dim_universe_membership WHERE ticker = ? AND model_family = ? LIMIT 1",
            (ticker, MODEL_FAMILY),
        ).fetchone()
        if machinery_history is not None:
            # Historical machinery members (e.g. delisted calibration names) keep the
            # status assigned by the historical loader; only true seed removals flip here.
            continue
        conn.execute(
            "UPDATE dim_company SET is_active = 0, universe_status = 'removed_from_machinery_seed', updated_at = ? WHERE ticker = ?",
            (utc_now(), ticker),
        )
    placeholders = ",".join("?" for _ in active_tickers)
    conn.execute(
        f"""
        DELETE FROM dim_industrials_taxonomy
        WHERE model_family = ?
          AND ticker NOT IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1
              FROM dim_universe_membership m
              WHERE m.model_family = dim_industrials_taxonomy.model_family
                AND m.ticker = dim_industrials_taxonomy.ticker
          )
        """,
        (MODEL_FAMILY, *sorted(active_tickers)),
    )
    return len(active_rows)


def _confidence(value: object, default: float = 0.7) -> float:
    try:
        return max(0.0, min(1.0, float(str(value or "").strip())))
    except ValueError:
        return default


def load_historical_membership(
    conn: sqlite3.Connection,
    *,
    membership_path: Path,
    delisted_path: Path,
    cohort_path: Path,
    membership_source_id: str,
    delisted_source_id: str,
    norgate_source_id: str,
) -> tuple[int, int]:
    memberships = read_csv_rows(membership_path)
    delisted_rows = read_csv_rows(delisted_path)
    cohorts = cohort_metadata(cohort_path)
    required = {
        "internal_ticker",
        "exchange_ticker",
        "price_source_symbol",
        "company_name",
        "cik",
        "exchange",
        "country",
        "currency",
        "security_type",
        "calibration_cohort_id",
        "calibration_cohort",
        "start_date",
        "end_date",
        "membership_status",
        "confidence",
    }
    if not memberships:
        raise ValueError(f"Historical membership contract is empty: {membership_path}")
    missing = sorted(required - set(memberships[0]))
    if missing:
        raise ValueError(f"Historical membership contract missing columns={missing}")
    incoming = [normalize_ticker(row.get("internal_ticker")) for row in memberships]
    if not all(incoming) or len(set(incoming)) != len(incoming):
        raise ValueError("Historical membership internal_ticker values must be nonblank and unique")

    conn.execute(
        "DELETE FROM dim_universe_membership WHERE model_family = ? AND membership_source_id = ?",
        (MODEL_FAMILY, membership_source_id),
    )
    conn.execute("DELETE FROM dim_delisted_calibration_seed WHERE model_family = ?", (MODEL_FAMILY,))

    active_tickers = {
        normalize_ticker(row["ticker"])
        for row in conn.execute(
            """
            SELECT ticker
            FROM dim_universe_membership
            WHERE model_family = ?
              AND membership_basis = ?
              AND is_current_member = 1
            """,
            (MODEL_FAMILY, CURRENT_MEMBERSHIP_BASIS),
        ).fetchall()
    }
    for row in memberships:
        ticker = normalize_ticker(row["internal_ticker"])
        cik = normalize_cik(row["cik"])
        end_date = _parse_date(row.get("end_date"), field="end_date", ticker=ticker, allow_blank=True)
        is_current = not end_date
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_company(
                ticker, cik, company_name, sector, industry, subsector, country, currency,
                universe_status, is_active, data_quality_status, first_seen_at, updated_at
            )
            VALUES (?, ?, ?, 'Industrials', 'Machinery', 'Machinery', ?, ?, ?, ?, 'historical_membership_validated', ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                cik = COALESCE(NULLIF(dim_company.cik, ''), excluded.cik),
                company_name = COALESCE(NULLIF(dim_company.company_name, ''), excluded.company_name),
                country = COALESCE(NULLIF(dim_company.country, ''), excluded.country),
                currency = COALESCE(NULLIF(dim_company.currency, ''), excluded.currency),
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                cik,
                row["company_name"],
                row["country"],
                row["currency"],
                "investable" if is_current else "historical_delisted",
                int(is_current),
                now,
                now,
            ),
        )
        company_id = _company_id(conn, ticker)
        if ticker not in active_tickers:
            conn.execute(
                """
                INSERT INTO dim_security(
                    company_id, ticker, exchange, security_type, listing_status,
                    is_primary_listing, currency, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'historical_delisted', 1, ?, ?, ?)
                ON CONFLICT(ticker, exchange) DO UPDATE SET
                    company_id = excluded.company_id,
                    security_type = excluded.security_type,
                    listing_status = excluded.listing_status,
                    currency = excluded.currency,
                    updated_at = excluded.updated_at
                """,
                (company_id, ticker, row["exchange"], row["security_type"], row["currency"], now, now),
            )
        _insert_identifier(
            conn,
            company_id=company_id,
            identifier_type="CIK",
            identifier_value=cik,
            source_id=membership_source_id,
            confidence=_confidence(row.get("confidence")),
        )
        _insert_identifier(
            conn,
            company_id=company_id,
            identifier_type="NORGATE_SYMBOL",
            identifier_value=row["price_source_symbol"],
            source_id=norgate_source_id,
            confidence=_confidence(row.get("confidence")),
        )
        cohort_id = row["calibration_cohort_id"]
        if cohort_id not in cohorts:
            raise ValueError(f"{ticker}: unknown historical cohort={cohort_id}")
        if ticker not in active_tickers:
            # Active-seed tickers keep the granular taxonomy loaded from the seed CSV
            # (script 01); upserting here would overwrite industry/subsector/source
            # with the coarse membership-level labels on every 01b rerun.
            _upsert_taxonomy(
                conn,
                company_id=company_id,
                ticker=ticker,
                sector="Industrials",
                industry="Machinery",
                subsector="Machinery",
                cohort_id=cohort_id,
                cohort=cohorts[cohort_id],
                taxonomy_source=membership_source_id,
                historical_only=True,
            )
        _upsert_membership(
            conn,
            company_id=company_id,
            ticker=ticker,
            source_id=membership_source_id,
            basis=HISTORICAL_MEMBERSHIP_BASIS,
            start_date=_parse_date(row["start_date"], field="start_date", ticker=ticker),
            end_date=end_date,
            status=row["membership_status"],
            is_current=is_current,
            confidence=_confidence(row.get("confidence")),
            reason=str(row.get("notes") or "Norgate-resolved PIT machinery membership")[:500],
        )

    for row in delisted_rows:
        ticker = normalize_ticker(row.get("ticker"))
        cohort_id = str(row.get("cohort") or "").strip()
        if cohort_id not in cohorts:
            raise ValueError(f"{ticker}: unknown delisted cohort={cohort_id}")
        confidence_label = str(row.get("confidence") or "").strip().lower()
        confidence_score = {"verified": 0.95, "high": 0.90, "medium": 0.70, "low": 0.50}.get(
            confidence_label,
            0.60,
        )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_delisted_calibration_seed(
                ticker, internal_ticker, model_family, company_name, calibration_cohort_id,
                exit_type, terminal_type, acquirer, exit_year, cik, confidence_label,
                confidence_score, source_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, model_family) DO UPDATE SET
                internal_ticker = excluded.internal_ticker,
                company_name = excluded.company_name,
                calibration_cohort_id = excluded.calibration_cohort_id,
                exit_type = excluded.exit_type,
                terminal_type = excluded.terminal_type,
                acquirer = excluded.acquirer,
                exit_year = excluded.exit_year,
                cik = excluded.cik,
                confidence_label = excluded.confidence_label,
                confidence_score = excluded.confidence_score,
                source_id = excluded.source_id,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                ticker,
                MODEL_FAMILY,
                row["company"],
                cohort_id,
                row.get("exit_type") or "",
                row.get("terminal_type") or "",
                row.get("acquirer") or "",
                int(row["exit_year"]) if str(row.get("exit_year") or "").isdigit() else None,
                normalize_cik(row.get("cik")),
                confidence_label,
                confidence_score,
                delisted_source_id,
                now,
                now,
            ),
        )
    return len(memberships), len(delisted_rows)


def load_ticker_aliases(conn: sqlite3.Connection, *, path: Path, source_id: str) -> int:
    rows = read_csv_rows(path)
    conn.execute("DELETE FROM dim_ticker_alias WHERE source_id = ?", (source_id,))
    conn.execute(
        "DELETE FROM fact_corporate_action WHERE source_id = ? AND action_type = 'ticker_alias'",
        (source_id,),
    )
    count = 0
    for row in rows:
        contract = normalize_ticker(row.get("contract_ticker"))
        active = normalize_ticker(row.get("active_ticker"))
        effective = _parse_date(row.get("effective_date"), field="effective_date", ticker=contract)
        if not contract or not active or not as_bool(row.get("verified_flag"), default=False):
            raise ValueError(f"Alias row must have verified contract/active tickers: {row}")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_ticker_alias(
                contract_ticker, active_ticker, predecessor_ticker, effective_date,
                price_history_csv, issuer_id, reason, source, verified_flag, notes,
                source_id, created_at, updated_at
            )
            VALUES (?, ?, NULLIF(?, ''), ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(contract_ticker, effective_date) DO UPDATE SET
                active_ticker = excluded.active_ticker,
                predecessor_ticker = excluded.predecessor_ticker,
                price_history_csv = excluded.price_history_csv,
                issuer_id = excluded.issuer_id,
                reason = excluded.reason,
                source = excluded.source,
                verified_flag = excluded.verified_flag,
                notes = excluded.notes,
                source_id = excluded.source_id,
                updated_at = excluded.updated_at
            """,
            (
                contract,
                active,
                normalize_ticker(row.get("predecessor_ticker")),
                effective,
                row.get("price_history_csv") or "",
                row.get("issuer_id") or "",
                row.get("reason") or "",
                row.get("source") or "",
                row.get("notes") or "",
                source_id,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO fact_corporate_action(
                issuer_id, ticker, related_ticker, action_type, action_date,
                source_id, reason, notes, created_at, updated_at
            )
            VALUES (NULLIF(?, ''), ?, ?, 'ticker_alias', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, related_ticker, action_type, action_date) DO UPDATE SET
                issuer_id = excluded.issuer_id,
                source_id = excluded.source_id,
                reason = excluded.reason,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                row.get("issuer_id") or "",
                contract,
                active,
                effective,
                source_id,
                row.get("reason") or "",
                row.get("notes") or "",
                now,
                now,
            ),
        )
        count += 1
    return count


def validate_database_contract(
    conn: sqlite3.Connection,
    *,
    active_source_id: str,
    historical_source_id: str,
    delisted_source_id: str,
    expected_active: int,
    expected_historical: int,
    expected_delisted: int,
) -> list[str]:
    checks = {
        "active_taxonomy": (
            "SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family = ? AND calibration_use <> 'historical_research'",
            (MODEL_FAMILY,),
            expected_active,
        ),
        "active_membership": (
            "SELECT COUNT(*) FROM dim_universe_membership WHERE model_family = ? AND membership_source_id = ?",
            (MODEL_FAMILY, active_source_id),
            expected_active,
        ),
        "historical_membership": (
            "SELECT COUNT(*) FROM dim_universe_membership WHERE model_family = ? AND membership_source_id = ?",
            (MODEL_FAMILY, historical_source_id),
            expected_historical,
        ),
        "delisted_seed": (
            "SELECT COUNT(*) FROM dim_delisted_calibration_seed WHERE model_family = ? AND source_id = ?",
            (MODEL_FAMILY, delisted_source_id),
            expected_delisted,
        ),
    }
    errors: list[str] = []
    for label, (sql, params, expected) in checks.items():
        actual = int(conn.execute(sql, params).fetchone()[0] or 0)
        if actual != expected:
            errors.append(f"{label}: expected={expected} actual={actual}")
    duplicate_taxonomy = conn.execute(
        """
        SELECT ticker, COUNT(*) AS n
        FROM dim_industrials_taxonomy
        WHERE model_family = ?
        GROUP BY ticker
        HAVING COUNT(*) > 1
        """,
        (MODEL_FAMILY,),
    ).fetchall()
    if duplicate_taxonomy:
        errors.append(f"duplicate machinery taxonomy tickers={[row['ticker'] for row in duplicate_taxonomy]}")
    bad_model_family = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM dim_universe_membership m
            WHERE m.model_family = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM dim_industrials_taxonomy t
                  WHERE t.ticker = m.ticker AND t.model_family = m.model_family
              )
            """,
            (MODEL_FAMILY,),
        ).fetchone()[0]
        or 0
    )
    if bad_model_family:
        errors.append(f"cross-family membership/taxonomy rows={bad_model_family}")
    return errors
