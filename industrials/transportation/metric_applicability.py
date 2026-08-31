from __future__ import annotations

import math
from typing import Mapping


def finite_number(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def explicit_zero_debt_interest_na(financial: Mapping[str, object]) -> bool:
    """Return whether interest coverage is undefined for a debt-free issuer."""
    debt = finite_number(financial.get("total_debt_usd"))
    interest = finite_number(financial.get("interest_expense_ttm_usd"))
    return debt == 0.0 and interest in {None, 0.0}
