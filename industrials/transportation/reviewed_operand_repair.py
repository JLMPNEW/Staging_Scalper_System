from __future__ import annotations

import html
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from bs4 import BeautifulSoup

from dedicated_parser.contracts import file_sha256, stable_hash
from dedicated_parser.schema import ensure_schema as ensure_parser_schema
from dedicated_parser.storage import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_VERSION = "transportation_required_metric_operand_repairs_v1"
SOURCE_ID = "transportation_reviewed_required_metric_operand_v1"
MODEL_FAMILY = "transportation"
ALLOWED_FACT_DERIVATIONS = frozenset(
    {
        "parser_evidence_sum",
        "normalized_fact_sum",
        "document_ix_fact",
        "document_explicit_zero",
        "document_reviewed_formula",
    }
)
ALLOWED_OVERRIDE_STATUSES = frozenset({"NOT_APPLICABLE"})


@dataclass(frozen=True)
class ResolvedFact:
    repair_id: str
    ticker: str
    cik: str
    accession_number: str
    form_type: str
    filing_date: str
    accepted_at: str
    fiscal_year: int | None
    fiscal_period: str
    period_start: str
    period_end: str
    canonical_metric: str
    financial_statement: str
    period_type: str
    unit: str
    value: float
    taxonomy: str
    concept_name: str
    derivation_type: str
    rationale: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class ResolvedOverride:
    override_id: str
    ticker: str
    metric_name: str
    availability_status: str
    status_reason: str
    valid_from: str
    evidence_key: str
    rationale: str
    provenance: dict[str, Any]


def load_policy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _required_text(row: Mapping[str, Any], field: str, *, identity: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise ValueError(f"{identity}: {field} is required")
    return value


def _same_text(values: Iterable[object], *, field: str, identity: str) -> str:
    unique = {str(value or "").strip() for value in values}
    if len(unique) != 1 or not next(iter(unique), ""):
        raise ValueError(f"{identity}: source rows disagree on {field}={sorted(unique)}")
    return next(iter(unique))


def _same_int(values: Iterable[object], *, field: str, identity: str) -> int | None:
    parsed = {int(str(value)) for value in values if value not in (None, "")}
    if len(parsed) > 1:
        raise ValueError(f"{identity}: source rows disagree on {field}={sorted(parsed)}")
    return next(iter(parsed)) if parsed else None


def _assert_close(actual: float, expected: float, *, identity: str) -> None:
    tolerance = max(1e-6, abs(expected) * 1e-10)
    if not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=tolerance):
        raise ValueError(f"{identity}: computed={actual} expected={expected}")


def _normalize_document_text(raw: str) -> str:
    parsed = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(parsed).replace("\xa0", " ")).strip()


def _resolve_document(
    source: Mapping[str, Any],
    *,
    project_root: Path,
    identity: str,
) -> tuple[Path, str]:
    relative = _required_text(source, "path", identity=identity)
    path = (project_root / relative).resolve()
    root = project_root.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{identity}: document escapes project root={path}") from exc
    if not path.is_file():
        raise FileNotFoundError(path)
    expected_sha = _required_text(source, "sha256", identity=identity).lower()
    actual_sha = file_sha256(path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"{identity}: document hash changed actual={actual_sha} expected={expected_sha}"
        )
    return path, actual_sha


