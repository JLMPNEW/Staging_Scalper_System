"""Append-only trust ledger for factor-validation registrations and publications."""

from __future__ import annotations

import errno
import json
import math
import os
import platform
import re
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import numpy as np
import scipy

from factor_validation.registry import (
    CampaignRegistry,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)


LEDGER_SCHEMA_VERSION = "factor_validation_campaign_ledger_v1"
LEDGER_HEAD_SCHEMA_VERSION = "factor_validation_campaign_ledger_head_v1"
LEDGER_FILE_NAME = "factor_validation_campaign_ledger.jsonl"
LEDGER_HEAD_FILE_NAME = "factor_validation_campaign_ledger.head.json"
LEDGER_LOCK_FILE_NAME = ".factor_validation_campaign_ledger.lock"
LEDGER_GENESIS_SHA256 = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
LEDGER_EVENT_TYPES = frozenset(
    {
        "campaign_registered",
        "campaign_report_published",
        "family_abandoned",
        "publication_attempted",
        "publication_failed",
        "publication_succeeded",
    }
)
LEDGER_ENTRY_KEYS = frozenset(
    {
        "attempt_id",
        "campaign_id",
        "cell_id",
        "entry_sha256",
        "environment_sha256",
        "error_code",
        "event_type",
        "family_id",
        "family_p_values",
        "fdr_member_id",
        "logical_cell_sha256",
        "manifest_sha256",
        "package_relative_path",
        "prior_entry_sha256",
        "registry_sha256",
        "schema_version",
        "sequence",
        "state",
        "supersedes_manifest_sha256",
    }
)


@dataclass(frozen=True)
class EnvironmentProvenance:
    """Runtime versions that can materially affect statistical evidence."""

    python_version: str
    python_implementation: str
    numpy_version: str
    scipy_version: str
    platform: str

    @classmethod
    def capture(cls) -> EnvironmentProvenance:
        return cls(
            python_version=platform.python_version(),
            python_implementation=platform.python_implementation(),
            numpy_version=np.__version__,
            scipy_version=scipy.__version__,
            platform=platform.platform(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "numpy_version": self.numpy_version,
            "platform": self.platform,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "scipy_version": self.scipy_version,
        }

    @property
    def sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_dict()))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EnvironmentProvenance:
        expected = {
            "numpy_version",
            "platform",
            "python_implementation",
            "python_version",
            "scipy_version",
        }
        if set(value) != expected or any(not isinstance(value[key], str) for key in expected):
            raise ValueError("environment provenance schema mismatch")
        return cls(**value)


@dataclass(frozen=True)
class LedgerVerificationReport:
    ok: bool
    errors: tuple[str, ...]
    entry_count: int
    head_sha256: str | None
    superseded_manifest_sha256: tuple[str, ...]


def campaign_ledger_path(output_root: str | Path) -> Path:
    return Path(output_root).resolve() / LEDGER_FILE_NAME


def campaign_ledger_head_path(output_root: str | Path) -> Path:
    return Path(output_root).resolve() / LEDGER_HEAD_FILE_NAME


def _head_payload(sequence: int, head_sha256: str) -> dict[str, Any]:
    return {
        "head_sha256": head_sha256,
        "schema_version": LEDGER_HEAD_SCHEMA_VERSION,
        "sequence": sequence,
    }


def _entry_digest(entry: dict[str, Any]) -> str:
    payload = dict(entry)
    payload.pop("entry_sha256", None)
    return sha256_bytes(canonical_json_bytes(payload))


def _nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _digest_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(_SHA256.fullmatch(value)))


