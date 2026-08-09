#!/usr/bin/env python3
"""Reconcile newly arrived IB statements into existing dated portfolio runs."""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ORCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ORCH_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    manifest_acceptance_value,
    read_manifest,
    sha256_file,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.ledger.ledger_common import peek_statement_period_end  # noqa: E402


def _latest_completed_trading_session() -> str:
    """Load the sibling runner without relying on a collision-prone package name."""
    module_name = "_late_ib_reconciliation_run_all"
    spec = importlib.util.spec_from_file_location(module_name, ORCH_DIR / "run_all.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {ORCH_DIR / 'run_all.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return str(module.latest_completed_trading_session())
    finally:
        sys.modules.pop(module_name, None)


DEFAULT_CONFIG = PROJECT_ROOT / "portfolio_layer" / "config.yaml"
PORTFOLIO_RUNNER = (
    PROJECT_ROOT / "portfolio_layer" / "orchestration" / "18_run_portfolio_pipeline.py"
)
RECOVERY_META_NAME = "late_statement_orchestration_meta.json"
LEDGER_META_NAME = "late_statement_ledger_meta.json"
BASE_POST_LEDGER_GROUPS = ("exits", "payout", "final", "final_report")
COVERAGE_POST_LEDGER_GROUPS = (
    "bootstrap_final",
    "earnings",
    "monitor",
    "monitor_filter",
    "rotation",
    "macro_contract",
    "bl",
    "sleeves",
    "exits",
    "payout",
    "governor",
    "final",
    "final_report",
)
REQUIRED_GROUPS = set(BASE_POST_LEDGER_GROUPS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile late IB statements without rebuilding upstream stages.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--target", default="", help="Latest statement end date eligible for reconciliation.")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _accepted_ledger(run_dir: Path, run_as_of: str) -> bool:
    path = run_dir / "ledger" / "ledger_manifest.json"
    try:
        manifest = read_manifest(path)
    except (OSError, ValueError):
        return False
    return (
        manifest_acceptance_value(manifest).startswith("PASS")
        and str(manifest.get("run_as_of", "")) == run_as_of
    )


def _completed_broker_chain(run_dir: Path, run_as_of: str) -> bool:
    if not _accepted_ledger(run_dir, run_as_of):
        return False
    recovery_path = run_dir / RECOVERY_META_NAME
    if not recovery_path.exists():
        # Existing accepted ledgers predate this automation and are grandfathered.
        # A future partial recovery always leaves RECOVERY_META_NAME and is retried.
        return True
    payload = _load_json(recovery_path)
    completed = {str(group) for group in payload.get("groups_completed", [])}
    return (
        str(payload.get("acceptance", "")) == "PASS"
        and REQUIRED_GROUPS.issubset(completed)
    )


def _sealed_tickers(
    *,
    csv_path: Path,
    manifest_path: Path,
    output_key: str,
    run_as_of: str,
    ticker_field: str,
) -> set[str]:
    """Read ticker membership only after validating the run-local artifact seal."""
    manifest = read_manifest(manifest_path)
    if not manifest_acceptance_value(manifest).startswith("PASS"):
        raise ValueError(f"artifact is not accepted: {manifest_path}")
    manifest_as_of = str(manifest.get("run_as_of", manifest.get("as_of_date", "")))
    if manifest_as_of != run_as_of:
        raise ValueError(
            f"artifact date mismatch: {manifest_path} has {manifest_as_of!r}, "
            f"expected {run_as_of!r}"
        )
    outputs = manifest.get("outputs_sha256", {})
    expected = str(outputs.get(output_key, "")) if isinstance(outputs, dict) else ""
    if not expected:
        # The holdings ledger seals broker inputs under provenance_sha256.
        provenance = manifest.get("provenance_sha256", {})
        expected = (
            str(provenance.get(Path(output_key).stem, ""))
            if isinstance(provenance, dict)
            else ""
        )
    if not expected or sha256_file(csv_path) != expected:
        raise ValueError(f"artifact hash mismatch or missing seal: {csv_path}")

    tickers: set[str] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = str(row.get(ticker_field, "")).strip().upper()
            if ticker:
                tickers.add(ticker)
    return tickers


def late_holding_coverage_gaps(run_dir: Path, run_as_of: str) -> dict[str, list[str]]:
    """Return same-date stock holdings absent from earnings or monitoring."""
    holdings = _sealed_tickers(
        csv_path=run_dir / "ledger" / "broker_net_stock_positions.csv",
        manifest_path=run_dir / "ledger" / "ledger_manifest.json",
        output_key="broker_net_stock_positions.csv",
        run_as_of=run_as_of,
        ticker_field="symbol",
    )
    earnings = _sealed_tickers(
        csv_path=run_dir / "earnings_dates" / "earnings_calendar.csv",
        manifest_path=run_dir / "earnings_dates" / "earnings_manifest.json",
        output_key="earnings_calendar.csv",
        run_as_of=run_as_of,
        ticker_field="ticker",
    )
    monitor = _sealed_tickers(
        csv_path=run_dir / "expectations_monitor" / "monitor_universe.csv",
        manifest_path=run_dir / "expectations_monitor" / "monitor_universe_manifest.json",
        output_key="monitor_universe.csv",
        run_as_of=run_as_of,
        ticker_field="ticker",
    )
    return {
        "holdings": sorted(holdings),
        "missing_from_earnings": sorted(holdings - earnings),
        "missing_from_monitor": sorted(holdings - monitor),
    }


def _has_existing_book(run_dir: Path) -> bool:
    return (
        (run_dir / "stocks_scores.csv").is_file()
        and (
            (run_dir / "final" / "final_target_weights.csv").is_file()
            or (run_dir / "final" / "final_target_book.csv").is_file()
        )
    )


def plan_reconciliations(
    *, source_dir: Path, statement_glob: str, runs_root: Path, target: str
) -> dict[str, Any]:
    by_date: dict[str, list[str]] = {}
    undated: list[str] = []
    for path in sorted(source_dir.glob(statement_glob)):
        end = peek_statement_period_end(path)
        if not end:
            undated.append(path.name)
            continue
        by_date.setdefault(end, []).append(str(path.resolve()))

    candidates: list[str] = []
    already_reconciled: list[str] = []
    no_existing_book: list[str] = []
    after_target: list[str] = []
    for end in sorted(by_date):
        if end > target:
            after_target.append(end)
            continue
        run_dir = runs_root / end
        if _completed_broker_chain(run_dir, end):
            already_reconciled.append(end)
        elif not _has_existing_book(run_dir):
            no_existing_book.append(end)
        else:
            candidates.append(end)
    return {
        "statement_dates": sorted(by_date),
        "statement_files_by_date": by_date,
        "candidate_dates": candidates,
        "already_reconciled": already_reconciled,
        "no_existing_book": no_existing_book,
        "after_target": after_target,
        "undated_files": undated,
    }


def reconciliation_command(
    config: Path,
    run_as_of: str,
    target: str,
    *,
    groups: tuple[str, ...],
    meta_name: str,
    late_holding_supplement: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        str(PORTFOLIO_RUNNER),
        "--config",
        str(config),
        "--as-of",
        run_as_of,
        "--groups",
        ",".join(groups),
        "--force",
        "--orchestration-meta-name",
        meta_name,
    ]
    if run_as_of < target:
        command.append("--historical-catchup")
    if late_holding_supplement:
        command.append("--late-holding-supplement")
    return command


def _selftest() -> int:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="late_ib_reconcile_"))
    source = root / "IB_reports"
    runs = root / "runs"
    source.mkdir()
    statement = source / "U_test_20260803.csv"
    statement.write_text('Statement,Data,Period,"August 3, 2026"\nOpen Positions,Header\n', encoding="utf-8")
    run = runs / "2026-08-03"
    (run / "final").mkdir(parents=True)
    (run / "stocks_scores.csv").write_text("ticker\nTEST\n", encoding="utf-8")
    (run / "final" / "final_target_weights.csv").write_text("ticker,weight\nTEST,1\n", encoding="utf-8")

    planned = plan_reconciliations(
        source_dir=source, statement_glob="U*.csv", runs_root=runs, target="2026-08-04"
    )
    assert planned["candidate_dates"] == ["2026-08-03"]
    command = reconciliation_command(
        root / "config.yaml",
        "2026-08-03",
        "2026-08-04",
        groups=("ledger",),
        meta_name=LEDGER_META_NAME,
    )
    assert "--historical-catchup" in command
    assert "ledger" in command
    assert LEDGER_META_NAME in command

    (run / "ledger").mkdir()
    write_manifest(
        run / "ledger" / "ledger_manifest.json",
        {"acceptance": "PASS", "run_as_of": "2026-08-03"},
    )
    grandfathered = plan_reconciliations(
        source_dir=source, statement_glob="U*.csv", runs_root=runs, target="2026-08-04"
    )
    assert grandfathered["already_reconciled"] == ["2026-08-03"]
    write_manifest(
        run / RECOVERY_META_NAME,
        {
            "acceptance": "FAIL",
            "run_as_of": "2026-08-03",
            "groups_completed": ["ledger"],
        },
    )
    retry = plan_reconciliations(
        source_dir=source, statement_glob="U*.csv", runs_root=runs, target="2026-08-04"
    )
    assert retry["candidate_dates"] == ["2026-08-03"]
    write_manifest(
        run / RECOVERY_META_NAME,
        {
            "acceptance": "PASS",
            "run_as_of": "2026-08-03",
            "groups_completed": sorted(REQUIRED_GROUPS),
        },
    )
    write_manifest(
        run / "final" / "final_manifest.json",
        {
            "acceptance": "PASS",
            "run_as_of": "2026-08-03",
            "ledger_as_of": "2026-08-03",
        },
    )
    planned_after = plan_reconciliations(
        source_dir=source, statement_glob="U*.csv", runs_root=runs, target="2026-08-04"
    )
    assert planned_after["candidate_dates"] == []
    assert planned_after["already_reconciled"] == ["2026-08-03"]
    print("LATE IB RECONCILIATION SELFTEST PASS: 8 checks")
    return 0


