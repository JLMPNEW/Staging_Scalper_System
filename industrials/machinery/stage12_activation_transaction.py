from __future__ import annotations

import csv
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from industrials.core.config import cfg_get, load_yaml, resolve_path
from industrials.machinery.scoring import file_sha256, read_rows, write_json_atomic
from industrials.machinery.stage12_activation import (
    ACTIVATION_STATUS_FULLY_VALIDATED,
    PRODUCTION_POLICY_STATUS_ACTIVE,
    ActivationPaths,
    _activation_date_checks,
    _write_bytes_atomic,
    activate_candidate,
    prepare_activation_candidate,
    production_policy_source_hashes,
    rollback_published_candidate,
)
from industrials.machinery.stage12_governance import (
    ACTIVATION_MODE_INITIAL,
    ACTIVATION_MODE_REPLACE_ACTIVE,
    MODEL_FAMILY,
    Stage12Paths,
    _portfolio_family,
    machinery_portfolio_policy_fingerprint,
    portfolio_activation_fingerprint,
    validate_stage12_lock,
)
from industrials.machinery.stage8_calibration import parse_date, utc_now
from portfolio_layer.core.paths import resolve_runtime_paths


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GLOBAL_ORCHESTRATION_LOCK = PROJECT_ROOT / "orchestration" / ".orchestrator.lock"
MASTER_PID_ENV = "STAGING_ORCHESTRATOR_PID"
PORTFOLIO_RUNNER = (
    PROJECT_ROOT
    / "portfolio_layer"
    / "orchestration"
    / "18_run_portfolio_pipeline.py"
)
MACHINERY_REFRESH_RUNNER = (
    PROJECT_ROOT
    / "industrials"
    / "machinery"
    / "scripts"
    / "17_run_machinery_refresh_pipeline.py"
)
REQUIRED_PORTFOLIO_GROUPS = frozenset(
    {
        "scores",
        "risk",
        "optimizer",
        "costs",
        "rotation",
        "macro",
        "bl",
        "sleeves",
        "payout",
        "governor",
        "final",
    }
)
REUSABLE_PORTFOLIO_PREFIX_GROUPS = frozenset(
    {
        "scores",
        "risk",
        "optimizer",
        "costs",
        "rotation",
        "macro",
        "bl",
        "sleeves",
    }
)
PORTFOLIO_RESUME_GROUPS = (
    "ledger",
    "exits",
    "payout",
    "governor",
    "final",
    "earnings",
)
PORTFOLIO_PREFIX_MANIFESTS = {
    "scores": "manifest.json",
    "risk": "risk/risk_manifest.json",
    "optimizer": "optimizer/optimizer_manifest.json",
    "costs": "costs/cost_manifest.json",
    "rotation": "rotation/rotation_manifest.json",
    "macro": "macro/macro_manifest.json",
    "bl": "blacklitterman/bl_manifest.json",
    "sleeves": "sleeves/sleeve_manifest.json",
}
ACCEPTED_ORCHESTRATION_RESULTS = frozenset(
    {"PASS", "PASS_WITH_ADVISORY_WARNINGS"}
)
CONFIG_BACKUP_NAME = "portfolio_config_shadow_backup.yaml"
TRANSACTION_RESULT_NAME = "machinery_activation_transaction.json"
RESUME_EVIDENCE_NAME = "portfolio_prefix_resume_evidence.json"
MARKET_TIMEZONE = ZoneInfo("America/New_York")
MARKET_FINAL_TIME_ET = time(17, 0)


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    return_code: int
    log_path: Path


class ActivationOrchestrationLock:
    """Fail-closed owner of the master orchestration lock during activation."""

    def __init__(self, path: Path = GLOBAL_ORCHESTRATION_LOCK) -> None:
        self.path = path
        self._descriptor: int | None = None
        self._children: set[int] = set()

    def __enter__(self) -> ActivationOrchestrationLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                f"Another orchestrator owns {self.path}; activation refused"
            ) from exc
        self._persist()
        return self

    def _persist(self) -> None:
        if self._descriptor is None:
            return
        payload = (
            f"pid={os.getpid()} started_utc={utc_now()}\n"
            f"children={','.join(str(pid) for pid in sorted(self._children))}\n"
        ).encode("utf-8")
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        os.ftruncate(self._descriptor, 0)
        os.write(self._descriptor, payload)
        os.fsync(self._descriptor)

    def register_child(self, pid: int) -> None:
        self._children.add(pid)
        self._persist()

    def unregister_child(self, pid: int) -> None:
        self._children.discard(pid)
        self._persist()

    def __exit__(self, *_args: object) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
            if re.search(rf"^pid={os.getpid()}\b", text, flags=re.MULTILINE):
                self.path.unlink(missing_ok=True)
        except FileNotFoundError:
            return


