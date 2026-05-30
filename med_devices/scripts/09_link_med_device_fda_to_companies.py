#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import re
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
from med_devices.core.text_norm import normalize_org_name, normalize_submission_identifier, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("link_med_device_fda_to_companies")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
STOP_TOKENS = {
    "A",
    "AN",
    "AND",
    "BIO",
    "BIOMEDICAL",
    "CARE",
    "CLINICAL",
    "DEVICE",
    "DEVICES",
    "DE",
    "DIAGNOSTIC",
    "DIAGNOSTICS",
    "GLOBAL",
    "HEALTH",
    "HEALTHCARE",
    "INTERNATIONAL",
    "LAB",
    "LABORATORIES",
    "LABORATORY",
    "LIFE",
    "MED",
    "MEDICAL",
    "MOLECULAR",
    "OF",
    "PRODUCT",
    "PRODUCTS",
    "SCIENCE",
    "SCIENCES",
    "SCIENTIFIC",
    "SERVICE",
    "SERVICES",
    "SOLUTIONS",
    "SURGICAL",
    "SYSTEM",
    "SYSTEMS",
    "TECH",
    "TECHNOLOGIES",
    "TECHNOLOGY",
    "THE",
    "US",
    "USA",
    "VASCULAR",
}
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
    "BV",
    "GMBH",
    "KG",
    "SAS",
    "SARL",
    "SRL",
    "PTY",
    "PTE",
    "KK",
    "AB",
    "OY",
    "AS",
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
    "matched_alias",
    "matched_alias_source",
    "second_best_ticker",
    "second_best_score",
    "candidate_summary",
    "manual_override_used",
    "approval_rows",
    "recall_rows",
    "adverse_event_rows",
    "inspection_rows",
    "compliance_rows",
    "total_fda_rows",
    "high_volume_unmapped",
    "review_reason",
]
ALLOWED_FACT_TABLES = {
    "fact_fda_approval": "fact_fda_approval",
    "fact_fda_recall": "fact_fda_recall",
    "fact_fda_adverse_event": "fact_fda_adverse_event",
    "fact_fda_inspection": "fact_fda_inspection",
    "fact_fda_compliance_action": "fact_fda_compliance_action",
}
ALIAS_SOURCE_PRIORITY = {
    "manual_override": 5,
    "extra_alias_csv": 4,
    "fda_footprint_csv": 4,
    "dim_company_alias": 3,
    "company_name_fragment": 2,
    "company_name": 2,
    "ticker": 1,
}
EXCLUDED_MAPPING_METHODS = {
    "do_not_map",
    "non_us_traded_parent",
    "not_in_investible_universe",
    "out_of_universe",
}


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
    matched_alias: str = ""
    matched_alias_source: str = ""
    second_best_ticker: str = ""
    second_best_score: float | None = None
    candidate_summary: str = ""
    manual_override_used: int = 0


@dataclass(frozen=True)
class ResolvedCompany:
    company_id: int
    ticker: str
    company_name: str


@dataclass(frozen=True)
class CompanyFootprint:
    company: ResolvedCompany
    primary_fda_entity: str
    product_codes: tuple[str, ...]
    premarket_numbers: tuple[str, ...]
    fei_numbers: tuple[str, ...]


@dataclass(frozen=True)
class ProductLineOverride:
    manufacturer_id: int | None
    manufacturer_name_norm: str
    company: ResolvedCompany
    confidence: float
    method: str
    product_codes: frozenset[str]
    keywords: tuple[str, ...]
    note: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Link FDA manufacturers/sponsors to public med-device companies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--high-volume-threshold", type=int, default=None)
    parser.add_argument("--extra-alias-csv", type=Path, default=None, help="Optional CSV: ticker,alias_raw[,source].")
    parser.add_argument(
        "--footprint-csv",
        type=Path,
        default=None,
        help="Optional CSV with ticker,primary_fda_entity,premarket_numbers,fei_numbers.",
    )
    parser.add_argument(
        "--manual-overrides-csv",
        type=Path,
        default=None,
        help="Optional CSV: fda_manufacturer_id or manufacturer_name plus ticker/company_id.",
    )
    parser.add_argument("--max-candidate-summary", type=int, default=None)
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


