from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

PORTFOLIO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_STORE_DIR = PORTFOLIO_ROOT / "output" / "snapshot_store"
RUNS_DIR = PORTFOLIO_ROOT / "output" / "runs"
SURVIVORSHIP_PANEL_DIR = PORTFOLIO_ROOT / "output" / "survivorship_panel"
MAX_STAGE2_PRICE_STALE_DAYS = 7


def _is_iso_date_dir(path: Path) -> bool:
    try:
        pd.Timestamp(path.name)
    except (TypeError, ValueError):
        return False
    return len(path.name) == 10 and path.name[4] == "-" and path.name[7] == "-"


def _first_present(frame: pd.DataFrame, names: list[str], default: Any = "") -> pd.Series:
    for name in names:
        if name in frame.columns:
            return frame[name]
    return pd.Series(default, index=frame.index)


def _clean_text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def _score_frame_from_csv(csv_path: Path, *, as_of_date: str, source: str, priority: int) -> pd.DataFrame:
    frame = pd.read_csv(csv_path)
    if frame.empty:
        return pd.DataFrame()
    as_of = _first_present(frame, ["as_of_date", "Date"], as_of_date)
    ticker = _first_present(frame, ["ticker", "Ticker"])
    score = _first_present(frame, ["final_score", "FinalScore", "Score"])
    sector = _clean_text(_first_present(frame, ["sector"]))
    industry = _clean_text(_first_present(frame, ["industry"]))
    aggregate = _clean_text(_first_present(frame, ["industry_aggregate", "industry_aggregate_name"]))
    aggregate = aggregate.mask(aggregate.eq(""), industry).mask(lambda s: s.eq(""), sector)
    industry = industry.mask(industry.eq(""), aggregate).mask(lambda s: s.eq(""), sector)
    return pd.DataFrame(
        {
            "Date": as_of,
            "Ticker": ticker,
            "sector": sector,
            "industry": industry,
            "industry_aggregate": aggregate,
            "Score": score,
            "Rating": _first_present(frame, ["rating", "Rating"], "Hold"),
            "Company": _first_present(frame, ["company_name", "company", "Company"], ""),
            "ScoreApproach": _first_present(frame, ["score_version", "score_model_version"], source),
            "RunId": as_of_date,
            "BaseOptimizerEligible": _first_present(
                frame,
                ["investable_eligible", "portfolio_candidate_gate", "BaseOptimizerEligible"],
                1,
            ),
            "EarningsBlocked_7D": _first_present(frame, ["earnings_blocked_7d", "EarningsBlocked_7D"], 0),
            "source_pipeline": _first_present(frame, ["source_pipeline"], ""),
            "SnapshotSource": source,
            "SourcePriority": priority,
        }
    )


def _date_in_bounds(path: Path, *, start_date: Any = None, end_date: Any = None) -> bool:
    value = pd.Timestamp(path.name).normalize()
    if start_date is not None and value < pd.Timestamp(start_date).normalize():
        return False
    if end_date is not None and value > pd.Timestamp(end_date).normalize():
        return False
    return True


