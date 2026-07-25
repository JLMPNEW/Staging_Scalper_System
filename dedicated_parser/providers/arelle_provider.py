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
    model = None
    facts: list[NormalizedFact] = []
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
            numeric_value: float | None = None
            try:
                candidate = float(value_text.replace(",", ""))
                if math.isfinite(candidate):
                    numeric_value = candidate
            except ValueError:
                pass
            dimensions = _dimension_payload(context)
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
                    scope="consolidated" if not dimensions else "dimensional",
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
