from __future__ import annotations

"""
Compile a full metric mapping CSV for sec_tier1_snapshot_enhanced.py from a
config-driven YAML overlay.

Why this exists:
- sec_tier1_snapshot_enhanced.py replaces the built-in mapping registry when
  --metric-mapping-csv is supplied. This script therefore emits a *full* merged
  registry: default base mapping + overlay rules.
- It keeps mapping growth out of the snapshot engine and makes new coverage
  additions auditable and easy to extend.
- It supports both broad industry/taxonomy rules and issuer-specific extension
  rules. Issuer-specific rules are safe because the compiled rows use exact
  taxonomy + concept pairs observed in the fact inventory, which typically makes
  extension mappings effectively issuer-scoped.

Main outputs:
1. compiled full mapping CSV (pass to --metric-mapping-csv)
2. overlay-only CSV (optional)
3. unmatched rule report (optional)

The overlay YAML supports:
- generic_rules: broad rules by metric/taxonomy/industry
- issuer_overrides: rules for a specific issuer (CIK or ticker label)
- exact rules: can compile without a fact inventory
- regex / startswith / contains rules: compile to exact concept rows when a
  fact inventory is supplied from CSV or DB

Suggested workflow:
1. Maintain sec_metric_mapping_overlay.yaml in source control.
2. Rebuild the mapping CSV whenever new gaps appear.
3. Feed the compiled CSV into sec_tier1_snapshot_enhanced.py.
4. Rebuild latest + historical as-of snapshots.
"""

import argparse
import importlib.util
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote

import pandas as pd
import yaml
from sec_fundamentals_config import normalize_cik_10d

try:
    from sqlalchemy import create_engine, text
    HAVE_SQLALCHEMY = True
except Exception:  # pragma: no cover
    create_engine = None
    HAVE_SQLALCHEMY = False

    def text(sql: str) -> str:
        return sql


LOGGER = logging.getLogger("sec_metric_mapping_overlay_builder")
SQLITE_BUSY_TIMEOUT_MS = 30000


# -----------------------------
# import default mapping safely
# -----------------------------

def _load_default_mapping_function():
    try:
        from sec_tier1_snapshot_enhanced import default_metric_mapping_df  # type: ignore
        return default_metric_mapping_df
    except Exception:
        here = Path(__file__).resolve().parent
        target = here / "sec_tier1_snapshot_enhanced.py"
        if not target.exists():
            raise
        spec = importlib.util.spec_from_file_location("sec_tier1_snapshot_enhanced", target)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not import {target}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[assignment]
        return module.default_metric_mapping_df


default_metric_mapping_df = _load_default_mapping_function()


# -----------------------------
# rule models
# -----------------------------

ALLOWED_SOURCE_KINDS = {"direct", "formula_component"}
ALLOWED_MATCH_MODES = {"exact", "regex", "startswith", "contains"}
ALLOWED_METRICS = {"revenue", "net_income", "operating_cash_flow", "total_assets", "total_equity"}


@dataclass(frozen=True)
class CompiledRow:
    metric_name: str
    source_kind: str
    taxonomy: str
    concept_name: str
    priority: int
    industry_aggregate: Optional[str]
    component_group: Optional[str]
    wide_column_name: Optional[str]
    period_type: Optional[str]
    rule_id: str
    rule_scope: str
    mapping_note: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "source_kind": self.source_kind,
            "taxonomy": self.taxonomy,
            "concept_name": self.concept_name,
            "priority": self.priority,
            "industry_aggregate": self.industry_aggregate,
            "component_group": self.component_group,
            "wide_column_name": self.wide_column_name,
            "period_type": self.period_type,
            "rule_id": self.rule_id,
            "rule_scope": self.rule_scope,
            "mapping_note": self.mapping_note,
        }


# -----------------------------
# helpers
# -----------------------------

def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text_value = str(value).strip()
    return text_value or None


def _clean_lower_text(value: Any) -> Optional[str]:
    text_value = _clean_text(value)
    return text_value.lower() if text_value else None


def _clean_taxonomy(value: Any) -> Optional[str]:
    out = _clean_text(value)
    return out.lower() if out else None