def split_alias_fragments(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    pieces = [text]
    pieces.extend(re.findall(r"\(([^()]{3,80})\)", text))
    split_pattern = r"\b(?:DBA|D/B/A|AKA|A/K/A|FORMERLY|F/K/A|DIVISION OF|DIV OF|SUBSIDIARY OF)\b|[/;|]"
    pieces.extend(re.split(split_pattern, text, flags=re.IGNORECASE))
    out: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        fragment = piece.strip()
        core = strip_suffixes(normalize_org_name(fragment))
        if core and core not in seen:
            out.append(fragment)
            seen.add(core)
    return out


def name_tokens(norm_name: str) -> set[str]:
    return {
        token
        for token in strip_suffixes(norm_name).split()
        if len(token) > 1 and token not in CORPORATE_SUFFIXES and token not in STOP_TOKENS
    }


def strong_token(token: str) -> bool:
    token = str(token or "").upper()
    return len(token) >= 5 and token not in STOP_TOKENS and token not in CORPORATE_SUFFIXES


def acronym(tokens: set[str]) -> str:
    ordered = [token for token in sorted(tokens) if strong_token(token)]
    return "".join(token[:1] for token in ordered)


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "") for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def method_key(raw: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")


def is_excluded_match(match: ManufacturerMatch) -> bool:
    return match.company_id is None and method_key(match.method) in EXCLUDED_MAPPING_METHODS


def split_multi_value(raw: object) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[;|]", str(raw or "")):
        value = item.strip()
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def normalize_product_code(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(raw or "").upper().strip())


def normalize_fei(raw: object) -> str:
    return re.sub(r"[^0-9]+", "", str(raw or "").strip())


def load_company_lookup(conn: Any) -> tuple[dict[int, ResolvedCompany], dict[str, ResolvedCompany]]:
    by_id: dict[int, ResolvedCompany] = {}
    by_ticker: dict[str, ResolvedCompany] = {}
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM dim_company
        WHERE is_active = 1
        """
    ).fetchall()
    for row in rows:
        company = ResolvedCompany(
            company_id=int(row["company_id"]),
            ticker=normalize_ticker(row["ticker"]),
            company_name=str(row["company_name"] or ""),
        )
        by_id[company.company_id] = company
        if company.ticker:
            by_ticker[company.ticker] = company
    return by_id, by_ticker


def load_company_footprints(conn: Any, path: Path | None) -> list[CompanyFootprint]:
    if path is None:
        return []
    if not path.exists():
        LOGGER.warning("Configured FDA footprint CSV does not exist: %s", path)
        return []
    _, by_ticker = load_company_lookup(conn)
    out: list[CompanyFootprint] = []
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
        company = by_ticker.get(ticker)
        if company is None:
            continue
        primary_entity = row_get(row, "primary_fda_entity", "fda_entity", "manufacturer_name")
        premarket_numbers = tuple(
            value
            for value in (normalize_submission_identifier(item) for item in split_multi_value(row_get(row, "premarket_numbers", "premarket_number")))
            if value
        )
        product_codes = tuple(
            value
            for value in (
                re.sub(r"[^A-Z0-9]+", "", item.upper().strip()) for item in split_multi_value(row_get(row, "product_codes", "product_code"))
            )
            if value
        )
        fei_numbers = tuple(
            value for value in (normalize_fei(item) for item in split_multi_value(row_get(row, "fei_numbers", "fei_number"))) if value
        )
        if not primary_entity and not product_codes and not premarket_numbers and not fei_numbers:
            continue
        out.append(
            CompanyFootprint(
                company=company,
                primary_fda_entity=primary_entity,
                product_codes=product_codes,
                premarket_numbers=premarket_numbers,
                fei_numbers=fei_numbers,
            )
        )
    LOGGER.info("Loaded FDA footprint link records: rows=%d path=%s", len(out), path)
    return out


def build_aliases(
    conn: Any,
    *,
    extra_alias_csv: Path | None = None,
    footprint_csv: Path | None = None,
) -> list[CompanyAlias]:
    companies = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM dim_company
        WHERE is_active = 1
        """
    ).fetchall()
    aliases: list[CompanyAlias] = []
    seen_index: dict[tuple[int, str], int] = {}

    def add(company_id: int, ticker: str, company_name: str, raw: str, source: str) -> None:
        for fragment in split_alias_fragments(raw):
            norm = normalize_org_name(fragment)
            core = strip_suffixes(norm)
            if not core:
                continue
            if source != "company_name" and (normalize_ticker(fragment) == ticker or len(core) <= 2):
                continue
            tokens = name_tokens(core)
            if not tokens and source not in {"manual_override", "extra_alias_csv", "fda_footprint_csv"}:
                continue
            key = (company_id, core)
            existing_idx = seen_index.get(key)
            candidate = CompanyAlias(
                company_id=company_id,
                ticker=ticker,
                company_name=company_name,
                alias_raw=fragment,
                alias_norm=norm,
                alias_core=core,
                tokens=tokens,
                source=source,
            )
            if existing_idx is not None:
                existing = aliases[existing_idx]
                if ALIAS_SOURCE_PRIORITY.get(source, 0) <= ALIAS_SOURCE_PRIORITY.get(existing.source, 0):
                    continue
                aliases[existing_idx] = candidate
                continue
            seen_index[key] = len(aliases)
            aliases.append(candidate)

    for row in companies:
        company_id = int(row["company_id"])
        ticker = normalize_ticker(row["ticker"])
        company_name = str(row["company_name"] or "")
        add(company_id, ticker, company_name, company_name, "company_name")

    if table_exists(conn, "dim_company_alias"):
        alias_rows = conn.execute(
            """
            SELECT a.company_id, c.ticker, c.company_name, a.alias_raw
            FROM dim_company_alias a
            JOIN dim_company c ON c.company_id = a.company_id
            WHERE c.is_active = 1
            """
        ).fetchall()
        for row in alias_rows:
            add(
                int(row["company_id"]),
                normalize_ticker(row["ticker"]),
                str(row["company_name"] or ""),
                str(row["alias_raw"] or ""),
                "dim_company_alias",
            )

    if extra_alias_csv is not None and extra_alias_csv.exists():
        _, by_ticker = load_company_lookup(conn)
        extra_rows = read_csv_flexible(extra_alias_csv)
        loaded = 0
        for row in extra_rows:
            ticker = normalize_ticker(row_get(row, "ticker", "mapped_ticker", "symbol"))
            alias_raw = row_get(row, "alias_raw", "alias", "manufacturer_name", "subsidiary_name", "brand")
            company = by_ticker.get(ticker)
            if company is None or not alias_raw:
                continue
            add(company.company_id, company.ticker, company.company_name, alias_raw, "extra_alias_csv")
            loaded += 1
        LOGGER.info("Loaded FDA/company extra aliases: rows=%d path=%s", loaded, extra_alias_csv)
    elif extra_alias_csv is not None:
        LOGGER.warning("Configured FDA extra alias CSV does not exist: %s", extra_alias_csv)
    for footprint in load_company_footprints(conn, footprint_csv):
        if footprint.primary_fda_entity:
            add(
                footprint.company.company_id,
                footprint.company.ticker,
                footprint.company.company_name,
                footprint.primary_fda_entity,
                "fda_footprint_csv",
            )
    return aliases


def edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_ch in enumerate(left, start=1):
        current = [i]
        for j, right_ch in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if left_ch == right_ch else 1),
                )
            )
        previous = current
    return previous[-1]


def whole_word_substring(left: str, right: str) -> bool:
    if len(left) < 6 or len(right) < 6:
        return False
    return bool(re.search(rf"(^|\s){re.escape(left)}($|\s)", right)) or bool(
        re.search(rf"(^|\s){re.escape(right)}($|\s)", left)
    )


def score_alias(manufacturer_name: str, alias: CompanyAlias, *, token_score_weight: float) -> tuple[float, str]:
    if alias.source == "ticker":
        return 0.0, "ticker_alias_not_used_for_fda_name_matching"
    manufacturer_norm = normalize_org_name(manufacturer_name)
    manufacturer_core = strip_suffixes(manufacturer_norm)
    if not manufacturer_core or not alias.alias_core:
        return 0.0, "empty_name"
    if manufacturer_norm == alias.alias_norm:
        return 100.0, f"exact_norm:{alias.source}"
    if manufacturer_core == alias.alias_core:
        return 97.0, f"exact_core:{alias.source}"
    if whole_word_substring(manufacturer_core, alias.alias_core):
        return 92.0, f"whole_word_substring:{alias.source}"
    manufacturer_tokens = name_tokens(manufacturer_core)
    if not manufacturer_tokens or not alias.tokens:
        return 0.0, "no_tokens"
    overlap = manufacturer_tokens & alias.tokens
    if not overlap:
        alias_acronym = acronym(alias.tokens)
        if len(alias_acronym) >= 3 and alias_acronym in manufacturer_tokens:
            return 76.0, f"acronym:{alias.source}:{alias_acronym}"
        return 0.0, "no_token_overlap"
    coverage_short = len(overlap) / max(1, min(len(manufacturer_tokens), len(alias.tokens)))
    coverage_long = len(overlap) / max(1, max(len(manufacturer_tokens), len(alias.tokens)))
    jaccard = len(overlap) / max(1, len(manufacturer_tokens | alias.tokens))
    score = 35.0 + (token_score_weight * 0.45 * coverage_short) + (35.0 * coverage_long) + (20.0 * jaccard)
    if len(overlap) == 1:
        token = next(iter(overlap))
        if strong_token(token) and len(manufacturer_tokens) == 1 and len(alias.tokens) == 1:
            score = max(score, 83.0)
        elif strong_token(token):
            score = min(score, 72.0)
        else:
            score = min(score, 55.0)
    manufacturer_first = manufacturer_core.split()[0] if manufacturer_core.split() else ""
    alias_first = alias.alias_core.split()[0] if alias.alias_core.split() else ""
    if len(overlap) > 1 and manufacturer_first and manufacturer_first == alias_first:
        score += 3.0
    if alias.source in {"extra_alias_csv", "fda_footprint_csv"} and len(overlap) > 1:
        score += 3.0
    return min(91.0, score), f"token_overlap:{alias.source}:{','.join(sorted(overlap))}"