def _truthy(value: object) -> bool:
    return str(value if value is not None else "").strip().lower() in {
        "1",
        "true",
        "t",
        "yes",
        "y",
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_wall_clock(
    asof: str,
    *,
    today: date | None = None,
) -> None:
    target = parse_date(asof, field="activation_asof")
    current = today or datetime.now(MARKET_TIMEZONE).date()
    if target > current:
        raise ValueError(
            f"Activation date {asof} is in the future relative to "
            f"{current.isoformat()}"
        )


def validate_completed_session(
    asof: str,
    *,
    now_et: datetime | None = None,
) -> None:
    target = parse_date(asof, field="activation_asof")
    current = now_et or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)
    if target == current.date() and current.time() < MARKET_FINAL_TIME_ET:
        raise ValueError(
            f"Activation date {asof} is not a completed session before "
            f"{MARKET_FINAL_TIME_ET.isoformat(timespec='minutes')} ET"
        )


def _replace_family_required(
    lines: list[str],
    *,
    required: bool,
) -> None:
    start: int | None = None
    item_indent = -1
    for index, line in enumerate(lines):
        match = re.match(
            r"^(\s*)-\s*model_family:\s*machinery\s*(?:#.*)?(?:\r?\n)?$",
            line,
        )
        if match:
            if start is not None:
                raise ValueError("Duplicate machinery score-contract entries")
            start = index
            item_indent = len(match.group(1))
    if start is None:
        raise ValueError("Machinery score-contract entry is missing")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(\s*)-\s*model_family:", lines[index])
        if match and len(match.group(1)) == item_indent:
            end = index
            break
    matches = [
        index
        for index in range(start + 1, end)
        if re.match(r"^\s*required:\s*(?:true|false)\b", lines[index])
    ]
    if len(matches) != 1:
        raise ValueError(
            "Machinery score-contract required setting is missing or ambiguous"
        )
    index = matches[0]
    replacement = "true" if required else "false"
    lines[index] = re.sub(
        r"(?P<prefix>^\s*required:\s*)(?:true|false)(?P<suffix>\s*(?:#.*)?(?:\r?\n)?$)",
        rf"\g<prefix>{replacement}\g<suffix>",
        lines[index],
    )


def _replace_optimizer_cap(lines: list[str], *, cap: float) -> None:
    optimizer_start: int | None = None
    for index, line in enumerate(lines):
        if re.match(r"^optimizer:\s*(?:#.*)?(?:\r?\n)?$", line):
            if optimizer_start is not None:
                raise ValueError("Duplicate optimizer config sections")
            optimizer_start = index
    if optimizer_start is None:
        raise ValueError("Optimizer config section is missing")
    optimizer_end = len(lines)
    for index in range(optimizer_start + 1, len(lines)):
        if re.match(r"^[^\s#][^:]*:", lines[index]):
            optimizer_end = index
            break
    caps_start: int | None = None
    caps_indent = -1
    for index in range(optimizer_start + 1, optimizer_end):
        match = re.match(r"^(\s+)sector_weight_caps:\s*(?:#.*)?(?:\r?\n)?$", lines[index])
        if match:
            if caps_start is not None:
                raise ValueError("Duplicate optimizer sector_weight_caps sections")
            caps_start = index
            caps_indent = len(match.group(1))
    if caps_start is None:
        raise ValueError("optimizer.sector_weight_caps is missing")
    caps_end = optimizer_end
    for index in range(caps_start + 1, optimizer_end):
        match = re.match(r"^(\s+)[A-Za-z_][A-Za-z0-9_]*:", lines[index])
        if match and len(match.group(1)) <= caps_indent:
            caps_end = index
            break
    matches = [
        index
        for index in range(caps_start + 1, caps_end)
        if re.match(r"^\s*machinery:\s*[-+0-9.eE]+\b", lines[index])
    ]
    if len(matches) != 1:
        raise ValueError(
            "optimizer.sector_weight_caps.machinery is missing or ambiguous"
        )
    index = matches[0]
    lines[index] = re.sub(
        r"(?P<prefix>^\s*machinery:\s*)[-+0-9.eE]+(?P<suffix>\s*(?:#.*)?(?:\r?\n)?$)",
        rf"\g<prefix>{cap:.2f}\g<suffix>",
        lines[index],
    )


def render_portfolio_activation_config(
    original: bytes,
    *,
    required: bool,
    cap: float,
) -> bytes:
    """Change only the reviewed machinery activation fields."""
    text = original.decode("utf-8")
    lines = text.splitlines(keepends=True)
    _replace_family_required(lines, required=required)
    _replace_optimizer_cap(lines, cap=cap)
    return "".join(lines).encode("utf-8")