def main() -> int:
    args = parse_args()
    if args.selftest:
        return _selftest()

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    target = args.target or _latest_completed_trading_session()
    if date.fromisoformat(target).isoformat() != target:
        raise ValueError(f"--target must use YYYY-MM-DD, got {target!r}")

    source_dir = ensure_not_prod_path(
        resolve_path(
            cfg_get(config, "holdings_ledger.source_reports_dir", "../IB_reports"),
            base_dir=config_path.parent,
        ),
        label="IB source dir",
    )
    if not source_dir.is_dir():
        raise FileNotFoundError(f"IB source directory not found: {source_dir}")
    statement_glob = str(cfg_get(config, "holdings_ledger.statement_glob", "U*.csv") or "U*.csv")
    runs_root = paths.output_dir / "runs"
    plan = plan_reconciliations(
        source_dir=source_dir,
        statement_glob=statement_glob,
        runs_root=runs_root,
        target=target,
    )
    started = datetime.now(timezone.utc)
    manifest: dict[str, Any] = {
        "schema_version": "late_ib_statement_reconciliation_v1",
        "acceptance": "RUNNING",
        "started_at_utc": started.isoformat(timespec="seconds"),
        "target": target,
        "dry_run": bool(args.dry_run),
        "source_dir": str(source_dir),
        "statement_glob": statement_glob,
        **plan,
        "runs": [],
        "source_sha256": {
            Path(__file__).name: sha256_file(Path(__file__).resolve()),
            PORTFOLIO_RUNNER.name: sha256_file(PORTFOLIO_RUNNER),
            config_path.name: sha256_file(config_path),
        },
    }
    manifest_path = args.manifest.expanduser().resolve() if args.manifest else None

    failed = bool(plan["undated_files"])
    for run_as_of in plan["candidate_dates"]:
        ledger_command = reconciliation_command(
            config_path,
            run_as_of,
            target,
            groups=("ledger",),
            meta_name=LEDGER_META_NAME,
        )
        commands: list[list[str]] = [ledger_command]
        gaps: dict[str, list[str]] = {}
        if args.dry_run:
            rc = 0
            acceptance = "DRY_RUN"
            print(subprocess.list2cmdline(ledger_command))
            print(
                "post-ledger branch: refresh earnings/monitor/downstream only "
                "when a sealed holding is absent from either coverage universe"
            )
        else:
            rc = subprocess.run(ledger_command, cwd=PROJECT_ROOT, check=False).returncode
            ledger_meta = _load_json(runs_root / run_as_of / LEDGER_META_NAME)
            ledger_acceptance = str(ledger_meta.get("acceptance", ""))
            if rc != 0 or ledger_acceptance != "PASS" or not _accepted_ledger(
                runs_root / run_as_of, run_as_of
            ):
                failed = True
                acceptance = "FAIL_LEDGER"
            else:
                try:
                    gaps = late_holding_coverage_gaps(runs_root / run_as_of, run_as_of)
                except (FileNotFoundError, OSError, ValueError) as exc:
                    failed = True
                    acceptance = f"FAIL_COVERAGE_AUDIT:{type(exc).__name__}"
                else:
                    refresh_coverage = bool(
                        gaps["missing_from_earnings"] or gaps["missing_from_monitor"]
                    )
                    post_groups = (
                        COVERAGE_POST_LEDGER_GROUPS
                        if refresh_coverage
                        else BASE_POST_LEDGER_GROUPS
                    )
                    post_command = reconciliation_command(
                        config_path,
                        run_as_of,
                        target,
                        groups=post_groups,
                        meta_name=RECOVERY_META_NAME,
                        late_holding_supplement=refresh_coverage,
                    )
                    commands.append(post_command)
                    print(
                        "late holding coverage audit "
                        f"{run_as_of}: missing earnings={gaps['missing_from_earnings']} "
                        f"monitor={gaps['missing_from_monitor']}; "
                        f"post-ledger groups={list(post_groups)}"
                    )
                    rc = subprocess.run(post_command, cwd=PROJECT_ROOT, check=False).returncode
                    meta = _load_json(runs_root / run_as_of / RECOVERY_META_NAME)
                    acceptance = str(meta.get("acceptance", ""))
                    completed = {str(group) for group in meta.get("groups_completed", [])}
                    post_complete = set(post_groups).issubset(completed)
                    if (
                        rc != 0
                        or acceptance != "PASS"
                        or not post_complete
                        or not _completed_broker_chain(runs_root / run_as_of, run_as_of)
                    ):
                        failed = True
        manifest["runs"].append(
            {
                "run_as_of": run_as_of,
                "commands": commands,
                "coverage_gaps_before_refresh": gaps,
                "rc": rc,
                "acceptance": acceptance,
            }
        )

    manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.dry_run:
        manifest["acceptance"] = "PASS_DRY_RUN" if not failed else "FAIL_UNDATED_STATEMENT"
    else:
        manifest["acceptance"] = "FAIL" if failed else "PASS"
    if manifest_path is not None:
        write_manifest(manifest_path, manifest)
    print(
        "late IB reconciliation: "
        f"acceptance={manifest['acceptance']} candidates={plan['candidate_dates']} "
        f"already={plan['already_reconciled']} no_book={plan['no_existing_book']}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