def best_match(
    manufacturer_name: str,
    aliases: list[CompanyAlias],
    *,
    token_score_weight: float,
    min_confidence: float,
    edit_distance_max_normalized: float,
    edit_distance_score: float,
    ambiguity_margin: float = 5.0,
    max_candidate_summary: int = 5,
) -> ManufacturerMatch:
    candidates: list[tuple[float, CompanyAlias, str]] = []
    manufacturer_core = strip_suffixes(normalize_org_name(manufacturer_name))
    for alias in aliases:
        score, method = score_alias(manufacturer_name, alias, token_score_weight=token_score_weight)
        if score < edit_distance_score and 3 < len(manufacturer_core) < 16 and 3 < len(alias.alias_core) < 16:
            distance = edit_distance(manufacturer_core, alias.alias_core)
            normalized = distance / max(len(manufacturer_core), len(alias.alias_core), 1)
            if normalized <= edit_distance_max_normalized:
                score = edit_distance_score
                method = f"edit_distance:{alias.source}:{normalized:.2f}"
        if score > 0:
            candidates.append((score, alias, method))
    if not candidates:
        return ManufacturerMatch(None, "", "", 0.0, "unmapped", "no_candidate")
    candidates.sort(
        key=lambda item: (
            item[0],
            ALIAS_SOURCE_PRIORITY.get(item[1].source, 0),
            len(item[1].tokens),
        ),
        reverse=True,
    )
    score, alias, method = candidates[0]
    second = next((item for item in candidates[1:] if item[1].company_id != alias.company_id), None)
    candidate_summary = "|".join(
        f"{cand_alias.ticker}:{round(cand_score, 1)}:{cand_method}:{cand_alias.alias_core}"
        for cand_score, cand_alias, cand_method in candidates[: max(1, max_candidate_summary)]
    )
    second_ticker = second[1].ticker if second is not None else ""
    second_score = round(second[0], 2) if second is not None else None
    if second is not None and score < 95.0 and score - second[0] < ambiguity_margin:
        return ManufacturerMatch(
            None,
            "",
            "",
            round(score, 2),
            "ambiguous",
            f"ambiguous_top2:{alias.ticker}:{round(score, 2)}:{second[1].ticker}:{round(second[0], 2)}",
            matched_alias=alias.alias_raw,
            matched_alias_source=alias.source,
            second_best_ticker=second_ticker,
            second_best_score=second_score,
            candidate_summary=candidate_summary,
        )
    if score < min_confidence:
        return ManufacturerMatch(
            None,
            "",
            "",
            round(score, 2),
            "unmapped",
            f"below_threshold:{round(score, 2)}",
            matched_alias=alias.alias_raw,
            matched_alias_source=alias.source,
            second_best_ticker=second_ticker,
            second_best_score=second_score,
            candidate_summary=candidate_summary,
        )
    return ManufacturerMatch(
        alias.company_id,
        alias.ticker,
        alias.company_name,
        round(score, 2),
        method,
        "",
        matched_alias=alias.alias_raw,
        matched_alias_source=alias.source,
        second_best_ticker=second_ticker,
        second_best_score=second_score,
        candidate_summary=candidate_summary,
    )


def load_manual_overrides(conn: Any, path: Path | None) -> dict[tuple[str, str], ManufacturerMatch]:
    if path is None:
        return {}
    if not path.exists():
        LOGGER.warning("Configured FDA manual override CSV does not exist: %s", path)
        return {}
    by_id, by_ticker = load_company_lookup(conn)
    out: dict[tuple[str, str], ManufacturerMatch] = {}
    rows = read_csv_flexible(path)
    loaded = 0
    for row in rows:
        method = row_get(row, "mapping_method", "method") or "manual_override"
        normalized_method = method_key(method)
        reason = row_get(row, "review_reason", "note", "notes")
        raw_confidence = row_get(row, "confidence", "mapping_confidence")
        confidence = 0.0 if normalized_method in EXCLUDED_MAPPING_METHODS else 99.0
        if raw_confidence:
            try:
                confidence = float(raw_confidence)
            except ValueError:
                LOGGER.warning("Ignoring invalid manual override confidence: %s", row)
        ticker = normalize_ticker(row_get(row, "ticker", "mapped_ticker", "symbol"))
        company_id_text = row_get(row, "company_id", "mapped_company_id")
        company: ResolvedCompany | None = None
        if company_id_text.isdigit():
            company = by_id.get(int(company_id_text))
        if company is None and ticker:
            company = by_ticker.get(ticker)
        if company is None:
            if normalized_method in EXCLUDED_MAPPING_METHODS:
                match = ManufacturerMatch(
                    None,
                    "",
                    "",
                    confidence,
                    normalized_method,
                    reason or "excluded_from_investible_universe",
                    matched_alias=row_get(row, "alias_raw", "alias", "manufacturer_name", "fda_manufacturer_name"),
                    matched_alias_source="manual_override",
                    manual_override_used=1,
                )
                manufacturer_id = row_get(row, "fda_manufacturer_id", "manufacturer_id")
                manufacturer_name = row_get(row, "manufacturer_name", "fda_manufacturer_name", "alias_raw", "alias")
                if manufacturer_id.isdigit():
                    out[("id", manufacturer_id)] = match
                if manufacturer_name:
                    out[("name", normalize_org_name(manufacturer_name))] = match
                loaded += 1
                continue
            LOGGER.warning("Skipping FDA manual override with unresolved company: %s", row)
            continue
        match = ManufacturerMatch(
            company.company_id,
            company.ticker,
            company.company_name,
            confidence,
            normalized_method or method,
            reason,
            matched_alias=row_get(row, "alias_raw", "alias", "manufacturer_name", "fda_manufacturer_name"),
            matched_alias_source="manual_override",
            manual_override_used=1,
        )
        manufacturer_id = row_get(row, "fda_manufacturer_id", "manufacturer_id")
        manufacturer_name = row_get(row, "manufacturer_name", "fda_manufacturer_name", "alias_raw", "alias")
        if manufacturer_id.isdigit():
            out[("id", manufacturer_id)] = match
        if manufacturer_name:
            out[("name", normalize_org_name(manufacturer_name))] = match
        loaded += 1
    LOGGER.info("Loaded FDA manual overrides: rows=%d path=%s", loaded, path)
    return out


