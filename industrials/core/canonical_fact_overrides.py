from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path


ACCEPTED = "ACCEPTED"
PREFERRED_CONCEPT_PRIORITY = -1_000_000
REQUIRED_FIELDS = (
    "model_family",
    "ticker",
    "canonical_metric",
    "taxonomy",
    "concept_name",
    "valid_from",
    "valid_to",
    "review_status",
    "reason",
)


@dataclass(frozen=True)
class CanonicalConceptOverride:
    model_family: str
    ticker: str
    canonical_metric: str
    taxonomy: str
    concept_name: str
    valid_from: date
    valid_to: date | None
    reason: str

    def matches(self, *, taxonomy: str, concept_name: str) -> bool:
        return (
            taxonomy.strip().lower() == self.taxonomy.lower()
            and concept_name.strip() == self.concept_name
        )


def _parse_date(value: str, *, field: str, path: Path, row_number: int) -> date:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ValueError(
            f"{path}:{row_number}: invalid {field}={value!r}; expected YYYY-MM-DD"
        ) from exc


def load_canonical_concept_overrides(
    path: Path | None,
    *,
    model_family: str,
    asof: date,
) -> dict[tuple[str, str], CanonicalConceptOverride]:
    """Load active, reviewed issuer-specific canonical concept preferences.

    These preferences resolve within-accession concept ambiguity. They never
    create facts, change the global concept map, or affect another family.
    """
    if path is None:
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in REQUIRED_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing required columns={missing}")
        selected: dict[tuple[str, str], CanonicalConceptOverride] = {}
        for row_number, raw in enumerate(reader, start=2):
            row_family = str(raw.get("model_family") or "").strip()
            if row_family != model_family:
                continue
            status = str(raw.get("review_status") or "").strip().upper()
            if status != ACCEPTED:
                continue
            valid_from = _parse_date(
                str(raw.get("valid_from") or ""),
                field="valid_from",
                path=path,
                row_number=row_number,
            )
            valid_to_text = str(raw.get("valid_to") or "").strip()
            valid_to = (
                _parse_date(
                    valid_to_text,
                    field="valid_to",
                    path=path,
                    row_number=row_number,
                )
                if valid_to_text
                else None
            )
            if valid_to is not None and valid_to < valid_from:
                raise ValueError(f"{path}:{row_number}: valid_to precedes valid_from")
            if asof < valid_from or (valid_to is not None and asof > valid_to):
                continue
            ticker = str(raw.get("ticker") or "").strip().upper()
            metric = str(raw.get("canonical_metric") or "").strip()
            taxonomy = str(raw.get("taxonomy") or "").strip()
            concept_name = str(raw.get("concept_name") or "").strip()
            reason = str(raw.get("reason") or "").strip()
            if not all((ticker, metric, taxonomy, concept_name, reason)):
                raise ValueError(f"{path}:{row_number}: active override has blank fields")
            key = (ticker, metric)
            if key in selected:
                raise ValueError(f"{path}:{row_number}: duplicate active override={key}")
            selected[key] = CanonicalConceptOverride(
                model_family=row_family,
                ticker=ticker,
                canonical_metric=metric,
                taxonomy=taxonomy,
                concept_name=concept_name,
                valid_from=valid_from,
                valid_to=valid_to,
                reason=reason,
            )
    return selected


def canonical_selection_priority(
    source_priority: int,
    *,
    override: CanonicalConceptOverride | None,
    taxonomy: str,
    concept_name: str,
) -> int:
    if override is not None and override.matches(
        taxonomy=taxonomy,
        concept_name=concept_name,
    ):
        return PREFERRED_CONCEPT_PRIORITY
    return int(source_priority)