def validate_policy_contract(policy: Mapping[str, Any]) -> None:
    if policy.get("policy_version") != POLICY_VERSION:
        raise ValueError("reviewed operand policy_version changed")
    if policy.get("model_family") != MODEL_FAMILY:
        raise ValueError("reviewed operand policy model_family changed")
    if policy.get("source_id") != SOURCE_ID:
        raise ValueError("reviewed operand policy source_id changed")
    if policy.get("review_status") != "ACCEPTED":
        raise ValueError("reviewed operand policy is not ACCEPTED")
    controls = policy.get("controls")
    if not isinstance(controls, dict):
        raise ValueError("reviewed operand policy controls are required")
    if controls.get("automatic_extension_promotion_allowed") is not False:
        raise ValueError("automatic issuer-extension promotion must remain disabled")
    if controls.get("reparse_allowed") is not False:
        raise ValueError("reviewed operand policy must prohibit reparsing")
    if controls.get("network_allowed") is not False:
        raise ValueError("reviewed operand policy must prohibit network access")
    facts = policy.get("fact_repairs")
    overrides = policy.get("availability_overrides")
    if not isinstance(facts, list) or not facts:
        raise ValueError("fact_repairs must be a non-empty list")
    if not isinstance(overrides, list) or not overrides:
        raise ValueError("availability_overrides must be a non-empty list")
    repair_ids: set[str] = set()
    fact_identities: set[tuple[str, str, str, str, str]] = set()
    for raw in facts:
        if not isinstance(raw, dict):
            raise ValueError("each fact repair must be an object")
        repair_id = _required_text(raw, "repair_id", identity="fact_repair")
        if repair_id in repair_ids:
            raise ValueError(f"duplicate repair_id={repair_id}")
        repair_ids.add(repair_id)
        derivation = _required_text(raw, "derivation_type", identity=repair_id)
        if derivation not in ALLOWED_FACT_DERIVATIONS:
            raise ValueError(f"{repair_id}: unsupported derivation_type={derivation}")
        identity = (
            _required_text(raw, "ticker", identity=repair_id),
            _required_text(raw, "canonical_metric", identity=repair_id),
            str(raw.get("period_start") or ""),
            str(raw.get("period_end") or ""),
            str(raw.get("accession_number") or ""),
        )
        if derivation in {"parser_evidence_sum", "normalized_fact_sum"}:
            # These fields are resolved from immutable run evidence.
            identity = (identity[0], identity[1], repair_id, "", "")
        if identity in fact_identities:
            raise ValueError(f"{repair_id}: duplicate output identity={identity}")
        fact_identities.add(identity)
        raw_value = raw.get("value")
        if raw_value is None or float(str(raw_value)) < 0:
            raise ValueError(f"{repair_id}: reviewed operand value must be nonnegative")
    override_ids: set[str] = set()
    for raw in overrides:
        if not isinstance(raw, dict):
            raise ValueError("each availability override must be an object")
        override_id = _required_text(raw, "override_id", identity="availability_override")
        if override_id in override_ids:
            raise ValueError(f"duplicate override_id={override_id}")
        override_ids.add(override_id)
        status = _required_text(raw, "availability_status", identity=override_id)
        if status not in ALLOWED_OVERRIDE_STATUSES:
            raise ValueError(f"{override_id}: unsupported availability_status={status}")


def _parser_evidence_rows(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    evidence_keys: list[str],
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in evidence_keys)
    return connection.execute(
        f"""
        SELECT evidence.*
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key=relation.evidence_key
        WHERE relation.run_id=?
          AND evidence.evidence_key IN ({placeholders})
        ORDER BY evidence.evidence_key
        """,
        (run_id, *evidence_keys),
    ).fetchall()


def _normalized_fact_rows(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    fingerprints: list[str],
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in fingerprints)
    return connection.execute(
        f"""
        SELECT fact.*
        FROM sec_parser_run_normalized_fact AS relation
        JOIN sec_parser_normalized_fact_shadow AS fact
          ON fact.fact_fingerprint=relation.fact_fingerprint
        WHERE relation.run_id=?
          AND fact.fact_fingerprint IN ({placeholders})
        ORDER BY fact.fact_fingerprint
        """,
        (run_id, *fingerprints),
    ).fetchall()


