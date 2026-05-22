from __future__ import annotations

from pathlib import Path

from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.text_norm import AliasCandidate, build_company_aliases, normalize_org_name


DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\biotech_index.sqlite")
ASOF_DATE = "2026-05-15"
SOURCE = "user_requested_activation_2026_05_21"

TARGETS = [
    {
        "ticker": "BBIO",
        "cik": "0001743881",
        "company_name": "BRIDGEBIO PHARMA INC",
        "reason_codes": "manual_reactivation_review;user_requested_activation",
        "aliases": ["BridgeBio Pharma", "BridgeBio Pharmaceuticals"],
    },
    {
        "ticker": "ARWR",
        "cik": "0000879407",
        "company_name": "ARROWHEAD PHARMACEUTICALS INC",
        "reason_codes": "manual_reactivation_review;user_requested_activation",
        "aliases": ["Arrowhead Pharmaceuticals", "Arrowhead Pharmaceuticals Inc"],
    },
    {
        "ticker": "NBIX",
        "cik": "0000914475",
        "company_name": "NEUROCRINE BIOSCIENCES INC",
        "reason_codes": "manual_include;user_requested_addition",
        "aliases": ["Neurocrine Biosciences", "Neurocrine Biosciences Inc"],
    },
    {
        "ticker": "ASND",
        "cik": "0001612042",
        "company_name": "ASCENDIS PHARMA A/S",
        "reason_codes": "manual_include;user_requested_addition",
        "aliases": ["Ascendis Pharma", "Ascendis Pharma A/S"],
    },
]


def insert_aliases(conn, *, company_id: int, company_name: str, ticker: str, aliases: list[str]) -> int:
    now = utc_now()
    alias_candidates = [
        AliasCandidate(alias_raw=ticker, alias_norm=normalize_org_name(ticker), source="ticker", confidence=1.0),
        *build_company_aliases(company_name),
        *[
            AliasCandidate(
                alias_raw=alias,
                alias_norm=normalize_org_name(alias),
                source="manual_sponsor_alias",
                confidence=0.95,
                is_manual=True,
            )
            for alias in aliases
        ],
    ]
    inserted = 0
    for alias in alias_candidates:
        if not alias.alias_norm:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO company_aliases(
                company_id, alias_raw, alias_norm, source, confidence, is_manual, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                alias.alias_raw,
                alias.alias_norm,
                alias.source,
                float(alias.confidence),
                1 if alias.is_manual else 0,
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def main() -> None:
    with connect(DB_PATH, timeout_sec=30.0) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="activate_user_requested_biotech_targets", input_path=Path(SOURCE))
        row_count = 0
        alias_count = 0
        try:
            now = utc_now()
            with conn:
                for target in TARGETS:
                    row = conn.execute(
                        """
                        INSERT INTO companies(
                            ticker, cik, company_name, exchange, sector, industry, industry_aggregate,
                            security_type, is_primary_listing, listing_status, country, currency,
                            manual_include, manual_exclude, manual_review, notes,
                            universe_status, is_active, source_screen_decision, reason_codes,
                            first_seen_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(ticker) DO UPDATE SET
                            cik = excluded.cik,
                            company_name = excluded.company_name,
                            exchange = excluded.exchange,
                            sector = excluded.sector,
                            industry = excluded.industry,
                            industry_aggregate = excluded.industry_aggregate,
                            security_type = excluded.security_type,
                            is_primary_listing = excluded.is_primary_listing,
                            listing_status = excluded.listing_status,
                            country = excluded.country,
                            currency = excluded.currency,
                            manual_include = excluded.manual_include,
                            manual_exclude = excluded.manual_exclude,
                            manual_review = excluded.manual_review,
                            notes = excluded.notes,
                            universe_status = excluded.universe_status,
                            is_active = excluded.is_active,
                            source_screen_decision = excluded.source_screen_decision,
                            reason_codes = excluded.reason_codes,
                            updated_at = excluded.updated_at
                        RETURNING company_id
                        """,
                        (
                            target["ticker"],
                            target["cik"],
                            target["company_name"],
                            "NASDAQ",
                            "Healthcare",
                            "Biotechnology",
                            "Healthcare: Biopharma & Research Tools",
                            "Common Stock",
                            "TRUE",
                            "active",
                            "United States" if target["ticker"] in {"BBIO", "ARWR", "NBIX"} else "Denmark",
                            "USD",
                            "true",
                            "",
                            "true",
                            "User requested active biotech scoring/source coverage on 2026-05-21.",
                            "keep",
                            1,
                            "keep",
                            target["reason_codes"],
                            now,
                            now,
                        ),
                    ).fetchone()
                    company_id = int(row["company_id"])
                    alias_count += insert_aliases(
                        conn,
                        company_id=company_id,
                        company_name=target["company_name"],
                        ticker=target["ticker"],
                        aliases=target["aliases"],
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO company_universe_history(
                            asof_date, company_id, ticker, universe_status, reason_codes, source_file, run_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ASOF_DATE,
                            company_id,
                            target["ticker"],
                            "keep",
                            target["reason_codes"],
                            SOURCE,
                            run_id,
                            now,
                        ),
                    )
                    row_count += 1
            finish_run(conn, run_id=run_id, status="success", row_count=row_count, message=f"aliases={alias_count}")
            print(f"activated={row_count} aliases_attempted={alias_count}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