def load_staging_score_panel(
    snapshot_store_dir: Path | None = None,
    runs_dir: Path | None = None,
    *,
    start_date: Any = None,
    end_date: Any = None,
) -> pd.DataFrame:
    """Load PIT Stage-1 score snapshots in the legacy MacroLayer score-panel shape.

    This is the Staging-owned replacement for the legacy BackTest score-panel loader.
    It reads only sealed portfolio-layer snapshot CSVs and never sector DBs or PROD.
    """
    root = (snapshot_store_dir or SNAPSHOT_STORE_DIR).resolve()
    run_root = (runs_dir or RUNS_DIR).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Staging snapshot store does not exist: {root}")

    frames: list[pd.DataFrame] = []
    for snap_dir in sorted(
        p for p in root.iterdir()
        if p.is_dir() and _is_iso_date_dir(p) and _date_in_bounds(p, start_date=start_date, end_date=end_date)
    ):
        csv_path = snap_dir / "stocks_scores.csv"
        if not csv_path.exists():
            continue
        out = _score_frame_from_csv(
            csv_path,
            as_of_date=snap_dir.name,
            source="staging_snapshot_store",
            priority=2,
        )
        frames.append(out)
    if run_root.exists():
        for run_dir in sorted(
            p for p in run_root.iterdir()
            if p.is_dir() and _is_iso_date_dir(p) and _date_in_bounds(p, start_date=start_date, end_date=end_date)
        ):
            csv_path = run_dir / "stocks_scores.csv"
            manifest_path = run_dir / "manifest.json"
            if not csv_path.exists() or not manifest_path.exists():
                continue
            out = _score_frame_from_csv(
                csv_path,
                as_of_date=run_dir.name,
                source="staging_run_output",
                priority=3,
            )
            frames.append(out)

    if not frames:
        raise ValueError(f"No stocks_scores.csv snapshots were found under {root}")
    panel = pd.concat(frames, ignore_index=True)
    panel["Date"] = pd.to_datetime(panel["Date"], errors="coerce").dt.normalize()
    panel["Ticker"] = panel["Ticker"].astype(str).str.upper().str.strip()
    panel["Score"] = pd.to_numeric(panel["Score"], errors="coerce")
    for col in ("sector", "industry", "industry_aggregate", "Rating", "Company", "ScoreApproach", "RunId"):
        panel[col] = panel[col].fillna("").astype(str).str.strip()
    panel = panel.loc[
        panel["Date"].notna()
        & panel["Score"].notna()
        & panel["Ticker"].ne("")
        & panel["sector"].ne("")
        & panel["industry"].ne("")
        & panel["industry_aggregate"].ne("")
    ].copy()
    if panel.empty:
        raise ValueError("Staging snapshot score panel is empty after required-field filtering.")
    panel = (
        panel.sort_values(["Date", "Ticker", "SourcePriority"])
        .drop_duplicates(subset=["Date", "Ticker"], keep="last")
        .reset_index(drop=True)
    )
    LOGGER.info(
        "Loaded Staging score panel: rows=%d dates=%d tickers=%d source=%s",
        len(panel),
        panel["Date"].nunique(),
        panel["Ticker"].nunique(),
        root,
    )
    return panel.sort_values(["Date", "Ticker"]).reset_index(drop=True)


def latest_accepted_survivorship_panel(panel_root: Path | None = None) -> Path:
    root = (panel_root or SURVIVORSHIP_PANEL_DIR).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Staging survivorship panel root does not exist: {root}")
    builds = sorted(p for p in root.iterdir() if p.is_dir() and (p / "survivorship_manifest.json").exists())
    if not builds:
        raise FileNotFoundError(f"No survivorship panel builds found under {root}")
    return builds[-1]


def _read_price_csv(price_path: Path) -> pd.DataFrame:
    prices = pd.read_csv(price_path, index_col=0)
    prices.index = pd.to_datetime(prices.index, errors="coerce").normalize()
    prices = prices.loc[prices.index.notna()].sort_index()
    prices.columns = [str(c).strip().upper() for c in prices.columns]
    return prices


def latest_accepted_stage2_risk_panel(*, runs_dir: Path | None = None, as_of_date: Any = None) -> Path:
    root = (runs_dir or RUNS_DIR).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Portfolio run root does not exist: {root}")
    cutoff = pd.Timestamp(as_of_date).normalize() if as_of_date is not None else None
    candidates = []
    for run_dir in sorted((p for p in root.iterdir() if p.is_dir() and _is_iso_date_dir(p)), reverse=True):
        run_date = pd.Timestamp(run_dir.name).normalize()
        if cutoff is not None and run_date > cutoff:
            continue
        risk_dir = run_dir / "risk"
        price_path = risk_dir / "prices_adjclose.csv"
        manifest_path = risk_dir / "risk_manifest.json"
        if not price_path.exists() or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("acceptance") != "PASS":
            continue
        candidates.append(risk_dir)
    if not candidates:
        raise FileNotFoundError(f"No accepted Stage 2 risk price panels found under {root}")
    return candidates[0]


