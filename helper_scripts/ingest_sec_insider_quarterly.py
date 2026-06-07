#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import os
import re
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sec_form4_config import cfg_get, load_sec_form4_config

SEC_DATASET_PAGE_DEFAULT = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\sec_insider.sqlite")
SEC_USER_AGENT = "JL, Independent Research, jm.357@yhotmail.com"
PLACEHOLDER_USER_AGENT = "Your Name your_email@example.com"
REQUEST_TIMEOUT_DEFAULT = 60
REQUEST_DOWNLOAD_TIMEOUT_DEFAULT = 120
SEC_HOST_HEADER_DEFAULT = "www.sec.gov"
SEC_ACCEPT_ENCODING_DEFAULT = "gzip, deflate"

REQUIRED_TABLES = (
    "sec_dataset_manifest",
    "sec_ownership_submission",
    "sec_ownership_reporting_owner",
    "sec_ownership_nonderiv_trans",
)
TO_SQL_CHUNKSIZE_DEFAULT = 50


def default_db_path() -> Path:
    return Path(os.getenv("SEC_INSIDER_DB_PATH", str(DEFAULT_DB_PATH)))


def default_raw_zip_dir() -> Path:
    return Path(
        os.getenv(
            "SEC_INSIDER_RAW_ZIP_DIR",
            str(default_db_path().parent / "raw_zips"),
        )
    )

# Canonical column mapping with tolerant aliases.
COLUMN_ALIASES: Dict[str, List[str]] = {
    # submission
    "accession_number": ["accession_number", "accessionnumber"],
    "document_type": ["document_type", "documenttype"],
    "filing_date": ["filing_date", "filingdate"],
    "period_of_report": ["period_of_report", "periodofreport"],
    "date_of_original_submission": [
        "date_of_original_submission",
        "dateoforiginalsubmission",
        "date_of_orig_submission",
        "dateoforigsubmission",
        "date_of_orig_sub",
    ],
    "issuer_cik": ["issuer_cik", "issuercik"],
    "issuer_name": ["issuer_name", "issuername"],
    "issuer_trading_symbol": ["issuer_trading_symbol", "issuertradingsymbol"],
    "aff10b5one": ["aff10b5one"],
    "accepted_ts_utc": [
        "accepted_ts_utc",
        "accepted_datetime",
        "acceptance_datetime",
        "acceptancedatetime",
        "acceptedat",
        "acceptance_time",
    ],

    # reporting owner
    "rptowner_cik": ["rptowner_cik", "rptownercik"],
    "rptowner_name": ["rptowner_name", "rptownername"],
    "rptowner_relationship": ["rptowner_relationship", "rptownerrelationship"],
    "rptowner_title": ["rptowner_title", "rptownertitle"],
    "is_director": ["is_director", "isdirector"],
    "is_officer": ["is_officer", "isofficer"],
    "is_ten_percent_owner": [
        "is_ten_percent_owner",
        "istenpercentowner",
        "is10percentowner",
    ],
    "is_other": ["is_other", "isother"],
    "officer_title": ["officer_title", "officertitle"],
    "other_text": ["other_text", "othertext"],

    # non-derivative transactions
    "nonderiv_trans_sk": ["nonderiv_trans_sk", "nonderivtrans_sk", "nonderivtranssk"],
    "security_title": ["security_title", "securitytitle"],
    "transaction_date": ["transaction_date", "transactiondate"],
    "transaction_code": ["transaction_code", "transactioncode", "trans_code"],
    "transaction_shares": ["transaction_shares", "transactionshares", "trans_shares"],
    "transaction_price_per_share": [
        "transaction_price_per_share",
        "transactionpricepershare",
        "trans_pricepershare",
    ],
    "transaction_acquired_disposed_code": [
        "transaction_acquired_disposed_code",
        "transactionacquireddisposedcode",
        "trans_acquired_disp_cd",
    ],
    "shares_owned_following_transaction": [
        "shares_owned_following_transaction",
        "sharesownedfollowingtransaction",
        "shrs_ownd_folwng_trans",
    ],
    "direct_or_indirect_ownership": [
        "direct_or_indirect_ownership",
        "directorindirectownership",
        "direct_indirect_ownership",
    ],
    "nature_of_ownership": ["nature_of_ownership", "natureofownership"],
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower().strip())


