#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from sec_form4_config import cfg_get, load_sec_form4_config

DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\sec_insider.sqlite")
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"
PLACEHOLDER_USER_AGENT = "Your Name your_email@example.com"

FORM_TYPES_DEFAULT = {"4", "4/A"}
BASE_ARCHIVES_URL_DEFAULT = "https://www.sec.gov/Archives"
REQUEST_TIMEOUT_DEFAULT = 60
REQUEST_MAX_RETRIES_DEFAULT = 4
RETRY_BACKOFF_BASE_DEFAULT = 1.5
RETRY_BACKOFF_CAP_DEFAULT = 60.0
SEC_HOST_HEADER_DEFAULT = "www.sec.gov"
SEC_ACCEPT_ENCODING_DEFAULT = "gzip, deflate"


def default_db_path() -> Path:
    return Path(os.getenv("SEC_INSIDER_DB_PATH", str(DEFAULT_DB_PATH)))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quarter_for(d: date) -> int:
    return ((d.month - 1) // 3) + 1


def iter_weekdays(start: date, end: date):
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            yield cur
        cur += timedelta(days=1)


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
            "Example: 'JL, Independent Research, jm.357@hotmail.com'."
        )
    return user_agent


def parse_date_strict(raw_value: str) -> date:
    value = raw_value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unsupported date format: {raw_value!r}")


