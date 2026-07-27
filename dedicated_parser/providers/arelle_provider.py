from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import NormalizedFact


def _date_text(value: Any, *, exclusive_end: bool = False) -> str:
    if not isinstance(value, datetime):
        return ""
    normalized = value - timedelta(days=1) if exclusive_end else value
    return normalized.date().isoformat()


def _dimension_payload(context: Any) -> dict[str, str]:
    payload: dict[str, str] = {}
    for dimension_qname, dimension in getattr(context, "qnameDims", {}).items():
        axis = str(dimension_qname)
        if bool(getattr(dimension, "isExplicit", False)):
            member = str(getattr(dimension, "memberQname", "") or "")
        else:
            typed = getattr(dimension, "typedMember", None)
            member = str(getattr(typed, "stringValue", "") or "")
        payload[axis] = member
    return payload


def _matches_requested_concept(concept_name: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, concept_name, re.IGNORECASE) for pattern in patterns)


def _safe_label(concept: Any) -> str:
    label_method = getattr(concept, "label", None)
    if not callable(label_method):
        return ""
    for kwargs in (
        {"lang": "en", "strip": True},
        {"lang": "en"},
        {},
    ):
        try:
            value = label_method(**kwargs)
        except (AttributeError, TypeError, ValueError):
            continue
        normalized = " ".join(str(value or "").split())
        if normalized:
            return normalized
    return ""


def _concept_name(concept: Any) -> str:
    qname = getattr(concept, "qname", None)
    return str(getattr(qname, "localName", "") or "")


def _relationship_concepts(
    model: Any,
    concept: Any,
    *,
    arcrole: str,
) -> tuple[str, ...]:
    try:
        relationship_set = model.relationshipSet(arcrole)
        relationships = [
            *relationship_set.fromModelObject(concept),
            *relationship_set.toModelObject(concept),
        ]
    except (AttributeError, TypeError, ValueError):
        return ()
    names: set[str] = set()
    for relationship in relationships:
        for related in (
            getattr(relationship, "fromModelObject", None),
            getattr(relationship, "toModelObject", None),
        ):
            name = _concept_name(related)
            if name and related is not concept:
                names.add(name)
    return tuple(sorted(names))