def find_best_columns(df: pd.DataFrame, wanted_cols: List[str]) -> pd.DataFrame:
    source_cols = {normalize_name(c): c for c in df.columns}
    rename_map = {}

    for canonical in wanted_cols:
        aliases = COLUMN_ALIASES.get(canonical, [canonical])
        found = None
        for alias in aliases:
            norm_alias = normalize_name(alias)
            if norm_alias in source_cols:
                found = source_cols[norm_alias]
                break
        if found:
            rename_map[found] = canonical

    out = df.rename(columns=rename_map).copy()
    for col in wanted_cols:
        if col not in out.columns:
            out[col] = None
    return out[wanted_cols]


def read_tsv_from_zip(zip_path: Path, logical_name: str) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        match = None
        logical = logical_name.lower()
        for name in names:
            base = Path(name).name.lower()
            if not base.endswith((".tsv", ".txt")):
                continue
            if logical == "nonderiv":
                # Prefer NONDERIV_TRANS and exclude NONDERIV_HOLDING.
                if "nonderiv" in base and "holding" not in base:
                    match = name
                    break
            elif logical in base:
                match = name
                break
        if match is None:
            raise FileNotFoundError(
                f"Could not find file for logical_name={logical_name!r} in {zip_path.name}. "
                f"Found: {names}"
            )

        raw = zf.read(match)
        return pd.read_csv(
            io.BytesIO(raw),
            sep="\t",
            dtype=str,
            low_memory=False,
            na_filter=False,
        )


def coerce_numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].replace("", pd.NA), errors="coerce")
    return df


def normalize_iso_dates(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    for col in cols:
        if col not in df.columns:
            continue
        raw = df[col].fillna("").astype(str).str.strip()
        dt = pd.to_datetime(raw, errors="coerce")
        missing = dt.isna() & raw.ne("")
        if missing.any():
            dt.loc[missing] = pd.to_datetime(
                raw.loc[missing],
                format="%d-%b-%Y",
                errors="coerce",
            )
        df[col] = dt.dt.strftime("%Y-%m-%d").where(dt.notna(), None)
    return df


def normalize_iso_timestamps(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    for col in cols:
        if col not in df.columns:
            continue
        raw = df[col].fillna("").astype(str).str.strip()
        dt = pd.to_datetime(raw, errors="coerce", utc=True)
        df[col] = dt.dt.strftime("%Y-%m-%dT%H:%M:%SZ").where(dt.notna(), None)
    return df


def resolve_user_agent(raw_user_agent: str) -> str:
    user_agent = (raw_user_agent or "").strip()
    lower_ua = user_agent.lower()
    if (
        not user_agent
        or user_agent == PLACEHOLDER_USER_AGENT
        or "example.com" in lower_ua
        or "your name" in lower_ua
    ):
        raise SystemExit(
            "Missing SEC User-Agent. Set --user-agent or SEC_USER_AGENT. "
            "Example: 'Jose L (josel@example.com)'."
        )
    return user_agent


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1].lower()
        for row in conn.execute(f"PRAGMA table_info({quote_ident(table_name)})").fetchall()
    }


def ensure_column(conn: sqlite3.Connection, table_name: str, col_name: str, decl: str) -> None:
    if col_name.lower() not in table_columns(conn, table_name):
        conn.execute(
            f"ALTER TABLE {quote_ident(table_name)} ADD COLUMN {quote_ident(col_name)} {decl}"
        )


def ensure_schema_columns(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "sec_ownership_submission", "issuer_name", "TEXT")
    ensure_column(conn, "sec_ownership_submission", "aff10b5one", "TEXT")
    ensure_column(conn, "sec_ownership_submission", "accepted_ts_utc", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "rptowner_relationship", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "rptowner_title", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "officer_title", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_director", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_officer", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_ten_percent_owner", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_other", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "other_text", "TEXT")


