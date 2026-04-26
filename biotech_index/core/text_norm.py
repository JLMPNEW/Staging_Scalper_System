from __future__ import annotations

import re
from dataclasses import dataclass


CORPORATE_SUFFIXES = {
    "INC",
    "INCORPORATED",
    "CORP",
    "CORPORATION",
    "COMPANY",
    "CO",
    "LTD",
    "LIMITED",
    "PLC",
    "AG",
    "NV",
    "N V",
    "SA",
    "S A",
    "SAS",
    "S A S",
    "SE",
    "LP",
    "LLC",
    "HOLDINGS",
    "HOLDING",
    "GROUP",
}
MULTI_TOKEN_CORPORATE_SUFFIXES = (
    ("S", "A", "S"),
    ("S", "A"),
    ("N", "V"),
)

GENERIC_ORG_TOKENS = set(CORPORATE_SUFFIXES) | {
    "THERAPEUTICS",
    "THERAPEUTIC",
    "THERAPEUT",
    "PHARMACEUTICALS",
    "PHARMACEUTICAL",
    "BIOPHARMA",
    "BIOPHARMACEUTICALS",
    "BIOSCIENCES",
    "BIOSCIENCE",
    "BIOTECH",
    "BIOTECHNOLOGY",
    "MEDICINES",
    "MEDICINE",
    "LABORATORIES",
    "LABS",
    "ONCOLOGY",
    "HEALTH",
    "RESEARCH",
    "HOLDI",
    "HOLDIN",
    "ADR",
    "ADS",
    "SPON",
    "SPONSORED",
    "CLASS",
    "CL",
}


@dataclass(frozen=True)
class AliasCandidate:
    alias_raw: str
    alias_norm: str
    source: str
    confidence: float = 1.0
    is_manual: bool = False


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_cik(raw: object) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def clean_name_for_query(raw: str) -> str:
    text = str(raw or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_org_name(raw: str) -> str:
    text = str(raw or "").upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_security_suffixes(norm_name: str) -> str:
    tokens = [tok for tok in str(norm_name or "").split() if tok]
    while tokens:
        if tokens[-1] in {"ADR", "ADS"}:
            tokens.pop()
            if tokens and tokens[-1] in {"SPON", "SPONSORED"}:
                tokens.pop()
            continue
        if len(tokens) >= 2 and tokens[-2] in {"CLASS", "CL"} and tokens[-1] in {"A", "B", "C"}:
            tokens = tokens[:-2]
            continue
        if len(tokens) >= 2 and tokens[-2] in {"HOLDING", "HOLDINGS", "HOLDIN", "HOLDI"} and tokens[-1] in {"A", "B", "C"}:
            tokens.pop()
            continue
        if tokens[-1] in {"HOLDIN", "HOLDI", "MASS"}:
            tokens.pop()
            continue
        break
    return " ".join(tokens).strip()


def strip_corporate_suffixes(norm_name: str) -> str:
    tokens = [tok for tok in strip_security_suffixes(str(norm_name or "")).split() if tok]
    while tokens:
        removed = False
        for suffix in MULTI_TOKEN_CORPORATE_SUFFIXES:
            if len(tokens) >= len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                tokens = tokens[: -len(suffix)]
                removed = True
                break
        if removed:
            continue
        if tokens[-1] in CORPORATE_SUFFIXES:
            tokens.pop()
            continue
        break
    return " ".join(tokens).strip()


def meaningful_org_tokens(norm_name: str) -> list[str]:
    stripped = strip_corporate_suffixes(strip_security_suffixes(str(norm_name or "").strip()))
    return [
        tok
        for tok in stripped.split()
        if tok and tok not in GENERIC_ORG_TOKENS and len(tok) >= 3
    ]


def build_company_aliases(company_name: str) -> list[AliasCandidate]:
    norm_company = strip_security_suffixes(normalize_org_name(company_name))
    norm_stripped = strip_corporate_suffixes(norm_company)
    core_tokens = meaningful_org_tokens(norm_stripped)
    core_alias = " ".join(core_tokens).title() if core_tokens and (len(core_tokens) >= 2 or len(core_tokens[0]) >= 5) else ""

    raw_aliases: list[tuple[str, str, float]] = []
    for source, candidate, confidence in (
        ("company_name", clean_name_for_query(company_name), 1.0),
        ("suffix_stripped", clean_name_for_query(norm_stripped.title()), 0.95),
        ("core_tokens", clean_name_for_query(core_alias), 0.85),
    ):
        if candidate:
            raw_aliases.append((source, candidate, confidence))

    out: list[AliasCandidate] = []
    seen: set[tuple[str, str]] = set()
    for source, alias_raw, confidence in raw_aliases:
        for alias_text in (alias_raw, strip_corporate_suffixes(normalize_org_name(alias_raw)).title()):
            alias_norm = normalize_org_name(alias_text)
            if not alias_norm or (source, alias_norm) in seen:
                continue
            seen.add((source, alias_norm))
            out.append(AliasCandidate(alias_raw=alias_text, alias_norm=alias_norm, source=source, confidence=confidence))
    return out


def alias_token_sets(norm_aliases: set[str]) -> list[set[str]]:
    seen: set[tuple[str, ...]] = set()
    out: list[set[str]] = []
    for alias in norm_aliases:
        toks = tuple(sorted(set(meaningful_org_tokens(alias))))
        if not toks or toks in seen:
            continue
        seen.add(toks)
        out.append(set(toks))
    out.sort(key=lambda s: (-len(s), sorted(s)))
    return out


def names_match(raw_name: str, norm_aliases: set[str], alias_tokens: list[set[str]] | None = None) -> bool:
    norm_full = normalize_org_name(raw_name)
    norm_stripped = strip_corporate_suffixes(norm_full)
    if norm_full and (norm_full in norm_aliases or norm_stripped in norm_aliases):
        return True

    raw_tokens = set(meaningful_org_tokens(norm_full))
    if not raw_tokens:
        return False

    for toks in alias_tokens if alias_tokens is not None else alias_token_sets(norm_aliases):
        if len(toks) >= 2 and toks.issubset(raw_tokens):
            return True
        if len(toks) == 1:
            tok = next(iter(toks))
            if len(tok) >= 5 and tok in raw_tokens:
                return True
    return False
