from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestration_contracts.financial_lineage import LINEAGE_FIELDS
from industrials.core.config import cfg_get, load_yaml, resolve_path
from industrials.core.financial_filing_lineage import (
    apply_financial_lineage_gate,
    build_financial_filing_lineage,
    write_financial_lineage_report,
)
from industrials.machinery.scoring import (
    FINAL_RANK_FIELDS,
    file_sha256,
    parse_asof,
    read_rows,
    write_json_atomic,
)
from industrials.machinery.stage12_activation import (
    ACTIVATION_STATUS_FULLY_VALIDATED,
    PRODUCTION_POLICY_STATUS_ACTIVE,
    ActivationPaths,
    _active_cycle_root,
    _write_bytes_atomic,
    apply_active_production_policy,
    changed_production_policy_sources,
    production_policy_source_paths,
    production_policy_source_hashes,
)
from industrials.machinery.stage12_governance import (
    MODEL_FAMILY,
    Stage12Paths,
    _portfolio_family,
    machinery_portfolio_policy_fingerprint,
    production_preview_rows,
)
from industrials.machinery.stage8_calibration import utc_now
from industrials.machinery.stage9_backtest import strategy_spec_by_name


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]

FINANCIAL_LINEAGE_SOURCE_AMENDMENT_FILES = frozenset(
    {
        "scoring.py",
        "10_build_machinery_calibrated_scores.py",
        "10b_publish_machinery_dashboard_reports.py",
        "10b_validate_machinery_dashboard_reports.py",
    }
)
FINANCIAL_LINEAGE_INVARIANT_FIELDS = tuple(
    field for field in FINAL_RANK_FIELDS if field not in LINEAGE_FIELDS
)
MAPPED_FACT_IDEMPOTENCY_SOURCE = "08_build_industrials_financial_features.py"
MAPPED_FACT_IDEMPOTENCY_PATCH = (
    "             ON CONFLICT(\n"
    "                 ticker, source_id, accession_number, taxonomy, concept_name,\n"
    "                 canonical_metric, unit, period_start, period_end, frame\n"
    "             ) DO NOTHING\n"
)


def _require_hash(path: Path, expected: object, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    actual = file_sha256(path)
    if actual != str(expected or ""):
        raise ValueError(f"{label} hash mismatch: expected={expected!r} actual={actual}")


def _changed_source_keys(
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> set[str]:
    return {
        key
        for key in set(previous) | set(current)
        if previous.get(key) != current.get(key)
    }


def _assert_exact_mapped_fact_idempotency_patch(
    source_path: Path,
    *,
    sealed_predecessor_sha256: str,
) -> str:
    """Prove the current builder differs from its seal only by the conflict guard."""
    current = source_path.read_text(encoding="utf-8")
    occurrences = current.count(MAPPED_FACT_IDEMPOTENCY_PATCH)
    if occurrences != 1:
        raise ValueError(
            "Mapped-fact idempotency amendment must occur exactly once; "
            f"found={occurrences}"
        )
    predecessor = current.replace(MAPPED_FACT_IDEMPOTENCY_PATCH, "", 1)
    predecessor_sha256 = sha256(predecessor.encode("utf-8")).hexdigest()
    if predecessor_sha256 != sealed_predecessor_sha256:
        raise ValueError(
            "Mapped-fact idempotency amendment does not reconstruct the sealed "
            "production source"
        )
    return predecessor_sha256


def _mapped_fact_duplicate_group_count(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    source_ids: Sequence[str],
    asof: str,
) -> int:
    """Count production-relevant raw groups that could hit the mapped UNIQUE key."""
    if not tickers or not source_ids:
        return 0
    ticker_ph = ",".join("?" for _ in tickers)
    source_ph = ",".join("?" for _ in source_ids)
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT r.ticker, r.source_id, r.accession_number, r.taxonomy,
                   r.concept_name, m.canonical_metric, r.unit,
                   r.period_start, r.period_end, r.frame
            FROM fact_sec_xbrl_fact_raw AS r
            JOIN dim_xbrl_concept_map AS m
              ON m.taxonomy = r.taxonomy
             AND m.concept_name = r.concept_name
             AND m.active_flag = 1
            WHERE r.ticker IN ({ticker_ph})
              AND r.source_id IN ({source_ph})
              AND r.accession_number IS NOT NULL
              AND r.unit IS NOT NULL
              AND r.period_start IS NOT NULL
              AND r.period_end IS NOT NULL
              AND r.frame IS NOT NULL
              AND r.period_end <= ?
              AND COALESCE(
                    NULLIF(SUBSTR(r.accepted_at, 1, 10), ''),
                    r.filing_date,
                    r.period_end
                  ) <= ?
            GROUP BY r.ticker, r.source_id, r.accession_number, r.taxonomy,
                     r.concept_name, m.canonical_metric, r.unit,
                     r.period_start, r.period_end, r.frame
            HAVING COUNT(DISTINCT r.raw_fact_id) > 1
        )
        """,
        (*tickers, *source_ids, asof, asof),
    ).fetchone()
    return int(row[0] if row is not None else 0)

def _assert_rank_projection_unchanged(
    sealed_rows: Sequence[Mapping[str, Any]],
    reproduced_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Require exact equality for every pre-lineage production output field."""
    sealed_by_ticker = {
        str(row.get("ticker") or ""): row for row in sealed_rows
    }
    reproduced_by_ticker = {
        str(row.get("ticker") or ""): row for row in reproduced_rows
    }
    if not sealed_by_ticker or set(sealed_by_ticker) != set(reproduced_by_ticker):
        raise ValueError("Lineage amendment changed the sealed ticker universe")
    for ticker in sorted(sealed_by_ticker):
        sealed = sealed_by_ticker[ticker]
        reproduced = reproduced_by_ticker[ticker]
        for field in FINANCIAL_LINEAGE_INVARIANT_FIELDS:
            before = str(sealed.get(field) or "")
            after = str(reproduced.get(field) or "")
            if before != after:
                raise ValueError(
                    "Lineage amendment changed sealed production output: "
                    f"{ticker}.{field} before={before!r} after={after!r}"
                )


def validate_active_portfolio_contract(
    portfolio_config: dict[str, Any],
    *,
    expected_cap: float,
    expected_policy_sha256: str,
) -> None:
    """Validate machinery activation invariants without freezing other sleeves."""
    family = _portfolio_family(portfolio_config)
    cap = float(
        cfg_get(
            portfolio_config,
            f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
            -1.0,
        )
    )
    fixed_equal = {
        str(value)
        for value in cfg_get(
            portfolio_config,
            "optimizer.fixed_equal_weight_sleeves",
            [],
        )
    }
    if (
        family.get("enabled") is not True
        or family.get("required") is not True
        or family.get("require_oos_score_valid") is not True
        or cap != expected_cap
        or MODEL_FAMILY not in fixed_equal
        or machinery_portfolio_policy_fingerprint(portfolio_config)
        != expected_policy_sha256
    ):
        raise ValueError(
            "Current portfolio configuration no longer satisfies the sealed "
            "machinery activation contract"
        )


def _run_validator(command: list[str], *, label: str) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} returned invalid JSON: {output or completed.stderr}") from exc
    if completed.returncode != 0 or payload.get("acceptance") != "PASS":
        raise ValueError(f"{label} failed: {payload}")
    return payload


