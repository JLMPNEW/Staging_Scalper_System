"""SEC 13F publication-calendar math shared by the staleness gate and daily sweeps.

A13-7 fix (2026-08-05): closes the inverted-gate defect where script 09's
``max_13f_staleness_days`` clock could arm BEFORE the only source could deliver
newer data. SEC DERA publishes "Form 13F data sets" archives bucketed by
3-month FILING-date windows (mar-may, jun-aug, sep-nov, dec-feb) a few weeks
after each window closes, so a plain wall-clock age threshold can demand a
filing the source cannot yet have published: e.g. a ticker whose managers all
filed by 2026-05-08 breaches 120 days on 2026-09-06, while the jun-aug archive
carrying their next (Q2) filings can land as late as ~2026-09-17. The
publication-calendar-capped clock below arms the gate only once BOTH hold:

* the snapshot age exceeds ``max_staleness_days`` (the policy bound), AND
* the DERA archive that must carry the ticker's NEXT expected filing round has
  been publishable in the worst observed case for a few grace days, so the
  data the gate demands could actually exist in the database.

When archives publish on time and the daily sweep in script 13 ingests them
the day they appear, the cap is inert (fresh filings reset the age clock); it
only defers failure across the unavoidable source-side publication gap. A
ticker whose managers genuinely stop filing still fails closed one archive
cycle after its last filing round.

Pure date math; no I/O. Exercised by script 13's --selftest.
"""
from __future__ import annotations

from datetime import date, timedelta


# SEC rule 13f-1: reports are due within 45 days of calendar-quarter end
# (Feb 14 / May 15 / Aug 14 / Nov 14; weekend/holiday shifts of a day or two
# never move a deadline across a DERA window boundary, so they are irrelevant
# to the publication math below).
SEC_13F_FILING_DEADLINE_LAG_DAYS = 45
# Worst observed DERA lag between a filing window closing and its archive
# appearing on the index page (<= ~2.5 weeks; e.g. the jun-aug window closing
# Aug 31 published by ~Sep 17).
SEC_13F_MAX_PUBLICATION_LAG_DAYS = 17
# Operator slack between worst-case publication and the gate arming: the daily
# 13F sweep ingests an archive the night it appears, so a few days absorb a
# missed nightly run without materially weakening the fail-closed bound.
SEC_13F_PUBLICATION_GRACE_DAYS = 3

_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def _quarter_end_after(day: date) -> date:
    """Smallest calendar-quarter end strictly after ``day``."""
    candidates = [
        date(year, month, dom)
        for year in (day.year, day.year + 1)
        for month, dom in _QUARTER_ENDS
    ]
    return min(candidate for candidate in candidates if candidate > day)


def _deadline_after(day: date, *, inclusive: bool) -> date:
    """Smallest quarterly 13F filing deadline after (or on, if inclusive) ``day``."""
    candidates = [
        date(year, month, dom) + timedelta(days=SEC_13F_FILING_DEADLINE_LAG_DAYS)
        for year in (day.year - 1, day.year, day.year + 1)
        for month, dom in _QUARTER_ENDS
    ]
    if inclusive:
        return min(candidate for candidate in candidates if candidate >= day)
    return min(candidate for candidate in candidates if candidate > day)


def dera_window_end_containing(day: date) -> date:
    """End of the DERA filing-date window (mar-may, jun-aug, sep-nov, dec-feb)
    that contains ``day``. Mirrors script 13's latest_completed_sec_13f_window."""
    if day.month >= 12:
        start = date(day.year, 12, 1)
    elif day.month >= 9:
        start = date(day.year, 9, 1)
    elif day.month >= 6:
        start = date(day.year, 6, 1)
    elif day.month >= 3:
        start = date(day.year, 3, 1)
    else:
        start = date(day.year - 1, 12, 1)
    if start.month == 12:
        return date(start.year + 1, 3, 1) - timedelta(days=1)
    return date(start.year, start.month + 3, 1) - timedelta(days=1)


def next_13f_publishable_date(
    *,
    last_filing: date,
    period_of_report: date | None = None,
    publication_lag_days: int = SEC_13F_MAX_PUBLICATION_LAG_DAYS,
) -> date:
    """Worst-case date the DERA archive carrying the NEXT expected 13F filing
    round after this snapshot becomes downloadable.

    With ``period_of_report`` (preferred, exact): the next round reports the
    following quarter end, due 45 days later, and lands in the DERA window
    containing that deadline. Without it, the round is inferred from the filing
    date: the deadline covering ``last_filing`` identifies the round just
    filed, and the next quarterly deadline identifies the next round. (For a
    late amendment filed after its own deadline the inference is conservative
    by one round — it can only defer the gate, never demand unpublishable
    data.)
    """
    if period_of_report is not None:
        next_deadline = _quarter_end_after(period_of_report) + timedelta(
            days=SEC_13F_FILING_DEADLINE_LAG_DAYS
        )
    else:
        covering_deadline = _deadline_after(last_filing, inclusive=True)
        next_deadline = _deadline_after(covering_deadline, inclusive=False)
    return dera_window_end_containing(next_deadline) + timedelta(
        days=max(0, publication_lag_days)
    )


def sec_13f_staleness_arming_date(
    *,
    last_filing: date,
    period_of_report: date | None = None,
    max_staleness_days: int,
    publication_lag_days: int = SEC_13F_MAX_PUBLICATION_LAG_DAYS,
    grace_days: int = SEC_13F_PUBLICATION_GRACE_DAYS,
) -> date | None:
    """First as-of date on which this 13F snapshot counts as stale.

    ``None`` when ``max_staleness_days <= 0`` (staleness gating disabled).
    Monotonically non-decreasing in ``last_filing``, so the earliest family
    arming date is the arming date of the earliest family last-filing.
    """
    if max_staleness_days <= 0:
        return None
    age_armed = last_filing + timedelta(days=max_staleness_days + 1)
    publication_armed = next_13f_publishable_date(
        last_filing=last_filing,
        period_of_report=period_of_report,
        publication_lag_days=publication_lag_days,
    ) + timedelta(days=max(0, grace_days))
    return max(age_armed, publication_armed)


def sec_13f_snapshot_is_stale(
    *,
    asof: date,
    last_filing: date,
    period_of_report: date | None = None,
    max_staleness_days: int,
    publication_lag_days: int = SEC_13F_MAX_PUBLICATION_LAG_DAYS,
    grace_days: int = SEC_13F_PUBLICATION_GRACE_DAYS,
) -> bool:
    """Publication-calendar-capped staleness verdict for one 13F snapshot.

    True only when the snapshot age exceeds ``max_staleness_days`` AND the
    archive that must carry the next filing round has (worst case) been
    publishable for ``grace_days`` — the gate never demands data its source
    cannot yet publish.
    """
    armed_on = sec_13f_staleness_arming_date(
        last_filing=last_filing,
        period_of_report=period_of_report,
        max_staleness_days=max_staleness_days,
        publication_lag_days=publication_lag_days,
        grace_days=grace_days,
    )
    return armed_on is not None and asof >= armed_on
