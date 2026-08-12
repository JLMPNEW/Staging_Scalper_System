from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.db import connect
from consumer_defensive.core.stage4 import (
    ALLOWED_FACT_FORMS,
    DOCUMENT_FORMS,
    FINANCIAL_FORM_FAMILIES,
    PROFILE_CONDITIONAL_XBRL_FORMS,
    PROFILE_FINANCIAL_FORMS,
    SEC_INGESTION_CONFIG_VERSION,
    _canonical_financial_form,
    _reporting_profile_anchor,
    _sec_ingestion_config_sha256,
    _validated_issuer_filing_projection,
    bootstrap_stage4,
    sync_sec_fundamentals,
)
from consumer_defensive.core.universe import load_current_universe, load_policy


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"
POLICY = ROOT / "consumer_defensive" / "data" / "consumer_defensive_universe_policy.yaml"


def _prepared_db(tmp_path: Path):
    source_bundle = load_config(CONFIG)
    payload = copy.deepcopy(source_bundle.payload)
    payload["sec_fundamentals"]["cache_dir"] = str(tmp_path / "sec_cache")
    bundle = ConfigBundle(source_bundle.path, source_bundle.base_dir, payload)
    conn = connect(tmp_path / "stage4_reporting_profiles.sqlite")
    bootstrap_stage4(conn, bundle)
    load_current_universe(conn, load_policy(POLICY))
    return bundle, conn


def _filing(
    accession: str,
    form: str,
    accepted_at: str,
    *,
    primary_document: str,
) -> dict[str, str]:
    return {
        "accessionNumber": accession,
        "filingDate": accepted_at[:10],
        "acceptanceDateTime": accepted_at,
        "reportDate": accepted_at[:4] + "-01-01",
        "form": form,
        "primaryDocument": primary_document,
    }


def _submissions(filings: list[dict[str, str]]) -> dict[str, object]:
    keys = (
        "accessionNumber",
        "filingDate",
        "acceptanceDateTime",
        "reportDate",
        "form",
        "primaryDocument",
    )
    return {
        "cik": "21344",
        "filings": {
            "recent": {key: [filing[key] for filing in filings] for key in keys},
            "files": [],
        }
    }


def _companyfacts(
    accession: str,
    form: str,
    filed_date: str,
) -> dict[str, object]:
    return {
        "cik": "21344",
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "USD": [
                            {
                                "start": filed_date[:4] + "-01-01",
                                "end": filed_date[:4] + "-12-31",
                                "val": 100.0,
                                "accn": accession,
                                "form": form,
                                "filed": filed_date,
                            }
                        ]
                    }
                }
            }
        }
    }


def _sync_profile(
    conn,
    bundle: ConfigBundle,
    *,
    filings: list[dict[str, str]],
    companyfacts: dict[str, object],
    documents: dict[str, bytes] | None = None,
    as_of: str = "2026-01-01",
) -> tuple[object, ...]:
    submissions = _submissions(filings)
    documents = documents or {}

    def fetch(url: str) -> bytes:
        if "companyfacts" in url:
            return json.dumps(companyfacts).encode()
        if "submissions" in url:
            return json.dumps(submissions).encode()
        if "Archives" in url:
            primary_document = url.rsplit("/", 1)[-1]
            return documents.get(primary_document, b"<html><body>ordinary filing</body></html>")
        raise AssertionError(url)

    result = sync_sec_fundamentals(
        conn,
        bundle,
        tickers=["KO"],
        as_of=as_of,
        force_refresh=True,
        fetch=fetch,
    )
    assert result["failures"] == []
    profile = conn.execute(
        """
        SELECT primary_annual_form, latest_filing_accepted_at,
               latest_companyfacts_accepted_at, companyfacts_lag_days,
               inline_xbrl_fallback_required, coverage_status
        FROM dim_issuer_reporting_profile WHERE ticker='KO'
        """
    ).fetchone()
    assert profile is not None
    return tuple(profile)


