from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence


ACCEPTED_STATUSES = frozenset(
    {"COVERED_ACCEPTED", "COVERED_FINANCIAL_DERIVED"}
)
USABLE_STATUSES = ACCEPTED_STATUSES | frozenset(
    {"COVERED_REVIEW_REQUIRED"}
)
DISCOVERED_STATUSES = USABLE_STATUSES | frozenset(
    {"DISCOVERED_REJECTED", "TEXT_HIT_NO_VALUE"}
)
PAIR_ACTION_STATUSES = frozenset(
    {
        "COVERED_REVIEW_REQUIRED",
        "DISCOVERED_REJECTED",
        "TEXT_HIT_NO_VALUE",
    }
)
EVENT_FORMS = frozenset({"8-K", "8-K/A", "6-K", "6-K/A"})
REGISTRATION_FORMS = frozenset(
    {
        "S-1",
        "S-1/A",
        "F-1",
        "F-1/A",
        "F-4",
        "F-4/A",
        "424B3",
        "424B4",
    }
)
MATERIAL_EVENT_ITEMS = frozenset({"1.01", "2.01", "8.01", "9.01"})


METRIC_GATE_FIELDS = (
    "run_id",
    "metric_id",
    "metric_pack",
    "source_lane",
    "active_applicable_count",
    "active_usable_count",
    "active_accepted_count",
    "active_discovered_count",
    "broad_required_count",
    "broad_usable_shortfall",
    "broad_accepted_shortfall",
    "best_usable_niche_archetype",
    "best_usable_niche_applicable_count",
    "best_usable_niche_required_count",
    "best_usable_niche_count",
    "best_usable_niche_shortfall",
    "best_accepted_niche_archetype",
    "best_accepted_niche_applicable_count",
    "best_accepted_niche_required_count",
    "best_accepted_niche_count",
    "best_accepted_niche_shortfall",
    "usable_gate_pass",
    "accepted_gate_pass",
    "minimum_usable_shortfall",
    "coverage_target_class",
    "review_priority",
    "source_search_target",
)

PAIR_QUEUE_FIELDS = (
    "queue_rank",
    "review_priority",
    "run_id",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "coverage_status",
    "coverage_target_class",
    "minimum_usable_shortfall",
    "desired_action",
    "active_pair_flag",
    "can_increase_usable_coverage",
    "text_hit_count",
    "value_candidate_count",
    "review_value_count",
    "rejected_value_count",
    "distinct_period_count",
    "recovery_class",
    "recovery_status_reason",
    "evidence_keys_json",
    "review_decision",
    "selected_evidence_key",
    "decision_reason",
    "review_notes",
    "reviewed_by",
    "reviewed_at",
)

SOURCE_CANDIDATE_FIELDS = (
    "candidate_key",
    "candidate_priority",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "submissions_items",
    "candidate_type",
    "original_decision",
    "original_reason",
    "index_path",
    "index_sha256",
    "metric_id",
    "metric_pack",
    "source_lane",
    "coverage_target_class",
    "minimum_usable_shortfall",
    "source_metric_ids",
    "candidate_basis",
    "matched_aliases",
    "matched_index_documents",
    "candidate_disposition",
    "hydration_authorized",
    "parser_authorized",
    "review_notes",
    "reviewed_by",
    "reviewed_at",
)

SOURCE_FILING_FIELDS = (
    "candidate_priority",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "submissions_items",
    "candidate_type",
    "candidate_basis",
    "target_metric_count",
    "target_metric_ids",
    "matched_aliases",
    "matched_index_documents",
    "index_path",
    "index_sha256",
    "candidate_disposition",
    "hydration_authorized",
    "parser_authorized",
)


def _status_count(
    rows: Sequence[Mapping[str, object]],
    statuses: frozenset[str],
) -> int:
    return sum(str(row.get("coverage_status") or "") in statuses for row in rows)


def _required_count(total: int, *, minimum: int, rate: float) -> int:
    if total <= 0:
        return 0
    return max(minimum, math.ceil(rate * total))


def _best_niche(
    rows: Sequence[Mapping[str, object]],
    *,
    statuses: frozenset[str],
) -> dict[str, object]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("primary_archetype") or "unknown")].append(row)
    candidates: list[dict[str, object]] = []
    for archetype, scoped in grouped.items():
        required = _required_count(len(scoped), minimum=3, rate=0.25)
        covered = _status_count(scoped, statuses)
        candidates.append(
            {
                "archetype": archetype,
                "applicable": len(scoped),
                "required": required,
                "covered": covered,
                "shortfall": max(0, required - covered),
            }
        )
    if not candidates:
        return {
            "archetype": "",
            "applicable": 0,
            "required": 0,
            "covered": 0,
            "shortfall": 0,
        }
    return min(
        candidates,
        key=lambda row: (
            int(str(row["shortfall"])),
            -int(str(row["covered"])),
            -int(str(row["applicable"])),
            str(row["archetype"]),
        ),
    )