def _event_field_errors(entry: dict[str, Any]) -> list[str]:
    """Validate event-specific domains before append and again during verification."""

    event = entry.get("event_type")
    if event not in LEDGER_EVENT_TYPES:
        return ["event_type_unknown"]
    errors: list[str] = []
    for key in ("attempt_id", "campaign_id"):
        if not _nonblank_string(entry.get(key)):
            errors.append(f"{key}_invalid")
    if not isinstance(entry.get("registry_sha256"), str) or not _SHA256.fullmatch(
        entry["registry_sha256"]
    ):
        errors.append("registry_sha256_invalid")
    for key in (
        "environment_sha256",
        "logical_cell_sha256",
        "manifest_sha256",
        "supersedes_manifest_sha256",
    ):
        if not _digest_or_none(entry.get(key)):
            errors.append(f"{key}_invalid")

    null_by_event: dict[str, tuple[str, ...]] = {
        "campaign_registered": (
            "cell_id",
            "environment_sha256",
            "error_code",
            "family_id",
            "family_p_values",
            "fdr_member_id",
            "logical_cell_sha256",
            "manifest_sha256",
            "supersedes_manifest_sha256",
        ),
        "campaign_report_published": (
            "cell_id",
            "environment_sha256",
            "error_code",
            "family_p_values",
            "fdr_member_id",
            "logical_cell_sha256",
            "supersedes_manifest_sha256",
        ),
        "family_abandoned": (
            "cell_id",
            "environment_sha256",
            "family_p_values",
            "fdr_member_id",
            "logical_cell_sha256",
            "manifest_sha256",
            "package_relative_path",
            "supersedes_manifest_sha256",
        ),
    }
    for key in null_by_event.get(str(event), ()):
        if entry.get(key) is not None:
            errors.append(f"{key}_must_be_null")

    if event == "campaign_registered":
        if entry.get("state") != "registered":
            errors.append("state_invalid")
        if not _nonblank_string(entry.get("package_relative_path")):
            errors.append("package_relative_path_invalid")
    elif event == "campaign_report_published":
        if entry.get("state") != "reported":
            errors.append("state_invalid")
        for key in ("family_id", "package_relative_path"):
            if not _nonblank_string(entry.get(key)):
                errors.append(f"{key}_invalid")
        if not isinstance(entry.get("manifest_sha256"), str) or not _SHA256.fullmatch(
            entry["manifest_sha256"]
        ):
            errors.append("manifest_sha256_required")
    elif event == "family_abandoned":
        if entry.get("state") != "abandoned":
            errors.append("state_invalid")
        for key in ("family_id", "error_code"):
            if not _nonblank_string(entry.get(key)):
                errors.append(f"{key}_invalid")
    else:
        for key in (
            "cell_id",
            "environment_sha256",
            "family_id",
            "fdr_member_id",
            "logical_cell_sha256",
            "package_relative_path",
        ):
            if not _nonblank_string(entry.get(key)):
                errors.append(f"{key}_invalid")
        family_values = entry.get("family_p_values")
        if not isinstance(family_values, dict) or not family_values:
            errors.append("family_p_values_invalid")
        else:
            for member_id, p_value in family_values.items():
                if not _nonblank_string(member_id):
                    errors.append("family_p_value_member_invalid")
                if p_value is not None and (
                    isinstance(p_value, bool)
                    or not isinstance(p_value, (int, float))
                    or not math.isfinite(float(p_value))
                    or not 0.0 <= float(p_value) <= 1.0
                ):
                    errors.append("family_p_value_invalid")
        if event == "publication_attempted":
            if entry.get("state") != "draft":
                errors.append("state_invalid")
            if entry.get("manifest_sha256") is not None or entry.get("error_code") is not None:
                errors.append("attempt_terminal_fields_present")
        elif event == "publication_succeeded":
            if entry.get("state") not in {"accepted", "rejected"}:
                errors.append("state_invalid")
            if not isinstance(entry.get("manifest_sha256"), str) or not _SHA256.fullmatch(
                entry["manifest_sha256"]
            ):
                errors.append("manifest_sha256_required")
            if entry.get("error_code") is not None:
                errors.append("error_code_must_be_null")
        elif event == "publication_failed":
            if entry.get("state") != "failed":
                errors.append("state_invalid")
            if entry.get("manifest_sha256") is not None:
                errors.append("manifest_sha256_must_be_null")
            if not _nonblank_string(entry.get("error_code")):
                errors.append("error_code_invalid")
    return sorted(set(errors))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _safe_relative_path(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(":" in part for part in pure.parts)
    ):
        return None
    resolved = (root / Path(*pure.parts)).resolve()
    return resolved if resolved.is_relative_to(root) else None


