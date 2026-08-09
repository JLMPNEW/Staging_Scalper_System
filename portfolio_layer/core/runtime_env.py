"""Local runtime-environment hydration for Windows scheduled processes.

Windows processes inherit an environment snapshot at process creation. A
long-lived scheduler/IDE can therefore miss user-scoped variables added later.
Only the fixed allowlist below is eligible, existing process values always win,
and values are never logged or persisted.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable


PORTFOLIO_USER_ENV_ALLOWLIST = (
    "ALPHAVANTAGE_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "ALPHAVANTAGE_PREMIUM_API_KEY",
    "ALPHA_VANTAGE_PREMIUM_API_KEY",
    "BIOTECH_DB_PATH",
    "EIA_API_KEY",
    "FMP_API_KEY",
    "FRED_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "IBKR_MONITOR_ACCOUNT",
    "INDUSTRIALS_DB_DIR",
    "MARKET_POSITIONING_DB_PATH",
    "PORTFOLIO_LAYER_DB_DIR",
    "SEC_INSIDER_DB_PATH",
    "TIINGO_API_KEY",
)


def _windows_user_environment_value(name: str) -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    text = str(value).strip()
    return text or None


def hydrate_missing_user_environment(
    names: Iterable[str] = PORTFOLIO_USER_ENV_ALLOWLIST,
    *,
    reader: Callable[[str], str | None] | None = None,
) -> tuple[str, ...]:
    """Fill missing process variables from local user scope.

    Returns names only. Values remain exclusively in the process environment.
    """
    source = reader or _windows_user_environment_value
    hydrated: list[str] = []
    for raw_name in names:
        name = str(raw_name).strip()
        if not name or os.environ.get(name):
            continue
        value = source(name)
        if value:
            os.environ[name] = value
            hydrated.append(name)
    return tuple(hydrated)