def _resolved_from_parser_evidence(
    connection: sqlite3.Connection,
    raw: Mapping[str, Any],
    *,
    run_id: int,
) -> ResolvedFact:
    repair_id = str(raw["repair_id"])
    keys = [str(value) for value in raw.get("evidence_keys") or []]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError(f"{repair_id}: evidence_keys must be unique and non-empty")
    rows = _parser_evidence_rows(connection, run_id=run_id, evidence_keys=keys)
    if len(rows) != len(keys):
        found = {str(row["evidence_key"]) for row in rows}
        raise ValueError(f"{repair_id}: missing evidence keys={sorted(set(keys) - found)}")
    ticker = _same_text((row["ticker"] for row in rows), field="ticker", identity=repair_id)
    if ticker != str(raw["ticker"]):
        raise ValueError(f"{repair_id}: policy/evidence ticker mismatch")
    metric = _same_text(
        (row["metric_name"] for row in rows),
        field="metric_name",
        identity=repair_id,
    )
    if metric != str(raw["canonical_metric"]):
        raise ValueError(f"{repair_id}: policy/evidence metric mismatch")
    if any(str(row["candidate_status"]) != "REVIEW_REQUIRED" for row in rows):
        raise ValueError(f"{repair_id}: source extension status changed")
    scopes = {str(row["scope"] or "") for row in rows}
    dimensional = scopes - {"consolidated"}
    if dimensional and not bool(raw.get("allow_dimensional_exhaustive_sum")):
        raise ValueError(f"{repair_id}: dimensional source requires explicit exhaustive policy")
    if dimensional and scopes != {"dimensional"}:
        raise ValueError(f"{repair_id}: mixed consolidated/dimensional sum is prohibited")
    values = [float(row["candidate_value"]) for row in rows]
    computed = sum(values)
    expected = float(raw["value"])
    _assert_close(computed, expected, identity=repair_id)
    period_start = _same_text(
        (row["period_start"] for row in rows),
        field="period_start",
        identity=repair_id,
    )
    period_end = _same_text(
        (str(row["period_end"] or "")[:10] for row in rows),
        field="period_end",
        identity=repair_id,
    )
    unit = _same_text((row["unit"] for row in rows), field="unit", identity=repair_id).upper()
    concept = (
        str(rows[0]["concept_name"])
        if len(rows) == 1
        else f"ReviewedExhaustive{metric.title().replace('_', '')}Sum"
    )
    return ResolvedFact(
        repair_id=repair_id,
        ticker=ticker,
        cik=_same_text((row["cik"] for row in rows), field="cik", identity=repair_id),
        accession_number=_same_text(
            (row["accession_number"] for row in rows),
            field="accession_number",
            identity=repair_id,
        ),
        form_type=_same_text(
            (row["form_type"] for row in rows),
            field="form_type",
            identity=repair_id,
        ).upper(),
        filing_date=_same_text(
            (str(row["filing_date"] or "")[:10] for row in rows),
            field="filing_date",
            identity=repair_id,
        ),
        accepted_at=_same_text(
            (row["accepted_at"] for row in rows),
            field="accepted_at",
            identity=repair_id,
        ),
        fiscal_year=int(period_end[:4]),
        fiscal_period="FY" if period_start.endswith("-01-01") else "",
        period_start=period_start,
        period_end=period_end,
        canonical_metric=metric,
        financial_statement=str(raw["financial_statement"]),
        period_type=str(raw["period_type"]),
        unit=unit,
        value=expected,
        taxonomy="transportation-reviewed",
        concept_name=concept,
        derivation_type=str(raw["derivation_type"]),
        rationale=str(raw.get("rationale") or ""),
        provenance={
            "evidence_keys": keys,
            "component_values": values,
            "component_scopes": sorted(scopes),
        },
    )


