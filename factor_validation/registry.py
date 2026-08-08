"""Immutable campaign and provenance registration for factor evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import Any, Literal

from factor_validation.core import CONTRACT_VERSION
from factor_validation.fdr import FDRFamily


REGISTRY_SCHEMA_VERSION = "factor_validation_campaign_registry_v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value into stable UTF-8 bytes."""

    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_id(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must start with an alphanumeric character and contain only "
            "letters, numbers, '.', '_', or '-'"
        )
    return normalized


def _nonblank(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _positive_int(value: int, *, field_name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _sha256(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase 64-character SHA-256 digest")
    return normalized


def _logical_path(value: str) -> str:
    text = str(value or "").strip().replace(chr(92), "/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise ValueError("logical_path must be a safe relative POSIX path")
    return path.as_posix()


@dataclass(frozen=True)
class FileSeal:
    """Content identity for one registered source, config, or code file."""

    logical_path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_path", _logical_path(self.logical_path))
        object.__setattr__(self, "sha256", _sha256(self.sha256, field_name="sha256"))
        object.__setattr__(
            self,
            "size_bytes",
            _positive_int(self.size_bytes, field_name="size_bytes", minimum=0),
        )

    @classmethod
    def from_path(cls, path: str | Path, *, logical_path: str) -> FileSeal:
        source = Path(path)
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"registered file must be a regular non-symlink file: {source}")
        return cls(
            logical_path=logical_path,
            sha256=sha256_file(source),
            size_bytes=source.stat().st_size,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FileSeal:
        return cls(
            logical_path=value.get("logical_path", ""),
            sha256=value.get("sha256", ""),
            size_bytes=value.get("size_bytes", -1),
        )


@dataclass(frozen=True)
class ObservedProvenance:
    """Hashes observed at evidence runtime, compared with pre-registration."""

    config_sha256: str
    source_files: tuple[FileSeal, ...]
    code_files: tuple[FileSeal, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "config_sha256",
            _sha256(self.config_sha256, field_name="config_sha256"),
        )
        for field_name in ("source_files", "code_files"):
            seals = tuple(sorted(getattr(self, field_name), key=lambda item: item.logical_path))
            if not seals:
                raise ValueError(f"{field_name} must contain at least one observed seal")
            paths = [item.logical_path for item in seals]
            if len(set(paths)) != len(paths):
                raise ValueError(f"{field_name} logical paths must be unique")
            object.__setattr__(self, field_name, seals)

    @classmethod
    def from_paths(
        cls,
        *,
        config_path: str | Path,
        source_paths: Mapping[str, str | Path],
        code_paths: Mapping[str, str | Path],
    ) -> ObservedProvenance:
        config = Path(config_path)
        if not config.is_file() or config.is_symlink():
            raise ValueError(f"config must be a regular non-symlink file: {config}")
        return cls(
            config_sha256=sha256_file(config),
            source_files=tuple(
                FileSeal.from_path(path, logical_path=logical_path)
                for logical_path, path in source_paths.items()
            ),
            code_files=tuple(
                FileSeal.from_path(path, logical_path=logical_path)
                for logical_path, path in code_paths.items()
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_files": [item.to_dict() for item in self.code_files],
            "config_sha256": self.config_sha256,
            "source_files": [item.to_dict() for item in self.source_files],
        }

    @property
    def observed_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_dict()))


@dataclass(frozen=True)
class ValidationCellRegistration:
    """Pre-registered identity and provenance for one factor/target/horizon cell."""

    cell_id: str
    sector_id: str
    factor_id: str
    target_name: str
    horizon_trading_days: int
    entry_lag_trading_days: int
    factor_direction: Literal["higher_is_better", "lower_is_better"]
    evaluation_step_trading_days: int
    fdr_family_id: str
    fdr_member_id: str
    config_sha256: str
    source_files: tuple[FileSeal, ...]
    code_files: tuple[FileSeal, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cell_id", _safe_id(self.cell_id, field_name="cell_id"))
        object.__setattr__(self, "sector_id", _safe_id(self.sector_id, field_name="sector_id"))
        object.__setattr__(self, "factor_id", _safe_id(self.factor_id, field_name="factor_id"))
        object.__setattr__(self, "target_name", _nonblank(self.target_name, field_name="target_name"))
        object.__setattr__(
            self,
            "horizon_trading_days",
            _positive_int(self.horizon_trading_days, field_name="horizon_trading_days"),
        )
        object.__setattr__(
            self,
            "entry_lag_trading_days",
            _positive_int(
                self.entry_lag_trading_days,
                field_name="entry_lag_trading_days",
                minimum=0,
            ),
        )
        object.__setattr__(
            self,
            "evaluation_step_trading_days",
            _positive_int(
                self.evaluation_step_trading_days,
                field_name="evaluation_step_trading_days",
            ),
        )
        if self.factor_direction not in {"higher_is_better", "lower_is_better"}:
            raise ValueError("factor_direction must be 'higher_is_better' or 'lower_is_better'")
        object.__setattr__(
            self,
            "fdr_family_id",
            _safe_id(self.fdr_family_id, field_name="fdr_family_id"),
        )
        object.__setattr__(
            self,
            "fdr_member_id",
            _nonblank(self.fdr_member_id, field_name="fdr_member_id"),
        )
        object.__setattr__(
            self,
            "config_sha256",
            _sha256(self.config_sha256, field_name="config_sha256"),
        )
        source_files = tuple(sorted(self.source_files, key=lambda item: item.logical_path))
        code_files = tuple(sorted(self.code_files, key=lambda item: item.logical_path))
        if not source_files or not code_files:
            raise ValueError("source_files and code_files must each contain at least one seal")
        for field_name, seals in (("source_files", source_files), ("code_files", code_files)):
            paths = [item.logical_path for item in seals]
            if len(set(paths)) != len(paths):
                raise ValueError(f"{field_name} logical paths must be unique")
        object.__setattr__(self, "source_files", source_files)
        object.__setattr__(self, "code_files", code_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "code_files": [item.to_dict() for item in self.code_files],
            "config_sha256": self.config_sha256,
            "entry_lag_trading_days": self.entry_lag_trading_days,
            "evaluation_step_trading_days": self.evaluation_step_trading_days,
            "factor_direction": self.factor_direction,
            "factor_id": self.factor_id,
            "fdr_family_id": self.fdr_family_id,
            "fdr_member_id": self.fdr_member_id,
            "horizon_trading_days": self.horizon_trading_days,
            "sector_id": self.sector_id,
            "source_files": [item.to_dict() for item in self.source_files],
            "target_name": self.target_name,
        }

    @property
    def registration_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_dict()))

    @property
    def registered_provenance(self) -> ObservedProvenance:
        return ObservedProvenance(
            config_sha256=self.config_sha256,
            source_files=self.source_files,
            code_files=self.code_files,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ValidationCellRegistration:
        source_values = value.get("source_files", [])
        code_values = value.get("code_files", [])
        if not isinstance(source_values, list) or not isinstance(code_values, list):
            raise TypeError("source_files and code_files must be lists")
        return cls(
            cell_id=value.get("cell_id", ""),
            sector_id=value.get("sector_id", ""),
            factor_id=value.get("factor_id", ""),
            target_name=value.get("target_name", ""),
            horizon_trading_days=value.get("horizon_trading_days", 0),
            entry_lag_trading_days=value.get("entry_lag_trading_days", -1),
            factor_direction=value.get("factor_direction", ""),
            evaluation_step_trading_days=value.get("evaluation_step_trading_days", 0),
            fdr_family_id=value.get("fdr_family_id", ""),
            fdr_member_id=value.get("fdr_member_id", ""),
            config_sha256=value.get("config_sha256", ""),
            source_files=tuple(FileSeal.from_dict(item) for item in source_values),
            code_files=tuple(FileSeal.from_dict(item) for item in code_values),
        )


@dataclass(frozen=True)
class CampaignRegistry:
    """Complete, hash-sealed registration for one validation campaign."""

    campaign_id: str
    cells: tuple[ValidationCellRegistration, ...]
    fdr_families: tuple[FDRFamily, ...]
    contract_version: str = CONTRACT_VERSION
    schema_version: str = REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "campaign_id",
            _safe_id(self.campaign_id, field_name="campaign_id"),
        )
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"contract_version must be {CONTRACT_VERSION!r}")
        if self.schema_version != REGISTRY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {REGISTRY_SCHEMA_VERSION!r}")
        cells = tuple(sorted(self.cells, key=lambda item: item.cell_id))
        families = tuple(
            sorted(
                (
                    FDRFamily(
                        family_id=item.family_id,
                        member_ids=tuple(sorted(item.member_ids)),
                        alpha=item.alpha,
                    )
                    for item in self.fdr_families
                ),
                key=lambda item: item.family_id,
            )
        )
        if not cells or not families:
            raise ValueError("a campaign must register at least one cell and one FDR family")
        if len({item.cell_id for item in cells}) != len(cells):
            raise ValueError("campaign cell_id values must be unique")
        if len({item.family_id for item in families}) != len(families):
            raise ValueError("campaign FDR family_id values must be unique")
        cell_keys = [
            (item.sector_id, item.factor_id, item.target_name, item.horizon_trading_days)
            for item in cells
        ]
        if len(set(cell_keys)) != len(cell_keys):
            raise ValueError("campaign factor/target/horizon cell identities must be unique")
        families_by_id = {item.family_id: item for item in families}
        assignments: dict[str, list[str]] = {item.family_id: [] for item in families}
        for cell in cells:
            family = families_by_id.get(cell.fdr_family_id)
            if family is None:
                raise ValueError(f"cell {cell.cell_id!r} references an unregistered FDR family")
            if cell.fdr_member_id not in family.member_ids:
                raise ValueError(
                    f"cell {cell.cell_id!r} member is absent from family {family.family_id!r}"
                )
            assignments[family.family_id].append(cell.fdr_member_id)
        for family in families:
            assigned = assignments[family.family_id]
            if len(set(assigned)) != len(assigned):
                raise ValueError(f"FDR family {family.family_id!r} has duplicate cell assignments")
            if set(assigned) != set(family.member_ids):
                missing = sorted(set(family.member_ids) - set(assigned))
                extra = sorted(set(assigned) - set(family.member_ids))
                raise ValueError(
                    f"FDR family {family.family_id!r} registration mismatch: "
                    f"missing={missing}; extra={extra}"
                )
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "fdr_families", families)

    def cell(self, cell_id: str) -> ValidationCellRegistration:
        normalized = _safe_id(cell_id, field_name="cell_id")
        for cell in self.cells:
            if cell.cell_id == normalized:
                return cell
        raise KeyError(f"unregistered validation cell {normalized!r}")

    def family(self, family_id: str) -> FDRFamily:
        normalized = _safe_id(family_id, field_name="fdr_family_id")
        for family in self.fdr_families:
            if family.family_id == normalized:
                return family
        raise KeyError(f"unregistered FDR family {normalized!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "cells": [item.to_dict() for item in self.cells],
            "contract_version": self.contract_version,
            "fdr_families": [
                {
                    "alpha": family.alpha,
                    "family_id": family.family_id,
                    "member_ids": sorted(family.member_ids),
                    "registration_sha256": family.registration_sha256,
                }
                for family in self.fdr_families
            ],
            "schema_version": self.schema_version,
        }

    @property
    def registration_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_dict()))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CampaignRegistry:
        cells_value = value.get("cells", [])
        families_value = value.get("fdr_families", [])
        if not isinstance(cells_value, list) or not isinstance(families_value, list):
            raise TypeError("registry cells and fdr_families must be lists")
        if any(not isinstance(item, dict) for item in cells_value):
            raise TypeError("registry cell records must be objects")
        families: list[FDRFamily] = []
        for item in families_value:
            if not isinstance(item, dict):
                raise TypeError("FDR family records must be objects")
            member_ids = item.get("member_ids", [])
            if not isinstance(member_ids, list):
                raise TypeError("FDR member_ids must be a list")
            family = FDRFamily(
                family_id=item.get("family_id", ""),
                member_ids=tuple(member_ids),
                alpha=item.get("alpha", math.nan),
            )
            if item.get("registration_sha256") != family.registration_sha256:
                raise ValueError(f"FDR family {family.family_id!r} registration digest mismatch")
            families.append(family)
        return cls(
            campaign_id=value.get("campaign_id", ""),
            cells=tuple(
                ValidationCellRegistration.from_dict(item)
                for item in cells_value
            ),
            fdr_families=tuple(families),
            contract_version=value.get("contract_version", ""),
            schema_version=value.get("schema_version", ""),
        )