def _relationship_details(
    model: Any,
    concept: Any,
    *,
    arcrole: str,
) -> tuple[dict[str, Any], ...]:
    try:
        relationship_set = model.relationshipSet(arcrole)
        outgoing = list(relationship_set.fromModelObject(concept))
        incoming = list(relationship_set.toModelObject(concept))
    except (AttributeError, TypeError, ValueError):
        return ()
    details: list[dict[str, Any]] = []
    for direction, relationships in (
        ("outgoing", outgoing),
        ("incoming", incoming),
    ):
        for relationship in relationships:
            related = (
                getattr(relationship, "toModelObject", None)
                if direction == "outgoing"
                else getattr(relationship, "fromModelObject", None)
            )
            related_name = _concept_name(related)
            if not related_name:
                continue
            network_children: list[dict[str, Any]] = []
            if direction == "incoming":
                try:
                    parent_relationships = (
                        relationship_set.fromModelObject(related)
                    )
                except (AttributeError, TypeError, ValueError):
                    parent_relationships = ()
                for parent_relationship in parent_relationships:
                    child = getattr(
                        parent_relationship,
                        "toModelObject",
                        None,
                    )
                    child_name = _concept_name(child)
                    if not child_name:
                        continue
                    child_weight_value = getattr(
                        parent_relationship,
                        "weight",
                        None,
                    )
                    try:
                        child_weight = (
                            float(child_weight_value)
                            if child_weight_value is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        child_weight = None
                    network_children.append(
                        {
                            "concept_name": child_name,
                            "weight": child_weight,
                        }
                    )
            weight_value = getattr(relationship, "weight", None)
            order_value = getattr(relationship, "order", None)
            try:
                weight = (
                    float(weight_value)
                    if weight_value is not None
                    and math.isfinite(float(weight_value))
                    else None
                )
            except (TypeError, ValueError):
                weight = None
            try:
                order = (
                    float(order_value)
                    if order_value is not None
                    and math.isfinite(float(order_value))
                    else None
                )
            except (TypeError, ValueError):
                order = None
            details.append(
                {
                    "related_concept": related_name,
                    "direction": direction,
                    "weight": weight,
                    "order": order,
                    "linkrole": str(
                        getattr(relationship, "linkrole", "") or ""
                    ),
                    "arcrole": str(
                        getattr(relationship, "arcrole", "") or arcrole
                    ),
                    "network_children": sorted(
                        network_children,
                        key=lambda item: str(item["concept_name"]),
                    ),
                }
            )
    return tuple(
        sorted(
            details,
            key=lambda item: (
                str(item["linkrole"]),
                str(item["direction"]),
                float(item["order"] or 0.0),
                str(item["related_concept"]),
            ),
        )
    )


def _concept_metadata(model: Any, concept: Any) -> dict[str, Any]:
    qname = getattr(concept, "qname", None)
    namespace_uri = str(getattr(qname, "namespaceURI", "") or "")
    label = _safe_label(concept)
    documentation = " ".join(
        str(getattr(concept, "documentation", "") or "").split()
    )[:1000]
    official_namespace = bool(
        re.search(
            r"(?:fasb\.org/us-gaap|xbrl\.ifrs\.org|sec\.gov/|xbrl\.org/)",
            namespace_uri,
            re.IGNORECASE,
        )
    )
    return {
        "namespace_uri": namespace_uri,
        "label": label,
        "documentation": documentation,
        "is_extension": not official_namespace,
        "presentation_related_concepts": _relationship_concepts(
            model,
            concept,
            arcrole="http://www.xbrl.org/2003/arcrole/parent-child",
        ),
        "calculation_related_concepts": _relationship_concepts(
            model,
            concept,
            arcrole="http://www.xbrl.org/2003/arcrole/summation-item",
        ),
        "presentation_relationships": _relationship_details(
            model,
            concept,
            arcrole="http://www.xbrl.org/2003/arcrole/parent-child",
        ),
        "calculation_relationships": _relationship_details(
            model,
            concept,
            arcrole="http://www.xbrl.org/2003/arcrole/summation-item",
        ),
    }


def _concept_search_text(concept_name: str, metadata: dict[str, Any]) -> str:
    related = [
        *metadata.get("presentation_related_concepts", ()),
        *metadata.get("calculation_related_concepts", ()),
    ]
    return " ".join(
        (
            concept_name,
            str(metadata.get("label") or ""),
            str(metadata.get("documentation") or ""),
            *[str(item) for item in related],
        )
    )


def _unit_text(value: Any) -> str:
    unit = str(value or "")
    return unit.upper() if re.fullmatch(r"[A-Za-z]{3}", unit) else unit


def _fact_unit_text(fact: Any) -> str:
    unit = getattr(fact, "unit", None)
    measures = getattr(unit, "measures", None)
    if isinstance(measures, tuple) and measures:
        numerator = measures[0] if len(measures) > 0 else ()
        denominator = measures[1] if len(measures) > 1 else ()

        def names(values: Any) -> list[str]:
            return [
                str(
                    getattr(value, "localName", "")
                    or getattr(value, "clarkNotation", "")
                    or value
                )
                for value in values
            ]

        numerator_names = names(numerator)
        denominator_names = names(denominator)
        if numerator_names and not denominator_names:
            return "*".join(numerator_names)
        if numerator_names:
            return (
                f"{'*'.join(numerator_names)}/"
                f"{'*'.join(denominator_names)}"
            )
    return _unit_text(getattr(fact, "unitID", ""))


def _numeric_fact_value(fact: Any, value_text: str) -> float | None:
    """Return Arelle's validated value before falling back to display text."""

    # For inline XBRL, ``fact.value`` is the displayed token while ``xValue``
    # includes transformation and scale attributes. Reading the display token
    # directly turns values such as 9,199 with scale=3 into 9,199 dollars.
    for raw_value in (getattr(fact, "xValue", None), value_text.replace(",", "")):
        if raw_value is None or isinstance(raw_value, bool):
            continue
        try:
            candidate = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(candidate):
            return candidate
    return None


def extract_facts(
    entrypoint: Path,
    *,
    concept_patterns: tuple[str, ...],
) -> tuple[list[NormalizedFact], dict[str, Any]]:
    """Extract requested dimensional facts from one local XBRL entry point."""

    try:
        from arelle import Cntlr
    except ImportError:
        return [], {
            "provider": "arelle",
            "available": False,
            "status": "dependency_missing",
        }

    controller = Cntlr.Cntlr(
        logFileName="logToBuffer",
        disable_persistent_config=True,
    )
    # Cache-first contract: Arelle must never fetch taxonomies over the
    # network during a parse. Unresolvable schema references surface as
    # parse_failed instead of a silent (slow, nondeterministic) download.
    web_cache = getattr(controller, "webCache", None)
    if web_cache is not None:
        web_cache.workOffline = True
    model = None
    facts: list[NormalizedFact] = []
    seen_fact_keys: set[tuple[str, str, str, str]] = set()
    try:
        model = controller.modelManager.load(str(entrypoint))
        if model is None:
            return [], {
                "provider": "arelle",
                "available": True,
                "status": "load_failed",
            }
        metadata_cache: dict[str, dict[str, Any]] = {}
        for fact in model.facts:
            qname = getattr(fact, "qname", None)
            concept_name = str(getattr(qname, "localName", "") or "")
            concept = getattr(fact, "concept", None)
            metadata_key = str(qname or concept_name)
            concept_metadata = metadata_cache.get(metadata_key)
            if concept_metadata is None:
                concept_metadata = _concept_metadata(model, concept)
                metadata_cache[metadata_key] = concept_metadata
            search_text = _concept_search_text(
                concept_name,
                concept_metadata,
            )
            if not concept_name or not _matches_requested_concept(
                search_text,
                concept_patterns,
            ):
                continue
            if bool(getattr(fact, "isNil", False)):
                continue
            context = getattr(fact, "context", None)
            if context is None:
                continue
            value_text = str(getattr(fact, "value", "") or "").strip()
            numeric_value = _numeric_fact_value(fact, value_text)
            # iXBRL filings routinely tag the same fact twice (table +
            # narrative). Emitting each occurrence double-counts downstream
            # dimension aggregations; keep one per (context, concept, unit,
            # value).
            fact_key = (
                str(getattr(fact, "contextID", "") or ""),
                str(qname or concept_name),
                str(getattr(fact, "unitID", "") or ""),
                str(numeric_value) if numeric_value is not None else value_text,
            )
            if fact_key in seen_fact_keys:
                continue
            seen_fact_keys.add(fact_key)
            dimensions = _dimension_payload(context)
            # Non-XDT filers put scoping content in plain <segment>/<scenario>
            # children that qnameDims cannot see; such facts must not pass as
            # consolidated.
            has_non_xdt_scope = False
            non_dim_values = getattr(context, "nonDimValues", None)
            if callable(non_dim_values):
                try:
                    has_non_xdt_scope = bool(non_dim_values("segment")) or bool(
                        non_dim_values("scenario")
                    )
                except Exception:
                    has_non_xdt_scope = False
            if bool(getattr(context, "isStartEndPeriod", False)):
                period_start = _date_text(getattr(context, "startDatetime", None))
                period_end = _date_text(
                    getattr(context, "endDatetime", None),
                    exclusive_end=True,
                )
            elif bool(getattr(context, "isInstantPeriod", False)):
                period_start = ""
                period_end = _date_text(
                    getattr(context, "instantDatetime", None),
                    exclusive_end=True,
                )
            else:
                period_start = ""
                period_end = ""
            prefix = str(getattr(qname, "prefix", "") or "")
            taxonomy = prefix or str(getattr(qname, "namespaceURI", "") or "")
            facts.append(
                NormalizedFact(
                    taxonomy=taxonomy,
                    concept_name=concept_name,
                    value_text=value_text,
                    numeric_value=numeric_value,
                    unit=_fact_unit_text(fact),
                    period_start=period_start,
                    period_end=period_end,
                    context_id=str(getattr(fact, "contextID", "") or ""),
                    dimensions_json=json.dumps(
                        dimensions,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    scope=(
                        "consolidated"
                        if not dimensions and not has_non_xdt_scope
                        else "dimensional"
                    ),
                    source_document=entrypoint.name,
                    provider="arelle",
                    decimals=str(getattr(fact, "decimals", "") or ""),
                    concept_metadata_json=json.dumps(
                        concept_metadata,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        return facts, {
            "provider": "arelle",
            "available": True,
            "status": "parsed",
            "entrypoint": entrypoint.name,
            "model_fact_count": len(model.facts),
            "selected_fact_count": len(facts),
            "model_error_count": len(getattr(model, "errors", [])),
        }
    except Exception as exc:
        return [], {
            "provider": "arelle",
            "available": True,
            "status": "parse_failed",
            "entrypoint": entrypoint.name,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if model is not None:
            model.close()
        controller.close()