def _read_entries(output_root: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = campaign_ledger_path(output_root)
    errors: list[str] = []
    if not path.exists():
        return [], errors
    if not path.is_file() or path.is_symlink():
        return [], ["ledger_not_regular_file"]
    entries: list[dict[str, Any]] = []
    try:
        raw_lines = path.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        return [], [f"ledger_read_failed:{exc.__class__.__name__}"]
    if any(not line.endswith(b"\n") for line in raw_lines):
        errors.append("ledger_line_missing_newline")
    previous = LEDGER_GENESIS_SHA256
    for line_number, raw in enumerate(raw_lines, start=1):
        try:
            value = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"ledger_invalid_json:{line_number}:{exc.__class__.__name__}")
            continue
        if not isinstance(value, dict):
            errors.append(f"ledger_entry_not_object:{line_number}")
            continue
        try:
            if canonical_json_bytes(value) != raw:
                errors.append(f"ledger_entry_not_canonical:{line_number}")
        except (TypeError, ValueError):
            errors.append(f"ledger_entry_not_canonical:{line_number}")
        if set(value) != LEDGER_ENTRY_KEYS:
            errors.append(f"ledger_entry_schema_mismatch:{line_number}")
        for event_error in _event_field_errors(value):
            errors.append(f"ledger_event_field_invalid:{line_number}:{event_error}")
        if value.get("schema_version") != LEDGER_SCHEMA_VERSION:
            errors.append(f"ledger_schema_version_mismatch:{line_number}")
        if value.get("sequence") != line_number:
            errors.append(f"ledger_sequence_mismatch:{line_number}")
        if value.get("prior_entry_sha256") != previous:
            errors.append(f"ledger_prior_hash_mismatch:{line_number}")
        try:
            computed = _entry_digest(value)
        except (TypeError, ValueError):
            computed = LEDGER_GENESIS_SHA256
            errors.append(f"ledger_entry_hash_uncomputable:{line_number}")
        if value.get("entry_sha256") != computed:
            errors.append(f"ledger_entry_hash_mismatch:{line_number}")
        for key in (
            "entry_sha256",
            "prior_entry_sha256",
            "registry_sha256",
        ):
            if not isinstance(value.get(key), str) or not _SHA256.fullmatch(value[key]):
                errors.append(f"ledger_digest_invalid:{line_number}:{key}")
        for key in (
            "environment_sha256",
            "logical_cell_sha256",
            "manifest_sha256",
            "supersedes_manifest_sha256",
        ):
            item = value.get(key)
            if item is not None and (
                not isinstance(item, str) or not _SHA256.fullmatch(item)
            ):
                errors.append(f"ledger_digest_invalid:{line_number}:{key}")
        previous = str(value.get("entry_sha256") or computed)
        entries.append(value)
    return entries, errors


def _read_head(output_root: str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = campaign_ledger_head_path(output_root)
    if not path.exists():
        return None, []
    if not path.is_file() or path.is_symlink():
        return None, ["ledger_head_not_regular_file"]
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return None, [f"ledger_head_invalid_json:{exc.__class__.__name__}"]
    if not isinstance(value, dict):
        return None, ["ledger_head_not_object"]
    expected = {"head_sha256", "schema_version", "sequence"}
    errors: list[str] = []
    if set(value) != expected:
        errors.append("ledger_head_schema_mismatch")
    if canonical_json_bytes(value) != raw:
        errors.append("ledger_head_not_canonical")
    if value.get("schema_version") != LEDGER_HEAD_SCHEMA_VERSION:
        errors.append("ledger_head_schema_version_mismatch")
    return value, errors


def read_campaign_ledger(output_root: str | Path) -> tuple[dict[str, Any], ...]:
    """Read and cryptographically verify the ledger and its persisted head."""

    entries, errors = _read_entries(output_root)
    head, head_errors = _read_head(output_root)
    errors.extend(head_errors)
    expected_sequence = len(entries)
    expected_hash = entries[-1]["entry_sha256"] if entries else LEDGER_GENESIS_SHA256
    if head is None:
        if entries:
            errors.append("ledger_head_missing")
    elif head.get("sequence") != expected_sequence or head.get("head_sha256") != expected_hash:
        errors.append("ledger_head_mismatch")
    if errors:
        raise ValueError(";".join(errors))
    return tuple(entries)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        still_active = 259
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            # Access denied is conservative evidence that the PID exists.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
                exit_code.value == still_active
            )
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _lock_is_stale(path: Path, *, stale_after_seconds: float) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return age >= stale_after_seconds
    if not isinstance(value, dict):
        return age >= stale_after_seconds
    if value.get("state") == "released":
        return True
    host = value.get("hostname")
    pid = value.get("pid")
    if host == socket.gethostname() and isinstance(pid, int) and not isinstance(pid, bool):
        return not _pid_is_alive(pid)
    return age >= stale_after_seconds