def _validate_live_outputs(
    *,
    config_path: Path,
    asof: str,
) -> dict[str, dict[str, Any]]:
    dashboard = _run_validator(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "10b_validate_machinery_dashboard_reports.py"),
            "--config",
            str(config_path),
            "--asof",
            asof,
        ],
        label="machinery dashboard validation",
    )
    adapter = _run_validator(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "20_validate_machinery_portfolio_adapter.py"),
            "--config",
            str(config_path),
            "--asof",
            asof,
            "--sector-output-root",
            str(PROJECT_ROOT / "output"),
            "--expect-production",
        ],
        label="machinery portfolio adapter validation",
    )
    return {"dashboard": dashboard, "adapter": adapter}


def upgrade_financial_lineage_contract(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Reseal an active model after a lineage-only output-contract change.

    The operation is intentionally narrower than a model amendment. It proves
    exact reproduction of every pre-lineage rank field and requires strict
    lineage coverage on the effective date before changing the source seal.
    """
    effective_asof = parse_asof(asof)
    state_paths = Stage12Paths(governance_root)
    state_path = state_paths.activation_state_json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if (
        state.get("acceptance") != "PASS"
        or state.get("production_policy_status") != PRODUCTION_POLICY_STATUS_ACTIVE
    ):
        raise ValueError("Machinery activation state is not active")

    previous_source_hashes = state.get("production_source_sha256")
    if not isinstance(previous_source_hashes, dict):
        raise ValueError("Machinery activation state has no source seal")
    current_source_hashes = production_policy_source_hashes()
    changed_sources = set(changed_production_policy_sources(previous_source_hashes))

    if not changed_sources:
        history = list(state.get("source_upgrade_history") or [])
        if any(
            item.get("reason") == "financial_lineage_output_contract_v1"
            for item in history
            if isinstance(item, dict)
        ):
            return {
                "acceptance": "PASS",
                "asof_date": effective_asof,
                "operation": "NO_CHANGE_ALREADY_RESEALED",
                "production_policy_status": PRODUCTION_POLICY_STATUS_ACTIVE,
            }
        raise ValueError("Production source seal is already current")
    unexpected = changed_sources - FINANCIAL_LINEAGE_SOURCE_AMENDMENT_FILES
    if unexpected:
        raise ValueError(
            "Financial-lineage amendment found unrelated source changes: "
            + ",".join(sorted(unexpected))
        )

    activation_asof = parse_asof(str(state.get("activation_asof") or ""))
    active_root = _active_cycle_root(state, default_root=governance_root)
    active_paths = Stage12Paths(active_root)
    activation_paths = ActivationPaths(active_root, activation_asof)
    result_path = activation_paths.activation_json
    candidate_path = activation_paths.rank_csv
    _require_hash(
        active_paths.lock_json,
        state.get("governance_lock_sha256"),
        label="governance lock",
    )
    _require_hash(
        candidate_path,
        state.get("candidate_rank_sha256"),
        label="activation candidate",
    )
    _require_hash(
        result_path,
        state.get("activation_result_sha256"),
        label="activation result",
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if (
        result.get("acceptance") != "PASS"
        or result.get("activation_status") != ACTIVATION_STATUS_FULLY_VALIDATED
        or result.get("asof_date") != activation_asof
    ):
        raise ValueError("Machinery activation result is not fully validated")

    live_rank = Path(str(result.get("rank_table") or ""))
    live_manifest = Path(str(result.get("rank_manifest") or ""))
    _require_hash(
        live_rank,
        result.get("rank_table_sha256"),
        label="live machinery rank table",
    )
    if file_sha256(live_rank) != file_sha256(candidate_path):
        raise ValueError("Live machinery rank table differs from sealed candidate")
    _require_hash(
        live_manifest,
        result.get("rank_manifest_sha256"),
        label="live machinery rank manifest",
    )
    live_metadata = json.loads(live_manifest.read_text(encoding="utf-8"))
    sidecar_path = live_manifest.with_name(
        "machinery_stage11_survivorship_calibration_panel.csv"
    )
    _require_hash(
        sidecar_path,
        live_metadata.get("sidecar_sha256"),
        label="shadow calibration sidecar",
    )

    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    lock = json.loads(active_paths.lock_json.read_text(encoding="utf-8"))
    validate_active_portfolio_contract(
        portfolio_config,
        expected_cap=float(lock["proposed_portfolio_cap"]),
        expected_policy_sha256=str(lock["machinery_portfolio_policy_sha256"]),
    )
    selection_policy = lock.get("production_selection_policy")
    if not isinstance(selection_policy, Mapping):
        raise ValueError("Governance lock has no production selection policy")
    candidate_rows = read_rows(candidate_path)
    sidecar_rows = read_rows(sidecar_path)
    reproduced_rows = production_preview_rows(
        sidecar_rows,
        weights={
            str(key): float(value)
            for key, value in lock["recommended_weights"].items()
        },
        asof=activation_asof,
        lock_date=str(lock["lockbox_start_date"]),
        score_model_version=str(
            cfg_get(config, "machinery_stage12.score_model_version")
        ),
        model_version=str(cfg_get(config, "machinery_stage12.model_version")),
        scoring_contract_version=str(
            cfg_get(config, "machinery_stage12.scoring_contract_version")
        ),
        selection_spec=strategy_spec_by_name(
            config,
            str(selection_policy.get("variant") or ""),
        ),
        minimum_positions=int(selection_policy["minimum_positions"]),
        universe_policy=str(selection_policy["universe_policy"]),
    )
    _assert_rank_projection_unchanged(candidate_rows, reproduced_rows)

    resolved_db = (
        db_path.expanduser().resolve()
        if db_path is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    with closing(
        sqlite3.connect(f"{resolved_db.as_uri()}?mode=ro", uri=True)
    ) as conn:
        conn.row_factory = sqlite3.Row
        lineage = build_financial_filing_lineage(
            conn,
            model_family=MODEL_FAMILY,
            asof=effective_asof,
            tickers=(row.get("ticker", "") for row in candidate_rows),
        )
    effective_rows = apply_financial_lineage_gate(
        [dict(row) for row in candidate_rows],
        lineage,
    )
    unresolved = sorted(
        str(row.get("ticker") or "")
        for row in effective_rows
        if str(row.get("financial_lineage_gate") or "") != "1"
    )
    if unresolved:
        raise ValueError(
            "Financial-lineage source amendment is not production-ready: "
            + ",".join(unresolved)
        )

    backup_root = (
        governance_root
        / "activation_contract_upgrades"
        / effective_asof
        / "financial_lineage_output_contract_v1"
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    manifest_path = active_paths.manifest_json
    stage12_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock_metadata = stage12_manifest.get("files", {}).get(
        active_paths.lock_json.name
    )
    if (
        not isinstance(lock_metadata, dict)
        or lock_metadata.get("sha256") != file_sha256(active_paths.lock_json)
    ):
        raise ValueError("Stage 12 manifest does not seal the current governance lock")
    backup_paths = {
        state_path: backup_root / "activation_state_before_upgrade.json",
        active_paths.lock_json: backup_root / "governance_lock_before_upgrade.json",
        manifest_path: backup_root / "stage12_manifest_before_upgrade.json",
    }
    original_bytes = {source: source.read_bytes() for source in backup_paths}
    for source, backup in backup_paths.items():
        if not backup.exists():
            _write_bytes_atomic(backup, original_bytes[source])
    lineage_path = backup_root / "machinery_financial_filing_lineage.csv"
    report_path = backup_root / "machinery_financial_lineage_source_upgrade.json"
    lineage_existed = lineage_path.exists()
    report_existed = report_path.exists()
    lineage_before = lineage_path.read_bytes() if lineage_existed else b""
    report_before = report_path.read_bytes() if report_existed else b""
    upgraded_at = utc_now()
    try:
        lineage_manifest = write_financial_lineage_report(
            lineage_path,
            effective_rows,
            model_family=MODEL_FAMILY,
            asof=effective_asof,
            policy_context="production",
        )
        if lineage_manifest.get("acceptance") != "PASS":
            raise ValueError("Shared financial-lineage policy rejected amendment")
        from portfolio_layer.scores.adapter_semantics import (
            industrial_adapter_semantic_sha256,
        )

        adapter_path = (
            config_path.parents[2] / "portfolio_layer" / "scores" / "adapters.py"
        )
        previous_adapter_semantic_sha256 = str(
            lock.get("portfolio_adapter_semantic_sha256") or ""
        )
        current_adapter_semantic_sha256 = industrial_adapter_semantic_sha256()
        lock.update(
            {
                "portfolio_adapter_semantic_sha256": (
                    current_adapter_semantic_sha256
                ),
                "portfolio_adapter_sha256": file_sha256(adapter_path),
                "financial_lineage_adapter_amendment": {
                    "version": "financial_lineage_output_contract_v1",
                    "effective_asof": effective_asof,
                    "upgraded_at_utc": upgraded_at,
                    "previous_adapter_semantic_sha256": (
                        previous_adapter_semantic_sha256
                    ),
                    "candidate_reproduction_required": True,
                },
            }
        )
        write_json_atomic(active_paths.lock_json, lock)
        lock_metadata["path"] = str(active_paths.lock_json)
        lock_metadata["sha256"] = file_sha256(active_paths.lock_json)
        write_json_atomic(manifest_path, stage12_manifest)

        history = list(state.get("source_upgrade_history") or [])
        history.append(
            {
                "upgraded_at_utc": upgraded_at,
                "effective_asof": effective_asof,
                "reason": "financial_lineage_output_contract_v1",
                "changed_source_files": sorted(changed_sources),
                "candidate_reproduced_exactly": True,
                "invariant_field_count": len(FINANCIAL_LINEAGE_INVARIANT_FIELDS),
                "previous_adapter_semantic_sha256": (
                    previous_adapter_semantic_sha256
                ),
                "portfolio_adapter_semantic_sha256": (
                    current_adapter_semantic_sha256
                ),
                "lineage_report": str(lineage_path),
                "lineage_report_sha256": file_sha256(lineage_path),
            }
        )
        state.update(
            {
                "production_source_sha256": current_source_hashes,
                "governance_lock_sha256": file_sha256(active_paths.lock_json),
                "financial_lineage_contract_effective_asof": effective_asof,
                "financial_lineage_contract_upgraded_at_utc": upgraded_at,
                "source_upgrade_history": history,
            }
        )
        write_json_atomic(state_path, state)
        regenerated_rows, policy_metadata = apply_active_production_policy(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=activation_asof,
            shadow_rows=sidecar_rows,
        )
        _assert_rank_projection_unchanged(candidate_rows, regenerated_rows)
        report = {
            "acceptance": "PASS",
            "asof_date": effective_asof,
            "activation_asof": activation_asof,
            "upgraded_at_utc": upgraded_at,
            "operation": "FINANCIAL_LINEAGE_OUTPUT_CONTRACT_RESEAL",
            "changed_source_files": sorted(changed_sources),
            "candidate_reproduced_exactly": True,
            "portfolio_adapter_semantic_resealed": True,
            "previous_adapter_semantic_sha256": (
                previous_adapter_semantic_sha256
            ),
            "portfolio_adapter_semantic_sha256": (
                current_adapter_semantic_sha256
            ),
            "governance_lock_sha256": file_sha256(active_paths.lock_json),
            "stage12_manifest_sha256": file_sha256(manifest_path),
            "invariant_field_count": len(FINANCIAL_LINEAGE_INVARIANT_FIELDS),
            "row_count": len(candidate_rows),
            "lineage_pass_count": len(effective_rows),
            "lineage_manifest": lineage_manifest,
            "production_policy_status": policy_metadata[
                "production_policy_status"
            ],
            "activation_state": str(state_path),
            "activation_state_sha256": file_sha256(state_path),
            "backup_artifacts": {
                source.name: str(backup)
                for source, backup in backup_paths.items()
            },
        }
        write_json_atomic(report_path, report)
    except BaseException:
        for source, payload in original_bytes.items():
            _write_bytes_atomic(source, payload)
        if lineage_existed:
            _write_bytes_atomic(lineage_path, lineage_before)
        else:
            lineage_path.unlink(missing_ok=True)
        if report_existed:
            _write_bytes_atomic(report_path, report_before)
        else:
            report_path.unlink(missing_ok=True)
        raise
    return report


def upgrade_mapped_fact_idempotency_contract(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Reseal the exact mapped-fact conflict guard when machinery is unaffected."""
    effective_asof = parse_asof(asof)
    state_path = Stage12Paths(governance_root).activation_state_json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if (
        state.get("acceptance") != "PASS"
        or state.get("production_policy_status") != PRODUCTION_POLICY_STATUS_ACTIVE
    ):
        raise ValueError("Machinery activation state is not active")

    previous_source_hashes = state.get("production_source_sha256")
    if not isinstance(previous_source_hashes, dict):
        raise ValueError("Machinery activation state has no source seal")
    current_source_hashes = production_policy_source_hashes()
    changed_sources = set(changed_production_policy_sources(previous_source_hashes))
    history = list(state.get("source_upgrade_history") or [])
    reason = "mapped_fact_idempotency_conflict_guard_v1"
    if not changed_sources:
        if any(
            isinstance(item, dict) and item.get("reason") == reason
            for item in history
        ):
            return {
                "acceptance": "PASS",
                "asof_date": effective_asof,
                "operation": "NO_CHANGE_ALREADY_RESEALED",
                "production_policy_status": PRODUCTION_POLICY_STATUS_ACTIVE,
            }
        raise ValueError("Production source seal is already current")
    if changed_sources != {MAPPED_FACT_IDEMPOTENCY_SOURCE}:
        raise ValueError(
            "Mapped-fact idempotency amendment found unrelated source changes: "
            + ",".join(sorted(changed_sources))
        )

    source_path = production_policy_source_paths()[MAPPED_FACT_IDEMPOTENCY_SOURCE]
    predecessor_sha256 = _assert_exact_mapped_fact_idempotency_patch(
        source_path,
        sealed_predecessor_sha256=str(
            previous_source_hashes[MAPPED_FACT_IDEMPOTENCY_SOURCE]
        ),
    )
    activation_asof = parse_asof(str(state.get("activation_asof") or ""))
    active_root = _active_cycle_root(state, default_root=governance_root)
    active_paths = Stage12Paths(active_root)
    activation_paths = ActivationPaths(active_root, activation_asof)
    result_path = activation_paths.activation_json
    candidate_path = activation_paths.rank_csv
    _require_hash(
        active_paths.lock_json,
        state.get("governance_lock_sha256"),
        label="governance lock",
    )
    _require_hash(
        candidate_path,
        state.get("candidate_rank_sha256"),
        label="activation candidate",
    )
    _require_hash(
        result_path,
        state.get("activation_result_sha256"),
        label="activation result",
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if (
        result.get("acceptance") != "PASS"
        or result.get("activation_status") != ACTIVATION_STATUS_FULLY_VALIDATED
        or result.get("asof_date") != activation_asof
    ):
        raise ValueError("Machinery activation result is not fully validated")

    live_rank = Path(str(result.get("rank_table") or ""))
    live_manifest = Path(str(result.get("rank_manifest") or ""))
    _require_hash(live_rank, result.get("rank_table_sha256"), label="live rank table")
    if file_sha256(live_rank) != file_sha256(candidate_path):
        raise ValueError("Live machinery rank table differs from sealed candidate")
    _require_hash(
        live_manifest,
        result.get("rank_manifest_sha256"),
        label="live rank manifest",
    )
    rank_manifest = json.loads(live_manifest.read_text(encoding="utf-8"))
    sidecar_path = live_manifest.with_name(
        "machinery_stage11_survivorship_calibration_panel.csv"
    )
    _require_hash(
        sidecar_path,
        rank_manifest.get("sidecar_sha256"),
        label="shadow calibration sidecar",
    )
    lock = json.loads(active_paths.lock_json.read_text(encoding="utf-8"))
    selection_policy = lock.get("production_selection_policy")
    if not isinstance(selection_policy, Mapping):
        raise ValueError("Governance lock has no production selection policy")
    candidate_rows = read_rows(candidate_path)
    sidecar_rows = read_rows(sidecar_path)
    reproduced_rows = production_preview_rows(
        sidecar_rows,
        weights={
            str(key): float(value)
            for key, value in lock["recommended_weights"].items()
        },
        asof=activation_asof,
        lock_date=str(lock["lockbox_start_date"]),
        score_model_version=str(
            cfg_get(config, "machinery_stage12.score_model_version")
        ),
        model_version=str(cfg_get(config, "machinery_stage12.model_version")),
        scoring_contract_version=str(
            cfg_get(config, "machinery_stage12.scoring_contract_version")
        ),
        selection_spec=strategy_spec_by_name(
            config,
            str(selection_policy.get("variant") or ""),
        ),
        minimum_positions=int(selection_policy["minimum_positions"]),
        universe_policy=str(selection_policy["universe_policy"]),
    )
    _assert_rank_projection_unchanged(candidate_rows, reproduced_rows)

    primary_source = str(
        cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")
        or "sec_companyfacts"
    )
    supplemental_raw = cfg_get(
        config,
        "model_families.machinery.financial.supplemental_disclosure_source_ids",
        None,
    )
    if supplemental_raw is None:
        supplemental_raw = cfg_get(
            config, "sec_fundamentals.supplemental_disclosure_source_ids", []
        )
    if isinstance(supplemental_raw, str):
        supplemental = [
            item.strip() for item in supplemental_raw.split(",") if item.strip()
        ]
    else:
        supplemental = [
            str(item).strip() for item in supplemental_raw or [] if str(item).strip()
        ]
    source_ids = tuple(dict.fromkeys((primary_source, *supplemental)))
    resolved_db = (
        db_path.expanduser().resolve()
        if db_path is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    with closing(sqlite3.connect(f"{resolved_db.as_uri()}?mode=ro", uri=True)) as conn:
        duplicate_groups = _mapped_fact_duplicate_group_count(
            conn,
            tickers=sorted(
                str(row.get("ticker") or "")
                for row in candidate_rows
                if str(row.get("ticker") or "")
            ),
            source_ids=source_ids,
            asof=effective_asof,
        )
    if duplicate_groups:
        raise ValueError(
            "Mapped-fact idempotency amendment affects the active machinery "
            f"universe; duplicate_groups={duplicate_groups}"
        )

    backup_root = (
        governance_root
        / "activation_contract_upgrades"
        / effective_asof
        / reason
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / "activation_state_before_upgrade.json"
    original_state = state_path.read_bytes()
    if not backup_path.exists():
        _write_bytes_atomic(backup_path, original_state)
    report_path = backup_root / "mapped_fact_idempotency_source_upgrade.json"
    upgraded_at = utc_now()
    try:
        history.append(
            {
                "upgraded_at_utc": upgraded_at,
                "effective_asof": effective_asof,
                "reason": reason,
                "changed_source_files": sorted(changed_sources),
                "sealed_predecessor_sha256": predecessor_sha256,
                "candidate_reproduced_exactly": True,
                "active_universe_duplicate_group_count": duplicate_groups,
            }
        )
        state.update(
            {
                "production_source_sha256": current_source_hashes,
                "mapped_fact_idempotency_contract_effective_asof": effective_asof,
                "mapped_fact_idempotency_contract_upgraded_at_utc": upgraded_at,
                "source_upgrade_history": history,
            }
        )
        write_json_atomic(state_path, state)
        regenerated_rows, policy_metadata = apply_active_production_policy(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=activation_asof,
            shadow_rows=sidecar_rows,
        )
        _assert_rank_projection_unchanged(candidate_rows, regenerated_rows)
        validations = _validate_live_outputs(
            config_path=config_path,
            asof=activation_asof,
        )
        report = {
            "acceptance": "PASS",
            "asof_date": effective_asof,
            "activation_asof": activation_asof,
            "upgraded_at_utc": upgraded_at,
            "operation": "MAPPED_FACT_IDEMPOTENCY_CONTRACT_RESEAL",
            "changed_source_files": sorted(changed_sources),
            "sealed_predecessor_sha256": predecessor_sha256,
            "current_source_sha256": current_source_hashes[
                MAPPED_FACT_IDEMPOTENCY_SOURCE
            ],
            "candidate_reproduced_exactly": True,
            "active_universe_duplicate_group_count": duplicate_groups,
            "source_ids_checked": list(source_ids),
            "row_count": len(candidate_rows),
            "production_policy_status": policy_metadata[
                "production_policy_status"
            ],
            "validations": validations,
            "activation_state": str(state_path),
            "activation_state_sha256": file_sha256(state_path),
            "backup_artifact": str(backup_path),
        }
        write_json_atomic(report_path, report)
    except BaseException:
        _write_bytes_atomic(state_path, original_state)
        report_path.unlink(missing_ok=True)
        raise
    return report

def upgrade_active_contract(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
) -> dict[str, Any]:
    """Upgrade only activation metadata after verifying the sealed 7/24 run."""
    asof = parse_asof(asof)
    state_store_paths = Stage12Paths(governance_root)
    state_path = state_store_paths.activation_state_json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    active_root = _active_cycle_root(state, default_root=governance_root)
    stage12_paths = Stage12Paths(active_root)
    activation_paths = ActivationPaths(active_root, asof)
    result_path = activation_paths.activation_json
    candidate_path = activation_paths.rank_csv
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if (
        state.get("acceptance") != "PASS"
        or state.get("production_policy_status") != PRODUCTION_POLICY_STATUS_ACTIVE
        or state.get("activation_asof") != asof
    ):
        raise ValueError("Machinery activation state is not active for this date")
    if (
        result.get("acceptance") != "PASS"
        or result.get("activation_status") != ACTIVATION_STATUS_FULLY_VALIDATED
        or result.get("asof_date") != asof
        or result.get("full_portfolio_smoke_required") is not False
    ):
        raise ValueError("Machinery activation result is not fully validated")

    current_source_hashes = production_policy_source_hashes()
    previous_source_hashes = state.get("production_source_sha256")
    if not isinstance(previous_source_hashes, dict):
        raise ValueError("Machinery activation state has no source seal")
    semantic_source_keys = {
        "scoring.py",
        "08_build_industrials_financial_features.py",
        "financial_metric_contract.py",
        "06a_build_machinery_scoring_features.py",
        "stage8_calibration.py",
        "stage9_backtest.py",
        "production_universe.py",
        "stage12_governance.py",
    }
    changed_semantic_sources = sorted(
        semantic_source_keys.intersection(
            changed_production_policy_sources(previous_source_hashes)
        )
    )
    if changed_semantic_sources:
        raise ValueError(
            "Scoring or selection semantics changed; run a new Stage 8/9/12 "
            "calibration and activation: " + ",".join(changed_semantic_sources)
        )

    _require_hash(
        stage12_paths.lock_json,
        state.get("governance_lock_sha256"),
        label="governance lock",
    )
    _require_hash(
        candidate_path,
        state.get("candidate_rank_sha256"),
        label="activation candidate",
    )
    _require_hash(
        result_path,
        state.get("activation_result_sha256"),
        label="activation result",
    )
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    governance_lock = json.loads(
        stage12_paths.lock_json.read_text(encoding="utf-8")
    )
    validate_active_portfolio_contract(
        portfolio_config,
        expected_cap=float(governance_lock["proposed_portfolio_cap"]),
        expected_policy_sha256=str(
            governance_lock["machinery_portfolio_policy_sha256"]
        ),
    )
    current_portfolio_config_sha256 = file_sha256(portfolio_config_path)
    # The portfolio run directory is shared and may be rebuilt after
    # activation. Its original hashes remain transitively sealed inside the
    # unchanged activation result verified above; do not compare those mutable
    # paths to later rerun content during a source-contract upgrade.

    live_rank = Path(str(result.get("rank_table") or ""))
    live_manifest = Path(str(result.get("rank_manifest") or ""))
    _require_hash(
        live_rank,
        result.get("rank_table_sha256"),
        label="live machinery rank table",
    )
    if file_sha256(live_rank) != file_sha256(candidate_path):
        raise ValueError("Live machinery rank table differs from sealed candidate")
    _require_hash(
        live_manifest,
        result.get("rank_manifest_sha256"),
        label="live machinery rank manifest",
    )
    sidecar_path = live_manifest.with_name("machinery_stage11_survivorship_calibration_panel.csv")
    manifest = json.loads(live_manifest.read_text(encoding="utf-8"))
    _require_hash(
        sidecar_path,
        manifest.get("sidecar_sha256"),
        label="shadow calibration sidecar",
    )

    candidate_rows = read_rows(candidate_path)
    sidecar_rows = read_rows(sidecar_path)
    if sorted(row["ticker"] for row in candidate_rows) != sorted(row["ticker"] for row in sidecar_rows):
        raise ValueError("Production and calibration ticker universes differ")

    backup_root = governance_root / "activation_contract_upgrades" / asof
    backup_root.mkdir(parents=True, exist_ok=True)
    backups = {
        live_manifest: backup_root / "rank_manifest_before_upgrade.json",
        result_path: backup_root / "activation_result_before_upgrade.json",
        state_path: backup_root / "activation_state_before_upgrade.json",
    }
    originals = {path: path.read_bytes() for path in backups}
    for source, backup in backups.items():
        if not backup.exists():
            _write_bytes_atomic(backup, originals[source])

    upgraded_at = utc_now()
    try:
        activation_metadata = dict(manifest.get("activation_metadata") or {})
        activation_metadata.update(
            {
                "activation_status": ACTIVATION_STATUS_FULLY_VALIDATED,
                "activation_asof": asof,
                "contract_upgraded_at_utc": upgraded_at,
            }
        )
        manifest.update(
            {
                "acceptance": "PASS",
                "asof_date": asof,
                "row_count": len(candidate_rows),
                "rank_ready_count": sum(row.get("rank_ready_flag") == "1" for row in candidate_rows),
                "portfolio_candidate_count": sum(row.get("portfolio_candidate_gate") == "1" for row in candidate_rows),
                "selected_sleeve_count": sum(
                    row.get("portfolio_sleeve_selected_flag") == "1" for row in candidate_rows
                ),
                "sidecar_calibration_eligible_count": sum(
                    row.get("stage11_calibration_input_eligible_flag") == "1" for row in sidecar_rows
                ),
                "contract_fields": FINAL_RANK_FIELDS,
                "scoring_contract_versions": sorted(
                    {row.get("scoring_contract_version", "") for row in candidate_rows}
                ),
                "rank_table_sha256": file_sha256(live_rank),
                "sidecar_sha256": file_sha256(sidecar_path),
                "production_promoted": True,
                "production_policy_active": True,
                "production_promotion_status": (ACTIVATION_STATUS_FULLY_VALIDATED),
                "sidecar_retained_shadow": True,
                "activation_metadata": activation_metadata,
            }
        )
        write_json_atomic(live_manifest, manifest)
        validations = _validate_live_outputs(
            config_path=config_path,
            asof=asof,
        )

        result["rank_manifest_sha256"] = file_sha256(live_manifest)
        result["contract_upgrade"] = {
            "acceptance": "PASS",
            "upgraded_at_utc": upgraded_at,
            "dashboard_validation": validations["dashboard"]["acceptance"],
            "adapter_validation": validations["adapter"]["acceptance"],
        }
        write_json_atomic(result_path, result)

        history = list(state.get("source_upgrade_history") or [])
        history.append(
            {
                "upgraded_at_utc": upgraded_at,
                "reason": "production_dashboard_sidecar_contract_alignment",
                "previous_activation_result_sha256": (file_sha256(backups[result_path])),
            }
        )
        state.update(
            {
                "activation_result_sha256": file_sha256(result_path),
                "portfolio_config_sha256_at_activation": (
                    current_portfolio_config_sha256
                ),
                "production_source_sha256": current_source_hashes,
                "contract_upgraded_at_utc": upgraded_at,
                "source_upgrade_history": history,
            }
        )
        write_json_atomic(state_path, state)

        regenerated_rows, policy_metadata = apply_active_production_policy(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=asof,
            shadow_rows=sidecar_rows,
        )
        if regenerated_rows != candidate_rows:
            raise ValueError("Resealed production policy does not reproduce the candidate")
    except BaseException:
        for path, payload in originals.items():
            _write_bytes_atomic(path, payload)
        raise

    report = {
        "acceptance": "PASS",
        "asof_date": asof,
        "upgraded_at_utc": upgraded_at,
        "historical_rebuild_performed": False,
        "portfolio_rerun_performed": False,
        "row_count": len(candidate_rows),
        "selected_sleeve_count": sum(row.get("portfolio_sleeve_selected_flag") == "1" for row in candidate_rows),
        "rank_manifest_sha256": file_sha256(live_manifest),
        "activation_result_sha256": file_sha256(result_path),
        "activation_state_sha256": file_sha256(state_path),
        "production_policy_status": policy_metadata["production_policy_status"],
        "validations": validations,
        "backup_root": str(backup_root),
    }
    write_json_atomic(
        backup_root / "machinery_activation_contract_upgrade.json",
        report,
    )
    return report


def migrate_active_adapter_semantic_seal(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
) -> dict[str, Any]:
    """Atomically replace a legacy whole-adapter seal with an industrial AST seal."""
    from industrials.machinery.stage12_governance import validate_stage12_lock
    from portfolio_layer.scores.adapter_semantics import (
        industrial_adapter_semantic_sha256,
    )

    asof = parse_asof(asof)
    state_store_paths = Stage12Paths(governance_root)
    state_path = state_store_paths.activation_state_json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    active_root = _active_cycle_root(state, default_root=governance_root)
    stage12_paths = Stage12Paths(active_root)
    activation_paths = ActivationPaths(active_root, asof)
    result_path = activation_paths.activation_json
    candidate_path = activation_paths.rank_csv
    result = json.loads(result_path.read_text(encoding="utf-8"))

    if (
        state.get("acceptance") != "PASS"
        or state.get("production_policy_status") != PRODUCTION_POLICY_STATUS_ACTIVE
        or state.get("activation_asof") != asof
    ):
        raise ValueError("Machinery activation state is not active for this date")
    if (
        result.get("acceptance") != "PASS"
        or result.get("activation_status") != ACTIVATION_STATUS_FULLY_VALIDATED
        or result.get("asof_date") != asof
        or result.get("full_portfolio_smoke_required") is not False
    ):
        raise ValueError("Machinery activation result is not fully validated")

    _require_hash(
        stage12_paths.lock_json,
        state.get("governance_lock_sha256"),
        label="governance lock",
    )
    _require_hash(
        candidate_path,
        state.get("candidate_rank_sha256"),
        label="activation candidate",
    )
    _require_hash(
        result_path,
        state.get("activation_result_sha256"),
        label="activation result",
    )
    stage12_validation = validate_stage12_lock(output_root=active_root)
    if stage12_validation.get("acceptance") != "PASS":
        raise ValueError(
            "Existing Stage 12 lock is invalid: "
            + ";".join(stage12_validation.get("issues", []))
        )

    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    lock = json.loads(stage12_paths.lock_json.read_text(encoding="utf-8"))
    validate_active_portfolio_contract(
        portfolio_config,
        expected_cap=float(lock["proposed_portfolio_cap"]),
        expected_policy_sha256=str(lock["machinery_portfolio_policy_sha256"]),
    )

    live_rank = Path(str(result.get("rank_table") or ""))
    live_manifest = Path(str(result.get("rank_manifest") or ""))
    _require_hash(live_rank, result.get("rank_table_sha256"), label="live rank table")
    if file_sha256(live_rank) != file_sha256(candidate_path):
        raise ValueError("Live machinery rank table differs from sealed candidate")
    previous_rank_manifest_sha256 = str(
        result.get("rank_manifest_sha256") or ""
    )
    current_rank_manifest_sha256 = file_sha256(live_manifest)
    rank_manifest = json.loads(live_manifest.read_text(encoding="utf-8"))
    if (
        rank_manifest.get("acceptance") != "PASS"
        or rank_manifest.get("asof_date") != asof
        or rank_manifest.get("rank_table_sha256") != file_sha256(live_rank)
        or rank_manifest.get("production_policy_active") is not True
    ):
        raise ValueError(
            "Live machinery rank manifest does not describe the sealed "
            "production candidate"
        )
    sidecar_path = live_manifest.with_name(
        "machinery_stage11_survivorship_calibration_panel.csv"
    )
    _require_hash(
        sidecar_path,
        rank_manifest.get("sidecar_sha256"),
        label="shadow calibration sidecar",
    )
    candidate_rows = read_rows(candidate_path)
    sidecar_rows = read_rows(sidecar_path)

    previous_source_hashes = state.get("production_source_sha256")
    if not isinstance(previous_source_hashes, dict):
        raise ValueError("Machinery activation state has no source seal")
    current_source_hashes = production_policy_source_hashes()
    permitted_migration_sources = {
        "stage12_activation.py",
        "stage12_governance.py",
        "adapter_semantics.py",
    }
    unexpected_source_changes = sorted(
        key
        for key in changed_production_policy_sources(previous_source_hashes)
        if key not in permitted_migration_sources
    )
    if unexpected_source_changes:
        raise ValueError(
            "Unrelated production policy sources changed; migration refused: "
            + ",".join(unexpected_source_changes)
        )

    semantic_sha256 = industrial_adapter_semantic_sha256()
    adapter_path = (
        config_path.parents[2] / "portfolio_layer" / "scores" / "adapters.py"
    )
    backup_root = (
        governance_root
        / "activation_contract_upgrades"
        / asof
        / "industrial_adapter_semantic_seal"
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    manifest_path = stage12_paths.manifest_json
    originals = {
        stage12_paths.lock_json: stage12_paths.lock_json.read_bytes(),
        manifest_path: manifest_path.read_bytes(),
        state_path: state_path.read_bytes(),
        result_path: result_path.read_bytes(),
    }
    backup_names = {
        stage12_paths.lock_json: "governance_lock_before_migration.json",
        manifest_path: "stage12_manifest_before_migration.json",
        state_path: "activation_state_before_migration.json",
        result_path: "activation_result_before_migration.json",
    }
    for source, payload in originals.items():
        backup = backup_root / backup_names[source]
        if not backup.exists():
            _write_bytes_atomic(backup, payload)

    migrated_at = utc_now()
    previous_lock_sha256 = file_sha256(stage12_paths.lock_json)
    try:
        lock["portfolio_adapter_semantic_sha256"] = semantic_sha256
        lock["portfolio_adapter_sha256"] = file_sha256(adapter_path)
        lock["adapter_seal_migration"] = {
            "version": "industrial_adapter_ast_v1",
            "migrated_at_utc": migrated_at,
            "previous_governance_lock_sha256": previous_lock_sha256,
            "candidate_reproduction_required": True,
        }
        write_json_atomic(stage12_paths.lock_json, lock)

        stage12_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        lock_metadata = stage12_manifest.get("files", {}).get(
            stage12_paths.lock_json.name
        )
        if not isinstance(lock_metadata, dict):
            raise ValueError("Stage 12 manifest does not seal the governance lock")
        lock_metadata["path"] = str(stage12_paths.lock_json)
        lock_metadata["sha256"] = file_sha256(stage12_paths.lock_json)
        write_json_atomic(manifest_path, stage12_manifest)
        result["rank_manifest_sha256"] = current_rank_manifest_sha256
        result["adapter_semantic_seal_migration"] = {
            "migrated_at_utc": migrated_at,
            "previous_rank_manifest_sha256": previous_rank_manifest_sha256,
            "validated_rank_manifest_sha256": current_rank_manifest_sha256,
        }
        write_json_atomic(result_path, result)

        history = list(state.get("source_upgrade_history") or [])
        history.append(
            {
                "upgraded_at_utc": migrated_at,
                "reason": "industrial_adapter_semantic_seal_migration",
                "previous_governance_lock_sha256": previous_lock_sha256,
                "portfolio_adapter_semantic_sha256": semantic_sha256,
            }
        )
        state.update(
            {
                "activation_result_sha256": file_sha256(result_path),
                "governance_lock_sha256": file_sha256(stage12_paths.lock_json),
                "production_source_sha256": current_source_hashes,
                "adapter_semantic_seal_migrated_at_utc": migrated_at,
                "source_upgrade_history": history,
            }
        )
        write_json_atomic(state_path, state)

        regenerated_rows, policy_metadata = apply_active_production_policy(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=asof,
            shadow_rows=sidecar_rows,
        )
        if regenerated_rows != candidate_rows:
            raise ValueError(
                "Scoped adapter seal does not reproduce the activated candidate"
            )
        validations = _validate_live_outputs(config_path=config_path, asof=asof)
    except BaseException:
        for path, payload in originals.items():
            _write_bytes_atomic(path, payload)
        raise

    report = {
        "acceptance": "PASS",
        "asof_date": asof,
        "migrated_at_utc": migrated_at,
        "historical_rebuild_performed": False,
        "portfolio_rerun_performed": False,
        "candidate_reproduced_exactly": True,
        "row_count": len(candidate_rows),
        "previous_governance_lock_sha256": previous_lock_sha256,
        "governance_lock_sha256": file_sha256(stage12_paths.lock_json),
        "portfolio_adapter_semantic_sha256": semantic_sha256,
        "production_policy_status": policy_metadata["production_policy_status"],
        "validations": validations,
        "backup_root": str(backup_root),
    }
    write_json_atomic(
        backup_root / "machinery_adapter_semantic_seal_migration.json",
        report,
    )
    return report
