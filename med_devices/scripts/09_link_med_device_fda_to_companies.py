#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_org_name, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("link_med_device_fda_to_companies")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CORPORATE_SUFFIXES = {
    "INC",
    "INCORPORATED",
    "CORP",
    "CORPORATION",
    "CO",
    "COMPANY",
    "PLC",
    "LTD",
    "LIMITED",
    "LLC",
    "LP",
    "NV",
    "SA",
    "AG",
    "SE",
    "HOLDING",
    "HOLDINGS",
    "GROUP",
}
FIELDNAMES = [
    "fda_manufacturer_id",
    "manufacturer_name",
    "mapped_ticker",
    "mapped_company_name",
    "mapping_confidence",
    "mapping_method",
    "approval_rows",
    "recall_rows",
    "adverse_event_rows",
    "review_reason",
]


@dataclass(frozen=True)
class CompanyAlias:
    company_id: int
    ticker: str
    company_name: str
    alias_raw: str
    alias_norm: str
    alias_core: str
    tokens: set[str]
    source: str


@dataclass(frozen=True)
class ManufacturerMatch:
    company_id: int | None
    ticker: str
    company_name: str
    confidence: float
    method: str
    review_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Link FDA manufacturers/sponsors to public med-device companies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--no-fact-update", action="store_true")
    return parser.parse_args()


def strip_suffixes(norm_name: str) -> str:
    tokens = [token for token in str(norm_name or "").split() if token]
    changed = True
    while changed and tokens:
        changed = False
        if tokens[-1] in CORPORATE_SUFFIXES:
            tokens.pop()
            changed = True
    return " ".join(tokens)


def name_tokens(norm_name: str) -> set[str]:
    return {token for token in strip_suffixes(norm_name).split() if token and token not in CORPORATE_SUFFIXES}


