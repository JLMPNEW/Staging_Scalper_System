#!/usr/bin/env python3
"""Backfill TRUE first-print ICSA (initial claims, weekly, SA) vintages ALFRED lacks.

ALFRED's ICSA archive begins with vintage 2009-05-28; every earlier weekly release
only survives inside that first edition as an already-revised value. This importer
recovers the genuine *advance* (first-print) seasonally adjusted initial-claims
figure straight from the DOL Employment & Training Administration weekly claims
press releases, which are the authoritative original publication.

Source (public, no key):
    Archive index (POST)  https://oui.doleta.gov/unemploy/archive.asp
                          form fields: report=press&year=YYYY
    Press releases        https://oui.doleta.gov/press/{year}/{MMDDYY}.asp

Each release headlines one reference week ending on the prior Saturday, e.g. the
release dated 2008-12-11 reports: "In the week ending Dec. 6, the advance figure
for seasonally adjusted initial claims was 573,000 ...". That 573,000 is the first
print; ALFRED's earliest surviving value for the same week (2008-12-06) is the
revised 552,000 carried in the 2009-05-28 edition.

Rows are written to ``macro_observation_raw`` with the exact registry identity of
the existing ALFRED ICSA rows (source_name=fred_alfred, source_series_id=ICSA),
observation_period = the Saturday week-ending date (ALFRED convention),
release_date = vintage_date = the true Thursday publication date, and the standard
dedupe key, so they merge with (never duplicate) ALFRED editions.

Default is a DRY RUN (parses + prints, writes nothing). Pass --apply to write.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from macro_raw_config import (
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    resolve_config_path,
    resolve_db_path,
    resolve_path,
    utc_now_iso,
)
from macro_registry import MetricSpec, load_metric_registry
from macro_storage import (
    _insert_artifacts,
    _upsert_observations,
    finish_run,
    init_db,
    start_run,
)
from macro_types import ObservationRecord, SourceArtifact

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://oui.doleta.gov/unemploy/archive.asp"
PRESS_BASE = "https://oui.doleta.gov"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

ICSA_REGISTRY_KEY = "us_initial_claims"

# Gap window keyed by week-ending (observation) date. ALFRED's first ICSA vintage
# is 2009-05-28 (week ending 2009-05-23); everything strictly earlier is
# unrecoverable from ALFRED.
DEFAULT_START_WEEK = "2008-03-01"
DEFAULT_END_WEEK = "2009-05-16"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_PRESS_LINK_RE = re.compile(r'/press/(\d{4})/(\d{6})\.asp', re.I)
_ADVANCE_RE = re.compile(
    r"week ending\s+([A-Za-z]+)\.?\s*(\d{1,2})\s*,?\s+the advance figure for "
    r"seasonally adjusted initial\s+claims was\s+([\d,]+)",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ClaimsFirstPrint:
    week_ending: date
    release_date: date
    advance_sa_claims: int
    source_url: str


def _http_get(url: str, *, data: bytes | None = None) -> bytes:
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (trusted gov host)
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            logger.warning("GET %s failed (attempt %d/3): %s", url, attempt + 1, exc)
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_exc}")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _notes_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()  # noqa: S324 (matches connector dedupe)


def _clean_text(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _release_date_from_filename(mmddyy: str) -> date:
    month = int(mmddyy[0:2])
    day = int(mmddyy[2:4])
    yy = int(mmddyy[4:6])
    year = 2000 + yy if yy < 90 else 1900 + yy
    return date(year, month, day)


def enumerate_release_urls(years: list[int]) -> list[tuple[date, str]]:
    found: dict[date, str] = {}
    for year in years:
        body = urllib.parse.urlencode({"report": "press", "year": str(year)}).encode("utf-8")
        html = _http_get(ARCHIVE_URL, data=body).decode("utf-8", errors="replace")
        for m in _PRESS_LINK_RE.finditer(html):
            link_year, mmddyy = m.group(1), m.group(2)
            try:
                release_date = _release_date_from_filename(mmddyy)
            except ValueError:
                continue
            url = f"{PRESS_BASE}/press/{link_year}/{mmddyy}.asp"
            found[release_date] = url
    return sorted(found.items())


def _resolve_week_ending(month: int, day: int, release_date: date) -> date:
    candidates: list[date] = []
    for year in (release_date.year - 1, release_date.year, release_date.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    # The week-ending Saturday sits ~5 days before the Thursday release; pick the
    # candidate year that lands closest to the release date.
    return min(candidates, key=lambda d: abs((release_date - d).days))


def parse_release(html: str, release_date: date, source_url: str) -> ClaimsFirstPrint | None:
    text = _clean_text(html)
    m = _ADVANCE_RE.search(text)
    if m is None:
        return None
    mon_token = m.group(1).lower()[:3]
    month = _MONTHS.get(mon_token)
    if month is None:
        return None
    day = int(m.group(2))
    value = int(m.group(3).replace(",", ""))
    week_ending = _resolve_week_ending(month, day, release_date)
    return ClaimsFirstPrint(
        week_ending=week_ending,
        release_date=release_date,
        advance_sa_claims=value,
        source_url=source_url,
    )


def _parse_iso(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def _spec_by_key(specs: list[MetricSpec], registry_key: str) -> MetricSpec:
    for spec in specs:
        if spec.registry_key == registry_key:
            return spec
    raise ValueError(f"Registry key {registry_key!r} not found in registry CSV.")


def _record(spec: MetricSpec, fp: ClaimsFirstPrint, retrieved: str, notes_hash: str | None) -> ObservationRecord:
    period = fp.week_ending.isoformat()
    release = fp.release_date.isoformat()
    return ObservationRecord(
        metric_key=spec.metric_key,
        source_name=spec.source_name,
        source_dataset=spec.source_dataset,
        source_series_id=spec.source_series_id,
        ref_area=spec.ref_area,
        frequency=spec.frequency,
        seasonal_adjustment=spec.seasonal_adjustment,
        units=spec.units,
        observation_period=period,
        observation_date=period,
        release_date=release,
        vintage_date=release,
        value=float(fp.advance_sa_claims),
        source_last_updated=None,
        retrieved_at=retrieved,
        revision_flag=0,
        notes_hash=notes_hash,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--db-path", type=Path, default=None, help="Optional SQLite DB path override.")
    parser.add_argument("--start", type=str, default=DEFAULT_START_WEEK, help="Start week-ending YYYY-MM-DD (inclusive).")
    parser.add_argument("--end", type=str, default=DEFAULT_END_WEEK, help="End week-ending YYYY-MM-DD (inclusive).")
    parser.add_argument("--sleep", type=float, default=0.4, help="Seconds to sleep between press-release fetches.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print only (default behavior).")
    parser.add_argument("--apply", action="store_true", help="Write parsed rows to macro_observation_raw.")
    return parser.parse_args()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    apply = bool(args.apply)
    config_path = resolve_config_path(args.config)
    _, cfg = load_macro_raw_config(config_path)
    db_path = resolve_db_path(cfg, config_path, override=args.db_path)
    registry_csv = resolve_path(config_path, str(cfg.get("registry_csv") or "MacroLayer/macro_metric_registry_full.csv"))
    if registry_csv is None:
        raise ValueError("Could not resolve registry_csv from config.")
    specs = load_metric_registry(registry_csv)
    icsa_spec = _spec_by_key(specs, ICSA_REGISTRY_KEY)

    start_week = _parse_iso(args.start)
    end_week = _parse_iso(args.end)

    # Releases run ~5 days after their week-ending Saturday, and an early-January
    # release reports a prior-December week, so widen the archive year span by one.
    years = list(range(start_week.year - 1, end_week.year + 2))
    release_urls = enumerate_release_urls(years)
    logger.info("Enumerated %d press releases across years %s.", len(release_urls), years)

    first_prints: list[ClaimsFirstPrint] = []
    payload_shas: dict[str, str] = {}
    parse_failures: list[str] = []
    for release_date, url in release_urls:
        # Cheap pre-filter: skip releases whose week cannot fall in the window.
        approx_week = release_date - timedelta(days=5)
        if approx_week < start_week - timedelta(days=10) or approx_week > end_week + timedelta(days=10):
            continue
        raw = _http_get(url)
        payload_shas[url] = _sha256(raw)
        fp = parse_release(raw.decode("utf-8", errors="replace"), release_date, url)
        if fp is None:
            parse_failures.append(url)
            logger.warning("Could not parse advance figure from %s", url)
            continue
        if start_week <= fp.week_ending <= end_week:
            first_prints.append(fp)
        if args.sleep > 0:
            time.sleep(args.sleep)

    first_prints.sort(key=lambda fp: fp.week_ending)
    if parse_failures:
        raise RuntimeError(
            f"{len(parse_failures)} press release(s) did not match the advance-figure pattern; "
            f"refusing to write a partial backfill: {parse_failures}"
        )

    retrieved = utc_now_iso()
    notes_hash = _notes_hash(icsa_spec.notes)
    records = [_record(icsa_spec, fp, retrieved, notes_hash) for fp in first_prints]

    logger.info(
        "ICSA first-print backfill: window %s..%s -> %d weekly first-print rows.",
        args.start,
        args.end,
        len(records),
    )
    for fp in first_prints:
        print(
            f"  week_ending={fp.week_ending.isoformat()} ({fp.week_ending.strftime('%a')})  "
            f"release={fp.release_date.isoformat()}  advance_SA_initial_claims={fp.advance_sa_claims:,}"
        )

    manifest: dict[str, Any] = {
        "script": "backfill_icsa_first_prints.py",
        "generated_at_utc": utc_now_iso(),
        "mode": "apply" if apply else "dry_run",
        "window": {"start_week": args.start, "end_week": args.end},
        "rows_parsed": len(records),
        "archive_url": ARCHIVE_URL,
        "archive_years": years,
        "release_source_urls": [fp.source_url for fp in first_prints],
        "payload_sha256": payload_shas,
        "db_path": str(db_path),
        "registry_keys": [ICSA_REGISTRY_KEY],
    }

    rows_written = 0
    if apply:
        rows_written = _apply(db_path=db_path, records=records, payload_shas=payload_shas)
        manifest["rows_written"] = rows_written
        logger.info("Applied %d ICSA first-print rows to %s", rows_written, db_path)
    else:
        logger.info("DRY RUN: no rows written. Re-run with --apply to persist.")

    manifest_path = _write_manifest(config_path, manifest)
    logger.info("Wrote manifest: %s", manifest_path)


def _apply(*, db_path: Path, records: list[ObservationRecord], payload_shas: dict[str, str]) -> int:
    conn = connect_sqlite(db_path)
    run_id = "icsa_firstprint_" + datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    try:
        init_db(conn)
        exists = conn.execute(
            "SELECT 1 FROM macro_metric_registry WHERE registry_key = ?", (ICSA_REGISTRY_KEY,)
        ).fetchone()
        if not exists:
            raise ValueError(
                f"Registry row {ICSA_REGISTRY_KEY!r} is missing; run the macro raw pipeline first."
            )
        start_run(
            conn,
            run_id=run_id,
            mode="backfill",
            as_of_date=date.today().isoformat(),
            source_filter="fred_alfred:ICSA",
            dry_run=False,
            task_count=1,
            source_count=1,
        )
        artifacts = [
            SourceArtifact(
                registry_key=ICSA_REGISTRY_KEY,
                source_name="dol_eta_weekly_claims",
                request_url=url,
                payload_hash=sha,
                http_status=200,
                fetched_at=utc_now_iso(),
                row_count=1,
                extra_json={"artifact_kind": "dol_weekly_claims_press_release"},
            )
            for url, sha in payload_shas.items()
        ]
        _insert_artifacts(conn, run_id=run_id, artifacts=artifacts)
        written = _upsert_observations(
            conn, run_id=run_id, registry_key=ICSA_REGISTRY_KEY, observations=records
        )
        finish_run(conn, run_id=run_id, status="completed", rows_written=written, error_count=0)
        conn.commit()
        return written
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_manifest(config_path: Path, manifest: dict[str, Any]) -> Path:
    out_dir = resolve_path(config_path, "MacroLayer/out/first_print_backfill")
    if out_dir is None:
        out_dir = Path("out/first_print_backfill")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "icsa_first_prints_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest_path


if __name__ == "__main__":
    main()
