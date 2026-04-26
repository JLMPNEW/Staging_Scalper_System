from __future__ import annotations

"""
Second-stage SEC snapshot gap resolver.

Why this exists
---------------
The main snapshot builder fixes point-in-time row selection and broad taxonomy
mapping. The remaining unresolved names usually fall into two buckets:

1) issuer-specific extension concepts that are present in the same filing but are
   not covered by the generic mapping registry
2) economically real "not applicable" cases, especially pre-commercial biotech
   issuers that do not yet report periodic revenue

This resolver is deliberately config-driven:
- exact issuer overrides can be loaded from CSV
- industry/sector pattern rules can be loaded from YAML
- applicability policies can reclassify gaps without inventing data
- unresolved same-filing concepts are exported as candidate overrides for review

The resolver is meant to run AFTER sec_tier1_snapshot_enhanced.py and write a
resolved table/view for downstream scoring, diagnostics, and mapping curation.

Outputs
-------
- updated snapshot rows with repaired metric_status_json / metric_provenance_json
- metric_applicability_json
- effective_missing_metric_count
- effective_any_core_metric_missing
- optional candidate CSV for future issuer-specific override additions

Important
---------
This script does not fabricate values. It only:
- uses same-filing facts already present in raw XBRL fact tables
- applies high-confidence rules or exact issuer overrides
- marks some null metrics as not applicable when policy explicitly allows it
"""

import argparse
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

import numpy as np
import pandas as pd
from sec_fundamentals_config import normalize_cik_10d, validate_sql_identifier

try:
    import yaml
    HAVE_YAML = True
except Exception:  # pragma: no cover
    yaml = None
    HAVE_YAML = False

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import Engine
    HAVE_SQLALCHEMY = True
except Exception:  # pragma: no cover
    create_engine = None
    text = None
    Engine = Any  # type: ignore[misc,assignment]
    HAVE_SQLALCHEMY = False

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    tqdm = None

LOGGER = logging.getLogger("sec_snapshot_gap_resolver")
SQLITE_BUSY_TIMEOUT_MS = 30000

CORE_METRICS = [
    "revenue",
    "net_income",
    "operating_cash_flow",
    "total_assets",
    "total_equity",
]

PRIOR_FILING_FALLBACK_CONCEPTS: Dict[str, Tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesAndTransfersOfOilAndGasProducedNetOfProductionCosts",
        "OperatingRevenue",
        "RegulatedOperatingRevenue",
        "RegulatedAndUnregulatedOperatingRevenue",
        "RentalRevenue",
        "OperatingLeaseLeaseIncome",
        "InterestAndDividendIncomeOperating",
        "RevenuesNetOfInterestExpense",
        "InterestAndFeeIncomeLoansAndLeases",
        "InterestAndFeeIncomeLoansAndLeasesHeldInPortfolio",
        "InterestAndFeeIncomeLoansCommercialRealEstate",
        "InterestIncomeOperating",
    ),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetIncomeLossAvailableToCommonStockholdersDiluted",
    ),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashFromOperatingActivities",
    ),
    "total_assets": (
        "Assets",
        "AssetsNet",
    ),
    "total_equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "CommonStockholdersEquity",
    ),
}


