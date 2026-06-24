#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

from macro_raw_config import configure_pipeline_logging, parse_boolish

logger = logging.getLogger(__name__)

DEFAULT_BASE_REGISTRY = Path(__file__).resolve().with_name("macro_metric_registry_seed.csv")
DEFAULT_COUNTRY_METADATA = Path(__file__).resolve().with_name("macro_country_metadata_seed.csv")
DEFAULT_COUNTRY_TEMPLATES = Path(__file__).resolve().with_name("macro_country_metric_templates.csv")
DEFAULT_OUTPUT = Path(__file__).resolve().with_name("macro_metric_registry_full.csv")


FIELDNAMES = [
    "registry_key",
    "metric_key",
    "regime_block",
    "source_name",
    "source_dataset",
    "source_series_id",
    "ref_area",
    "frequency",
    "seasonal_adjustment",
    "units",
    "vintage_policy",
    "update_cadence",
    "history_start_date",
    "revision_window_days",
    "source_priority",
    "worker_hint",
    "enabled",
    "source_params_json",
    "notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build full macro metric registry from U.S. base rows plus foreign-country templates.")
    parser.add_argument("--base-registry", type=Path, default=DEFAULT_BASE_REGISTRY)
    parser.add_argument("--country-metadata", type=Path, default=DEFAULT_COUNTRY_METADATA)
    parser.add_argument("--country-templates", type=Path, default=DEFAULT_COUNTRY_TEMPLATES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--include-disabled-country-rows",
        action="store_true",
        help="Include disabled foreign-country template rows in the generated registry. Default behavior is tier-1 runtime mode, which skips unresolved foreign rows entirely.",
    )
    return parser.parse_args()


def _read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]

def _apply_scope(scope_raw: str, allowed_raw: str) -> bool:
    scope = str(scope_raw or "").strip()
    allowed = {item.strip() for item in str(allowed_raw or "").split(",") if item.strip()}
    if not allowed:
        return True
    return scope in allowed


def _parse_csv_set(raw: str | None) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def _ref_area_is_validated(ref_area: str, template: dict[str, str]) -> bool:
    allowed = _parse_csv_set(template.get("enabled_ref_areas"))
    blocked = _parse_csv_set(template.get("disabled_ref_areas"))
    if allowed and ref_area not in allowed:
        return False
    if blocked and ref_area in blocked:
        return False
    return True


def _country_class_is_enabled(country_class: str, template: dict[str, str]) -> bool:
    allowed = _parse_csv_set(template.get("enabled_country_classes"))
    if not allowed:
        return True
    return country_class in allowed


def _missing_context_keys(context: dict[str, str], template: dict[str, str]) -> list[str]:
    required = _parse_csv_set(template.get("required_context_keys"))
    missing: list[str] = []
    for key in sorted(required):
        if not str(context.get(key, "") or "").strip():
            missing.append(key)
    return missing


def _format_template_text(text: str, context: dict[str, str]) -> str:
    template = str(text or "").strip()
    if not template:
        return ""
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{key}}}", value)
    return rendered


def _oecd_short_rate_measure(ref_area: str) -> str:
    # OECD FINMARK uses a country-specific short-rate measure code for Brazil.
    if ref_area == "BRA":
        return "IRSTCI"
    return "IR3TIB"


