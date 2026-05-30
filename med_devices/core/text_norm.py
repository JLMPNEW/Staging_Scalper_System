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


def normalize_code(raw: object) -> str:
    text = str(raw or "").upper().strip()
    base = re.split(r"[-\s]", text, maxsplit=1)[0]
    return "".join(ch for ch in base if ch.isalnum())[:12]


def normalize_submission_identifier(raw: object) -> str:
    value = re.sub(r"[^A-Z0-9-]+", "", str(raw or "").upper().strip())
    if not value or not any(ch.isdigit() for ch in value):
        return ""
    unsupported = {
        "510KDENOVOPIPELINE",
        "510KPIPELINE",
        "CLASSIREGISTRY",
        "DISTRIBUTIONONLY",
        "DMF",
        "FEI-ONLY",
        "FEIONLY",
        "MASTERFILE",
        "PMA-PIPELINE",
        "PMAPIPELINE",
    }
    return "" if value in unsupported else value


def as_bool(raw: object, *, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default
