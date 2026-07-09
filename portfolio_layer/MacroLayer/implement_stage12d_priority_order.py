from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from staging_portfolio_adapter import load_staging_prices  # noqa: E402

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class IbkrConfig:
    host: str
    port: int
    client_id: int
    account: str | None
    market_data_type: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Implement Stage 12D priority review: compare cases, select candidate, and build trade deltas."
    )
    parser.add_argument("--final-dir", type=Path, default=Path("MacroLayer/out/final_optimizer"))
    parser.add_argument("--base-config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--trade-config", type=Path, default=Path("Portfolio_Execution/trade_config.yaml"))
    parser.add_argument("--baseline-case", default="baseline_no_macro")
    parser.add_argument("--candidate-case", default="macro_full")
    parser.add_argument("--tie-break-case", default="macro_full")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--fetch-ibkr", action="store_true")
    parser.add_argument("--ib-client-id", type=int, default=913)
    parser.add_argument("--portfolio-value", type=float, default=None)
    parser.add_argument("--min-trade-dollars", type=float, default=100.0)
    return parser.parse_args()


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def _resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def _as_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _read_weights(final_dir: Path, case_name: str) -> pd.DataFrame:
    path = final_dir / case_name / "weights_long_only.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    out = pd.read_csv(path)
    if "Ticker" not in out.columns or "Weight" not in out.columns:
        raise ValueError(f"{path} must contain Ticker and Weight columns.")
    out["Ticker"] = out["Ticker"].astype(str).str.upper().str.strip()
    out["Weight"] = _as_float(out["Weight"]).fillna(0.0)
    out["CaseName"] = case_name
    return out


def _read_case_summary(final_dir: Path) -> pd.DataFrame:
    path = final_dir / "stage12d_optimizer_case_summary.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    out = pd.read_csv(path)
    for col in (
        "exp_return_ann",
        "vol_ann",
        "sharpe_ann",
        "cash_max_slack",
        "objective_value",
    ):
        if col in out.columns:
            out[col] = _as_float(out[col])
    return out


def _read_acceptance(final_dir: Path) -> pd.DataFrame:
    path = final_dir / "checks" / "stage12d_optimizer_acceptance_summary.csv"
    if not path.exists():
        return pd.DataFrame()
    out = pd.read_csv(path)
    if "passed" in out.columns:
        out["passed"] = _as_float(out["passed"]).fillna(0).astype(int)
    return out


def build_case_comparison(summary: pd.DataFrame, baseline_case: str) -> pd.DataFrame:
    out = summary.copy()
    baseline = out.loc[out["case_name"].astype(str).eq(baseline_case)]
    if baseline.empty:
        raise ValueError(f"Baseline case not found in case summary: {baseline_case}")
    base_row = baseline.iloc[0]
    for col in ("exp_return_ann", "vol_ann", "sharpe_ann", "cash_max_slack"):
        if col in out.columns:
            out[f"{col}_delta_vs_{baseline_case}"] = _as_float(out[col]) - float(base_row[col])
    ranked = out.sort_values(["sharpe_ann", "exp_return_ann"], ascending=[False, False]).reset_index(drop=True)
    ranked.insert(0, "case_rank", np.arange(1, len(ranked) + 1))
    return ranked


