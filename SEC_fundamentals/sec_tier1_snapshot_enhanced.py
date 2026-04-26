
from __future__ import annotations

"""
Enhanced tier-1 SEC fundamentals point-in-time snapshot builder.

This version extends the original snapshot builder with:
1. strict point-in-time gating using acceptance_datetime with an optional SEC publication buffer
2. same-filing metric repair before any prior-period backfill
3. taxonomy-aware mapping (US-GAAP + IFRS) from optional long-form fact rows
4. sector-aware revenue derivation for financial issuers using industry_aggregate
5. entity-level snapshots plus security-level fan-out via ticker alias / share-class mapping
6. historical as-of batch builds with leakage audits
7. coverage diagnostics and metric-level provenance/status tracking

The builder still supports the original wide source table shape
(sec_fundamental_period_t1-like rows), but it can also consume an optional
long metric/fact dataframe or DB table to repair IFRS and financial-sector
mapping gaps within the same filing before falling back to older filings.

Expected alias mapping columns:
    Ticker_1, Ticker_2, CIK

Expected optional issuer profile columns:
    ticker, sector, industry, industry_aggregate [, cik]

Expected optional long metric/fact columns (configurable):
    cik, accession_number, report_period_end, taxonomy, concept_name, fact_value
    [context_id, unit, period_type, dimension_count, statement_type]
"""

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import requests
from sec_fundamentals_config import ANNUAL_FORMS, QUARTERLY_FORMS, SUPPLEMENTAL_FORMS

try:  # Optional dependency for DB wrappers.
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import Engine
    HAVE_SQLALCHEMY = True
except ImportError:  # pragma: no cover
    create_engine = None
    Engine = Any  # type: ignore[misc,assignment]
    HAVE_SQLALCHEMY = False

    def text(sql: str) -> str:
        return sql


LOGGER = logging.getLogger("sec_tier1_snapshot_enhanced")
UA_EMAIL_PATTERN = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
UA_PLACEHOLDER_TOKENS = ("example", "placeholder", "test", "your")


# -----------------------------
# SEC API client (optional use)
# -----------------------------

class SecRateLimiter:
    """Simple fixed-interval limiter for SEC fair-access compliance."""

    def __init__(self, rate_per_sec: float = 8.0) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate_per_sec = rate_per_sec
        self._next_allowed = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
        self._next_allowed = max(now, self._next_allowed) + (1.0 / self.rate_per_sec)