def _normalize_cik(value: Any) -> Optional[str]:
    return normalize_cik_10d(value)


def _normalize_opt_col_name(value: Any) -> Optional[str]:
    text_value = _clean_text(value)
    return text_value if text_value else None


def _rule_industry_values(rule: Mapping[str, Any]) -> List[Optional[str]]:
    values: List[Optional[str]] = []
    primary = _clean_text(rule.get("industry_aggregate"))
    if primary:
        values.append(primary)
    for raw in _as_list(rule.get("industry_aggregate_any_of")):
        candidate = _clean_text(raw)
        if candidate and candidate not in values:
            values.append(candidate)
    if not values:
        return [None]
    return values


def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Overlay YAML must parse to a mapping/object at the top level.")
    return data


def _validate_rule(rule: Mapping[str, Any], scope: str) -> None:
    metric_name = _clean_text(rule.get("metric_name"))
    if metric_name not in ALLOWED_METRICS:
        raise ValueError(f"Invalid metric_name in {scope}: {metric_name!r}")

    source_kind = _clean_text(rule.get("source_kind"))
    if source_kind not in ALLOWED_SOURCE_KINDS:
        raise ValueError(f"Invalid source_kind in {scope}: {source_kind!r}")

    match_obj = rule.get("match") or {}
    if not isinstance(match_obj, dict):
        raise ValueError(f"Rule match block must be an object for {rule.get('rule_id')}")
    mode = _clean_text(match_obj.get("mode"))
    if mode not in ALLOWED_MATCH_MODES:
        raise ValueError(f"Invalid match.mode in {scope}: {mode!r}")
    concepts = [_clean_text(x) for x in _as_list(match_obj.get("concepts"))]
    concepts = [x for x in concepts if x]
    if not concepts:
        raise ValueError(f"Rule concepts are required for {scope}: {rule.get('rule_id')}")

    if source_kind == "formula_component" and not _clean_text(rule.get("component_group")):
        raise ValueError(f"formula_component rule requires component_group: {rule.get('rule_id')}")

    ind_list = rule.get("industry_aggregate_any_of")
    if ind_list is not None and not isinstance(ind_list, list):
        raise ValueError(
            f"industry_aggregate_any_of must be a list for {scope}: {rule.get('rule_id')}"
        )


# -----------------------------
# fact inventory loading
# -----------------------------

def load_fact_inventory_from_csv(
    csv_path: str,
    cik_col: str = "cik",
    taxonomy_col: str = "taxonomy",
    concept_col: str = "concept_name",
    period_type_col: str = "period_type",
) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    return _normalize_fact_inventory(df, cik_col, taxonomy_col, concept_col, period_type_col)