def _resolved_from_normalized_facts(
    connection: sqlite3.Connection,
    raw: Mapping[str, Any],
    *,
    run_id: int,
) -> ResolvedFact:
    repair_id = str(raw["repair_id"])
    fingerprints = [str(value) for value in raw.get("fact_fingerprints") or []]
    excluded = [str(value) for value in raw.get("excluded_fact_fingerprints") or []]
    if not fingerprints or len(fingerprints) != len(set(fingerprints)):
        raise ValueError(f"{repair_id}: fact_fingerprints must be unique and non-empty")
    if set(fingerprints) & set(excluded):
        raise ValueError(f"{repair_id}: selected and excluded fingerprints overlap")
    rows = _normalized_fact_rows(
        connection,
        run_id=run_id,
        fingerprints=fingerprints,
    )
    if len(rows) != len(fingerprints):
        found = {str(row["fact_fingerprint"]) for row in rows}
        raise ValueError(
            f"{repair_id}: missing normalized facts={sorted(set(fingerprints) - found)}"
        )
    if excluded:
        excluded_rows = _normalized_fact_rows(
            connection,
            run_id=run_id,
            fingerprints=excluded,
        )
        if len(excluded_rows) != len(excluded):
            raise ValueError(f"{repair_id}: excluded alternative evidence changed")
    ticker = _same_text((row["ticker"] for row in rows), field="ticker", identity=repair_id)
    if ticker != str(raw["ticker"]):
        raise ValueError(f"{repair_id}: policy/fact ticker mismatch")
    if any(str(row["dimensions_json"] or "{}") not in {"", "{}"} for row in rows):
        raise ValueError(f"{repair_id}: normalized sum contains dimensional facts")
    values = [float(row["numeric_value"]) for row in rows]
    expected = float(raw["value"])
    _assert_close(sum(values), expected, identity=repair_id)
    period_start = _same_text(
        (row["period_start"] for row in rows),
        field="period_start",
        identity=repair_id,
    )
    period_end = _same_text(
        (str(row["period_end"] or "")[:10] for row in rows),
        field="period_end",
        identity=repair_id,
    )
    return ResolvedFact(
        repair_id=repair_id,
        ticker=ticker,
        cik=_same_text((row["cik"] for row in rows), field="cik", identity=repair_id),
        accession_number=_same_text(
            (row["accession_number"] for row in rows),
            field="accession_number",
            identity=repair_id,
        ),
        form_type=_same_text(
            (row["form_type"] for row in rows),
            field="form_type",
            identity=repair_id,
        ).upper(),
        filing_date=_same_text(
            (str(row["filing_date"] or "")[:10] for row in rows),
            field="filing_date",
            identity=repair_id,
        ),
        accepted_at=_same_text(
            (row["accepted_at"] for row in rows),
            field="accepted_at",
            identity=repair_id,
        ),
        fiscal_year=int(raw.get("fiscal_year") or period_end[:4]),
        fiscal_period=str(raw.get("fiscal_period") or "FY"),
        period_start=period_start,
        period_end=period_end,
        canonical_metric=str(raw["canonical_metric"]),
        financial_statement=str(raw["financial_statement"]),
        period_type=str(raw["period_type"]),
        unit=_same_text((row["unit"] for row in rows), field="unit", identity=repair_id).upper(),
        value=expected,
        taxonomy=str(raw.get("taxonomy") or "transportation-reviewed"),
        concept_name=str(raw.get("concept_name") or "ReviewedNormalizedFactSum"),
        derivation_type=str(raw["derivation_type"]),
        rationale=str(raw.get("rationale") or ""),
        provenance={
            "fact_fingerprints": fingerprints,
            "excluded_fact_fingerprints": excluded,
            "component_values": values,
        },
    )


def _base_document_fact(
    raw: Mapping[str, Any],
    *,
    provenance: dict[str, Any],
) -> ResolvedFact:
    return ResolvedFact(
        repair_id=str(raw["repair_id"]),
        ticker=str(raw["ticker"]),
        cik=str(raw["cik"]),
        accession_number=str(raw["accession_number"]),
        form_type=str(raw["form_type"]).upper(),
        filing_date=str(raw["filing_date"])[:10],
        accepted_at=str(raw["accepted_at"]),
        fiscal_year=int(raw["fiscal_year"]),
        fiscal_period=str(raw["fiscal_period"]),
        period_start=str(raw["period_start"]),
        period_end=str(raw["period_end"])[:10],
        canonical_metric=str(raw["canonical_metric"]),
        financial_statement=str(raw["financial_statement"]),
        period_type=str(raw["period_type"]),
        unit=str(raw["unit"]).upper(),
        value=float(raw["value"]),
        taxonomy=str(raw["taxonomy"]),
        concept_name=str(raw["concept_name"]),
        derivation_type=str(raw["derivation_type"]),
        rationale=str(raw.get("rationale") or ""),
        provenance=provenance,
    )