def build_metric_gate_rows(
    coverage_rows: Sequence[Mapping[str, object]],
    *,
    near_gate_max_shortfall: int = 2,
) -> list[dict[str, object]]:
    if near_gate_max_shortfall < 1:
        raise ValueError("near_gate_max_shortfall must be at least 1")
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in coverage_rows:
        if (
            str(row.get("applicability_status") or "") == "APPLICABLE"
            and str(row.get("universe_role") or "") == "active"
        ):
            grouped[str(row["metric_id"])].append(row)
    output: list[dict[str, object]] = []
    for metric_id, scoped in sorted(grouped.items()):
        first = scoped[0]
        applicable = len(scoped)
        usable = _status_count(scoped, USABLE_STATUSES)
        accepted = _status_count(scoped, ACCEPTED_STATUSES)
        discovered = _status_count(scoped, DISCOVERED_STATUSES)
        broad_required = _required_count(
            applicable,
            minimum=5,
            rate=0.30,
        )
        broad_usable_shortfall = max(0, broad_required - usable)
        broad_accepted_shortfall = max(0, broad_required - accepted)
        niche_usable = _best_niche(scoped, statuses=USABLE_STATUSES)
        niche_accepted = _best_niche(scoped, statuses=ACCEPTED_STATUSES)
        usable_gate_pass = (
            broad_usable_shortfall == 0
            or int(str(niche_usable["shortfall"])) == 0
        )
        accepted_gate_pass = (
            broad_accepted_shortfall == 0
            or int(str(niche_accepted["shortfall"])) == 0
        )
        minimum_shortfall = min(
            broad_usable_shortfall,
            int(str(niche_usable["shortfall"])),
        )
        source_lane = str(first.get("source_lane") or "")
        if accepted_gate_pass:
            target_class = "ACCEPTED_COVERAGE_PASS"
            review_priority = 4
        elif usable_gate_pass:
            target_class = "USABLE_GATE_PASS_REVIEW_REQUIRED"
            review_priority = 1
        elif discovered == 0:
            target_class = "ZERO_ACTIVE_DISCOVERY_SOURCE_TARGET"
            review_priority = 3
        elif minimum_shortfall <= near_gate_max_shortfall:
            target_class = (
                f"NEAR_GATE_SHORTFALL_{minimum_shortfall}"
            )
            review_priority = 2
        else:
            target_class = "BELOW_GATE"
            review_priority = 5
        source_search_target = int(
            source_lane != "FIN-D"
            and (
                target_class == "ZERO_ACTIVE_DISCOVERY_SOURCE_TARGET"
                or target_class.startswith("NEAR_GATE_SHORTFALL_")
            )
        )
        output.append(
            {
                "run_id": first.get("run_id", ""),
                "metric_id": metric_id,
                "metric_pack": first.get("metric_pack", ""),
                "source_lane": source_lane,
                "active_applicable_count": applicable,
                "active_usable_count": usable,
                "active_accepted_count": accepted,
                "active_discovered_count": discovered,
                "broad_required_count": broad_required,
                "broad_usable_shortfall": broad_usable_shortfall,
                "broad_accepted_shortfall": broad_accepted_shortfall,
                "best_usable_niche_archetype": niche_usable[
                    "archetype"
                ],
                "best_usable_niche_applicable_count": niche_usable[
                    "applicable"
                ],
                "best_usable_niche_required_count": niche_usable[
                    "required"
                ],
                "best_usable_niche_count": niche_usable["covered"],
                "best_usable_niche_shortfall": niche_usable[
                    "shortfall"
                ],
                "best_accepted_niche_archetype": niche_accepted[
                    "archetype"
                ],
                "best_accepted_niche_applicable_count": niche_accepted[
                    "applicable"
                ],
                "best_accepted_niche_required_count": niche_accepted[
                    "required"
                ],
                "best_accepted_niche_count": niche_accepted["covered"],
                "best_accepted_niche_shortfall": niche_accepted[
                    "shortfall"
                ],
                "usable_gate_pass": int(usable_gate_pass),
                "accepted_gate_pass": int(accepted_gate_pass),
                "minimum_usable_shortfall": minimum_shortfall,
                "coverage_target_class": target_class,
                "review_priority": review_priority,
                "source_search_target": source_search_target,
            }
        )
    return output


