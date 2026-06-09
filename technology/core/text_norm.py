from __future__ import annotations

import re
from typing import Any


NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
ORG_SUFFIX_RE = re.compile(
    r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|PLC|N\.?V\.?|NV|AG|SE|SA|LLC|LP|ADR|ADS)\b"
)


def normalize_ticker(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return ""
    return text.replace(".", "-")


def normalize_cik(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def as_bool(raw: Any) -> bool:
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def normalize_label(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def normalize_org_name(raw: Any) -> str:
    text = str(raw or "").upper()
    text = ORG_SUFFIX_RE.sub(" ", text)
    text = NON_ALNUM_RE.sub(" ", text)
    return " ".join(part for part in text.split() if part)

