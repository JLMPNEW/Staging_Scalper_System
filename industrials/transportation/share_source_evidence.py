from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence


RELEVANT_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "8-K",
        "8-K/A",
        "10-12B",
        "10-12B/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "F-1",
        "F-1/A",
        "S-1",
        "S-1/A",
        "424B3",
        "424B4",
    }
)
ANNUAL_AND_REGISTRATION_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "8-K",
        "8-K/A",
        "10-12B",
        "10-12B/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "F-1",
        "F-1/A",
        "S-1",
        "S-1/A",
        "424B3",
        "424B4",
    }
)
NUMBER_WORDS = {
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "twenty": 20.0,
    "fifty": 50.0,
    "one hundred": 100.0,
    "five hundred": 500.0,
}
RATIO_PATTERNS = (
    re.compile(
        r"(?is)(?:american\s+depositary\s+shares?|\bADSs?\b)"
        r".{0,220}?each\s+represent(?:s|ing)?\s+"
        r"(?P<number>(?:one\s+hundred|five\s+hundred|one|two|three|four|five|six|seven|eight|nine|ten|twenty|fifty|\d[\d,]*(?:\.\d+)?)"
        r"(?:\s*\(\s*\d[\d,]*(?:\.\d+)?\s*\))?)\s+"
        r"(?:shares?\s+of\s+)?"
        r"(?P<class>(?:class\s+[a-z]\s+)?(?:ordinary\s+participation|ordinary|common|series|participation|cpo))"
    ),
    re.compile(
        r"(?is)each\s+(?:american\s+depositary\s+share|\bADS\b)"
        r".{0,80}?represent(?:s|ing)?\s+"
        r"(?P<number>(?:one\s+hundred|five\s+hundred|one|two|three|four|five|six|seven|eight|nine|ten|twenty|fifty|\d[\d,]*(?:\.\d+)?)"
        r"(?:\s*\(\s*\d[\d,]*(?:\.\d+)?\s*\))?)\s+"
        r"(?:shares?\s+of\s+)?"
        r"(?P<class>(?:class\s+[a-z]\s+)?(?:ordinary\s+participation|ordinary|common|series|participation|cpo))"
    ),
)
SHARE_COUNT_PATTERNS = (
    re.compile(
        r"(?is)as\s+of\s+(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})"
        r".{0,100}?(?:there\s+were|the\s+registrant\s+had)\s+"
        r"(?P<count>\d[\d,]*)\s+(?:shares|ordinary\s+shares|common\s+shares|units)"
        r".{0,100}?outstanding"
    ),
    re.compile(
        r"(?is)(?P<count>\d[\d,]*)\s+(?:shares|ordinary\s+shares|common\s+shares|units)"
        r".{0,100}?outstanding\s+as\s+of\s+"
        r"(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})"
    ),
)


@dataclass(frozen=True)
class Filing:
    accession_number: str
    filing_date: date
    form: str
    primary_document: str
    report_date: date | None = None

    @property
    def accession_compact(self) -> str:
        return self.accession_number.replace("-", "")


def html_to_text(raw: str) -> str:
    without_code = re.sub(
        r"(?is)<script.*?</script>|<style.*?</style>",
        " ",
        raw,
    )
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_code)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def load_submissions_filings(
    path: Path,
    *,
    asof: date,
) -> list[Filing]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    recent = payload.get("filings", {}).get("recent", {})
    if not isinstance(recent, Mapping):
        return []
    accessions = recent.get("accessionNumber")
    if not isinstance(accessions, Sequence) or isinstance(accessions, (str, bytes)):
        return []
    output: list[Filing] = []
    for index, accession in enumerate(accessions):
        try:
            filed = date.fromisoformat(str(recent["filingDate"][index])[:10])
            form = str(recent["form"][index] or "").strip().upper()
            primary = str(recent["primaryDocument"][index] or "").strip()
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        report_values = recent.get("reportDate") or []
        report_text = str(report_values[index] or "")[:10] if index < len(report_values) else ""
        try:
            report_date = date.fromisoformat(report_text) if report_text else None
        except ValueError:
            report_date = None
        if filed <= asof and form in RELEVANT_FORMS:
            output.append(
                Filing(
                    accession_number=str(accession),
                    filing_date=filed,
                    form=form,
                    primary_document=primary,
                    report_date=report_date,
                )
            )
    return sorted(output, key=lambda item: (item.filing_date, item.accession_number), reverse=True)


