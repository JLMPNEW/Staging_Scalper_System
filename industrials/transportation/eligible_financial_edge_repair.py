from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from statistics import mean
from typing import Mapping, Sequence

from industrials.core.oos_research import finite_float, spearman
from industrials.transportation.financial_contract import MetricDefinition
from industrials.transportation.scoring import OBSERVED_STATUSES
from industrials.transportation.surface_freight_research import (
    build_directional_metric_scores,
    metric_score_field,
)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._suppressed = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_filing_text(path: Path, *, expected_sha256: str) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = artifact_sha256(path)
    if actual.lower() != expected_sha256.strip().lower():
        raise ValueError(
            f"immutable filing hash mismatch path={path} "
            f"expected={expected_sha256} actual={actual}"
        )
    parser = _VisibleTextParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\s+", " ", " ".join(parser.parts).replace("\u00a0", " ")).strip()


def _one_distinct_number(
    text: str,
    pattern: str,
    *,
    label: str,
) -> float:
    values = {
        float(value.replace(",", ""))
        for value in re.findall(pattern, text, flags=re.IGNORECASE)
    }
    if len(values) != 1:
        raise ValueError(f"{label}: expected one distinct value, found={sorted(values)}")
    return values.pop()


def extract_chrw_interest_ttm_usd(
    *,
    annual_path: Path,
    annual_sha256: str,
    quarter_path: Path,
    quarter_sha256: str,
) -> dict[str, float]:
    """Reconstruct CHRW's 2026-Q1 TTM interest expense from pinned filings.

    The 10-K supplies FY2025 interest expense. The Q1 filing supplies Q1 2026
    interest expense and the year-over-year decrease, allowing Q1 2025 and the
    TTM roll-forward to be derived without substituting the combined
    interest-and-other line.
    """
    annual = normalized_filing_text(annual_path, expected_sha256=annual_sha256)
    quarter = normalized_filing_text(quarter_path, expected_sha256=quarter_sha256)
    annual_millions = _one_distinct_number(
        annual,
        r"\$([0-9][0-9,.]*)\s+million of interest expense",
        label="CHRW FY2025 interest expense",
    )
    # The alternation returns tuples, so normalize the populated capture.
    current_matches = re.findall(
        r"(?:\$([0-9][0-9,.]*)\s+million of interest expense|interest expense of \$([0-9][0-9,.]*)\s+million)",
        quarter,
        flags=re.IGNORECASE,
    )
    current_values = {
        float((left or right).replace(",", "")) for left, right in current_matches
    }
    if len(current_values) != 1:
        raise ValueError(
            "CHRW Q1-2026 interest expense: expected one distinct value, "
            f"found={sorted(current_values)}"
        )
    current_quarter_millions = current_values.pop()
    decrease_millions = _one_distinct_number(
        quarter,
        r"interest expense.{0,120}?decreased \$([0-9][0-9,.]*)\s+million",
        label="CHRW Q1 interest expense decrease",
    )
    prior_quarter_millions = current_quarter_millions + decrease_millions
    ttm_millions = annual_millions + current_quarter_millions - prior_quarter_millions
    if min(annual_millions, current_quarter_millions, prior_quarter_millions, ttm_millions) <= 0:
        raise ValueError("CHRW interest roll-forward produced a non-positive operand")
    return {
        "annual_interest_expense_usd": annual_millions * 1_000_000.0,
        "current_quarter_interest_expense_usd": current_quarter_millions * 1_000_000.0,
        "prior_quarter_interest_expense_usd": prior_quarter_millions * 1_000_000.0,
        "interest_expense_ttm_usd": ttm_millions * 1_000_000.0,
    }


def extract_expd_current_debt_usd(
    *,
    quarter_path: Path,
    quarter_sha256: str,
) -> float:
    text = normalized_filing_text(quarter_path, expected_sha256=quarter_sha256)
    millions = _one_distinct_number(
        text,
        r"At March 31, 2026, borrowings under these credit lines were \$([0-9][0-9,.]*)\s+million",
        label="EXPD 2026-Q1 short-term borrowings",
    )
    if millions <= 0:
        raise ValueError("EXPD disclosed borrowings must be positive")
    return millions * 1_000_000.0


