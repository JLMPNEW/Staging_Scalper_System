from __future__ import annotations

from collections.abc import Mapping
from typing import Any


UNRELIABLE_CASH_RUNWAY_PROXIES = frozenset(
    {
        "cash_only_for_cash_and_investments",
        "cash_and_equivalents_for_cash_and_investments",
        "long_term_investments_only_for_cash_and_investments",
        "reported_investments_total_only_for_cash_and_investments",
        "net_income_for_ttm_cash_burn",
        "partial_quarter_annualized_operating_cash_flow",
        "annualized_ytd_operating_cash_flow",
    }
)


def proxy_field_names(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = str(raw or "").replace("|", ";").split(";")
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def cash_runway_is_reliable(record: Mapping[str, Any] | None) -> bool:
    if not record:
        return False
    explicit = record.get("cash_runway_reliable_flag")
    if explicit not in {None, ""}:
        try:
            return float(str(explicit).strip()) > 0.0
        except (TypeError, ValueError):
            return str(explicit).strip().lower() in {"true", "yes", "y"}
    proxies = set(proxy_field_names(record.get("proxy_fields_used")))
    return not bool(proxies & UNRELIABLE_CASH_RUNWAY_PROXIES)