def _resolved_from_ix_fact(
    raw: Mapping[str, Any],
    *,
    project_root: Path,
) -> ResolvedFact:
    repair_id = str(raw["repair_id"])
    path, sha = _resolve_document(
        raw["source_document"],
        project_root=project_root,
        identity=repair_id,
    )
    document = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(document, "html.parser")
    expected = raw["ix_fact"]
    matches = []
    for tag in soup.find_all(True):
        if not tag.name or not tag.name.lower().endswith("nonfraction"):
            continue
        if (
            str(tag.get("name") or "").lower() == str(expected["name"]).lower()
            and str(tag.get("contextref") or "") == str(expected["context_ref"])
            and str(tag.get("unitref") or "") == str(expected["unit_ref"])
            and int(str(tag.get("scale") or 0)) == int(expected["scale"])
        ):
            matches.append(tag)
    if len(matches) != 1:
        raise ValueError(f"{repair_id}: expected exactly one matching ix fact, found={len(matches)}")
    raw_text = re.sub(r"\s+", "", matches[0].get_text("", strip=True))
    expected_text = re.sub(r"\s+", "", str(expected["raw_value"]))
    if raw_text != expected_text:
        raise ValueError(f"{repair_id}: ix raw value changed={raw_text}")
    numeric = float(raw_text.replace(",", "")) * (10 ** int(expected["scale"]))
    if str(matches[0].get("sign") or "") == "-":
        numeric = -numeric
    _assert_close(abs(numeric), float(raw["value"]), identity=repair_id)
    return _base_document_fact(
        raw,
        provenance={
            "source_document": str(path),
            "content_sha256": sha,
            "ix_fact": dict(expected),
        },
    )


def _resolved_from_explicit_zero(
    raw: Mapping[str, Any],
    *,
    project_root: Path,
) -> ResolvedFact:
    repair_id = str(raw["repair_id"])
    if float(raw["value"]) != 0.0:
        raise ValueError(f"{repair_id}: explicit-zero repair must equal zero")
    path, sha = _resolve_document(
        raw["source_document"],
        project_root=project_root,
        identity=repair_id,
    )
    normalized = _normalize_document_text(
        path.read_text(encoding="utf-8", errors="ignore")
    ).lower()
    anchors = [str(value) for value in raw.get("text_anchors") or []]
    if len(anchors) < 2:
        raise ValueError(f"{repair_id}: explicit zero requires multiple anchors")
    missing = [anchor for anchor in anchors if anchor.lower() not in normalized]
    if missing:
        raise ValueError(f"{repair_id}: document anchors changed={missing}")
    return _base_document_fact(
        raw,
        provenance={
            "source_document": str(path),
            "content_sha256": sha,
            "text_anchors": anchors,
            "explicit_zero": True,
        },
    )


def _resolved_from_reviewed_formula(
    raw: Mapping[str, Any],
    *,
    project_root: Path,
) -> ResolvedFact:
    repair_id = str(raw["repair_id"])
    path, sha = _resolve_document(
        raw["source_document"],
        project_root=project_root,
        identity=repair_id,
    )
    normalized = _normalize_document_text(
        path.read_text(encoding="utf-8", errors="ignore")
    ).lower()
    components = raw.get("formula_components")
    if not isinstance(components, list) or len(components) < 2:
        raise ValueError(f"{repair_id}: formula_components are required")
    for component in components:
        label = str(component.get("label") or "").lower()
        display = str(component.get("display_value") or "").lower()
        if not label or label not in normalized or not display or display not in normalized:
            raise ValueError(f"{repair_id}: formula component evidence changed={component}")
    computed = sum(float(component["signed_value"]) for component in components)
    expected = float(raw["value"])
    _assert_close(computed, expected, identity=repair_id)
    cross = raw.get("cross_check")
    if not isinstance(cross, dict):
        raise ValueError(f"{repair_id}: independent cross_check is required")
    signed_cross_components = cross.get("signed_components")
    if signed_cross_components is not None:
        if (
            not isinstance(signed_cross_components, list)
            or len(signed_cross_components) < 2
        ):
            raise ValueError(
                f"{repair_id}: cross_check signed_components are invalid"
            )
        for component in signed_cross_components:
            label = str(component.get("label") or "").lower()
            display = str(component.get("display_value") or "").lower()
            if (
                not label
                or label not in normalized
                or not display
                or display not in normalized
            ):
                raise ValueError(
                    f"{repair_id}: cross-check evidence changed={component}"
                )
        cross_value = sum(
            float(component["signed_value"])
            for component in signed_cross_components
        )
    else:
        cross_value = (
            float(cross["pretax_income"])
            + float(cross["interest_expense"])
            + float(cross["loss_on_debt_extinguishment"])
            - float(cross["interest_income"])
        )
    _assert_close(cross_value, expected, identity=f"{repair_id}:cross_check")
    _assert_close(float(cross["expected_value"]), expected, identity=f"{repair_id}:expected")
    return _base_document_fact(
        raw,
        provenance={
            "source_document": str(path),
            "content_sha256": sha,
            "formula_components": components,
            "cross_check": dict(cross),
        },
    )