def normalize_ticker(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    out = str(value).strip().upper()
    return out or None


def normalize_cik(value: Any) -> Optional[str]:
    return normalize_cik_10d(value)


def _validate_sql_table_name(name: str, label: str) -> str:
    return validate_sql_identifier(name, label, allow_dotted=True)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def canonical_label(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    # Keep both full and post-prefix parts comparable (e.g. "Healthcare: X").
    parts = [p.strip() for p in raw.split(":") if p.strip()]
    if not parts:
        return raw
    return " ".join(parts)


def json_load_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            preview = value[:200]
            if len(value) > 200:
                preview += "..."
            LOGGER.debug("Malformed JSON value: %r", preview)
            return {}
    return {}


def json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if value is pd.NA:
        return None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass
class ResolverConfig:
    snapshot_table: str = "sec_fundamental_snapshot_filled_security_t1"
    facts_table: str = "sec_xbrl_facts_raw"
    output_table: str = "sec_fundamental_snapshot_filled_security_t1_resolved"
    candidate_table: Optional[str] = None

    # Optional file inputs
    snapshot_csv: Optional[str] = None
    facts_csv: Optional[str] = None
    extension_rule_yaml: Optional[str] = None
    applicability_yaml: Optional[str] = None
    issuer_override_csv: Optional[str] = None

    # DB / filtering
    as_of_date: Optional[str] = None
    prior_filing_fallback_enabled: bool = True
    prior_filing_max_staleness_days: int = 550

    # Snapshot column names
    ticker_col: str = "ticker"
    cik_col: str = "cik"
    accession_col: str = "accession_number"
    report_period_end_col: str = "anchor_period_end"
    sector_col: str = "sector"
    industry_col: str = "industry"
    industry_aggregate_col: str = "industry_aggregate"
    metric_status_json_col: str = "metric_status_json"
    metric_prov_json_col: str = "metric_provenance_json"
    null_reason_col: str = "null_reason"

    # Fact column names
    fact_cik_col: str = "cik"
    fact_accession_col: str = "accession_number"
    fact_taxonomy_col: str = "taxonomy"
    fact_concept_col: str = "tag"
    fact_value_col: str = "value_num"
    fact_period_end_col: str = "report_period_end"
    fact_period_start_col: str = "period_start"
    fact_filed_date_col: str = "filed_date"
    fact_period_type_col: str = "period_type"
    fact_dimension_count_col: str = "dimension_count"
    fact_context_col: str = "context_id"
    fact_statement_col: str = "statement_type"


@dataclass(frozen=True)
class ExtensionRule:
    rule_id: str
    metric_name: str
    taxonomies: Tuple[str, ...]
    exact_concepts: Tuple[str, ...] = ()
    regex_patterns: Tuple[str, ...] = ()
    period_type: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    industry_regex: Optional[str] = None
    industry_aggregate: Optional[str] = None
    industry_aggregate_aliases: Tuple[str, ...] = ()
    cik: Optional[str] = None
    ticker: Optional[str] = None
    priority: int = 100
    auto_apply: bool = True
    confidence: str = "high"
    source_kind: str = "direct"
    note: Optional[str] = None


@dataclass(frozen=True)
class ApplicabilityPolicy:
    policy_id: str
    metric_name: str
    action: str
    sector: Optional[str] = None
    industry: Optional[str] = None
    industry_regex: Optional[str] = None
    industry_aggregate: Optional[str] = None
    industry_aggregate_aliases: Tuple[str, ...] = ()
    cik: Optional[str] = None
    ticker: Optional[str] = None
    require_statuses: Tuple[str, ...] = ("mapping_gap_same_filing", "missing")
    applicable: Optional[bool] = None
    note: Optional[str] = None


class SnapshotGapResolver:
    def __init__(
        self,
        config: ResolverConfig,
        engine: Optional[Engine] = None,
        sqlite_conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.config = config
        self._validate_runtime_identifiers()
        self.engine = engine
        self.sqlite_conn = sqlite_conn
        self._facts_table_columns_cache: Optional[set[str]] = None
        self.extension_rules = self._load_extension_rules()
        self.applicability_policies = self._load_applicability_policies()

    def _validate_runtime_identifiers(self) -> None:
        self.config.snapshot_table = _validate_sql_table_name(
            self.config.snapshot_table,
            "snapshot_table",
        )
        self.config.facts_table = _validate_sql_table_name(
            self.config.facts_table,
            "facts_table",
        )
        self.config.output_table = _validate_sql_table_name(
            self.config.output_table,
            "output_table",
        )
        if self.config.candidate_table:
            self.config.candidate_table = _validate_sql_table_name(
                self.config.candidate_table,
                "candidate_table",
            )
        self.config.fact_cik_col = validate_sql_identifier(self.config.fact_cik_col, "fact_cik_col")
        self.config.fact_accession_col = validate_sql_identifier(
            self.config.fact_accession_col,
            "fact_accession_col",
        )
        self.config.fact_taxonomy_col = validate_sql_identifier(
            self.config.fact_taxonomy_col,
            "fact_taxonomy_col",
        )
        self.config.fact_concept_col = validate_sql_identifier(
            self.config.fact_concept_col,
            "fact_concept_col",
        )
        self.config.fact_value_col = validate_sql_identifier(
            self.config.fact_value_col,
            "fact_value_col",
        )
        self.config.fact_period_end_col = validate_sql_identifier(
            self.config.fact_period_end_col,
            "fact_period_end_col",
        )
        self.config.fact_period_start_col = validate_sql_identifier(
            self.config.fact_period_start_col,
            "fact_period_start_col",
        )
        self.config.fact_filed_date_col = validate_sql_identifier(
            self.config.fact_filed_date_col,
            "fact_filed_date_col",
        )
        self.config.fact_period_type_col = validate_sql_identifier(
            self.config.fact_period_type_col,
            "fact_period_type_col",
        )
        self.config.fact_dimension_count_col = validate_sql_identifier(
            self.config.fact_dimension_count_col,
            "fact_dimension_count_col",
        )
        self.config.fact_context_col = validate_sql_identifier(
            self.config.fact_context_col,
            "fact_context_col",
        )
        self.config.fact_statement_col = validate_sql_identifier(
            self.config.fact_statement_col,
            "fact_statement_col",
        )

    # -----------------------------
    # Public API
    # -----------------------------

    def run(
        self,
        persist: bool = False,
        candidate_csv: Optional[str] = None,
        missing_tickers_csv: Optional[str] = None,
        resolved_csv: Optional[str] = None,
        summary_csv: Optional[str] = None,
    ) -> Dict[str, pd.DataFrame]:
        snapshot_df = self._load_snapshot_df()
        if snapshot_df.empty:
            raise ValueError("No snapshot rows found for the requested inputs.")
        snapshot_df = self._normalize_snapshot_df(snapshot_df)

        unresolved_mask = self._build_unresolved_mask(snapshot_df)
        unresolved_df = snapshot_df.loc[unresolved_mask].copy()
        LOGGER.info("Loaded %d snapshot rows; %d unresolved candidates", len(snapshot_df), len(unresolved_df))

        facts_df = self._load_facts_df(unresolved_df)
        facts_df = self._normalize_facts_df(facts_df)
        facts_by_accession = {
            acc: grp.copy()
            for acc, grp in facts_df.groupby("accession_number", dropna=False)
        } if not facts_df.empty else {}
        facts_by_cik: Dict[str, pd.DataFrame] = {}
        if self.config.prior_filing_fallback_enabled and not unresolved_df.empty:
            fallback_facts = self._load_facts_by_cik_df(unresolved_df)
            fallback_facts = self._normalize_facts_df(fallback_facts)
            if not fallback_facts.empty:
                facts_by_cik = {
                    str(normalize_cik(cik) or ""): grp.copy()
                    for cik, grp in fallback_facts.groupby("cik", dropna=False)
                }

        override_rules = self._load_issuer_override_rules()
        all_rules = self.extension_rules + override_rules

        candidate_records: List[Dict[str, Any]] = []
        stats = {
            "rows_total": int(len(snapshot_df)),
            "rows_unresolved_in": int(len(unresolved_df)),
            "rows_resolved_numeric": 0,
            "rows_reclassified_applicability": 0,
            "metrics_resolved_numeric": 0,
            "metrics_reclassified_applicability": 0,
        }

        index_to_row = {int(i): row for i, row in snapshot_df.iterrows()}
        out_rows: Dict[int, Dict[str, Any]] = {i: dict(row) for i, row in index_to_row.items()}

        row_iter: Iterable[tuple[Any, Any]]
        row_iter = unresolved_df.iterrows()
        if tqdm is not None and not unresolved_df.empty:
            row_iter = tqdm(row_iter, total=len(unresolved_df), desc="Resolving metrics")
        for idx, row in row_iter:
            row_cik = normalize_cik(row.get("cik"))
            resolved, candidates, row_stats = self._resolve_single_row(
                dict(row),
                facts_by_accession.get(row["accession_number"], pd.DataFrame()),
                facts_by_cik.get(str(row_cik or ""), pd.DataFrame()),
                all_rules,
            )
            out_rows[int(idx)] = resolved
            candidate_records.extend(candidates)
            for key, value in row_stats.items():
                stats[key] = stats.get(key, 0) + int(value)

        resolved_df = pd.DataFrame(list(out_rows.values()))
        resolved_df = self._recompute_effective_gap_columns(resolved_df)

        candidate_df = pd.DataFrame(candidate_records)
        if not candidate_df.empty:
            candidate_df = candidate_df.sort_values(
                ["metric_name", "ticker", "taxonomy", "concept_name"],
                ascending=[True, True, True, True],
            ).reset_index(drop=True)

        summary_df = self._build_summary_df(snapshot_df, resolved_df)
        missing_tickers_df = self._build_missing_tickers_df(resolved_df)
        LOGGER.info("Resolver stats: %s", json.dumps(stats, sort_keys=True))
        LOGGER.info("Summary:\n%s", summary_df.to_string(index=False))

        if candidate_csv:
            candidate_df.to_csv(candidate_csv, index=False)
            LOGGER.info("Wrote candidate CSV: %s", candidate_csv)

        if missing_tickers_csv:
            missing_tickers_df.to_csv(missing_tickers_csv, index=False)
            LOGGER.info("Wrote missing-tickers CSV: %s", missing_tickers_csv)

        if resolved_csv:
            resolved_df.to_csv(resolved_csv, index=False)
            LOGGER.info("Wrote resolved CSV: %s", resolved_csv)

        if summary_csv:
            summary_df.to_csv(summary_csv, index=False)
            LOGGER.info("Wrote summary CSV: %s", summary_csv)

        if persist:
            self._persist_outputs(resolved_df, candidate_df)

        return {
            "resolved_df": resolved_df,
            "candidate_df": candidate_df,
            "missing_tickers_df": missing_tickers_df,
            "summary_df": summary_df,
        }

    # -----------------------------
    # Loaders
    # -----------------------------

    def _table_columns(self, table_name: str) -> set[str]:
        if self._facts_table_columns_cache is not None and table_name == self.config.facts_table:
            return self._facts_table_columns_cache
        columns: set[str] = set()
        if self.sqlite_conn is not None:
            cur = self.sqlite_conn.cursor()
            cur.execute(f"SELECT * FROM {table_name} LIMIT 0")
            columns = {
                str(desc[0])
                for desc in (cur.description or [])
                if desc and desc[0]
            }
        elif self.engine is not None:
            if text is None:
                raise RuntimeError("SQLAlchemy text() unavailable")
            with self.engine.connect() as conn:
                result = conn.execute(text(f"SELECT * FROM {table_name} LIMIT 0"))
                columns = {str(col) for col in result.keys()}
        if table_name == self.config.facts_table:
            self._facts_table_columns_cache = columns
        return columns

    def _table_exists_any(self, table_name: str) -> bool:
        if self.sqlite_conn is not None:
            return _table_exists(self.sqlite_conn, table_name)
        if self.engine is None:
            return False
        if text is None:
            raise RuntimeError("SQLAlchemy text() unavailable")
        try:
            with self.engine.connect() as conn:
                conn.execute(text(f"SELECT * FROM {table_name} LIMIT 0"))
            return True
        except Exception:
            return False

    def _fact_select_columns(self, *, for_prior_fallback: bool = False) -> List[str]:
        columns = [
            self.config.fact_cik_col,
            self.config.fact_accession_col,
            self.config.fact_taxonomy_col,
            self.config.fact_concept_col,
            self.config.fact_value_col,
            self.config.fact_period_end_col,
            self.config.fact_filed_date_col,
        ]
        if not for_prior_fallback:
            columns.extend(
                [
                    self.config.fact_period_start_col,
                    self.config.fact_period_type_col,
                    self.config.fact_dimension_count_col,
                    self.config.fact_context_col,
                    self.config.fact_statement_col,
                ]
            )
        # Preserve order while avoiding duplicate projections if config aliases collide.
        ordered = list(dict.fromkeys(columns))
        if self.engine is None and self.sqlite_conn is None:
            return ordered
        available = self._table_columns(self.config.facts_table)
        if not available:
            return ordered
        return [col for col in ordered if col in available]

    def _load_snapshot_df(self) -> pd.DataFrame:
        if self.config.snapshot_csv:
            df = pd.read_csv(self.config.snapshot_csv)
            if self.config.as_of_date and "as_of_date" in df.columns:
                df = df[df["as_of_date"].astype(str) == str(self.config.as_of_date)].copy()
            return df
        if self.engine is None and self.sqlite_conn is None:
            raise ValueError("Provide snapshot_csv or a DB connection")
        sql = f"SELECT * FROM {self.config.snapshot_table}"
        params: Dict[str, Any] = {}
        if self.config.as_of_date:
            sql += " WHERE as_of_date = :as_of_date"
            params = {"as_of_date": self.config.as_of_date}
        return self._read_sql_df(sql, params=params)

    def _load_facts_df(self, unresolved_df: pd.DataFrame) -> pd.DataFrame:
        if unresolved_df.empty:
            return pd.DataFrame()
        accessions = sorted({str(x) for x in unresolved_df["accession_number"].dropna().astype(str).unique() if str(x).strip()})
        if not accessions:
            return pd.DataFrame()
        if self.config.facts_csv:
            df = pd.read_csv(self.config.facts_csv)
            if self.config.fact_accession_col not in df.columns:
                raise ValueError(f"facts_csv missing accession column {self.config.fact_accession_col!r}")
            df[self.config.fact_accession_col] = df[self.config.fact_accession_col].astype(str)
            return df[df[self.config.fact_accession_col].isin(accessions)].copy()
        if self.engine is None and self.sqlite_conn is None:
            raise ValueError("Provide facts_csv or a DB connection")

        pieces: List[pd.DataFrame] = []
        chunk = 500
        select_cols = ", ".join(self._fact_select_columns())
        for i in range(0, len(accessions), chunk):
            part = accessions[i:i + chunk]
            placeholders = ", ".join("?" for _ in part)
            sql = (
                f"SELECT {select_cols} FROM {self.config.facts_table} "
                f"WHERE CAST({self.config.fact_accession_col} AS TEXT) IN ({placeholders})"
            )
            pieces.append(self._read_sql_df(sql, params={f"p{j}": v for j, v in enumerate(part)}))
        return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()

    def _load_facts_by_cik_df(self, unresolved_df: pd.DataFrame) -> pd.DataFrame:
        if unresolved_df.empty:
            return pd.DataFrame()
        cik_ints = sorted({
            int(normalize_cik(x)) for x in unresolved_df["cik"].dropna().astype(str).unique() if normalize_cik(x)
        })
        if not cik_ints:
            return pd.DataFrame()
        if self.engine is None and self.sqlite_conn is None:
            raise ValueError("Provide DB connection for prior-filing fallback facts")
        fallback_concepts = sorted(
            {
                concept
                for concepts in PRIOR_FILING_FALLBACK_CONCEPTS.values()
                for concept in concepts
                if str(concept).strip()
            }
        )
        if not fallback_concepts:
            return pd.DataFrame()
        pieces: List[pd.DataFrame] = []
        chunk = 100
        cutoff: pd.Timestamp | None = None
        if self.config.as_of_date:
            cutoff_ts = pd.to_datetime(self.config.as_of_date, errors="coerce", utc=True)
            if pd.notna(cutoff_ts):
                cutoff = cutoff_ts.normalize()
        select_cols = ", ".join(self._fact_select_columns(for_prior_fallback=True))
        for i in range(0, len(cik_ints), chunk):
            part = cik_ints[i:i + chunk]
            cik_placeholders = ", ".join("?" for _ in part)
            concept_placeholders = ", ".join("?" for _ in fallback_concepts)
            sql = (
                f"SELECT {select_cols} FROM {self.config.facts_table} "
                f"WHERE CAST({self.config.fact_cik_col} AS INTEGER) IN ({cik_placeholders}) "
                f"AND LOWER(CAST({self.config.fact_concept_col} AS TEXT)) IN ({concept_placeholders})"
            )
            params = {f"p{j}": v for j, v in enumerate(part)}
            offset = len(part)
            for j, concept in enumerate(fallback_concepts):
                params[f"p{offset + j}"] = str(concept).lower()
            chunk_df = self._read_sql_df(sql, params=params)
            if chunk_df.empty:
                continue
            if self.config.fact_filed_date_col in chunk_df.columns and cutoff is not None:
                filed = pd.to_datetime(
                    chunk_df[self.config.fact_filed_date_col],
                    errors="coerce",
                    utc=True,
                ).dt.normalize()
                chunk_df = chunk_df[(filed.isna()) | (filed <= cutoff)].copy()
            if not chunk_df.empty:
                pieces.append(chunk_df)
        out = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
        return out

    def _read_sql_df(self, sql: str, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
        params = params or {}
        param_keys = list(params.keys())
        if param_keys and all(re.fullmatch(r"p\d+", k) for k in param_keys):
            param_keys = sorted(param_keys, key=lambda k: int(k[1:]))
        if self.sqlite_conn is not None:
            if "?" in sql:
                positional: List[Any] = [params[k] for k in param_keys]
                return pd.read_sql_query(sql, self.sqlite_conn, params=positional)
            return pd.read_sql_query(sql, self.sqlite_conn, params=params)
        if self.engine is None:
            raise ValueError("DB connection unavailable")
        if text is None:
            raise RuntimeError("SQLAlchemy text() unavailable")
        named = {k: v for k, v in params.items()}
        named_sql = sql
        if "?" in sql:
            for key in param_keys:
                named_sql = named_sql.replace("?", f":{key}", 1)
        return pd.read_sql(text(named_sql), self.engine, params=named)

    def _load_extension_rules(self) -> List[ExtensionRule]:
        path = self.config.extension_rule_yaml
        if not path:
            return []
        payload = self._read_yaml(path)
        out: List[ExtensionRule] = []
        for item in payload.get("auto_apply_rules", []) or []:
            rule = self._parse_rule(item, default_auto_apply=True)
            if rule is not None:
                out.append(rule)
        for item in payload.get("candidate_rules", []) or []:
            rule = self._parse_rule(item, default_auto_apply=False)
            if rule is not None:
                out.append(rule)
        return sorted(out, key=lambda x: (x.metric_name, x.priority, x.rule_id))

    def _load_issuer_override_rules(self) -> List[ExtensionRule]:
        path = self.config.issuer_override_csv
        if not path:
            return []
        df = pd.read_csv(path)
        if df.empty:
            return []

        rules: List[ExtensionRule] = []
        for _, row in df.iterrows():
            enabled = self._text_or_default(row.get("enabled"), "true").lower()
            if enabled in {"0", "false", "no", "n"}:
                continue
            metric_name = self._none_if_blank(row.get("metric_name")) or ""
            taxonomy = (self._none_if_blank(row.get("taxonomy")) or "").lower()
            concept_name = self._none_if_blank(row.get("concept_name")) or ""
            if not metric_name or not taxonomy or not concept_name:
                continue
            priority_raw = pd.to_numeric(row.get("priority", 50), errors="coerce")
            rule = ExtensionRule(
                rule_id=self._text_or_default(
                    row.get("rule_id"),
                    f"issuer_override_{metric_name}_{concept_name}",
                ),
                metric_name=metric_name,
                taxonomies=(taxonomy,),
                exact_concepts=(concept_name,),
                regex_patterns=(),
                period_type=self._none_if_blank(row.get("period_type")),
                sector=self._none_if_blank(row.get("sector")),
                industry=self._none_if_blank(row.get("industry")),
                industry_regex=self._none_if_blank(row.get("industry_regex")),
                industry_aggregate=self._none_if_blank(row.get("industry_aggregate")),
                industry_aggregate_aliases=self._split_aliases(row.get("industry_aggregate_aliases")),
                cik=normalize_cik(row.get("cik")),
                ticker=normalize_ticker(row.get("ticker")),
                priority=int(priority_raw) if pd.notna(priority_raw) else 50,
                auto_apply=self._text_or_default(row.get("auto_apply"), "true").lower() not in {"0", "false", "no", "n"},
                confidence=self._text_or_default(row.get("confidence"), "high").lower(),
                source_kind=self._text_or_default(row.get("source_kind"), "direct").lower(),
                note=self._none_if_blank(row.get("note")),
            )
            rules.append(rule)
        return sorted(rules, key=lambda x: (x.metric_name, x.priority, x.rule_id))

    def _load_applicability_policies(self) -> List[ApplicabilityPolicy]:
        path = self.config.applicability_yaml
        if not path:
            return []
        payload = self._read_yaml(path)
        out: List[ApplicabilityPolicy] = []
        for item in payload.get("policies", []) or []:
            enabled = bool(item.get("enabled", True))
            if not enabled:
                continue
            metric_name = str(item.get("metric_name", "")).strip()
            action = str(item.get("action", "")).strip()
            if not metric_name or not action:
                continue
            req = item.get("require_statuses") or ["mapping_gap_same_filing", "missing"]
            out.append(
                ApplicabilityPolicy(
                    policy_id=self._text_or_default(item.get("policy_id"), f"policy_{metric_name}_{action}"),
                    metric_name=metric_name,
                    action=action,
                    sector=self._none_if_blank(item.get("sector")),
                    industry=self._none_if_blank(item.get("industry")),
                    industry_regex=self._none_if_blank(item.get("industry_regex")),
                    industry_aggregate=self._none_if_blank(item.get("industry_aggregate")),
                    industry_aggregate_aliases=self._split_aliases(item.get("industry_aggregate_aliases")),
                    cik=normalize_cik(item.get("cik")),
                    ticker=normalize_ticker(item.get("ticker")),
                    require_statuses=tuple(str(x) for x in req),
                    applicable=(
                        None
                        if item.get("applicable", None) is None
                        else bool(item.get("applicable"))
                    ),
                    note=self._none_if_blank(item.get("note")),
                )
            )
        return out

    # -----------------------------
    # Normalization
    # -----------------------------

    def _normalize_snapshot_df(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        for col in [self.config.ticker_col, self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]:
            if col not in out.columns:
                out[col] = None
        for metric in CORE_METRICS:
            if metric not in out.columns:
                out[metric] = pd.NA
        if self.config.cik_col not in out.columns:
            out[self.config.cik_col] = None
        out["ticker"] = out[self.config.ticker_col].map(normalize_ticker)
        out["cik"] = out[self.config.cik_col].map(normalize_cik)
        out["accession_number"] = out[self.config.accession_col].astype(str)
        out["sector"] = out[self.config.sector_col].where(out[self.config.sector_col].notna(), None)
        out["industry"] = out[self.config.industry_col].where(out[self.config.industry_col].notna(), None)
        out["industry_aggregate"] = out[self.config.industry_aggregate_col].where(out[self.config.industry_aggregate_col].notna(), None)
        if self.config.report_period_end_col in out.columns:
            out["report_period_end"] = pd.to_datetime(out[self.config.report_period_end_col], errors="coerce", utc=True).dt.normalize()
        else:
            out["report_period_end"] = pd.NaT
        if self.config.metric_status_json_col not in out.columns:
            out[self.config.metric_status_json_col] = "{}"
        if self.config.metric_prov_json_col not in out.columns:
            out[self.config.metric_prov_json_col] = "{}"
        return out

    def _normalize_facts_df(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=[
                "cik", "accession_number", "taxonomy", "concept_name", "concept_key",
                "fact_value", "period_end", "period_type", "dimension_count", "context_id",
                "statement_type",
            ])
        out = df.copy()
        out["cik"] = out[self.config.fact_cik_col].map(normalize_cik) if self.config.fact_cik_col in out.columns else None
        out["accession_number"] = out[self.config.fact_accession_col].astype(str)
        out["taxonomy"] = out[self.config.fact_taxonomy_col].astype(str).str.lower() if self.config.fact_taxonomy_col in out.columns else None
        out["concept_name"] = out[self.config.fact_concept_col].astype(str)
        out["concept_key"] = out["concept_name"].str.lower()
        out["fact_value"] = pd.to_numeric(out[self.config.fact_value_col], errors="coerce")
        out = out[out["fact_value"].notna()].copy()
        if self.config.fact_period_end_col in out.columns:
            out["period_end"] = pd.to_datetime(out[self.config.fact_period_end_col], errors="coerce", utc=True).dt.normalize()
        else:
            out["period_end"] = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
        if self.config.fact_period_start_col in out.columns:
            out["period_start"] = pd.to_datetime(out[self.config.fact_period_start_col], errors="coerce", utc=True).dt.normalize()
        else:
            out["period_start"] = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
        if self.config.fact_filed_date_col in out.columns:
            out["filed_date"] = pd.to_datetime(out[self.config.fact_filed_date_col], errors="coerce", utc=True).dt.normalize()
        else:
            out["filed_date"] = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
        if self.config.fact_period_type_col in out.columns:
            out["period_type"] = out[self.config.fact_period_type_col].astype(str).str.lower()
        else:
            out["period_type"] = None
            # Derive a period type fallback from period_start/period_end when source column is absent.
            period_end = pd.to_datetime(out["period_end"], errors="coerce", utc=True)
            period_start = pd.to_datetime(out["period_start"], errors="coerce", utc=True)
            dur = (period_end - period_start).dt.days
            out.loc[dur.fillna(0).gt(0), "period_type"] = "duration"
            out.loc[dur.fillna(0).le(0), "period_type"] = "instant"
        if self.config.fact_dimension_count_col in out.columns:
            out["dimension_count"] = pd.to_numeric(out[self.config.fact_dimension_count_col], errors="coerce").fillna(0).astype(int)
        else:
            out["dimension_count"] = 0
        if self.config.fact_context_col in out.columns:
            out["context_id"] = out[self.config.fact_context_col].where(out[self.config.fact_context_col].notna(), None)
        else:
            out["context_id"] = None
        if self.config.fact_statement_col in out.columns:
            out["statement_type"] = out[self.config.fact_statement_col].where(out[self.config.fact_statement_col].notna(), None)
        else:
            out["statement_type"] = None
        return out.reset_index(drop=True)

    # -----------------------------
    # Core resolution logic
    # -----------------------------

    def _build_unresolved_mask(self, snapshot_df: pd.DataFrame) -> pd.Series:
        mask = pd.Series(False, index=snapshot_df.index)
        for metric in CORE_METRICS:
            mask = mask | snapshot_df.get(metric, pd.Series(pd.NA, index=snapshot_df.index)).isna()
        if self.config.null_reason_col in snapshot_df.columns:
            mask = mask & (snapshot_df[self.config.null_reason_col].fillna("") != "no_eligible_source_row_before_as_of")
        return mask

    def _resolve_single_row(
        self,
        row: Dict[str, Any],
        facts_for_accession: pd.DataFrame,
        facts_for_cik: pd.DataFrame,
        rules: Sequence[ExtensionRule],
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, int]]:
        stats = {
            "rows_resolved_numeric": 0,
            "rows_reclassified_applicability": 0,
            "metrics_resolved_numeric": 0,
            "metrics_reclassified_applicability": 0,
        }
        candidates: List[Dict[str, Any]] = []
        status_map = json_load_dict(row.get(self.config.metric_status_json_col))
        prov_map = json_load_dict(row.get(self.config.metric_prov_json_col))
        applicability_map = json_load_dict(row.get("metric_applicability_json"))
        if not applicability_map:
            applicability_map = {m: True for m in CORE_METRICS}

        numeric_resolved_this_row = 0
        applicability_reclassified_this_row = 0

        for metric in CORE_METRICS:
            value = row.get(metric)
            if pd.notna(value):
                applicability_map.setdefault(metric, True)
                continue

            prior_status = str(status_map.get(metric, "missing"))
            if prior_status == "no_eligible_source_row_before_as_of":
                applicability_map.setdefault(metric, True)
                continue

            # Phase 1: same-filing exact/pattern resolution.
            match = self._find_same_filing_rule_match(row, metric, facts_for_accession, rules)
            if match and match.get("auto_apply", True):
                LOGGER.debug(
                    "Resolver same-filing match: ticker=%s cik=%s metric=%s taxonomy=%s concept=%s rule_id=%s",
                    row.get("ticker"),
                    row.get("cik"),
                    metric,
                    match.get("taxonomy"),
                    match.get("concept_name"),
                    match.get("rule_id"),
                )
                row[metric] = match["fact_value"]
                status_map[metric] = "mapped_same_filing_extension_rule"
                prov_map[metric] = {
                    "status": "mapped_same_filing_extension_rule",
                    "metric_name": metric,
                    "prior_status": prior_status,
                    "rule_id": match["rule_id"],
                    "taxonomy": match["taxonomy"],
                    "concept_name": match["concept_name"],
                    "context_id": match.get("context_id"),
                    "statement_type": match.get("statement_type"),
                    "period_end": match.get("period_end"),
                    "industry_aggregate": row.get("industry_aggregate"),
                    "source_stage": "gap_resolver",
                    "note": match.get("note"),
                }
                applicability_map[metric] = True
                numeric_resolved_this_row += 1
                stats["metrics_resolved_numeric"] += 1
                continue

            if match and not match.get("auto_apply", True):
                candidates.append(self._candidate_record_from_match(row, metric, match, reason="candidate_rule_match_review"))

            # Phase 2: unresolved concept harvesting for manual curation.
            candidates.extend(self._harvest_candidates(row, metric, facts_for_accession, prior_status))

            # Phase 3: applicability reclassification.
            policy = self._find_applicability_policy(row, metric, prior_status)
            if policy is not None:
                LOGGER.debug(
                    "Resolver applicability policy: ticker=%s cik=%s metric=%s policy_id=%s action=%s prior_status=%s",
                    row.get("ticker"),
                    row.get("cik"),
                    metric,
                    policy.policy_id,
                    policy.action,
                    prior_status,
                )
                status_map[metric] = policy.action
                prov_map[metric] = {
                    "status": policy.action,
                    "metric_name": metric,
                    "prior_status": prior_status,
                    "policy_id": policy.policy_id,
                    "source_stage": "gap_resolver",
                    "note": policy.note,
                }
                if policy.applicable is not None:
                    applicability_map[metric] = bool(policy.applicable)
                else:
                    applicability_map[metric] = False if policy.action.startswith("not_applicable") else True
                applicability_reclassified_this_row += 1
                stats["metrics_reclassified_applicability"] += 1
                continue

            # Phase 4: prior-filing carry-forward fallback within staleness window.
            if self.config.prior_filing_fallback_enabled:
                prior_match = self._find_prior_filing_fallback(row, metric, facts_for_cik)
                if prior_match is not None:
                    LOGGER.debug(
                        "Resolver prior-filing fallback: ticker=%s cik=%s metric=%s accession=%s concept=%s staleness_days=%s",
                        row.get("ticker"),
                        row.get("cik"),
                        metric,
                        prior_match.get("accession_number"),
                        prior_match.get("concept_name"),
                        prior_match.get("staleness_days"),
                    )
                    row[metric] = prior_match["fact_value"]
                    status_map[metric] = "mapped_prior_filing_carry_forward"
                    prov_map[metric] = {
                        "status": "mapped_prior_filing_carry_forward",
                        "metric_name": metric,
                        "prior_status": prior_status,
                        "source_stage": "gap_resolver",
                        "source_accession_number": prior_match.get("accession_number"),
                        "taxonomy": prior_match.get("taxonomy"),
                        "concept_name": prior_match.get("concept_name"),
                        "period_end": prior_match.get("period_end"),
                        "filed_date": prior_match.get("filed_date"),
                        "staleness_days": prior_match.get("staleness_days"),
                        "note": "Used latest prior reported value within configured staleness window.",
                    }
                    applicability_map[metric] = True
                    numeric_resolved_this_row += 1
                    stats["metrics_resolved_numeric"] += 1
                    continue

            applicability_map.setdefault(metric, True)

        if numeric_resolved_this_row > 0:
            stats["rows_resolved_numeric"] += 1
        if applicability_reclassified_this_row > 0:
            stats["rows_reclassified_applicability"] += 1

        row[self.config.metric_status_json_col] = json_dumps(status_map)
        row[self.config.metric_prov_json_col] = json_dumps(prov_map)
        row["metric_applicability_json"] = json_dumps(applicability_map)
        row["gap_resolver_applied"] = int(numeric_resolved_this_row > 0 or applicability_reclassified_this_row > 0)
        return row, candidates, stats

    def _find_same_filing_rule_match(
        self,
        row: Mapping[str, Any],
        metric: str,
        facts_for_accession: pd.DataFrame,
        rules: Sequence[ExtensionRule],
    ) -> Optional[Dict[str, Any]]:
        if facts_for_accession is None or facts_for_accession.empty:
            return None
        applicable_rules = [r for r in rules if r.metric_name == metric and self._rule_matches_row(r, row)]
        if not applicable_rules:
            return None

        period_end = row.get("report_period_end")
        facts = facts_for_accession.copy()
        if period_end is not None and not pd.isna(period_end):
            facts["period_distance"] = (facts["period_end"] - period_end).dt.days.abs()
            facts["period_distance"] = facts["period_distance"].fillna(999999)
        else:
            facts["period_distance"] = 999999

        best_match: Optional[Dict[str, Any]] = None
        for rule in sorted(applicable_rules, key=lambda r: (r.priority, not r.auto_apply, r.rule_id)):
            filtered = facts.copy()
            if rule.taxonomies and "*" not in rule.taxonomies:
                filtered = filtered[filtered["taxonomy"].isin(set(rule.taxonomies))].copy()
            if rule.period_type:
                filtered = filtered[(filtered["period_type"].isna()) | (filtered["period_type"] == str(rule.period_type).lower())].copy()
            if filtered.empty:
                continue

            concept_lower = filtered["concept_key"].astype(str)
            mask = pd.Series(False, index=filtered.index)
            if rule.exact_concepts:
                exact_set = {c.lower() for c in rule.exact_concepts}
                mask = mask | concept_lower.isin(exact_set)
            for pattern in rule.regex_patterns:
                try:
                    mask = mask | concept_lower.str.contains(pattern, case=False, regex=True, na=False)
                except re.error:
                    LOGGER.warning("Invalid regex in rule %s: %s", rule.rule_id, pattern)
            matches = filtered[mask].copy()
            if matches.empty:
                continue
            matches = matches.sort_values(
                ["dimension_count", "period_distance", "concept_name"],
                ascending=[True, True, True],
            ).reset_index(drop=True)
            best = matches.iloc[0].to_dict()
            record = {
                "rule_id": rule.rule_id,
                "metric_name": metric,
                "taxonomy": best.get("taxonomy"),
                "concept_name": best.get("concept_name"),
                "fact_value": best.get("fact_value"),
                "period_end": best.get("period_end"),
                "context_id": best.get("context_id"),
                "statement_type": best.get("statement_type"),
                "confidence": rule.confidence,
                "auto_apply": rule.auto_apply,
                "note": rule.note,
            }
            if best_match is None:
                best_match = record
            if rule.auto_apply:
                return best_match
        return best_match

    def _rule_matches_row(self, rule: ExtensionRule, row: Mapping[str, Any]) -> bool:
        if rule.cik and normalize_cik(row.get("cik")) != rule.cik:
            return False
        if rule.ticker and normalize_ticker(row.get("ticker")) != rule.ticker:
            return False
        row_sector = canonical_label(row.get("sector"))
        row_industry = canonical_label(row.get("industry"))
        row_ind_agg = canonical_label(row.get("industry_aggregate"))
        if rule.sector and row_sector != canonical_label(rule.sector):
            return False
        if rule.industry and row_industry != canonical_label(rule.industry):
            return False
        if rule.industry_aggregate or rule.industry_aggregate_aliases:
            aliases = {canonical_label(x) for x in rule.industry_aggregate_aliases if canonical_label(x)}
            if rule.industry_aggregate:
                aliases.add(canonical_label(rule.industry_aggregate))
            if row_ind_agg not in aliases and not any(row_ind_agg.endswith(a) for a in aliases if a):
                return False
        if rule.industry_regex:
            if not re.search(rule.industry_regex, str(row.get("industry") or ""), flags=re.IGNORECASE):
                return False
        return True

    def _find_applicability_policy(self, row: Mapping[str, Any], metric: str, prior_status: str) -> Optional[ApplicabilityPolicy]:
        for policy in self.applicability_policies:
            if policy.metric_name != metric:
                continue
            if policy.cik and normalize_cik(row.get("cik")) != policy.cik:
                continue
            if policy.ticker and normalize_ticker(row.get("ticker")) != policy.ticker:
                continue
            row_sector = canonical_label(row.get("sector"))
            row_industry = canonical_label(row.get("industry"))
            row_ind_agg = canonical_label(row.get("industry_aggregate"))
            if policy.sector and row_sector != canonical_label(policy.sector):
                continue
            if policy.industry and row_industry != canonical_label(policy.industry):
                continue
            if policy.industry_aggregate or policy.industry_aggregate_aliases:
                aliases = {canonical_label(x) for x in policy.industry_aggregate_aliases if canonical_label(x)}
                if policy.industry_aggregate:
                    aliases.add(canonical_label(policy.industry_aggregate))
                if row_ind_agg not in aliases and not any(row_ind_agg.endswith(a) for a in aliases if a):
                    continue
            if policy.industry_regex:
                if not re.search(policy.industry_regex, str(row.get("industry") or ""), flags=re.IGNORECASE):
                    continue
            if policy.require_statuses and prior_status not in set(policy.require_statuses):
                continue
            return policy
        return None

    def _find_prior_filing_fallback(
        self,
        row: Mapping[str, Any],
        metric: str,
        facts_for_cik: pd.DataFrame,
    ) -> Optional[Dict[str, Any]]:
        if facts_for_cik is None or facts_for_cik.empty:
            return None
        concepts = PRIOR_FILING_FALLBACK_CONCEPTS.get(metric, ())
        if not concepts:
            return None
        concept_set = {c.lower() for c in concepts}
        cands = facts_for_cik[facts_for_cik["concept_key"].isin(concept_set)].copy()
        if cands.empty:
            return None
        if self.config.as_of_date is not None:
            cutoff = pd.to_datetime(self.config.as_of_date, errors="coerce", utc=True).normalize()
            if pd.notna(cutoff) and "filed_date" in cands.columns:
                cands = cands[(cands["filed_date"].isna()) | (cands["filed_date"] <= cutoff)].copy()
        if cands.empty:
            return None
        # Keep only prior rows; do not reuse same accession here.
        current_accession = str(row.get("accession_number") or "")
        cands = cands[cands["accession_number"].astype(str) != current_accession].copy()
        if cands.empty:
            return None
        anchor = pd.to_datetime(row.get("report_period_end"), errors="coerce", utc=True).normalize()
        if pd.notna(anchor):
            cands = cands[cands["period_end"].notna()].copy()
            cands = cands[cands["period_end"] <= anchor].copy()
            if cands.empty:
                return None
            cands["staleness_days"] = (anchor - cands["period_end"]).dt.days
            cands = cands[cands["staleness_days"].fillna(999999) <= int(self.config.prior_filing_max_staleness_days)].copy()
            if cands.empty:
                return None
        else:
            cands["staleness_days"] = pd.NA
        # Prefer the most recent report period, then filing date.
        cands = cands.sort_values(
            ["period_end", "filed_date", "concept_name"],
            ascending=[False, False, True],
        ).reset_index(drop=True)
        best = cands.iloc[0]
        return {
            "fact_value": best.get("fact_value"),
            "taxonomy": best.get("taxonomy"),
            "concept_name": best.get("concept_name"),
            "period_end": best.get("period_end"),
            "filed_date": best.get("filed_date"),
            "accession_number": best.get("accession_number"),
            "staleness_days": int(best.get("staleness_days")) if pd.notna(best.get("staleness_days")) else None,
        }

    def _harvest_candidates(
        self,
        row: Mapping[str, Any],
        metric: str,
        facts_for_accession: pd.DataFrame,
        prior_status: str,
    ) -> List[Dict[str, Any]]:
        if facts_for_accession is None or facts_for_accession.empty:
            return []

        keywords_by_metric = {
            "revenue": [
                r"revenue", r"sales", r"rental", r"lease", r"royalt", r"licen", r"grant",
                r"milestone", r"collaboration", r"commission", r"premium", r"interest", r"fee",
            ],
            "net_income": [r"profit", r"netincome", r"earnings", r"income"],
            "operating_cash_flow": [r"cash", r"operatingactivities", r"operations"],
            "total_assets": [r"assets"],
            "total_equity": [r"equity", r"capital"],
        }
        pats = keywords_by_metric.get(metric, [])
        if not pats:
            return []
        pattern = "|".join(pats)
        subset = facts_for_accession[facts_for_accession["concept_key"].str.contains(pattern, case=False, regex=True, na=False)].copy()
        if subset.empty:
            return []

        period_end = row.get("report_period_end")
        if period_end is not None and not pd.isna(period_end):
            subset["period_distance"] = (subset["period_end"] - period_end).dt.days.abs().fillna(999999)
        else:
            subset["period_distance"] = 999999
        subset = subset.sort_values(
            ["dimension_count", "period_distance", "taxonomy", "concept_name"],
            ascending=[True, True, True, True],
        ).head(12)

        out: List[Dict[str, Any]] = []
        for _, cand in subset.iterrows():
            out.append(
                {
                    "ticker": row.get("ticker"),
                    "cik": row.get("cik"),
                    "accession_number": row.get("accession_number"),
                    "metric_name": metric,
                    "sector": row.get("sector"),
                    "industry": row.get("industry"),
                    "industry_aggregate": row.get("industry_aggregate"),
                    "prior_status": prior_status,
                    "taxonomy": cand.get("taxonomy"),
                    "concept_name": cand.get("concept_name"),
                    "period_type": cand.get("period_type"),
                    "period_end": cand.get("period_end"),
                    "fact_value": cand.get("fact_value"),
                    "candidate_reason": "keyword_harvest_same_filing",
                }
            )
        return out

    def _candidate_record_from_match(self, row: Mapping[str, Any], metric: str, match: Mapping[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "ticker": row.get("ticker"),
            "cik": row.get("cik"),
            "accession_number": row.get("accession_number"),
            "metric_name": metric,
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "industry_aggregate": row.get("industry_aggregate"),
            "taxonomy": match.get("taxonomy"),
            "concept_name": match.get("concept_name"),
            "period_type": None,
            "period_end": match.get("period_end"),
            "fact_value": match.get("fact_value"),
            "candidate_reason": reason,
            "rule_id": match.get("rule_id"),
        }

    # -----------------------------
    # Summary / persistence
    # -----------------------------

    def _recompute_effective_gap_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        effective_counts: List[int] = []
        resolved_reason: List[str] = []
        for _, row in out.iterrows():
            applicability = json_load_dict(row.get("metric_applicability_json"))
            statuses = json_load_dict(row.get(self.config.metric_status_json_col))
            missing_count = 0
            for metric in CORE_METRICS:
                applicable = applicability.get(metric, True)
                value = row.get(metric)
                is_missing = pd.isna(value)
                if is_missing and applicable:
                    missing_count += 1
            effective_counts.append(missing_count)
            if missing_count == 0:
                if any(str(statuses.get(m, "")).startswith("not_applicable") for m in CORE_METRICS):
                    resolved_reason.append("no_effective_gap_not_applicable_policy")
                else:
                    resolved_reason.append("no_effective_gap")
            else:
                resolved_reason.append("effective_core_metric_gap")
        out["effective_missing_metric_count"] = effective_counts
        out["effective_any_core_metric_missing"] = pd.Series(effective_counts, index=out.index).gt(0).astype(int)
        out["resolver_null_reason"] = resolved_reason
        return out

    def _build_summary_df(self, before_df: pd.DataFrame, after_df: pd.DataFrame) -> pd.DataFrame:
        def summarize(df: pd.DataFrame, label: str) -> Dict[str, Any]:
            missing_flags = pd.concat(
                [
                    df.get(metric, pd.Series(pd.NA, index=df.index)).isna().rename(metric)
                    for metric in CORE_METRICS
                ],
                axis=1,
            ) if CORE_METRICS else pd.DataFrame(index=df.index)
            row: Dict[str, Any] = {
                "stage": label,
                "rows": int(len(df)),
                "any_raw_core_missing": int(missing_flags.any(axis=1).sum()) if not missing_flags.empty else 0,
            }
            for metric in CORE_METRICS:
                row[f"{metric}_null"] = int(df.get(metric, pd.Series(pd.NA, index=df.index)).isna().sum())
            if "effective_any_core_metric_missing" in df.columns:
                row["any_effective_core_missing"] = int(df["effective_any_core_metric_missing"].sum())
            else:
                row["any_effective_core_missing"] = None
            return row
        return pd.DataFrame([summarize(before_df, "before"), summarize(after_df, "after")])

    def _build_missing_tickers_df(self, resolved_df: pd.DataFrame) -> pd.DataFrame:
        if resolved_df.empty or "effective_any_core_metric_missing" not in resolved_df.columns:
            return pd.DataFrame(
                columns=[
                    "as_of_date",
                    "ticker",
                    "cik",
                    "industry_aggregate",
                    "accession_number",
                    "effective_missing_metric_count",
                    "missing_metrics",
                    "metric_status_json",
                    "metric_applicability_json",
                ]
            )
        work = resolved_df[resolved_df["effective_any_core_metric_missing"] == 1].copy()
        if work.empty:
            return pd.DataFrame(
                columns=[
                    "as_of_date",
                    "ticker",
                    "cik",
                    "industry_aggregate",
                    "accession_number",
                    "effective_missing_metric_count",
                    "missing_metrics",
                    "metric_status_json",
                    "metric_applicability_json",
                ]
            )

        missing_metrics: List[str] = []
        for _, row in work.iterrows():
            statuses = json_load_dict(row.get(self.config.metric_status_json_col))
            applicability = json_load_dict(row.get("metric_applicability_json"))
            row_missing: List[str] = []
            for metric in CORE_METRICS:
                is_applicable = bool(applicability.get(metric, True))
                value = row.get(metric)
                is_missing = pd.isna(value)
                if is_missing and is_applicable:
                    row_missing.append(metric)
                # Keep safety if status already marks inapplicable.
                if str(statuses.get(metric, "")).startswith("not_applicable"):
                    if metric in row_missing:
                        row_missing.remove(metric)
            missing_metrics.append("|".join(sorted(set(row_missing))))
        work["missing_metrics"] = missing_metrics
        keep_cols = [
            "as_of_date",
            "ticker",
            "cik",
            "industry_aggregate",
            "accession_number",
            "effective_missing_metric_count",
            "missing_metrics",
            self.config.metric_status_json_col,
            "metric_applicability_json",
        ]
        for col in keep_cols:
            if col not in work.columns:
                work[col] = pd.NA
        out = work[keep_cols].copy()
        out = out.sort_values(["effective_missing_metric_count", "ticker"], ascending=[False, True]).reset_index(drop=True)
        return out

    def _persist_outputs(self, resolved_df: pd.DataFrame, candidate_df: pd.DataFrame) -> None:
        if self.engine is None and self.sqlite_conn is None:
            raise ValueError("DB persistence requires a DB connection")
        as_of = self.config.as_of_date

        def _persist_frame(df: pd.DataFrame, table: str) -> None:
            table = validate_sql_identifier(table, "output_table", allow_dotted=True)
            payload = df.copy()
            if as_of and "as_of_date" not in payload.columns:
                payload["as_of_date"] = as_of
            if self.sqlite_conn is not None:
                cur = self.sqlite_conn.cursor()
                if as_of and "as_of_date" in payload.columns and _table_exists(self.sqlite_conn, table):
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE as_of_date = ?", (as_of,))
                    except sqlite3.OperationalError as exc:
                        self.sqlite_conn.rollback()
                        LOGGER.warning(
                            "Resolver pre-delete failed for sqlite table=%s as_of_date=%s: %s",
                            table,
                            as_of,
                            exc,
                        )
                        raise
                try:
                    payload.to_sql(table, self.sqlite_conn, if_exists="append", index=False)
                    self.sqlite_conn.commit()
                except Exception:
                    self.sqlite_conn.rollback()
                    raise
                return
            if self.engine is None:
                raise ValueError("DB engine unavailable")
            if text is None:
                raise RuntimeError("SQLAlchemy text() unavailable")
            with self.engine.begin() as conn:
                if as_of and "as_of_date" in payload.columns and self._table_exists_any(table):
                    try:
                        conn.execute(text(f"DELETE FROM {table} WHERE as_of_date = :as_of"), {"as_of": as_of})
                    except Exception as exc:
                        LOGGER.warning(
                            "Resolver pre-delete failed for table=%s as_of_date=%s: %s",
                            table,
                            as_of,
                            exc,
                        )
                        raise
                payload.to_sql(table, conn, if_exists="append", index=False)

        _persist_frame(resolved_df, self.config.output_table)
        LOGGER.info("Persisted resolved table: %s (%d rows)", self.config.output_table, len(resolved_df))
        if self.config.candidate_table and not candidate_df.empty:
            _persist_frame(candidate_df, self.config.candidate_table)
            LOGGER.info("Persisted candidate table: %s (%d rows)", self.config.candidate_table, len(candidate_df))

    # -----------------------------
    # Utilities
    # -----------------------------

    def _read_yaml(self, path: str) -> Dict[str, Any]:
        if not HAVE_YAML:
            raise RuntimeError("PyYAML is required to read YAML config files.")
        with open(path, "r", encoding="utf-8") as f:
            payload = yaml.safe_load(f) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"YAML root must be a mapping: {path}")
        return payload

    def _parse_rule(self, item: Mapping[str, Any], default_auto_apply: bool) -> Optional[ExtensionRule]:
        enabled = bool(item.get("enabled", True))
        if not enabled:
            return None
        metric_name = str(item.get("metric_name", "")).strip()
        rule_id = str(item.get("rule_id", "")).strip() or f"rule_{metric_name}"
        if not metric_name:
            return None
        match = item.get("match") or {}
        concepts = match.get("concepts") or []
        mode = str(match.get("mode", "exact")).strip().lower()
        exact: List[str] = []
        regex: List[str] = []
        if mode == "exact":
            exact = [str(x) for x in concepts]
        elif mode == "regex":
            regex = [str(x) for x in concepts]
        elif mode == "both":
            exact = [str(x) for x in match.get("exact_concepts", [])]
            regex = [str(x) for x in match.get("regex_concepts", [])]
        else:
            raise ValueError(f"Unsupported match mode in rule {rule_id}: {mode}")
        taxonomies = tuple(str(x).lower() for x in (item.get("taxonomies") or ["*"]))
        priority_raw = pd.to_numeric(
            item.get("priority", item.get("priority_start", 100)),
            errors="coerce",
        )
        return ExtensionRule(
            rule_id=rule_id,
            metric_name=metric_name,
            taxonomies=taxonomies,
            exact_concepts=tuple(exact),
            regex_patterns=tuple(regex),
            period_type=self._none_if_blank(item.get("period_type")),
            sector=self._none_if_blank(item.get("sector")),
            industry=self._none_if_blank(item.get("industry")),
            industry_regex=self._none_if_blank(item.get("industry_regex")),
            industry_aggregate=self._none_if_blank(item.get("industry_aggregate")),
            industry_aggregate_aliases=self._split_aliases(item.get("industry_aggregate_aliases")),
            cik=normalize_cik(item.get("cik")),
            ticker=normalize_ticker(item.get("ticker")),
            priority=int(priority_raw) if pd.notna(priority_raw) else 100,
            auto_apply=bool(item.get("auto_apply", default_auto_apply)),
            confidence=self._text_or_default(
                item.get("confidence"),
                "high" if default_auto_apply else "review",
            ).lower(),
            source_kind=self._text_or_default(item.get("source_kind"), "direct").lower(),
            note=self._none_if_blank(item.get("note")),
        )

    @staticmethod
    def _none_if_blank(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        text_val = str(value).strip()
        return text_val or None

    @classmethod
    def _text_or_default(cls, value: Any, default: str) -> str:
        text_val = cls._none_if_blank(value)
        return text_val if text_val is not None else default

    @classmethod
    def _split_aliases(cls, value: Any) -> Tuple[str, ...]:
        if value is None:
            return ()
        try:
            if pd.isna(value):
                return ()
        except (TypeError, ValueError):
            pass
        if isinstance(value, str):
            raw_items = value.split("|")
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = [value]
        aliases: List[str] = []
        for item in raw_items:
            text_val = cls._none_if_blank(item)
            if text_val:
                aliases.append(text_val)
        return tuple(aliases)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve residual SEC core-metric gaps using issuer overrides and applicability policies.")
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--snapshot-table", default="sec_fundamental_snapshot_filled_security_t1")
    parser.add_argument("--facts-table", default="sec_xbrl_facts_raw")
    parser.add_argument("--output-table", default="sec_fundamental_snapshot_filled_security_t1_resolved")
    parser.add_argument("--candidate-table", default=None)
    parser.add_argument("--snapshot-csv", default=None)
    parser.add_argument("--facts-csv", default=None)
    parser.add_argument("--as-of-date", default=None)
    parser.add_argument("--extension-rule-yaml", default=None)
    parser.add_argument("--applicability-yaml", default=None)
    parser.add_argument("--issuer-override-csv", default=None)
    parser.add_argument("--prior-filing-fallback-enabled", default="true")
    parser.add_argument("--prior-filing-max-staleness-days", type=int, default=550)
    parser.add_argument("--candidate-csv", default=None)
    parser.add_argument("--missing-tickers-csv", default=None)
    parser.add_argument("--resolved-csv", default=None)
    parser.add_argument("--summary-csv", default=None)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text_val = str(value).strip().lower()
    if text_val in {"1", "true", "yes", "y", "on"}:
        return True
    if text_val in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _sqlite_path_from_db_url(db_url: str) -> Optional[str]:
    parsed = urlparse(db_url)
    if parsed.scheme != "sqlite":
        return None
    path = db_url.replace("sqlite:///", "", 1)
    path = path.replace("sqlite://", "", 1)
    path = unquote(path)
    return path or None


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    engine: Optional[Engine] = None
    sqlite_conn: Optional[sqlite3.Connection] = None
    if args.db_url:
        sqlite_path = _sqlite_path_from_db_url(args.db_url)
        if sqlite_path:
            sqlite_conn = sqlite3.connect(sqlite_path, timeout=30.0)
            sqlite_conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            sqlite_conn.execute("PRAGMA foreign_keys = ON")
            sqlite_conn.execute("PRAGMA journal_mode = WAL")
            sqlite_conn.execute("PRAGMA synchronous = NORMAL")
        elif HAVE_SQLALCHEMY:
            engine = create_engine(args.db_url, future=True)
        else:
            raise RuntimeError("SQLAlchemy is required for non-sqlite DB URLs.")

    config = ResolverConfig(
        snapshot_table=args.snapshot_table,
        facts_table=args.facts_table,
        output_table=args.output_table,
        candidate_table=args.candidate_table,
        snapshot_csv=args.snapshot_csv,
        facts_csv=args.facts_csv,
        extension_rule_yaml=args.extension_rule_yaml,
        applicability_yaml=args.applicability_yaml,
        issuer_override_csv=args.issuer_override_csv,
        as_of_date=args.as_of_date,
        prior_filing_fallback_enabled=_coerce_bool(args.prior_filing_fallback_enabled, default=True),
        prior_filing_max_staleness_days=int(args.prior_filing_max_staleness_days),
    )
    try:
        resolver = SnapshotGapResolver(config=config, engine=engine, sqlite_conn=sqlite_conn)
        outputs = resolver.run(
            persist=args.persist,
            candidate_csv=args.candidate_csv,
            missing_tickers_csv=args.missing_tickers_csv,
            resolved_csv=args.resolved_csv,
            summary_csv=args.summary_csv,
        )
        LOGGER.info("Resolved rows head:\n%s", outputs["resolved_df"].head(10).to_string(index=False))
        if not outputs["candidate_df"].empty:
            LOGGER.info("Candidate rows head:\n%s", outputs["candidate_df"].head(20).to_string(index=False))
        if not outputs["missing_tickers_df"].empty:
            LOGGER.info("Missing-tickers rows head:\n%s", outputs["missing_tickers_df"].head(20).to_string(index=False))
    finally:
        if sqlite_conn is not None:
            sqlite_conn.close()


if __name__ == "__main__":
    main()
