from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "helper_scripts"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from build_form4_buy_events_v1 import (  # noqa: E402
    DEFAULT_SCORING_PARAMS,
    render_tier1_sql,
)


def test_form4_tier1_accepts_common_shares_security_title() -> None:
    sql = render_tier1_sql(DEFAULT_SCORING_PARAMS)
    assert "LIKE '%common shares%'" in sql