def _overlay_prices(history: pd.DataFrame, live: pd.DataFrame) -> pd.DataFrame:
    all_index = history.index.union(live.index).sort_values()
    all_columns = pd.Index(history.columns).union(pd.Index(live.columns))
    combined = history.reindex(index=all_index, columns=all_columns)
    live_aligned = live.reindex(index=all_index, columns=all_columns)
    return live_aligned.combine_first(combined).sort_index()


def _enforce_live_tail(
    combined: pd.DataFrame,
    live: pd.DataFrame,
    *,
    wanted: list[str],
    freshness_date: pd.Timestamp,
    max_stage2_stale_days: int,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Drop future-dated rows and flag out-of-tolerance stale tails WITHOUT discarding valid history.

    A fit-universe ticker keeps its full in-tolerance history: a survivorship-only name whose most
    recent bar is within ``max_stage2_stale_days`` of ``freshness_date`` is preserved intact (its
    few-day-behind tail is acceptable and is gated again, per weekly date, by
    ``staleness_gated_weekly`` downstream). Only a name whose most recent observation is staler than
    the tolerance has its current-window rows blanked -- and even then all deep history before the
    stale cutoff is retained so the trailing-window fit keeps its members. This replaces the earlier
    ``live_start`` blanking, which wiped ~2 years of valid survivorship prices for every name absent
    from the (much smaller) live risk panel and gutted the sector baskets.
    """
    if live.empty:
        return combined, [], []
    freshness = pd.Timestamp(freshness_date).normalize()
    stale_cutoff = freshness - pd.Timedelta(days=int(max_stage2_stale_days))
    wanted_set = {str(t).strip().upper() for t in wanted if str(t).strip()}
    missing_live = sorted(wanted_set - set(live.columns))

    # never present future bars (> freshness) as history
    future_mask = combined.index > freshness
    if bool(future_mask.any()):
        combined.loc[future_mask, :] = float("nan")

    stale_blanked: list[str] = []
    for ticker in sorted(wanted_set):
        if ticker not in combined.columns:
            continue
        last_real = combined[ticker].last_valid_index()
        if last_real is None:
            continue
        if pd.Timestamp(last_real).normalize() < stale_cutoff:
            # out of tolerance: forbid any current-window bar so a downstream forward-fill cannot
            # manufacture a "current" price from this stale tail; deep history before the cutoff is kept.
            combined.loc[combined.index >= stale_cutoff, ticker] = float("nan")
            stale_blanked.append(ticker)
    return combined, missing_live, stale_blanked


def staleness_gated_weekly(
    prices: pd.DataFrame,
    weekly_dates: Any,
    *,
    max_stale_days: int = MAX_STAGE2_PRICE_STALE_DAYS,
) -> pd.DataFrame:
    """Project daily prices onto ``weekly_dates``, forward-filling gaps but NULLING any carried
    value whose most-recent real observation is older than ``max_stale_days`` at that weekly date.

    Makes staleness protection explicit and independent of any forward-fill row limit: an in-tolerance
    gap (holiday / thin trading) is still bridged, but a stale tail is never presented as a current
    weekly price. Replaces the prior ``reindex(...).ffill(limit=5)`` whose 5-row limit only happened
    to be tighter than the configured staleness tolerance.
    """
    weekly = pd.DatetimeIndex(pd.to_datetime(weekly_dates, errors="coerce")).dropna().normalize().unique()
    weekly = pd.DatetimeIndex(weekly).sort_values()
    if prices.empty or len(weekly) == 0:
        return pd.DataFrame(index=weekly, columns=prices.columns, dtype=float)
    prices = prices.sort_index()
    dense_index = prices.index.union(weekly).sort_values()
    dense = prices.reindex(dense_index)
    filled = dense.ffill()
    row_dates = np.asarray(dense_index.values, dtype="datetime64[ns]")
    observed = dense.notna().to_numpy()
    obs_stamp = np.where(observed, row_dates[:, None], np.datetime64("NaT", "ns"))
    last_obs = pd.DataFrame(obs_stamp, index=dense_index, columns=dense.columns).ffill().to_numpy()
    age_days = (row_dates[:, None] - last_obs) / np.timedelta64(1, "D")
    gated = filled.where(age_days <= float(max_stale_days))
    return gated.reindex(weekly)


def load_staging_prices(
    *,
    tickers: list[str],
    start_date: Any,
    end_date: Any,
    panel_dir: Path | None = None,
    runs_dir: Path | None = None,
    max_stage2_stale_days: int = MAX_STAGE2_PRICE_STALE_DAYS,
    freshness_as_of: Any = None,
) -> pd.DataFrame:
    """Load adjusted-close prices from Staging-owned sources.

    Deep history comes from the Stage 11 survivorship panel. The current tail is overlaid from
    the latest accepted Stage 2 risk panel so daily macro refreshes cannot silently relabel stale
    research-panel prices as current.
    """
    selected_dir = panel_dir or latest_accepted_survivorship_panel()
    history_path = selected_dir / "prices_adjclose.csv"
    if not history_path.exists():
        raise FileNotFoundError(f"Staging survivorship prices not found: {history_path}")
    history = _read_price_csv(history_path)
    freshness_date = pd.Timestamp(freshness_as_of if freshness_as_of is not None else end_date).normalize()
    live_dir = latest_accepted_stage2_risk_panel(runs_dir=runs_dir, as_of_date=freshness_date)
    live_path = live_dir / "prices_adjclose.csv"
    live = _read_price_csv(live_path)
    if live.empty:
        raise ValueError(f"Accepted Stage 2 risk panel is empty: {live_path}")
    end = pd.Timestamp(end_date).normalize()
    live_right_edge = pd.Timestamp(live.index.max()).normalize()
    stale_days = int((freshness_date - live_right_edge).days)
    if stale_days < 0:
        live = live.loc[live.index <= freshness_date].copy()
        live_right_edge = pd.Timestamp(live.index.max()).normalize()
        stale_days = int((freshness_date - live_right_edge).days)
    if stale_days > max_stage2_stale_days:
        raise ValueError(
            f"Stage 2 live risk price panel is stale for macro fit: right_edge={live_right_edge.date()} "
            f"freshness_as_of={freshness_date.date()} stale_days={stale_days} max={max_stage2_stale_days} path={live_path}"
        )
    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    prices = _overlay_prices(history, live)
    prices, missing_live, stale_live = _enforce_live_tail(
        prices,
        live,
        wanted=wanted,
        freshness_date=freshness_date,
        max_stage2_stale_days=max_stage2_stale_days,
    )
    if missing_live:
        LOGGER.info(
            "Stage 2 live risk panel does not cover %d/%d requested tickers; their survivorship history "
            "is retained and gated per-week by staleness (freshness=%s, max_stale=%dd); sample=%s",
            len(missing_live),
            len(set(wanted)),
            freshness_date.date(),
            max_stage2_stale_days,
            missing_live[:10],
        )
    if stale_live:
        LOGGER.warning(
            "Out-of-tolerance stale tails (last bar > %dd before freshness=%s) blanked in the current "
            "window for %d/%d requested tickers; sample=%s",
            max_stage2_stale_days,
            freshness_date.date(),
            len(stale_live),
            len(set(wanted)),
            stale_live[:10],
        )
    available = [t for t in wanted if t in prices.columns]
    missing = sorted(set(wanted) - set(available))
    if missing:
        LOGGER.warning(
            "Staging price sources missing %d/%d requested tickers; sample=%s",
            len(missing),
            len(wanted),
            missing[:10],
        )
    start = pd.Timestamp(start_date).normalize()
    if end < start:
        raise ValueError(f"end_date {end.date()} is before start_date {start.date()}")
    out = prices.loc[(prices.index >= start) & (prices.index <= end), available].copy()
    if out.empty:
        raise ValueError(
            f"No Staging prices available for requested window {start.date()}..{end.date()} "
            f"from history={history_path} live={live_path}"
        )
    LOGGER.info(
        "Loaded Staging prices: tickers=%d history=%s live=%s live_right_edge=%s stale_days=%d",
        len(available),
        history_path,
        live_path,
        live_right_edge.date(),
        stale_days,
    )
    return out