def apply_metric_repairs(
    rows: Sequence[Mapping[str, object]],
    repairs: Mapping[tuple[str, str], float],
    *,
    only_when_missing: bool = True,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for source in rows:
        row = dict(source)
        ticker = str(row.get("ticker") or "").upper()
        try:
            values = json.loads(str(row.get("metric_values_json") or "{}"))
            statuses = json.loads(str(row.get("metric_status_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid metric JSON for {ticker}") from exc
        for (repair_ticker, metric_id), value in repairs.items():
            if ticker != repair_ticker.upper():
                continue
            observed = (
                str(statuses.get(metric_id) or "") in OBSERVED_STATUSES
                and finite_float(values.get(metric_id)) is not None
            )
            if only_when_missing and observed:
                continue
            values[metric_id] = float(value)
            statuses[metric_id] = "DERIVED"
        row["metric_values_json"] = json.dumps(values, sort_keys=True, separators=(",", ":"))
        row["metric_status_json"] = json.dumps(statuses, sort_keys=True, separators=(",", ":"))
        output.append(row)
    return output


def recompute_surface_generic_scores(
    rows: Sequence[Mapping[str, object]],
    *,
    definitions: Sequence[MetricDefinition],
    policy: Mapping[str, object],
    component_weights: Mapping[str, float],
) -> list[dict[str, object]]:
    scored = build_directional_metric_scores(
        rows,
        definitions=definitions,
        policy=policy,
    )
    generic = [definition for definition in definitions if not definition.specialized]
    for row in scored:
        components: dict[str, float] = {}
        for component in component_weights:
            values = [
                value
                for definition in generic
                if definition.component == component
                and definition.applies_to(
                    cohort=str(row.get("calibration_cohort") or ""),
                    industry=str(row.get("industry") or ""),
                )
                and (
                    not definition.birthdate
                    or str(row.get("asof_date") or "") >= definition.birthdate
                )
                and (
                    value := finite_float(row.get(metric_score_field(definition.metric_id)))
                )
                is not None
            ]
            if values:
                components[component] = mean(values)
                row[f"recomputed_{component}_score"] = components[component]
        weighted = [
            (components[component], float(weight))
            for component, weight in component_weights.items()
            if component in components and float(weight) > 0
        ]
        total_weight = sum(weight for _, weight in weighted)
        row["recomputed_generic_score"] = (
            sum(value * weight for value, weight in weighted) / total_weight
            if total_weight
            else None
        )
    return scored


def train_score_diagnostics(
    rows: Sequence[Mapping[str, object]],
    *,
    minimum_cross_section: int,
    top_fraction: float,
) -> dict[str, float | int | None]:
    by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        if (
            str(row.get("split") or "") == "train"
            and str(row.get("calibration_eligible_flag") or "") == "1"
            and str(row.get("outcome_available_flag") or "") == "1"
            and str(row.get("horizon_sessions") or "") == "63"
            and finite_float(row.get("recomputed_generic_score")) is not None
            and finite_float(row.get("forward_excess_return")) is not None
        ):
            by_date[str(row.get("asof_date") or "")].append(row)
    ics: list[float] = []
    top_returns: list[float] = []
    bottom_returns: list[float] = []
    for members in by_date.values():
        if len(members) < minimum_cross_section:
            continue
        ordered = sorted(
            members,
            key=lambda row: (
                -float(str(row["recomputed_generic_score"])),
                str(row.get("ticker") or ""),
            ),
        )
        cross_ic = spearman(
            [float(str(row["recomputed_generic_score"])) for row in ordered],
            [float(str(row["forward_excess_return"])) for row in ordered],
        )
        if cross_ic is not None:
            ics.append(cross_ic)
        count = max(1, math.ceil(len(ordered) * top_fraction))
        top_returns.append(mean(float(str(row["forward_excess_return"])) for row in ordered[:count]))
        bottom_returns.append(mean(float(str(row["forward_excess_return"])) for row in ordered[-count:]))
    return {
        "snapshot_count": len(ics),
        "mean_ic": mean(ics) if ics else None,
        "mean_top_excess": mean(top_returns) if top_returns else None,
        "mean_bottom_excess": mean(bottom_returns) if bottom_returns else None,
        "mean_top_bottom_spread": (
            mean(top - bottom for top, bottom in zip(top_returns, bottom_returns))
            if top_returns
            else None
        ),
    }
