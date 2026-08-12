"""Shared atomic-snapshot fences for local Norgate provider reads."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


NORGATE_EQUITY_DATABASES = ("US Equities", "US Equities Delisted")
NORGATE_MEMBERSHIP_DATABASES = (*NORGATE_EQUITY_DATABASES, "US Indices")


class NorgateSnapshotChanged(RuntimeError):
    """Raised when any consumed Norgate database changes during extraction."""

    def __init__(
        self,
        *,
        context: str,
        expected: Mapping[str, str],
        observed: Mapping[str, str],
    ) -> None:
        self.context = context
        self.expected = dict(expected)
        self.observed = dict(observed)
        self.changed_databases = tuple(
            database
            for database in self.expected
            if self.observed.get(database) != self.expected[database]
        )
        super().__init__(
            "Norgate provider databases changed "
            f"{context}; changed={list(self.changed_databases)} "
            f"start={self.expected} current={self.observed}. Rerun required."
        )


def norgate_database_fingerprint(
    provider: Any,
    databases: Iterable[str],
) -> dict[str, str]:
    """Capture update timestamps for every provider database being consumed."""
    return {
        str(database): str(provider.last_database_update_time(str(database)))
        for database in databases
    }


def require_norgate_snapshot(
    provider: Any,
    expected: Mapping[str, str],
    *,
    context: str,
) -> dict[str, str]:
    """Re-read a fingerprint and fail when any component has changed."""
    observed = norgate_database_fingerprint(provider, expected)
    if observed != dict(expected):
        raise NorgateSnapshotChanged(
            context=context,
            expected=expected,
            observed=observed,
        )
    return observed


__all__ = [
    "NORGATE_EQUITY_DATABASES",
    "NORGATE_MEMBERSHIP_DATABASES",
    "NorgateSnapshotChanged",
    "norgate_database_fingerprint",
    "require_norgate_snapshot",
]