class SecEdgarClient:
    """Lightweight official SEC data.sec.gov client."""

    BASE_URL = "https://data.sec.gov"

    def __init__(
        self,
        user_agent: Optional[str] = None,
        rate_limit_per_sec: float = 8.0,
        timeout_seconds: int = 30,
        session: Optional[requests.Session] = None,
    ) -> None:
        resolved_user_agent = (user_agent or os.getenv("SEC_USER_AGENT", "")).strip()
        if not resolved_user_agent:
            raise ValueError(
                "Missing SEC user agent. Set SEC_USER_AGENT or pass user_agent with a real contact email."
            )
        email_match = UA_EMAIL_PATTERN.search(resolved_user_agent)
        if not email_match:
            raise ValueError(
                "Invalid SEC user agent. Include a real contact email, e.g. 'Name team@company.com'."
            )
        email_domain = email_match.group(1).split("@", 1)[1].lower()
        if any(tok in email_domain for tok in UA_PLACEHOLDER_TOKENS):
            raise ValueError(
                "SEC user agent email appears to be placeholder/test; provide a real monitored email."
            )
        self.user_agent = resolved_user_agent
        self.timeout_seconds = timeout_seconds
        self.rate_limiter = SecRateLimiter(rate_per_sec=rate_limit_per_sec)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json, text/plain, */*",
            }
        )

    @staticmethod
    def normalize_cik(cik: Any) -> str:
        digits = re.sub(r"\D", "", str(cik or ""))
        if not digits:
            raise ValueError(f"Invalid CIK: {cik!r}")
        return digits.zfill(10)

    def _get_json(self, url: str) -> Dict[str, Any]:
        self.rate_limiter.wait()
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.json()

    def get_submissions(self, cik: Any) -> Dict[str, Any]:
        cik10 = self.normalize_cik(cik)
        return self._get_json(f"{self.BASE_URL}/submissions/CIK{cik10}.json")

    def iter_all_submission_history(self, cik: Any) -> Iterator[Dict[str, Any]]:
        payload = self.get_submissions(cik)
        yield payload
        for item in payload.get("filings", {}).get("files", []) or []:
            name = item.get("name")
            if name:
                yield self._get_json(f"{self.BASE_URL}/submissions/{name}")

    def get_companyfacts(self, cik: Any) -> Dict[str, Any]:
        cik10 = self.normalize_cik(cik)
        return self._get_json(f"{self.BASE_URL}/api/xbrl/companyfacts/CIK{cik10}.json")


# -----------------------------
# Helpers / constants
# -----------------------------

DEFAULT_ALIAS_ROWS: List[Dict[str, str]] = []


def normalize_ticker(value: Any) -> Optional[str]:
    if value is None:
        return None
    if pd.isna(value):
        return None
    out = str(value).strip().upper()
    return out or None


def normalize_cik_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if pd.isna(value):
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    return digits.zfill(10)


def canonical_form(form_type: Any) -> str:
    if form_type is None or pd.isna(form_type):
        return ""
    return re.sub(r"\s+", "", str(form_type).upper())


def form_family(form_type: Any) -> str:
    form = canonical_form(form_type)
    if form in QUARTERLY_FORMS:
        return "quarterly"
    if form in ANNUAL_FORMS:
        return "annual"
    if form in SUPPLEMENTAL_FORMS:
        return "supplemental"
    return "other"


def is_periodic_form(form_type: Any) -> bool:
    return form_family(form_type) in {"quarterly", "annual"}


def _parse_as_of_timestamp(value: Any, default_tz: str = "UTC") -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        resolved_tz = str(default_tz).strip() or "UTC"
        LOGGER.warning("Naive as_of timestamp %r received; assuming %s.", value, resolved_tz)
        ts = ts.tz_localize(resolved_tz)
    return ts.tz_convert("UTC")


def _normalize_date_col(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True).dt.normalize()


def _safe_json_loads(value: Any) -> Dict[str, Any]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _first_non_null(*values: Any) -> Any:
    for value in values:
        if value is not None and not pd.isna(value):
            return value
    return None


def make_as_of_timestamp(
    as_of_date: str,
    cutoff_time: str = "16:15:00",
    timezone: str = "America/New_York",
) -> str:
    """Convenience helper for historical builds aligned to a trading cutoff."""
    try:
        tzinfo = ZoneInfo(str(timezone).strip() or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid cutoff timezone: {timezone!r}") from exc
    try:
        local_dt = dt.datetime.strptime(f"{as_of_date} {cutoff_time}", "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(
            f"Invalid as-of timestamp components: as_of_date={as_of_date!r}, cutoff_time={cutoff_time!r}."
        ) from exc
    local_ts = pd.Timestamp(local_dt, tz=tzinfo)
    return local_ts.tz_convert("UTC").isoformat()


# -----------------------------
# Default mapping registry
# -----------------------------

def default_metric_mapping_df() -> pd.DataFrame:
    """
    Built-in mapping registry. Meant to be a strong default, not the final word.
    Extend with --metric-mapping-csv (or DataFrame input) as needed.
    """
    records: List[Dict[str, Any]] = []

    def add_direct(metric: str, taxonomy: str, concept: str, priority: int, industry_aggregate: Optional[str] = None) -> None:
        records.append(
            {
                "metric_name": metric,
                "source_kind": "direct",
                "taxonomy": taxonomy,
                "concept_name": concept,
                "priority": priority,
                "industry_aggregate": industry_aggregate,
                "component_group": None,
                "wide_column_name": None,
                "period_type": "instant" if metric in {"total_assets", "total_equity"} else "duration",
            }
        )

    def add_formula_component(
        metric: str,
        taxonomy: str,
        concept: str,
        priority: int,
        component_group: str,
        industry_aggregate: str,
        wide_column_name: Optional[str] = None,
    ) -> None:
        records.append(
            {
                "metric_name": metric,
                "source_kind": "formula_component",
                "taxonomy": taxonomy,
                "concept_name": concept,
                "priority": priority,
                "industry_aggregate": industry_aggregate,
                "component_group": component_group,
                "wide_column_name": wide_column_name,
                "period_type": "instant" if metric in {"total_assets", "total_equity"} else "duration",
            }
        )

    # ---- Core direct mappings: US-GAAP
    for priority, concept in enumerate(
        [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueServicesNet",
            "OperatingRevenue",
        ],
        start=10,
    ):
        add_direct("revenue", "us-gaap", concept, priority)
    for priority, concept in enumerate(
        [
            "NetIncomeLoss",
            "ProfitLoss",
        ],
        start=10,
    ):
        add_direct("net_income", "us-gaap", concept, priority)
    for priority, concept in enumerate(
        [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ],
        start=10,
    ):
        add_direct("operating_cash_flow", "us-gaap", concept, priority)
    for priority, concept in enumerate(
        [
            "Assets",
        ],
        start=10,
    ):
        add_direct("total_assets", "us-gaap", concept, priority)
    for priority, concept in enumerate(
        [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
            "MembersEquity",
        ],
        start=10,
    ):
        add_direct("total_equity", "us-gaap", concept, priority)

    # ---- Core direct mappings: IFRS
    for priority, concept in enumerate(
        [
            "Revenue",
            "RevenueFromContractsWithCustomers",
            "InsuranceRevenue",
        ],
        start=10,
    ):
        add_direct("revenue", "ifrs-full", concept, priority)
    for priority, concept in enumerate(
        [
            "ProfitLoss",
            "ProfitLossAttributableToOwnersOfParent",
        ],
        start=10,
    ):
        add_direct("net_income", "ifrs-full", concept, priority)
    for priority, concept in enumerate(
        [
            "NetCashFlowsFromUsedInOperatingActivities",
            "CashFlowsFromUsedInOperatingActivities",
        ],
        start=10,
    ):
        add_direct("operating_cash_flow", "ifrs-full", concept, priority)
    add_direct("total_assets", "ifrs-full", "Assets", 10)
    for priority, concept in enumerate(
        [
            "Equity",
            "EquityAttributableToOwnersOfParent",
        ],
        start=10,
    ):
        add_direct("total_equity", "ifrs-full", concept, priority)

    # ---- Banking & consumer finance revenue formula components
    bank = "Banking & Consumer Finance"
    for priority, concept in enumerate(
        ["NetInterestIncome", "InterestAndDividendIncomeOperating"],
        start=10,
    ):
        add_formula_component("revenue", "us-gaap", concept, priority, "bank_net_interest_income", bank, "net_interest_income")
    for priority, concept in enumerate(
        [
            "NoninterestIncome",
            "FeesAndCommissionsRevenue",
            "TradingRevenue",
            "BrokerageCommissionsRevenue",
            "InvestmentBankingRevenue",
            "ServicingFees",
        ],
        start=20,
    ):
        add_formula_component("revenue", "us-gaap", concept, priority, "bank_noninterest_income", bank, "noninterest_income")
    for priority, concept in enumerate(
        [
            "InterestRevenueCalculatedUsingEffectiveInterestMethod",
            "InterestAndSimilarIncome",
            "NetInterestIncome",
        ],
        start=10,
    ):
        add_formula_component("revenue", "ifrs-full", concept, priority, "bank_net_interest_income", bank, "net_interest_income")
    for priority, concept in enumerate(
        [
            "FeeAndCommissionIncome",
            "NetTradingIncome",
            "OtherOperatingIncome",
            "FeesAndCommissionsRevenue",
        ],
        start=20,
    ):
        add_formula_component("revenue", "ifrs-full", concept, priority, "bank_noninterest_income", bank, "noninterest_income")

    # ---- Capital markets / asset managers / exchanges revenue components
    capital_markets = "Capital Markets, Asset Managers & Exchanges"
    for priority, concept in enumerate(
        [
            "InvestmentBankingRevenue",
            "BrokerageCommissionsRevenue",
            "TradingRevenue",
            "AssetManagementFees1",
            "FeesAndCommissionsRevenue",
            "AdvisoryFeeRevenue",
            "CommissionRevenue",
        ],
        start=10,
    ):
        add_formula_component("revenue", "us-gaap", concept, priority, "capital_markets_core_revenue", capital_markets)
    for priority, concept in enumerate(
        [
            "FeeAndCommissionIncome",
            "NetTradingIncome",
            "Revenue",
            "OtherOperatingIncome",
        ],
        start=10,
    ):
        add_formula_component("revenue", "ifrs-full", concept, priority, "capital_markets_core_revenue", capital_markets)

    # ---- Insurance & brokerage revenue components
    insurance = "Insurance & Brokerage"
    for priority, concept in enumerate(
        [
            "InsuranceRevenue",
            "PremiumsEarnedNet",
            "BrokerageCommissionsRevenue",
            "FeesAndCommissionsRevenue",
            "CommissionRevenue",
        ],
        start=10,
    ):
        add_formula_component("revenue", "us-gaap", concept, priority, "insurance_core_revenue", insurance)
    for priority, concept in enumerate(
        [
            "InsuranceRevenue",
            "GrossWrittenPremiums",
            "FeeAndCommissionIncome",
            "Revenue",
        ],
        start=10,
    ):
        add_formula_component("revenue", "ifrs-full", concept, priority, "insurance_core_revenue", insurance)

    df = pd.DataFrame(records)
    if not df.empty:
        df["taxonomy_key"] = df["taxonomy"].astype(str).str.lower()
        df["concept_key"] = df["concept_name"].astype(str).str.lower()
    return df


DEFAULT_FORMULA_RULES: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("revenue", "Banking & Consumer Finance"): {
        "formula_name": "bank_topline_equivalent",
        "component_groups": ["bank_net_interest_income", "bank_noninterest_income"],
        "min_components": 2,  # conservative; avoids filling from only one partial leg
    },
    ("revenue", "Capital Markets, Asset Managers & Exchanges"): {
        "formula_name": "capital_markets_core_revenue",
        "component_groups": ["capital_markets_core_revenue"],
        "min_components": 1,
    },
    ("revenue", "Insurance & Brokerage"): {
        "formula_name": "insurance_brokerage_core_revenue",
        "component_groups": ["insurance_core_revenue"],
        "min_components": 1,
    },
}


# -----------------------------
# Configuration / results
# -----------------------------

@dataclass(frozen=True)
class MetricRule:
    name: str
    weight: int = 1
    max_backfill_days: int = 800


@dataclass
class SnapshotConfig:
    source_table: str = "sec_fundamental_period_t1"
    metric_source_table: Optional[str] = None
    universe_table: Optional[str] = None
    issuer_profile_table: Optional[str] = None
    alias_mapping_table: Optional[str] = None

    # Entity-level outputs
    strict_table: str = "sec_fundamental_snapshot_strict_t1"
    filled_table: str = "sec_fundamental_snapshot_filled_t1"

    # Security-level outputs
    security_strict_table: str = "sec_fundamental_snapshot_strict_security_t1"
    security_filled_table: str = "sec_fundamental_snapshot_filled_security_t1"

    run_table: str = "sec_fundamental_snapshot_run_t1"

    # Paths (optional, overrides DB tables for those inputs)
    alias_mapping_path: Optional[str] = None
    issuer_profile_path: Optional[str] = None
    metric_mapping_path: Optional[str] = None

    # Core source columns
    cik_col: str = "cik"
    ticker_col: str = "ticker"
    accession_col: str = "accession_number"
    form_col: str = "form_type"
    filing_date_col: str = "filing_date"
    acceptance_col: str = "acceptance_datetime"
    report_period_end_col: str = "report_period_end"

    # Metric / fact columns (long-form optional source)
    metric_cik_col: str = "cik"
    metric_accession_col: str = "accession_number"
    metric_period_end_col: str = "report_period_end"
    metric_taxonomy_col: str = "taxonomy"
    metric_concept_col: str = "concept_name"
    metric_value_col: str = "fact_value"
    metric_context_col: str = "context_id"
    metric_unit_col: str = "unit"
    metric_period_type_col: str = "period_type"
    metric_dimension_count_col: str = "dimension_count"
    metric_statement_col: str = "statement_type"

    # Alias mapping columns
    alias_ticker_1_col: str = "Ticker_1"
    alias_ticker_2_col: str = "Ticker_2"
    alias_cik_col: str = "CIK"

    # Issuer profile columns
    sector_col: str = "sector"
    industry_col: str = "industry"
    industry_aggregate_col: str = "industry_aggregate"

    delete_existing_as_of: bool = True
    use_universe: bool = True
    include_missing_universe_rows: bool = True
    output_security_snapshots: bool = True
    fanout_aliases: bool = True

    strict_min_non_null_metrics: int = 2
    allow_supplemental_as_anchor_when_no_periodic: bool = False
    supplemental_min_non_null_metrics: int = 3
    lookback_days: int = 900

    same_filing_repair_enabled: bool = True
    publication_lag_minutes: int = 0  # set >0 for strict intraday historical use

    enforce_quality_gates: bool = False
    max_all5_missing_entity: int = 0

    metrics: List[MetricRule] = field(
        default_factory=lambda: [
            MetricRule("revenue", weight=3, max_backfill_days=550),
            MetricRule("net_income", weight=3, max_backfill_days=550),
            MetricRule("operating_cash_flow", weight=4, max_backfill_days=550),
            MetricRule("total_assets", weight=3, max_backfill_days=900),
            MetricRule("total_equity", weight=3, max_backfill_days=900),
        ]
    )

    def metric_names(self) -> List[str]:
        return [m.name for m in self.metrics]

    def metric_weights(self) -> Dict[str, int]:
        return {m.name: m.weight for m in self.metrics}

    def metric_max_backfill_days(self) -> Dict[str, int]:
        return {m.name: m.max_backfill_days for m in self.metrics}


@dataclass
class SnapshotRunResult:
    entity_strict_df: pd.DataFrame
    entity_filled_df: pd.DataFrame
    security_strict_df: pd.DataFrame
    security_filled_df: pd.DataFrame
    coverage_report_df: pd.DataFrame
    audit_report_df: pd.DataFrame
    stats: Dict[str, Any]


@dataclass
class SnapshotHistoryResult:
    entity_strict_df: pd.DataFrame
    entity_filled_df: pd.DataFrame
    security_strict_df: pd.DataFrame
    security_filled_df: pd.DataFrame
    coverage_report_df: pd.DataFrame
    audit_report_df: pd.DataFrame
    stats_df: pd.DataFrame


# -----------------------------
# Core builder
# -----------------------------

class Tier1FundamentalSnapshotBuilder:
    def __init__(self, engine: Optional[Engine] = None, config: Optional[SnapshotConfig] = None) -> None:
        self.engine = engine
        self.config = config or SnapshotConfig()

    # ---------- public API ----------

    def run(
        self,
        as_of_timestamp: str,
        universe_as_of_date: Optional[str] = None,
        ciks: Optional[Sequence[str]] = None,
        persist: bool = True,
    ) -> SnapshotRunResult:
        """
        DB mode.
        """
        if self.engine is None:
            raise ValueError("run() requires a DB engine. Use run_from_dataframes() for pure pandas mode.")

        as_of_ts = _parse_as_of_timestamp(as_of_timestamp)
        as_of_date = as_of_ts.date().isoformat()
        LOGGER.info("Building enhanced tier-1 snapshot from DB for as_of=%s", as_of_ts.isoformat())

        alias_df = self._load_alias_mapping()
        universe_df = self._load_universe(as_of_date=universe_as_of_date or as_of_date, ciks=ciks)
        issuer_profile_df = self._load_issuer_profile()
        source_df = self._load_source_rows(as_of_ts=as_of_ts, universe_df=universe_df, ciks=ciks)
        metric_facts_df = self._load_metric_source_rows(source_df) if self.config.metric_source_table else None
        metric_mapping_df = self._load_metric_mapping_df()
        result = self.run_from_dataframes(
            as_of_timestamp=as_of_ts.isoformat(),
            source_df=source_df,
            universe_df=universe_df,
            alias_df=alias_df,
            issuer_profile_df=issuer_profile_df,
            metric_facts_df=metric_facts_df,
            metric_mapping_df=metric_mapping_df,
            persist=persist,
        )
        return result

    def run_from_dataframes(
        self,
        as_of_timestamp: str,
        source_df: pd.DataFrame,
        universe_df: Optional[pd.DataFrame] = None,
        alias_df: Optional[pd.DataFrame] = None,
        issuer_profile_df: Optional[pd.DataFrame] = None,
        metric_facts_df: Optional[pd.DataFrame] = None,
        metric_mapping_df: Optional[pd.DataFrame] = None,
        persist: bool = False,
    ) -> SnapshotRunResult:
        as_of_ts = _parse_as_of_timestamp(as_of_timestamp)
        as_of_date = as_of_ts.date().isoformat()
        LOGGER.info("Building enhanced tier-1 snapshot in pandas mode for as_of=%s", as_of_ts.isoformat())

        alias_long_df = self._normalize_alias_mapping(alias_df)
        universe_norm_df = self._normalize_universe(universe_df, alias_long_df)
        issuer_profile_norm_df = self._normalize_issuer_profile(issuer_profile_df, alias_long_df)
        mapping_df = self._normalize_metric_mapping_df(metric_mapping_df)
        prepared_df = self._prepare_source_base(source_df, as_of_ts=as_of_ts)
        prepared_df = self._attach_issuer_profile(prepared_df, issuer_profile_norm_df)
        metric_facts_norm_df = self._normalize_metric_facts(metric_facts_df)
        prepared_df = self._apply_same_filing_metric_repairs(
            prepared_df=prepared_df,
            metric_facts_df=metric_facts_norm_df,
            metric_mapping_df=mapping_df,
        )
        prepared_df = self._recompute_bundle_scores(prepared_df)
        accession_bundles_df = self._choose_best_row_per_accession(prepared_df)

        entity_strict_df = self._build_entity_strict_snapshot(accession_bundles_df, as_of_ts, universe_norm_df)
        entity_filled_df = self._build_entity_filled_snapshot(entity_strict_df, accession_bundles_df, as_of_ts, universe_norm_df)

        if self.config.output_security_snapshots:
            security_strict_df = self._build_security_snapshot(entity_strict_df, universe_norm_df, alias_long_df, as_of_ts, snapshot_kind="strict")
            security_filled_df = self._build_security_snapshot(entity_filled_df, universe_norm_df, alias_long_df, as_of_ts, snapshot_kind="filled")
        else:
            security_strict_df = entity_strict_df.copy()
            security_filled_df = entity_filled_df.copy()

        coverage_report_df = self._build_coverage_report(entity_filled_df, security_filled_df, as_of_ts)
        audit_report_df = self._build_audit_report(
            as_of_ts=as_of_ts,
            source_df=source_df,
            prepared_df=prepared_df,
            entity_filled_df=entity_filled_df,
            security_filled_df=security_filled_df,
        )

        stats: Dict[str, Any] = {
            "as_of_timestamp": as_of_ts.isoformat(),
            "as_of_date": as_of_date,
            "source_rows": int(len(source_df)),
            "prepared_rows": int(len(prepared_df)),
            "accession_bundles": int(len(accession_bundles_df)),
            "entity_strict_rows": int(len(entity_strict_df)),
            "entity_filled_rows": int(len(entity_filled_df)),
            "security_strict_rows": int(len(security_strict_df)),
            "security_filled_rows": int(len(security_filled_df)),
            "entity_filled_non_null_by_metric": {
                metric: int(entity_filled_df[metric].notna().sum()) if metric in entity_filled_df.columns else 0
                for metric in self.config.metric_names()
            },
            "security_filled_non_null_by_metric": {
                metric: int(security_filled_df[metric].notna().sum()) if metric in security_filled_df.columns else 0
                for metric in self.config.metric_names()
            },
            "entity_all5_missing_count": int(self._count_all_metrics_missing(entity_filled_df)),
            "security_all5_missing_count": int(self._count_all_metrics_missing(security_filled_df)),
            "future_leak_count_entity": int(audit_report_df["future_leak_count_entity"].iloc[0]) if not audit_report_df.empty else 0,
            "future_leak_count_security": int(audit_report_df["future_leak_count_security"].iloc[0]) if not audit_report_df.empty else 0,
        }

        if self.config.enforce_quality_gates and stats["entity_all5_missing_count"] > self.config.max_all5_missing_entity:
            raise ValueError(
                f"Quality gate failed: entity_all5_missing_count={stats['entity_all5_missing_count']} "
                f"> max_all5_missing_entity={self.config.max_all5_missing_entity}"
            )

        if persist:
            if self.engine is None:
                raise ValueError("persist=True requires a DB engine.")
            self._persist(
                entity_strict_df=entity_strict_df,
                entity_filled_df=entity_filled_df,
                security_strict_df=security_strict_df,
                security_filled_df=security_filled_df,
                coverage_report_df=coverage_report_df,
                audit_report_df=audit_report_df,
                stats=stats,
            )

        return SnapshotRunResult(
            entity_strict_df=entity_strict_df,
            entity_filled_df=entity_filled_df,
            security_strict_df=security_strict_df,
            security_filled_df=security_filled_df,
            coverage_report_df=coverage_report_df,
            audit_report_df=audit_report_df,
            stats=stats,
        )

    def run_history_from_dataframes(
        self,
        as_of_timestamps: Sequence[str],
        source_df: pd.DataFrame,
        universe_df: Optional[pd.DataFrame] = None,
        alias_df: Optional[pd.DataFrame] = None,
        issuer_profile_df: Optional[pd.DataFrame] = None,
        metric_facts_df: Optional[pd.DataFrame] = None,
        metric_mapping_df: Optional[pd.DataFrame] = None,
        persist: bool = False,
    ) -> SnapshotHistoryResult:
        entity_strict_parts: List[pd.DataFrame] = []
        entity_filled_parts: List[pd.DataFrame] = []
        security_strict_parts: List[pd.DataFrame] = []
        security_filled_parts: List[pd.DataFrame] = []
        coverage_parts: List[pd.DataFrame] = []
        audit_parts: List[pd.DataFrame] = []
        stats_rows: List[Dict[str, Any]] = []

        for as_of_timestamp in as_of_timestamps:
            result = self.run_from_dataframes(
                as_of_timestamp=as_of_timestamp,
                source_df=source_df,
                universe_df=universe_df,
                alias_df=alias_df,
                issuer_profile_df=issuer_profile_df,
                metric_facts_df=metric_facts_df,
                metric_mapping_df=metric_mapping_df,
                persist=persist,
            )
            entity_strict_parts.append(result.entity_strict_df)
            entity_filled_parts.append(result.entity_filled_df)
            security_strict_parts.append(result.security_strict_df)
            security_filled_parts.append(result.security_filled_df)
            coverage_parts.append(result.coverage_report_df)
            audit_parts.append(result.audit_report_df)
            stats_rows.append(result.stats)

        return SnapshotHistoryResult(
            entity_strict_df=pd.concat(entity_strict_parts, ignore_index=True) if entity_strict_parts else pd.DataFrame(),
            entity_filled_df=pd.concat(entity_filled_parts, ignore_index=True) if entity_filled_parts else pd.DataFrame(),
            security_strict_df=pd.concat(security_strict_parts, ignore_index=True) if security_strict_parts else pd.DataFrame(),
            security_filled_df=pd.concat(security_filled_parts, ignore_index=True) if security_filled_parts else pd.DataFrame(),
            coverage_report_df=pd.concat(coverage_parts, ignore_index=True) if coverage_parts else pd.DataFrame(),
            audit_report_df=pd.concat(audit_parts, ignore_index=True) if audit_parts else pd.DataFrame(),
            stats_df=pd.DataFrame(stats_rows),
        )

    # ---------- DB loaders ----------

    def _load_alias_mapping(self) -> Optional[pd.DataFrame]:
        if self.config.alias_mapping_path:
            return pd.read_csv(self.config.alias_mapping_path)
        if self.engine is not None and self.config.alias_mapping_table:
            sql = text(f"SELECT * FROM {self.config.alias_mapping_table}")
            with self.engine.begin() as conn:
                return pd.read_sql(sql, conn)
        return pd.DataFrame(columns=[self.config.alias_ticker_1_col, self.config.alias_ticker_2_col, self.config.alias_cik_col])

    def _load_metric_mapping_df(self) -> pd.DataFrame:
        if self.config.metric_mapping_path:
            return pd.read_csv(self.config.metric_mapping_path)
        return default_metric_mapping_df()

    def _load_issuer_profile(self) -> Optional[pd.DataFrame]:
        if self.config.issuer_profile_path:
            return pd.read_csv(self.config.issuer_profile_path)
        if self.engine is not None and self.config.issuer_profile_table:
            sql = text(f"SELECT * FROM {self.config.issuer_profile_table}")
            with self.engine.begin() as conn:
                return pd.read_sql(sql, conn)
        return None

    def _load_universe(self, as_of_date: str, ciks: Optional[Sequence[str]] = None) -> Optional[pd.DataFrame]:
        if ciks:
            df = pd.DataFrame([{self.config.cik_col: normalize_cik_text(c), self.config.ticker_col: None} for c in ciks])
            return df

        if self.engine is None or not (self.config.use_universe and self.config.universe_table):
            return None

        sql = text(
            f"""
            SELECT *
            FROM {self.config.universe_table}
            WHERE as_of_date = :as_of_date
            """
        )
        with self.engine.begin() as conn:
            df = pd.read_sql(sql, conn, params={"as_of_date": as_of_date})
        return df if not df.empty else None

    def _load_source_rows(
        self,
        as_of_ts: pd.Timestamp,
        universe_df: Optional[pd.DataFrame],
        ciks: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        if self.engine is None:
            raise ValueError("_load_source_rows requires a DB engine.")

        columns = [
            self.config.cik_col,
            self.config.ticker_col,
            self.config.accession_col,
            self.config.form_col,
            self.config.filing_date_col,
            self.config.acceptance_col,
            self.config.report_period_end_col,
            *self.config.metric_names(),
        ]
        select_cols = ",\n                ".join(
            [f"CAST({self.config.cik_col} AS TEXT) AS {self.config.cik_col}"]
            + [col for col in columns if col != self.config.cik_col]
        )

        if ciks:
            cik_list = [normalize_cik_text(c) for c in ciks if normalize_cik_text(c)]
        elif universe_df is not None and self.config.cik_col in universe_df.columns:
            cik_list = [
                normalize_cik_text(c)
                for c in universe_df[self.config.cik_col].tolist()
                if normalize_cik_text(c)
            ]
        else:
            cik_list = []

        params: Dict[str, Any] = {
            "as_of_ts": as_of_ts.to_pydatetime(),
            "as_of_date": as_of_ts.date().isoformat(),
        }
        filters = [
            f"""(
                {self.config.acceptance_col} <= :as_of_ts
                OR (
                    {self.config.acceptance_col} IS NULL
                    AND {self.config.filing_date_col} IS NOT NULL
                    AND {self.config.filing_date_col} <= :as_of_date
                )
            )""",
            f"{self.config.accession_col} IS NOT NULL",
        ]
        if cik_list:
            bind_text_names: List[str] = []
            bind_int_names: List[str] = []
            for idx, cik in enumerate(cik_list):
                text_name = f"cik_text_{idx}"
                int_name = f"cik_int_{idx}"
                params[text_name] = str(cik)
                params[int_name] = int(str(cik))
                bind_text_names.append(f":{text_name}")
                bind_int_names.append(f":{int_name}")
            filters.append(
                "("
                f"CAST({self.config.cik_col} AS TEXT) IN ({', '.join(bind_text_names)}) "
                f"OR CAST({self.config.cik_col} AS INTEGER) IN ({', '.join(bind_int_names)})"
                ")"
            )

        sql = text(
            f"""
            SELECT
                {select_cols}
            FROM {self.config.source_table}
            WHERE {' AND '.join(filters)}
            """
        )
        with self.engine.begin() as conn:
            df = pd.read_sql(sql, conn, params=params)
        return df

    def _load_metric_source_rows(self, source_df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if self.engine is None:
            raise ValueError("_load_metric_source_rows requires a DB engine.")
        if source_df.empty:
            return pd.DataFrame()

        accessions = (
            source_df[self.config.accession_col]
            .dropna()
            .astype(str)
            .drop_duplicates()
            .tolist()
        )
        if not accessions:
            return pd.DataFrame()

        params: Dict[str, Any] = {}
        bind_names: List[str] = []
        for idx, accession in enumerate(accessions):
            name = f"acc_{idx}"
            params[name] = accession
            bind_names.append(f":{name}")

        sql = text(
            f"""
            SELECT *
            FROM {self.config.metric_source_table}
            WHERE {self.config.metric_accession_col} IN ({', '.join(bind_names)})
            """
        )
        with self.engine.begin() as conn:
            return pd.read_sql(sql, conn, params=params)

    # ---------- Normalization / preparation ----------

    def _normalize_alias_mapping(self, alias_df: Optional[pd.DataFrame]) -> pd.DataFrame:
        if alias_df is None or alias_df.empty:
            return pd.DataFrame(
                columns=["ticker", "canonical_ticker", "cik", "ticker_role", "alias_group", "xref_source"]
            )

        df = alias_df.copy()
        for col in [self.config.alias_ticker_1_col, self.config.alias_ticker_2_col]:
            if col not in df.columns:
                raise ValueError(f"Alias mapping is missing required column: {col}")
        if self.config.alias_cik_col not in df.columns:
            raise ValueError(f"Alias mapping is missing required column: {self.config.alias_cik_col}")

        long_rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            cik = normalize_cik_text(row.get(self.config.alias_cik_col))
            t1 = normalize_ticker(row.get(self.config.alias_ticker_1_col))
            t2 = normalize_ticker(row.get(self.config.alias_ticker_2_col))
            if not cik or not t1:
                continue
            alias_group = f"{cik}|{t1}"
            long_rows.append(
                {
                    "ticker": t1,
                    "canonical_ticker": t1,
                    "cik": cik,
                    "ticker_role": "primary",
                    "alias_group": alias_group,
                    "xref_source": "alias_mapping",
                }
            )
            if t2:
                long_rows.append(
                    {
                        "ticker": t2,
                        "canonical_ticker": t1,
                        "cik": cik,
                        "ticker_role": "alias",
                        "alias_group": alias_group,
                        "xref_source": "alias_mapping",
                    }
                )

        out = pd.DataFrame(long_rows).drop_duplicates(subset=["ticker", "cik"]).reset_index(drop=True)
        return out

    def _normalize_universe(self, universe_df: Optional[pd.DataFrame], alias_long_df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if universe_df is None or universe_df.empty:
            return None

        df = universe_df.copy()
        if self.config.ticker_col in df.columns:
            df[self.config.ticker_col] = df[self.config.ticker_col].map(normalize_ticker)
        else:
            df[self.config.ticker_col] = None
        if self.config.cik_col in df.columns:
            df[self.config.cik_col] = df[self.config.cik_col].map(normalize_cik_text)
        else:
            df[self.config.cik_col] = None

        alias_by_ticker = alias_long_df.rename(columns={"ticker": self.config.ticker_col, "cik": "_alias_cik"})
        df = df.merge(
            alias_by_ticker[[self.config.ticker_col, "_alias_cik", "canonical_ticker", "ticker_role", "xref_source"]],
            on=self.config.ticker_col,
            how="left",
        )

        # Fill or correct CIK from alias xref when available.
        df[self.config.cik_col] = df[self.config.cik_col].where(df[self.config.cik_col].notna(), df["_alias_cik"])
        conflict_mask = df[self.config.cik_col].notna() & df["_alias_cik"].notna() & (df[self.config.cik_col] != df["_alias_cik"])
        if conflict_mask.any():
            conflict_rows = df.loc[conflict_mask, [self.config.ticker_col, self.config.cik_col, "_alias_cik"]]
            LOGGER.warning("Universe CIK conflicts found; alias mapping CIK will override for rows:\n%s", conflict_rows.to_string(index=False))
            df.loc[conflict_mask, self.config.cik_col] = df.loc[conflict_mask, "_alias_cik"]

        df["canonical_ticker"] = df["canonical_ticker"].where(df["canonical_ticker"].notna(), df[self.config.ticker_col])
        df["ticker_role"] = df["ticker_role"].where(df["ticker_role"].notna(), "self")
        df["xref_source"] = df["xref_source"].where(df["xref_source"].notna(), "universe")

        # De-dupe by ticker/cik.
        subset_cols = [self.config.ticker_col]
        if self.config.cik_col in df.columns:
            subset_cols.append(self.config.cik_col)
        df = df.drop_duplicates(subset=subset_cols).reset_index(drop=True)
        return df.drop(columns=["_alias_cik"], errors="ignore")

    def _normalize_issuer_profile(self, issuer_profile_df: Optional[pd.DataFrame], alias_long_df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if issuer_profile_df is None or issuer_profile_df.empty:
            return None
        df = issuer_profile_df.copy()

        ticker_candidates = [col for col in df.columns if col.lower() == self.config.ticker_col.lower()]
        if ticker_candidates:
            ticker_col = ticker_candidates[0]
            df[self.config.ticker_col] = df[ticker_col].map(normalize_ticker)
        else:
            df[self.config.ticker_col] = None

        if self.config.cik_col in df.columns:
            df[self.config.cik_col] = df[self.config.cik_col].map(normalize_cik_text)
        else:
            df[self.config.cik_col] = None

        alias_by_ticker = alias_long_df.rename(columns={"ticker": self.config.ticker_col, "cik": "_alias_cik"})
        df = df.merge(alias_by_ticker[[self.config.ticker_col, "_alias_cik"]], on=self.config.ticker_col, how="left")
        df[self.config.cik_col] = df[self.config.cik_col].where(df[self.config.cik_col].notna(), df["_alias_cik"])

        keep_cols = [self.config.cik_col, self.config.ticker_col]
        for col in [self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]:
            if col not in df.columns:
                df[col] = None
            cleaned = df[col].astype("string").str.strip()
            df[col] = cleaned.where(cleaned.notna() & cleaned.ne(""), None).astype("object")
            keep_cols.append(col)

        # First preference by cik, fallback by ticker downstream.
        out = df[keep_cols].drop_duplicates().reset_index(drop=True)
        return out

    def _normalize_metric_mapping_df(self, metric_mapping_df: Optional[pd.DataFrame]) -> pd.DataFrame:
        df = metric_mapping_df.copy() if metric_mapping_df is not None else default_metric_mapping_df()
        required = {"metric_name", "source_kind", "taxonomy", "concept_name", "priority"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"metric_mapping_df missing required columns: {sorted(missing)}")
        if "industry_aggregate" not in df.columns:
            df["industry_aggregate"] = None
        if "component_group" not in df.columns:
            df["component_group"] = None
        if "wide_column_name" not in df.columns:
            df["wide_column_name"] = None
        if "period_type" not in df.columns:
            df["period_type"] = None
        df["taxonomy_key"] = df["taxonomy"].astype(str).str.lower()
        df["concept_key"] = df["concept_name"].astype(str).str.lower()
        df["priority"] = pd.to_numeric(df["priority"], errors="coerce").fillna(9999).astype(int)
        return df.reset_index(drop=True)

    def _normalize_metric_facts(self, metric_facts_df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if metric_facts_df is None or metric_facts_df.empty:
            return None
        df = metric_facts_df.copy()

        required = [
            self.config.metric_cik_col,
            self.config.metric_accession_col,
            self.config.metric_concept_col,
            self.config.metric_value_col,
        ]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"metric_facts_df missing required columns: {missing}")

        df["cik"] = df[self.config.metric_cik_col].map(normalize_cik_text)
        df["accession_number"] = df[self.config.metric_accession_col].astype(str)
        if self.config.metric_period_end_col in df.columns:
            df["metric_period_end"] = _normalize_date_col(df[self.config.metric_period_end_col])
        else:
            df["metric_period_end"] = pd.NaT
        df["metric_period_end_key"] = df["metric_period_end"].dt.strftime("%Y-%m-%d").fillna("__NA__")
        if self.config.metric_taxonomy_col in df.columns:
            df["taxonomy"] = df[self.config.metric_taxonomy_col].astype(str).str.lower()
        else:
            df["taxonomy"] = None
        df["concept_name"] = df[self.config.metric_concept_col].astype(str)
        df["concept_key"] = df["concept_name"].str.lower()
        df["fact_value"] = pd.to_numeric(df[self.config.metric_value_col], errors="coerce")
        df = df[df["fact_value"].notna()].copy()

        if self.config.metric_context_col in df.columns:
            df["context_id"] = df[self.config.metric_context_col].where(df[self.config.metric_context_col].notna(), None).astype(object)
        else:
            df["context_id"] = None
        if self.config.metric_unit_col in df.columns:
            df["unit"] = df[self.config.metric_unit_col].where(df[self.config.metric_unit_col].notna(), None).astype(object)
        else:
            df["unit"] = None
        if self.config.metric_period_type_col in df.columns:
            raw_period_type = df[self.config.metric_period_type_col].copy()
            period_type = raw_period_type.where(raw_period_type.notna(), None)
            df["period_type"] = period_type.astype(str).str.lower()
            df.loc[raw_period_type.isna(), "period_type"] = None
        else:
            df["period_type"] = None
        if self.config.metric_dimension_count_col in df.columns:
            df["dimension_count"] = pd.to_numeric(df[self.config.metric_dimension_count_col], errors="coerce").fillna(0).astype(int)
        else:
            df["dimension_count"] = 0
        if self.config.metric_statement_col in df.columns:
            df["statement_type"] = df[self.config.metric_statement_col].astype(str)
        else:
            df["statement_type"] = None

        df["dimension_rank"] = df["dimension_count"]
        return df.reset_index(drop=True)

    def _prepare_source_base(self, df: pd.DataFrame, as_of_ts: pd.Timestamp) -> pd.DataFrame:
        if df.empty:
            return df.copy()

        out = df.copy()
        out[self.config.cik_col] = out[self.config.cik_col].map(normalize_cik_text)
        if self.config.ticker_col in out.columns:
            out[self.config.ticker_col] = out[self.config.ticker_col].map(normalize_ticker)
        else:
            out[self.config.ticker_col] = None

        out[self.config.filing_date_col] = _normalize_date_col(out[self.config.filing_date_col])
        out[self.config.acceptance_col] = pd.to_datetime(out[self.config.acceptance_col], errors="coerce", utc=True)
        out[self.config.report_period_end_col] = _normalize_date_col(out[self.config.report_period_end_col])

        filing_eod = out[self.config.filing_date_col] + pd.Timedelta(hours=23, minutes=59, seconds=59)
        out["effective_acceptance_datetime"] = out[self.config.acceptance_col].fillna(filing_eod)
        out["acceptance_source"] = out[self.config.acceptance_col].notna().map(
            {True: "acceptance_datetime", False: "filing_date_eod_fallback"}
        )

        cutoff_ts = as_of_ts
        if self.config.publication_lag_minutes > 0:
            cutoff_ts = cutoff_ts - pd.Timedelta(minutes=self.config.publication_lag_minutes)
        out = out[out["effective_acceptance_datetime"] <= cutoff_ts].copy()

        out["canonical_form"] = out[self.config.form_col].map(canonical_form)
        out["form_family"] = out[self.config.form_col].map(form_family)
        out["is_periodic_form"] = out[self.config.form_col].map(is_periodic_form)
        out["period_end_key"] = out[self.config.report_period_end_col].dt.strftime("%Y-%m-%d").fillna("__NA__")
        out["acceptance_rank_key"] = out["effective_acceptance_datetime"].astype("int64")
        period_key = out[self.config.report_period_end_col].fillna(pd.Timestamp("1970-01-01", tz="UTC"))
        out["period_end_rank_key"] = period_key.astype("int64")

        if "primary_taxonomy" not in out.columns:
            out["primary_taxonomy"] = None
        if "taxonomy_profile" not in out.columns:
            out["taxonomy_profile"] = None
        for col in [self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]:
            if col not in out.columns:
                out[col] = None

        return out.reset_index(drop=True)

    def _attach_issuer_profile(self, prepared_df: pd.DataFrame, issuer_profile_df: Optional[pd.DataFrame]) -> pd.DataFrame:
        if prepared_df.empty or issuer_profile_df is None or issuer_profile_df.empty:
            return prepared_df

        out = prepared_df.copy()
        profile_df = issuer_profile_df.copy()

        by_cik = (
            profile_df
            .dropna(subset=[self.config.cik_col])
            .drop_duplicates(subset=[self.config.cik_col])
            [[self.config.cik_col, self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]]
        )
        out = out.merge(
            by_cik,
            on=self.config.cik_col,
            how="left",
            suffixes=("", "_profile_cik"),
        )
        for col in [self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]:
            profile_col = f"{col}_profile_cik"
            if profile_col in out.columns:
                out[col] = out[col].where(out[col].notna(), out[profile_col])
                out = out.drop(columns=[profile_col])

        by_ticker = (
            profile_df
            .dropna(subset=[self.config.ticker_col])
            .drop_duplicates(subset=[self.config.ticker_col])
            [[self.config.ticker_col, self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]]
        )
        out = out.merge(
            by_ticker,
            on=self.config.ticker_col,
            how="left",
            suffixes=("", "_profile_ticker"),
        )
        for col in [self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]:
            profile_col = f"{col}_profile_ticker"
            if profile_col in out.columns:
                out[col] = out[col].where(out[col].notna(), out[profile_col])
                out = out.drop(columns=[profile_col])

        return out

    # ---------- Same-filing metric repair ----------

    def _apply_same_filing_metric_repairs(
        self,
        prepared_df: pd.DataFrame,
        metric_facts_df: Optional[pd.DataFrame],
        metric_mapping_df: pd.DataFrame,
    ) -> pd.DataFrame:
        if prepared_df.empty:
            return prepared_df

        out_rows: List[Dict[str, Any]] = []

        facts_by_accession: Dict[Tuple[str, str], pd.DataFrame] = {}
        if metric_facts_df is not None and not metric_facts_df.empty:
            for (cik, accession), group in metric_facts_df.groupby(["cik", "accession_number"], sort=False):
                facts_by_accession[(str(cik), str(accession))] = group.reset_index(drop=True)

        for _, row in prepared_df.iterrows():
            row_dict = row.to_dict()
            cik = normalize_cik_text(row_dict.get(self.config.cik_col))
            accession = str(row_dict.get(self.config.accession_col) or "")
            row_period = row_dict.get(self.config.report_period_end_col)
            row_period_key = row_dict.get("period_end_key", "__NA__")
            industry_aggregate = row_dict.get(self.config.industry_aggregate_col)
            row_facts = facts_by_accession.get((str(cik), accession))
            taxonomy_profile, primary_taxonomy = self._derive_taxonomy_profile(row_facts, row_dict)

            row_dict["taxonomy_profile"] = taxonomy_profile
            row_dict["primary_taxonomy"] = primary_taxonomy

            same_filing_provenance: Dict[str, Any] = {}
            same_filing_status: Dict[str, str] = {}

            for metric in self.config.metric_names():
                current_value = row_dict.get(metric)
                if pd.notna(current_value):
                    same_filing_status[metric] = "reported_source_row"
                    same_filing_provenance[metric] = self._same_filing_provenance_for_source_row(row_dict, metric)
                    continue

                if not self.config.same_filing_repair_enabled:
                    same_filing_status[metric] = "mapping_gap_same_filing"
                    same_filing_provenance[metric] = {
                        "metric": metric,
                        "status": "mapping_gap_same_filing",
                        "reason": "same_filing_repair_disabled",
                    }
                    continue

                facts_exact, facts_any = self._facts_for_row(row_facts, row_period)
                direct = self._select_direct_same_filing_metric(
                    metric=metric,
                    industry_aggregate=industry_aggregate,
                    primary_taxonomy=primary_taxonomy,
                    facts_exact=facts_exact,
                    facts_any=facts_any,
                    metric_mapping_df=metric_mapping_df,
                )
                if direct is not None:
                    row_dict[metric] = direct["value"]
                    same_filing_status[metric] = direct["status"]
                    same_filing_provenance[metric] = direct["provenance"]
                    continue

                formula = self._evaluate_sector_formula(
                    row_dict=row_dict,
                    metric=metric,
                    industry_aggregate=industry_aggregate,
                    primary_taxonomy=primary_taxonomy,
                    facts_exact=facts_exact,
                    facts_any=facts_any,
                    metric_mapping_df=metric_mapping_df,
                )
                if formula is not None:
                    row_dict[metric] = formula["value"]
                    same_filing_status[metric] = formula["status"]
                    same_filing_provenance[metric] = formula["provenance"]
                    continue

                same_filing_status[metric] = "mapping_gap_same_filing"
                same_filing_provenance[metric] = {
                    "metric": metric,
                    "status": "mapping_gap_same_filing",
                    "reason": "no_same_filing_direct_or_formula_match",
                    "industry_aggregate": industry_aggregate,
                    "primary_taxonomy": primary_taxonomy,
                }

            row_dict["same_filing_metric_provenance_json"] = json.dumps(
                same_filing_provenance,
                default=self._json_default,
                sort_keys=True,
            )
            row_dict["same_filing_metric_status_json"] = json.dumps(
                same_filing_status,
                default=self._json_default,
                sort_keys=True,
            )
            out_rows.append(row_dict)

        return pd.DataFrame(out_rows)

    def _derive_taxonomy_profile(
        self,
        row_facts: Optional[pd.DataFrame],
        row_dict: Mapping[str, Any],
    ) -> Tuple[Optional[str], Optional[str]]:
        existing_primary = row_dict.get("primary_taxonomy")
        existing_profile = row_dict.get("taxonomy_profile")
        if isinstance(existing_primary, str) and existing_primary.strip():
            primary = existing_primary.strip().lower()
            profile = existing_profile.strip().lower() if isinstance(existing_profile, str) and existing_profile.strip() else primary
            return profile, primary

        if row_facts is None or row_facts.empty or "taxonomy" not in row_facts.columns:
            return None, None

        taxonomies = [t for t in row_facts["taxonomy"].dropna().astype(str).str.lower().tolist() if t]
        if not taxonomies:
            return None, None

        uniq = sorted(set(taxonomies))
        if len(uniq) == 1:
            return uniq[0], uniq[0]

        counts = pd.Series(taxonomies).value_counts()
        primary = str(counts.index[0]).lower()
        return "mixed:" + ",".join(uniq), primary

    def _facts_for_row(
        self,
        row_facts: Optional[pd.DataFrame],
        row_period: Any,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        empty = pd.DataFrame()
        if row_facts is None or row_facts.empty:
            return empty, empty

        facts_any = row_facts.copy()
        if pd.notna(row_period):
            row_period_ts = pd.Timestamp(row_period)
            exact = facts_any[facts_any["metric_period_end"] == row_period_ts].copy()
            facts_any["period_distance_days"] = (
                facts_any["metric_period_end"] - row_period_ts
            ).abs().dt.days.fillna(10_000).astype(int)
            facts_any = facts_any.sort_values(["period_distance_days", "dimension_rank"], ascending=[True, True]).reset_index(drop=True)
            exact = exact.sort_values(["dimension_rank"], ascending=[True]).reset_index(drop=True)
            return exact, facts_any
        facts_any["period_distance_days"] = 10_000
        facts_any = facts_any.sort_values(["dimension_rank"], ascending=[True]).reset_index(drop=True)
        return empty, facts_any

    def _select_direct_same_filing_metric(
        self,
        metric: str,
        industry_aggregate: Optional[str],
        primary_taxonomy: Optional[str],
        facts_exact: pd.DataFrame,
        facts_any: pd.DataFrame,
        metric_mapping_df: pd.DataFrame,
    ) -> Optional[Dict[str, Any]]:
        candidates = metric_mapping_df[
            (metric_mapping_df["metric_name"] == metric) &
            (metric_mapping_df["source_kind"] == "direct")
        ].copy()
        if candidates.empty:
            return None
        candidates = self._sort_mapping_candidates(candidates, industry_aggregate, primary_taxonomy)
        match = self._find_best_fact_match(candidates, facts_exact, facts_any)
        if match is None:
            return None

        return {
            "value": match["fact_value"],
            "status": "mapped_same_filing_direct",
            "provenance": {
                "metric": metric,
                "status": "mapped_same_filing_direct",
                "source_kind": "fact_direct",
                "source_taxonomy": match.get("taxonomy"),
                "source_concept": match.get("concept_name"),
                "source_context_id": match.get("context_id"),
                "source_unit": match.get("unit"),
                "source_period_end": self._json_default(match.get("metric_period_end")),
                "mapping_priority": int(match.get("_mapping_priority", 9999)),
                "period_distance_days": int(match.get("period_distance_days", 10_000)),
            },
        }

    def _evaluate_sector_formula(
        self,
        row_dict: Mapping[str, Any],
        metric: str,
        industry_aggregate: Optional[str],
        primary_taxonomy: Optional[str],
        facts_exact: pd.DataFrame,
        facts_any: pd.DataFrame,
        metric_mapping_df: pd.DataFrame,
    ) -> Optional[Dict[str, Any]]:
        if not industry_aggregate:
            return None
        rule = DEFAULT_FORMULA_RULES.get((metric, industry_aggregate))
        if rule is None:
            return None

        formula_name = str(rule["formula_name"])
        component_groups = list(rule["component_groups"])
        min_components = int(rule.get("min_components", 1))

        component_values: List[Dict[str, Any]] = []
        total_value = 0.0

        for group_name in component_groups:
            candidates = metric_mapping_df[
                (metric_mapping_df["metric_name"] == metric) &
                (metric_mapping_df["source_kind"] == "formula_component") &
                (metric_mapping_df["component_group"] == group_name)
            ].copy()
            if candidates.empty:
                continue
            candidates = self._sort_mapping_candidates(candidates, industry_aggregate, primary_taxonomy)

            component_match = self._find_best_fact_or_wide_component_match(candidates, facts_exact, facts_any, row_dict)
            if component_match is None:
                continue
            total_value += float(component_match["value"])
            component_values.append(component_match)

        if len(component_values) < min_components:
            return None

        complete = len(component_values) == len(component_groups)
        status = "mapped_same_filing_formula_complete" if complete else "mapped_same_filing_formula_partial"
        return {
            "value": total_value,
            "status": status,
            "provenance": {
                "metric": metric,
                "status": status,
                "source_kind": "sector_formula",
                "formula_name": formula_name,
                "industry_aggregate": industry_aggregate,
                "component_count_found": len(component_values),
                "component_count_expected": len(component_groups),
                "components": component_values,
            },
        }

    def _sort_mapping_candidates(
        self,
        candidates: pd.DataFrame,
        industry_aggregate: Optional[str],
        primary_taxonomy: Optional[str],
    ) -> pd.DataFrame:
        out = candidates.copy()
        out["_industry_specific"] = 0
        if industry_aggregate:
            out["_industry_specific"] = (out["industry_aggregate"] == industry_aggregate).astype(int)
            out = out[(out["industry_aggregate"].isna()) | (out["industry_aggregate"] == industry_aggregate)].copy()
        out["_taxonomy_primary"] = 0
        if primary_taxonomy:
            out["_taxonomy_primary"] = (out["taxonomy_key"] == str(primary_taxonomy).lower()).astype(int)
        out = out.sort_values(
            ["_industry_specific", "_taxonomy_primary", "priority"],
            ascending=[False, False, True],
        ).reset_index(drop=True)
        return out

    def _find_best_fact_match(
        self,
        candidates: pd.DataFrame,
        facts_exact: pd.DataFrame,
        facts_any: pd.DataFrame,
    ) -> Optional[Dict[str, Any]]:
        for _, cand in candidates.iterrows():
            for facts_source, source_label in [(facts_exact, "exact_period"), (facts_any, "nearest_period")]:
                if facts_source is None or facts_source.empty:
                    continue
                matches = facts_source[
                    (facts_source["concept_key"] == cand["concept_key"]) &
                    (facts_source["taxonomy"] == cand["taxonomy_key"])
                ].copy()
                if "period_type" in facts_source.columns and pd.notna(cand.get("period_type")):
                    matches = matches[(matches["period_type"].isna()) | (matches["period_type"] == str(cand["period_type"]).lower())].copy()
                if matches.empty:
                    continue
                if "period_distance_days" not in matches.columns:
                    matches["period_distance_days"] = 0
                matches = matches.sort_values(
                    ["dimension_rank", "period_distance_days"],
                    ascending=[True, True],
                )
                best = matches.iloc[0].to_dict()
                best["_mapping_priority"] = int(cand["priority"])
                best["_mapping_source_label"] = source_label
                return best
        return None

    def _find_best_fact_or_wide_component_match(
        self,
        candidates: pd.DataFrame,
        facts_exact: pd.DataFrame,
        facts_any: pd.DataFrame,
        row_dict: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        fact_match = self._find_best_fact_match(candidates, facts_exact, facts_any)
        if fact_match is not None:
            return {
                "value": float(fact_match["fact_value"]),
                "source_type": "fact",
                "source_taxonomy": fact_match.get("taxonomy"),
                "source_concept": fact_match.get("concept_name"),
                "source_context_id": fact_match.get("context_id"),
                "source_unit": fact_match.get("unit"),
                "source_period_end": self._json_default(fact_match.get("metric_period_end")),
                "period_distance_days": int(fact_match.get("period_distance_days", 10_000)),
            }

        for _, cand in candidates.iterrows():
            wide_col = cand.get("wide_column_name")
            if wide_col and wide_col in row_dict and pd.notna(row_dict.get(wide_col)):
                return {
                    "value": float(row_dict.get(wide_col)),
                    "source_type": "wide_column",
                    "source_taxonomy": cand.get("taxonomy"),
                    "source_concept": cand.get("concept_name"),
                    "wide_column_name": wide_col,
                    "period_distance_days": 0,
                }
        return None

    def _same_filing_provenance_for_source_row(self, row_dict: Mapping[str, Any], metric: str) -> Dict[str, Any]:
        return {
            "metric": metric,
            "status": "reported_source_row",
            "source_kind": "wide_source_row",
            "source_accession_number": row_dict.get(self.config.accession_col),
            "source_form_type": row_dict.get(self.config.form_col),
            "source_filing_date": self._json_default(row_dict.get(self.config.filing_date_col)),
            "source_acceptance_datetime": self._json_default(row_dict.get("effective_acceptance_datetime")),
            "source_anchor_period_end": self._json_default(row_dict.get(self.config.report_period_end_col)),
        }

    def _recompute_bundle_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df.copy()

        out = df.copy()
        out["bundle_non_null_count"] = 0
        out["bundle_completeness_score"] = 0
        weights = self.config.metric_weights()
        for metric in self.config.metric_names():
            non_null = out[metric].notna().astype(int)
            out["bundle_non_null_count"] += non_null
            out["bundle_completeness_score"] += non_null * weights.get(metric, 1)
        return out

    # ---------- Accession selection ----------

    def _choose_best_row_per_accession(self, prepared_df: pd.DataFrame) -> pd.DataFrame:
        if prepared_df.empty:
            return prepared_df.copy()
        ranked = prepared_df.sort_values(
            [
                self.config.cik_col,
                self.config.accession_col,
                "bundle_completeness_score",
                "bundle_non_null_count",
                "period_end_rank_key",
                "acceptance_rank_key",
            ],
            ascending=[True, True, False, False, False, False],
        )
        best = ranked.groupby([self.config.cik_col, self.config.accession_col], as_index=False).head(1).copy()
        best = best.rename(columns={self.config.report_period_end_col: "anchor_period_end"})
        best["bundle_selection_reason"] = "best_row_within_accession_by_completeness"
        return best.reset_index(drop=True)

    # ---------- Strict / entity snapshot ----------

    def _pick_strict_bundle_for_cik(self, cik_df: pd.DataFrame) -> Optional[pd.Series]:
        if cik_df.empty:
            return None

        periodic = cik_df[cik_df["is_periodic_form"]].copy()
        usable_periodic = periodic[periodic["bundle_non_null_count"] >= self.config.strict_min_non_null_metrics].copy()

        def latest(df: pd.DataFrame) -> Optional[pd.Series]:
            if df.empty:
                return None
            ordered = df.sort_values(
                ["acceptance_rank_key", "bundle_completeness_score", "period_end_rank_key"],
                ascending=[False, False, False],
            )
            return ordered.iloc[0]

        picked = latest(usable_periodic)
        if picked is not None:
            picked = picked.copy()
            picked["strict_selection_reason"] = "latest_usable_periodic_filing"
            return picked

        picked = latest(periodic)
        if picked is not None:
            picked = picked.copy()
            picked["strict_selection_reason"] = "latest_periodic_filing_sparse_fallback"
            return picked

        if self.config.allow_supplemental_as_anchor_when_no_periodic:
            supplemental = cik_df[cik_df["form_family"] == "supplemental"].copy()
            supplemental = supplemental[supplemental["bundle_non_null_count"] >= self.config.supplemental_min_non_null_metrics]
            picked = latest(supplemental)
            if picked is not None:
                picked = picked.copy()
                picked["strict_selection_reason"] = "supplemental_anchor_no_periodic_available"
                return picked

        picked = latest(cik_df)
        if picked is not None:
            picked = picked.copy()
            picked["strict_selection_reason"] = "latest_any_form_last_resort"
            return picked
        return None

    def _build_entity_strict_snapshot(
        self,
        accession_bundles_df: pd.DataFrame,
        as_of_ts: pd.Timestamp,
        universe_df: Optional[pd.DataFrame],
    ) -> pd.DataFrame:
        records: List[Dict[str, Any]] = []
        if not accession_bundles_df.empty:
            for _, cik_df in accession_bundles_df.groupby(self.config.cik_col, sort=False):
                picked = self._pick_strict_bundle_for_cik(cik_df)
                if picked is None:
                    continue
                row = picked.to_dict()
                row["as_of_date"] = as_of_ts.date().isoformat()
                row["as_of_timestamp"] = as_of_ts.isoformat()
                row["snapshot_kind"] = "strict"
                row["null_reason"] = self._derive_row_null_reason_from_values(row)
                records.append(row)
        strict_df = pd.DataFrame(records)
        if strict_df.empty:
            strict_df = pd.DataFrame(columns=self._entity_strict_output_columns())

        strict_df = self._attach_missing_universe_rows_entity(strict_df, as_of_ts, universe_df, snapshot_kind="strict")

        for metric in self.config.metric_names():
            if metric not in strict_df.columns:
                strict_df[metric] = pd.NA
        if "same_filing_metric_provenance_json" not in strict_df.columns:
            strict_df["same_filing_metric_provenance_json"] = "{}"
        if "same_filing_metric_status_json" not in strict_df.columns:
            strict_df["same_filing_metric_status_json"] = "{}"

        return strict_df.reindex(columns=self._entity_strict_output_columns())

    # ---------- Filled / entity snapshot ----------

    def _build_entity_filled_snapshot(
        self,
        entity_strict_df: pd.DataFrame,
        accession_bundles_df: pd.DataFrame,
        as_of_ts: pd.Timestamp,
        universe_df: Optional[pd.DataFrame],
    ) -> pd.DataFrame:
        max_days_by_metric = self.config.metric_max_backfill_days()
        by_cik: Dict[str, pd.DataFrame] = {}
        if not accession_bundles_df.empty:
            for cik, group in accession_bundles_df.groupby(self.config.cik_col, sort=False):
                by_cik[str(cik)] = group.sort_values(
                    ["acceptance_rank_key", "period_end_rank_key"],
                    ascending=[False, False],
                )

        records: List[Dict[str, Any]] = []
        for _, strict_row in entity_strict_df.iterrows():
            row = strict_row.to_dict()
            row["snapshot_kind"] = "filled"
            row["strict_accession_number"] = strict_row.get(self.config.accession_col)
            row["strict_anchor_period_end"] = strict_row.get("anchor_period_end")

            strict_same_prov = _safe_json_loads(strict_row.get("same_filing_metric_provenance_json"))
            strict_same_status = _safe_json_loads(strict_row.get("same_filing_metric_status_json"))

            final_provenance: Dict[str, Any] = {}
            final_status: Dict[str, str] = {}
            cik = normalize_cik_text(strict_row.get(self.config.cik_col))
            candidates = by_cik.get(str(cik), pd.DataFrame()) if cik else pd.DataFrame()

            for metric in self.config.metric_names():
                if pd.notna(strict_row.get(metric)):
                    final_status[metric] = str(strict_same_status.get(metric, "strict_same_filing"))
                    final_provenance[metric] = strict_same_prov.get(metric, self._metric_provenance_dict(strict_row, metric, "strict_same_filing", as_of_ts))
                    continue

                donor = None
                strict_acceptance = strict_row.get("effective_acceptance_datetime")
                if not candidates.empty and pd.notna(strict_acceptance):
                    strict_acceptance = pd.Timestamp(strict_acceptance)
                    prior = candidates[candidates["effective_acceptance_datetime"] < strict_acceptance].copy()
                    if not prior.empty:
                        # only prior periodic donors for backfill
                        prior = prior[prior["is_periodic_form"].fillna(False)].copy()
                        max_days = max_days_by_metric.get(metric, self.config.lookback_days)
                        prior = prior[(strict_acceptance - prior["effective_acceptance_datetime"]).dt.days <= max_days].copy()
                        prior = prior[prior[metric].notna()].copy()
                        if not prior.empty:
                            donor = prior.sort_values(
                                ["acceptance_rank_key", "period_end_rank_key"],
                                ascending=[False, False],
                            ).iloc[0]

                if donor is not None:
                    row[metric] = donor[metric]
                    final_status[metric] = "backfilled_prior_periodic"
                    final_provenance[metric] = self._metric_provenance_dict(donor, metric, "backfilled_prior_periodic", as_of_ts)
                else:
                    row[metric] = pd.NA
                    prior_reason = strict_same_status.get(metric, "mapping_gap_same_filing")
                    final_status[metric] = "missing"
                    final_provenance[metric] = {
                        "metric": metric,
                        "status": "missing",
                        "reason": "no_prior_non_null_periodic_donor_within_lookback",
                        "same_filing_status": prior_reason,
                    }

            row["metric_provenance_json"] = json.dumps(final_provenance, default=self._json_default, sort_keys=True)
            row["metric_status_json"] = json.dumps(final_status, default=self._json_default, sort_keys=True)
            row["null_reason"] = self._derive_row_null_reason_from_statuses(final_status)
            records.append(row)

        filled_df = pd.DataFrame(records)
        if filled_df.empty:
            filled_df = pd.DataFrame(columns=self._entity_filled_output_columns())

        filled_df = self._attach_missing_universe_rows_entity(
            filled_df,
            as_of_ts,
            universe_df,
            snapshot_kind="filled",
            filled=True,
        )
        for metric in self.config.metric_names():
            if metric not in filled_df.columns:
                filled_df[metric] = pd.NA
        if "metric_provenance_json" not in filled_df.columns:
            filled_df["metric_provenance_json"] = "{}"
        if "metric_status_json" not in filled_df.columns:
            filled_df["metric_status_json"] = "{}"

        return filled_df.reindex(columns=self._entity_filled_output_columns())

    # ---------- Security fan-out ----------

    def _build_security_snapshot(
        self,
        entity_snapshot_df: pd.DataFrame,
        universe_df: Optional[pd.DataFrame],
        alias_long_df: pd.DataFrame,
        as_of_ts: pd.Timestamp,
        snapshot_kind: str,
    ) -> pd.DataFrame:
        security_universe = self._build_security_universe(entity_snapshot_df, universe_df, alias_long_df)
        if security_universe.empty:
            return entity_snapshot_df.copy()

        entity = entity_snapshot_df.copy()
        entity = entity.rename(columns={self.config.ticker_col: "entity_ticker"})
        merged = security_universe.merge(
            entity,
            on=self.config.cik_col,
            how="left",
            suffixes=("", "_entity"),
        )

        # Coalesce issuer profile fields from security universe first, then entity.
        for col in [self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]:
            if f"{col}_entity" in merged.columns:
                merged[col] = merged[col].where(merged[col].notna(), merged[f"{col}_entity"])
                merged = merged.drop(columns=[f"{col}_entity"])

        merged["entity_ticker"] = merged["entity_ticker"].where(merged["entity_ticker"].notna(), merged.get("canonical_ticker"))
        merged[self.config.ticker_col] = merged[self.config.ticker_col].where(merged[self.config.ticker_col].notna(), merged.get("entity_ticker"))
        merged["snapshot_kind"] = snapshot_kind

        # Rows with no entity match: create clean null reasons / statuses.
        no_match = merged[self.config.accession_col].isna()
        if no_match.any():
            merged.loc[no_match, "null_reason"] = "no_eligible_source_row_before_as_of"
            if snapshot_kind == "filled":
                for metric in self.config.metric_names():
                    merged.loc[no_match, metric] = pd.NA
                merged.loc[no_match, "metric_status_json"] = json.dumps(
                    {metric: "missing" for metric in self.config.metric_names()},
                    sort_keys=True,
                )
                merged.loc[no_match, "metric_provenance_json"] = json.dumps(
                    {
                        metric: {
                            "metric": metric,
                            "status": "missing",
                            "reason": "no_eligible_source_row_before_as_of",
                        }
                        for metric in self.config.metric_names()
                    },
                    sort_keys=True,
                )
            else:
                merged.loc[no_match, "same_filing_metric_status_json"] = json.dumps(
                    {metric: "missing" for metric in self.config.metric_names()},
                    sort_keys=True,
                )
                merged.loc[no_match, "same_filing_metric_provenance_json"] = json.dumps(
                    {
                        metric: {
                            "metric": metric,
                            "status": "missing",
                            "reason": "no_eligible_source_row_before_as_of",
                        }
                        for metric in self.config.metric_names()
                    },
                    sort_keys=True,
                )

        output_cols = self._security_filled_output_columns() if snapshot_kind == "filled" else self._security_strict_output_columns()
        for col in output_cols:
            if col not in merged.columns:
                merged[col] = pd.NA
        return merged.reindex(columns=output_cols)

    def _build_security_universe(
        self,
        entity_snapshot_df: pd.DataFrame,
        universe_df: Optional[pd.DataFrame],
        alias_long_df: pd.DataFrame,
    ) -> pd.DataFrame:
        rows: List[Dict[str, Any]] = []

        if universe_df is not None and not universe_df.empty:
            security = universe_df.copy()
            # Ensure alias enrichment exists.
            if "canonical_ticker" not in security.columns or "ticker_role" not in security.columns:
                security = self._normalize_universe(security, alias_long_df)
            for _, row in security.iterrows():
                rows.append(
                    {
                        self.config.cik_col: normalize_cik_text(row.get(self.config.cik_col)),
                        self.config.ticker_col: normalize_ticker(row.get(self.config.ticker_col)),
                        "canonical_ticker": normalize_ticker(row.get("canonical_ticker")) or normalize_ticker(row.get(self.config.ticker_col)),
                        "ticker_role": row.get("ticker_role") or "self",
                        "xref_source": row.get("xref_source") or "universe",
                        self.config.sector_col: row.get(self.config.sector_col),
                        self.config.industry_col: row.get(self.config.industry_col),
                        self.config.industry_aggregate_col: row.get(self.config.industry_aggregate_col),
                    }
                )
        else:
            entity_keys = entity_snapshot_df[[self.config.cik_col, self.config.ticker_col, self.config.sector_col, self.config.industry_col, self.config.industry_aggregate_col]].drop_duplicates()
            for _, row in entity_keys.iterrows():
                cik = normalize_cik_text(row.get(self.config.cik_col))
                entity_ticker = normalize_ticker(row.get(self.config.ticker_col))
                if not cik:
                    continue
                alias_matches = alias_long_df[alias_long_df["cik"] == cik].copy() if self.config.fanout_aliases else pd.DataFrame()
                if alias_matches.empty:
                    rows.append(
                        {
                            self.config.cik_col: cik,
                            self.config.ticker_col: entity_ticker,
                            "canonical_ticker": entity_ticker,
                            "ticker_role": "self",
                            "xref_source": "entity_snapshot",
                            self.config.sector_col: row.get(self.config.sector_col),
                            self.config.industry_col: row.get(self.config.industry_col),
                            self.config.industry_aggregate_col: row.get(self.config.industry_aggregate_col),
                        }
                    )
                else:
                    for _, a in alias_matches.iterrows():
                        rows.append(
                            {
                                self.config.cik_col: cik,
                                self.config.ticker_col: a.get("ticker"),
                                "canonical_ticker": a.get("canonical_ticker"),
                                "ticker_role": a.get("ticker_role"),
                                "xref_source": a.get("xref_source"),
                                self.config.sector_col: row.get(self.config.sector_col),
                                self.config.industry_col: row.get(self.config.industry_col),
                                self.config.industry_aggregate_col: row.get(self.config.industry_aggregate_col),
                            }
                        )

        security_universe = pd.DataFrame(rows)
        if security_universe.empty:
            return security_universe
        security_universe[self.config.cik_col] = security_universe[self.config.cik_col].map(normalize_cik_text)
        security_universe[self.config.ticker_col] = security_universe[self.config.ticker_col].map(normalize_ticker)

        # Fill missing CIKs from entity snapshot ticker/canonical ticker when universe rows omit CIK.
        if self.config.cik_col in entity_snapshot_df.columns and self.config.ticker_col in entity_snapshot_df.columns:
            entity_ticker_xref = (
                entity_snapshot_df[[self.config.ticker_col, self.config.cik_col]]
                .dropna()
                .drop_duplicates(subset=[self.config.ticker_col])
                .rename(columns={self.config.cik_col: "_entity_cik"})
            )
            security_universe = security_universe.merge(entity_ticker_xref, on=self.config.ticker_col, how="left")
            security_universe[self.config.cik_col] = security_universe[self.config.cik_col].where(
                security_universe[self.config.cik_col].notna(), security_universe["_entity_cik"]
            )
            security_universe = security_universe.drop(columns=["_entity_cik"], errors="ignore")

            if "canonical_ticker" in security_universe.columns:
                entity_canonical_xref = (
                    entity_snapshot_df[[self.config.ticker_col, self.config.cik_col]]
                    .dropna()
                    .drop_duplicates(subset=[self.config.ticker_col])
                    .rename(columns={self.config.ticker_col: "canonical_ticker", self.config.cik_col: "_canonical_entity_cik"})
                )
                security_universe = security_universe.merge(entity_canonical_xref, on="canonical_ticker", how="left")
                security_universe[self.config.cik_col] = security_universe[self.config.cik_col].where(
                    security_universe[self.config.cik_col].notna(), security_universe["_canonical_entity_cik"]
                )
                security_universe = security_universe.drop(columns=["_canonical_entity_cik"], errors="ignore")

        security_universe = security_universe.dropna(subset=[self.config.cik_col, self.config.ticker_col]).drop_duplicates(
            subset=[self.config.cik_col, self.config.ticker_col]
        ).reset_index(drop=True)
        return security_universe

    # ---------- Helpers ----------

    def _metric_provenance_dict(
        self,
        row: Mapping[str, Any],
        metric: str,
        status: str,
        as_of_ts: pd.Timestamp,
    ) -> Dict[str, Any]:
        acceptance_dt = row.get("effective_acceptance_datetime")
        staleness_days = None
        if pd.notna(acceptance_dt):
            staleness_days = int((as_of_ts - pd.Timestamp(acceptance_dt)).days)
        return {
            "metric": metric,
            "status": status,
            "value": self._json_default(row.get(metric)),
            "source_accession_number": row.get(self.config.accession_col),
            "source_form_type": row.get(self.config.form_col),
            "source_filing_date": self._json_default(row.get(self.config.filing_date_col)),
            "source_acceptance_datetime": self._json_default(acceptance_dt),
            "source_anchor_period_end": self._json_default(row.get("anchor_period_end", row.get(self.config.report_period_end_col))),
            "source_selection_reason": row.get("strict_selection_reason", row.get("bundle_selection_reason")),
            "staleness_days": staleness_days,
        }

    def _attach_missing_universe_rows_entity(
        self,
        snapshot_df: pd.DataFrame,
        as_of_ts: pd.Timestamp,
        universe_df: Optional[pd.DataFrame],
        snapshot_kind: str,
        filled: bool = False,
    ) -> pd.DataFrame:
        if not (self.config.include_missing_universe_rows and universe_df is not None and not universe_df.empty):
            return snapshot_df

        current = snapshot_df.copy()
        universe_ciks = (
            universe_df[[self.config.cik_col]]
            .dropna()
            .drop_duplicates()
            .reset_index(drop=True)
        )
        current_ciks = set(current[self.config.cik_col].dropna().map(normalize_cik_text).tolist()) if self.config.cik_col in current.columns else set()
        missing_ciks = universe_ciks[~universe_ciks[self.config.cik_col].map(normalize_cik_text).isin(current_ciks)].copy()
        if missing_ciks.empty:
            return current

        # choose one representative ticker/profile per missing CIK
        missing_universe = (
            universe_df[universe_df[self.config.cik_col].map(normalize_cik_text).isin(missing_ciks[self.config.cik_col].tolist())]
            .sort_values([self.config.cik_col, "ticker_role"] if "ticker_role" in universe_df.columns else [self.config.cik_col])
            .drop_duplicates(subset=[self.config.cik_col])
        )

        filler_rows: List[Dict[str, Any]] = []
        for _, u in missing_universe.iterrows():
            row: Dict[str, Any] = {
                "as_of_date": as_of_ts.date().isoformat(),
                "as_of_timestamp": as_of_ts.isoformat(),
                self.config.cik_col: normalize_cik_text(u.get(self.config.cik_col)),
                self.config.ticker_col: normalize_ticker(u.get("canonical_ticker") or u.get(self.config.ticker_col)),
                self.config.form_col: None,
                self.config.accession_col: None,
                self.config.filing_date_col: None,
                "effective_acceptance_datetime": None,
                "acceptance_source": None,
                "anchor_period_end": None,
                "canonical_form": None,
                "form_family": None,
                "is_periodic_form": None,
                "bundle_non_null_count": 0,
                "bundle_completeness_score": 0,
                "bundle_selection_reason": None,
                "strict_selection_reason": None,
                self.config.sector_col: u.get(self.config.sector_col),
                self.config.industry_col: u.get(self.config.industry_col),
                self.config.industry_aggregate_col: u.get(self.config.industry_aggregate_col),
                "primary_taxonomy": None,
                "taxonomy_profile": None,
                "snapshot_kind": snapshot_kind,
                "null_reason": "no_eligible_source_row_before_as_of",
            }
            for metric in self.config.metric_names():
                row[metric] = pd.NA
            row["same_filing_metric_provenance_json"] = json.dumps(
                {
                    metric: {
                        "metric": metric,
                        "status": "missing",
                        "reason": "no_eligible_source_row_before_as_of",
                    }
                    for metric in self.config.metric_names()
                },
                sort_keys=True,
            )
            row["same_filing_metric_status_json"] = json.dumps(
                {metric: "missing" for metric in self.config.metric_names()},
                sort_keys=True,
            )
            if filled:
                row["strict_accession_number"] = None
                row["strict_anchor_period_end"] = None
                row["metric_provenance_json"] = json.dumps(
                    {
                        metric: {
                            "metric": metric,
                            "status": "missing",
                            "reason": "no_eligible_source_row_before_as_of",
                        }
                        for metric in self.config.metric_names()
                    },
                    sort_keys=True,
                )
                row["metric_status_json"] = json.dumps(
                    {metric: "missing" for metric in self.config.metric_names()},
                    sort_keys=True,
                )
            filler_rows.append(row)

        filler_df = pd.DataFrame(filler_rows)
        if current.empty:
            return filler_df
        return pd.concat([current, filler_df], ignore_index=True, sort=False)

    def _derive_row_null_reason_from_values(self, row: Mapping[str, Any]) -> Optional[str]:
        metric_values = [row.get(metric) for metric in self.config.metric_names()]
        if all(pd.isna(v) for v in metric_values):
            return "all_core_metrics_missing_in_strict_snapshot"
        if any(pd.isna(v) for v in metric_values):
            return "partial_core_metrics_missing_in_strict_snapshot"
        return None

    def _derive_row_null_reason_from_statuses(self, status_by_metric: Mapping[str, str]) -> Optional[str]:
        statuses = [status_by_metric.get(metric, "missing") for metric in self.config.metric_names()]
        if all(status == "missing" for status in statuses):
            return "all_core_metrics_missing_after_same_filing_mapping_and_backfill"
        if any(status == "missing" for status in statuses):
            return "partial_core_metrics_missing_after_same_filing_mapping_and_backfill"
        return None

    def _count_all_metrics_missing(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        mask = pd.Series(True, index=df.index)
        for metric in self.config.metric_names():
            mask &= df[metric].isna()
        return int(mask.sum())

    # ---------- Coverage / audit ----------

    def _build_coverage_report(
        self,
        entity_filled_df: pd.DataFrame,
        security_filled_df: pd.DataFrame,
        as_of_ts: pd.Timestamp,
    ) -> pd.DataFrame:
        reports: List[Dict[str, Any]] = []

        def add_rows(df: pd.DataFrame, grain: str) -> None:
            if df.empty:
                return
            overall_total = len(df)
            for metric in self.config.metric_names():
                non_null = int(df[metric].notna().sum()) if metric in df.columns else 0
                reports.append(
                    {
                        "as_of_date": as_of_ts.date().isoformat(),
                        "as_of_timestamp": as_of_ts.isoformat(),
                        "grain": grain,
                        "dimension_name": "overall",
                        "dimension_value": "ALL",
                        "metric": metric,
                        "total_rows": overall_total,
                        "non_null_rows": non_null,
                        "missing_rows": overall_total - non_null,
                        "coverage_pct": float(non_null / overall_total) if overall_total else None,
                    }
                )

            for dim_col in ["primary_taxonomy", self.config.industry_aggregate_col]:
                if dim_col not in df.columns:
                    continue
                grouped = df.groupby(df[dim_col].fillna("__NULL__"), dropna=False)
                for dim_value, group in grouped:
                    total = len(group)
                    for metric in self.config.metric_names():
                        non_null = int(group[metric].notna().sum()) if metric in group.columns else 0
                        reports.append(
                            {
                                "as_of_date": as_of_ts.date().isoformat(),
                                "as_of_timestamp": as_of_ts.isoformat(),
                                "grain": grain,
                                "dimension_name": dim_col,
                                "dimension_value": dim_value,
                                "metric": metric,
                                "total_rows": total,
                                "non_null_rows": non_null,
                                "missing_rows": total - non_null,
                                "coverage_pct": float(non_null / total) if total else None,
                            }
                        )

        add_rows(entity_filled_df, "entity")
        add_rows(security_filled_df, "security")
        return pd.DataFrame(reports)

    def _build_audit_report(
        self,
        as_of_ts: pd.Timestamp,
        source_df: pd.DataFrame,
        prepared_df: pd.DataFrame,
        entity_filled_df: pd.DataFrame,
        security_filled_df: pd.DataFrame,
    ) -> pd.DataFrame:
        future_leak_entity = int(
            entity_filled_df["effective_acceptance_datetime"].dropna().gt(as_of_ts).sum()
        ) if "effective_acceptance_datetime" in entity_filled_df.columns else 0
        future_leak_security = int(
            security_filled_df["effective_acceptance_datetime"].dropna().gt(as_of_ts).sum()
        ) if "effective_acceptance_datetime" in security_filled_df.columns else 0

        max_prepared_acceptance = (
            prepared_df["effective_acceptance_datetime"].dropna().max()
            if not prepared_df.empty and "effective_acceptance_datetime" in prepared_df.columns
            else pd.NaT
        )
        max_prepared_filing = (
            prepared_df[self.config.filing_date_col].dropna().max()
            if not prepared_df.empty and self.config.filing_date_col in prepared_df.columns
            else pd.NaT
        )

        row = {
            "as_of_date": as_of_ts.date().isoformat(),
            "as_of_timestamp": as_of_ts.isoformat(),
            "source_rows": int(len(source_df)),
            "prepared_rows": int(len(prepared_df)),
            "future_leak_count_entity": future_leak_entity,
            "future_leak_count_security": future_leak_security,
            "max_prepared_acceptance_datetime": self._json_default(max_prepared_acceptance),
            "max_prepared_filing_date": self._json_default(max_prepared_filing),
            "entity_all5_missing_count": int(self._count_all_metrics_missing(entity_filled_df)),
            "security_all5_missing_count": int(self._count_all_metrics_missing(security_filled_df)),
        }
        return pd.DataFrame([row])

    # ---------- Persistence ----------

    def _persist(
        self,
        entity_strict_df: pd.DataFrame,
        entity_filled_df: pd.DataFrame,
        security_strict_df: pd.DataFrame,
        security_filled_df: pd.DataFrame,
        coverage_report_df: pd.DataFrame,
        audit_report_df: pd.DataFrame,
        stats: Dict[str, Any],
    ) -> None:
        if self.engine is None:
            raise ValueError("_persist requires a DB engine.")

        self._ensure_tables()
        as_of_date = stats["as_of_date"]

        with self.engine.begin() as conn:
            if self.config.delete_existing_as_of:
                for table_name in [
                    self.config.strict_table,
                    self.config.filled_table,
                    self.config.security_strict_table,
                    self.config.security_filled_table,
                    self.config.run_table,
                ]:
                    conn.execute(text(f"DELETE FROM {table_name} WHERE as_of_date = :as_of_date"), {"as_of_date": as_of_date})

        for df in [entity_strict_df, entity_filled_df, security_strict_df, security_filled_df]:
            for col in [
                "as_of_timestamp",
                "effective_acceptance_datetime",
                self.config.filing_date_col,
                "anchor_period_end",
                "strict_anchor_period_end",
            ]:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], errors="coerce")

        entity_strict_df.to_sql(self.config.strict_table, self.engine, if_exists="append", index=False)
        entity_filled_df.to_sql(self.config.filled_table, self.engine, if_exists="append", index=False)
        security_strict_df.to_sql(self.config.security_strict_table, self.engine, if_exists="append", index=False)
        security_filled_df.to_sql(self.config.security_filled_table, self.engine, if_exists="append", index=False)

        run_df = pd.DataFrame([
            {
                "as_of_date": stats["as_of_date"],
                "as_of_timestamp": pd.to_datetime(stats["as_of_timestamp"]),
                "source_rows": stats["source_rows"],
                "prepared_rows": stats["prepared_rows"],
                "accession_bundles": stats["accession_bundles"],
                "entity_strict_rows": stats["entity_strict_rows"],
                "entity_filled_rows": stats["entity_filled_rows"],
                "security_strict_rows": stats["security_strict_rows"],
                "security_filled_rows": stats["security_filled_rows"],
                "entity_filled_non_null_by_metric_json": json.dumps(stats["entity_filled_non_null_by_metric"], sort_keys=True),
                "security_filled_non_null_by_metric_json": json.dumps(stats["security_filled_non_null_by_metric"], sort_keys=True),
                "coverage_report_json": coverage_report_df.to_json(orient="records", date_format="iso"),
                "audit_report_json": audit_report_df.to_json(orient="records", date_format="iso"),
                "config_json": json.dumps(dataclasses.asdict(self.config), default=self._json_default, sort_keys=True),
                "created_at": pd.Timestamp.utcnow(),
            }
        ])
        run_df.to_sql(self.config.run_table, self.engine, if_exists="append", index=False)

    def _ensure_tables(self) -> None:
        if self.engine is None:
            raise ValueError("_ensure_tables requires a DB engine.")
        metric_cols_sql = ",\n                        ".join([f"{metric} NUMERIC" for metric in self.config.metric_names()])
        profile_cols_sql = f"""
                    {self.config.sector_col} TEXT NULL,
                    {self.config.industry_col} TEXT NULL,
                    {self.config.industry_aggregate_col} TEXT NULL,
                    primary_taxonomy TEXT NULL,
                    taxonomy_profile TEXT NULL,
        """
        common_entity_cols = f"""
                    as_of_date DATE NOT NULL,
                    as_of_timestamp TIMESTAMP NULL,
                    {self.config.cik_col} TEXT NOT NULL,
                    {self.config.ticker_col} TEXT NULL,
                    {self.config.accession_col} TEXT NULL,
                    {self.config.form_col} TEXT NULL,
                    {self.config.filing_date_col} TIMESTAMP NULL,
                    effective_acceptance_datetime TIMESTAMP NULL,
                    acceptance_source TEXT NULL,
                    anchor_period_end TIMESTAMP NULL,
                    canonical_form TEXT NULL,
                    form_family TEXT NULL,
                    is_periodic_form BOOLEAN NULL,
                    bundle_non_null_count INTEGER NULL,
                    bundle_completeness_score INTEGER NULL,
                    bundle_selection_reason TEXT NULL,
                    strict_selection_reason TEXT NULL,
                    {profile_cols_sql}
                    {metric_cols_sql},
                    same_filing_metric_provenance_json TEXT NULL,
                    same_filing_metric_status_json TEXT NULL,
                    snapshot_kind TEXT NULL,
                    null_reason TEXT NULL
        """
        common_security_extra = """
                    entity_ticker TEXT NULL,
                    canonical_ticker TEXT NULL,
                    ticker_role TEXT NULL,
                    xref_source TEXT NULL,
        """
        with self.engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {self.config.strict_table} (
                    {common_entity_cols}
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {self.config.filled_table} (
                    {common_entity_cols},
                    strict_accession_number TEXT NULL,
                    strict_anchor_period_end TIMESTAMP NULL,
                    metric_provenance_json TEXT NULL,
                    metric_status_json TEXT NULL
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {self.config.security_strict_table} (
                    as_of_date DATE NOT NULL,
                    as_of_timestamp TIMESTAMP NULL,
                    {self.config.cik_col} TEXT NOT NULL,
                    {self.config.ticker_col} TEXT NULL,
                    {common_security_extra}
                    {self.config.accession_col} TEXT NULL,
                    {self.config.form_col} TEXT NULL,
                    {self.config.filing_date_col} TIMESTAMP NULL,
                    effective_acceptance_datetime TIMESTAMP NULL,
                    acceptance_source TEXT NULL,
                    anchor_period_end TIMESTAMP NULL,
                    canonical_form TEXT NULL,
                    form_family TEXT NULL,
                    is_periodic_form BOOLEAN NULL,
                    bundle_non_null_count INTEGER NULL,
                    bundle_completeness_score INTEGER NULL,
                    bundle_selection_reason TEXT NULL,
                    strict_selection_reason TEXT NULL,
                    {profile_cols_sql}
                    {metric_cols_sql},
                    same_filing_metric_provenance_json TEXT NULL,
                    same_filing_metric_status_json TEXT NULL,
                    snapshot_kind TEXT NULL,
                    null_reason TEXT NULL
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {self.config.security_filled_table} (
                    as_of_date DATE NOT NULL,
                    as_of_timestamp TIMESTAMP NULL,
                    {self.config.cik_col} TEXT NOT NULL,
                    {self.config.ticker_col} TEXT NULL,
                    {common_security_extra}
                    {self.config.accession_col} TEXT NULL,
                    {self.config.form_col} TEXT NULL,
                    {self.config.filing_date_col} TIMESTAMP NULL,
                    effective_acceptance_datetime TIMESTAMP NULL,
                    acceptance_source TEXT NULL,
                    anchor_period_end TIMESTAMP NULL,
                    canonical_form TEXT NULL,
                    form_family TEXT NULL,
                    is_periodic_form BOOLEAN NULL,
                    bundle_non_null_count INTEGER NULL,
                    bundle_completeness_score INTEGER NULL,
                    bundle_selection_reason TEXT NULL,
                    strict_selection_reason TEXT NULL,
                    {profile_cols_sql}
                    {metric_cols_sql},
                    same_filing_metric_provenance_json TEXT NULL,
                    same_filing_metric_status_json TEXT NULL,
                    snapshot_kind TEXT NULL,
                    null_reason TEXT NULL,
                    strict_accession_number TEXT NULL,
                    strict_anchor_period_end TIMESTAMP NULL,
                    metric_provenance_json TEXT NULL,
                    metric_status_json TEXT NULL
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {self.config.run_table} (
                    as_of_date DATE NOT NULL,
                    as_of_timestamp TIMESTAMP NULL,
                    source_rows INTEGER NULL,
                    prepared_rows INTEGER NULL,
                    accession_bundles INTEGER NULL,
                    entity_strict_rows INTEGER NULL,
                    entity_filled_rows INTEGER NULL,
                    security_strict_rows INTEGER NULL,
                    security_filled_rows INTEGER NULL,
                    entity_filled_non_null_by_metric_json TEXT NULL,
                    security_filled_non_null_by_metric_json TEXT NULL,
                    coverage_report_json TEXT NULL,
                    audit_report_json TEXT NULL,
                    config_json TEXT NULL,
                    created_at TIMESTAMP NULL
                )
            """))

    # ---------- Output columns ----------

    def _entity_strict_output_columns(self) -> List[str]:
        return [
            "as_of_date",
            "as_of_timestamp",
            self.config.cik_col,
            self.config.ticker_col,
            self.config.accession_col,
            self.config.form_col,
            self.config.filing_date_col,
            "effective_acceptance_datetime",
            "acceptance_source",
            "anchor_period_end",
            "canonical_form",
            "form_family",
            "is_periodic_form",
            "bundle_non_null_count",
            "bundle_completeness_score",
            "bundle_selection_reason",
            "strict_selection_reason",
            self.config.sector_col,
            self.config.industry_col,
            self.config.industry_aggregate_col,
            "primary_taxonomy",
            "taxonomy_profile",
            *self.config.metric_names(),
            "same_filing_metric_provenance_json",
            "same_filing_metric_status_json",
            "snapshot_kind",
            "null_reason",
        ]

    def _entity_filled_output_columns(self) -> List[str]:
        return [
            *self._entity_strict_output_columns(),
            "strict_accession_number",
            "strict_anchor_period_end",
            "metric_provenance_json",
            "metric_status_json",
        ]

    def _security_strict_output_columns(self) -> List[str]:
        return [
            "as_of_date",
            "as_of_timestamp",
            self.config.cik_col,
            self.config.ticker_col,
            "entity_ticker",
            "canonical_ticker",
            "ticker_role",
            "xref_source",
            self.config.accession_col,
            self.config.form_col,
            self.config.filing_date_col,
            "effective_acceptance_datetime",
            "acceptance_source",
            "anchor_period_end",
            "canonical_form",
            "form_family",
            "is_periodic_form",
            "bundle_non_null_count",
            "bundle_completeness_score",
            "bundle_selection_reason",
            "strict_selection_reason",
            self.config.sector_col,
            self.config.industry_col,
            self.config.industry_aggregate_col,
            "primary_taxonomy",
            "taxonomy_profile",
            *self.config.metric_names(),
            "same_filing_metric_provenance_json",
            "same_filing_metric_status_json",
            "snapshot_kind",
            "null_reason",
        ]

    def _security_filled_output_columns(self) -> List[str]:
        return [
            *self._security_strict_output_columns(),
            "strict_accession_number",
            "strict_anchor_period_end",
            "metric_provenance_json",
            "metric_status_json",
        ]

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                return str(value)
        return value


# -----------------------------
# Convenience readers
# -----------------------------

def fetch_snapshot_for_universe(
    engine: Engine,
    as_of_date: str,
    table_name: str = "sec_fundamental_snapshot_filled_security_t1",
    ticker_col: str = "ticker",
) -> pd.DataFrame:
    if engine is None:
        raise ValueError("engine is required")
    sql = text(f"SELECT * FROM {table_name} WHERE as_of_date = :as_of_date ORDER BY {ticker_col}")
    with engine.begin() as conn:
        return pd.read_sql(sql, conn, params={"as_of_date": as_of_date})


# -----------------------------
# CLI
# -----------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build enhanced tier-1 SEC point-in-time fundamental snapshots.")
    parser.add_argument("--db-url", required=True, help="SQLAlchemy DB URL.")
    parser.add_argument("--as-of", default=None, help="As-of timestamp, e.g. 2026-03-15T23:59:59Z")
    parser.add_argument("--as-of-date", default=None, help="As-of date for helper timestamp generation, e.g. 2026-03-15")
    parser.add_argument("--cutoff-time", default="16:15:00", help="Used with --as-of-date")
    parser.add_argument("--cutoff-timezone", default="America/New_York", help="Used with --as-of-date")
    parser.add_argument("--universe-as-of", default=None, help="Universe as_of_date (YYYY-MM-DD).")
    parser.add_argument("--source-table", default="sec_fundamental_period_t1")
    parser.add_argument("--metric-source-table", default=None)
    parser.add_argument("--universe-table", default=None)
    parser.add_argument("--issuer-profile-table", default=None)
    parser.add_argument("--alias-mapping-table", default=None)
    parser.add_argument("--strict-table", default="sec_fundamental_snapshot_strict_t1")
    parser.add_argument("--filled-table", default="sec_fundamental_snapshot_filled_t1")
    parser.add_argument("--security-strict-table", default="sec_fundamental_snapshot_strict_security_t1")
    parser.add_argument("--security-filled-table", default="sec_fundamental_snapshot_filled_security_t1")
    parser.add_argument("--run-table", default="sec_fundamental_snapshot_run_t1")
    parser.add_argument("--strict-min-non-null", type=int, default=2)
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--publication-lag-minutes", type=int, default=0)
    parser.add_argument("--enforce-quality-gates", action="store_true")
    parser.add_argument("--max-all5-missing-entity", type=int, default=0)
    parser.add_argument("--allow-supplemental-anchor", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--alias-mapping-csv", default=None)
    parser.add_argument("--issuer-profile-csv", default=None)
    parser.add_argument("--metric-mapping-csv", default=None)
    parser.add_argument("--no-security-output", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not HAVE_SQLALCHEMY:
        raise RuntimeError("SQLAlchemy is not installed. Install it for DB mode, or call run_from_dataframes() from Python.")

    if args.as_of:
        as_of_timestamp = args.as_of
    elif args.as_of_date:
        as_of_timestamp = make_as_of_timestamp(
            as_of_date=args.as_of_date,
            cutoff_time=args.cutoff_time,
            timezone=args.cutoff_timezone,
        )
    else:
        raise ValueError("Provide --as-of or --as-of-date")

    engine = create_engine(args.db_url, future=True)
    config = SnapshotConfig(
        source_table=args.source_table,
        metric_source_table=args.metric_source_table,
        universe_table=args.universe_table or None,
        issuer_profile_table=args.issuer_profile_table or None,
        alias_mapping_table=args.alias_mapping_table or None,
        strict_table=args.strict_table,
        filled_table=args.filled_table,
        security_strict_table=args.security_strict_table,
        security_filled_table=args.security_filled_table,
        run_table=args.run_table,
        strict_min_non_null_metrics=args.strict_min_non_null,
        lookback_days=args.lookback_days,
        allow_supplemental_as_anchor_when_no_periodic=args.allow_supplemental_anchor,
        publication_lag_minutes=args.publication_lag_minutes,
        enforce_quality_gates=bool(args.enforce_quality_gates),
        max_all5_missing_entity=int(args.max_all5_missing_entity),
        alias_mapping_path=args.alias_mapping_csv,
        issuer_profile_path=args.issuer_profile_csv,
        metric_mapping_path=args.metric_mapping_csv,
        output_security_snapshots=not args.no_security_output,
    )
    builder = Tier1FundamentalSnapshotBuilder(engine=engine, config=config)
    result = builder.run(
        as_of_timestamp=as_of_timestamp,
        universe_as_of_date=args.universe_as_of,
        persist=not args.no_persist,
    )
    LOGGER.info("Run stats: %s", json.dumps(result.stats, sort_keys=True))
    LOGGER.info("Entity filled head:\n%s", result.entity_filled_df.head(10).to_string(index=False))
    LOGGER.info("Security filled head:\n%s", result.security_filled_df.head(10).to_string(index=False))
    LOGGER.info("Audit:\n%s", result.audit_report_df.to_string(index=False))


if __name__ == "__main__":
    main()
