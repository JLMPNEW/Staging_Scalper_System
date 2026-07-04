"""Shared scoring-eligibility policy loader/resolver (EL-1 / EL-2).

Single implementation used by both the eligibility validator (scripts/10) and
the shadow rank-table publisher (defense/scripts/17) so the two components can
never again disagree about the same policy CSV.

No-catch-all rule
-----------------
``resolve_policy`` resolves exactly two tiers: the exact
``(reporting_profile, development_stage)`` key, then the
``(reporting_profile, 'any')`` key, then returns ``None``. There is
deliberately NO global fallback tier (the legacy
``('NO_FINANCIALS_REVIEW', 'any')`` catch-all made the missing-policy tripwire
mathematically unreachable and silently judged tickers under the wrong
policy). A ``None`` result means the policy table has a gap; callers must
surface it loudly (collect it as missing and raise), never substitute another
profile's policy.

Point-in-time columns
---------------------
Policy CSVs may carry two optional PIT columns (med_devices convention):

- ``valid_from`` — the date (YYYY-MM-DD) from which the row is effective,
  same-day inclusive at the EVALUATION asof: a row with
  ``valid_from == asof`` is already effective. Rows with a blank
  ``valid_from`` are effective from the beginning of time. Consumers must
  pass the FEATURE/EVALUATION asof, never ``date.today()``, so historical
  rebuilds see the policy that was in force at that asof.
- ``reviewed_at`` — provenance documentation only (when the row was last
  reviewed/approved). It never affects row selection.

When more than one version of a key exists (same profile/stage, different
``valid_from``), ``load_eligibility_policy`` requires an ``asof`` and selects
the newest row whose ``valid_from`` is on or before it. Without an ``asof``,
versioned keys raise so a caller cannot accidentally load a wall-clock or
mixed view of a versioned policy table.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

PolicyRow = dict[str, str]
PolicyKey = tuple[str, str]

_ANY_STAGE = "any"


def _csv_value(row: dict[str, str | None], key: str) -> str:
    return str(row.get(key) or "").strip()


def _normalize_asof(asof: str | date | None, *, context: str) -> str | None:
    if asof is None:
        return None
    if isinstance(asof, date):
        return asof.isoformat()
    text = str(asof).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{context}: invalid asof={asof!r}; expected YYYY-MM-DD") from exc


def _normalize_valid_from(raw: str, *, context: str) -> str:
    if not raw:
        return ""
    try:
        # Canonical zero-padded form keeps lexical date comparisons sound.
        return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{context}: invalid valid_from={raw!r}; expected YYYY-MM-DD") from exc


def load_eligibility_policy(
    path: Path | str,
    *,
    asof: str | date | None = None,
) -> dict[PolicyKey, PolicyRow]:
    """Load a scoring-eligibility policy CSV keyed by (profile, stage_or_'any').

    Raises on: a missing file, a missing/blank ``reporting_profile``, duplicate
    ``(profile, stage, valid_from)`` rows, and duplicate ``(profile, stage)``
    keys when no ``asof`` is supplied to disambiguate versioned rows.

    A blank ``development_stage`` defaults to ``'any'`` (the validator's
    long-standing behavior; the publisher's silent row drop was a bug, EL-2).
    Rows are returned as plain ``dict[str, str]`` copies of the CSV columns,
    values stripped, with ``development_stage`` and ``valid_from`` normalized.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Scoring eligibility policy CSV not found: {path}")
    asof_iso = _normalize_asof(asof, context=str(path))

    versions: dict[tuple[str, str, str], tuple[int, PolicyRow]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, raw_row in enumerate(reader, start=2):
            profile = _csv_value(raw_row, "reporting_profile")
            if not profile:
                raise ValueError(f"{path}:{line_number} missing reporting_profile")
            stage = _csv_value(raw_row, "development_stage") or _ANY_STAGE
            valid_from = _normalize_valid_from(
                _csv_value(raw_row, "valid_from"),
                context=f"{path}:{line_number}",
            )
            row: PolicyRow = {key: _csv_value(raw_row, key) for key in raw_row if key is not None}
            row["reporting_profile"] = profile
            row["development_stage"] = stage
            row["valid_from"] = valid_from
            version_key = (profile, stage, valid_from)
            if version_key in versions:
                raise ValueError(
                    f"{path}:{line_number} duplicate policy row profile={profile} "
                    f"development_stage={stage} valid_from={valid_from or '(blank)'}"
                )
            versions[version_key] = (line_number, row)

    policies: dict[PolicyKey, PolicyRow] = {}
    chosen_valid_from: dict[PolicyKey, str] = {}
    for (profile, stage, valid_from), (line_number, row) in versions.items():
        key: PolicyKey = (profile, stage)
        if asof_iso is None:
            if key in policies:
                raise ValueError(
                    f"{path}:{line_number} duplicate policy profile={profile} development_stage={stage}: "
                    "multiple valid_from versions exist; pass the evaluation asof to "
                    "load_eligibility_policy to select the effective row"
                )
            policies[key] = row
            continue
        if valid_from and valid_from > asof_iso:
            continue  # not yet effective at the evaluation asof (same-day inclusive)
        if key not in policies or valid_from > chosen_valid_from[key]:
            policies[key] = row
            chosen_valid_from[key] = valid_from
    return policies


def resolve_policy(
    policies: dict[PolicyKey, PolicyRow],
    profile: str,
    stage: str,
) -> PolicyRow | None:
    """Resolve exact (profile, stage), then (profile, 'any'), then None.

    NO catch-all fallback tier: ``None`` means the policy table has no row for
    this combination and the caller must treat that as a loud policy-table gap
    (report it and raise), never as an implicit NO_FINANCIALS_REVIEW.
    """
    profile_key = str(profile or "").strip()
    stage_key = str(stage or "").strip() or _ANY_STAGE
    row = policies.get((profile_key, stage_key))
    if row is None and stage_key != _ANY_STAGE:
        row = policies.get((profile_key, _ANY_STAGE))
    return row
