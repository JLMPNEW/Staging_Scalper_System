#!/usr/bin/env python3
"""Stage 11 - PIT score snapshot store: immutable, append-only archive of sealed Stage 1 contracts.

Modes (exactly one per invocation):
  --archive   (default) archive sealed live runs not yet in the store (daily hook + gap backfill)
  --replay    Stage 1 historical replay: for every date where EVERY enabled sector has a dated CSV,
              run 01 -> 02 -> 03 and archive the sealed result (provenance=reconstructed)
  --validate  store-wide integrity gates (tamper, coverage, drift, lockbox consistency)
  --supersede DATE  re-archive DATE from its current sealed run; prior snapshot bytes are preserved
              as <DATE>.superseded.<stamp> and the supersession is logged to data_quality_issues

Lockbox: docs/LOCKBOX_PROTOCOL.md is canonical; the config `stage11_lockbox` block must match it or
this script refuses to run. Archiving snapshots dated inside the sealed window is ALLOWED (capture is
mandatory); no outcome/label statistic is computed here (that is 66/16 territory). Per-date snapshot
dirs are the immutable units; snapshot_index.csv and the validation report are regenerated views.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import statistics
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import add_issue, connect, init_db, utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox as _load_lockbox  # noqa: E402
from portfolio_layer.scores.adapters import dated_candidates  # noqa: E402


LOGGER = logging.getLogger("pit_snapshot_store")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
STAGE1_SCRIPTS = [
    "01_collect_sector_scores.py",
    "02_calibrate_cross_sector_scores.py",
    "03_validate_score_contract.py",
]
STAGE11_REQUIRED_FIELDS = (
    "calibration_research_eligible",
    "calibration_research_reason",
    "calibration_sample_role",
    "stage1_sample_role",
    "oos_score_valid_flag",
)
RUN_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS score_snapshots (
    as_of_date TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    provenance TEXT NOT NULL,
    in_lockbox INTEGER NOT NULL,
    stage11_fields_complete INTEGER NOT NULL,
    acceptance TEXT NOT NULL,
    hard_gate_acceptance TEXT NOT NULL,
    contract_version TEXT,
    n_rows INTEGER NOT NULL,
    n_eligible INTEGER NOT NULL,
    n_research_eligible INTEGER NOT NULL,
    n_sectors INTEGER NOT NULL,
    stocks_scores_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    config_sha256 TEXT,
    protocol_sha256 TEXT NOT NULL,
    run_generated_at TEXT,
    archived_at TEXT NOT NULL,
    snapshot_dir TEXT NOT NULL,
    sector_stats_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_input_hashes (
    as_of_date TEXT NOT NULL,
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    rows INTEGER,
    PRIMARY KEY (as_of_date, file_name)
);
"""