def _desired_action(status: str) -> str:
    if status == "COVERED_REVIEW_REQUIRED":
        return "ADJUDICATE_EXISTING_VALUE_NO_REPARSE"
    if status == "DISCOVERED_REJECTED":
        return "AUDIT_POLICY_FALSE_NEGATIVE_NO_REPARSE"
    if status == "TEXT_HIT_NO_VALUE":
        return "RECOVER_VALUE_FROM_CACHED_DOCUMENT_TARGETED_ONLY"
    raise ValueError(f"Unsupported pair action status={status!r}")


def build_pair_review_queue(
    coverage_rows: Sequence[Mapping[str, str]],
    gate_rows: Sequence[Mapping[str, object]],
    recovery_rows: Sequence[Mapping[str, str]],
) -> list[dict[str, object]]:
    gates = {str(row["metric_id"]): row for row in gate_rows}
    recovery = {
        (str(row.get("ticker") or ""), str(row.get("metric_name") or "")): row
        for row in recovery_rows
    }
    records: list[dict[str, object]] = []
    for row in coverage_rows:
        status = str(row.get("coverage_status") or "")
        if status not in PAIR_ACTION_STATUSES:
            continue
        metric_id = str(row["metric_id"])
        gate = gates[metric_id]
        active = str(row.get("universe_role") or "") == "active"
        target_class = str(gate["coverage_target_class"])
        if (
            status == "COVERED_REVIEW_REQUIRED"
            and target_class == "USABLE_GATE_PASS_REVIEW_REQUIRED"
        ):
            priority = 1
        elif (
            status in {"DISCOVERED_REJECTED", "TEXT_HIT_NO_VALUE"}
            and target_class.startswith("NEAR_GATE_SHORTFALL_")
            and active
        ):
            priority = 2
        elif target_class in {
            "USABLE_GATE_PASS_REVIEW_REQUIRED",
            "ZERO_ACTIVE_DISCOVERY_SOURCE_TARGET",
        } or target_class.startswith("NEAR_GATE_SHORTFALL_"):
            priority = 3
        else:
            priority = 4
        recovered = recovery.get(
            (str(row.get("ticker") or ""), metric_id),
            {},
        )
        records.append(
            {
                "queue_rank": 0,
                "review_priority": priority,
                "run_id": row.get("run_id", ""),
                "ticker": row.get("ticker", ""),
                "universe_role": row.get("universe_role", ""),
                "calibration_cohort": row.get(
                    "calibration_cohort",
                    "",
                ),
                "primary_archetype": row.get(
                    "primary_archetype",
                    "",
                ),
                "metric_id": metric_id,
                "metric_pack": row.get("metric_pack", ""),
                "source_lane": row.get("source_lane", ""),
                "coverage_status": status,
                "coverage_target_class": target_class,
                "minimum_usable_shortfall": gate[
                    "minimum_usable_shortfall"
                ],
                "desired_action": _desired_action(status),
                "active_pair_flag": int(active),
                "can_increase_usable_coverage": int(
                    active
                    and status
                    in {"DISCOVERED_REJECTED", "TEXT_HIT_NO_VALUE"}
                ),
                "text_hit_count": row.get("text_hit_count", "0"),
                "value_candidate_count": row.get(
                    "value_candidate_count",
                    "0",
                ),
                "review_value_count": row.get(
                    "review_value_count",
                    "0",
                ),
                "rejected_value_count": row.get(
                    "rejected_value_count",
                    "0",
                ),
                "distinct_period_count": row.get(
                    "distinct_period_count",
                    "0",
                ),
                "recovery_class": recovered.get("recovery_class", ""),
                "recovery_status_reason": recovered.get(
                    "status_reason",
                    "",
                ),
                "evidence_keys_json": recovered.get(
                    "evidence_keys_json",
                    "[]",
                ),
                "review_decision": "",
                "selected_evidence_key": "",
                "decision_reason": "",
                "review_notes": "",
                "reviewed_by": "",
                "reviewed_at": "",
            }
        )
    records.sort(
        key=lambda row: (
            int(str(row["review_priority"])),
            -int(str(row["active_pair_flag"])),
            int(str(row["minimum_usable_shortfall"])),
            str(row["metric_id"]),
            str(row["ticker"]),
        )
    )
    for rank, row in enumerate(records, start=1):
        row["queue_rank"] = rank
    return records