def load_product_line_overrides(conn: Any, path: Path | None) -> list[ProductLineOverride]:
    if path is None:
        return []
    if not path.exists():
        LOGGER.warning("Configured FDA product-line override CSV does not exist: %s", path)
        return []
    _, by_ticker = load_company_lookup(conn)
    out: list[ProductLineOverride] = []
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "mapped_ticker", "symbol"))
        company = by_ticker.get(ticker)
        if company is None:
            LOGGER.warning("Skipping FDA product-line override with unresolved ticker: %s", row)
            continue
        manufacturer_id_text = row_get(row, "fda_manufacturer_id", "manufacturer_id")
        manufacturer_id = int(manufacturer_id_text) if manufacturer_id_text.isdigit() else None
        manufacturer_name = row_get(row, "manufacturer_name", "fda_manufacturer_name")
        manufacturer_name_norm = normalize_org_name(manufacturer_name)
        product_codes = frozenset(
            code
            for code in (normalize_product_code(item) for item in split_multi_value(row_get(row, "product_codes", "product_code")))
            if code
        )
        keywords = tuple(
            keyword.casefold()
            for keyword in split_multi_value(row_get(row, "match_keywords", "keywords", "keyword"))
            if keyword.strip()
        )
        if manufacturer_id is None and not manufacturer_name_norm:
            LOGGER.warning("Skipping FDA product-line override without manufacturer identifier: %s", row)
            continue
        if not product_codes and not keywords:
            LOGGER.warning("Skipping FDA product-line override without product codes or keywords: %s", row)
            continue
        raw_confidence = row_get(row, "confidence", "mapping_confidence")
        try:
            confidence = float(raw_confidence) if raw_confidence else 97.0
        except ValueError:
            LOGGER.warning("Ignoring invalid product-line override confidence: %s", row)
            confidence = 97.0
        out.append(
            ProductLineOverride(
                manufacturer_id=manufacturer_id,
                manufacturer_name_norm=manufacturer_name_norm,
                company=company,
                confidence=confidence,
                method=row_get(row, "mapping_method", "method") or "product_line_override",
                product_codes=product_codes,
                keywords=keywords,
                note=row_get(row, "note", "review_reason", "notes"),
            )
        )
    LOGGER.info("Loaded FDA product-line overrides: rows=%d path=%s", len(out), path)
    return out


def load_fact_counts(conn: Any) -> dict[int, tuple[int, int, int, int, int]]:
    counts: dict[int, list[int]] = {}
    queries = [
        (0, "fact_fda_approval", "SELECT fda_manufacturer_id, COUNT(*) AS n FROM fact_fda_approval WHERE fda_manufacturer_id IS NOT NULL GROUP BY fda_manufacturer_id"),
        (1, "fact_fda_recall", "SELECT fda_manufacturer_id, COUNT(*) AS n FROM fact_fda_recall WHERE fda_manufacturer_id IS NOT NULL GROUP BY fda_manufacturer_id"),
        (2, "fact_fda_adverse_event", "SELECT fda_manufacturer_id, COUNT(*) AS n FROM fact_fda_adverse_event WHERE fda_manufacturer_id IS NOT NULL GROUP BY fda_manufacturer_id"),
        (3, "fact_fda_inspection", "SELECT fda_manufacturer_id, COUNT(*) AS n FROM fact_fda_inspection WHERE fda_manufacturer_id IS NOT NULL GROUP BY fda_manufacturer_id"),
        (4, "fact_fda_compliance_action", "SELECT fda_manufacturer_id, COUNT(*) AS n FROM fact_fda_compliance_action WHERE fda_manufacturer_id IS NOT NULL GROUP BY fda_manufacturer_id"),
    ]
    for idx, table_name, query in queries:
        if not table_exists(conn, table_name):
            continue
        for row in conn.execute(query).fetchall():
            manufacturer_id = int(row["fda_manufacturer_id"])
            counts.setdefault(manufacturer_id, [0, 0, 0, 0, 0])[idx] = int(row["n"] or 0)
    return {manufacturer_id: (values[0], values[1], values[2], values[3], values[4]) for manufacturer_id, values in counts.items()}