def assert_required_tables(conn: sqlite3.Connection) -> None:
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [name for name in REQUIRED_TABLES if name not in existing_tables]
    if missing:
        raise RuntimeError(
            "SQLite schema is missing required tables: "
            f"{', '.join(missing)}. Run helper_scripts/init_sqlite_db.py first."
        )


def insert_ignore_dataframe(
    conn: sqlite3.Connection,
    table_name: str,
    df: pd.DataFrame,
    to_sql_chunksize: int,
    where_clause: Optional[str] = None,
) -> tuple[int, int, int, int]:
    if df.empty:
        return (0, 0, 0, 0)

    stage_table = f"_stg_{table_name}"
    conn.execute(f"DROP TABLE IF EXISTS {quote_ident(stage_table)}")
    try:
        df.to_sql(
            stage_table,
            conn,
            if_exists="replace",
            index=False,
            method="multi",
            chunksize=max(1, int(to_sql_chunksize)),
        )

        cols = list(df.columns)
        col_csv = ", ".join(quote_ident(col) for col in cols)
        sql = (
            f"INSERT OR IGNORE INTO {quote_ident(table_name)} ({col_csv}) "
            f"SELECT {col_csv} FROM {quote_ident(stage_table)}"
        )
        if where_clause:
            sql = f"{sql} WHERE {where_clause}"

        conn.execute(sql)
        inserted_count = int(conn.execute("SELECT changes()").fetchone()[0])
        staged_count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(stage_table)}").fetchone()[0])
        if where_clause:
            eligible_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {quote_ident(stage_table)} WHERE {where_clause}"
                ).fetchone()[0]
            )
        else:
            eligible_count = staged_count
        filtered_out_count = max(staged_count - eligible_count, 0)
        return (staged_count, eligible_count, inserted_count, filtered_out_count)
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {quote_ident(stage_table)}")