def commit_portfolio_activation_config(
    portfolio_config_path: Path,
    *,
    cap: float,
) -> dict[str, Any]:
    original = portfolio_config_path.read_bytes()
    before = load_yaml(portfolio_config_path)
    family_before = _portfolio_family(before)
    cap_before = float(
        cfg_get(before, f"optimizer.sector_weight_caps.{MODEL_FAMILY}", -1.0)
    )
    if family_before.get("required") is not False or cap_before != 0.0:
        raise ValueError(
            "Portfolio config is not in the sealed machinery shadow state"
        )
    fingerprint = portfolio_activation_fingerprint(before)
    updated = render_portfolio_activation_config(
        original,
        required=True,
        cap=cap,
    )
    _write_bytes_atomic(portfolio_config_path, updated)
    try:
        after = load_yaml(portfolio_config_path)
        family_after = _portfolio_family(after)
        cap_after = float(
            cfg_get(
                after,
                f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
                -1.0,
            )
        )
        fixed_equal = {
            str(value)
            for value in cfg_get(
                after,
                "optimizer.fixed_equal_weight_sleeves",
                [],
            )
        }
        if (
            family_after.get("required") is not True
            or cap_after != cap
            or MODEL_FAMILY not in fixed_equal
        ):
            raise ValueError("Committed portfolio activation settings are invalid")
        if portfolio_activation_fingerprint(after) != fingerprint:
            raise ValueError(
                "Portfolio config changed outside machinery activation settings"
            )
    except BaseException:
        _write_bytes_atomic(portfolio_config_path, original)
        raise
    return {
        "before_sha256": file_sha256_bytes(original),
        "after_sha256": file_sha256(portfolio_config_path),
        "portfolio_non_activation_config_sha256": fingerprint,
        "machinery_required": True,
        "machinery_cap": cap,
    }