def resolve_policy(
    connection: sqlite3.Connection,
    policy: Mapping[str, Any],
    *,
    project_root: Path = PROJECT_ROOT,
) -> tuple[list[ResolvedFact], list[ResolvedOverride], int]:
    validate_policy_contract(policy)
    run_id = int(policy["parser_run_id"])
    run = connection.execute(
        "SELECT * FROM sec_parser_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if (
        run is None
        or str(run["model_family"]) != MODEL_FAMILY
        or str(run["status"]) != "COMPLETED"
        or int(run["failed_work_count"] or 0) != 0
    ):
        raise ValueError("sealed parser run is missing, incomplete, or has failures")
    resolved_facts: list[ResolvedFact] = []
    validated_documents: set[Path] = set()
    for raw in policy["fact_repairs"]:
        derivation = str(raw["derivation_type"])
        if derivation == "parser_evidence_sum":
            fact = _resolved_from_parser_evidence(
                connection,
                raw,
                run_id=run_id,
            )
        elif derivation == "normalized_fact_sum":
            fact = _resolved_from_normalized_facts(
                connection,
                raw,
                run_id=run_id,
            )
        elif derivation == "document_ix_fact":
            fact = _resolved_from_ix_fact(raw, project_root=project_root)
        elif derivation == "document_explicit_zero":
            fact = _resolved_from_explicit_zero(raw, project_root=project_root)
        elif derivation == "document_reviewed_formula":
            fact = _resolved_from_reviewed_formula(raw, project_root=project_root)
        else:  # pragma: no cover - guarded by validate_policy_contract
            raise AssertionError(derivation)
        if "source_document" in raw:
            path, _ = _resolve_document(
                raw["source_document"],
                project_root=project_root,
                identity=str(raw["repair_id"]),
            )
            validated_documents.add(path)
        resolved_facts.append(fact)
    resolved_overrides: list[ResolvedOverride] = []
    for raw in policy["availability_overrides"]:
        override_id = str(raw["override_id"])
        path, sha = _resolve_document(
            raw["source_document"],
            project_root=project_root,
            identity=override_id,
        )
        validated_documents.add(path)
        evidence_key = stable_hash(
            {
                "policy_version": POLICY_VERSION,
                "override_id": override_id,
                "content_sha256": sha,
                "status": raw["availability_status"],
            }
        )
        resolved_overrides.append(
            ResolvedOverride(
                override_id=override_id,
                ticker=str(raw["ticker"]),
                metric_name=str(raw["metric_name"]),
                availability_status=str(raw["availability_status"]),
                status_reason=(
                    f"{POLICY_VERSION}:{str(raw['status_reason'])}"
                ),
                valid_from=str(raw["valid_from"])[:10],
                evidence_key=evidence_key,
                rationale=str(raw.get("rationale") or ""),
                provenance={
                    "source_document": str(path),
                    "content_sha256": sha,
                },
            )
        )
    identities = {
        (
            fact.ticker,
            fact.canonical_metric,
            fact.period_start,
            fact.period_end,
            fact.accession_number,
        )
        for fact in resolved_facts
    }
    if len(identities) != len(resolved_facts):
        raise ValueError("resolved fact outputs contain duplicate identities")
    return resolved_facts, resolved_overrides, len(validated_documents)


