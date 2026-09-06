"""Governed listed-security share/ADR conversion for Basic Materials."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_bytes, atomic_write_csv, atomic_write_json
from basic_materials.core.config import BasicMaterialsConfig
from basic_materials.core.db import assert_database_identity, utc_now


class SecurityRatioError(RuntimeError):
    """Raised when security-unit evidence or contract semantics are invalid."""


@dataclass(frozen=True)
class SecurityRatioPolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class SecurityRatioStats:
    policy_version: str
    policy_sha256: str
    as_of_date: str
    contract_rows: int
    direct_share_rows: int
    adr_ads_rows: int
    evidence_payloads: int
    cache_manifest_sha256: str
    cache_only: bool
    loaded_rows: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_CSV_FIELDS = (
    "ticker",
    "cik",
    "exchange",
    "listed_security_title",
    "security_basis",
    "issuer_shares_per_traded_security",
    "effective_from_date",
    "effective_to_date",
    "evidence_accession",
    "evidence_form",
    "evidence_accepted_at",
    "evidence_document",
    "evidence_url",
    "ratio_evidence_text",
    "source_id",
    "review_status",
    "reviewed_on",
    "notes",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(*values: Any) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_bytes(payload.encode("utf-8"))


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SecurityRatioError(f"{context} must be a mapping")
    return value


def _iso_date(value: Any, context: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise SecurityRatioError(f"{context} is not an ISO date: {text!r}") from exc


def _timestamp(value: Any, context: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        raise SecurityRatioError(f"{context} must be a UTC timestamp ending in Z")
    _iso_date(text[:10], context)
    return text


def load_security_ratio_policy(path: str | Path) -> SecurityRatioPolicy:
    policy_path = Path(path).resolve()
    payload = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "security ratio policy")
    expected = {
        "policy_version",
        "model_family",
        "sector",
        "contract_as_of_date",
        "reviewed_on",
        "calibration_eligible",
        "contract",
        "ratio_semantics",
        "coverage",
        "evidence",
        "gates",
    }
    if set(root) != expected:
        raise SecurityRatioError(
            f"Security ratio policy keys differ: missing={sorted(expected - set(root))}, "
            f"unexpected={sorted(set(root) - expected)}"
        )
    if root["policy_version"] != "basic_materials_security_ratio_policy_v1":
        raise SecurityRatioError("Unsupported security ratio policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise SecurityRatioError("Security ratio policy identity mismatch")
    if root["calibration_eligible"] is not False:
        raise SecurityRatioError("Security ratio policy cannot activate calibration")
    as_of_date = _iso_date(root["contract_as_of_date"], "contract_as_of_date")
    _iso_date(root["reviewed_on"], "reviewed_on")
    semantics = _mapping(root["ratio_semantics"], "ratio_semantics")
    if semantics.get("field_name") != "issuer_shares_per_traded_security":
        raise SecurityRatioError("Security ratio field semantics changed")
    if semantics.get("market_cap_formula") != (
        "market_price_usd * diluted_issuer_shares / issuer_shares_per_traded_security"
    ):
        raise SecurityRatioError("Security ratio market-cap formula changed")
    if semantics.get("infer_missing_ratio") is not False:
        raise SecurityRatioError("Missing security ratios must never be inferred")
    if semantics.get("historical_backfill_authorized") is not False:
        raise SecurityRatioError("Current ratio policy cannot authorize historical backfill")
    gates = _mapping(root["gates"], "gates")
    if (
        gates.get("historical_valuation_enabled") is not False
        or gates.get("specialized_metric_weighting_activated") is not False
        or gates.get("historical_calibration_activated") is not False
        or gates.get("portfolio_candidate_gate") is not False
        or gates.get("oos_score_valid_flag") is not False
    ):
        raise SecurityRatioError("Security ratio policy opened a prohibited downstream gate")
    return SecurityRatioPolicy(
        path=policy_path,
        checksum=_sha256_bytes(payload),
        version=str(root["policy_version"]),
        as_of_date=as_of_date,
        payload=root,
    )


def load_security_ratio_rows(
    csv_path: str | Path,
    *,
    policy: SecurityRatioPolicy,
) -> list[dict[str, Any]]:
    path = Path(csv_path).resolve()
    contract = _mapping(policy.payload["contract"], "contract")
    actual_sha = _sha256_path(path)
    if actual_sha != str(contract["csv_sha256"]):
        raise SecurityRatioError(
            f"Security ratio CSV hash mismatch: expected {contract['csv_sha256']}, got {actual_sha}"
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _CSV_FIELDS:
            raise SecurityRatioError("Security ratio CSV columns differ from the governed contract")
        raw_rows = [dict(row) for row in reader]
    if len(raw_rows) != int(contract["expected_rows"]):
        raise SecurityRatioError(
            f"Expected {contract['expected_rows']} security ratio rows, found {len(raw_rows)}"
        )
    coverage = _mapping(policy.payload["coverage"], "coverage")
    expected_tickers = tuple(str(value) for value in coverage["expected_current_tickers"])
    tickers = tuple(sorted(str(row["ticker"]).strip().upper() for row in raw_rows))
    if tickers != tuple(sorted(expected_tickers)) or len(set(tickers)) != len(tickers):
        raise SecurityRatioError("Security ratio ticker set is not exact and unique")
    allowed_reviews = set(str(value) for value in contract["allowed_review_statuses"])
    expected_ads = {
        str(ticker): float(value)
        for ticker, value in _mapping(coverage["adr_ads_ratios"], "adr_ads_ratios").items()
    }
    rows: list[dict[str, Any]] = []
    keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_rows, start=2):
        row = {key: str(raw.get(key) or "").strip() for key in _CSV_FIELDS}
        ticker = row["ticker"].upper()
        row["ticker"] = ticker
        if not re.fullmatch(r"\d{10}", row["cik"]):
            raise SecurityRatioError(f"row {index}: CIK must be ten digits")
        if not row["exchange"] or not row["listed_security_title"]:
            raise SecurityRatioError(f"row {index}: exchange and listed title are required")
        basis = row["security_basis"]
        if basis not in {"direct_share", "adr_ads"}:
            raise SecurityRatioError(f"row {index}: invalid security_basis {basis!r}")
        try:
            ratio = float(row["issuer_shares_per_traded_security"])
        except ValueError as exc:
            raise SecurityRatioError(f"row {index}: invalid security ratio") from exc
        if ratio <= 0:
            raise SecurityRatioError(f"row {index}: security ratio must be positive")
        if basis == "direct_share" and ratio != 1:
            raise SecurityRatioError(f"row {index}: a direct share must have ratio 1")
        if (ticker in expected_ads) != (basis == "adr_ads"):
            raise SecurityRatioError(f"row {index}: ADS classification differs from policy")
        if ticker in expected_ads and ratio != expected_ads[ticker]:
            raise SecurityRatioError(f"row {index}: ADS ratio differs from policy")
        effective_from = _iso_date(row["effective_from_date"], f"row {index}.effective_from_date")
        effective_to = (
            _iso_date(row["effective_to_date"], f"row {index}.effective_to_date")
            if row["effective_to_date"]
            else ""
        )
        accepted_at = _timestamp(row["evidence_accepted_at"], f"row {index}.evidence_accepted_at")
        if effective_from > policy.as_of_date or accepted_at[:10] > policy.as_of_date:
            raise SecurityRatioError(f"row {index}: ratio evidence is unavailable at the cutoff")
        if effective_to and (effective_to < effective_from or policy.as_of_date > effective_to):
            raise SecurityRatioError(f"row {index}: ratio is not effective at the cutoff")
        if effective_from > accepted_at[:10]:
            raise SecurityRatioError(f"row {index}: effective date follows evidence acceptance")
        accession_digits = row["evidence_accession"].replace("-", "")
        if not re.fullmatch(r"\d{18}", accession_digits):
            raise SecurityRatioError(f"row {index}: invalid SEC accession")
        if Path(row["evidence_document"]).name != row["evidence_document"]:
            raise SecurityRatioError(f"row {index}: unsafe evidence document")
        parsed_url = urlparse(row["evidence_url"])
        expected_path = (
            f"/Archives/edgar/data/{int(row['cik'])}/{accession_digits}/"
            f"{row['evidence_document']}"
        )
        if parsed_url.scheme != "https" or parsed_url.netloc != "www.sec.gov":
            raise SecurityRatioError(f"row {index}: evidence must use www.sec.gov HTTPS")
        if parsed_url.path != expected_path:
            raise SecurityRatioError(f"row {index}: evidence URL does not match CIK/accession/document")
        if basis == "adr_ads" and not row["ratio_evidence_text"]:
            raise SecurityRatioError(f"row {index}: ADS ratio evidence text is required")
        if row["source_id"] != contract["source_id"]:
            raise SecurityRatioError(f"row {index}: source_id differs from policy")
        if row["review_status"] not in allowed_reviews:
            raise SecurityRatioError(f"row {index}: review status is not allowed")
        _iso_date(row["reviewed_on"], f"row {index}.reviewed_on")
        key = (ticker, effective_from)
        if key in keys:
            raise SecurityRatioError(f"row {index}: duplicate ticker/effective date")
        keys.add(key)
        row["issuer_shares_per_traded_security"] = ratio
        row["effective_from_date"] = effective_from
        row["effective_to_date"] = effective_to
        row["evidence_accepted_at"] = accepted_at
        rows.append(row)
    direct_count = sum(row["security_basis"] == "direct_share" for row in rows)
    ads_count = sum(row["security_basis"] == "adr_ads" for row in rows)
    if direct_count != int(contract["expected_direct_share_rows"]):
        raise SecurityRatioError("Direct-share row count differs from policy")
    if ads_count != int(contract["expected_adr_ads_rows"]):
        raise SecurityRatioError("ADR/ADS row count differs from policy")
    return rows


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)


def _document_text(payload: bytes) -> str:
    parser = _TextExtractor()
    parser.feed(payload.decode("utf-8", errors="replace"))
    return " ".join(parser.parts)


def _normalized_tokens(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", unescape(value).casefold()))


def _request_bytes(
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float,
    max_retries: int,
    interval_seconds: float,
) -> bytes:
    last_error = ""
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=timeout_seconds,
            )
            if response.status_code == 200 and response.content:
                time.sleep(interval_seconds)
                return response.content
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < max_retries:
            time.sleep(max(interval_seconds, 0.25) * (attempt + 1))
    raise SecurityRatioError(f"Unable to fetch SEC security-ratio evidence {url}: {last_error}")


def _evidence_payload(
    *,
    config: BasicMaterialsConfig,
    policy: SecurityRatioPolicy,
    row: Mapping[str, Any],
    cache_only: bool,
) -> tuple[bytes, Path, str]:
    evidence = _mapping(policy.payload["evidence"], "evidence")
    cache_root = (
        config.paths.cache_root / str(evidence["cache_relative_path"])
    ).resolve()
    path = (
        cache_root
        / str(row["ticker"])
        / str(row["evidence_accession"]).replace("-", "")
        / str(row["evidence_document"])
    ).resolve()
    try:
        path.relative_to(cache_root)
    except ValueError as exc:
        raise SecurityRatioError(f"Evidence cache path escapes its root: {path}") from exc
    status = "cache_hit"
    if path.is_file():
        payload = path.read_bytes()
    else:
        if cache_only:
            raise SecurityRatioError(f"Cache-only ratio evidence is missing: {path}")
        payload = _request_bytes(
            str(row["evidence_url"]),
            user_agent=config.sec_fundamentals.user_agent,
            timeout_seconds=config.sec_fundamentals.timeout_seconds,
            max_retries=config.sec_fundamentals.max_retries,
            interval_seconds=config.sec_fundamentals.request_interval_seconds,
        )
        atomic_write_bytes(path, payload)
        status = "downloaded"
    text = _normalized_tokens(_document_text(payload))
    title = _normalized_tokens(str(row["listed_security_title"]))
    if title not in text:
        raise SecurityRatioError(
            f"{row['ticker']}: listed security title is absent from SEC evidence"
        )
    ratio_phrase = _normalized_tokens(str(row["ratio_evidence_text"]))
    if row["security_basis"] == "adr_ads" and ratio_phrase not in text:
        raise SecurityRatioError(f"{row['ticker']}: ADS ratio phrase is absent from SEC evidence")
    return payload, path, status


def _row_projection(
    row: Mapping[str, Any],
    *,
    security_id: int,
    evidence_payload_sha256: str,
    policy: SecurityRatioPolicy,
) -> dict[str, Any]:
    return {
        "security_id": security_id,
        "ticker": row["ticker"],
        "cik": row["cik"],
        "exchange": row["exchange"],
        "listed_security_title": row["listed_security_title"],
        "security_basis": row["security_basis"],
        "issuer_shares_per_traded_security": row[
            "issuer_shares_per_traded_security"
        ],
        "effective_from_date": row["effective_from_date"],
        "effective_to_date": row["effective_to_date"],
        "evidence_accession": row["evidence_accession"],
        "evidence_form": row["evidence_form"],
        "evidence_accepted_at": row["evidence_accepted_at"],
        "evidence_document": row["evidence_document"],
        "evidence_url": row["evidence_url"],
        "evidence_payload_sha256": evidence_payload_sha256,
        "ratio_evidence_text": row["ratio_evidence_text"],
        "source_id": row["source_id"],
        "review_status": row["review_status"],
        "reviewed_on": row["reviewed_on"],
        "policy_version": policy.version,
        "policy_sha256": policy.checksum,
    }


def load_security_share_ratios(
    conn: sqlite3.Connection,
    *,
    config: BasicMaterialsConfig,
    policy: SecurityRatioPolicy,
    cache_only: bool = False,
) -> SecurityRatioStats:
    """Verify SEC evidence and atomically load current effective ratios."""

    assert_database_identity(conn)
    rows = load_security_ratio_rows(config.paths.security_share_ratios_csv, policy=policy)
    identity_rows = {
        str(row["ticker"]): dict(row)
        for row in conn.execute(
            """
            SELECT s.security_id, s.ticker, s.exchange, p.cik, p.role_type
            FROM dim_security AS s
            JOIN dim_issuer_reporting_profile AS p ON p.security_id = s.security_id
            WHERE p.role_type = 'current_universe'
            """
        ).fetchall()
    }
    prepared: list[dict[str, Any]] = []
    cache_records: list[dict[str, Any]] = []
    for row in rows:
        identity = identity_rows.get(str(row["ticker"]))
        if identity is None:
            raise SecurityRatioError(f"{row['ticker']}: current security identity is missing")
        if str(identity["cik"]) != str(row["cik"]):
            raise SecurityRatioError(f"{row['ticker']}: CIK differs from the reporting profile")
        if str(identity["exchange"]) != str(row["exchange"]):
            raise SecurityRatioError(f"{row['ticker']}: exchange differs from dim_security")
        payload, cache_path, _cache_status = _evidence_payload(
            config=config,
            policy=policy,
            row=row,
            cache_only=cache_only,
        )
        payload_sha = _sha256_bytes(payload)
        projection = _row_projection(
            row,
            security_id=int(identity["security_id"]),
            evidence_payload_sha256=payload_sha,
            policy=policy,
        )
        row_sha = _sha256_bytes(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        prepared.append(
            projection
            | {
                "ratio_key": _stable_hash(
                    "basic_materials_security_ratio",
                    row["ticker"],
                    row["effective_from_date"],
                    row_sha,
                ),
                "row_sha256": row_sha,
            }
        )
        cache_records.append(
            {
                "ticker": row["ticker"],
                "accession": row["evidence_accession"],
                "document": row["evidence_document"],
                "url": row["evidence_url"],
                "cache_path": str(cache_path),
                "sha256": payload_sha,
                "byte_size": len(payload),
                "status": "available",
            }
        )
    cache_records.sort(key=lambda item: (item["ticker"], item["accession"], item["document"]))
    evidence = _mapping(policy.payload["evidence"], "evidence")
    cache_root = (config.paths.cache_root / str(evidence["cache_relative_path"])).resolve()
    cache_manifest_projection = {
        "artifact_id": "basic_materials_security_share_ratio_cache_v1",
        "policy_version": policy.version,
        "policy_sha256": policy.checksum,
        "as_of_date": policy.as_of_date,
        "records": cache_records,
    }
    cache_manifest_payload = cache_manifest_projection | {
        "manifest_sha256": _sha256_bytes(
            json.dumps(
                cache_manifest_projection, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
    }
    manifest_path = cache_root / str(evidence["cache_manifest"])
    if manifest_path.is_file():
        existing = _mapping(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            "security ratio cache manifest",
        )
        existing_projection = {
            key: value for key, value in existing.items() if key != "manifest_sha256"
        }
        existing_sha = _sha256_bytes(
            json.dumps(
                existing_projection, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        if str(existing.get("manifest_sha256") or "") != existing_sha:
            raise SecurityRatioError("Existing security-ratio cache manifest seal is invalid")
        normalized_existing = dict(existing_projection)
        normalized_existing["records"] = [
            dict(record) | {"status": "available"}
            for record in existing_projection.get("records", [])
        ]
        if normalized_existing != cache_manifest_projection:
            raise SecurityRatioError(
                "Existing security-ratio cache manifest differs from governed evidence"
            )
    atomic_write_json(manifest_path, cache_manifest_payload)
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in prepared:
            conn.execute(
                """
                INSERT INTO dim_security_share_ratio (
                    ratio_key, security_id, ticker, cik, exchange,
                    listed_security_title, security_basis,
                    issuer_shares_per_traded_security, effective_from_date,
                    effective_to_date, evidence_accession, evidence_form,
                    evidence_accepted_at, evidence_document, evidence_url,
                    evidence_payload_sha256, ratio_evidence_text, source_id,
                    review_status, reviewed_on, policy_version, policy_sha256,
                    row_sha256, created_at_utc, updated_at_utc
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(ticker, effective_from_date) DO UPDATE SET
                    ratio_key = excluded.ratio_key,
                    security_id = excluded.security_id,
                    cik = excluded.cik,
                    exchange = excluded.exchange,
                    listed_security_title = excluded.listed_security_title,
                    security_basis = excluded.security_basis,
                    issuer_shares_per_traded_security =
                        excluded.issuer_shares_per_traded_security,
                    effective_to_date = excluded.effective_to_date,
                    evidence_accession = excluded.evidence_accession,
                    evidence_form = excluded.evidence_form,
                    evidence_accepted_at = excluded.evidence_accepted_at,
                    evidence_document = excluded.evidence_document,
                    evidence_url = excluded.evidence_url,
                    evidence_payload_sha256 = excluded.evidence_payload_sha256,
                    ratio_evidence_text = excluded.ratio_evidence_text,
                    source_id = excluded.source_id,
                    review_status = excluded.review_status,
                    reviewed_on = excluded.reviewed_on,
                    policy_version = excluded.policy_version,
                    policy_sha256 = excluded.policy_sha256,
                    row_sha256 = excluded.row_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    row["ratio_key"],
                    row["security_id"],
                    row["ticker"],
                    row["cik"],
                    row["exchange"],
                    row["listed_security_title"],
                    row["security_basis"],
                    row["issuer_shares_per_traded_security"],
                    row["effective_from_date"],
                    row["effective_to_date"],
                    row["evidence_accession"],
                    row["evidence_form"],
                    row["evidence_accepted_at"],
                    row["evidence_document"],
                    row["evidence_url"],
                    row["evidence_payload_sha256"],
                    row["ratio_evidence_text"],
                    row["source_id"],
                    row["review_status"],
                    row["reviewed_on"],
                    row["policy_version"],
                    row["policy_sha256"],
                    row["row_sha256"],
                    now,
                    now,
                ),
            )
        placeholders = ",".join("?" for _ in prepared)
        keep_keys = tuple(str(row["ratio_key"]) for row in prepared)
        conn.execute(
            f"""
            DELETE FROM dim_security_share_ratio
            WHERE source_id = ? AND ratio_key NOT IN ({placeholders})
            """,
            (str(policy.payload["contract"]["source_id"]), *keep_keys),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    loaded = int(
        conn.execute(
            "SELECT COUNT(*) FROM dim_security_share_ratio WHERE policy_sha256 = ?",
            (policy.checksum,),
        ).fetchone()[0]
    )
    if loaded != len(prepared):
        raise SecurityRatioError(
            f"Expected {len(prepared)} loaded security ratios, found {loaded}"
        )
    return SecurityRatioStats(
        policy_version=policy.version,
        policy_sha256=policy.checksum,
        as_of_date=policy.as_of_date,
        contract_rows=len(prepared),
        direct_share_rows=sum(row["security_basis"] == "direct_share" for row in prepared),
        adr_ads_rows=sum(row["security_basis"] == "adr_ads" for row in prepared),
        evidence_payloads=len(cache_records),
        cache_manifest_sha256=str(cache_manifest_payload["manifest_sha256"]),
        cache_only=cache_only,
        loaded_rows=loaded,
    )


def active_security_ratio(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    as_of_date: str,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT *
        FROM dim_security_share_ratio
        WHERE security_id = ?
          AND effective_from_date <= ?
          AND (effective_to_date = '' OR effective_to_date >= ?)
          AND evidence_accepted_at <= ?
        ORDER BY effective_from_date DESC, evidence_accepted_at DESC
        """,
        (security_id, as_of_date, as_of_date, f"{as_of_date}T23:59:59Z"),
    ).fetchall()
    if len(rows) > 1:
        raise SecurityRatioError(
            f"Security {security_id} has overlapping effective ratio rows at {as_of_date}"
        )
    return dict(rows[0]) if rows else None


def write_security_ratio_reports(
    conn: sqlite3.Connection,
    *,
    stats: SecurityRatioStats,
    report_dir: str | Path,
) -> dict[str, str]:
    target = Path(report_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    summary = atomic_write_json(target / "security_ratio_summary.json", stats.as_dict())
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ticker, security_basis, issuer_shares_per_traded_security,
                   effective_from_date, effective_to_date, listed_security_title,
                   evidence_accession, evidence_form, evidence_accepted_at,
                   evidence_url, evidence_payload_sha256, policy_version,
                   policy_sha256, row_sha256
            FROM dim_security_share_ratio
            WHERE policy_sha256 = ?
            ORDER BY ticker, effective_from_date
            """,
            (stats.policy_sha256,),
        ).fetchall()
    ]
    csv_path = atomic_write_csv(
        target / "security_ratio_evidence.csv",
        rows,
        tuple(rows[0]) if rows else ("ticker",),
    )
    artifacts = {
        "summary": str(summary),
        "evidence": str(csv_path),
    }
    manifest_rows = [
        {
            "artifact": name,
            "path": path,
            "sha256": _sha256_path(Path(path)),
            "byte_size": Path(path).stat().st_size,
        }
        for name, path in sorted(artifacts.items())
    ]
    manifest = atomic_write_json(
        target / "security_ratio_artifact_manifest.json",
        {"artifacts": manifest_rows},
    )
    return artifacts | {"manifest": str(manifest)}
