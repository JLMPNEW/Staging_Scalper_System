from __future__ import annotations

import logging
import re


LOGGER = logging.getLogger(__name__)
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,14}$")


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


def normalize_org_name(raw: object) -> str:
    text = str(raw or "").upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_subsector(raw: object) -> str:
    text = normalize_org_name(raw).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y"}