def test_profile_anchor_forms_are_coherent_with_fact_and_document_ingestion() -> None:
    assert SEC_INGESTION_CONFIG_VERSION == 8
    recognized = PROFILE_FINANCIAL_FORMS | PROFILE_CONDITIONAL_XBRL_FORMS
    assert recognized <= ALLOWED_FACT_FORMS
    assert recognized <= DOCUMENT_FORMS

    for form in sorted(recognized):
        accession = "0000000000-25-000001"
        latest, annual = _reporting_profile_anchor(
            [
                _filing(
                    accession,
                    form.lower(),
                    "2025-02-20T16:30:00Z",
                    primary_document="financial.htm",
                )
            ],
            cutoff="2025-12-31T23:59:59Z",
            companyfacts_xbrl_accessions={accession},
            inline_xbrl_accessions=set(),
        )
        assert latest == "2025-02-20T16:30:00Z"
        expected_annual = _canonical_financial_form(form)
        assert annual == (expected_annual if expected_annual in {"10-K", "20-F", "40-F"} else "")


def test_financial_form_families_are_bound_into_immutable_config_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _sec_ingestion_config_sha256({})
    monkeypatch.setitem(FINANCIAL_FORM_FAMILIES, "10-QT", "10-K")

    assert _sec_ingestion_config_sha256({}) != baseline


@pytest.mark.parametrize(
    ("submissions_form", "companyfacts_form"),
    (
        ("10-Q/A", "10-Q"),
        ("10-K/A", "10-K"),
        ("10-QT", "10-Q"),
        ("10-KT", "10-K"),
        ("10-QT/A", "10-Q"),
        ("10-KT/A", "10-K"),
        ("20-F/A", "20-F"),
        ("40-F/A", "40-F"),
        ("6-K/A", "6-K"),
    ),
)
def test_companyfacts_base_form_matches_only_recognized_submission_variants(
    submissions_form: str, companyfacts_form: str,
) -> None:
    accession = "0000000000-25-000001"
    filings = [
        _filing(
            accession,
            submissions_form,
            "2025-02-20T16:30:00Z",
            primary_document="financial.htm",
        )
    ]

    assert _validated_issuer_filing_projection(
        filings, _companyfacts(accession, companyfacts_form, "2025-02-20"),
    ) == filings


def test_companyfacts_form_family_does_not_mask_unrelated_mismatch() -> None:
    accession = "0000000000-25-000001"
    filings = [
        _filing(
            accession,
            "10-K/A",
            "2025-02-20T16:30:00Z",
            primary_document="financial.htm",
        )
    ]

    with pytest.raises(ValueError, match="Companyfacts='10-Q'.*submissions='10-K/A'"):
        _validated_issuer_filing_projection(
            filings, _companyfacts(accession, "10-Q", "2025-02-20"),
        )


def test_transitional_annual_form_sync_uses_canonical_profile(tmp_path: Path) -> None:
    accession = "0000000000-25-000001"
    accepted = "2025-02-20T16:30:00Z"
    bundle, conn = _prepared_db(tmp_path)
    try:
        profile = _sync_profile(
            conn,
            bundle,
            filings=[
                _filing(
                    accession,
                    "10-KT",
                    accepted,
                    primary_document="transition.htm",
                )
            ],
            companyfacts=_companyfacts(accession, "10-K", "2025-02-20"),
        )
    finally:
        conn.close()

    assert profile == ("10-K", accepted, accepted, 0, 0, "covered")


def test_later_ordinary_6k_and_ownership_filing_do_not_move_profile_anchor(
    tmp_path: Path,
) -> None:
    annual_accession = "0000000000-24-000001"
    filings = [
        _filing(
            annual_accession,
            "20-F",
            "2024-03-01T12:00:00Z",
            primary_document="annual-2024.htm",
        ),
        _filing(
            "0000000000-25-000002",
            "6-K",
            "2025-01-15T12:00:00Z",
            primary_document="ordinary-6k.htm",
        ),
        _filing(
            "0000000000-25-000003",
            "4",
            "2025-02-15T12:00:00Z",
            primary_document="ownership.xml",
        ),
    ]
    bundle, conn = _prepared_db(tmp_path)
    try:
        profile = _sync_profile(
            conn,
            bundle,
            filings=filings,
            companyfacts=_companyfacts(annual_accession, "20-F", "2024-03-01"),
        )
    finally:
        conn.close()

    assert profile[:5] == (
        "20-F",
        "2024-03-01T12:00:00Z",
        "2024-03-01T12:00:00Z",
        0,
        0,
    )


