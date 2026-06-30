from __future__ import annotations

import logging
import re


LOGGER = logging.getLogger(__name__)
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,14}$")
NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
ORG_SUFFIX_RE = re.compile(
    r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|PLC|N\.?V\.?|NV|AG|SE|SA|LLC|LP|HOLDINGS?|GROUP|THE)\b"
)


def normalize_ticker(raw: object) -> str:
    ticker = str(raw or "").strip().upper().replace(".", "-")
    if not ticker:
        return ""
    if not TICKER_RE.fullmatch(ticker):
        LOGGER.debug("Invalid ticker value ignored: %r", raw)
        return ""
    return ticker


def normalize_cik(raw: object) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def normalize_label(raw: object) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def normalize_org_name(raw: object) -> str:
    text = str(raw or "").upper().replace("&", " AND ")
    text = ORG_SUFFIX_RE.sub(" ", text)
    text = NON_ALNUM_RE.sub(" ", text)
    return " ".join(part for part in text.split() if part)


def as_bool(raw: object, *, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default