def load_fact_inventory_from_db(
    db_url: str,
    table_name: str,
    cik_col: str = "cik",
    taxonomy_col: str = "taxonomy",
    concept_col: str = "tag",
    period_type_col: str = "",
    value_col: str = "value_num",
    issuer_ciks: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    db_url_text = str(db_url).strip()
    if _is_sqlite_db_url(db_url_text):
        return _load_fact_inventory_from_sqlite(
            db_url=db_url_text,
            table_name=table_name,
            cik_col=cik_col,
            taxonomy_col=taxonomy_col,
            concept_col=concept_col,
            period_type_col=period_type_col,
            value_col=value_col,
            issuer_ciks=issuer_ciks,
        )

    if not HAVE_SQLALCHEMY:
        raise RuntimeError(
            "SQLAlchemy is not installed; DB mode is unavailable for non-SQLite DB URLs."
        )

    concept_col_name = _normalize_opt_col_name(concept_col)
    period_type_col_name = _normalize_opt_col_name(period_type_col)
    value_col_name = _normalize_opt_col_name(value_col)

    if not concept_col_name:
        raise ValueError("metric concept column cannot be empty in non-SQLite DB mode.")

    engine = create_engine(db_url_text, future=True)
    filters = [f"{concept_col_name} IS NOT NULL", f"{taxonomy_col} IS NOT NULL"]
    params: Dict[str, Any] = {}
    if value_col_name:
        filters.append(f"{value_col_name} IS NOT NULL")
    if issuer_ciks:
        bind_names: List[str] = []
        for idx, cik in enumerate(sorted(set(str(c) for c in issuer_ciks if c))):
            key = f"cik_{idx}"
            params[key] = cik
            bind_names.append(f":{key}")
        if bind_names:
            filters.append(f"CAST({cik_col} AS TEXT) IN ({', '.join(bind_names)})")

    period_select = (
        f"{period_type_col_name} AS {period_type_col_name}"
        if period_type_col_name
        else "NULL AS period_type"
    )
    period_norm_col = period_type_col_name or "period_type"

    sql = text(
        f"""
        SELECT DISTINCT
            CAST({cik_col} AS TEXT) AS {cik_col},
            {taxonomy_col} AS {taxonomy_col},
            {concept_col_name} AS {concept_col_name},
            {period_select}
        FROM {table_name}
        WHERE {' AND '.join(filters)}
        """
    )
    with engine.begin() as conn:
        df = pd.read_sql(sql, conn, params=params)
    return _normalize_fact_inventory(df, cik_col, taxonomy_col, concept_col_name, period_norm_col)


def _is_sqlite_db_url(db_url: str) -> bool:
    value = db_url.strip().lower()
    return value.startswith("sqlite:///") or value.endswith(".sqlite") or value.endswith(".db")


def _sqlite_path_from_db_url(db_url: str) -> str:
    raw = db_url.strip()
    if raw.lower().startswith("sqlite:///"):
        path = unquote(raw[len("sqlite:///") :])
    elif raw.lower().startswith("sqlite://"):
        path = unquote(raw[len("sqlite://") :])
    else:
        path = raw
    if re.match(r"^/[a-zA-Z]:/", path):
        path = path[1:]
    return path


def _sqlite_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(r[1]).lower() for r in rows}


def _resolve_db_column(
    available_cols: set[str],
    requested: Optional[str],
    candidates: Sequence[str],
    *,
    required: bool,
    label: str,
) -> Optional[str]:
    requested_clean = _normalize_opt_col_name(requested)
    if requested_clean and requested_clean.lower() in available_cols:
        return requested_clean
    if requested_clean and requested_clean.lower() not in available_cols and required:
        raise ValueError(f"Requested {label} column not found: {requested_clean}")
    for candidate in candidates:
        if candidate.lower() in available_cols:
            return candidate
    if required:
        raise ValueError(
            f"Could not resolve required {label} column. "
            f"Tried requested={requested_clean!r}, candidates={list(candidates)}"
        )
    return None


def _load_fact_inventory_from_sqlite(
    db_url: str,
    table_name: str,
    cik_col: str,
    taxonomy_col: str,
    concept_col: str,
    period_type_col: str,
    value_col: str,
    issuer_ciks: Optional[Sequence[str]],
) -> pd.DataFrame:
    db_path = _sqlite_path_from_db_url(db_url)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        available = _sqlite_table_columns(conn, table_name)
        if not available:
            raise ValueError(f"Table not found or has no columns: {table_name}")

        cik_col_resolved = _resolve_db_column(
            available,
            cik_col,
            ["cik"],
            required=True,
            label="cik",
        )
        taxonomy_col_resolved = _resolve_db_column(
            available,
            taxonomy_col,
            ["taxonomy"],
            required=True,
            label="taxonomy",
        )
        concept_col_resolved = _resolve_db_column(
            available,
            concept_col,
            ["tag", "concept_name", "concept"],
            required=True,
            label="concept",
        )
        period_col_resolved = _resolve_db_column(
            available,
            period_type_col,
            ["period_type"],
            required=False,
            label="period_type",
        )
        value_col_resolved = _resolve_db_column(
            available,
            value_col,
            ["value_num", "fact_value"],
            required=False,
            label="value",
        )

        where_parts = [
            f"{concept_col_resolved} IS NOT NULL",
            f"{taxonomy_col_resolved} IS NOT NULL",
        ]
        params: List[Any] = []
        if value_col_resolved:
            where_parts.append(f"{value_col_resolved} IS NOT NULL")

        if issuer_ciks:
            issuer_norm = sorted(set(str(c) for c in issuer_ciks if c))
            if issuer_norm:
                placeholders = ",".join(["?"] * len(issuer_norm))
                where_parts.append(f"CAST({cik_col_resolved} AS TEXT) IN ({placeholders})")
                params.extend(issuer_norm)

        period_sql = (
            f"{period_col_resolved} AS period_type"
            if period_col_resolved
            else "NULL AS period_type"
        )
        sql = f"""
            SELECT DISTINCT
                CAST({cik_col_resolved} AS TEXT) AS cik,
                {taxonomy_col_resolved} AS taxonomy,
                {concept_col_resolved} AS concept_name,
                {period_sql}
            FROM {table_name}
            WHERE {' AND '.join(where_parts)}
        """
        df = pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()

    return _normalize_fact_inventory(
        df,
        cik_col="cik",
        taxonomy_col="taxonomy",
        concept_col="concept_name",
        period_type_col="period_type",
    )