def _ensure_source_registry(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_owner, source_type,
            base_url, authentication_required, free_key_required,
            refresh_frequency, data_owner, raw_schema, staging_tables,
            canonical_tables, feature_stages, subsector_scope, priority,
            status, notes, created_at, updated_at
        )
        VALUES (
            ?, 'financial_fundamentals',
            'Transportation Reviewed Required-Metric Operands',
            'internal', 'reviewed_sec_filing_evidence',
            'sec-cache://transportation-reviewed-operands', 0, 0,
            'policy_review', 'SEC issuers',
            '["sec_parser_metric_evidence_shadow","sec_parser_normalized_fact_shadow","cached_sec_filings"]',
            '["fact_sec_xbrl_fact_raw","fact_sec_xbrl_fact"]',
            '["fact_financial_statement_canonical"]',
            '["financial_features"]', 'transportation', 5, 'active',
            'Fail-closed reviewed operands derived from sealed parser run 68 and hash-locked cached filings.',
            ?, ?
        )
        ON CONFLICT(source_id) DO UPDATE SET
            source_name=excluded.source_name,
            source_type=excluded.source_type,
            base_url=excluded.base_url,
            refresh_frequency=excluded.refresh_frequency,
            raw_schema=excluded.raw_schema,
            staging_tables=excluded.staging_tables,
            canonical_tables=excluded.canonical_tables,
            feature_stages=excluded.feature_stages,
            subsector_scope=excluded.subsector_scope,
            priority=excluded.priority,
            status=excluded.status,
            notes=excluded.notes,
            updated_at=excluded.updated_at
        """,
        (source_id, now, now),
    )


def _insert_resolved_fact(
    connection: sqlite3.Connection,
    *,
    fact: ResolvedFact,
    source_id: str,
    source_priority: int,
    policy_sha256: str,
    now: str,
) -> int:
    fact_key = stable_hash(
        {
            "source_id": source_id,
            "policy_sha256": policy_sha256,
            "repair_id": fact.repair_id,
            "ticker": fact.ticker,
            "accession_number": fact.accession_number,
            "period_start": fact.period_start,
            "period_end": fact.period_end,
            "canonical_metric": fact.canonical_metric,
            "value": fact.value,
            "unit": fact.unit,
        }
    )
    frame = f"reviewed-policy:{fact.repair_id}"
    source_detail = (
        f"{fact.canonical_metric}:transportation_reviewed_required_metric_operand"
    )
    payload = {
        "policy_version": POLICY_VERSION,
        "policy_sha256": policy_sha256,
        "repair_id": fact.repair_id,
        "review_status": "ACCEPTED",
        "derivation_type": fact.derivation_type,
        "rationale": fact.rationale,
        "provenance": fact.provenance,
    }
    connection.execute(
        """
        INSERT INTO fact_sec_xbrl_fact_raw(
            fact_key, ticker, cik, source_id, accession_number, form_type,
            filing_date, accepted_at, fiscal_year, fiscal_period,
            period_start, period_end, frame, taxonomy, concept_name, unit,
            raw_value, decimals, source_detail, payload_json, created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                ?, ?, ?, ?)
        ON CONFLICT(fact_key) DO UPDATE SET
            filing_date=excluded.filing_date,
            accepted_at=excluded.accepted_at,
            fiscal_year=excluded.fiscal_year,
            fiscal_period=excluded.fiscal_period,
            raw_value=excluded.raw_value,
            payload_json=excluded.payload_json,
            updated_at=excluded.updated_at
        """,
        (
            fact_key,
            fact.ticker,
            fact.cik,
            source_id,
            fact.accession_number,
            fact.form_type,
            fact.filing_date,
            fact.accepted_at,
            fact.fiscal_year,
            fact.fiscal_period,
            fact.period_start,
            fact.period_end,
            frame,
            fact.taxonomy,
            fact.concept_name,
            fact.unit.lower(),
            fact.value,
            source_detail,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            now,
            now,
        ),
    )
    raw_row = connection.execute(
        "SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw WHERE fact_key=?",
        (fact_key,),
    ).fetchone()
    if raw_row is None:
        raise RuntimeError(f"{fact.repair_id}: raw fact was not persisted")
    raw_fact_id = int(raw_row["raw_fact_id"])
    sign_policy = "positive_abs" if fact.canonical_metric == "capex" else "as_reported"
    connection.execute(
        """
        INSERT INTO fact_sec_xbrl_fact(
            raw_fact_id, ticker, cik, source_id, accession_number, form_type,
            filing_date, accepted_at, fiscal_year, fiscal_period,
            period_start, period_end, frame, taxonomy, concept_name,
            canonical_metric, financial_statement, period_type, unit, value,
            sign_policy, source_priority, source_detail, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name,
                    canonical_metric, unit, period_start, period_end, frame)
        DO UPDATE SET
            raw_fact_id=excluded.raw_fact_id,
            filing_date=excluded.filing_date,
            accepted_at=excluded.accepted_at,
            fiscal_year=excluded.fiscal_year,
            fiscal_period=excluded.fiscal_period,
            value=excluded.value,
            source_priority=excluded.source_priority,
            source_detail=excluded.source_detail,
            updated_at=excluded.updated_at
        """,
        (
            raw_fact_id,
            fact.ticker,
            fact.cik,
            source_id,
            fact.accession_number,
            fact.form_type,
            fact.filing_date,
            fact.accepted_at,
            fact.fiscal_year,
            fact.fiscal_period,
            fact.period_start,
            fact.period_end,
            frame,
            fact.taxonomy,
            fact.concept_name,
            fact.canonical_metric,
            fact.financial_statement,
            fact.period_type,
            fact.unit.lower(),
            fact.value,
            sign_policy,
            source_priority,
            f"{source_detail}_mapped",
            now,
            now,
        ),
    )
    return raw_fact_id


def persist_policy(
    connection: sqlite3.Connection,
    *,
    facts: list[ResolvedFact],
    overrides: list[ResolvedOverride],
    policy_path: Path,
    source_priority: int,
) -> dict[str, Any]:
    ensure_parser_schema(connection)
    now = utc_now()
    policy_sha = file_sha256(policy_path)
    with connection:
        _ensure_source_registry(connection, source_id=SOURCE_ID, now=now)
        connection.execute(
            "DELETE FROM fact_sec_xbrl_fact WHERE source_id=?",
            (SOURCE_ID,),
        )
        connection.execute(
            "DELETE FROM fact_sec_xbrl_fact_raw WHERE source_id=?",
            (SOURCE_ID,),
        )
        connection.execute(
            """
            UPDATE sec_parser_production_metric_override
            SET active=0
            WHERE model_family=?
              AND status_reason LIKE ?
            """,
            (MODEL_FAMILY, f"{POLICY_VERSION}:%"),
        )
        raw_fact_ids = [
            _insert_resolved_fact(
                connection,
                fact=fact,
                source_id=SOURCE_ID,
                source_priority=source_priority,
                policy_sha256=policy_sha,
                now=now,
            )
            for fact in facts
        ]
        for override in overrides:
            connection.execute(
                """
                INSERT INTO sec_parser_production_metric_override(
                    model_family, ticker, metric_name, availability_status,
                    status_reason, evidence_key, valid_from, active, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(model_family, ticker, metric_name, evidence_key)
                DO UPDATE SET
                    availability_status=excluded.availability_status,
                    status_reason=excluded.status_reason,
                    valid_from=excluded.valid_from,
                    active=1
                """,
                (
                    MODEL_FAMILY,
                    override.ticker,
                    override.metric_name,
                    override.availability_status,
                    override.status_reason,
                    override.evidence_key,
                    override.valid_from,
                    now,
                ),
            )
    fact_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM fact_sec_xbrl_fact WHERE source_id=?",
            (SOURCE_ID,),
        ).fetchone()[0]
    )
    active_override_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM sec_parser_production_metric_override
            WHERE model_family=? AND active=1
              AND status_reason LIKE ?
            """,
            (MODEL_FAMILY, f"{POLICY_VERSION}:%"),
        ).fetchone()[0]
    )
    if fact_count != len(facts):
        raise RuntimeError(
            f"persisted reviewed fact count={fact_count} expected={len(facts)}"
        )
    if active_override_count != len(overrides):
        raise RuntimeError(
            "persisted reviewed override count="
            f"{active_override_count} expected={len(overrides)}"
        )
    return {
        "source_id": SOURCE_ID,
        "policy_sha256": policy_sha,
        "fact_count": fact_count,
        "raw_fact_ids": raw_fact_ids,
        "active_override_count": active_override_count,
    }