def file_sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_logged_command(
    command: Sequence[str],
    *,
    log_path: Path,
    lock: ActivationOrchestrationLock,
) -> CommandResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment[MASTER_PID_ENV] = str(os.getpid())
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    with log_path.open("w", encoding="utf-8", newline="") as log:
        proc = subprocess.Popen(
            list(command),
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        lock.register_child(proc.pid)
        try:
            return_code = proc.wait()
        except BaseException:
            _terminate_process_tree(proc)
            raise
        finally:
            lock.unregister_child(proc.pid)
    return CommandResult(
        command=list(command),
        return_code=int(return_code),
        log_path=log_path,
    )


def _selected_candidate_tickers(paths: ActivationPaths) -> set[str]:
    return {
        row["ticker"]
        for row in read_rows(paths.rank_csv)
        if _truthy(row.get("portfolio_sleeve_selected_flag"))
    }


def _direct_prefix_manifest_records(
    *,
    run_dir: Path,
    asof: str,
    active_config_sha256: str,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    payloads: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    for group, relative in PORTFOLIO_PREFIX_MANIFESTS.items():
        manifest_path = run_dir / relative
        if not manifest_path.exists():
            raise FileNotFoundError(manifest_path)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        acceptance = str(payload.get("acceptance") or "")
        if not acceptance.startswith("PASS"):
            raise ValueError(
                f"Portfolio prefix manifest did not pass for {group}: "
                f"{acceptance or 'MISSING'}"
            )
        manifest_asof = str(
            payload.get(
                "run_as_of",
                payload.get(
                    "run_as_of_date",
                    payload.get("as_of_date", ""),
                ),
            )
            or ""
        )
        if manifest_asof and manifest_asof != asof:
            raise ValueError(
                f"Portfolio prefix manifest date mismatch for {group}: "
                f"{manifest_asof}"
            )
        manifest_sha256 = file_sha256(manifest_path)
        payloads[group] = payload
        paths[group] = manifest_path
        records.append(
            {
                "group": group,
                "manifest": str(manifest_path),
                "manifest_sha256": manifest_sha256,
            }
        )
    score_config_sha = str(
        (
            (payloads["scores"].get("provenance") or {}).get(
                "config_yaml",
                {},
            )
            or {}
        ).get("sha256", "")
    )
    if score_config_sha != active_config_sha256:
        raise ValueError(
            "Portfolio score manifest was not produced under the exact "
            "active machinery configuration"
        )
    for group in (
        "optimizer",
        "costs",
        "rotation",
        "macro",
        "bl",
        "sleeves",
    ):
        config_sha = str(
            (payloads[group].get("provenance_sha256") or {}).get(
                "config.yaml",
                "",
            )
        )
        if config_sha != active_config_sha256:
            raise ValueError(
                f"Portfolio {group} manifest active config hash mismatch"
            )
    optimizer_provenance = payloads["optimizer"].get(
        "provenance_sha256",
        {},
    )
    if str(optimizer_provenance.get("stage1_manifest.json") or "") != (
        file_sha256(paths["scores"])
    ):
        raise ValueError("Optimizer no longer seals the Stage 1 manifest")
    if str(
        optimizer_provenance.get("stage2_risk_manifest.json") or ""
    ) != file_sha256(paths["risk"]):
        raise ValueError("Optimizer no longer seals the risk manifest")
    return records


def _validate_reusable_portfolio_prefix(
    *,
    portfolio_config_path: Path,
    asof: str,
    activation_paths: ActivationPaths,
    evidence_path: Path,
) -> dict[str, Any]:
    config = load_yaml(portfolio_config_path)
    runtime = resolve_runtime_paths(config, portfolio_config_path)
    run_dir = runtime.output_dir / "runs" / asof
    active_config_sha256 = file_sha256(portfolio_config_path)
    if evidence_path.exists():
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    else:
        orchestration_path = run_dir / "orchestration_meta.json"
        if orchestration_path.exists():
            orchestration = json.loads(
                orchestration_path.read_text(encoding="utf-8")
            )
            completed = {
                str(value)
                for value in orchestration.get("groups_completed", [])
            }
            missing = sorted(REUSABLE_PORTFOLIO_PREFIX_GROUPS - completed)
            if missing:
                raise ValueError(
                    "Portfolio resume source omitted completed prefix groups: "
                    f"{missing}"
                )
            failed = {
                str(value)
                for value in orchestration.get("groups_failed", [])
            }
            invalid = sorted(REUSABLE_PORTFOLIO_PREFIX_GROUPS & failed)
            if invalid:
                raise ValueError(
                    "Portfolio resume source contains failed prefix groups: "
                    f"{invalid}"
                )
            recorded_config_sha = str(
                (orchestration.get("inputs_sha256") or {}).get(
                    "config.yaml",
                    "",
                )
            )
            if recorded_config_sha != active_config_sha256:
                raise ValueError(
                    "Portfolio resume source was not produced under the exact "
                    "active machinery configuration"
                )
        records = _direct_prefix_manifest_records(
            run_dir=run_dir,
            asof=asof,
            active_config_sha256=active_config_sha256,
        )
        evidence = {
            "acceptance": "PASS",
            "asof_date": asof,
            "active_config_sha256": active_config_sha256,
            "groups": sorted(REUSABLE_PORTFOLIO_PREFIX_GROUPS),
            "source_orchestration_manifest": (
                str(orchestration_path)
                if orchestration_path.exists()
                else ""
            ),
            "source_orchestration_manifest_sha256": (
                file_sha256(orchestration_path)
                if orchestration_path.exists()
                else ""
            ),
            "manifests": records,
            "created_at_utc": utc_now(),
        }
        write_json_atomic(evidence_path, evidence)
    if str(evidence.get("acceptance") or "") != "PASS":
        raise ValueError("Portfolio prefix resume evidence did not pass")
    if str(evidence.get("asof_date") or "") != asof:
        raise ValueError("Portfolio prefix resume evidence date mismatch")
    if str(evidence.get("active_config_sha256") or "") != active_config_sha256:
        raise ValueError("Portfolio prefix resume active config hash mismatch")
    evidence_groups = {
        str(value) for value in evidence.get("groups", [])
    }
    if evidence_groups != REUSABLE_PORTFOLIO_PREFIX_GROUPS:
        raise ValueError("Portfolio prefix resume group set changed")
    records = evidence.get("manifests", [])
    if len(records) != len(REUSABLE_PORTFOLIO_PREFIX_GROUPS):
        raise ValueError("Portfolio prefix resume manifest set is incomplete")
    for record in records:
        manifest_path = Path(str(record.get("manifest") or ""))
        if not manifest_path.exists():
            raise FileNotFoundError(manifest_path)
        if file_sha256(manifest_path) != str(
            record.get("manifest_sha256") or ""
        ):
            raise ValueError(
                "Portfolio prefix resume manifest hash mismatch: "
                f"{manifest_path}"
            )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not str(payload.get("acceptance") or "").startswith("PASS"):
            raise ValueError(
                f"Portfolio prefix resume manifest no longer passes: "
                f"{manifest_path}"
            )
    _validate_machinery_portfolio_membership(
        portfolio_config_path=portfolio_config_path,
        asof=asof,
        activation_paths=activation_paths,
    )
    return evidence


def _validate_machinery_portfolio_membership(
    *,
    portfolio_config_path: Path,
    asof: str,
    activation_paths: ActivationPaths,
) -> dict[str, Any]:
    config = load_yaml(portfolio_config_path)
    runtime = resolve_runtime_paths(config, portfolio_config_path)
    run_dir = runtime.output_dir / "runs" / asof
    expected = _selected_candidate_tickers(activation_paths)
    scores = _read_csv(run_dir / "stocks_scores.csv")
    machinery_scores = [
        row for row in scores if row.get("source_pipeline") == MODEL_FAMILY
    ]
    stage1_selected = {
        str(row.get("ticker") or "")
        for row in machinery_scores
        if _truthy(row.get("investable_eligible"))
    }
    if stage1_selected != expected:
        raise ValueError(
            "Portfolio Stage 1 machinery membership mismatch: "
            f"expected={sorted(expected)} actual={sorted(stage1_selected)}"
        )
    weights = _read_csv(run_dir / "optimizer" / "target_weights.csv")
    machinery_weights = [
        row for row in weights if row.get("source_pipeline") == MODEL_FAMILY
    ]
    optimizer_tickers = {
        str(row.get("ticker") or "") for row in machinery_weights
    }
    if optimizer_tickers != expected:
        raise ValueError(
            "Portfolio optimizer machinery membership mismatch: "
            f"expected={sorted(expected)} actual={sorted(optimizer_tickers)}"
        )
    parsed_weights = [float(row["weight"]) for row in machinery_weights]
    if parsed_weights and max(parsed_weights) - min(parsed_weights) > 1.0e-8:
        raise ValueError("Portfolio optimizer broke machinery equal weighting")
    cap = float(
        cfg_get(config, f"optimizer.sector_weight_caps.{MODEL_FAMILY}", -1.0)
    )
    sleeve_weight = sum(parsed_weights)
    if sleeve_weight < -1.0e-10 or sleeve_weight > cap + 1.0e-8:
        raise ValueError(
            "Portfolio optimizer machinery sleeve weight is outside its cap: "
            f"weight={sleeve_weight} cap={cap}"
        )
    return {
        "stage1_machinery_rows": len(machinery_scores),
        "stage1_machinery_investable_count": len(stage1_selected),
        "optimizer_machinery_count": len(machinery_weights),
        "optimizer_machinery_weight": sleeve_weight,
        "optimizer_machinery_equal_weight": (
            parsed_weights[0] if parsed_weights else 0.0
        ),
        "optimizer_machinery_cap": cap,
    }


def validate_portfolio_smoke(
    *,
    portfolio_config_path: Path,
    asof: str,
    activation_paths: ActivationPaths,
    reused_groups: frozenset[str] = frozenset(),
    resume_evidence_path: Path | None = None,
) -> dict[str, Any]:
    config = load_yaml(portfolio_config_path)
    runtime = resolve_runtime_paths(config, portfolio_config_path)
    run_dir = runtime.output_dir / "runs" / asof
    orchestration_path = run_dir / "orchestration_meta.json"
    if not orchestration_path.exists():
        raise FileNotFoundError(orchestration_path)
    orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
    acceptance = str(orchestration.get("acceptance") or "")
    if acceptance not in ACCEPTED_ORCHESTRATION_RESULTS:
        raise ValueError(
            f"Portfolio orchestration did not pass: {acceptance or 'MISSING'}"
        )
    completed = {str(value) for value in orchestration.get("groups_completed", [])}
    if reused_groups and resume_evidence_path is None:
        raise ValueError("Reused portfolio groups require sealed resume evidence")
    resume_evidence_sha256 = ""
    if resume_evidence_path is not None:
        evidence = _validate_reusable_portfolio_prefix(
            portfolio_config_path=portfolio_config_path,
            asof=asof,
            activation_paths=activation_paths,
            evidence_path=resume_evidence_path,
        )
        sealed_reused_groups = frozenset(
            str(value) for value in evidence.get("groups", [])
        )
        if reused_groups != sealed_reused_groups:
            raise ValueError("Portfolio reused groups do not match resume evidence")
        resume_evidence_sha256 = file_sha256(resume_evidence_path)
    missing_groups = sorted(
        REQUIRED_PORTFOLIO_GROUPS - completed - reused_groups
    )
    if missing_groups:
        raise ValueError(
            f"Portfolio smoke omitted required groups: {missing_groups}"
        )
    membership = _validate_machinery_portfolio_membership(
        portfolio_config_path=portfolio_config_path,
        asof=asof,
        activation_paths=activation_paths,
    )
    final_manifest = run_dir / "final" / "final_manifest.json"
    if not final_manifest.exists():
        raise FileNotFoundError(final_manifest)
    final_payload = json.loads(final_manifest.read_text(encoding="utf-8"))
    if str(final_payload.get("acceptance") or "") != "PASS":
        raise ValueError("Portfolio final target book manifest did not pass")
    return {
        "acceptance": "PASS",
        "run_dir": str(run_dir),
        "orchestration_manifest": str(orchestration_path),
        "orchestration_manifest_sha256": file_sha256(orchestration_path),
        "orchestration_acceptance": acceptance,
        "required_groups_completed": sorted(REQUIRED_PORTFOLIO_GROUPS),
        "orchestration_groups_completed": sorted(completed),
        "reused_groups": sorted(reused_groups),
        "resume_evidence": (
            str(resume_evidence_path) if resume_evidence_path else ""
        ),
        "resume_evidence_sha256": resume_evidence_sha256,
        **membership,
        "final_manifest": str(final_manifest),
        "final_manifest_sha256": file_sha256(final_manifest),
    }


def preflight_activation_transaction(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
    today: date | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    validation = validate_stage12_lock(output_root=governance_root)
    if validation.get("acceptance") != "PASS":
        issues.extend(str(value) for value in validation.get("issues", []))
    lock_path = Stage12Paths(governance_root).lock_json
    lock: Mapping[str, Any] = {}
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        try:
            _activation_date_checks(lock, asof)
        except ValueError as exc:
            issues.append(str(exc))
    else:
        issues.append(f"missing governance lock {lock_path}")
    try:
        validate_wall_clock(asof, today=today)
    except ValueError as exc:
        issues.append(str(exc))
    if today is None:
        try:
            validate_completed_session(asof)
        except ValueError as exc:
            issues.append(str(exc))
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio = load_yaml(portfolio_config_path)
    family = _portfolio_family(portfolio)
    cap = float(
        cfg_get(
            portfolio,
            f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
            -1.0,
        )
    )
    activation_mode = str(
        lock.get("activation_mode") or ACTIVATION_MODE_INITIAL
    )
    if activation_mode == ACTIVATION_MODE_INITIAL:
        settings_valid = family.get("required") is False and cap == 0.0
    elif activation_mode == ACTIVATION_MODE_REPLACE_ACTIVE:
        settings_valid = (
            family.get("required") is True
            and cap == float(lock.get("proposed_portfolio_cap") or -1.0)
        )
        active_state_path = Path(
            str(lock.get("active_activation_state") or "")
        )
        if (
            not active_state_path.is_file()
            or active_state_path.resolve()
            != Stage12Paths(
                resolve_path(
                    cfg_get(config, "machinery_stage12.output_root"),
                    base_dir=config_path.parent,
                )
            ).activation_state_json.resolve()
            or file_sha256(active_state_path)
            != str(lock.get("previous_activation_state_sha256") or "")
        ):
            issues.append("current machinery activation state changed")
    else:
        settings_valid = False
        issues.append(f"unknown activation mode {activation_mode!r}")
    if not settings_valid:
        issues.append(
            "portfolio config does not match the sealed machinery "
            f"activation mode (required={family.get('required')!r}, cap={cap})"
        )
    source_rank_raw = str(lock.get("source_dashboard_rank") or "").strip()
    if source_rank_raw:
        source_rank = Path(source_rank_raw)
    else:
        source_rank = resolve_path(
            cfg_get(config, "machinery_scoring.dashboard_root"),
            base_dir=config_path.parent,
        ) / asof / "machinery_final_rank_table.csv"
    return {
        "acceptance": "PASS" if not issues else "BLOCKED",
        "asof_date": asof,
        "checked_at_utc": utc_now(),
        "wall_clock_date": (
            today or datetime.now(MARKET_TIMEZONE).date()
        ).isoformat(),
        "governance_acceptance": validation.get("acceptance"),
        "activation_mode": activation_mode,
        "portfolio_shadow_required": family.get("required"),
        "portfolio_shadow_cap": cap,
        "source_dashboard_exists": source_rank.exists(),
        "issues": issues,
    }


def run_activation_transaction(  # noqa: C901
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
    approval_token: str,
    run_refresh: bool,
    force_candidate: bool,
    reuse_risk_price_data: bool,
    resume_portfolio_smoke: bool = False,
) -> dict[str, Any]:
    validate_wall_clock(asof)
    validate_completed_session(asof)
    governance_validation = validate_stage12_lock(output_root=governance_root)
    if governance_validation.get("acceptance") != "PASS":
        raise ValueError(
            "Stage 12 governance validation failed: "
            + ";".join(governance_validation.get("issues", []))
        )
    lock_payload = json.loads(
        Stage12Paths(governance_root).lock_json.read_text(encoding="utf-8")
    )
    _activation_date_checks(lock_payload, asof)
    configured_token = str(
        cfg_get(config, "machinery_stage12.activation_approval_token", "")
    )
    if not configured_token or approval_token != configured_token:
        raise PermissionError("Explicit machinery activation token is invalid")
    paths = ActivationPaths(governance_root, asof)
    cycle_stage12_paths = Stage12Paths(governance_root)
    active_root_config = str(
        cfg_get(config, "machinery_stage12.output_root", "") or ""
    ).strip()
    active_governance_root = (
        resolve_path(active_root_config, base_dir=config_path.parent)
        if active_root_config
        else governance_root
    )
    active_stage12_paths = Stage12Paths(active_governance_root)
    activation_mode = str(
        lock_payload.get("activation_mode") or ACTIVATION_MODE_INITIAL
    )
    existing_state = active_stage12_paths.activation_state_json
    previous_state_bytes: bytes | None = None
    if activation_mode == ACTIVATION_MODE_INITIAL:
        if existing_state.exists():
            raise ValueError(
                "Machinery production activation state already exists; "
                "use a sealed active-model replacement cycle"
            )
    elif activation_mode == ACTIVATION_MODE_REPLACE_ACTIVE:
        if not existing_state.is_file():
            raise ValueError(
                "Active-model replacement requires the current activation state"
            )
        approved_state_path = Path(
            str(lock_payload.get("active_activation_state") or "")
        )
        if approved_state_path.resolve() != existing_state.resolve():
            raise ValueError(
                "Replacement approval points to a different activation state"
            )
        if file_sha256(existing_state) != str(
            lock_payload.get("previous_activation_state_sha256") or ""
        ):
            raise ValueError(
                "Current activation state changed after replacement approval"
            )
        previous_state_bytes = existing_state.read_bytes()
    else:
        raise ValueError(f"Unknown machinery activation mode: {activation_mode}")
    transaction_root = governance_root / "activation_transactions" / asof
    transaction_root.mkdir(parents=True, exist_ok=True)
    result_path = transaction_root / TRANSACTION_RESULT_NAME
    resume_evidence_path = transaction_root / RESUME_EVIDENCE_NAME
    logs_root = transaction_root / "logs"
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_before = load_yaml(portfolio_config_path)
    family_before = _portfolio_family(portfolio_before)
    cap_before = float(
        cfg_get(
            portfolio_before,
            f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
            -1.0,
        )
    )
    proposed_cap = float(lock_payload["proposed_portfolio_cap"])
    expected_required = activation_mode == ACTIVATION_MODE_REPLACE_ACTIVE
    expected_cap = proposed_cap if expected_required else 0.0
    if (
        family_before.get("required") is not expected_required
        or cap_before != expected_cap
    ):
        raise ValueError(
            "Portfolio config does not match the sealed machinery "
            "activation mode"
        )
    if (
        portfolio_activation_fingerprint(portfolio_before)
        != lock_payload.get("portfolio_non_activation_config_sha256")
    ):
        raise ValueError(
            "Portfolio configuration changed outside activation settings"
        )
    if (
        machinery_portfolio_policy_fingerprint(portfolio_before)
        != lock_payload.get("machinery_portfolio_policy_sha256")
    ):
        raise ValueError("Machinery portfolio policy changed after approval")
    original_config = b""
    backup_path = transaction_root / CONFIG_BACKUP_NAME
    config_committed = False
    published = False
    activation_state_written = False
    commands: list[dict[str, Any]] = []
    failure = ""
    with ActivationOrchestrationLock() as orchestration_lock:
        original_config = portfolio_config_path.read_bytes()
        try:
            locked_portfolio = load_yaml(portfolio_config_path)
            locked_family = _portfolio_family(locked_portfolio)
            locked_cap = float(
                cfg_get(
                    locked_portfolio,
                    f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
                    -1.0,
                )
            )
            if (
                locked_family.get("required") is not expected_required
                or locked_cap != expected_cap
                or portfolio_activation_fingerprint(locked_portfolio)
                != lock_payload.get("portfolio_non_activation_config_sha256")
                or machinery_portfolio_policy_fingerprint(locked_portfolio)
                != lock_payload.get("machinery_portfolio_policy_sha256")
            ):
                raise ValueError(
                    "Portfolio config changed before the activation lock "
                    "was acquired"
                )
            if run_refresh and activation_mode == ACTIVATION_MODE_REPLACE_ACTIVE:
                raise ValueError(
                    "Active-model replacement requires a separately built, "
                    "sealed shadow dashboard; use --skip-refresh"
                )
            if run_refresh:
                refresh = run_logged_command(
                    [
                        sys.executable,
                        str(MACHINERY_REFRESH_RUNNER),
                        "--config",
                        str(config_path),
                        "--asof",
                        asof,
                    ],
                    log_path=logs_root / "machinery_refresh.log",
                    lock=orchestration_lock,
                )
                commands.append(
                    {
                        "name": "machinery_incremental_refresh",
                        "command": refresh.command,
                        "return_code": refresh.return_code,
                        "log_path": str(refresh.log_path),
                    }
                )
                if refresh.return_code != 0:
                    raise RuntimeError(
                        "Machinery incremental refresh failed; see "
                        f"{refresh.log_path}"
                    )
            candidate = prepare_activation_candidate(
                config,
                config_path=config_path,
                governance_root=governance_root,
                asof=asof,
                force=force_candidate,
            )
            _write_bytes_atomic(backup_path, original_config)
            if activation_mode == ACTIVATION_MODE_INITIAL:
                config_transition = commit_portfolio_activation_config(
                    portfolio_config_path,
                    cap=proposed_cap,
                )
                config_committed = True
            else:
                config_transition = {
                    "mode": ACTIVATION_MODE_REPLACE_ACTIVE,
                    "before_sha256": file_sha256(portfolio_config_path),
                    "after_sha256": file_sha256(portfolio_config_path),
                    "portfolio_non_activation_config_sha256": (
                        portfolio_activation_fingerprint(portfolio_before)
                    ),
                    "machinery_required": True,
                    "machinery_cap": proposed_cap,
                    "configuration_changed": False,
                }
            activation = activate_candidate(
                config,
                config_path=config_path,
                governance_root=governance_root,
                asof=asof,
                approval_token=approval_token,
            )
            published = True
            portfolio_command = [
                sys.executable,
                str(PORTFOLIO_RUNNER),
                "--config",
                str(portfolio_config_path),
                "--as-of",
                asof,
                "--cadence",
                "strategic",
                "--force",
            ]
            reused_groups = frozenset()
            if resume_portfolio_smoke:
                resume_evidence = _validate_reusable_portfolio_prefix(
                    portfolio_config_path=portfolio_config_path,
                    asof=asof,
                    activation_paths=paths,
                    evidence_path=resume_evidence_path,
                )
                reused_groups = frozenset(
                    str(value) for value in resume_evidence["groups"]
                )
                portfolio_command.extend(
                    ["--groups", ",".join(PORTFOLIO_RESUME_GROUPS)]
                )
            elif reuse_risk_price_data:
                portfolio_command.append("--reuse-risk-price-data")
            portfolio = run_logged_command(
                portfolio_command,
                log_path=logs_root / "portfolio_strategic_smoke.log",
                lock=orchestration_lock,
            )
            commands.append(
                {
                    "name": "portfolio_strategic_smoke",
                    "command": portfolio.command,
                    "return_code": portfolio.return_code,
                    "log_path": str(portfolio.log_path),
                }
            )
            if portfolio.return_code != 0:
                raise RuntimeError(
                    "Portfolio strategic smoke failed; see "
                    f"{portfolio.log_path}"
                )
            smoke = validate_portfolio_smoke(
                portfolio_config_path=portfolio_config_path,
                asof=asof,
                activation_paths=paths,
                reused_groups=reused_groups,
                resume_evidence_path=(
                    resume_evidence_path
                    if resume_portfolio_smoke
                    else None
                ),
            )
            result = {
                "acceptance": "PASS",
                "activation_status": ACTIVATION_STATUS_FULLY_VALIDATED,
                "completed_at_utc": utc_now(),
                "asof_date": asof,
                "candidate": candidate,
                "config_transition": config_transition,
                "activation": activation,
                "portfolio_smoke": smoke,
                "commands": commands,
                "rollback_performed": False,
            }
            activation_result = {
                **activation,
                "activation_status": ACTIVATION_STATUS_FULLY_VALIDATED,
                "full_portfolio_smoke_required": False,
                "portfolio_smoke_manifest": smoke["orchestration_manifest"],
                "portfolio_smoke_manifest_sha256": smoke[
                    "orchestration_manifest_sha256"
                ],
                "portfolio_final_manifest": smoke["final_manifest"],
                "portfolio_final_manifest_sha256": smoke[
                    "final_manifest_sha256"
                ],
            }
            write_json_atomic(paths.activation_json, activation_result)
            activation_state = {
                "acceptance": "PASS",
                "production_policy_status": PRODUCTION_POLICY_STATUS_ACTIVE,
                "activated_at_utc": utc_now(),
                "activation_asof": asof,
                "activation_mode": activation_mode,
                "governance_root": str(governance_root),
                "governance_lock": str(cycle_stage12_paths.lock_json),
                "governance_lock_sha256": file_sha256(
                    cycle_stage12_paths.lock_json
                ),
                "candidate_rank": str(paths.rank_csv),
                "candidate_rank_sha256": file_sha256(paths.rank_csv),
                "activation_result": str(paths.activation_json),
                "activation_result_sha256": file_sha256(
                    paths.activation_json
                ),
                "portfolio_config": str(portfolio_config_path),
                "portfolio_config_sha256_at_activation": file_sha256(
                    portfolio_config_path
                ),
                "production_selection_policy": lock_payload[
                    "production_selection_policy"
                ],
                "selected_sleeve_count": candidate[
                    "selected_sleeve_count"
                ],
                "broad_eligible_count": candidate["broad_eligible_count"],
                "portfolio_cap": float(
                    lock_payload["proposed_portfolio_cap"]
                ),
                "production_source_sha256": (
                    production_policy_source_hashes()
                ),
                "previous_activation_state_sha256": (
                    file_sha256_bytes(previous_state_bytes)
                    if previous_state_bytes is not None
                    else ""
                ),
            }
            write_json_atomic(
                active_stage12_paths.activation_state_json,
                activation_state,
            )
            activation_state_written = True
            result["activation_state"] = {
                **activation_state,
                "path": str(active_stage12_paths.activation_state_json),
                "sha256": file_sha256(
                    active_stage12_paths.activation_state_json
                ),
            }
            write_json_atomic(result_path, result)
            return result
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {exc}"
            rollback_errors: list[str] = []
            if config_committed:
                try:
                    _write_bytes_atomic(portfolio_config_path, original_config)
                except BaseException as rollback_exc:
                    rollback_errors.append(
                        "portfolio config rollback failed: "
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
            if published:
                try:
                    rollback_published_candidate(
                        governance_root=governance_root,
                        asof=asof,
                        reason=failure,
                    )
                except BaseException as rollback_exc:
                    rollback_errors.append(
                        "dashboard rollback failed: "
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
            if activation_state_written:
                try:
                    if previous_state_bytes is None:
                        active_stage12_paths.activation_state_json.unlink(
                            missing_ok=True
                        )
                    else:
                        _write_bytes_atomic(
                            active_stage12_paths.activation_state_json,
                            previous_state_bytes,
                        )
                except BaseException as rollback_exc:
                    rollback_errors.append(
                        "activation state rollback failed: "
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
            result = {
                "acceptance": (
                    "FAIL_NO_PRODUCTION_CHANGE"
                    if not config_committed and not published
                    else "FAIL_ROLLED_BACK"
                    if not rollback_errors
                    else "FAIL_ROLLBACK_INCOMPLETE"
                ),
                "activation_status": (
                    "PRE_ACTIVATION_FAILED"
                    if not config_committed and not published
                    else "ROLLED_BACK_AFTER_SMOKE_FAILURE"
                    if not rollback_errors
                    else "ROLLBACK_INCOMPLETE"
                ),
                "completed_at_utc": utc_now(),
                "asof_date": asof,
                "failure": failure,
                "rollback_errors": rollback_errors,
                "commands": commands,
                "portfolio_config_restored": (
                    portfolio_config_path.exists()
                    and portfolio_config_path.read_bytes() == original_config
                ),
                "dashboard_rollback_attempted": published,
                "activation_state_rollback_attempted": (
                    activation_state_written
                ),
                "production_files_changed": config_committed or published,
            }
            write_json_atomic(result_path, result)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return result