def locate_primary_document(
    archive_root: Path,
    *,
    filing: Filing,
) -> Path | None:
    folder = archive_root / filing.accession_compact
    primary = folder / filing.primary_document
    if filing.primary_document and primary.is_file():
        return primary
    if not folder.is_dir():
        return None
    candidates = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in {".htm", ".html", ".txt"}
    ]
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def locate_supporting_documents(
    archive_root: Path,
    *,
    filing: Filing,
    primary: Path | None,
    limit: int = 2,
) -> list[Path]:
    folder = archive_root / filing.accession_compact
    if not folder.is_dir() or limit <= 0:
        return []
    candidates = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path != primary
        and path.suffix.lower() in {".htm", ".html", ".txt"}
    ]
    return sorted(candidates, key=lambda path: path.stat().st_size, reverse=True)[:limit]


def _number(raw: str) -> float | None:
    text = re.sub(r"\s+", " ", raw.strip().lower())
    parenthesized = re.search(r"\(\s*(\d[\d,]*(?:\.\d+)?)\s*\)", text)
    if parenthesized:
        return float(parenthesized.group(1).replace(",", ""))
    if text in NUMBER_WORDS:
        return NUMBER_WORDS[text]
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _snippet(text: str, start: int, end: int, *, radius: int = 220) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)].strip()


def extract_listing_evidence(
    text: str,
    *,
    ticker: str,
) -> dict[str, object]:
    cover = text[:50000]
    ratio_hits: list[tuple[float, str]] = []
    for pattern in RATIO_PATTERNS:
        for match in pattern.finditer(cover):
            ratio = _number(match.group("number"))
            if ratio is not None and ratio > 0.0:
                ratio_hits.append((ratio, _snippet(cover, match.start(), match.end())))
    ratios = sorted({ratio for ratio, _ in ratio_hits})
    direct_pattern = re.compile(
        rf"(?is)(?P<class>(?:class\s+[a-z]\s+)?(?:ordinary|common)\s+"
        rf"(?:shares|stock|units)|common\s+units).{{0,220}}?\b{re.escape(ticker)}\b"
        rf".{{0,160}}?(?:new\s+york\s+stock\s+exchange|nasdaq\s+stock\s+market|nasdaq)"
    )
    direct_match = direct_pattern.search(cover)
    depositary_near_cover = bool(
        re.search(r"(?is)american\s+depositary\s+(?:shares?|receipts?)", cover[:15000])
    )
    if len(ratios) == 1:
        ratio = ratios[0]
        evidence = next(snippet for value, snippet in ratio_hits if value == ratio)
        return {
            "candidate_instrument": "ADR_ADS",
            "candidate_ratio": ratio,
            "evidence_status": "EXACT_ADR_RATIO",
            "evidence_text": evidence,
        }
    if len(ratios) > 1:
        return {
            "candidate_instrument": "",
            "candidate_ratio": "",
            "evidence_status": "CONFLICTING_RATIOS_IN_FILING",
            "evidence_text": " | ".join(snippet for _, snippet in ratio_hits[:3]),
        }
    if direct_match and not depositary_near_cover:
        return {
            "candidate_instrument": "DIRECT_SHARE",
            "candidate_ratio": 1.0,
            "evidence_status": "EXACT_DIRECT_LISTED_CLASS",
            "evidence_text": _snippet(cover, direct_match.start(), direct_match.end()),
        }
    return {
        "candidate_instrument": "",
        "candidate_ratio": "",
        "evidence_status": "NO_EXACT_LISTING_EVIDENCE",
        "evidence_text": "",
    }


def extract_cover_share_counts(text: str) -> list[dict[str, object]]:
    cover = text[:75000]
    output: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for pattern in SHARE_COUNT_PATTERNS:
        for match in pattern.finditer(cover):
            count = int(match.group("count").replace(",", ""))
            key = (match.group("date"), count)
            if count <= 0 or key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "share_count_date_text": match.group("date"),
                    "share_count": count,
                    "share_count_evidence_text": _snippet(
                        cover,
                        match.start(),
                        match.end(),
                    ),
                }
            )
    return output


def sec_archive_url(*, cik: object, filing: Filing) -> str:
    digits = str(int(str(cik)))
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{digits}/{filing.accession_compact}/{filing.primary_document}"
    )