def acquire_advisory_lock(path: Path, *, stale_after_seconds: float = 3600.0) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            payload = canonical_json_bytes(
                {
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "schema_version": "factor_validation_lock_v1",
                    "state": "acquired",
                }
            )
            os.write(descriptor, payload)
            os.fsync(descriptor)
            return descriptor
        except FileExistsError:
            if attempt == 0 and _lock_is_stale(
                path, stale_after_seconds=stale_after_seconds
            ):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            raise FileExistsError(f"active factor-validation ledger lock: {path}") from None
    raise RuntimeError("ledger lock acquisition exhausted")  # pragma: no cover


def release_advisory_lock(path: Path, descriptor: int | None) -> bool:
    """Close and best-effort unlink a lock without invalidating completed work."""

    if descriptor is None:
        return False
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(
            descriptor,
            canonical_json_bytes(
                {
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "schema_version": "factor_validation_lock_v1",
                    "state": "released",
                }
            ),
        )
        os.fsync(descriptor)
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass
    for attempt in range(5):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
            return True
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
        except OSError:
            return False
    return False


def _write_head(output_root: Path, *, sequence: int, head_sha256: str) -> None:
    target = campaign_ledger_head_path(output_root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".factor_validation_ledger.head-",
        dir=output_root,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(_head_payload(sequence, head_sha256)))
            handle.flush()
            os.fsync(handle.fileno())
        last_permission_error: PermissionError | None = None
        for attempt in range(10):
            try:
                os.replace(temporary, target)
                return
            except PermissionError as exc:
                last_permission_error = exc
                time.sleep(0.05 * (attempt + 1))
        assert last_permission_error is not None
        raise last_permission_error
    finally:
        for attempt in range(5):
            try:
                if temporary.exists():
                    temporary.unlink()
                break
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
            except OSError:
                break


def repair_campaign_ledger_head(output_root: str | Path) -> dict[str, Any]:
    """Rebuild only the derived head after validating the append-only chain.

    This is intentionally explicit rather than part of ordinary reads: a
    mismatched head remains fail-closed until an operator invokes recovery.
    No ledger entry is changed, removed, or reordered.
    """

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LEDGER_LOCK_FILE_NAME
    descriptor: int | None = acquire_advisory_lock(lock_path)
    try:
        entries, errors = _read_entries(root)
        if errors:
            raise ValueError("cannot repair an invalid ledger chain:" + ";".join(errors))
        sequence = len(entries)
        head_sha256 = (
            entries[-1]["entry_sha256"] if entries else LEDGER_GENESIS_SHA256
        )
        _write_head(root, sequence=sequence, head_sha256=head_sha256)
        repaired = read_campaign_ledger(root)
        if len(repaired) != sequence:
            raise RuntimeError("ledger head repair verification failed")
        return _head_payload(sequence, head_sha256)
    finally:
        release_advisory_lock(lock_path, descriptor)