INSERT_SNAPSHOT_SQL = """
INSERT INTO score_snapshots(
    as_of_date, mode, provenance, in_lockbox, stage11_fields_complete, acceptance,
    hard_gate_acceptance, contract_version, n_rows, n_eligible, n_research_eligible, n_sectors,
    stocks_scores_sha256, manifest_sha256, config_sha256, protocol_sha256,
    run_generated_at, archived_at, snapshot_dir, sector_stats_json)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

INDEX_FIELDS = [
    "as_of_date", "provenance", "mode", "in_lockbox", "stage11_fields_complete", "acceptance",
    "n_rows", "n_eligible", "n_research_eligible", "n_sectors", "stocks_scores_sha256",
    "contract_version", "archived_at",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 PIT score snapshot store (archive/replay/validate).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--db", type=Path, default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--archive", action="store_true", help="Archive sealed live runs missing from the store (default).")
    mode.add_argument("--replay", action="store_true", help="Replay Stage 1 for historical dates with full sector coverage.")
    mode.add_argument("--validate", action="store_true", help="Run store-wide integrity gates.")
    mode.add_argument("--supersede", type=iso_date_arg, default=None, metavar="DATE",
                      help="Re-archive DATE from its current sealed run, preserving the prior snapshot bytes.")
    p.add_argument("--from-date", type=iso_date_arg, default=None, help="Replay range start (default: dev_window_start).")
    p.add_argument("--to-date", type=iso_date_arg, default=None, help="Replay range end (default: today).")
    p.add_argument("--limit", type=int, default=0, help="Replay at most N dates this invocation (0 = no cap).")
    p.add_argument("--dry-run", action="store_true", help="Replay: report coverage and the plan without running.")
    return p.parse_args()


def _sector_stats(rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        pipe = str(r.get("source_pipeline", "")).strip()
        if not pipe:
            continue
        item = by.setdefault(pipe, {"rows": 0, "eligible": 0, "research_eligible": 0, "natives": [], "source_asof": ""})
        item["rows"] += 1
        item["eligible"] += 1 if str(r.get("investable_eligible", "")).strip() == "1" else 0
        item["research_eligible"] += 1 if str(r.get("calibration_research_eligible", "")).strip() == "1" else 0
        try:
            item["natives"].append(float(r.get("native_score", "")))
        except (TypeError, ValueError):
            pass
        if not item["source_asof"]:
            item["source_asof"] = str(r.get("source_asof_date", "")).strip()
    return {
        pipe: {
            "rows": v["rows"],
            "eligible": v["eligible"],
            "research_eligible": v["research_eligible"],
            "native_median": round(statistics.median(v["natives"]), 4) if v["natives"] else None,
            "source_asof": v["source_asof"],
        }
        for pipe, v in sorted(by.items())
    }


def _provenance(*, mode: str, as_of: str, generated_at: str, live_lag_days: int) -> str:
    """live = the run was generated within N days of its as-of (daily operation); else reconstructed."""
    if mode == "replay":
        return "reconstructed"
    try:
        gen = date.fromisoformat(str(generated_at)[:10])
        lag = (gen - date.fromisoformat(as_of)).days
    except ValueError:
        return "reconstructed"
    return "live" if 0 <= lag <= live_lag_days else "reconstructed"


def _insert_snapshot(conn, meta: dict[str, Any], snapshot_dir: Path) -> None:
    input_rows = [(meta["as_of_date"], "stocks_scores.csv", meta["stocks_scores_sha256"], meta["n_rows"])]
    if meta.get("config_sha256"):
        input_rows.append((meta["as_of_date"], "config.yaml", meta["config_sha256"], None))
    for name, info in sorted((meta.get("raw_inputs_sha256") or {}).items()):
        input_rows.append((meta["as_of_date"], f"raw/{name}", str((info or {}).get("sha256", "")),
                           int((info or {}).get("rows") or 0)))
    with conn:
        conn.execute(INSERT_SNAPSHOT_SQL, (
            meta["as_of_date"], meta["mode"], meta["provenance"], meta["in_lockbox"],
            meta["stage11_fields_complete"], meta["acceptance"], meta["hard_gate_acceptance"],
            meta["contract_version"], meta["n_rows"], meta["n_eligible"], meta["n_research_eligible"],
            meta["n_sectors"], meta["stocks_scores_sha256"], meta["manifest_sha256"],
            meta["config_sha256"], meta["protocol_sha256"], meta["run_generated_at"],
            meta["archived_at"], str(snapshot_dir), json.dumps(meta["sector_stats"], sort_keys=True),
        ))
        conn.executemany(
            "INSERT OR REPLACE INTO snapshot_input_hashes(as_of_date, file_name, sha256, rows) VALUES (?,?,?,?)",
            input_rows,
        )


def _archive_run(
    *,
    runs_root: Path,
    store_dir: Path,
    conn,
    as_of: str,
    lockbox: dict[str, Any],
    mode: str,
    live_lag_days: int,
    script_sha: str,
) -> tuple[str, str]:
    """Archive one sealed Stage 1 run. Returns (status, detail)."""
    run_dir = runs_root / as_of
    manifest_path = run_dir / "manifest.json"
    scores_path = run_dir / "stocks_scores.csv"
    if not manifest_path.exists() or not scores_path.exists():
        return "invalid", "missing manifest.json or stocks_scores.csv"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    acceptance = str(manifest.get("acceptance", ""))
    hard = str(manifest.get("hard_gate_acceptance", ""))
    if hard != "PASS" or acceptance not in {"PASS", "PASS_WITH_DEFERRED"}:
        return "failed_run", f"acceptance={acceptance} hard_gate={hard}"
    recorded_sha = str(((manifest.get("files") or {}).get("stocks_scores.csv") or {}).get("sha256", ""))
    actual_sha = sha256_file(scores_path)
    if not recorded_sha or recorded_sha != actual_sha:
        return "stale_run", f"stocks_scores sha {actual_sha[:12]} != sealed manifest {recorded_sha[:12]}"

    existing = conn.execute(
        "SELECT stocks_scores_sha256 FROM score_snapshots WHERE as_of_date = ?", (as_of,)
    ).fetchone()
    if existing is not None:
        if str(existing["stocks_scores_sha256"]) == actual_sha:
            return "already_current", actual_sha[:12]
        return "mismatch", (
            f"archived {str(existing['stocks_scores_sha256'])[:12]} != current run {actual_sha[:12]}; "
            "immutable store refuses overwrite (review, then --supersede)"
        )

    rows = read_csv(scores_path)
    if not rows:
        return "invalid", "stocks_scores.csv has no rows"
    header = set(rows[0].keys())
    missing_fields = [f for f in STAGE11_REQUIRED_FIELDS if f not in header]
    if missing_fields and mode == "replay":
        return "invalid", f"replayed contract missing Stage 11 fields {missing_fields} (Stage 1 code bug)"
    stats = _sector_stats(rows)
    bad_sources = [p for p, s in stats.items() if s["source_asof"] and s["source_asof"] > as_of]
    if bad_sources:
        return "invalid", f"future source_asof for {bad_sources}"

    final_dir = store_dir / as_of
    if final_dir.exists():
        # Crash-recovery: adopt a fully-written orphan dir if its bytes match this run; else refuse.
        meta_path = final_dir / "snapshot_meta.json"
        if meta_path.exists():
            orphan = json.loads(meta_path.read_text(encoding="utf-8"))
            if (
                str(orphan.get("stocks_scores_sha256", "")) == actual_sha
                and (final_dir / "stocks_scores.csv").exists()
                and sha256_file(final_dir / "stocks_scores.csv") == actual_sha
            ):
                _insert_snapshot(conn, orphan, final_dir)
                return "adopted", f"orphan snapshot dir re-indexed sha={actual_sha[:12]}"
        return "mismatch", f"store dir exists without a matching index row: {final_dir}"

    staging = store_dir / ".staging" / as_of
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copy2(scores_path, staging / "stocks_scores.csv")
    shutil.copy2(manifest_path, staging / "manifest.json")

    generated_at = str(manifest.get("generated_at", ""))
    meta = {
        "stage": "stage11_pit_snapshot",
        "as_of_date": as_of,
        "archived_at": utc_now(),
        "mode": mode,
        "provenance": _provenance(mode=mode, as_of=as_of, generated_at=generated_at, live_lag_days=live_lag_days),
        "in_lockbox": 1 if as_of >= lockbox["sealed_start"] else 0,
        "stage11_fields_complete": 0 if missing_fields else 1,
        "stage11_fields_missing": missing_fields,
        "acceptance": acceptance,
        "hard_gate_acceptance": hard,
        "contract_version": str(manifest.get("contract_version", "")),
        "run_generated_at": generated_at,
        "n_rows": len(rows),
        "n_eligible": sum(s["eligible"] for s in stats.values()),
        "n_research_eligible": sum(s["research_eligible"] for s in stats.values()),
        "n_sectors": len(stats),
        "sector_stats": stats,
        "stocks_scores_sha256": actual_sha,
        "manifest_sha256": sha256_file(manifest_path),
        "config_sha256": str(((manifest.get("provenance") or {}).get("config_yaml") or {}).get("sha256", "")),
        "protocol_sha256": lockbox["protocol_sha256"],
        "source_run_dir": str(run_dir),
        "store_script_sha256": script_sha,
        "raw_inputs_sha256": {name: dict(info or {}) for name, info in sorted((manifest.get("raw") or {}).items())},
    }
    write_manifest(staging / "snapshot_meta.json", meta)
    staging.rename(final_dir)
    _insert_snapshot(conn, meta, final_dir)
    note = f"{meta['provenance']} rows={len(rows)} sha={actual_sha[:12]}"
    if missing_fields:
        note += f" LEGACY(no {missing_fields})"
    return "archived", note


def _sealed_run_dates(runs_root: Path) -> list[str]:
    if not runs_root.exists():
        return []
    return sorted(
        p.name for p in runs_root.iterdir()
        if p.is_dir() and RUN_DATE_RE.match(p.name) and (p / "manifest.json").exists()
    )


def _write_index(conn, store_dir: Path) -> None:
    rows = conn.execute(
        f"SELECT {', '.join(INDEX_FIELDS)} FROM score_snapshots ORDER BY as_of_date"
    ).fetchall()
    write_csv(store_dir / "snapshot_index.csv", INDEX_FIELDS, [dict(r) for r in rows])


def _cmd_archive(*, runs_root: Path, store_dir: Path, conn, lockbox: dict[str, Any],
                 live_lag_days: int, script_sha: str) -> int:
    shutil.rmtree(store_dir / ".staging", ignore_errors=True)
    tallies: dict[str, list[str]] = {}
    for as_of in _sealed_run_dates(runs_root):
        status, detail = _archive_run(
            runs_root=runs_root, store_dir=store_dir, conn=conn, as_of=as_of,
            lockbox=lockbox, mode="archive", live_lag_days=live_lag_days, script_sha=script_sha,
        )
        tallies.setdefault(status, []).append(as_of)
        level = LOGGER.info if status in {"archived", "already_current", "adopted"} else LOGGER.warning
        if status != "already_current":
            level("[%s] %s -- %s", status, as_of, detail)
    _write_index(conn, store_dir)
    summary = {status: dates for status, dates in sorted(tallies.items())}
    n_current = len(tallies.get("already_current", []))
    LOGGER.info(
        "Archive complete: archived=%s adopted=%s already_current=%d failed_run=%s stale_run=%s mismatch=%s invalid=%s",
        tallies.get("archived") or "none", tallies.get("adopted") or "none", n_current,
        tallies.get("failed_run") or "none", tallies.get("stale_run") or "none",
        tallies.get("mismatch") or "none", tallies.get("invalid") or "none",
    )
    bad = [s for s in ("stale_run", "mismatch", "invalid") if summary.get(s)]
    return 1 if bad else 0


def _run_stage1_chain(as_of: str, config_path: Path, db_arg: Path | None) -> tuple[bool, str]:
    for script in STAGE1_SCRIPTS:
        cmd = [sys.executable, str(PACKAGE_ROOT / "scores" / script), "--as-of", as_of, "--config", str(config_path)]
        if db_arg is not None:
            cmd += ["--db", str(db_arg)]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return False, f"{script} rc={proc.returncode}: {' | '.join(tail)}"
    return True, "ok"


def _cmd_replay(*, config: dict[str, Any], config_path: Path, runs_root: Path, store_dir: Path, conn,
                lockbox: dict[str, Any], live_lag_days: int, script_sha: str,
                from_date: str | None, to_date: str | None, limit: int, dry_run: bool,
                db_arg: Path | None) -> int:
    shutil.rmtree(store_dir / ".staging", ignore_errors=True)
    sector_root = resolve_path(
        cfg_get(config, "score_contract.sector_output_root", "../output"), base_dir=config_path.parent
    )
    sectors = [s for s in cfg_get(config, "score_contract.sectors", []) if bool(s.get("enabled", True))]
    non_dated = [str(s.get("model_family")) for s in sectors if str(s.get("file_mode", "flat")) != "dated"]
    if non_dated:
        LOGGER.error("Replay needs dated sector sources; non-dated: %s", non_dated)
        return 1

    lo = from_date or lockbox["dev_window_start"]
    hi = to_date or date.today().isoformat()
    avail: dict[str, set[str]] = {}
    for cfg in sectors:
        pipe = str(cfg.get("model_family"))
        dates = {f"{d[:4]}-{d[4:6]}-{d[6:]}" for d, _ in dated_candidates(cfg, sector_root)}
        avail[pipe] = {d for d in dates if lo <= d <= hi}
        first = min(avail[pipe]) if avail[pipe] else "-"
        last = max(avail[pipe]) if avail[pipe] else "-"
        LOGGER.info("coverage %-24s dates=%-4d range=[%s .. %s]", pipe, len(avail[pipe]), first, last)

    union = set().union(*avail.values()) if avail else set()
    inter = set.intersection(*avail.values()) if avail else set()
    LOGGER.info("coverage union=%d intersection=%d (range %s..%s)", len(union), len(inter), lo, hi)
    for d in sorted(union - inter)[:10]:
        missing = sorted(p for p, ds in avail.items() if d not in ds)
        LOGGER.info("  incomplete %s missing=%s", d, missing)
    if len(union - inter) > 10:
        LOGGER.info("  ... %d more incomplete dates", len(union - inter) - 10)

    archived_dates = {str(r["as_of_date"]) for r in conn.execute("SELECT as_of_date FROM score_snapshots")}
    plan: list[str] = []
    partial_blocked: list[str] = []
    sealed_existing = 0
    for d in sorted(inter):
        if d in archived_dates:
            continue
        run_dir = runs_root / d
        if (run_dir / "manifest.json").exists():
            sealed_existing += 1  # picked up by --archive, not replay
            continue
        if run_dir.exists() and any(run_dir.iterdir()):
            partial_blocked.append(d)
            continue
        plan.append(d)
    if partial_blocked:
        LOGGER.error("Partial run dirs block replay (clean or re-seal them manually): %s", partial_blocked[:10])
    if limit > 0:
        plan = plan[:limit]
    LOGGER.info(
        "Replay plan: %d dates%s (already_archived=%d, sealed_runs_for_archive_mode=%d, partial_blocked=%d)",
        len(plan), f" (limit {limit})" if limit else "", len(inter & archived_dates), sealed_existing,
        len(partial_blocked),
    )
    if dry_run:
        LOGGER.info("Dry run: first=%s last=%s", plan[0] if plan else "-", plan[-1] if plan else "-")
        return 1 if partial_blocked else 0

    replayed: list[str] = []
    failed: list[str] = []
    for d in plan:
        ok, detail = _run_stage1_chain(d, config_path, db_arg)
        if not ok:
            failed.append(d)
            LOGGER.error("[replay_failed] %s -- %s", d, detail)
            continue
        status, note = _archive_run(
            runs_root=runs_root, store_dir=store_dir, conn=conn, as_of=d,
            lockbox=lockbox, mode="replay", live_lag_days=live_lag_days, script_sha=script_sha,
        )
        if status == "archived":
            replayed.append(d)
            LOGGER.info("[replayed] %s -- %s", d, note)
        else:
            failed.append(d)
            LOGGER.error("[archive_%s] %s -- %s", status, d, note)
    _write_index(conn, store_dir)
    LOGGER.info("Replay complete: replayed=%d failed=%d remaining_plan=%d",
                len(replayed), len(failed), max(0, len(plan) - len(replayed) - len(failed)))
    return 1 if failed or partial_blocked else 0


def _cmd_supersede(*, runs_root: Path, store_dir: Path, conn, lockbox: dict[str, Any],
                   live_lag_days: int, script_sha: str, as_of: str) -> int:
    row = conn.execute("SELECT * FROM score_snapshots WHERE as_of_date = ?", (as_of,)).fetchone()
    if row is None:
        LOGGER.error("No archived snapshot for %s; plain --archive handles new dates", as_of)
        return 1
    if not (runs_root / as_of / "manifest.json").exists():
        LOGGER.error("No sealed run at runs/%s to supersede from", as_of)
        return 1
    old_dir = Path(str(row["snapshot_dir"]))
    old_sha = str(row["stocks_scores_sha256"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    retired = store_dir / f"{as_of}.superseded.{stamp}"
    if old_dir.exists():
        old_dir.rename(retired)
    with conn:
        conn.execute("DELETE FROM score_snapshots WHERE as_of_date = ?", (as_of,))
        conn.execute("DELETE FROM snapshot_input_hashes WHERE as_of_date = ?", (as_of,))
    add_issue(
        conn, stage="stage11_snapshot_store", issue_type="snapshot_superseded",
        detail=f"as_of={as_of}; old_sha={old_sha[:16]}; retired_dir={retired.name}", severity="warning",
    )
    status, note = _archive_run(
        runs_root=runs_root, store_dir=store_dir, conn=conn, as_of=as_of,
        lockbox=lockbox, mode="archive", live_lag_days=live_lag_days, script_sha=script_sha,
    )
    _write_index(conn, store_dir)
    if status != "archived":
        LOGGER.error("Supersede re-archive failed [%s]: %s (old bytes kept at %s)", status, note, retired.name)
        return 1
    LOGGER.info("Superseded %s: old bytes kept at %s; new %s", as_of, retired.name, note)
    return 0


def _csv_header(path: Path) -> set[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        first = handle.readline().strip("\r\n")
    return {col.strip() for col in first.split(",")} if first else set()


def _cmd_validate(*, config: dict[str, Any], runs_root: Path, store_dir: Path, conn,
                  lockbox: dict[str, Any]) -> int:
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    rows = [dict(r) for r in conn.execute("SELECT * FROM score_snapshots ORDER BY as_of_date").fetchall()]
    expected = sorted(
        str(s.get("model_family")) for s in cfg_get(config, "score_contract.sectors", [])
        if bool(s.get("enabled", True))
    )
    rec("lockbox_config_protocol_consistent", "PASS",
        f"protocol sha={lockbox['protocol_sha256'][:12]} sealed_start={lockbox['sealed_start']} "
        f"opened={lockbox['lockbox_opened']}")

    if not rows:
        rec("store_populated", "WARN", "store is empty; run --archive / --replay first")
        write_csv(store_dir / "validation" / "snapshot_store_validation.csv", ["check", "status", "detail"], checks)
        for c in checks:
            LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
        return 0

    tamper, run_diverged, bad_accept, bad_sectors, bad_fields, bad_sources, bad_dates = [], [], [], [], [], [], []
    legacy, drifted_sectors = [], []
    for r in rows:
        as_of = str(r["as_of_date"])
        if not RUN_DATE_RE.match(as_of):
            bad_dates.append(as_of)
            continue
        snap_dir = Path(str(r["snapshot_dir"]))
        scores = snap_dir / "stocks_scores.csv"
        manifest = snap_dir / "manifest.json"
        meta = snap_dir / "snapshot_meta.json"
        if not (snap_dir.exists() and scores.exists() and manifest.exists() and meta.exists()):
            tamper.append(f"{as_of}:missing_files")
            continue
        if sha256_file(scores) != str(r["stocks_scores_sha256"]):
            tamper.append(f"{as_of}:stocks_scores_hash")
        if sha256_file(manifest) != str(r["manifest_sha256"]):
            tamper.append(f"{as_of}:manifest_hash")
        run_scores = runs_root / as_of / "stocks_scores.csv"
        if run_scores.exists() and sha256_file(run_scores) != str(r["stocks_scores_sha256"]):
            run_diverged.append(as_of)
        if str(r["hard_gate_acceptance"]) != "PASS" or str(r["acceptance"]) not in {"PASS", "PASS_WITH_DEFERRED"}:
            bad_accept.append(f"{as_of}:{r['acceptance']}/{r['hard_gate_acceptance']}")
        stats = json.loads(str(r["sector_stats_json"]))
        if sorted(stats) != expected:
            # a reconstruction is built NOW from the current config, so a missing sector is an
            # integrity failure; a LIVE capture predating a later-added sector is honest history
            # (retro-demanding the new sector would be anachronistic) -> drift warning instead
            if str(r["provenance"]) == "live" and set(stats).issubset(expected):
                drifted_sectors.append(f"{as_of}:missing={sorted(set(expected) - set(stats))}")
            else:
                bad_sectors.append(f"{as_of}:{sorted(stats)}")
        for pipe, s in stats.items():
            src = str(s.get("source_asof") or "")
            if src and src > as_of:
                bad_sources.append(f"{as_of}:{pipe}:{src}")
        if int(r["stage11_fields_complete"]) != 1:
            legacy.append(as_of)
        elif not set(STAGE11_REQUIRED_FIELDS).issubset(_csv_header(scores)):
            bad_fields.append(as_of)

    known = {str(r["as_of_date"]) for r in rows}
    orphans = sorted(
        p.name for p in store_dir.iterdir()
        if p.is_dir() and RUN_DATE_RE.match(p.name) and p.name not in known
    ) if store_dir.exists() else []

    rec("snapshot_hashes_immutable", "PASS" if not tamper else "FAIL",
        f"{len(rows)} snapshots recompute" if not tamper else f"tampered/incomplete: {tamper[:8]}")
    rec("no_orphan_snapshot_dirs", "PASS" if not orphans else "FAIL",
        "all dated dirs indexed" if not orphans else f"orphans: {orphans[:8]}")
    rec("runs_tree_matches_archive", "PASS" if not run_diverged else "FAIL",
        "live runs byte-match archived snapshots" if not run_diverged else
        f"runs rewritten after archive (review, then --supersede): {run_diverged[:8]}")
    rec("snapshots_accepted", "PASS" if not bad_accept else "FAIL",
        "all snapshots sealed PASS" if not bad_accept else f"{bad_accept[:8]}")
    rec("sector_coverage_complete", "PASS" if not bad_sectors else "FAIL",
        f"every snapshot covers {expected}" if not bad_sectors else f"{bad_sectors[:8]}")
    rec("sector_set_drift_live_captures", "PASS" if not drifted_sectors else "WARN",
        "none" if not drifted_sectors else
        f"{len(drifted_sectors)} live captures predate a later-added sector: {drifted_sectors[:8]}")
    rec("no_future_source_dates", "PASS" if not bad_sources else "FAIL",
        "sector source dates PIT-consistent" if not bad_sources else f"{bad_sources[:8]}")
    rec("valid_snapshot_dates", "PASS" if not bad_dates else "FAIL",
        "ISO dates" if not bad_dates else f"{bad_dates[:8]}")
    rec("stage11_fields_present", "PASS" if not bad_fields else "FAIL",
        "flagged-complete snapshots carry Stage 11 fields" if not bad_fields else f"{bad_fields[:8]}")
    rec("legacy_contract_snapshots", "PASS" if not legacy else "WARN",
        "none" if not legacy else f"{len(legacy)} snapshots predate Stage 11 contract fields: {legacy[:8]}")

    drift_warn = float(cfg_get(config, "snapshot_store.native_median_drift_warn", 20.0))
    drop_frac = float(cfg_get(config, "snapshot_store.row_count_drop_warn_fraction", 0.5))
    drift, drops = [], []
    prev_stats: dict[str, dict[str, Any]] = {}
    for r in rows:
        stats = json.loads(str(r["sector_stats_json"]))
        for pipe, s in stats.items():
            prev = prev_stats.get(pipe)
            if prev:
                m0, m1 = prev.get("native_median"), s.get("native_median")
                if m0 is not None and m1 is not None and abs(float(m1) - float(m0)) > drift_warn:
                    drift.append(f"{r['as_of_date']}:{pipe}:{m0}->{m1}")
                if int(prev.get("rows") or 0) > 0 and int(s.get("rows") or 0) < drop_frac * int(prev["rows"]):
                    drops.append(f"{r['as_of_date']}:{pipe}:{prev['rows']}->{s['rows']}")
            prev_stats[pipe] = s
    rec("native_scale_drift", "PASS" if not drift else "WARN",
        f"no sector median jump > {drift_warn}" if not drift else f"{drift[:8]}")
    rec("sector_row_count_stability", "PASS" if not drops else "WARN",
        f"no sector row drop > {1 - drop_frac:.0%}" if not drops else f"{drops[:8]}")

    n_lock = sum(1 for r in rows if int(r["in_lockbox"]) == 1)
    protocol_shas = {str(r["protocol_sha256"])[:12] for r in rows}
    rec("lockbox_partition", "PASS",
        f"dev={len(rows) - n_lock} lockbox={n_lock} (capture-only); protocol_shas_seen={sorted(protocol_shas)}")

    write_csv(store_dir / "validation" / "snapshot_store_validation.csv", ["check", "status", "detail"], checks)
    _write_index(conn, store_dir)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    passed = all(c["status"] in {"PASS", "WARN"} for c in checks)
    if passed:
        LOGGER.info("SNAPSHOT STORE VALIDATION: PASS (%d snapshots, %d in lockbox)", len(rows), n_lock)
        return 0
    LOGGER.error("SNAPSHOT STORE VALIDATION: FAIL")
    return 1


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        db_path = resolve_database_path(paths, args.db)
        lockbox = _load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    runs_root = paths.output_dir / "runs"
    store_dir = paths.output_dir / str(cfg_get(config, "snapshot_store.dir", "snapshot_store"))
    store_dir.mkdir(parents=True, exist_ok=True)
    live_lag_days = int(cfg_get(config, "snapshot_store.live_max_generation_lag_days", 5))
    script_sha = sha256_file(Path(__file__).resolve())

    with connect(db_path) as conn:
        init_db(conn)
        with conn:
            conn.executescript(SNAPSHOT_DDL)
        if args.validate:
            return _cmd_validate(config=config, runs_root=runs_root, store_dir=store_dir, conn=conn, lockbox=lockbox)
        if args.supersede:
            return _cmd_supersede(runs_root=runs_root, store_dir=store_dir, conn=conn, lockbox=lockbox,
                                  live_lag_days=live_lag_days, script_sha=script_sha, as_of=args.supersede)
        if args.replay:
            return _cmd_replay(config=config, config_path=config_path, runs_root=runs_root, store_dir=store_dir,
                               conn=conn, lockbox=lockbox, live_lag_days=live_lag_days, script_sha=script_sha,
                               from_date=args.from_date, to_date=args.to_date, limit=args.limit,
                               dry_run=args.dry_run, db_arg=args.db)
        return _cmd_archive(runs_root=runs_root, store_dir=store_dir, conn=conn, lockbox=lockbox,
                            live_lag_days=live_lag_days, script_sha=script_sha)


if __name__ == "__main__":
    raise SystemExit(main())