def test_newer_20f_moves_anchor_and_requires_inline_fallback(tmp_path: Path) -> None:
    old_accession = "0000000000-23-000001"
    filings = [
        _filing(
            old_accession,
            "20-F",
            "2023-03-01T12:00:00Z",
            primary_document="annual-2023.htm",
        ),
        _filing(
            "0000000000-25-000002",
            "20-F",
            "2025-03-15T12:00:00Z",
            primary_document="annual-2025.htm",
        ),
    ]
    bundle, conn = _prepared_db(tmp_path)
    try:
        profile = _sync_profile(
            conn,
            bundle,
            filings=filings,
            companyfacts=_companyfacts(old_accession, "20-F", "2023-03-01"),
        )
    finally:
        conn.close()

    assert profile[0] == "20-F"
    assert profile[1] == "2025-03-15T12:00:00Z"
    assert profile[2] == "2023-03-01T12:00:00Z"
    assert profile[3] > 120
    assert profile[4:] == (1, "inline_fallback_required")


def test_verified_inline_xbrl_6k_moves_profile_anchor(tmp_path: Path) -> None:
    annual_accession = "0000000000-23-000001"
    inline_primary = "financial-6k.htm"
    filings = [
        _filing(
            annual_accession,
            "20-F",
            "2023-03-01T12:00:00Z",
            primary_document="annual-2023.htm",
        ),
        _filing(
            "0000000000-25-000002",
            "6-K",
            "2025-04-01T12:00:00Z",
            primary_document=inline_primary,
        ),
    ]
    inline_document = (
        b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        b'<ix:nonFraction name="ifrs-full:Revenue">100</ix:nonFraction></html>'
    )
    bundle, conn = _prepared_db(tmp_path)
    try:
        profile = _sync_profile(
            conn,
            bundle,
            filings=filings,
            companyfacts=_companyfacts(annual_accession, "20-F", "2023-03-01"),
            documents={inline_primary: inline_document},
        )
    finally:
        conn.close()

    assert profile[0] == "20-F"
    assert profile[1] == "2025-04-01T12:00:00Z"
    assert profile[2] == "2023-03-01T12:00:00Z"
    assert profile[3] > 120
    assert profile[4:] == (1, "inline_fallback_required")


def test_same_companyfacts_rerun_corrects_legacy_false_positive_without_backdating(
    tmp_path: Path,
) -> None:
    annual_accession = "0000000000-24-000001"
    annual_accepted = "2024-03-01T12:00:00Z"
    ordinary_accepted = "2025-01-15T12:00:00Z"
    filings = [
        _filing(
            annual_accession,
            "20-F",
            annual_accepted,
            primary_document="annual-2024.htm",
        ),
        _filing(
            "0000000000-25-000002",
            "6-K",
            ordinary_accepted,
            primary_document="ordinary-6k.htm",
        ),
    ]
    facts = _companyfacts(annual_accession, "20-F", "2024-03-01")
    bundle, conn = _prepared_db(tmp_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO dim_issuer_reporting_profile(
                    ticker,cik,primary_annual_form,foreign_issuer_flag,us_gaap_flag,
                    ifrs_flag,latest_filing_accepted_at,latest_companyfacts_accepted_at,
                    companyfacts_lag_days,inline_xbrl_fallback_required,coverage_status,
                    review_reason,updated_at
                ) VALUES(
                    'KO','21344','20-F',1,0,1,?,?,320,1,
                    'inline_fallback_required','legacy_any_form_heuristic','2025-01-16T00:00:00Z'
                )
                """,
                (ordinary_accepted, annual_accepted),
            )

        corrected = _sync_profile(
            conn,
            bundle,
            filings=filings,
            companyfacts=facts,
            as_of="2025-12-31",
        )
        assert corrected[:5] == (
            "20-F",
            annual_accepted,
            annual_accepted,
            0,
            0,
        )

        with pytest.raises(RuntimeError, match="reverse replay rejected"):
            _sync_profile(
                conn,
                bundle,
                filings=filings,
                companyfacts=facts,
                as_of="2023-12-31",
            )
        assert tuple(conn.execute(
            """SELECT primary_annual_form,latest_filing_accepted_at,
                      latest_companyfacts_accepted_at,companyfacts_lag_days,
                      inline_xbrl_fallback_required,coverage_status
               FROM dim_issuer_reporting_profile WHERE ticker='KO'"""
        ).fetchone()) == corrected
    finally:
        conn.close()