def build_aliases(conn: Any) -> list[CompanyAlias]:
    companies = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM dim_company
        WHERE is_active = 1
        """
    ).fetchall()
    aliases: list[CompanyAlias] = []
    seen: set[tuple[int, str]] = set()

    def add(company_id: int, ticker: str, company_name: str, raw: str, source: str) -> None:
        norm = normalize_org_name(raw)
        core = strip_suffixes(norm)
        if not core:
            return
        key = (company_id, core)
        if key in seen:
            return
        seen.add(key)
        aliases.append(
            CompanyAlias(
                company_id=company_id,
                ticker=ticker,
                company_name=company_name,
                alias_raw=raw,
                alias_norm=norm,
                alias_core=core,
                tokens=name_tokens(core),
                source=source,
            )
        )

    for row in companies:
        company_id = int(row["company_id"])
        ticker = normalize_ticker(row["ticker"])
        company_name = str(row["company_name"] or "")
        add(company_id, ticker, company_name, company_name, "company_name")
        if ticker:
            add(company_id, ticker, company_name, ticker, "ticker")

    alias_rows = conn.execute(
        """
        SELECT a.company_id, c.ticker, c.company_name, a.alias_raw
        FROM dim_company_alias a
        JOIN dim_company c ON c.company_id = a.company_id
        WHERE c.is_active = 1
        """
    ).fetchall()
    for row in alias_rows:
        add(int(row["company_id"]), normalize_ticker(row["ticker"]), str(row["company_name"] or ""), str(row["alias_raw"] or ""), "dim_company_alias")
    return aliases


def score_alias(manufacturer_name: str, alias: CompanyAlias, *, token_score_weight: float) -> tuple[float, str]:
    manufacturer_norm = normalize_org_name(manufacturer_name)
    manufacturer_core = strip_suffixes(manufacturer_norm)
    if not manufacturer_core or not alias.alias_core:
        return 0.0, "empty_name"
    if manufacturer_norm == alias.alias_norm:
        return 100.0, f"exact_norm:{alias.source}"
    if manufacturer_core == alias.alias_core:
        return 95.0, f"exact_core:{alias.source}"
    if len(manufacturer_core) >= 6 and len(alias.alias_core) >= 6:
        if manufacturer_core in alias.alias_core or alias.alias_core in manufacturer_core:
            return 90.0, f"substring_core:{alias.source}"
    manufacturer_tokens = name_tokens(manufacturer_core)
    if not manufacturer_tokens or not alias.tokens:
        return 0.0, "no_tokens"
    overlap = manufacturer_tokens & alias.tokens
    if not overlap:
        return 0.0, "no_token_overlap"
    coverage = len(overlap) / max(1, min(len(manufacturer_tokens), len(alias.tokens)))
    return min(89.0, token_score_weight * coverage), f"token_overlap:{alias.source}:{','.join(sorted(overlap))}"


def best_match(manufacturer_name: str, aliases: list[CompanyAlias], *, token_score_weight: float, min_confidence: float) -> ManufacturerMatch:
    best: tuple[float, CompanyAlias | None, str] = (0.0, None, "")
    for alias in aliases:
        score, method = score_alias(manufacturer_name, alias, token_score_weight=token_score_weight)
        if score > best[0]:
            best = (score, alias, method)
    score, alias, method = best
    if alias is None or score < min_confidence:
        return ManufacturerMatch(None, "", "", round(score, 2), "unmapped", f"below_threshold:{round(score, 2)}")
    return ManufacturerMatch(alias.company_id, alias.ticker, alias.company_name, round(score, 2), method, "")


def fact_counts(conn: Any, manufacturer_id: int) -> tuple[int, int, int]:
    approval = conn.execute(
        "SELECT COUNT(*) AS n FROM fact_fda_approval WHERE fda_manufacturer_id = ?",
        (manufacturer_id,),
    ).fetchone()
    recall = conn.execute(
        "SELECT COUNT(*) AS n FROM fact_fda_recall WHERE fda_manufacturer_id = ?",
        (manufacturer_id,),
    ).fetchone()
    adverse = conn.execute(
        "SELECT COUNT(*) AS n FROM fact_fda_adverse_event WHERE fda_manufacturer_id = ?",
        (manufacturer_id,),
    ).fetchone()
    return int(approval["n"] or 0), int(recall["n"] or 0), int(adverse["n"] or 0)


def update_fact_company_ids(conn: Any, *, min_confidence: float) -> None:
    for table in ("fact_fda_approval", "fact_fda_recall", "fact_fda_adverse_event", "fact_fda_inspection", "fact_fda_compliance_action"):
        conn.execute(
            f"""
            UPDATE {table}
            SET company_id = (
                SELECT parent_company_id
                FROM dim_fda_manufacturer m
                WHERE m.fda_manufacturer_id = {table}.fda_manufacturer_id
                  AND m.mapping_confidence >= ?
            )
            WHERE fda_manufacturer_id IS NOT NULL
            """,
            (min_confidence,),
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in FIELDNAMES} for row in rows])


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "fda_entity_linking.output_csv", "../output/med_devices_reports/med_device_fda_entity_mapping.csv"),
            base_dir=base_dir,
        )
    )
    min_confidence = (
        float(args.min_confidence)
        if args.min_confidence is not None
        else float(cfg_get(config, "fda_entity_linking.min_auto_confidence", 75.0))
    )
    token_score_weight = float(cfg_get(config, "fda_entity_linking.token_score_weight", 100.0))
    update_facts = (
        not args.no_fact_update
        and str(cfg_get(config, "fda_entity_linking.update_fact_company_ids", True)).strip().lower() not in {"0", "false", "no"}
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="link_med_device_fda_to_companies", input_path=config_path)
        try:
            aliases = build_aliases(conn)
            manufacturers = conn.execute(
                """
                SELECT fda_manufacturer_id, manufacturer_name
                FROM dim_fda_manufacturer
                ORDER BY manufacturer_name
                """
            ).fetchall()
            rows: list[dict[str, Any]] = []
            now = utc_now()
            mapped = 0
            for manufacturer in manufacturers:
                manufacturer_id = int(manufacturer["fda_manufacturer_id"])
                manufacturer_name = str(manufacturer["manufacturer_name"] or "")
                match = best_match(
                    manufacturer_name,
                    aliases,
                    token_score_weight=token_score_weight,
                    min_confidence=min_confidence,
                )
                parent_company_id = match.company_id if match.company_id is not None else None
                if parent_company_id is not None:
                    mapped += 1
                conn.execute(
                    """
                    UPDATE dim_fda_manufacturer
                    SET parent_company_id = ?,
                        mapping_confidence = ?,
                        mapping_method = ?,
                        updated_at = ?
                    WHERE fda_manufacturer_id = ?
                    """,
                    (parent_company_id, match.confidence, match.method, now, manufacturer_id),
                )
                approval_rows, recall_rows, adverse_rows = fact_counts(conn, manufacturer_id)
                rows.append(
                    {
                        "fda_manufacturer_id": manufacturer_id,
                        "manufacturer_name": manufacturer_name,
                        "mapped_ticker": match.ticker,
                        "mapped_company_name": match.company_name,
                        "mapping_confidence": match.confidence,
                        "mapping_method": match.method,
                        "approval_rows": approval_rows,
                        "recall_rows": recall_rows,
                        "adverse_event_rows": adverse_rows,
                        "review_reason": match.review_reason,
                    }
                )
            if update_facts:
                update_fact_company_ids(conn, min_confidence=min_confidence)
            write_csv(output_csv, rows)
            message = f"manufacturers={len(rows)} mapped={mapped} min_confidence={min_confidence} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=message)
            LOGGER.info("FDA entity linking complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