def _normalize_fact_inventory(
    df: pd.DataFrame,
    cik_col: str,
    taxonomy_col: str,
    concept_col: str,
    period_type_col: str,
) -> pd.DataFrame:
    required = [cik_col, taxonomy_col, concept_col]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Fact inventory is missing required columns: {missing}")

    out = df.copy()
    out["cik"] = out[cik_col].map(_normalize_cik)
    out["taxonomy"] = out[taxonomy_col].map(_clean_taxonomy)
    out["concept_name"] = out[concept_col].map(_clean_text)
    if period_type_col in out.columns:
        out["period_type"] = out[period_type_col].map(_clean_lower_text)
    else:
        out["period_type"] = None
    out = out[out["taxonomy"].notna() & out["concept_name"].notna()].copy()
    out = out[["cik", "taxonomy", "concept_name", "period_type"]].drop_duplicates().reset_index(drop=True)
    return out


# -----------------------------
# rule compilation
# -----------------------------

def _rule_taxonomies(rule: Mapping[str, Any]) -> List[str]:
    values = [_clean_taxonomy(x) for x in _as_list(rule.get("taxonomies"))]
    values = [x for x in values if x]
    return values or ["*"]


def _rule_concepts(rule: Mapping[str, Any]) -> List[str]:
    match_obj = rule.get("match") or {}
    values = [_clean_text(x) for x in _as_list(match_obj.get("concepts"))]
    return [x for x in values if x]


def _match_concept(mode: str, pattern: str, concept: str) -> bool:
    if mode == "exact":
        return concept == pattern
    if mode == "regex":
        return re.search(pattern, concept) is not None
    if mode == "startswith":
        return concept.startswith(pattern)
    if mode == "contains":
        return pattern in concept
    raise ValueError(f"Unsupported match mode: {mode}")


