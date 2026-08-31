"""Strict prospective signal-capture and outcome-evaluation primitives.

The protocol has only two evidence-bearing states:

``captured_pending_outcomes``
    An externally anchored signal snapshot that contains no revealed outcome.
``evaluated_future_only``
    A later evaluation that binds exact outcome bytes to one or more captures.

Retrospective diagnostics are deliberately not accepted as inputs.  None of
the payloads produced here authorize Portfolio Layer writes; a separate,
independent promotion decision must consume a passing evaluation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .canonical_values import exact_date, exact_utc


TrustedReceiptVerifier = Callable[[Path, str, Mapping[str, Any]], bool]
_HEX = frozenset("0123456789abcdef")
_FORBIDDEN_SIGNAL_TOKENS = (
    "forward_",
    "future_",
    "outcome",
    "realized",
    "gross_return",
    "net_return",
    "exit_date",
    "exit_price",
    "target_",
)
_REQUIRED_SIGNAL_FIELDS = frozenset(
    {
        "ticker",
        "sleeve_id",
        "group_id",
        "score",
        "rank",
        "ranking_mode",
        "eligible_flag",
        "selected_top_flag",
        "selected_bottom_flag",
    }
)
_REQUIRED_OUTCOME_FIELDS = frozenset(
    {
        "capture_id",
        "ticker",
        "sleeve_id",
        "group_id",
        "horizon_sessions",
        "entry_date",
        "exit_date",
        "gross_return",
        "membership_status",
        "terminal_event_status",
        "outcome_available_at_utc",
    }
)


@dataclass(frozen=True)
class FutureEvidencePolicy:
    """Immutable acceptance policy used by the generic evaluator."""

    family: str
    policy_id: str
    effective_from: date
    first_signal_date: date
    horizons: tuple[int, ...]
    minimum_counts: Mapping[int, int]
    minimum_ic: float
    minimum_top_minus_cohort: float
    minimum_top_minus_bottom: float
    minimum_hit_rate: float
    transaction_cost_bps: float
    minimum_cross_sections: Mapping[str, int]
    require_group_pass: bool = True
    top_minus_bottom_basis: str = "net"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def exact_sha256(value: Any, *, label: str = "sha256") -> str:
    text = str(value).strip().lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{label} must be exact 64-hex SHA-256")
    return text


def immutable_write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically create a JSON artifact and refuse every overwrite."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"immutable artifact already exists: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link creation is exclusive even if another process wins the race.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _iso_date(value: Any, *, label: str) -> date:
    return exact_date(value, label=label)


def _artifact_identity(path: Path, *, role: str) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{role} is missing: {resolved}")
    return {
        "role": role,
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": file_sha256(resolved),
    }


def _trusted_receipt(
    *,
    path: Path,
    expected_sha256: str,
    verifier: TrustedReceiptVerifier | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if verifier is None:
        raise ValueError("an independent trusted receipt verifier is required")
    identity = _artifact_identity(path, role="trusted_capture_receipt")
    expected = exact_sha256(expected_sha256, label="trusted receipt sha256")
    if identity["sha256"] != expected:
        raise ValueError("trusted capture receipt SHA-256 mismatch")
    payload = json.loads(Path(identity["path"]).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("trusted capture receipt must be a JSON object")
    if verifier(Path(identity["path"]), expected, payload) is not True:
        raise ValueError("trusted capture receipt failed independent verification")
    return dict(payload), identity


def _no_outcome_fields(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        for key in row:
            normalized = str(key).strip().lower()
            if any(token in normalized for token in _FORBIDDEN_SIGNAL_TOKENS):
                raise ValueError(f"outcome/revealed field is forbidden at capture: {key}")


def _validate_signal_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    asof_date: str,
) -> list[dict[str, Any]]:
    if not rows:
        raise ValueError("signal capture requires at least one row")
    _no_outcome_fields(rows)
    clean: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw in rows:
        missing = _REQUIRED_SIGNAL_FIELDS - set(raw)
        if missing:
            raise ValueError(f"signal row missing fields={sorted(missing)}")
        row = dict(raw)
        if "asof_date" in row and _iso_date(
            row["asof_date"], label="signal row asof_date"
        ) != _iso_date(asof_date, label="capture asof_date"):
            raise ValueError("signal row asof_date differs from capture asof_date")
        ticker = str(row["ticker"]).strip().upper()
        sleeve = str(row["sleeve_id"]).strip()
        group = str(row["group_id"]).strip()
        if not ticker or not sleeve or not group:
            raise ValueError("signal identity fields cannot be blank")
        key = ticker
        if key in keys:
            raise ValueError(f"duplicate signal identity={key}")
        keys.add(key)
        score = float(row["score"])
        rank = float(row["rank"])
        if not math.isfinite(score) or not math.isfinite(rank):
            raise ValueError("signal score and rank must be finite")
        flags = {
            name: int(row[name])
            for name in ("eligible_flag", "selected_top_flag", "selected_bottom_flag")
        }
        if any(value not in (0, 1) for value in flags.values()):
            raise ValueError("signal flags must be strict 0/1")
        if flags["selected_top_flag"] and flags["selected_bottom_flag"]:
            raise ValueError("one signal row cannot be both top and bottom")
        if (flags["selected_top_flag"] or flags["selected_bottom_flag"]) and not flags["eligible_flag"]:
            raise ValueError("an ineligible signal cannot be selected")
        normalized = {
            **row,
            "ticker": ticker,
            "sleeve_id": sleeve,
            "group_id": group,
            "score": score,
            "rank": rank,
            **flags,
        }
        normalized["signal_row_sha256"] = canonical_sha256(normalized)
        clean.append(normalized)
    return sorted(clean, key=lambda row: (row["sleeve_id"], row["group_id"], row["ticker"]))


def build_capture_payload(
    *,
    policy: FutureEvidencePolicy,
    asof_date: str,
    capture_date: str,
    signal_rows: Sequence[Mapping[str, Any]],
    source_paths: Mapping[str, Path],
    expected_source_sha256: Mapping[str, str],
    required_source_roles: Iterable[str],
    trusted_receipt_path: Path,
    expected_trusted_receipt_sha256: str,
    trusted_receipt_verifier: TrustedReceiptVerifier | None,
) -> dict[str, Any]:
    """Build a hash-bound prospective capture after verifying an external receipt."""

    asof = _iso_date(asof_date, label="asof_date")
    captured_on = _iso_date(capture_date, label="capture_date")
    if captured_on != asof:
        raise ValueError("capture_date must exactly equal asof_date")
    if asof < policy.effective_from or asof < policy.first_signal_date:
        raise ValueError("pre-effective/pre-first-signal artifacts cannot start the future clock")
    roles = set(required_source_roles)
    if set(source_paths) != roles or set(expected_source_sha256) != roles:
        raise ValueError("source roles must exactly match the protocol contract")
    identities: dict[str, dict[str, Any]] = {}
    for role in sorted(roles):
        identity = _artifact_identity(source_paths[role], role=role)
        expected = exact_sha256(expected_source_sha256[role], label=f"{role} sha256")
        if identity["sha256"] != expected:
            raise ValueError(f"{role} SHA-256 mismatch")
        identities[role] = identity
    rows = _validate_signal_rows(signal_rows, asof_date=asof.isoformat())
    receipt, receipt_identity = _trusted_receipt(
        path=trusted_receipt_path,
        expected_sha256=expected_trusted_receipt_sha256,
        verifier=trusted_receipt_verifier,
    )
    if str(receipt.get("schema_version")) != "future_signal_capture_receipt_v1":
        raise ValueError("unsupported trusted capture receipt schema")
    if str(receipt.get("family")) != policy.family:
        raise ValueError("trusted capture receipt family mismatch")
    if str(receipt.get("asof_date")) != asof.isoformat() or str(receipt.get("capture_date")) != asof.isoformat():
        raise ValueError("trusted capture receipt does not bind capture/asof identity")
    receipt_time = _utc(receipt.get("captured_at_utc"), label="captured_at_utc")
    if receipt_time.date() not in {asof, date.fromordinal(asof.toordinal() + 1)}:
        raise ValueError("trusted receipt is not contemporaneous with the signal date")
    receipt_hashes = receipt.get("source_sha256")
    if not isinstance(receipt_hashes, dict) or receipt_hashes != {
        role: identity["sha256"] for role, identity in identities.items()
    }:
        raise ValueError("trusted capture receipt does not bind exact source bytes")
    signal_rows_sha256 = canonical_sha256(rows)
    if str(receipt.get("signal_rows_sha256")) != signal_rows_sha256:
        raise ValueError("trusted capture receipt does not bind exact signal rows")
    body: dict[str, Any] = {
        "schema_version": "future_only_signal_capture_v1",
        "state": "captured_pending_outcomes",
        "evidence_class": "prospective_future_only",
        "family": policy.family,
        "policy_id": policy.policy_id,
        "policy_effective_from": policy.effective_from.isoformat(),
        "first_signal_date": policy.first_signal_date.isoformat(),
        "asof_date": asof.isoformat(),
        "capture_date": captured_on.isoformat(),
        "captured_at_utc": receipt_time.isoformat(),
        "signal_rows": rows,
        "signal_rows_sha256": signal_rows_sha256,
        "source_identities": identities,
        "trusted_receipt": receipt_identity,
        "outcomes_present_at_capture": False,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    body["capture_id"] = canonical_sha256(body)
    body["payload_sha256"] = canonical_sha256(body)
    return body


def validate_capture_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    capture = dict(payload)
    if capture.get("schema_version") != "future_only_signal_capture_v1":
        raise ValueError("unsupported capture schema")
    if capture.get("state") != "captured_pending_outcomes":
        raise ValueError("capture is not pending future outcomes")
    if capture.get("evidence_class") != "prospective_future_only":
        raise ValueError("historical/revealed evidence cannot satisfy future gates")
    for field in (
        "outcomes_present_at_capture",
        "historical_results_can_authorize_production",
        "production_activation_authorized",
        "portfolio_write_enabled",
    ):
        if capture.get(field) is not False:
            raise ValueError(f"capture fail-closed field is not false: {field}")
    if float(capture.get("optimizer_cap") or 0.0) != 0.0:
        raise ValueError("capture optimizer cap must remain zero")
    expected_payload_hash = capture.pop("payload_sha256", None)
    if canonical_sha256(capture) != exact_sha256(expected_payload_hash, label="capture payload_sha256"):
        raise ValueError("capture payload hash mismatch")
    # Reinsert only after validating the self-excluding digest.
    capture["payload_sha256"] = expected_payload_hash
    expected_capture_id = capture["capture_id"]
    id_body = dict(capture)
    id_body.pop("payload_sha256")
    id_body.pop("capture_id")
    if canonical_sha256(id_body) != exact_sha256(expected_capture_id, label="capture_id"):
        raise ValueError("capture id mismatch")
    rows = capture.get("signal_rows")
    if not isinstance(rows, list):
        raise ValueError("capture signal_rows must be a list")
    if canonical_sha256(rows) != exact_sha256(capture.get("signal_rows_sha256"), label="signal_rows_sha256"):
        raise ValueError("capture signal-row hash mismatch")
    _validate_signal_rows(rows, asof_date=str(capture["asof_date"]))
    return capture


def _average_ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position + 1
        while end < len(indexed) and indexed[end][1] == indexed[position][1]:
            end += 1
        average = (position + 1 + end) / 2.0
        for original_index, _ in indexed[position:end]:
            ranks[original_index] = average
        position = end
    return ranks


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left) * sum((y - right_mean) ** 2 for y in right)
    )
    if denominator == 0:
        return None
    return numerator / denominator


def _spearman(scores: Sequence[float], returns: Sequence[float]) -> float | None:
    return _correlation(_average_ranks(scores), _average_ranks(returns))


def _mean(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return statistics.fmean(clean) if clean else None


def _turnover(previous: set[str] | None, current: set[str]) -> float:
    if not current:
        raise ValueError("selected portfolio cannot be empty")
    if previous is None:
        return 1.0
    tickers = previous | current
    previous_weight = 1.0 / len(previous) if previous else 0.0
    current_weight = 1.0 / len(current)
    return 0.5 * sum(
        abs((previous_weight if ticker in previous else 0.0) - (current_weight if ticker in current else 0.0))
        for ticker in tickers
    )


def _nonoverlap(periods: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_exit: date | None = None
    for period in sorted(periods, key=lambda row: (row["entry_date"], row["exit_date"], row["capture_id"])):
        entry = _iso_date(period["entry_date"], label="entry_date")
        exit_date = _iso_date(period["exit_date"], label="exit_date")
        if exit_date <= entry:
            raise ValueError("outcome exit_date must be after entry_date")
        if last_exit is None or entry >= last_exit:
            selected.append(period)
            last_exit = exit_date
    return selected


def _period_metric(
    signal_rows: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    outcome_by_ticker = {str(row["ticker"]): row for row in outcome_rows}
    eligible = [row for row in signal_rows if int(row["eligible_flag"]) == 1]
    if set(outcome_by_ticker) != {str(row["ticker"]) for row in eligible}:
        raise ValueError("outcome ticker census does not exactly match captured eligible signals")
    returns = {ticker: float(row["gross_return"]) for ticker, row in outcome_by_ticker.items()}
    if any(not math.isfinite(value) for value in returns.values()):
        raise ValueError("outcome returns must be finite")
    top = {str(row["ticker"]) for row in eligible if int(row["selected_top_flag"]) == 1}
    bottom = {str(row["ticker"]) for row in eligible if int(row["selected_bottom_flag"]) == 1}
    if not top or not bottom:
        raise ValueError("each ranked period requires non-empty top and bottom selections")
    scores = [float(row["score"]) for row in eligible]
    realized = [returns[str(row["ticker"])] for row in eligible]
    return {
        "cross_section": len(eligible),
        "ic": _spearman(scores, realized),
        "top_gross": statistics.fmean(returns[ticker] for ticker in top),
        "bottom_gross": statistics.fmean(returns[ticker] for ticker in bottom),
        "cohort_gross": statistics.fmean(returns.values()),
        "top_tickers": sorted(top),
        "bottom_tickers": sorted(bottom),
        "cohort_tickers": sorted(returns),
    }


def _costed_metrics(
    periods: Sequence[dict[str, Any]],
    *,
    transaction_cost_bps: float,
) -> list[dict[str, Any]]:
    cost_rate = transaction_cost_bps / 10_000.0
    result: list[dict[str, Any]] = []
    previous: dict[str, set[str]] | None = None
    previous_exit: date | None = None
    for index, period in enumerate(periods):
        entry = _iso_date(period["entry_date"], label="entry_date")
        gap = previous_exit is not None and entry > previous_exit
        if gap and result:
            result[-1]["top_exit_turnover"] = 1.0
            result[-1]["bottom_exit_turnover"] = 1.0
            result[-1]["cohort_exit_turnover"] = 1.0
        if previous is None or gap:
            top_entry = bottom_entry = cohort_entry = 1.0
        else:
            top_entry = _turnover(previous["top"], set(period["top_tickers"]))
            bottom_entry = _turnover(previous["bottom"], set(period["bottom_tickers"]))
            cohort_entry = _turnover(previous["cohort"], set(period["cohort_tickers"]))
        row = {
            **period,
            "top_entry_turnover": top_entry,
            "bottom_entry_turnover": bottom_entry,
            "cohort_entry_turnover": cohort_entry,
            "top_exit_turnover": 0.0,
            "bottom_exit_turnover": 0.0,
            "cohort_exit_turnover": 0.0,
        }
        result.append(row)
        previous = {
            "top": set(period["top_tickers"]),
            "bottom": set(period["bottom_tickers"]),
            "cohort": set(period["cohort_tickers"]),
        }
        previous_exit = _iso_date(period["exit_date"], label="exit_date")
    if result:
        result[-1]["top_exit_turnover"] = 1.0
        result[-1]["bottom_exit_turnover"] = 1.0
        result[-1]["cohort_exit_turnover"] = 1.0
    for row in result:
        row["top_net"] = row["top_gross"] - cost_rate * (
            row["top_entry_turnover"] + row["top_exit_turnover"]
        )
        row["bottom_net"] = row["bottom_gross"] - cost_rate * (
            row["bottom_entry_turnover"] + row["bottom_exit_turnover"]
        )
        row["cohort_net"] = row["cohort_gross"] - cost_rate * (
            row["cohort_entry_turnover"] + row["cohort_exit_turnover"]
        )
        row["top_minus_cohort_net"] = (
            row["top_gross"]
            - row["cohort_gross"]
            - cost_rate
            * (row["top_entry_turnover"] + row["top_exit_turnover"])
        )
        row["top_minus_bottom_gross"] = row["top_gross"] - row["bottom_gross"]
        row["top_minus_bottom_net"] = (
            row["top_gross"]
            - row["bottom_gross"]
            - cost_rate
            * (
                row["top_entry_turnover"]
                + row["top_exit_turnover"]
                + row["bottom_entry_turnover"]
                + row["bottom_exit_turnover"]
            )
        )
    return result


def _verdict(
    periods: Sequence[dict[str, Any]],
    *,
    policy: FutureEvidencePolicy,
    horizon: int,
    minimum_cross_section: int,
) -> dict[str, Any]:
    count = len(periods)
    mean_ic = _mean(row.get("ic") for row in periods)
    top_minus_cohort = _mean(row["top_minus_cohort_net"] for row in periods)
    top_minus_bottom_gross = _mean(row["top_minus_bottom_gross"] for row in periods)
    top_minus_bottom_net = _mean(row["top_minus_bottom_net"] for row in periods)
    hit_rate = _mean(1.0 if row["top_minus_cohort_net"] > 0 else 0.0 for row in periods)
    breadth_pass = all(int(row["cross_section"]) >= minimum_cross_section for row in periods)
    gates = {
        "minimum_count_pass": count >= int(policy.minimum_counts[horizon]),
        "ic_pass": mean_ic is not None and mean_ic > policy.minimum_ic,
        "top_minus_cohort_pass": (
            top_minus_cohort is not None and top_minus_cohort > policy.minimum_top_minus_cohort
        ),
        "top_minus_bottom_pass": (
            (top_minus_bottom_gross if policy.top_minus_bottom_basis == "gross" else top_minus_bottom_net)
            is not None
            and float(
                top_minus_bottom_gross if policy.top_minus_bottom_basis == "gross" else top_minus_bottom_net
            )
            > policy.minimum_top_minus_bottom
        ),
        "hit_rate_pass": hit_rate is not None and hit_rate >= policy.minimum_hit_rate,
        "cross_section_pass": breadth_pass,
        "initial_cost_charged_pass": bool(periods) and float(periods[0]["top_entry_turnover"]) == 1.0,
        "final_cost_charged_pass": bool(periods) and float(periods[-1]["top_exit_turnover"]) == 1.0,
        "true_nonoverlap_pass": all(
            _iso_date(left["exit_date"], label="exit_date")
            <= _iso_date(right["entry_date"], label="entry_date")
            for left, right in zip(periods, periods[1:])
        ),
    }
    return {
        "horizon_sessions": horizon,
        "nonoverlapping_outcome_count": count,
        "minimum_required_count": int(policy.minimum_counts[horizon]),
        "mean_ic": mean_ic,
        "mean_top_minus_cohort_net": top_minus_cohort,
        "mean_top_minus_bottom_gross": top_minus_bottom_gross,
        "mean_top_minus_bottom_net": top_minus_bottom_net,
        "top_minus_bottom_gate_basis": policy.top_minus_bottom_basis,
        "hit_rate": hit_rate,
        "transaction_cost_bps": policy.transaction_cost_bps,
        "gates": gates,
        "pass": all(gates.values()),
        "periods": list(periods),
    }


def evaluate_future_evidence(
    *,
    policy: FutureEvidencePolicy,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    evaluation_at_utc: str,
    required_terminal_event_statuses: frozenset[str] = frozenset({"none", "cash_settled", "delisting_return_applied"}),
) -> dict[str, Any]:
    """Evaluate only matured, exact-census outcomes from trusted captures."""

    if not capture_paths:
        raise ValueError("at least one prospective capture is required")
    evaluated_at = _utc(evaluation_at_utc, label="evaluation_at_utc")
    captures: list[dict[str, Any]] = []
    capture_identities: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for path in capture_paths:
        identity = _artifact_identity(path, role="prospective_signal_capture")
        payload = json.loads(Path(identity["path"]).read_text(encoding="utf-8"))
        capture = validate_capture_payload(payload)
        if capture["family"] != policy.family or capture["policy_id"] != policy.policy_id:
            raise ValueError("capture family/policy identity mismatch")
        if capture["capture_id"] in by_id:
            raise ValueError("duplicate capture id")
        captures.append(capture)
        capture_identities.append(identity)
        by_id[capture["capture_id"]] = capture
    outcome_identity = _artifact_identity(outcome_path, role="future_outcomes")
    outcome_payload = json.loads(Path(outcome_identity["path"]).read_text(encoding="utf-8"))
    if not isinstance(outcome_payload, dict) or outcome_payload.get("schema_version") != "future_only_outcomes_v1":
        raise ValueError("unsupported outcome schema")
    if outcome_payload.get("evidence_class") != "prospective_future_only":
        raise ValueError("historical/revealed diagnostics cannot be submitted as future outcomes")
    rows = outcome_payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("future outcome rows are required")
    if canonical_sha256(rows) != exact_sha256(outcome_payload.get("rows_sha256"), label="outcome rows_sha256"):
        raise ValueError("future outcome row hash mismatch")
    expected_keys: set[tuple[str, str, int]] = set()
    signal_index: dict[tuple[str, str], dict[str, Any]] = {}
    for capture in captures:
        for signal in capture["signal_rows"]:
            if int(signal["eligible_flag"]) == 1:
                signal_index[(capture["capture_id"], signal["ticker"])] = signal
                for horizon in policy.horizons:
                    expected_keys.add((capture["capture_id"], signal["ticker"], horizon))
    seen: set[tuple[str, str, int]] = set()
    normalized_outcomes: list[dict[str, Any]] = []
    for raw in rows:
        missing = _REQUIRED_OUTCOME_FIELDS - set(raw)
        if missing:
            raise ValueError(f"outcome row missing fields={sorted(missing)}")
        row = dict(raw)
        capture_id = str(row["capture_id"])
        ticker = str(row["ticker"]).strip().upper()
        horizon = int(row["horizon_sessions"])
        key = (capture_id, ticker, horizon)
        if key not in expected_keys:
            raise ValueError(f"unexpected outcome identity={key}")
        if key in seen:
            raise ValueError(f"duplicate outcome identity={key}")
        seen.add(key)
        signal = signal_index[(capture_id, ticker)]
        if str(row["sleeve_id"]) != signal["sleeve_id"] or str(row["group_id"]) != signal["group_id"]:
            raise ValueError("outcome sleeve/group does not match captured signal")
        entry = _iso_date(row["entry_date"], label="entry_date")
        exit_date = _iso_date(row["exit_date"], label="exit_date")
        capture = by_id[capture_id]
        captured_at = _utc(capture["captured_at_utc"], label="captured_at_utc")
        available_at = _utc(row["outcome_available_at_utc"], label="outcome_available_at_utc")
        if entry < _iso_date(capture["asof_date"], label="capture asof_date"):
            raise ValueError("outcome interval starts before its signal")
        if not captured_at < available_at <= evaluated_at:
            raise ValueError("outcome chronology does not follow signal capture")
        if exit_date > available_at.date():
            raise ValueError("outcome was marked available before its exit date")
        if str(row["membership_status"]) not in {"member_at_entry", "eligible_at_entry"}:
            raise ValueError("prospective membership status is not valid at entry")
        if str(row["terminal_event_status"]) not in required_terminal_event_statuses:
            raise ValueError("terminal-event outcome is missing a governed disposition")
        normalized_outcomes.append(
            {
                **row,
                "capture_id": capture_id,
                "ticker": ticker,
                "horizon_sessions": horizon,
                "entry_date": entry.isoformat(),
                "exit_date": exit_date.isoformat(),
                "gross_return": float(row["gross_return"]),
            }
        )
    if seen != expected_keys:
        missing = sorted(expected_keys - seen)
        raise ValueError(f"outcome census is incomplete; missing={missing[:10]}")

    # Build one cross-sectional period per capture/sleeve/group and also one
    # sleeve-wide period.  Group failure is never hidden by aggregation.
    signal_by_capture = {capture["capture_id"]: capture["signal_rows"] for capture in captures}
    periods_by_scope: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for capture in captures:
        capture_id = capture["capture_id"]
        for horizon in policy.horizons:
            horizon_outcomes = [
                row for row in normalized_outcomes
                if row["capture_id"] == capture_id and row["horizon_sessions"] == horizon
            ]
            for sleeve in sorted({row["sleeve_id"] for row in signal_by_capture[capture_id]}):
                sleeve_signals = [
                    row for row in signal_by_capture[capture_id]
                    if row["sleeve_id"] == sleeve and int(row["eligible_flag"]) == 1
                ]
                sleeve_outcomes = [row for row in horizon_outcomes if row["sleeve_id"] == sleeve]
                if sleeve_signals:
                    metric = _period_metric(sleeve_signals, sleeve_outcomes)
                    metric.update(
                        capture_id=capture_id,
                        scope_kind="sleeve",
                        scope_id=sleeve,
                        sleeve_id=sleeve,
                        entry_date=sleeve_outcomes[0]["entry_date"],
                        exit_date=sleeve_outcomes[0]["exit_date"],
                    )
                    if any(
                        row["entry_date"] != metric["entry_date"] or row["exit_date"] != metric["exit_date"]
                        for row in sleeve_outcomes
                    ):
                        raise ValueError("one capture/horizon must use one exact outcome interval")
                    periods_by_scope.setdefault(("sleeve", sleeve, horizon), []).append(metric)
                for group in sorted({row["group_id"] for row in sleeve_signals}):
                    group_signals = [row for row in sleeve_signals if row["group_id"] == group]
                    if any(row["ranking_mode"] == "eligibility_equal_weight" for row in group_signals):
                        # Equal-weight eligibility groups have no rank spread and
                        # are monitored for return/cost/breadth, not IC separation.
                        continue
                    group_outcomes = [row for row in sleeve_outcomes if row["group_id"] == group]
                    metric = _period_metric(group_signals, group_outcomes)
                    metric.update(
                        capture_id=capture_id,
                        scope_kind="group",
                        scope_id=group,
                        sleeve_id=sleeve,
                        entry_date=group_outcomes[0]["entry_date"],
                        exit_date=group_outcomes[0]["exit_date"],
                    )
                    periods_by_scope.setdefault(("group", group, horizon), []).append(metric)

    scope_verdicts: list[dict[str, Any]] = []
    for (scope_kind, scope_id, horizon), periods in sorted(periods_by_scope.items()):
        selected = _nonoverlap(periods)
        costed = _costed_metrics(selected, transaction_cost_bps=policy.transaction_cost_bps)
        minimum_cross_section = int(policy.minimum_cross_sections.get(scope_id, 2))
        verdict = _verdict(
            costed,
            policy=policy,
            horizon=horizon,
            minimum_cross_section=minimum_cross_section,
        )
        scope_verdicts.append(
            {
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                **verdict,
            }
        )

    sleeve_ids = sorted({row["sleeve_id"] for row in normalized_outcomes})
    sleeve_verdicts: list[dict[str, Any]] = []
    for sleeve in sleeve_ids:
        sleeve_scopes = [
            row for row in scope_verdicts
            if row["scope_kind"] == "sleeve" and row["scope_id"] == sleeve
        ]
        group_scopes = [
            row for row in scope_verdicts
            if row["scope_kind"] == "group"
            and any(
                period["sleeve_id"] == sleeve
                for period in row["periods"]
            )
        ]
        horizons_complete = {row["horizon_sessions"] for row in sleeve_scopes} == set(policy.horizons)
        tankers_coverage = all(
            bool(capture.get("sleeve_coverage_gates", {}).get(sleeve, True))
            for capture in captures
        )
        pass_flag = (
            horizons_complete
            and all(row["pass"] for row in sleeve_scopes)
            and (not policy.require_group_pass or all(row["pass"] for row in group_scopes))
            and tankers_coverage
        )
        sleeve_verdicts.append(
            {
                "sleeve_id": sleeve,
                "horizons_complete": horizons_complete,
                "sleeve_horizon_pass": all(row["pass"] for row in sleeve_scopes),
                "group_pass": all(row["pass"] for row in group_scopes),
                "coverage_gate_pass": tankers_coverage,
                "pass": pass_flag,
                "action": "eligible_for_independent_promotion_review" if pass_flag else "remain_shadow_fail_closed",
            }
        )
    all_pass = bool(sleeve_verdicts) and all(row["pass"] for row in sleeve_verdicts)
    body: dict[str, Any] = {
        "schema_version": "future_only_evidence_evaluation_v1",
        "state": "evaluated_future_only",
        "evidence_class": "prospective_future_only",
        "family": policy.family,
        "policy_id": policy.policy_id,
        "evaluated_at_utc": evaluated_at.isoformat(),
        "capture_identities": capture_identities,
        "outcome_identity": outcome_identity,
        "scope_verdicts": scope_verdicts,
        "sleeve_verdicts": sleeve_verdicts,
        "all_sleeves_pass": all_pass,
        "independent_promotion_review_required": True,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "action": "submit_independent_promotion_review" if all_pass else "remain_shadow_fail_closed",
    }
    body["payload_sha256"] = canonical_sha256(body)
    return body


__all__ = [
    "FutureEvidencePolicy",
    "TrustedReceiptVerifier",
    "build_capture_payload",
    "canonical_sha256",
    "evaluate_future_evidence",
    "exact_sha256",
    "file_sha256",
    "immutable_write_json",
    "validate_capture_payload",
]
