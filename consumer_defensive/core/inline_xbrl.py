"""Deterministic numeric fact extraction from sealed inline-XBRL documents.

This is deliberately a narrow Stage 4 fallback parser.  It extracts normalized
numeric observations and their reporting contexts; it does not implement the
specialized-metric policy or promotion workflow owned by Stage 6B.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any


PARSER_VERSION = "consumer_defensive_inline_xbrl_v1"
_SPACE = re.compile(r"[\s\u00a0\u202f]+")
_NUMERIC_TAGS = {"nonfraction", "fraction"}


@dataclass(frozen=True)
class InlineNumericFact:
    taxonomy: str
    concept: str
    value_text: str
    numeric_value: float
    unit: str | None
    period_start: str | None
    period_end: str
    context_id: str
    dimensions_json: str


@dataclass(frozen=True)
class InlineParseResult:
    facts: tuple[InlineNumericFact, ...]
    contexts: int
    units: int
    skipped_facts: int
    unsupported_transformations: tuple[str, ...]


def _local_name(value: str) -> str:
    return value.rsplit(":", 1)[-1].lower()


class _InlineDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.contexts: dict[str, dict[str, Any]] = {}
        self.units: dict[str, dict[str, list[str]]] = {}
        self.facts: list[dict[str, Any]] = []
        self.continuations: dict[str, dict[str, Any]] = {}
        self.current_context: dict[str, Any] | None = None
        self.current_unit: dict[str, Any] | None = None
        self.current_fact: dict[str, Any] | None = None
        self.current_continuation: dict[str, Any] | None = None
        self.exclude_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs, self_closing=True)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        attr = {str(key).lower(): "" if value is None else str(value) for key, value in attrs}
        local = _local_name(tag)
        self.stack.append(local)
        if local == "context":
            self.current_context = {
                "id": attr.get("id", ""),
                "start": [],
                "end": [],
                "instant": [],
                "identifier": [],
                "dimensions": [],
                "current_dimension": None,
            }
        elif local in {"explicitmember", "typedmember"} and self.current_context:
            item = {
                "dimension": attr.get("dimension", ""),
                "kind": local,
                "value": [],
            }
            self.current_context["dimensions"].append(item)
            self.current_context["current_dimension"] = item
        elif local == "unit":
            self.current_unit = {"id": attr.get("id", ""), "numerator": [], "denominator": []}
        elif local in _NUMERIC_TAGS:
            self.current_fact = {"attrs": attr, "text": []}
            self.exclude_depth = 0
        elif local == "continuation":
            self.current_continuation = {"attrs": attr, "text": []}
            self.exclude_depth = 0
        elif local == "exclude" and (self.current_fact or self.current_continuation):
            self.exclude_depth += 1
        if self_closing:
            self._end(tag)

    def handle_endtag(self, tag: str) -> None:
        self._end(tag)

    def _end(self, tag: str) -> None:
        local = _local_name(tag)
        if local == "exclude" and self.exclude_depth:
            self.exclude_depth -= 1
        elif local in {"explicitmember", "typedmember"} and self.current_context:
            self.current_context["current_dimension"] = None
        elif local == "context" and self.current_context is not None:
            context_id = str(self.current_context.get("id") or "")
            if context_id:
                self.contexts[context_id] = self.current_context
            self.current_context = None
        elif local == "unit" and self.current_unit is not None:
            unit_id = str(self.current_unit.get("id") or "")
            if unit_id:
                self.units[unit_id] = self.current_unit
            self.current_unit = None
        elif local in _NUMERIC_TAGS and self.current_fact is not None:
            self.facts.append(self.current_fact)
            self.current_fact = None
            self.exclude_depth = 0
        elif local == "continuation" and self.current_continuation is not None:
            continuation_id = str(self.current_continuation["attrs"].get("id") or "")
            if continuation_id:
                self.continuations[continuation_id] = self.current_continuation
            self.current_continuation = None
            self.exclude_depth = 0
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self.current_fact is not None and not self.exclude_depth:
            self.current_fact["text"].append(data)
        if self.current_continuation is not None and not self.exclude_depth:
            self.current_continuation["text"].append(data)
        if self.current_context is not None and self.stack:
            local = self.stack[-1]
            key = {
                "startdate": "start",
                "enddate": "end",
                "instant": "instant",
                "identifier": "identifier",
            }.get(local)
            if key:
                self.current_context[key].append(data)
            dimension = self.current_context.get("current_dimension")
            if dimension is not None and local not in {"explicitmember", "typedmember"}:
                dimension["value"].append(data)
            elif dimension is not None:
                dimension["value"].append(data)
        if self.current_unit is not None and self.stack and self.stack[-1] == "measure":
            target = "denominator" if "unitdenominator" in self.stack else "numerator"
            self.current_unit[target].append(data)


def _joined(parts: list[str]) -> str:
    return _SPACE.sub(" ", "".join(parts)).strip()


def _continuation_text(
    first: dict[str, Any], continuations: dict[str, dict[str, Any]]
) -> str:
    values = [_joined(first["text"])]
    next_id = str(first["attrs"].get("continuedat") or "")
    seen: set[str] = set()
    while next_id:
        if next_id in seen or next_id not in continuations:
            raise ValueError(f"Invalid inline-XBRL continuation chain at {next_id!r}")
        seen.add(next_id)
        item = continuations[next_id]
        values.append(_joined(item["text"]))
        next_id = str(item["attrs"].get("continuedat") or "")
    return _joined(values)


def _numeric_value(text: str, attrs: dict[str, str]) -> Decimal:
    transform = str(attrs.get("format") or "").rsplit(":", 1)[-1].lower()
    normalized = _SPACE.sub("", text).strip()
    if transform in {"fixed-zero", "zerodash", "num-zero"}:
        value = Decimal(0)
    else:
        negative_parentheses = normalized.startswith("(") and normalized.endswith(")")
        if negative_parentheses:
            normalized = normalized[1:-1]
        normalized = re.sub(r"[^0-9,\.eE+\-]", "", normalized)
        if transform in {"num-comma-decimal", "numcomma", "num-dot-comma"}:
            normalized = normalized.replace(".", "").replace(",", ".")
        elif transform in {
            "", "num-dot-decimal", "numdotdecimal", "num-comma-dot",
            "num-unit-decimal", "numunitdecimal",
        }:
            normalized = normalized.replace(",", "")
        else:
            raise ValueError(f"unsupported_transform:{transform}")
        if normalized in {"", "+", "-", "."}:
            raise ValueError("empty_numeric_fact")
        try:
            value = Decimal(normalized)
        except InvalidOperation as exc:
            raise ValueError("invalid_numeric_fact") from exc
        if negative_parentheses:
            value = -value
    sign = str(attrs.get("sign") or "")
    if sign == "-":
        value = -abs(value)
    elif sign == "+":
        value = abs(value)
    try:
        scale = int(str(attrs.get("scale") or "0"))
    except ValueError as exc:
        raise ValueError("invalid_scale") from exc
    return value.scaleb(scale)


def _unit_text(unit: dict[str, list[str]] | None) -> str | None:
    if not unit:
        return None
    numerator = [_joined([value]) for value in unit["numerator"] if _joined([value])]
    denominator = [_joined([value]) for value in unit["denominator"] if _joined([value])]

    def compact(value: str) -> str:
        prefix, separator, local = value.partition(":")
        if separator and prefix.lower() == "iso4217":
            return local.upper()
        return value

    top = "*".join(compact(value) for value in numerator) or "pure"
    bottom = "*".join(compact(value) for value in denominator)
    return f"{top}/{bottom}" if bottom else top


def parse_inline_xbrl(raw: bytes) -> InlineParseResult:
    parser = _InlineDocumentParser()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
    except Exception as exc:
        raise ValueError(f"Inline-XBRL document could not be parsed: {exc}") from exc
    facts: list[InlineNumericFact] = []
    skipped = 0
    unsupported: set[str] = set()
    seen: set[tuple[Any, ...]] = set()
    for item in parser.facts:
        attrs = item["attrs"]
        if str(attrs.get("nil") or attrs.get("xsi:nil") or "").lower() in {"true", "1"}:
            skipped += 1
            continue
        qualified_name = str(attrs.get("name") or "").strip()
        context_id = str(attrs.get("contextref") or "").strip()
        if ":" not in qualified_name or not context_id or context_id not in parser.contexts:
            skipped += 1
            continue
        context = parser.contexts[context_id]
        period_end = _joined(context["instant"]) or _joined(context["end"])
        period_start = _joined(context["start"]) or None
        if not period_end:
            skipped += 1
            continue
        text = _continuation_text(item, parser.continuations)
        try:
            decimal_value = _numeric_value(text, attrs)
            numeric = float(decimal_value)
            if not math.isfinite(numeric):
                raise ValueError("nonfinite_numeric_fact")
        except ValueError as exc:
            reason = str(exc)
            if reason.startswith("unsupported_transform:"):
                unsupported.add(reason.partition(":")[2])
            skipped += 1
            continue
        taxonomy, concept = qualified_name.split(":", 1)
        dimensions = []
        for dimension in context["dimensions"]:
            value = _joined(dimension["value"])
            if dimension["dimension"] and value:
                dimensions.append({
                    "dimension": dimension["dimension"],
                    "member": value,
                    "kind": dimension["kind"],
                })
        dimensions.sort(key=lambda value: (value["dimension"], value["member"], value["kind"]))
        unit = _unit_text(parser.units.get(str(attrs.get("unitref") or "")))
        key = (
            taxonomy, concept, text, str(decimal_value), unit, period_start,
            period_end, context_id, json.dumps(dimensions, sort_keys=True),
        )
        if key in seen:
            continue
        seen.add(key)
        facts.append(InlineNumericFact(
            taxonomy=taxonomy,
            concept=concept,
            value_text=text,
            numeric_value=numeric,
            unit=unit,
            period_start=period_start,
            period_end=period_end,
            context_id=context_id,
            dimensions_json=json.dumps(dimensions, sort_keys=True, separators=(",", ":")),
        ))
    facts.sort(key=lambda fact: (
        fact.period_end, fact.period_start or "", fact.taxonomy, fact.concept,
        fact.context_id, fact.unit or "", fact.numeric_value, fact.value_text,
    ))
    return InlineParseResult(
        facts=tuple(facts),
        contexts=len(parser.contexts),
        units=len(parser.units),
        skipped_facts=skipped,
        unsupported_transformations=tuple(sorted(unsupported)),
    )