def _resolve_candidates_from_inventory(
    rule: Mapping[str, Any],
    fact_inventory_df: pd.DataFrame,
    scope: str,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Returns (matched_taxonomy_concept_pairs, reasons_if_unmatched).
    """
    match_obj = rule.get("match") or {}
    mode = str(match_obj.get("mode")).strip().lower()
    patterns = _rule_concepts(rule)
    taxonomies = _rule_taxonomies(rule)
    issuer_cik = _normalize_cik(rule.get("cik"))

    if fact_inventory_df is None or fact_inventory_df.empty:
        return [], ["fact_inventory_required_for_non_exact_or_issuer_rule"]

    work = fact_inventory_df.copy()
    if issuer_cik:
        work = work[work["cik"] == issuer_cik].copy()
    if taxonomies != ["*"]:
        work = work[work["taxonomy"].isin(taxonomies)].copy()

    period_type = _clean_text(rule.get("period_type"))
    if period_type:
        work = work[(work["period_type"].isna()) | (work["period_type"] == period_type.lower())].copy()

    if work.empty:
        return [], ["no_fact_inventory_rows_after_scope_filters"]

    matches: List[Tuple[str, str]] = []
    for _, fact in work.iterrows():
        concept_name = str(fact["concept_name"])
        taxonomy = str(fact["taxonomy"])
        if any(_match_concept(mode, pattern, concept_name) for pattern in patterns):
            matches.append((taxonomy, concept_name))

    if not matches:
        return [], ["no_fact_inventory_concepts_matched_patterns"]

    return sorted(set(matches)), []


def compile_overlay_rules(
    overlay_config: Mapping[str, Any],
    fact_inventory_df: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Returns (overlay_rows_df, unmatched_rules_df, rule_diagnostics_df).
    """
    compiled_rows: List[Dict[str, Any]] = []
    unmatched_rows: List[Dict[str, Any]] = []
    diagnostics_rows: List[Dict[str, Any]] = []

    for scope_key in ["generic_rules", "issuer_overrides"]:
        scope_rules = overlay_config.get(scope_key) or []
        if not isinstance(scope_rules, list):
            raise ValueError(f"{scope_key} must be a list")

        for idx, raw_rule in enumerate(scope_rules):
            if not isinstance(raw_rule, dict):
                raise ValueError(f"{scope_key}[{idx}] must be an object")
            rule = dict(raw_rule)
            rule_id = _clean_text(rule.get("rule_id")) or f"{scope_key}_{idx+1}"
            rule["rule_id"] = rule_id
            enabled = bool(rule.get("enabled", True))
            if not enabled:
                continue

            _validate_rule(rule, scope_key)

            metric_name = str(rule["metric_name"]).strip()
            source_kind = str(rule["source_kind"]).strip()
            industry_values = _rule_industry_values(rule)
            component_group = _clean_text(rule.get("component_group"))
            wide_column_name = _clean_text(rule.get("wide_column_name"))
            period_type = _clean_text(rule.get("period_type"))
            mapping_note = _clean_text(rule.get("note"))
            priority_start = int(rule.get("priority_start", 500))
            match_obj = rule.get("match") or {}
            mode = str(match_obj.get("mode")).strip().lower()
            concepts = _rule_concepts(rule)
            taxonomies = _rule_taxonomies(rule)
            needs_inventory = (mode != "exact") or (scope_key == "issuer_overrides") or (taxonomies == ["*"])

            resolved_pairs: List[Tuple[str, str]] = []
            reasons: List[str] = []

            if not needs_inventory and mode == "exact":
                for taxonomy in taxonomies:
                    for concept in concepts:
                        resolved_pairs.append((taxonomy, concept))
            else:
                resolved_pairs, reasons = _resolve_candidates_from_inventory(rule, fact_inventory_df, scope_key)

            if not resolved_pairs:
                unmatched_rows.append(
                    {
                        "rule_id": rule_id,
                        "rule_scope": scope_key,
                        "metric_name": metric_name,
                        "source_kind": source_kind,
                        "industry_aggregate": _clean_text(rule.get("industry_aggregate")),
                        "industry_aggregate_any_of": json.dumps(
                            [x for x in industry_values if x], sort_keys=True
                        ),
                        "match_mode": mode,
                        "concept_patterns": json.dumps(concepts),
                        "taxonomies": json.dumps(taxonomies),
                        "reason": ";".join(reasons) if reasons else "no_resolved_pairs",
                    }
                )
                diagnostics_rows.append(
                    {
                        "rule_id": rule_id,
                        "rule_scope": scope_key,
                        "metric_name": metric_name,
                        "source_kind": source_kind,
                        "match_mode": mode,
                        "industry_values": json.dumps([x for x in industry_values if x]),
                        "resolved_taxonomy_concept_pairs": 0,
                        "emitted_rows": 0,
                        "unmatched": 1,
                        "reason": ";".join(reasons) if reasons else "no_resolved_pairs",
                    }
                )
                continue

            resolved_pairs = sorted(set(resolved_pairs))
            emitted_rows = 0
            for industry_aggregate in industry_values:
                for offset, (taxonomy, concept_name) in enumerate(resolved_pairs):
                    row = CompiledRow(
                        metric_name=metric_name,
                        source_kind=source_kind,
                        taxonomy=taxonomy,
                        concept_name=concept_name,
                        priority=priority_start + offset,
                        industry_aggregate=industry_aggregate,
                        component_group=component_group,
                        wide_column_name=wide_column_name,
                        period_type=period_type.lower() if period_type else None,
                        rule_id=rule_id,
                        rule_scope=scope_key,
                        mapping_note=mapping_note,
                    )
                    compiled_rows.append(row.as_dict())
                    emitted_rows += 1
            diagnostics_rows.append(
                {
                    "rule_id": rule_id,
                    "rule_scope": scope_key,
                    "metric_name": metric_name,
                    "source_kind": source_kind,
                    "match_mode": mode,
                    "industry_values": json.dumps([x for x in industry_values if x]),
                    "resolved_taxonomy_concept_pairs": int(len(resolved_pairs)),
                    "emitted_rows": int(emitted_rows),
                    "unmatched": 0,
                    "reason": "",
                }
            )

    overlay_df = pd.DataFrame(compiled_rows)
    if overlay_df.empty:
        overlay_df = pd.DataFrame(
            columns=[
                "metric_name", "source_kind", "taxonomy", "concept_name", "priority",
                "industry_aggregate", "component_group", "wide_column_name", "period_type",
                "rule_id", "rule_scope", "mapping_note",
            ]
        )
    unmatched_df = pd.DataFrame(unmatched_rows)
    diagnostics_df = pd.DataFrame(diagnostics_rows)
    return overlay_df, unmatched_df, diagnostics_df


# -----------------------------
# merge / dedupe
# -----------------------------

def normalize_mapping_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    required = ["metric_name", "source_kind", "taxonomy", "concept_name", "priority"]
    missing = [col for col in required if col not in out.columns]
    if missing:
        raise ValueError(f"Mapping dataframe missing required columns: {missing}")

    for col in ["industry_aggregate", "component_group", "wide_column_name", "period_type", "rule_id", "rule_scope", "mapping_note"]:
        if col not in out.columns:
            out[col] = None
    out["metric_name"] = out["metric_name"].astype(str).str.strip()
    out["source_kind"] = out["source_kind"].astype(str).str.strip()
    out["taxonomy"] = out["taxonomy"].astype(str).str.strip().str.lower()
    out["concept_name"] = out["concept_name"].astype(str).str.strip()
    out["priority"] = pd.to_numeric(out["priority"], errors="coerce").fillna(9999).astype(int)
    out["period_type"] = out["period_type"].map(_clean_lower_text)
    out["industry_aggregate"] = out["industry_aggregate"].map(_clean_text)
    out["component_group"] = out["component_group"].map(_clean_text)
    out["wide_column_name"] = out["wide_column_name"].map(_clean_text)
    return out.reset_index(drop=True)


def merge_base_and_overlay(base_df: pd.DataFrame, overlay_df: pd.DataFrame) -> pd.DataFrame:
    base_df = normalize_mapping_df(base_df)
    overlay_df = normalize_mapping_df(overlay_df)

    base_df = base_df.copy()
    if "rule_id" not in base_df.columns:
        base_df["rule_id"] = "default_base"
    if "rule_scope" not in base_df.columns:
        base_df["rule_scope"] = "default_base"
    if "mapping_note" not in base_df.columns:
        base_df["mapping_note"] = None

    combined = pd.concat([base_df, overlay_df], ignore_index=True, sort=False)

    dedupe_keys = ["metric_name", "source_kind", "taxonomy", "concept_name", "industry_aggregate", "component_group", "wide_column_name", "period_type"]
    combined["_overlay_preferred"] = (combined["rule_scope"] != "default_base").astype(int)
    combined = combined.sort_values(["_overlay_preferred", "priority"], ascending=[False, True])
    combined = combined.drop_duplicates(subset=dedupe_keys, keep="first").drop(columns=["_overlay_preferred"]).reset_index(drop=True)

    combined = combined.sort_values(["metric_name", "industry_aggregate", "source_kind", "priority", "taxonomy", "concept_name"], na_position="last").reset_index(drop=True)
    return combined


# -----------------------------
# CLI / main
# -----------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile merged SEC metric mapping CSV from overlay YAML.")
    parser.add_argument("--overlay-yaml", required=True, help="Path to YAML overlay config.")
    parser.add_argument("--output-csv", required=True, help="Path to the merged full mapping CSV output.")
    parser.add_argument("--overlay-only-csv", default=None, help="Optional path for overlay-only rows.")
    parser.add_argument("--unmatched-rules-csv", default=None, help="Optional path for unmatched rules report.")
    parser.add_argument("--rule-diagnostics-csv", default=None, help="Optional path for per-rule diagnostics report.")
    parser.add_argument("--fact-inventory-csv", default=None, help="Optional CSV with distinct/raw fact concepts.")
    parser.add_argument("--db-url", default=None, help="Optional SQLAlchemy DB URL for fact inventory DB mode.")
    parser.add_argument("--metric-source-table", default=None, help="Fact table for DB mode.")
    parser.add_argument("--metric-cik-col", default="cik")
    parser.add_argument("--metric-taxonomy-col", default="taxonomy")
    parser.add_argument("--metric-concept-col", default="tag")
    parser.add_argument("--metric-period-type-col", default="")
    parser.add_argument("--metric-value-col", default="value_num")
    parser.add_argument("--overlay-only", action="store_true", help="Do not include built-in base mapping in the output CSV.")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    overlay_config = _load_yaml(args.overlay_yaml)

    fact_inventory_df: Optional[pd.DataFrame] = None
    fact_inventory_source = None

    if args.fact_inventory_csv:
        fact_inventory_df = load_fact_inventory_from_csv(
            csv_path=args.fact_inventory_csv,
            cik_col=args.metric_cik_col,
            taxonomy_col=args.metric_taxonomy_col,
            concept_col=args.metric_concept_col,
            period_type_col=args.metric_period_type_col,
        )
        fact_inventory_source = f"csv:{args.fact_inventory_csv}"
    elif args.db_url and args.metric_source_table:
        fact_inventory_df = load_fact_inventory_from_db(
            db_url=args.db_url,
            table_name=args.metric_source_table,
            cik_col=args.metric_cik_col,
            taxonomy_col=args.metric_taxonomy_col,
            concept_col=args.metric_concept_col,
            period_type_col=args.metric_period_type_col,
            value_col=args.metric_value_col,
            issuer_ciks=None,  # keep full inventory for generic regex expansion
        )
        fact_inventory_source = f"db:{args.metric_source_table}"

    overlay_df, unmatched_df, rule_diag_df = compile_overlay_rules(
        overlay_config=overlay_config,
        fact_inventory_df=fact_inventory_df,
    )

    if args.overlay_only:
        final_df = normalize_mapping_df(overlay_df)
    else:
        base_df = default_metric_mapping_df()
        final_df = merge_base_and_overlay(base_df=base_df, overlay_df=overlay_df)

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_df.to_csv(output_path, index=False)

    if args.overlay_only_csv:
        Path(args.overlay_only_csv).parent.mkdir(parents=True, exist_ok=True)
        normalize_mapping_df(overlay_df).to_csv(args.overlay_only_csv, index=False)

    if args.unmatched_rules_csv:
        Path(args.unmatched_rules_csv).parent.mkdir(parents=True, exist_ok=True)
        unmatched_df.to_csv(args.unmatched_rules_csv, index=False)
    if args.rule_diagnostics_csv:
        Path(args.rule_diagnostics_csv).parent.mkdir(parents=True, exist_ok=True)
        rule_diag_df.to_csv(args.rule_diagnostics_csv, index=False)

    summary = {
        "fact_inventory_source": fact_inventory_source,
        "compiled_overlay_rows": int(len(overlay_df)),
        "compiled_final_rows": int(len(final_df)),
        "unmatched_rules": int(len(unmatched_df)),
        "resolved_rules": int((rule_diag_df["unmatched"] == 0).sum()) if not rule_diag_df.empty else 0,
        "rule_diagnostics_rows": int(len(rule_diag_df)),
        "metrics": sorted(final_df["metric_name"].dropna().astype(str).unique().tolist()) if not final_df.empty else [],
    }
    LOGGER.info("Compilation summary: %s", json.dumps(summary, sort_keys=True))
    if not unmatched_df.empty:
        LOGGER.warning("Some overlay rules did not resolve. Inspect %s or rerun with a fact inventory.", args.unmatched_rules_csv or "the returned unmatched_df")


if __name__ == "__main__":
    main()