def _merge_weights(target: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    meta_cols = [
        "Ticker",
        "Company",
        "Sleeve",
        "AssetType",
        "Rating",
        "SectorName",
        "IndustryAggregateName",
        "IndustryName",
        "RegionGroup",
        "SignalScore",
        "NextEarningsDate",
        "EarningsDaysAhead",
        "EarningsDaysAheadAsOf",
    ]
    target_cols = [c for c in meta_cols if c in target.columns] + ["Weight", "Low", "High"]
    baseline_cols = [c for c in meta_cols if c in baseline.columns] + ["Weight", "Low", "High"]
    merged = target[target_cols].merge(
        baseline[baseline_cols],
        on="Ticker",
        how="outer",
        suffixes=("_target", "_baseline"),
    )
    for col in meta_cols:
        if col == "Ticker":
            continue
        target_col = f"{col}_target"
        baseline_col = f"{col}_baseline"
        if target_col in merged.columns and baseline_col in merged.columns:
            merged[col] = merged[target_col].combine_first(merged[baseline_col])
            merged = merged.drop(columns=[target_col, baseline_col])
        elif target_col in merged.columns:
            merged = merged.rename(columns={target_col: col})
        elif baseline_col in merged.columns:
            merged = merged.rename(columns={baseline_col: col})
    merged["target_weight"] = _as_float(merged.get("Weight_target", pd.Series(dtype="float64"))).fillna(0.0)
    merged["baseline_weight"] = _as_float(merged.get("Weight_baseline", pd.Series(dtype="float64"))).fillna(0.0)
    merged["delta_weight"] = merged["target_weight"] - merged["baseline_weight"]
    merged["abs_delta_weight"] = merged["delta_weight"].abs()
    merged["case_change"] = np.select(
        [
            merged["baseline_weight"].le(0) & merged["target_weight"].gt(0),
            merged["baseline_weight"].gt(0) & merged["target_weight"].le(0),
            merged["delta_weight"].gt(0),
            merged["delta_weight"].lt(0),
        ],
        ["ADD", "REMOVE", "INCREASE", "DECREASE"],
        default="UNCHANGED",
    )
    drop_cols = [c for c in ("Weight_target", "Weight_baseline", "Low_baseline", "High_baseline") if c in merged.columns]
    merged = merged.drop(columns=drop_cols)
    rename_cols = {"Low_target": "target_low", "High_target": "target_high"}
    merged = merged.rename(columns={k: v for k, v in rename_cols.items() if k in merged.columns})
    return merged.sort_values("abs_delta_weight", ascending=False).reset_index(drop=True)


def build_group_comparison(holdings_diff: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for group_type, col in (
        ("Sleeve", "Sleeve"),
        ("AssetType", "AssetType"),
        ("Sector", "SectorName"),
        ("IndustryAggregate", "IndustryAggregateName"),
    ):
        if col not in holdings_diff.columns:
            continue
        group = holdings_diff.copy()
        group[col] = group[col].fillna("UNKNOWN").astype(str).replace("", "UNKNOWN")
        agg = (
            group.groupby(col, as_index=False)
            .agg(
                target_weight=("target_weight", "sum"),
                baseline_weight=("baseline_weight", "sum"),
                member_count=("Ticker", "nunique"),
            )
            .rename(columns={col: "group_name"})
        )
        agg.insert(0, "group_type", group_type)
        agg["delta_weight"] = agg["target_weight"] - agg["baseline_weight"]
        agg["abs_delta_weight"] = agg["delta_weight"].abs()
        rows.append(agg)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).sort_values(
        ["group_type", "abs_delta_weight"],
        ascending=[True, False],
    )


def choose_candidate(
    comparison: pd.DataFrame,
    acceptance: pd.DataFrame,
    *,
    preferred_case: str,
    tie_break_case: str,
) -> dict[str, Any]:
    passing_cases: set[str] | None = None
    if not acceptance.empty and {"case_name", "passed"}.issubset(acceptance.columns):
        grouped = acceptance.groupby("case_name")["passed"].min()
        passing_cases = {str(case) for case, passed in grouped.items() if int(passed) == 1}
    eligible = comparison.copy()
    if passing_cases is not None:
        eligible = eligible.loc[eligible["case_name"].astype(str).isin(passing_cases)].copy()
    if eligible.empty:
        raise ValueError("No passing Stage 12D cases were available for candidate selection.")
    best_sharpe = float(eligible["sharpe_ann"].max())
    ties = eligible.loc[np.isclose(eligible["sharpe_ann"], best_sharpe, rtol=0.0, atol=1e-12)].copy()
    if tie_break_case in set(ties["case_name"].astype(str)):
        selected = ties.loc[ties["case_name"].astype(str).eq(tie_break_case)].iloc[0]
    elif preferred_case in set(ties["case_name"].astype(str)):
        selected = ties.loc[ties["case_name"].astype(str).eq(preferred_case)].iloc[0]
    else:
        selected = ties.sort_values("exp_return_ann", ascending=False).iloc[0]
    return {
        "selected_case": str(selected["case_name"]),
        "selected_portfolio": str(selected.get("portfolio", "LONG_ONLY")),
        "selection_rule": "highest passing sharpe_ann; tie broken by configured full-macro preference",
        "sharpe_ann": float(selected["sharpe_ann"]),
        "exp_return_ann": float(selected["exp_return_ann"]),
        "vol_ann": float(selected["vol_ann"]),
        "cash_budget_relaxation_used": str(selected.get("cash_budget_relaxation_used", "")),
        "candidate_case_requested": preferred_case,
        "tie_break_case": tie_break_case,
        "passing_cases": sorted(passing_cases) if passing_cases is not None else None,
    }


def _load_ibkr_config(path: Path, client_id_override: int) -> IbkrConfig:
    cfg = _read_yaml(path)
    ib_cfg = dict(cfg.get("ib", {}) or {})
    return IbkrConfig(
        host=str(ib_cfg.get("host", "127.0.0.1")),
        port=int(ib_cfg.get("port", 7497)),
        client_id=int(client_id_override),
        account=str(ib_cfg["account"]) if ib_cfg.get("account") else None,
        market_data_type=int(ib_cfg.get("market_data_type", 1)),
    )


def fetch_ibkr_holdings(ibkr_cfg: IbkrConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
    from ib_insync import IB

    ib = IB()
    ib.connect(ibkr_cfg.host, ibkr_cfg.port, clientId=ibkr_cfg.client_id, timeout=15)
    try:
        try:
            ib.reqMarketDataType(ibkr_cfg.market_data_type)
        except Exception:
            logger.warning("reqMarketDataType failed; continuing.", exc_info=True)
        accounts = list(ib.managedAccounts())
        account = ibkr_cfg.account or (accounts[0] if accounts else None)
        summary_items = ib.accountSummary(account=account) if account else ib.accountSummary()
        summary: dict[str, Any] = {"account": account}
        for item in summary_items:
            tag = str(getattr(item, "tag", ""))
            if tag in {"NetLiquidation", "TotalCashValue", "BuyingPower"}:
                try:
                    summary[tag] = float(item.value)
                except (TypeError, ValueError):
                    summary[tag] = item.value
        rows: list[dict[str, Any]] = []
        for item in ib.portfolio():
            if account and getattr(item, "account", None) and getattr(item, "account", None) != account:
                continue
            contract = getattr(item, "contract", None)
            ticker = str(getattr(contract, "symbol", "") or "").upper().strip()
            if not ticker:
                continue
            rows.append(
                {
                    "Ticker": ticker,
                    "account": getattr(item, "account", None),
                    "position": float(getattr(item, "position", 0.0) or 0.0),
                    "market_price": float(getattr(item, "marketPrice", np.nan)),
                    "market_value": float(getattr(item, "marketValue", 0.0) or 0.0),
                    "average_cost": float(getattr(item, "averageCost", np.nan)),
                    "unrealized_pnl": float(getattr(item, "unrealizedPNL", np.nan)),
                    "realized_pnl": float(getattr(item, "realizedPNL", np.nan)),
                    "currency": str(getattr(contract, "currency", "") or ""),
                    "exchange": str(getattr(contract, "exchange", "") or ""),
                }
            )
        return pd.DataFrame(rows), summary
    finally:
        ib.disconnect()


def _latest_close_prices(base_config: Path, tickers: list[str]) -> pd.Series:
    if not tickers:
        return pd.Series(dtype="float64")
    cfg = _read_yaml(base_config)
    end_raw = cfg.get("end")
    end = pd.to_datetime(end_raw, errors="coerce")
    if pd.isna(end):
        return pd.Series(dtype="float64")
    start = end - pd.Timedelta(days=20)
    try:
        prices = load_staging_prices(tickers=tickers, start_date=start, end_date=end)
    except Exception:
        logger.warning("Unable to load close prices from Staging survivorship panel.", exc_info=True)
        return pd.Series(dtype="float64")
    if prices.empty:
        return pd.Series(dtype="float64")
    return prices.ffill().iloc[-1].rename("reference_price")


def build_trade_delta(
    target: pd.DataFrame,
    current_holdings: pd.DataFrame,
    account_summary: dict[str, Any],
    *,
    base_config: Path,
    portfolio_value_override: float | None,
    min_trade_dollars: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    non_cash_target = target.loc[target["Ticker"].ne("CASH")].copy()
    net_liq = portfolio_value_override
    if net_liq is None:
        raw = account_summary.get("NetLiquidation")
        net_liq = float(raw) if raw is not None and np.isfinite(float(raw)) else None
    if net_liq is None or not np.isfinite(float(net_liq)) or float(net_liq) <= 0:
        raise ValueError("A positive portfolio value is required for trade delta generation.")
    portfolio_value = float(net_liq)

    holdings = current_holdings.copy()
    if holdings.empty:
        holdings = pd.DataFrame(columns=["Ticker", "position", "market_value", "market_price"])
    holdings["Ticker"] = holdings["Ticker"].astype(str).str.upper().str.strip()
    holdings = holdings.groupby("Ticker", as_index=False).agg(
        current_position=("position", "sum"),
        current_market_value=("market_value", "sum"),
        current_market_price=("market_price", "last"),
    )
    cash_raw = account_summary.get("TotalCashValue")
    if cash_raw is not None:
        cash_value = float(cash_raw)
    else:
        cash_value = portfolio_value - float(holdings["current_market_value"].sum())
    cash_row = pd.DataFrame(
        [
            {
                "Ticker": "CASH",
                "current_position": np.nan,
                "current_market_value": cash_value,
                "current_market_price": np.nan,
            }
        ]
    )
    holdings = pd.concat([holdings, cash_row], ignore_index=True)

    target_cols = [
        "Ticker",
        "Company",
        "Sleeve",
        "AssetType",
        "Rating",
        "SectorName",
        "IndustryAggregateName",
        "IndustryName",
        "SignalScore",
        "Weight",
        "Low",
        "High",
    ]
    target_frame = target[[c for c in target_cols if c in target.columns]].copy()
    target_frame = target_frame.rename(columns={"Weight": "target_weight", "Low": "target_low", "High": "target_high"})
    out = target_frame.merge(holdings, on="Ticker", how="outer")
    out["target_weight"] = _as_float(out.get("target_weight", pd.Series(dtype="float64"))).fillna(0.0)
    out["current_market_value"] = _as_float(out.get("current_market_value", pd.Series(dtype="float64"))).fillna(0.0)
    out["current_weight"] = out["current_market_value"] / portfolio_value
    out["target_market_value"] = out["target_weight"] * portfolio_value
    out["trade_dollar"] = out["target_market_value"] - out["current_market_value"]
    tickers = [t for t in out["Ticker"].dropna().astype(str).str.upper().unique().tolist() if t and t != "CASH"]
    close_prices = _latest_close_prices(base_config, tickers)
    out = out.merge(close_prices.rename_axis("Ticker").reset_index(), on="Ticker", how="left")
    out["reference_price"] = out["reference_price"].combine_first(_as_float(out.get("current_market_price", pd.Series(dtype="float64"))))
    out["estimated_trade_shares"] = np.where(
        out["Ticker"].eq("CASH") | out["reference_price"].isna() | out["reference_price"].le(0),
        np.nan,
        out["trade_dollar"] / out["reference_price"],
    )
    out["trade_action"] = np.select(
        [
            out["Ticker"].eq("CASH"),
            out["trade_dollar"].abs().lt(float(min_trade_dollars)),
            out["trade_dollar"].gt(0),
            out["trade_dollar"].lt(0),
        ],
        ["CASH_TARGET", "HOLD", "BUY", "SELL"],
        default="HOLD",
    )
    out["abs_trade_dollar"] = out["trade_dollar"].abs()
    out = out.sort_values("abs_trade_dollar", ascending=False).reset_index(drop=True)
    meta = {
        "portfolio_value": portfolio_value,
        "min_trade_dollars": float(min_trade_dollars),
        "net_liquidation_source": "override" if portfolio_value_override is not None else "IBKR NetLiquidation",
        "account": account_summary.get("account"),
        "target_case": str(target["CaseName"].iloc[0]) if "CaseName" in target.columns and not target.empty else None,
        "target_non_cash_names": int(non_cash_target["Ticker"].nunique()),
        "buy_count": int(out["trade_action"].eq("BUY").sum()),
        "sell_count": int(out["trade_action"].eq("SELL").sum()),
        "hold_count": int(out["trade_action"].eq("HOLD").sum()),
    }
    return out, meta


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_No rows._"
    display = frame.copy()
    for col in display.columns:
        if pd.api.types.is_float_dtype(display[col]):
            display[col] = display[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.6f}")
        else:
            display[col] = display[col].fillna("").astype(str)
    columns = [str(col) for col in display.columns]
    rows = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for _, row in display.iterrows():
        rows.append("| " + " | ".join(str(row[col]) for col in display.columns) + " |")
    return "\n".join(rows)


def write_markdown_summary(
    path: Path,
    decision: dict[str, Any],
    case_comparison: pd.DataFrame,
    holdings_diff: pd.DataFrame,
    trade_meta: dict[str, Any] | None,
) -> None:
    top_changes = holdings_diff.loc[holdings_diff["Ticker"].ne("CASH")].head(10)
    lines = [
        "# Stage 12D Priority Review",
        "",
        f"Selected case: `{decision['selected_case']}`",
        f"Selection rule: {decision['selection_rule']}",
        "",
        "## Case Metrics",
        "",
        _markdown_table(
            case_comparison[
                [
                    "case_rank",
                    "is_selected",
                    "case_name",
                    "exp_return_ann",
                    "vol_ann",
                    "sharpe_ann",
                    "cash_budget_relaxation_used",
                ]
            ]
        ),
        "",
        "## Largest Target Changes vs Baseline",
        "",
        _markdown_table(
            top_changes[
                ["Ticker", "Company", "target_weight", "baseline_weight", "delta_weight", "case_change"]
            ]
        ),
    ]
    if trade_meta is not None:
        lines.extend(
            [
                "",
                "## Trade Delta",
                "",
                f"Portfolio value used: `{trade_meta['portfolio_value']:.2f}`",
                f"Buy rows: `{trade_meta['buy_count']}`",
                f"Sell rows: `{trade_meta['sell_count']}`",
                f"Minimum trade dollars: `{trade_meta['min_trade_dollars']:.2f}`",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args()
    final_dir = _resolve_path(args.final_dir)
    out_dir = _resolve_path(args.out_dir) if args.out_dir else final_dir / "priority_review"
    base_config = _resolve_path(args.base_config)
    trade_config = _resolve_path(args.trade_config)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = _read_case_summary(final_dir)
    acceptance = _read_acceptance(final_dir)
    case_comparison = build_case_comparison(summary, args.baseline_case)
    decision = choose_candidate(
        case_comparison,
        acceptance,
        preferred_case=args.candidate_case,
        tie_break_case=args.tie_break_case,
    )
    selected_case = str(decision["selected_case"])
    case_comparison.insert(1, "is_selected", case_comparison["case_name"].astype(str).eq(selected_case))
    selected_rank = case_comparison.loc[case_comparison["is_selected"], "case_rank"]
    if not selected_rank.empty:
        decision["selected_rank"] = int(selected_rank.iloc[0])

    baseline = _read_weights(final_dir, args.baseline_case)
    selected = _read_weights(final_dir, selected_case)
    holdings_diff = _merge_weights(selected, baseline)
    group_comparison = build_group_comparison(holdings_diff)

    _write_csv(out_dir / "stage12d_case_metric_comparison.csv", case_comparison)
    _write_csv(out_dir / f"{selected_case}_vs_{args.baseline_case}_holding_deltas.csv", holdings_diff)
    _write_csv(out_dir / f"{selected_case}_vs_{args.baseline_case}_group_deltas.csv", group_comparison)
    (out_dir / "production_candidate_decision.json").write_text(
        json.dumps(decision, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    trade_meta: dict[str, Any] | None = None
    if args.fetch_ibkr:
        ibkr_cfg = _load_ibkr_config(trade_config, args.ib_client_id)
        current_holdings, account_summary = fetch_ibkr_holdings(ibkr_cfg)
        _write_csv(out_dir / "current_holdings_ibkr.csv", current_holdings)
        (out_dir / "ibkr_account_summary.json").write_text(
            json.dumps(account_summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        trade_delta, trade_meta = build_trade_delta(
            selected,
            current_holdings,
            account_summary,
            base_config=base_config,
            portfolio_value_override=args.portfolio_value,
            min_trade_dollars=args.min_trade_dollars,
        )
        _write_csv(out_dir / f"trade_delta_to_{selected_case}.csv", trade_delta)
        (out_dir / "trade_delta_summary.json").write_text(
            json.dumps(trade_meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    write_markdown_summary(out_dir / "priority_review_summary.md", decision, case_comparison, holdings_diff, trade_meta)
    logger.info("Priority review complete: selected_case=%s out_dir=%s", selected_case, out_dir)


if __name__ == "__main__":
    main()