def build_rows(
    *,
    base_registry_rows: list[dict[str, str]],
    country_rows: list[dict[str, str]],
    template_rows: list[dict[str, str]],
    include_disabled_country_rows: bool = False,
) -> list[dict[str, str]]:
    output = [normalize_base_row(row) for row in base_registry_rows]
    for country in country_rows:
        if not parse_boolish(country.get("enabled"), default=True):
            continue
        if not parse_boolish(country.get("country_pack_enabled"), default=True):
            continue
        scope = str(country.get("country_pack_scope", "") or "").strip()
        country_class = str(country.get("country_class", "") or "").strip()
        ref_area = str(country.get("oecd_ref_area") or country.get("ref_area") or "").strip()
        imf_ref_area = str(country.get("imf_ref_area") or country.get("ref_area") or "").strip()
        ref_area_lc = ref_area.lower()
        for template in template_rows:
            if not _apply_scope(scope, template.get("apply_scopes", "")):
                continue
            metric_suffix = str(template.get("metric_suffix", "") or "").strip()
            if not metric_suffix:
                raise ValueError(f"Country template row is missing metric_suffix: {template}")
            source_name = str(template.get("source_name", "") or "").strip()
            row_ref_area = ref_area
            if source_name == "imf_sdmx":
                row_ref_area = imf_ref_area
            context = {key: str(value or "").strip() for key, value in country.items()}
            context.update(
                {
                    "ref_area": row_ref_area,
                    "oecd_ref_area": ref_area,
                    "imf_ref_area": imf_ref_area,
                    "country_class": country_class,
                    "short_rate_measure": _oecd_short_rate_measure(row_ref_area),
                }
            )
            template_enabled = parse_boolish(template.get("enabled"), default=True)
            validated_for_ref_area = _ref_area_is_validated(row_ref_area, template)
            enabled_for_country_class = _country_class_is_enabled(country_class, template)
            missing_context_keys = _missing_context_keys(context, template)
            enabled = template_enabled and validated_for_ref_area and enabled_for_country_class and not missing_context_keys
            if not enabled and not include_disabled_country_rows:
                continue
            source_series_id = ""
            source_series_id_template = str(template.get("source_series_id_template", "") or "").strip()
            if enabled and source_series_id_template:
                source_series_id = _format_template_text(source_series_id_template, context)
            notes = _format_template_text(str(template.get("notes", "") or "").strip(), context)
            disabled_reasons: list[str] = []
            if template_enabled and not validated_for_ref_area:
                disabled_reasons.append(str(template.get("disabled_reason", "") or "").strip())
            if template_enabled and not enabled_for_country_class:
                disabled_reasons.append(
                    f"Excluded from the tier-1 runtime registry because country class {country_class or '<blank>'} does not use this metric in v1."
                )
            if missing_context_keys:
                disabled_reasons.append(
                    f"Excluded from the tier-1 runtime registry because required country metadata fields are missing: {', '.join(missing_context_keys)}."
                )
            disabled_reasons = [reason for reason in disabled_reasons if reason]
            if disabled_reasons:
                notes = f"{notes} {' '.join(disabled_reasons)}".strip()
            country_note = str(country.get("notes", "") or "").strip()
            if country_note:
                notes = f"{notes} Country notes: {country_note}" if notes else country_note
            try:
                source_params_json = normalize_json_text(
                    _format_template_text(str(template.get("source_params_json", "") or "").strip(), context)
                )
            except ValueError as exc:
                template_key = str(template.get("template_key", "") or "").strip() or "<unknown>"
                raise ValueError(
                    f"Country template {template_key} for {row_ref_area} has invalid source_params_json: {exc}"
                ) from exc
            output.append(
                {
                    "registry_key": f"{ref_area_lc}_{metric_suffix}_{source_name}",
                    "metric_key": f"{ref_area_lc}_{metric_suffix}",
                    "regime_block": _format_template_text(str(template.get("regime_block", "") or "").strip(), context),
                    "source_name": source_name,
                    "source_dataset": _format_template_text(str(template.get("source_dataset", "") or "").strip(), context),
                    "source_series_id": source_series_id,
                    "ref_area": row_ref_area,
                    "frequency": _format_template_text(str(template.get("frequency", "") or "").strip(), context),
                    "seasonal_adjustment": _format_template_text(str(template.get("seasonal_adjustment", "") or "").strip(), context),
                    "units": _format_template_text(str(template.get("units", "") or "").strip(), context),
                    "vintage_policy": _format_template_text(str(template.get("vintage_policy", "") or "").strip(), context),
                    "update_cadence": _format_template_text(str(template.get("update_cadence", "") or "").strip(), context),
                    "history_start_date": _format_template_text(str(template.get("history_start_date", "") or "").strip(), context),
                    "revision_window_days": _format_template_text(str(template.get("revision_window_days", "") or "").strip(), context),
                    "source_priority": _format_template_text(str(template.get("source_priority", "") or "").strip(), context),
                    "worker_hint": _format_template_text(str(template.get("worker_hint", "") or "").strip(), context),
                    "enabled": "1" if enabled else "0",
                    "source_params_json": source_params_json,
                    "notes": notes,
                }
            )
    validate_rows(output)
    return output


def normalize_base_row(row: dict[str, str]) -> dict[str, str]:
    out = {field: str(row.get(field, "") or "").strip() for field in FIELDNAMES}
    try:
        out["source_params_json"] = normalize_json_text(out["source_params_json"])
    except ValueError as exc:
        registry_key = out.get("registry_key") or "<unknown>"
        raise ValueError(f"Base registry row {registry_key} has invalid source_params_json: {exc}") from exc
    return out


def normalize_json_text(text: str) -> str:
    if not text:
        return "{}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"source_params_json is not valid JSON: {text!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError("source_params_json must be an object.")
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def validate_rows(rows: list[dict[str, str]]) -> None:
    seen_registry: set[str] = set()
    for row in rows:
        registry_key = row["registry_key"]
        if not registry_key:
            raise ValueError("registry_key is required.")
        if registry_key in seen_registry:
            raise ValueError(f"Duplicate registry_key detected: {registry_key}")
        seen_registry.add(registry_key)


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    rows = build_rows(
        base_registry_rows=_read_rows(args.base_registry),
        country_rows=_read_rows(args.country_metadata),
        template_rows=_read_rows(args.country_templates),
        include_disabled_country_rows=args.include_disabled_country_rows,
    )
    write_rows(args.output, rows)
    logger.info("Wrote %d registry rows to: %s", len(rows), args.output)


if __name__ == "__main__":
    main()