def append_campaign_ledger_event(
    output_root: str | Path,
    *,
    event_type: Literal[
        "campaign_registered",
        "campaign_report_published",
        "family_abandoned",
        "publication_attempted",
        "publication_succeeded",
        "publication_failed",
    ],
    attempt_id: str,
    campaign_id: str,
    registry_sha256: str,
    cell_id: str | None = None,
    state: str,
    manifest_sha256: str | None = None,
    package_relative_path: str | None = None,
    family_id: str | None = None,
    fdr_member_id: str | None = None,
    family_p_values: dict[str, float | None] | None = None,
    logical_cell_sha256: str | None = None,
    supersedes_manifest_sha256: str | None = None,
    environment_sha256: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Append one fsync'd event after verifying the complete existing chain."""

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LEDGER_LOCK_FILE_NAME
    if package_relative_path is not None and _safe_relative_path(
        root, package_relative_path
    ) is None:
        raise ValueError("package_relative_path must remain inside the ledger output root")
    candidate = {
        "attempt_id": attempt_id,
        "campaign_id": campaign_id,
        "cell_id": cell_id,
        "environment_sha256": environment_sha256,
        "error_code": error_code,
        "event_type": event_type,
        "family_id": family_id,
        "family_p_values": family_p_values,
        "fdr_member_id": fdr_member_id,
        "logical_cell_sha256": logical_cell_sha256,
        "manifest_sha256": manifest_sha256,
        "package_relative_path": package_relative_path,
        "registry_sha256": registry_sha256,
        "state": state,
        "supersedes_manifest_sha256": supersedes_manifest_sha256,
    }
    field_errors = _event_field_errors(candidate)
    if field_errors:
        raise ValueError("invalid ledger event fields:" + ";".join(field_errors))
    descriptor: int | None = acquire_advisory_lock(lock_path)
    try:
        entries = list(read_campaign_ledger(root))
        if event_type == "campaign_registered":
            for existing in entries:
                if (
                    existing.get("event_type") == "campaign_registered"
                    and str(existing.get("campaign_id", "")).casefold()
                    == str(campaign_id).casefold()
                ):
                    raise ValueError("campaign registration already exists or collides")
        if event_type == "campaign_report_published" and any(
            entry.get("event_type") == "campaign_report_published"
            and entry.get("campaign_id") == campaign_id
            for entry in entries
        ):
            raise ValueError("campaign report is already anchored")
        if event_type == "publication_succeeded":
            abandoned = {
                (str(entry.get("campaign_id")), str(entry.get("family_id")))
                for entry in entries
                if entry.get("event_type") == "family_abandoned"
            }
            successes = {
                str(entry["manifest_sha256"]): entry
                for entry in entries
                if entry.get("event_type") == "publication_succeeded"
                and entry.get("logical_cell_sha256") == logical_cell_sha256
                and (str(entry.get("campaign_id")), str(entry.get("family_id")))
                not in abandoned
            }
            superseded = {
                str(entry["supersedes_manifest_sha256"])
                for entry in successes.values()
                if entry.get("supersedes_manifest_sha256") is not None
            }
            active = sorted(set(successes) - superseded)
            if len(active) > 1:
                raise ValueError("ledger already has multiple active logical publications")
            expected_supersedes = active[0] if active else None
            if supersedes_manifest_sha256 != expected_supersedes:
                raise ValueError(
                    "publication supersession changed before ledger commit; "
                    f"expected={expected_supersedes!r}"
                )
        sequence = len(entries) + 1
        previous = entries[-1]["entry_sha256"] if entries else LEDGER_GENESIS_SHA256
        entry: dict[str, Any] = {
            "attempt_id": str(attempt_id),
            "campaign_id": str(campaign_id),
            "cell_id": cell_id,
            "entry_sha256": "",
            "environment_sha256": environment_sha256,
            "error_code": error_code,
            "event_type": event_type,
            "family_id": family_id,
            "family_p_values": family_p_values,
            "fdr_member_id": fdr_member_id,
            "logical_cell_sha256": logical_cell_sha256,
            "manifest_sha256": manifest_sha256,
            "package_relative_path": package_relative_path,
            "prior_entry_sha256": previous,
            "registry_sha256": registry_sha256,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "sequence": sequence,
            "state": state,
            "supersedes_manifest_sha256": supersedes_manifest_sha256,
        }
        entry["entry_sha256"] = _entry_digest(entry)
        path = campaign_ledger_path(root)
        prior_size = path.stat().st_size if path.exists() else 0
        with path.open("ab") as handle:
            handle.write(canonical_json_bytes(entry))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _write_head(root, sequence=sequence, head_sha256=entry["entry_sha256"])
        except Exception:
            # The head is the commit point. If it cannot advance, remove only
            # this uncommitted append while the exclusive ledger lock is held.
            with path.open("r+b") as handle:
                handle.truncate(prior_size)
                handle.flush()
                os.fsync(handle.fileno())
            raise
        return entry
    finally:
        release_advisory_lock(lock_path, descriptor)


def verify_campaign_ledger(output_root: str | Path) -> LedgerVerificationReport:
    """Validate chain, head, attempts, packages, FDR vectors, and supersession."""

    root = Path(output_root).resolve()
    entries, errors = _read_entries(root)
    head, head_errors = _read_head(root)
    errors.extend(head_errors)
    expected_hash = entries[-1]["entry_sha256"] if entries else LEDGER_GENESIS_SHA256
    if entries and head is None:
        errors.append("ledger_head_missing")
    elif head is not None and (
        head.get("sequence") != len(entries) or head.get("head_sha256") != expected_hash
    ):
        errors.append("ledger_head_mismatch")

    registrations: dict[str, str] = {}
    registered_campaigns: dict[str, CampaignRegistry] = {}
    attempts: dict[str, dict[str, Any]] = {}
    terminals: set[str] = set()
    manifests: dict[str, dict[str, Any]] = {}
    family_vectors: dict[tuple[str, str], bytes] = {}
    superseded: set[str] = set()
    abandoned_families: set[tuple[str, str]] = set()
    campaign_reports: dict[str, dict[str, Any]] = {}
    for entry in entries:
        event = entry.get("event_type")
        attempt_id = entry.get("attempt_id")
        campaign_id = entry.get("campaign_id")
        if event == "campaign_registered":
            prior = registrations.get(str(campaign_id))
            if prior is not None and prior != entry.get("registry_sha256"):
                errors.append(f"ledger_campaign_reregistered:{campaign_id}")
            registrations[str(campaign_id)] = str(entry.get("registry_sha256"))
            registry_path = _safe_relative_path(
                root, entry.get("package_relative_path")
            )
            if registry_path is None:
                errors.append(f"ledger_registered_registry_path_invalid:{campaign_id}")
            elif not registry_path.is_file() or registry_path.is_symlink():
                errors.append(f"ledger_registered_registry_missing:{campaign_id}")
            elif sha256_file(registry_path) != entry.get("registry_sha256"):
                errors.append(f"ledger_registered_registry_hash_mismatch:{campaign_id}")
            else:
                try:
                    value = json.loads(registry_path.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise TypeError("registry is not an object")
                    registered_campaigns[str(campaign_id)] = CampaignRegistry.from_dict(value)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    errors.append(f"ledger_registered_registry_invalid:{campaign_id}")
        elif event == "campaign_report_published":
            campaign_key = str(campaign_id)
            if campaign_key in campaign_reports:
                errors.append(f"ledger_duplicate_campaign_report:{campaign_id}")
            campaign_reports[campaign_key] = entry
            if registrations.get(campaign_key) != entry.get("registry_sha256"):
                errors.append(f"ledger_report_without_registration:{campaign_id}")
            report_path = _safe_relative_path(root, entry.get("package_relative_path"))
            report_sha = entry.get("manifest_sha256")
            if report_path is None:
                errors.append(f"ledger_report_path_invalid:{campaign_id}")
            elif not report_path.is_file() or report_path.is_symlink():
                errors.append(f"ledger_report_missing:{campaign_id}")
            elif not isinstance(report_sha, str) or sha256_file(report_path) != report_sha:
                errors.append(f"ledger_report_hash_mismatch:{campaign_id}")
            registry = registered_campaigns.get(campaign_key)
            if registry is not None:
                try:
                    registry.family(str(entry.get("family_id")))
                except (KeyError, ValueError):
                    errors.append(f"ledger_report_family_invalid:{campaign_id}")
        elif event == "family_abandoned":
            family_key = (str(campaign_id), str(entry.get("family_id")))
            if family_key in abandoned_families:
                errors.append(f"ledger_duplicate_family_abandonment:{campaign_id}:{family_key[1]}")
            abandoned_families.add(family_key)
            if registrations.get(str(campaign_id)) != entry.get("registry_sha256"):
                errors.append(f"ledger_abandonment_without_registration:{campaign_id}")
            required_null = (
                "cell_id",
                "environment_sha256",
                "family_p_values",
                "fdr_member_id",
                "logical_cell_sha256",
                "manifest_sha256",
                "package_relative_path",
                "supersedes_manifest_sha256",
            )
            if any(entry.get(key) is not None for key in required_null):
                errors.append(f"ledger_abandonment_fields_invalid:{campaign_id}:{family_key[1]}")
            if entry.get("state") != "abandoned" or not isinstance(
                entry.get("error_code"), str
            ):
                errors.append(f"ledger_abandonment_state_invalid:{campaign_id}:{family_key[1]}")
        elif event == "publication_attempted":
            if attempt_id in attempts:
                errors.append(f"ledger_duplicate_attempt:{attempt_id}")
            attempts[str(attempt_id)] = entry
        elif event in {"publication_succeeded", "publication_failed"}:
            if attempt_id not in attempts:
                errors.append(f"ledger_terminal_without_attempt:{attempt_id}")
            else:
                attempted = attempts[str(attempt_id)]
                immutable_attempt_fields = (
                    "campaign_id",
                    "cell_id",
                    "environment_sha256",
                    "family_id",
                    "family_p_values",
                    "fdr_member_id",
                    "logical_cell_sha256",
                    "package_relative_path",
                    "registry_sha256",
                    "supersedes_manifest_sha256",
                )
                if any(
                    attempted.get(key) != entry.get(key)
                    for key in immutable_attempt_fields
                ):
                    errors.append(f"ledger_attempt_terminal_mismatch:{attempt_id}")
            if attempt_id in terminals:
                errors.append(f"ledger_duplicate_terminal:{attempt_id}")
            terminals.add(str(attempt_id))
            if event == "publication_succeeded":
                family_key = (str(campaign_id), str(entry.get("family_id")))
                if family_key in abandoned_families:
                    errors.append(
                        f"ledger_success_after_family_abandonment:{campaign_id}:{family_key[1]}"
                    )
                if registrations.get(str(campaign_id)) != entry.get("registry_sha256"):
                    errors.append(f"ledger_success_without_registration:{attempt_id}")
                manifest_sha = entry.get("manifest_sha256")
                relative = entry.get("package_relative_path")
                if not isinstance(manifest_sha, str) or not isinstance(relative, str):
                    errors.append(f"ledger_success_missing_package_identity:{attempt_id}")
                    continue
                package_path = _safe_relative_path(root, relative)
                manifest_path = (
                    package_path / "manifest.json" if package_path is not None else None
                )
                if package_path is None:
                    errors.append(f"ledger_package_path_invalid:{manifest_sha}")
                elif manifest_path is None or not manifest_path.is_file() or manifest_path.is_symlink():
                    errors.append(f"ledger_package_missing:{manifest_sha}")
                elif sha256_file(manifest_path) != manifest_sha:
                    errors.append(f"ledger_manifest_hash_mismatch:{manifest_sha}")
                manifests[manifest_sha] = entry
                family_id = entry.get("family_id")
                family_values = entry.get("family_p_values")
                family_key = (str(campaign_id), str(family_id))
                vector = canonical_json_bytes(family_values)
                existing = family_vectors.get(family_key)
                if existing is not None and existing != vector:
                    errors.append(
                        f"ledger_family_p_vector_mismatch:{campaign_id}:{family_id}"
                    )
                family_vectors[family_key] = vector
                member_id = entry.get("fdr_member_id")
                if not isinstance(family_values, dict) or member_id not in family_values:
                    errors.append(f"ledger_family_member_missing:{attempt_id}")
                elif entry.get("state") not in {"accepted", "rejected"}:
                    errors.append(f"ledger_success_state_invalid:{attempt_id}")
            elif entry.get("manifest_sha256") is not None:
                errors.append(f"ledger_failed_attempt_has_manifest:{attempt_id}")
    for attempt_id in sorted(set(attempts) - terminals):
        errors.append(f"ledger_attempt_without_terminal:{attempt_id}")
    for campaign_id, registry in registered_campaigns.items():
        successful = [
            entry
            for entry in manifests.values()
            if entry.get("campaign_id") == campaign_id
        ]
        for family in registry.fdr_families:
            family_key = (campaign_id, family.family_id)
            members = {
                str(entry.get("fdr_member_id"))
                for entry in successful
                if entry.get("family_id") == family.family_id
            }
            if family_key in abandoned_families:
                if not members or members == set(family.member_ids):
                    errors.append(
                        f"ledger_invalid_family_abandonment:{campaign_id}:{family.family_id}"
                    )
            elif members and members != set(family.member_ids):
                errors.append(
                    f"ledger_incomplete_published_family:{campaign_id}:{family.family_id}:"
                    f"missing={sorted(set(family.member_ids) - members)}"
                )
    supersession_manifests = {
        manifest_sha: entry
        for manifest_sha, entry in manifests.items()
        if (str(entry.get("campaign_id")), str(entry.get("family_id")))
        not in abandoned_families
    }
    for manifest_sha, entry in supersession_manifests.items():
        supersedes = entry.get("supersedes_manifest_sha256")
        if supersedes is None:
            continue
        prior = supersession_manifests.get(str(supersedes))
        if prior is None:
            errors.append(f"ledger_dangling_supersession:{manifest_sha}")
        elif prior.get("logical_cell_sha256") != entry.get("logical_cell_sha256"):
            errors.append(f"ledger_cross_cell_supersession:{manifest_sha}")
        elif str(supersedes) in superseded:
            errors.append(f"ledger_duplicate_supersession:{supersedes}")
        else:
            superseded.add(str(supersedes))
    active_by_logical: dict[str, list[str]] = {}
    for manifest_sha, entry in supersession_manifests.items():
        if manifest_sha in superseded:
            continue
        logical = entry.get("logical_cell_sha256")
        if isinstance(logical, str):
            active_by_logical.setdefault(logical, []).append(manifest_sha)
    for logical, active in sorted(active_by_logical.items()):
        if len(active) > 1:
            errors.append(
                f"ledger_multiple_active_publications:{logical}:{sorted(active)}"
            )
    return LedgerVerificationReport(
        ok=not errors,
        errors=tuple(errors),
        entry_count=len(entries),
        head_sha256=expected_hash if entries else None,
        superseded_manifest_sha256=tuple(sorted(superseded)),
    )


def abandon_incomplete_family(
    output_root: str | Path,
    registry: CampaignRegistry,
    *,
    family_id: str,
    reason_code: str,
) -> dict[str, Any]:
    """Append an auditable terminal for a partial family that cannot be resumed.

    Abandonment is allowed only when the ledger has exactly one semantic error:
    the named registered family is incomplete. Package and hash failures can
    never be waived through this transition.
    """

    if not isinstance(reason_code, str) or not re.fullmatch(r"[a-z0-9_]{3,80}", reason_code):
        raise ValueError("reason_code must be a lowercase machine-readable identifier")
    family = registry.family(family_id)
    root = Path(output_root).resolve()
    entries = read_campaign_ledger(root)
    existing = [
        entry
        for entry in entries
        if entry.get("event_type") == "family_abandoned"
        and entry.get("campaign_id") == registry.campaign_id
        and entry.get("family_id") == family.family_id
    ]
    if existing:
        if len(existing) == 1 and existing[0].get("error_code") == reason_code:
            return existing[0]
        raise ValueError("family already has a different abandonment transition")
    report = verify_campaign_ledger(root)
    expected_prefix = (
        f"ledger_incomplete_published_family:{registry.campaign_id}:{family.family_id}:"
    )
    if len(report.errors) != 1 or not report.errors[0].startswith(expected_prefix):
        raise ValueError(
            "family abandonment requires exactly one incomplete-family ledger error"
        )
    registered = [
        entry
        for entry in entries
        if entry.get("event_type") == "campaign_registered"
        and entry.get("campaign_id") == registry.campaign_id
        and entry.get("registry_sha256") == registry.registration_sha256
    ]
    if len(registered) != 1:
        raise ValueError("incomplete family registry is not uniquely anchored")
    entry = append_campaign_ledger_event(
        root,
        event_type="family_abandoned",
        attempt_id=f"abandon:{registry.campaign_id}:{family.family_id}",
        campaign_id=registry.campaign_id,
        registry_sha256=registry.registration_sha256,
        state="abandoned",
        family_id=family.family_id,
        error_code=reason_code,
    )
    repaired = verify_campaign_ledger(root)
    if not repaired.ok:
        raise RuntimeError(f"family abandonment did not restore ledger validity:{repaired.errors}")
    return entry


def anchor_campaign_report(
    output_root: str | Path,
    registry: CampaignRegistry,
    *,
    family_id: str,
    report_path: str | Path,
) -> dict[str, Any]:
    """Bind one immutable campaign-level report to the append-only ledger."""

    root = Path(output_root).resolve()
    path = Path(report_path).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise ValueError("campaign report must be a regular file inside output_root")
    registry.family(family_id)
    report_sha256 = sha256_file(path)
    relative = path.relative_to(root).as_posix()
    entries = read_campaign_ledger(root)
    existing = [
        entry
        for entry in entries
        if entry.get("event_type") == "campaign_report_published"
        and entry.get("campaign_id") == registry.campaign_id
    ]
    if existing:
        if (
            len(existing) == 1
            and existing[0].get("registry_sha256") == registry.registration_sha256
            and existing[0].get("family_id") == family_id
            and existing[0].get("manifest_sha256") == report_sha256
            and existing[0].get("package_relative_path") == relative
        ):
            return existing[0]
        raise ValueError("campaign report already has a different ledger anchor")
    entry = append_campaign_ledger_event(
        root,
        event_type="campaign_report_published",
        attempt_id=f"report:{registry.campaign_id}:{report_sha256}",
        campaign_id=registry.campaign_id,
        registry_sha256=registry.registration_sha256,
        state="reported",
        manifest_sha256=report_sha256,
        package_relative_path=relative,
        family_id=family_id,
    )
    verified = verify_campaign_ledger(root)
    if not verified.ok:
        raise RuntimeError(f"campaign report anchor failed verification:{verified.errors}")
    return entry


def successful_ledger_entry(
    output_root: str | Path, manifest_sha256: str
) -> dict[str, Any] | None:
    for entry in read_campaign_ledger(output_root):
        if (
            entry.get("event_type") == "publication_succeeded"
            and entry.get("manifest_sha256") == manifest_sha256
        ):
            return entry
    return None