def normalize_sec_date(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    if value == "":
        return None
    try:
        return parse_date_strict(value).isoformat()
    except ValueError:
        return None


def normalize_form_types(raw_form_types: object, default: set[str]) -> set[str]:
    if raw_form_types is None:
        return set(default)
    if isinstance(raw_form_types, str):
        items = [part.strip() for part in raw_form_types.split(",")]
    elif isinstance(raw_form_types, (list, tuple, set)):
        items = [str(part).strip() for part in raw_form_types]
    else:
        items = [str(raw_form_types).strip()]
    out = {item.upper() for item in items if item}
    return out or set(default)


def normalize_int_set(raw_values: object, default: set[int]) -> set[int]:
    if raw_values is None:
        return set(default)
    if isinstance(raw_values, str):
        tokens = [part.strip() for part in raw_values.split(",")]
    elif isinstance(raw_values, (list, tuple, set)):
        tokens = [str(part).strip() for part in raw_values]
    else:
        tokens = [str(raw_values).strip()]
    out: set[int] = set()
    for tok in tokens:
        if tok == "":
            continue
        try:
            out.add(int(tok))
        except ValueError:
            continue
    return out or set(default)


def optional_positive_int(raw_value: object, *, default: int = 0, name: str = "value") -> int:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        raise SystemExit(f"{name} must be an integer >= 0, got {raw_value!r}")
    try:
        value = int(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must be an integer >= 0, got {raw_value!r}") from exc
    if value < 0:
        raise SystemExit(f"{name} must be >= 0, got {raw_value!r}")
    return value


def optional_positive_float(raw_value: object, *, default: float = 0.0, name: str = "value") -> float:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        raise SystemExit(f"{name} must be a number >= 0, got {raw_value!r}")
    try:
        value = float(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must be a number >= 0, got {raw_value!r}") from exc
    if value < 0:
        raise SystemExit(f"{name} must be >= 0, got {raw_value!r}")
    return value


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1].lower()
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def ensure_column(conn: sqlite3.Connection, table_name: str, col_name: str, decl: str) -> None:
    if col_name.lower() not in table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {decl}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    required = {
        "sec_dataset_manifest",
        "sec_ownership_submission",
        "sec_ownership_reporting_owner",
        "sec_ownership_nonderiv_trans",
    }
    existing = {
        row[0].lower()
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            "Missing required raw SEC tables: "
            + ", ".join(missing)
            + ". Run your SQLite init + quarterly ingest first."
        )

    # Use explicit statements inside a SAVEPOINT to avoid executescript() implicit COMMIT behavior.
    conn.execute("SAVEPOINT ensure_schema")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_form4_daily_ingest_log (
                accession_number    TEXT PRIMARY KEY,
                form_type           TEXT NOT NULL,
                cik                 TEXT,
                filing_date         TEXT NOT NULL,
                filing_url          TEXT NOT NULL,
                status              TEXT NOT NULL,
                last_attempted_utc  TEXT NOT NULL,
                loaded_utc          TEXT,
                error_text          TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sec_form4_daily_ingest_log_filing_date
                ON sec_form4_daily_ingest_log(filing_date)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_form4_daily_state (
                process_name        TEXT PRIMARY KEY,
                last_index_date     TEXT,
                updated_at_utc      TEXT NOT NULL
            )
            """
        )

        # Make your schema tolerant to both historical-quarterly and daily-XML styles.
        ensure_column(conn, "sec_ownership_submission", "aff10b5one", "TEXT")
        ensure_column(conn, "sec_ownership_submission", "issuer_name", "TEXT")
        ensure_column(conn, "sec_ownership_submission", "accepted_ts_utc", "TEXT")

        ensure_column(conn, "sec_ownership_reporting_owner", "rptowner_relationship", "TEXT")
        ensure_column(conn, "sec_ownership_reporting_owner", "rptowner_title", "TEXT")
        ensure_column(conn, "sec_ownership_reporting_owner", "is_director", "TEXT")
        ensure_column(conn, "sec_ownership_reporting_owner", "is_officer", "TEXT")
        ensure_column(conn, "sec_ownership_reporting_owner", "is_ten_percent_owner", "TEXT")
        ensure_column(conn, "sec_ownership_reporting_owner", "is_other", "TEXT")
        ensure_column(conn, "sec_ownership_reporting_owner", "officer_title", "TEXT")
        ensure_column(conn, "sec_ownership_reporting_owner", "other_text", "TEXT")

        ensure_column(conn, "sec_ownership_nonderiv_trans", "trans_timeliness", "TEXT")
        conn.execute("RELEASE SAVEPOINT ensure_schema")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ensure_schema")
        conn.execute("RELEASE SAVEPOINT ensure_schema")
        raise


def get_state_date(conn: sqlite3.Connection, process_name: str) -> date | None:
    row = conn.execute(
        """
        SELECT last_index_date
        FROM sec_form4_daily_state
        WHERE process_name = ?
        """
    , (process_name,)).fetchone()
    if not row or not row[0]:
        return None
    return datetime.strptime(row[0], "%Y-%m-%d").date()


def set_state_date(conn: sqlite3.Connection, process_name: str, dt: date) -> None:
    conn.execute(
        """
        INSERT INTO sec_form4_daily_state(process_name, last_index_date, updated_at_utc)
        VALUES(?, ?, ?)
        ON CONFLICT(process_name) DO UPDATE SET
            last_index_date = excluded.last_index_date,
            updated_at_utc = excluded.updated_at_utc
        """,
        (process_name, dt.isoformat(), utc_now_iso()),
    )


def daily_form_index_url(dt: date, archives_base_url: str) -> str:
    return (
        f"{archives_base_url}/edgar/daily-index/{dt.year}/QTR{quarter_for(dt)}"
        f"/form.{dt.strftime('%Y%m%d')}.idx"
    )


def current_quarter_full_index_url(dt: date, archives_base_url: str) -> str:
    return f"{archives_base_url}/edgar/full-index/{dt.year}/QTR{quarter_for(dt)}/form.idx"


def _retry_delay_seconds(
    resp: requests.Response,
    attempt: int,
    retry_backoff_base_seconds: float,
    retry_backoff_cap_seconds: float,
) -> float:
    retry_after = (resp.headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        return max(1.0, float(retry_after))
    # Exponential fallback with cap.
    return min(retry_backoff_cap_seconds, retry_backoff_base_seconds * (2**attempt))


def fetch_text(
    session: requests.Session,
    url: str,
    timeout: int = 60,
    missing_statuses: set[int] | None = None,
    max_retries: int = 5,
    retry_backoff_base_seconds: float = RETRY_BACKOFF_BASE_DEFAULT,
    retry_backoff_cap_seconds: float = RETRY_BACKOFF_CAP_DEFAULT,
) -> str | None:
    missing = missing_statuses or {404}
    for attempt in range(max_retries):
        r = session.get(url, timeout=timeout)
        if r.status_code in missing:
            return None
        if r.status_code in {429, 500, 502, 503, 504} and attempt < (max_retries - 1):
            time.sleep(
                _retry_delay_seconds(
                    r,
                    attempt,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_cap_seconds=retry_backoff_cap_seconds,
                )
            )
            continue
        r.raise_for_status()
        return r.text
    return None


def parse_form_index(idx_text: str) -> list[dict[str, str]]:
    lines = idx_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("-----"):
            start = i + 1
            break
    if start is None:
        return []

    rows: list[dict[str, str]] = []
    for line in lines[start:]:
        line = line.rstrip()
        if not line:
            continue

        filename_start = line.find("edgar/data/")
        if filename_start < 0:
            continue
        filename = line[filename_start:].strip()
        left = line[:filename_start].rstrip()

        m_date = re.search(r"(\d{4}-\d{2}-\d{2}|\d{8})\s*$", left)
        if not m_date:
            continue
        raw_filing_date = m_date.group(1)
        left = left[:m_date.start()].rstrip()

        m_cik = re.search(r"(\d{1,12})\s*$", left)
        if not m_cik:
            continue
        cik = m_cik.group(1)
        left = left[:m_cik.start()].rstrip()

        if not left:
            continue
        parts = re.split(r"\s{2,}", left, maxsplit=1)
        if len(parts) == 2:
            form_type, company_name = parts[0].strip(), parts[1].strip()
        else:
            toks = left.split(None, 1)
            if not toks:
                continue
            form_type = toks[0].strip()
            company_name = toks[1].strip() if len(toks) > 1 else ""

        try:
            filing_dt = parse_date_strict(raw_filing_date)
        except ValueError:
            continue

        rows.append(
            {
                "form_type": form_type,
                "company_name": company_name,
                "cik": cik,
                "filing_date": filing_dt.isoformat(),
                "filing_date_iso": filing_dt.isoformat(),
                "source_dataset_id": f"daily:{filing_dt.isoformat()}",
                "filename": filename,
            }
        )
    return rows


def accession_from_filename(filename: str) -> str:
    return Path(filename).name.rsplit(".", 1)[0]


def xml_block_from_submission_txt(submission_txt: str) -> str | None:
    patterns = [
        r"(?is)<XML>\s*(<ownershipDocument\b.*?</ownershipDocument>)\s*</XML>",
        r"(?is)(<ownershipDocument\b.*?</ownershipDocument>)",
    ]
    for pattern in patterns:
        m = re.search(pattern, submission_txt)
        if m:
            return m.group(1).strip()
    return None


def extract_acceptance_ts_utc(submission_txt: str) -> str | None:
    patterns = (
        r"(?im)^\s*ACCEPTANCE-DATETIME:\s*(\d{14})\s*$",
        r"(?is)<ACCEPTANCE-DATETIME>\s*(\d{14})\s*</ACCEPTANCE-DATETIME>",
    )
    raw_value: str | None = None
    for pattern in patterns:
        m = re.search(pattern, submission_txt)
        if m:
            raw_value = m.group(1)
            break
    if not raw_value:
        return None
    try:
        dt_local = datetime.strptime(raw_value, "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    except Exception:
        return None
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_text(node: ET.Element | None, *paths: str) -> str | None:
    if node is None:
        return None
    for path in paths:
        found = node.find(path)
        if found is not None and found.text is not None:
            value = found.text.strip()
            if value != "":
                return value
    return None


def bool_text(value: str | None) -> str:
    if value is None:
        return "0"
    return "1" if value.strip().upper() in {"1", "Y", "YES", "TRUE", "T"} else "0"


def to_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def build_relationship_label(
    is_director: str,
    is_officer: str,
    is_ten_percent_owner: str,
    is_other: str,
) -> str:
    parts: list[str] = []
    if is_officer == "1":
        parts.append("OFFICER")
    if is_director == "1":
        parts.append("DIRECTOR")
    if is_ten_percent_owner == "1":
        parts.append("TENPERCENTOWNER")
    if is_other == "1":
        parts.append("OTHER")
    return ";".join(parts) if parts else "OTHER"


def parse_form4_xml(
    accession_number: str,
    form_type: str,
    filing_date: str,
    source_dataset_id: str,
    company_name_from_index: str,
    accepted_ts_utc: str | None,
    xml_text: str,
) -> tuple[dict, list[dict], list[dict]]:
    root = ET.fromstring(xml_text)

    issuer_name = find_text(root, "./issuer/issuerName") or company_name_from_index or None
    sub = {
        "accession_number": accession_number,
        "document_type": find_text(root, "./documentType") or form_type,
        "filing_date": filing_date,
        "accepted_ts_utc": accepted_ts_utc,
        "period_of_report": normalize_sec_date(find_text(root, "./periodOfReport")),
        "date_of_original_submission": normalize_sec_date(find_text(root, "./dateOfOriginalSubmission")),
        "issuer_cik": find_text(root, "./issuer/issuerCik"),
        "issuer_name": issuer_name,
        "issuer_trading_symbol": find_text(root, "./issuer/issuerTradingSymbol"),
        "aff10b5one": find_text(root, "./aff10b5One"),
        "source_dataset_id": source_dataset_id,
    }

    owners: list[dict] = []
    for ro in root.findall("./reportingOwner"):
        is_director = bool_text(find_text(ro, "./reportingOwnerRelationship/isDirector"))
        is_officer = bool_text(find_text(ro, "./reportingOwnerRelationship/isOfficer"))
        is_ten_percent_owner = bool_text(find_text(ro, "./reportingOwnerRelationship/isTenPercentOwner"))
        is_other = bool_text(find_text(ro, "./reportingOwnerRelationship/isOther"))
        officer_title = find_text(ro, "./reportingOwnerRelationship/officerTitle")
        other_text = find_text(ro, "./reportingOwnerRelationship/otherText")

        row = {
            "accession_number": accession_number,
            "rptowner_cik": find_text(ro, "./reportingOwnerId/rptOwnerCik"),
            "rptowner_name": find_text(ro, "./reportingOwnerId/rptOwnerName"),
            "rptowner_relationship": build_relationship_label(
                is_director=is_director,
                is_officer=is_officer,
                is_ten_percent_owner=is_ten_percent_owner,
                is_other=is_other,
            ),
            "rptowner_title": officer_title,
            "is_director": is_director,
            "is_officer": is_officer,
            "is_ten_percent_owner": is_ten_percent_owner,
            "is_other": is_other,
            "officer_title": officer_title,
            "other_text": other_text,
            "source_dataset_id": source_dataset_id,
        }
        if row["rptowner_cik"]:
            owners.append(row)

    trans_rows: list[dict] = []
    for i, nd in enumerate(root.findall("./nonDerivativeTable/nonDerivativeTransaction"), start=1):
        trans_rows.append(
            {
                "accession_number": accession_number,
                "nonderiv_trans_sk": str(i),
                "security_title": find_text(nd, "./securityTitle/value"),
                "transaction_date": normalize_sec_date(find_text(nd, "./transactionDate/value")),
                "transaction_code": find_text(nd, "./transactionCoding/transactionCode"),
                "transaction_shares": to_float(find_text(nd, "./transactionAmounts/transactionShares/value")),
                "transaction_price_per_share": to_float(find_text(nd, "./transactionAmounts/transactionPricePerShare/value")),
                "transaction_acquired_disposed_code": find_text(
                    nd, "./transactionAmounts/transactionAcquiredDisposedCode/value"
                ),
                "shares_owned_following_transaction": to_float(
                    find_text(nd, "./postTransactionAmounts/sharesOwnedFollowingTransaction/value")
                ),
                "direct_or_indirect_ownership": find_text(
                    nd, "./ownershipNature/directOrIndirectOwnership/value"
                ),
                "nature_of_ownership": find_text(nd, "./ownershipNature/natureOfOwnership/value"),
                "trans_timeliness": find_text(
                    nd,
                    "./transactionCoding/transactionTimeliness/value",
                    "./transactionTimeliness/value",
                ),
                "source_dataset_id": source_dataset_id,
            }
        )

    return sub, owners, trans_rows


def upsert_submission(conn: sqlite3.Connection, sub: dict) -> None:
    conn.execute(
        """
        INSERT INTO sec_ownership_submission (
            accession_number,
            document_type,
            filing_date,
            accepted_ts_utc,
            period_of_report,
            date_of_original_submission,
            issuer_cik,
            issuer_name,
            issuer_trading_symbol,
            aff10b5one,
            source_dataset_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_number) DO UPDATE SET
            document_type = excluded.document_type,
            filing_date = excluded.filing_date,
            accepted_ts_utc = excluded.accepted_ts_utc,
            period_of_report = excluded.period_of_report,
            date_of_original_submission = excluded.date_of_original_submission,
            issuer_cik = excluded.issuer_cik,
            issuer_name = excluded.issuer_name,
            issuer_trading_symbol = excluded.issuer_trading_symbol,
            aff10b5one = excluded.aff10b5one,
            source_dataset_id = excluded.source_dataset_id
        """,
        (
            sub["accession_number"],
            sub["document_type"],
            sub["filing_date"],
            sub["accepted_ts_utc"],
            sub["period_of_report"],
            sub["date_of_original_submission"],
            sub["issuer_cik"],
            sub["issuer_name"],
            sub["issuer_trading_symbol"],
            sub["aff10b5one"],
            sub["source_dataset_id"],
        ),
    )


def upsert_reporting_owner(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO sec_ownership_reporting_owner (
            accession_number,
            rptowner_cik,
            rptowner_name,
            rptowner_relationship,
            rptowner_title,
            is_director,
            is_officer,
            is_ten_percent_owner,
            is_other,
            officer_title,
            other_text,
            source_dataset_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_number, rptowner_cik) DO UPDATE SET
            rptowner_name = excluded.rptowner_name,
            rptowner_relationship = excluded.rptowner_relationship,
            rptowner_title = excluded.rptowner_title,
            is_director = excluded.is_director,
            is_officer = excluded.is_officer,
            is_ten_percent_owner = excluded.is_ten_percent_owner,
            is_other = excluded.is_other,
            officer_title = excluded.officer_title,
            other_text = excluded.other_text,
            source_dataset_id = excluded.source_dataset_id
        """,
        (
            row["accession_number"],
            row["rptowner_cik"],
            row["rptowner_name"],
            row["rptowner_relationship"],
            row["rptowner_title"],
            row["is_director"],
            row["is_officer"],
            row["is_ten_percent_owner"],
            row["is_other"],
            row["officer_title"],
            row["other_text"],
            row["source_dataset_id"],
        ),
    )


def upsert_nonderiv_trans(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO sec_ownership_nonderiv_trans (
            accession_number,
            nonderiv_trans_sk,
            security_title,
            transaction_date,
            transaction_code,
            transaction_shares,
            transaction_price_per_share,
            transaction_acquired_disposed_code,
            shares_owned_following_transaction,
            direct_or_indirect_ownership,
            nature_of_ownership,
            trans_timeliness,
            source_dataset_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_number, nonderiv_trans_sk) DO UPDATE SET
            security_title = excluded.security_title,
            transaction_date = excluded.transaction_date,
            transaction_code = excluded.transaction_code,
            transaction_shares = excluded.transaction_shares,
            transaction_price_per_share = excluded.transaction_price_per_share,
            transaction_acquired_disposed_code = excluded.transaction_acquired_disposed_code,
            shares_owned_following_transaction = excluded.shares_owned_following_transaction,
            direct_or_indirect_ownership = excluded.direct_or_indirect_ownership,
            nature_of_ownership = excluded.nature_of_ownership,
            trans_timeliness = excluded.trans_timeliness,
            source_dataset_id = excluded.source_dataset_id
        """,
        (
            row["accession_number"],
            row["nonderiv_trans_sk"],
            row["security_title"],
            row["transaction_date"],
            row["transaction_code"],
            row["transaction_shares"],
            row["transaction_price_per_share"],
            row["transaction_acquired_disposed_code"],
            row["shares_owned_following_transaction"],
            row["direct_or_indirect_ownership"],
            row["nature_of_ownership"],
            row["trans_timeliness"],
            row["source_dataset_id"],
        ),
    )


def mark_log(
    conn: sqlite3.Connection,
    accession_number: str,
    form_type: str,
    cik: str,
    filing_date: str,
    filing_url: str,
    status: str,
    error_text: str | None = None,
    loaded: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO sec_form4_daily_ingest_log (
            accession_number, form_type, cik, filing_date, filing_url,
            status, last_attempted_utc, loaded_utc, error_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_number) DO UPDATE SET
            form_type = excluded.form_type,
            cik = excluded.cik,
            filing_date = excluded.filing_date,
            filing_url = excluded.filing_url,
            status = excluded.status,
            last_attempted_utc = excluded.last_attempted_utc,
            loaded_utc = excluded.loaded_utc,
            error_text = excluded.error_text
        """,
        (
            accession_number,
            form_type,
            cik,
            filing_date,
            filing_url,
            status,
            utc_now_iso(),
            utc_now_iso() if loaded else None,
            error_text,
        ),
    )


def already_loaded(conn: sqlite3.Connection, accession_number: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sec_form4_daily_ingest_log
        WHERE accession_number = ?
          AND status = 'loaded'
        """,
        (accession_number,),
    ).fetchone()
    return row is not None


def upsert_daily_manifest_row(
    conn: sqlite3.Connection,
    source_dataset_id: str,
    filing_date_iso: str,
    filing_url: str,
    archives_base_url: str,
) -> None:
    filing_dt = parse_date_strict(filing_date_iso)
    conn.execute(
        """
        INSERT INTO sec_dataset_manifest (
            dataset_id, year, quarter, source_page, source_zip_url, local_zip_path,
            downloaded_at_utc, loaded_at_utc, status, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dataset_id) DO UPDATE SET
            source_page = excluded.source_page,
            source_zip_url = excluded.source_zip_url,
            loaded_at_utc = excluded.loaded_at_utc,
            status = excluded.status,
            notes = excluded.notes
        """,
        (
            source_dataset_id,
            filing_dt.year,
            quarter_for(filing_dt),
            f"{archives_base_url}/edgar/daily-index",
            filing_url,
            None,
            utc_now_iso(),
            utc_now_iso(),
            "loaded",
            "Daily incremental Form 4/4-A ingestion from SEC daily/full form index.",
        ),
    )


def process_rows(
    conn: sqlite3.Connection,
    session: requests.Session,
    rows: list[dict[str, str]],
    form_types: set[str],
    archives_base_url: str,
    sleep_seconds: float,
    filing_timeout_seconds: int,
    filing_max_retries: int,
    filing_missing_statuses: set[int],
    retry_backoff_base_seconds: float,
    retry_backoff_cap_seconds: float,
    force_reprocess: bool,
    max_filings: int = 0,
    progress_every_filings: int = 100,
    progress_interval_sec: float = 60.0,
    stop_deadline: float | None = None,
    stage_label: str = "daily",
) -> tuple[int, int, bool]:
    seen = 0
    loaded = 0
    limit_hit = False
    manifest_seen: set[str] = set()
    started = time.monotonic()
    last_progress_at = started
    last_progress_seen = 0

    def maybe_progress(*, force: bool = False) -> None:
        nonlocal last_progress_at, last_progress_seen
        now = time.monotonic()
        by_count = progress_every_filings > 0 and seen - last_progress_seen >= progress_every_filings
        by_time = progress_interval_sec > 0 and now - last_progress_at >= progress_interval_sec
        if not force and not by_count and not by_time:
            return
        print(
            "[PROGRESS] "
            f"stage={stage_label} seen={seen:,} loaded={loaded:,} "
            f"elapsed_sec={now - started:.1f}"
        )
        last_progress_at = now
        last_progress_seen = seen

    for row in rows:
        form_type = row["form_type"].upper()
        if form_type not in form_types:
            continue

        accession_number = accession_from_filename(row["filename"])
        if (not force_reprocess) and already_loaded(conn, accession_number):
            continue

        if max_filings > 0 and seen >= max_filings:
            limit_hit = True
            print(
                f"[LIMIT] stage={stage_label} max_filings={max_filings:,} reached; "
                "leaving remaining pending rows for a later run."
            )
            break
        if stop_deadline is not None and time.monotonic() >= stop_deadline:
            limit_hit = True
            print(
                f"[LIMIT] stage={stage_label} stop_after_sec reached; "
                "leaving remaining pending rows for a later run."
            )
            break

        filing_url = f"{archives_base_url}/{row['filename']}"
        source_dataset_id = row["source_dataset_id"]
        seen += 1

        try:
            if source_dataset_id not in manifest_seen:
                upsert_daily_manifest_row(
                    conn=conn,
                    source_dataset_id=source_dataset_id,
                    filing_date_iso=row["filing_date_iso"],
                    filing_url=filing_url,
                    archives_base_url=archives_base_url,
                )
                manifest_seen.add(source_dataset_id)

            submission_txt = fetch_text(
                session,
                filing_url,
                timeout=filing_timeout_seconds,
                missing_statuses=filing_missing_statuses,
                max_retries=filing_max_retries,
                retry_backoff_base_seconds=retry_backoff_base_seconds,
                retry_backoff_cap_seconds=retry_backoff_cap_seconds,
            )
            if not submission_txt:
                raise RuntimeError("Filing .txt not found")

            accepted_ts_utc = extract_acceptance_ts_utc(submission_txt)

            ownership_xml = xml_block_from_submission_txt(submission_txt)
            if not ownership_xml:
                raise RuntimeError("Could not locate ownershipDocument XML in filing text")

            sub, owners, trans_rows = parse_form4_xml(
                accession_number=accession_number,
                form_type=form_type,
                filing_date=row["filing_date"],
                source_dataset_id=source_dataset_id,
                company_name_from_index=row["company_name"],
                accepted_ts_utc=accepted_ts_utc,
                xml_text=ownership_xml,
            )

            upsert_submission(conn, sub)
            # Replace child rows by accession to keep current truth on reprocess.
            conn.execute(
                "DELETE FROM sec_ownership_reporting_owner WHERE accession_number = ?",
                (accession_number,),
            )
            conn.execute(
                "DELETE FROM sec_ownership_nonderiv_trans WHERE accession_number = ?",
                (accession_number,),
            )
            for owner in owners:
                upsert_reporting_owner(conn, owner)
            for trans in trans_rows:
                upsert_nonderiv_trans(conn, trans)

            mark_log(
                conn=conn,
                accession_number=accession_number,
                form_type=form_type,
                cik=row["cik"],
                filing_date=row["filing_date"],
                filing_url=filing_url,
                status="loaded",
                loaded=True,
            )
            conn.commit()
            loaded += 1
        except Exception as exc:
            mark_log(
                conn=conn,
                accession_number=accession_number,
                form_type=form_type,
                cik=row["cik"],
                filing_date=row["filing_date"],
                filing_url=filing_url,
                status="error",
                error_text=str(exc)[:4000],
                loaded=False,
            )
            conn.commit()

        maybe_progress()
        time.sleep(max(sleep_seconds, 0.0))

    maybe_progress(force=True)
    return seen, loaded, limit_hit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None, help="Path to SEC Form 4 YAML config.")
    parser.add_argument("--mode", type=str, choices=["daily", "weekly"], default=None)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--start-date", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--user-agent",
        type=str,
        default=None,
        help="SEC identifying User-Agent header.",
    )
    parser.add_argument("--days-back", type=int, default=None)
    parser.add_argument("--sleep-seconds", type=float, default=None)
    parser.add_argument("--max-index-days", type=int, default=None, help="Maximum daily indexes to attempt; 0 means no cap.")
    parser.add_argument("--max-filings", type=int, default=None, help="Maximum eligible daily-index filings to attempt; 0 means no cap.")
    parser.add_argument(
        "--max-reconcile-filings",
        type=int,
        default=None,
        help="Maximum eligible current-quarter reconciliation filings to attempt; 0 means no cap.",
    )
    parser.add_argument(
        "--progress-every-filings",
        type=int,
        default=None,
        help="Emit progress after this many eligible filings; 0 disables count-based progress.",
    )
    parser.add_argument(
        "--progress-interval-sec",
        type=float,
        default=None,
        help="Emit progress at least this often while processing filings; 0 disables time-based progress.",
    )
    parser.add_argument(
        "--stop-after-sec",
        type=float,
        default=None,
        help="Stop after this many seconds and leave remaining work for the next run; 0 disables.",
    )
    parser.add_argument(
        "--force-reprocess",
        dest="force_reprocess",
        action="store_true",
        help="Reprocess filings even if they were already marked loaded.",
    )
    parser.add_argument(
        "--no-force-reprocess",
        dest="force_reprocess",
        action="store_false",
        help="Disable forced reprocessing even if enabled in config.",
    )
    parser.add_argument(
        "--reconcile-current-quarter",
        dest="reconcile_current_quarter",
        action="store_true",
        help="Also re-read the current-quarter full index and upsert all Form 4 / 4-A filings it lists.",
    )
    parser.add_argument(
        "--no-reconcile-current-quarter",
        dest="reconcile_current_quarter",
        action="store_false",
        help="Disable current-quarter reconciliation even if enabled in config.",
    )
    parser.set_defaults(reconcile_current_quarter=None, force_reprocess=None)
    args = parser.parse_args()

    _, cfg = load_sec_form4_config(args.config)
    mode = (args.mode or str(cfg_get(cfg, "run_mode", default="daily"))).lower()
    if mode not in {"daily", "weekly"}:
        raise SystemExit(f"Unsupported mode={mode!r}. Use daily or weekly.")
    mode_key = "weekly" if mode == "weekly" else "daily"
    request_cfg = cfg_get(cfg, "request", default={})

    db_path = Path(
        args.db_path
        if args.db_path is not None
        else cfg_get(cfg, "db_path", default=str(default_db_path()))
    )
    user_agent_raw = (
        args.user_agent
        if args.user_agent is not None
        else cfg_get(cfg, "user_agent", default=os.getenv("SEC_USER_AGENT", DEFAULT_USER_AGENT))
    )
    user_agent = resolve_user_agent(str(user_agent_raw))
    archives_base_url = str(
        cfg_get(cfg, "archives", "base_url", default=BASE_ARCHIVES_URL_DEFAULT)
    ).rstrip("/")
    form_types = normalize_form_types(
        cfg_get(cfg, "archives", "form_types", default=sorted(FORM_TYPES_DEFAULT)),
        FORM_TYPES_DEFAULT,
    )

    request_timeout_seconds = int(
        cfg_get(request_cfg, "timeout_seconds", default=REQUEST_TIMEOUT_DEFAULT)
    )
    request_max_retries = int(
        cfg_get(request_cfg, "max_retries", default=REQUEST_MAX_RETRIES_DEFAULT)
    )
    retry_backoff_base_seconds = float(
        cfg_get(request_cfg, "backoff_base_seconds", default=RETRY_BACKOFF_BASE_DEFAULT)
    )
    retry_backoff_cap_seconds = float(
        cfg_get(request_cfg, "backoff_cap_seconds", default=RETRY_BACKOFF_CAP_DEFAULT)
    )
    sec_accept_encoding = str(
        cfg_get(request_cfg, "accept_encoding", default=SEC_ACCEPT_ENCODING_DEFAULT)
    )
    sec_host_header = str(cfg_get(request_cfg, "host_header", default=SEC_HOST_HEADER_DEFAULT))

    days_back = int(
        args.days_back
        if args.days_back is not None
        else cfg_get(cfg, mode_key, "days_back", default=5)
    )
    sleep_seconds = float(
        args.sleep_seconds
        if args.sleep_seconds is not None
        else cfg_get(cfg, mode_key, "sleep_seconds", default=0.25)
    )
    reconcile_cfg = bool(cfg_get(cfg, mode_key, "reconcile_current_quarter", default=False))
    reconcile_current_quarter = (
        args.reconcile_current_quarter
        if args.reconcile_current_quarter is not None
        else reconcile_cfg
    )
    force_reprocess = (
        args.force_reprocess
        if args.force_reprocess is not None
        else bool(cfg_get(cfg, mode_key, "force_reprocess", default=False))
    )
    process_name = str(
        cfg_get(
            cfg,
            mode_key,
            "process_name",
            default=("form4_weekly" if mode == "weekly" else "form4_daily"),
        )
    ).strip() or ("form4_weekly" if mode == "weekly" else "form4_daily")
    start_date_raw = (
        args.start_date
        if args.start_date is not None
        else cfg_get(cfg, mode_key, "start_date", default=None)
    )
    end_date_raw = (
        args.end_date
        if args.end_date is not None
        else cfg_get(cfg, mode_key, "end_date", default=None)
    )
    index_timeout_seconds = int(
        cfg_get(cfg, mode_key, "index_timeout_seconds", default=request_timeout_seconds)
    )
    index_max_retries = int(
        cfg_get(cfg, mode_key, "index_max_retries", default=request_max_retries)
    )
    index_missing_statuses = normalize_int_set(
        cfg_get(cfg, mode_key, "index_missing_statuses", default=[403, 404]),
        {403, 404},
    )
    full_index_timeout_seconds = int(
        cfg_get(cfg, mode_key, "full_index_timeout_seconds", default=request_timeout_seconds)
    )
    full_index_max_retries = int(
        cfg_get(cfg, mode_key, "full_index_max_retries", default=request_max_retries)
    )
    full_index_missing_statuses = normalize_int_set(
        cfg_get(cfg, mode_key, "full_index_missing_statuses", default=[403, 404]),
        {403, 404},
    )
    filing_timeout_seconds = int(
        cfg_get(cfg, mode_key, "filing_timeout_seconds", default=120)
    )
    filing_max_retries = int(
        cfg_get(cfg, mode_key, "filing_max_retries", default=6)
    )
    filing_missing_statuses = normalize_int_set(
        cfg_get(cfg, mode_key, "filing_missing_statuses", default=[404]),
        {404},
    )
    max_index_days = optional_positive_int(
        args.max_index_days
        if args.max_index_days is not None
        else cfg_get(cfg, mode_key, "max_index_days", default=0),
        default=0,
        name=f"{mode_key}.max_index_days",
    )
    max_filings = optional_positive_int(
        args.max_filings
        if args.max_filings is not None
        else cfg_get(cfg, mode_key, "max_filings_per_run", default=0),
        default=0,
        name=f"{mode_key}.max_filings_per_run",
    )
    max_reconcile_filings = optional_positive_int(
        args.max_reconcile_filings
        if args.max_reconcile_filings is not None
        else cfg_get(cfg, mode_key, "max_reconcile_filings", default=max_filings),
        default=max_filings,
        name=f"{mode_key}.max_reconcile_filings",
    )
    progress_every_filings = optional_positive_int(
        args.progress_every_filings
        if args.progress_every_filings is not None
        else cfg_get(cfg, mode_key, "progress_every_filings", default=100),
        default=100,
        name=f"{mode_key}.progress_every_filings",
    )
    progress_interval_sec = optional_positive_float(
        args.progress_interval_sec
        if args.progress_interval_sec is not None
        else cfg_get(cfg, mode_key, "progress_interval_sec", default=60.0),
        default=60.0,
        name=f"{mode_key}.progress_interval_sec",
    )
    stop_after_sec = optional_positive_float(
        args.stop_after_sec
        if args.stop_after_sec is not None
        else cfg_get(cfg, mode_key, "stop_after_sec", default=0.0),
        default=0.0,
        name=f"{mode_key}.stop_after_sec",
    )
    stop_deadline = time.monotonic() + stop_after_sec if stop_after_sec > 0 else None

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    ensure_schema(conn)

    if end_date_raw:
        end_date = datetime.strptime(str(end_date_raw), "%Y-%m-%d").date()
    else:
        end_date = date.today()

    if start_date_raw:
        start_date = datetime.strptime(str(start_date_raw), "%Y-%m-%d").date()
    else:
        last = get_state_date(conn, process_name)
        if last:
            start_date = last + timedelta(days=1)
        else:
            start_date = end_date - timedelta(days=max(days_back, 1))

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept-Encoding": sec_accept_encoding,
            "Host": sec_host_header,
        }
    )

    total_seen = 0
    total_loaded = 0
    days_attempted = 0
    daily_limit_hit = False

    try:
        for dt in iter_weekdays(start_date, end_date):
            if max_index_days > 0 and days_attempted >= max_index_days:
                print(
                    f"[LIMIT] max_index_days={max_index_days:,} reached at {dt.isoformat()}; "
                    "leaving remaining dates for a later run."
                )
                break
            if stop_deadline is not None and time.monotonic() >= stop_deadline:
                daily_limit_hit = True
                print(
                    f"[LIMIT] stop_after_sec reached before daily index {dt.isoformat()}; "
                    "leaving remaining dates for a later run."
                )
                break
            days_attempted += 1
            url = daily_form_index_url(dt, archives_base_url=archives_base_url)
            print(f"[DAY] {dt.isoformat()} fetching daily Form 4 index")
            try:
                idx_text = fetch_text(
                    session,
                    url,
                    timeout=index_timeout_seconds,
                    missing_statuses=index_missing_statuses,
                    max_retries=index_max_retries,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_cap_seconds=retry_backoff_cap_seconds,
                )
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in index_missing_statuses:
                    print(f"[INFO] Daily index unavailable ({status}), will retry later: {url}")
                    continue
                raise
            if idx_text is None:
                missing_label = ",".join(str(x) for x in sorted(index_missing_statuses))
                print(f"[INFO] Daily index unavailable ({missing_label}), will retry later: {url}")
                continue

            rows = parse_form_index(idx_text)
            remaining_filings = max_filings - total_seen if max_filings > 0 else 0
            if max_filings > 0 and remaining_filings <= 0:
                daily_limit_hit = True
                print(
                    f"[LIMIT] max_filings={max_filings:,} reached before {dt.isoformat()}; "
                    "leaving remaining dates for a later run."
                )
                break
            seen, loaded, limit_hit = process_rows(
                conn=conn,
                session=session,
                rows=rows,
                form_types=form_types,
                archives_base_url=archives_base_url,
                sleep_seconds=sleep_seconds,
                filing_timeout_seconds=filing_timeout_seconds,
                filing_max_retries=filing_max_retries,
                filing_missing_statuses=filing_missing_statuses,
                retry_backoff_base_seconds=retry_backoff_base_seconds,
                retry_backoff_cap_seconds=retry_backoff_cap_seconds,
                force_reprocess=force_reprocess,
                max_filings=remaining_filings,
                progress_every_filings=progress_every_filings,
                progress_interval_sec=progress_interval_sec,
                stop_deadline=stop_deadline,
                stage_label=f"daily:{dt.isoformat()}",
            )
            total_seen += seen
            total_loaded += loaded

            if limit_hit:
                daily_limit_hit = True
                conn.commit()
                print(
                    f"[LIMIT] Daily processing stopped before completing {dt.isoformat()}; "
                    "state date was not advanced for this date."
                )
                break
            else:
                set_state_date(conn, process_name, dt)
                conn.commit()

        if reconcile_current_quarter and not daily_limit_hit:
            full_idx = None
            try:
                full_idx = fetch_text(
                    session,
                    current_quarter_full_index_url(
                        end_date,
                        archives_base_url=archives_base_url,
                    ),
                    timeout=full_index_timeout_seconds,
                    missing_statuses=full_index_missing_statuses,
                    max_retries=full_index_max_retries,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_cap_seconds=retry_backoff_cap_seconds,
                )
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in full_index_missing_statuses:
                    print("[INFO] Current-quarter full index unavailable; skipping reconciliation.")
                else:
                    raise
            if full_idx:
                rows = parse_form_index(full_idx)
                seen, loaded, _ = process_rows(
                    conn=conn,
                    session=session,
                    rows=rows,
                    form_types=form_types,
                    archives_base_url=archives_base_url,
                    sleep_seconds=sleep_seconds,
                    filing_timeout_seconds=filing_timeout_seconds,
                    filing_max_retries=filing_max_retries,
                    filing_missing_statuses=filing_missing_statuses,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_cap_seconds=retry_backoff_cap_seconds,
                    force_reprocess=True,
                    max_filings=max_reconcile_filings,
                    progress_every_filings=progress_every_filings,
                    progress_interval_sec=progress_interval_sec,
                    stop_deadline=stop_deadline,
                    stage_label="current_quarter_reconcile",
                )
                total_seen += seen
                total_loaded += loaded
        elif reconcile_current_quarter and daily_limit_hit:
            print("[INFO] Skipping current-quarter reconciliation because daily incremental work hit a run limit.")

        print(
            "Daily Form 4 ingest complete. "
            f"DaysAttempted={days_attempted:,} Seen={total_seen:,} Loaded/Upserted={total_loaded:,}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