def _index_items(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = ((payload.get("directory") or {}).get("item") or [])
    output: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        output.append(
            {
                "name": name,
                "type": str(
                    item.get("type")
                    or item.get("document_type")
                    or ""
                ).strip(),
                "description": str(
                    item.get("description")
                    or item.get("title")
                    or ""
                ).strip(),
            }
        )
    return output


def _alias_pattern(alias: str) -> re.Pattern[str] | None:
    tokens = re.findall(r"[A-Za-z0-9]+", alias)
    if not tokens:
        return None
    if len(tokens) == 1 and len(tokens[0]) <= 4:
        pattern = rf"\b{re.escape(tokens[0])}\b"
    else:
        pattern = (
            r"\b"
            + r"[\s/_-]*".join(re.escape(token) for token in tokens)
            + r"\b"
        )
    return re.compile(pattern, re.IGNORECASE)


def _metric_sources(
    metric_id: str,
    *,
    source_lane: str,
    aliases: Mapping[str, Sequence[str]],
    derived_dependencies: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    if source_lane == "DP":
        return (metric_id,) if metric_id in aliases else ()
    if source_lane == "DP-D":
        return tuple(derived_dependencies.get(metric_id, ()))
    return ()


def _material_event_item(items: str) -> bool:
    values = {
        value.strip()
        for value in str(items or "").split(",")
        if value.strip()
    }
    return bool(values & MATERIAL_EVENT_ITEMS)


def screen_cached_source_candidates(
    *,
    decisions: Sequence[Mapping[str, str]],
    coverage_rows: Sequence[Mapping[str, str]],
    gate_rows: Sequence[Mapping[str, object]],
    cache_dir: Path,
    aliases: Mapping[str, Sequence[str]],
    derived_dependencies: Mapping[str, Sequence[str]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    gates = {str(row["metric_id"]): row for row in gate_rows}
    targets_by_ticker: dict[str, list[Mapping[str, str]]] = defaultdict(
        list
    )
    for row in coverage_rows:
        gate = gates.get(str(row.get("metric_id") or ""))
        if (
            str(row.get("applicability_status") or "") == "APPLICABLE"
            and gate is not None
            and int(str(gate["source_search_target"])) == 1
        ):
            targets_by_ticker[str(row["ticker"])].append(row)
    output: list[dict[str, object]] = []
    counters = {
        "eligible_cached_excluded_filings": 0,
        "invalid_or_missing_index_count": 0,
        "metadata_alias_match_rows": 0,
        "material_event_item_review_rows": 0,
        "development_registration_review_rows": 0,
    }
    for decision in decisions:
        form = str(decision.get("form_type") or "").upper()
        candidate_type = str(decision.get("candidate_type") or "")
        if (
            str(decision.get("decision") or "") == "INCLUDE"
            or str(decision.get("index_status") or "") != "CACHED"
            or (
                form not in EVENT_FORMS
                and form not in REGISTRATION_FORMS
            )
        ):
            continue
        ticker = str(decision.get("ticker") or "").upper()
        targets = targets_by_ticker.get(ticker, [])
        if not targets:
            continue
        counters["eligible_cached_excluded_filings"] += 1
        cik = re.sub(r"\D", "", str(decision.get("cik") or "")).zfill(
            10
        )
        accession = str(decision.get("accession_number") or "")
        index_path = (
            cache_dir
            / "sec_archive_xbrl"
            / f"CIK{cik}"
            / accession.replace("-", "")
            / "index.json"
        )
        items = _index_items(index_path)
        if not items:
            counters["invalid_or_missing_index_count"] += 1
            continue
        index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
        for target in targets:
            metric_id = str(target["metric_id"])
            gate = gates[metric_id]
            source_metric_ids = _metric_sources(
                metric_id,
                source_lane=str(target.get("source_lane") or ""),
                aliases=aliases,
                derived_dependencies=derived_dependencies,
            )
            matched_aliases: set[str] = set()
            matched_documents: set[str] = set()
            for source_metric in source_metric_ids:
                for alias in aliases.get(source_metric, ()):
                    pattern = _alias_pattern(str(alias))
                    if pattern is None:
                        continue
                    for item in items:
                        text = " ".join(
                            (
                                item["name"],
                                item["type"],
                                item["description"],
                            )
                        )
                        if pattern.search(text):
                            matched_aliases.add(str(alias))
                            matched_documents.add(item["name"])
            basis = ""
            if matched_aliases:
                basis = "CACHED_INDEX_METRIC_ALIAS"
                counters["metadata_alias_match_rows"] += 1
            elif (
                form in {"8-K", "8-K/A"}
                and _material_event_item(
                    str(decision.get("submissions_items") or "")
                )
            ):
                basis = "MATERIAL_EVENT_ITEM_REVIEW"
                counters["material_event_item_review_rows"] += 1
            elif (
                form in REGISTRATION_FORMS
                and str(target.get("metric_pack") or "") == "development"
            ):
                basis = "DEVELOPMENT_REGISTRATION_REVIEW"
                counters[
                    "development_registration_review_rows"
                ] += 1
            if not basis:
                continue
            priority = (
                1
                if str(gate["coverage_target_class"])
                == "NEAR_GATE_SHORTFALL_1"
                else 2
                if str(gate["coverage_target_class"]).startswith(
                    "NEAR_GATE_SHORTFALL_"
                )
                else 3
            )
            key_payload = "\x1f".join(
                (ticker, accession, metric_id, basis)
            )
            output.append(
                {
                    "candidate_key": hashlib.sha256(
                        key_payload.encode("utf-8")
                    ).hexdigest(),
                    "candidate_priority": priority,
                    "ticker": ticker,
                    "cik": cik,
                    "accession_number": accession,
                    "form_type": form,
                    "filing_date": decision.get("filing_date", ""),
                    "submissions_items": decision.get(
                        "submissions_items",
                        "",
                    ),
                    "candidate_type": candidate_type,
                    "original_decision": decision.get("decision", ""),
                    "original_reason": decision.get("reason", ""),
                    "index_path": str(index_path.resolve()),
                    "index_sha256": index_hash,
                    "metric_id": metric_id,
                    "metric_pack": target.get("metric_pack", ""),
                    "source_lane": target.get("source_lane", ""),
                    "coverage_target_class": gate[
                        "coverage_target_class"
                    ],
                    "minimum_usable_shortfall": gate[
                        "minimum_usable_shortfall"
                    ],
                    "source_metric_ids": "|".join(source_metric_ids),
                    "candidate_basis": basis,
                    "matched_aliases": "|".join(
                        sorted(matched_aliases)
                    ),
                    "matched_index_documents": "|".join(
                        sorted(matched_documents)
                    ),
                    "candidate_disposition": "PENDING_REVIEW",
                    "hydration_authorized": 0,
                    "parser_authorized": 0,
                    "review_notes": "",
                    "reviewed_by": "",
                    "reviewed_at": "",
                }
            )
    output.sort(
        key=lambda row: (
            int(str(row["candidate_priority"])),
            str(row["metric_id"]),
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
        )
    )
    return output, counters


def build_source_filing_rows(
    rows: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[
        tuple[str, str, str],
        list[Mapping[str, object]],
    ] = defaultdict(list)
    for row in rows:
        grouped[
            (
                str(row["ticker"]),
                str(row["accession_number"]),
                str(row["candidate_basis"]),
            )
        ].append(row)
    output: list[dict[str, object]] = []
    for _, scoped in sorted(grouped.items()):
        first = scoped[0]
        output.append(
            {
                "candidate_priority": min(
                    int(str(row["candidate_priority"])) for row in scoped
                ),
                "ticker": first["ticker"],
                "cik": first["cik"],
                "accession_number": first["accession_number"],
                "form_type": first["form_type"],
                "filing_date": first["filing_date"],
                "submissions_items": first["submissions_items"],
                "candidate_type": first["candidate_type"],
                "candidate_basis": first["candidate_basis"],
                "target_metric_count": len(
                    {str(row["metric_id"]) for row in scoped}
                ),
                "target_metric_ids": "|".join(
                    sorted({str(row["metric_id"]) for row in scoped})
                ),
                "matched_aliases": "|".join(
                    sorted(
                        {
                            alias
                            for row in scoped
                            for alias in str(
                                row["matched_aliases"]
                            ).split("|")
                            if alias
                        }
                    )
                ),
                "matched_index_documents": "|".join(
                    sorted(
                        {
                            name
                            for row in scoped
                            for name in str(
                                row["matched_index_documents"]
                            ).split("|")
                            if name
                        }
                    )
                ),
                "index_path": first["index_path"],
                "index_sha256": first["index_sha256"],
                "candidate_disposition": "PENDING_REVIEW",
                "hydration_authorized": 0,
                "parser_authorized": 0,
            }
        )
    output.sort(
        key=lambda row: (
            int(str(row["candidate_priority"])),
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
            str(row["candidate_basis"]),
        )
    )
    return output