def parse_dataset_year_quarter(label: str, href: str) -> tuple[int, int] | None:
    label_clean = " ".join((label or "").split())
    label_lower = label_clean.lower()
    href_lower = (href or "").lower()
    filename_lower = Path(href_lower).name

    # Keep parsing constrained to insider Form 3/4/5 datasets.
    if "345" not in label_lower and "345" not in href_lower and "form345" not in href_lower:
        return None

    patterns = (
        r"\b(20\d{2})\s*[-_/ ]?q([1-4])\b",
        r"\b(20\d{2})q([1-4])\b",
        r"\bq([1-4])\s*(20\d{2})\b",
    )
    for pat in patterns:
        m = re.search(pat, label_clean, flags=re.IGNORECASE)
        if m:
            if pat.startswith(r"\bq"):
                year = int(m.group(2))
                quarter = int(m.group(1))
            else:
                year = int(m.group(1))
                quarter = int(m.group(2))
            return (year, quarter)

    m = re.search(r"(20\d{2})\s*[-_/ ]?q([1-4])", filename_lower, flags=re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    return None


def discover_dataset_links(
    session: requests.Session,
    dataset_page_url: str,
    dataset_page_timeout_seconds: int,
) -> List[Tuple[str, int, int, str]]:
    r = session.get(dataset_page_url, timeout=dataset_page_timeout_seconds)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    links_by_dataset: Dict[str, Tuple[str, int, int, str]] = {}
    zip_links_seen = 0
    skipped_non_345 = 0
    skipped_unparsed = 0

    for a in soup.find_all("a", href=True):
        raw_href = a.get("href")
        if not isinstance(raw_href, str):
            continue
        href = urljoin(dataset_page_url, raw_href)
        if not href.lower().endswith(".zip"):
            continue
        zip_links_seen += 1

        label = " ".join(a.get_text(" ", strip=True).split())
        parsed = parse_dataset_year_quarter(label, href)
        if parsed is None:
            if "345" not in label.lower() and "345" not in href.lower():
                skipped_non_345 += 1
            else:
                skipped_unparsed += 1
            continue

        year, quarter = parsed
        dataset_id = f"{year}Q{quarter}"
        links_by_dataset[dataset_id] = (dataset_id, year, quarter, href)

    out = sorted(links_by_dataset.values(), key=lambda x: (x[1], x[2]))
    print(
        "[INFO] Dataset link discovery: "
        f"zip_links_seen={zip_links_seen}, matched={len(out)}, "
        f"skipped_non_345={skipped_non_345}, skipped_unparsed_345={skipped_unparsed}"
    )
    if not out:
        raise RuntimeError(
            "No SEC quarter ZIP links were discovered from the SEC dataset page."
        )
    return out


def select_datasets(
    all_links: List[Tuple[str, int, int, str]],
    start_year: Optional[int],
    end_year: Optional[int],
    start_quarter: Optional[int],
    end_quarter: Optional[int],
) -> List[Tuple[str, int, int, str]]:
    selected = []
    for dataset_id, year, quarter, href in all_links:
        if start_year is not None and year < start_year:
            continue
        if end_year is not None and year > end_year:
            continue
        if start_year is not None and year == start_year and start_quarter is not None and quarter < start_quarter:
            continue
        if end_year is not None and year == end_year and end_quarter is not None and quarter > end_quarter:
            continue
        selected.append((dataset_id, year, quarter, href))
    return selected


def upsert_manifest(
    conn: sqlite3.Connection,
    dataset_id: str,
    year: int,
    quarter: int,
    source_page: str,
    source_zip_url: str,
    local_zip_path: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sec_dataset_manifest (
            dataset_id, year, quarter, source_page, source_zip_url, local_zip_path,
            downloaded_at_utc, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dataset_id) DO UPDATE SET
            source_page=excluded.source_page,
            source_zip_url=excluded.source_zip_url,
            local_zip_path=excluded.local_zip_path,
            downloaded_at_utc=excluded.downloaded_at_utc,
            status=CASE
                WHEN sec_dataset_manifest.status = 'loaded' THEN sec_dataset_manifest.status
                ELSE 'downloaded'
            END
        """,
        (
            dataset_id,
            year,
            quarter,
            source_page,
            source_zip_url,
            local_zip_path,
            now_utc_iso(),
            "downloaded",
        ),
    )


def mark_loaded(conn: sqlite3.Connection, dataset_id: str) -> None:
    conn.execute(
        """
        UPDATE sec_dataset_manifest
        SET loaded_at_utc = ?, status = 'loaded'
        WHERE dataset_id = ?
        """,
        (now_utc_iso(), dataset_id),
    )


def download_file(
    session: requests.Session,
    url: str,
    out_path: Path,
    timeout_seconds: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, timeout=timeout_seconds, stream=True) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def load_one_dataset(
    conn: sqlite3.Connection,
    dataset_id: str,
    zip_path: Path,
    to_sql_chunksize: int,
) -> None:
    sub_needed = [
        "accession_number",
        "document_type",
        "filing_date",
        "accepted_ts_utc",
        "period_of_report",
        "date_of_original_submission",
        "issuer_cik",
        "issuer_name",
        "issuer_trading_symbol",
        "aff10b5one",
    ]
    ro_needed = [
        "accession_number",
        "rptowner_cik",
        "rptowner_name",
        "rptowner_relationship",
        "rptowner_title",
        "is_director",
        "is_officer",
        "is_ten_percent_owner",
        "is_other",
        "officer_title",
        "other_text",
    ]
    nd_needed = [
        "accession_number",
        "nonderiv_trans_sk",
        "security_title",
        "transaction_date",
        "transaction_code",
        "transaction_shares",
        "transaction_price_per_share",
        "transaction_acquired_disposed_code",
        "shares_owned_following_transaction",
        "direct_or_indirect_ownership",
        "nature_of_ownership",
    ]

    submission_df = read_tsv_from_zip(zip_path, "submission")
    submission_df = find_best_columns(submission_df, sub_needed)
    submission_df = normalize_iso_dates(
        submission_df,
        ["filing_date", "period_of_report", "date_of_original_submission"],
    )
    submission_df = normalize_iso_timestamps(submission_df, ["accepted_ts_utc"])
    submission_df["source_dataset_id"] = dataset_id
    sub_stats = insert_ignore_dataframe(
        conn,
        "sec_ownership_submission",
        submission_df,
        to_sql_chunksize=to_sql_chunksize,
    )
    del submission_df

    reporting_owner_df = read_tsv_from_zip(zip_path, "reportingowner")
    reporting_owner_df = find_best_columns(reporting_owner_df, ro_needed)
    reporting_owner_df["source_dataset_id"] = dataset_id
    ro_stats = insert_ignore_dataframe(
        conn,
        "sec_ownership_reporting_owner",
        reporting_owner_df,
        to_sql_chunksize=to_sql_chunksize,
        where_clause="accession_number IN (SELECT accession_number FROM sec_ownership_submission)",
    )
    del reporting_owner_df

    nonderiv_df = read_tsv_from_zip(zip_path, "nonderiv")
    nonderiv_df = find_best_columns(nonderiv_df, nd_needed)
    nonderiv_df = normalize_iso_dates(nonderiv_df, ["transaction_date"])
    nonderiv_df["source_dataset_id"] = dataset_id
    nonderiv_df = coerce_numeric(
        nonderiv_df,
        ["transaction_shares", "transaction_price_per_share", "shares_owned_following_transaction"],
    )
    nd_stats = insert_ignore_dataframe(
        conn,
        "sec_ownership_nonderiv_trans",
        nonderiv_df,
        to_sql_chunksize=to_sql_chunksize,
        where_clause="accession_number IN (SELECT accession_number FROM sec_ownership_submission)",
    )
    del nonderiv_df

    def fmt_stats(name: str, stats: tuple[int, int, int, int]) -> str:
        staged, eligible, inserted, filtered = stats
        ignored = max(eligible - inserted, 0)
        return (
            f"{name}: staged={staged:,} eligible={eligible:,} "
            f"inserted={inserted:,} ignored_existing={ignored:,} filtered_orphan={filtered:,}"
        )

    print(f"[INFO] {dataset_id} load stats | {fmt_stats('submission', sub_stats)}")
    print(f"[INFO] {dataset_id} load stats | {fmt_stats('reporting_owner', ro_stats)}")
    print(f"[INFO] {dataset_id} load stats | {fmt_stats('nonderiv', nd_stats)}")

    ro_filtered = ro_stats[3]
    nd_filtered = nd_stats[3]
    if ro_filtered > 0 or nd_filtered > 0:
        print(
            "[WARN] "
            f"{dataset_id}: filtered rows due to missing submission accession "
            f"(reporting_owner={ro_filtered:,}, nonderiv={nd_filtered:,})."
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None, help="Path to SEC Form 4 YAML config.")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--start-quarter", type=int, choices=[1, 2, 3, 4], default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--end-quarter", type=int, choices=[1, 2, 3, 4], default=None)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--raw-zip-dir", type=Path, default=None)
    parser.add_argument("--user-agent", type=str, default=None)
    parser.add_argument("--sleep-seconds", type=float, default=None)
    args = parser.parse_args()

    _, cfg = load_sec_form4_config(args.config)
    quarterly_cfg = cfg_get(cfg, "quarterly", default={})
    request_cfg = cfg_get(cfg, "request", default={})

    start_year = (
        args.start_year
        if args.start_year is not None
        else cfg_get(quarterly_cfg, "start_year", default=None)
    )
    start_quarter = (
        args.start_quarter
        if args.start_quarter is not None
        else cfg_get(quarterly_cfg, "start_quarter", default=None)
    )
    end_year = (
        args.end_year
        if args.end_year is not None
        else cfg_get(quarterly_cfg, "end_year", default=None)
    )
    end_quarter = (
        args.end_quarter
        if args.end_quarter is not None
        else cfg_get(quarterly_cfg, "end_quarter", default=None)
    )
    sleep_seconds = float(
        args.sleep_seconds
        if args.sleep_seconds is not None
        else cfg_get(quarterly_cfg, "sleep_seconds", default=0.5)
    )
    dataset_page_url = str(
        cfg_get(quarterly_cfg, "dataset_page_url", default=SEC_DATASET_PAGE_DEFAULT)
    )
    dataset_page_timeout_seconds = int(
        cfg_get(
            quarterly_cfg,
            "dataset_page_timeout_seconds",
            default=cfg_get(request_cfg, "timeout_seconds", default=REQUEST_TIMEOUT_DEFAULT),
        )
    )
    download_timeout_seconds = int(
        cfg_get(
            quarterly_cfg,
            "download_timeout_seconds",
            default=REQUEST_DOWNLOAD_TIMEOUT_DEFAULT,
        )
    )
    to_sql_chunksize = int(
        cfg_get(quarterly_cfg, "to_sql_chunksize", default=TO_SQL_CHUNKSIZE_DEFAULT)
    )
    sec_accept_encoding = str(
        cfg_get(request_cfg, "accept_encoding", default=SEC_ACCEPT_ENCODING_DEFAULT)
    )
    sec_host_header = str(cfg_get(request_cfg, "host_header", default=SEC_HOST_HEADER_DEFAULT))

    if any(v is None for v in (start_year, start_quarter, end_year, end_quarter)):
        raise SystemExit(
            "Missing quarterly range. Provide CLI args or set "
            "sec_form4.quarterly.start_year/start_quarter/end_year/end_quarter in config."
        )

    db_path = Path(
        args.db_path
        if args.db_path is not None
        else cfg_get(cfg, "db_path", default=str(default_db_path()))
    )
    raw_zip_dir = Path(
        args.raw_zip_dir
        if args.raw_zip_dir is not None
        else cfg_get(cfg, "raw_zip_dir", default=str(default_raw_zip_dir()))
    )
    user_agent_raw = (
        args.user_agent
        if args.user_agent is not None
        else cfg_get(cfg, "user_agent", default=os.getenv("SEC_USER_AGENT", SEC_USER_AGENT))
    )
    user_agent = resolve_user_agent(str(user_agent_raw))

    raw_zip_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    request_headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": sec_accept_encoding,
        "Host": sec_host_header,
    }
    session = requests.Session()
    session.headers.update(request_headers)
    links = discover_dataset_links(
        session,
        dataset_page_url=dataset_page_url,
        dataset_page_timeout_seconds=dataset_page_timeout_seconds,
    )
    selected = select_datasets(
        links,
        int(start_year),
        int(end_year),
        int(start_quarter),
        int(end_quarter),
    )

    if not selected:
        raise SystemExit("No datasets matched your requested year/quarter range.")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        assert_required_tables(conn)
        ensure_schema_columns(conn)

        for dataset_id, year, quarter, href in selected:
            local_zip = raw_zip_dir / f"{dataset_id}_form345.zip"
            print(f"[INFO] {dataset_id}: {href}")

            if not local_zip.exists():
                download_file(
                    session,
                    href,
                    local_zip,
                    timeout_seconds=download_timeout_seconds,
                )
                time.sleep(sleep_seconds)

            upsert_manifest(
                conn,
                dataset_id,
                year,
                quarter,
                source_page=dataset_page_url,
                source_zip_url=href,
                local_zip_path=str(local_zip),
            )
            conn.commit()

            print(f"[INFO] Loading {local_zip.name} into SQLite...")
            load_one_dataset(
                conn,
                dataset_id,
                local_zip,
                to_sql_chunksize=to_sql_chunksize,
            )
            mark_loaded(conn, dataset_id)
            conn.commit()

        print("[DONE] SEC quarterly ownership data loaded.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