def update_fact_company_ids(conn: Any, *, min_confidence: float) -> None:
    for table in ALLOWED_FACT_TABLES.values():
        if not table_exists(conn, table):
            LOGGER.info("Skipping FDA fact company update; table not found: %s", table)
            continue
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
              AND COALESCE(company_id, -1) != COALESCE((
                SELECT parent_company_id
                FROM dim_fda_manufacturer m
                WHERE m.fda_manufacturer_id = {table}.fda_manufacturer_id
                  AND m.mapping_confidence >= ?
              ), -1)
            """,
            (min_confidence, min_confidence),
        )


PRODUCT_LINE_FACT_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "fact_fda_approval": (
        "fda_approval_id",
        ("product_code", "submission_number", "device_name", "decision", "payload_json"),
    ),
    "fact_fda_recall": (
        "fda_recall_id",
        (
            "product_code",
            "recall_number",
            "event_id",
            "classification",
            "status",
            "recalling_firm",
            "reason_for_recall",
            "payload_json",
        ),
    ),
    "fact_fda_adverse_event": (
        "adverse_event_id",
        ("product_code", "event_type", "device_problem_codes", "patient_problem_codes", "payload_json"),
    ),
}


def product_line_override_matches(row: Any, override: ProductLineOverride) -> bool:
    product_code = normalize_product_code(row["product_code"] if "product_code" in row.keys() else "")
    if product_code and product_code in override.product_codes:
        return True
    if not override.keywords:
        return False
    text = " ".join(str(row[key] or "") for key in row.keys()).casefold()
    return any(keyword in text for keyword in override.keywords)


def product_line_override_manufacturer_ids(conn: Any, override: ProductLineOverride) -> list[int]:
    if override.manufacturer_id is not None:
        return [override.manufacturer_id]
    if not override.manufacturer_name_norm:
        return []
    rows = conn.execute(
        """
        SELECT fda_manufacturer_id
        FROM dim_fda_manufacturer
        WHERE manufacturer_name_norm = ?
        """,
        (override.manufacturer_name_norm,),
    ).fetchall()
    return [int(row["fda_manufacturer_id"]) for row in rows]


def apply_product_line_fact_links(conn: Any, overrides: list[ProductLineOverride]) -> dict[str, int]:
    counts = {table_name: 0 for table_name in PRODUCT_LINE_FACT_TABLES}
    if not overrides:
        return counts
    now = utc_now()
    for override in overrides:
        manufacturer_ids = product_line_override_manufacturer_ids(conn, override)
        if not manufacturer_ids:
            LOGGER.warning("Product-line override found no manufacturer rows: %s", override)
            continue
        for manufacturer_id in manufacturer_ids:
            for table_name, (pk_column, text_columns) in PRODUCT_LINE_FACT_TABLES.items():
                if not table_exists(conn, table_name):
                    continue
                columns = ", ".join(text_columns)
                rows = conn.execute(
                    f"""
                    SELECT {pk_column}, company_id, {columns}
                    FROM {table_name}
                    WHERE fda_manufacturer_id = ?
                    """,
                    (manufacturer_id,),
                ).fetchall()
                for row in rows:
                    if not product_line_override_matches(row, override):
                        continue
                    if row["company_id"] is not None and int(row["company_id"]) != override.company.company_id:
                        LOGGER.warning(
                            "Skipping FDA product-line override conflict: table=%s row_id=%s ticker=%s existing_company_id=%s",
                            table_name,
                            row[pk_column],
                            override.company.ticker,
                            row["company_id"],
                        )
                        continue
                    cur = conn.execute(
                        f"""
                        UPDATE {table_name}
                        SET company_id = ?,
                            updated_at = ?
                        WHERE {pk_column} = ?
                          AND (
                            company_id IS NULL
                            OR company_id = ?
                          )
                        """,
                        (override.company.company_id, now, row[pk_column], override.company.company_id),
                    )
                    counts[table_name] += max(0, cur.rowcount if cur.rowcount is not None else 0)
    return counts


def approval_submission_clause(identifier: str) -> tuple[str, list[str]]:
    if identifier.startswith("P") and re.match(r"^P[0-9]{5,}", identifier):
        return "(submission_number = ? OR submission_number LIKE ?)", [identifier, f"{identifier}-%"]
    return "submission_number = ?", [identifier]


def org_names_corroborate(left: str, right: str) -> bool:
    left_core = strip_suffixes(normalize_org_name(left))
    right_core = strip_suffixes(normalize_org_name(right))
    if not left_core or not right_core:
        return False
    if left_core == right_core or whole_word_substring(left_core, right_core):
        return True
    left_tokens = name_tokens(left_core)
    right_tokens = name_tokens(right_core)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    if len(overlap) >= 2:
        coverage = len(overlap) / max(1, min(len(left_tokens), len(right_tokens)))
        return coverage >= 0.67
    if len(overlap) == 1:
        token = next(iter(overlap))
        return strong_token(token) and (len(left_tokens) == 1 or len(right_tokens) == 1)
    return False


def approval_candidate_is_confirmed(footprint: CompanyFootprint, row: Any) -> bool:
    manufacturer_name = str(row["manufacturer_name"] or "")
    # Exact submission numbers are still not enough by themselves; analyst-supplied IDs can be stale
    # or wrong. Require the FDA applicant/manufacturer to corroborate the ticker's FDA footprint.
    return (
        org_names_corroborate(footprint.primary_fda_entity, manufacturer_name)
        or org_names_corroborate(footprint.company.company_name, manufacturer_name)
    )


def apply_footprint_fact_links(conn: Any, footprints: list[CompanyFootprint]) -> dict[str, int]:
    counts = {
        "approval_rows": 0,
        "manufacturer_rows": 0,
        "inspection_rows": 0,
        "compliance_rows": 0,
        "conflict_rows": 0,
        "unconfirmed_approval_rows": 0,
    }
    if not footprints:
        return counts
    now = utc_now()
    for footprint in footprints:
        company = footprint.company
        for submission_number in footprint.premarket_numbers:
            clause, params = approval_submission_clause(submission_number)
            approval_rows = conn.execute(
                f"""
                SELECT a.fda_approval_id, a.company_id, a.fda_manufacturer_id,
                       a.submission_number, a.product_code, a.device_name,
                       m.manufacturer_name, m.parent_company_id, m.mapping_confidence
                FROM fact_fda_approval a
                LEFT JOIN dim_fda_manufacturer m
                  ON m.fda_manufacturer_id = a.fda_manufacturer_id
                WHERE {clause}
                """,
                params,
            ).fetchall()
            for approval in approval_rows:
                if not approval_candidate_is_confirmed(footprint, approval):
                    counts["unconfirmed_approval_rows"] += 1
                    LOGGER.warning(
                        "Skipping unconfirmed FDA footprint approval link: ticker=%s submission=%s product_code=%s applicant=%s",
                        company.ticker,
                        str(approval["submission_number"] or ""),
                        str(approval["product_code"] or ""),
                        str(approval["manufacturer_name"] or ""),
                    )
                    continue
                if approval["company_id"] is not None and int(approval["company_id"]) != company.company_id:
                    counts["conflict_rows"] += 1
                    LOGGER.warning(
                        "FDA footprint approval link has existing company conflict: ticker=%s submission=%s mapped_company_id=%s",
                        company.ticker,
                        str(approval["submission_number"] or ""),
                        approval["company_id"],
                    )
                    continue
                manufacturer_id = approval["fda_manufacturer_id"]
                if manufacturer_id is not None:
                    cur = conn.execute(
                        """
                        UPDATE dim_fda_manufacturer
                        SET parent_company_id = ?,
                            mapping_confidence = CASE WHEN mapping_confidence > 99.0 THEN mapping_confidence ELSE 99.0 END,
                            mapping_method = CASE WHEN mapping_confidence > 99.0 THEN mapping_method ELSE 'fda_footprint_premarket' END,
                            updated_at = ?
                        WHERE fda_manufacturer_id = ?
                          AND (
                            parent_company_id IS NULL
                            OR parent_company_id = ?
                            OR mapping_confidence < 95.0
                          )
                        """,
                        (company.company_id, now, int(manufacturer_id), company.company_id),
                    )
                    counts["manufacturer_rows"] += max(0, cur.rowcount if cur.rowcount is not None else 0)
                cur = conn.execute(
                    """
                    UPDATE fact_fda_approval
                    SET company_id = ?,
                        updated_at = ?
                    WHERE fda_approval_id = ?
                      AND (
                        company_id IS NULL
                        OR company_id = ?
                      )
                    """,
                    (company.company_id, now, int(approval["fda_approval_id"]), company.company_id),
                )
                counts["approval_rows"] += max(0, cur.rowcount if cur.rowcount is not None else 0)
        if footprint.fei_numbers:
            placeholders = ", ".join("?" for _ in footprint.fei_numbers)
            cur = conn.execute(
                f"""
                UPDATE dim_fda_manufacturer
                SET parent_company_id = ?,
                    mapping_confidence = CASE WHEN mapping_confidence > 99.0 THEN mapping_confidence ELSE 99.0 END,
                    mapping_method = CASE WHEN mapping_confidence > 99.0 THEN mapping_method ELSE 'fda_footprint_fei' END,
                    updated_at = ?
                WHERE fei_number IN ({placeholders})
                  AND (
                    parent_company_id IS NULL
                    OR parent_company_id = ?
                    OR mapping_confidence < 95.0
                  )
                """,
                (company.company_id, now, *footprint.fei_numbers, company.company_id),
            )
            counts["manufacturer_rows"] += max(0, cur.rowcount if cur.rowcount is not None else 0)
            if table_exists(conn, "fact_fda_inspection"):
                cur = conn.execute(
                    f"""
                    UPDATE fact_fda_inspection
                    SET company_id = ?,
                        updated_at = ?
                    WHERE fei_number IN ({placeholders})
                      AND (
                        company_id IS NULL
                        OR company_id = ?
                      )
                    """,
                    (company.company_id, now, *footprint.fei_numbers, company.company_id),
                )
                counts["inspection_rows"] += max(0, cur.rowcount if cur.rowcount is not None else 0)
            if table_exists(conn, "fact_fda_compliance_action"):
                cur = conn.execute(
                    f"""
                    UPDATE fact_fda_compliance_action
                    SET company_id = ?,
                        updated_at = ?
                    WHERE fei_number IN ({placeholders})
                      AND (
                        company_id IS NULL
                        OR company_id = ?
                      )
                    """,
                    (company.company_id, now, *footprint.fei_numbers, company.company_id),
                )
                counts["compliance_rows"] += max(0, cur.rowcount if cur.rowcount is not None else 0)
    return counts


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
    edit_distance_max_normalized = float(cfg_get(config, "fda_entity_linking.edit_distance_max_normalized", 0.20))
    edit_distance_score = float(cfg_get(config, "fda_entity_linking.edit_distance_score", 70.0))
    ambiguity_margin = float(cfg_get(config, "fda_entity_linking.ambiguity_margin", 5.0))
    max_candidate_summary = (
        int(args.max_candidate_summary)
        if args.max_candidate_summary is not None
        else int(cfg_get(config, "fda_entity_linking.max_candidate_summary", 5))
    )
    high_volume_threshold = (
        int(args.high_volume_threshold)
        if args.high_volume_threshold is not None
        else int(cfg_get(config, "fda_entity_linking.high_volume_unmapped_record_threshold", 50))
    )
    extra_alias_raw = str(cfg_get(config, "fda_entity_linking.extra_alias_csv", "") or "").strip()
    extra_alias_csv = (
        args.extra_alias_csv.expanduser().resolve()
        if args.extra_alias_csv
        else resolve_path(extra_alias_raw, base_dir=base_dir)
        if extra_alias_raw
        else None
    )
    footprint_raw = str(
        cfg_get(
            config,
            "fda_entity_linking.footprint_csv",
            cfg_get(config, "fda_features.footprint_csv", ""),
        )
        or ""
    ).strip()
    footprint_csv = (
        args.footprint_csv.expanduser().resolve()
        if args.footprint_csv
        else resolve_path(footprint_raw, base_dir=base_dir)
        if footprint_raw
        else None
    )
    manual_overrides_raw = str(cfg_get(config, "fda_entity_linking.manual_overrides_csv", "") or "").strip()
    manual_overrides_csv = (
        args.manual_overrides_csv.expanduser().resolve()
        if args.manual_overrides_csv
        else resolve_path(manual_overrides_raw, base_dir=base_dir)
        if manual_overrides_raw
        else None
    )
    product_line_overrides_raw = str(cfg_get(config, "fda_entity_linking.product_line_overrides_csv", "") or "").strip()
    product_line_overrides_csv = (
        resolve_path(product_line_overrides_raw, base_dir=base_dir)
        if product_line_overrides_raw
        else None
    )
    update_facts = (
        not args.no_fact_update
        and str(cfg_get(config, "fda_entity_linking.update_fact_company_ids", True)).strip().lower() not in {"0", "false", "no"}
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="link_med_device_fda_to_companies", input_path=config_path)
        try:
            aliases = build_aliases(conn, extra_alias_csv=extra_alias_csv, footprint_csv=footprint_csv)
            footprints = load_company_footprints(conn, footprint_csv)
            manual_overrides = load_manual_overrides(conn, manual_overrides_csv)
            product_line_overrides = load_product_line_overrides(conn, product_line_overrides_csv)
            counts_by_manufacturer = load_fact_counts(conn)
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
            ambiguous = 0
            high_volume_unmapped_count = 0
            for manufacturer in manufacturers:
                manufacturer_id = int(manufacturer["fda_manufacturer_id"])
                manufacturer_name = str(manufacturer["manufacturer_name"] or "")
                match = manual_overrides.get(("id", str(manufacturer_id))) or manual_overrides.get(
                    ("name", normalize_org_name(manufacturer_name))
                )
                if match is None:
                    match = best_match(
                        manufacturer_name,
                        aliases,
                        token_score_weight=token_score_weight,
                        min_confidence=min_confidence,
                        edit_distance_max_normalized=edit_distance_max_normalized,
                        edit_distance_score=edit_distance_score,
                        ambiguity_margin=ambiguity_margin,
                        max_candidate_summary=max_candidate_summary,
                    )
                parent_company_id = match.company_id if match.company_id is not None else None
                if parent_company_id is not None:
                    mapped += 1
                if match.method == "ambiguous":
                    ambiguous += 1
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
                approval_rows, recall_rows, adverse_rows, inspection_rows, compliance_rows = counts_by_manufacturer.get(
                    manufacturer_id,
                    (0, 0, 0, 0, 0),
                )
                total_rows = approval_rows + recall_rows + adverse_rows + inspection_rows + compliance_rows
                high_volume_unmapped = (
                    1
                    if parent_company_id is None and total_rows >= high_volume_threshold and not is_excluded_match(match)
                    else 0
                )
                high_volume_unmapped_count += high_volume_unmapped
                if high_volume_unmapped:
                    LOGGER.warning(
                        "High-volume FDA manufacturer is unmapped: id=%s name=%s records=%d best=%s",
                        manufacturer_id,
                        manufacturer_name,
                        total_rows,
                        match.candidate_summary,
                    )
                rows.append(
                    {
                        "fda_manufacturer_id": manufacturer_id,
                        "manufacturer_name": manufacturer_name,
                        "mapped_ticker": match.ticker,
                        "mapped_company_name": match.company_name,
                        "mapping_confidence": match.confidence,
                        "mapping_method": match.method,
                        "matched_alias": match.matched_alias,
                        "matched_alias_source": match.matched_alias_source,
                        "second_best_ticker": match.second_best_ticker,
                        "second_best_score": match.second_best_score if match.second_best_score is not None else "",
                        "candidate_summary": match.candidate_summary,
                        "manual_override_used": match.manual_override_used,
                        "approval_rows": approval_rows,
                        "recall_rows": recall_rows,
                        "adverse_event_rows": adverse_rows,
                        "inspection_rows": inspection_rows,
                        "compliance_rows": compliance_rows,
                        "total_fda_rows": total_rows,
                        "high_volume_unmapped": high_volume_unmapped,
                        "review_reason": match.review_reason,
                    }
                )
            if update_facts:
                update_fact_company_ids(conn, min_confidence=min_confidence)
                product_line_link_counts = apply_product_line_fact_links(conn, product_line_overrides)
                footprint_link_counts = apply_footprint_fact_links(conn, footprints)
            else:
                product_line_link_counts = {
                    "fact_fda_approval": 0,
                    "fact_fda_recall": 0,
                    "fact_fda_adverse_event": 0,
                }
                footprint_link_counts = {
                    "approval_rows": 0,
                    "manufacturer_rows": 0,
                    "inspection_rows": 0,
                    "compliance_rows": 0,
                    "conflict_rows": 0,
                    "unconfirmed_approval_rows": 0,
                }
            write_csv(output_csv, rows)
            message = (
                f"manufacturers={len(rows)} mapped={mapped} ambiguous={ambiguous} "
                f"high_volume_unmapped={high_volume_unmapped_count} aliases={len(aliases)} "
                f"manual_overrides={len(manual_overrides)} footprint_links={footprint_link_counts} "
                f"product_line_links={product_line_link_counts} "
                f"min_confidence={min_confidence} output={output_csv}"
            )
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=message)
            LOGGER.info("FDA entity linking complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
