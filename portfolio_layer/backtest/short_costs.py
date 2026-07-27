"""Point-in-time execution and stock-borrow inputs for Stage 11 short research.

The adapter is deliberately independent of the market_positioning package. It
opens the Staging-owned SQLite database read-only, performs backward-only
as-of lookups, and records every resolved observation so the consuming
backtest can seal the exact economic inputs it used.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


def snapshot_sqlite_database(source: Path, destination_root: Path) -> tuple[Path, str]:
    """Create and content-address one transactionally consistent SQLite image."""
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"SQLite source is missing: {source}")
    destination_root.mkdir(parents=True, exist_ok=True)
    staging = destination_root / f".snapshot-{os.getpid()}.sqlite"
    if staging.exists():
        staging.unlink()
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(staging)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    digest = hashlib.sha256()
    with staging.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    snapshot = destination_root / f"{sha256}.sqlite"
    if snapshot.exists():
        staging.unlink()
    else:
        staging.replace(snapshot)
    return snapshot, sha256


def parse_spread_tiers(raw: Any) -> list[tuple[float, float]]:
    """Normalize the config price-tier map into a descending ``(min_price, half_spread_bps)`` list.

    A flat fallback half spread is optimistic by an order of magnitude for sub-$5 names, which is how
    a corrupt sub-penny tape can look tradable. Tiers are matched highest ``min_price`` first, so the
    list is stored descending and the last entry must be the catch-all floor.
    """
    if not raw:
        return []
    tiers: list[tuple[float, float]] = []
    entries: Sequence[Any] = raw if isinstance(raw, (list, tuple)) else []
    if not entries and isinstance(raw, dict):
        entries = [
            {"min_price": key, "half_spread_bps": value} for key, value in raw.items()
        ]
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"spread tier entries must be mappings, got {entry!r}")
        price = _nonnegative(entry.get("min_price", 0.0), "spread tier min_price")
        spread = _nonnegative(entry.get("half_spread_bps", 0.0), "spread tier half_spread_bps")
        tiers.append((price, spread))
    if not tiers:
        return []
    tiers.sort(key=lambda item: item[0], reverse=True)
    if len({price for price, _ in tiers}) != len(tiers):
        raise ValueError("spread tier min_price values must be unique")
    if tiers[-1][0] > 0.0:
        raise ValueError("spread tier map needs a min_price=0 catch-all floor")
    return tiers


def tier_half_spread_bps(
    tiers: list[tuple[float, float]], price: float | None
) -> tuple[float, str] | None:
    """Resolve ``price`` against the descending tier list; None when no tier map is configured."""
    if not tiers:
        return None
    if price is None or not math.isfinite(price) or price <= 0:
        # An unpriced name gets the most punitive tier. Never the cheapest.
        worst = max(tiers, key=lambda item: item[1])
        return worst[1], "price_tier_unpriced"
    for min_price, spread in tiers:
        if price >= min_price:
            return spread, f"price_tier_ge_{min_price:g}"
    worst = max(tiers, key=lambda item: item[1])
    return worst[1], "price_tier_unpriced"


@dataclass(frozen=True)
class AsOfValue:
    value: float
    observation_date: str
    age_days: int
    source: str


@dataclass(frozen=True)
class ResolvedShortCost:
    as_of_date: str
    ticker: str
    half_spread_bps: float
    spread_source: str
    borrow_fee_annual: float
    borrow_source: str
    shortable_shares: float | None
    shortable_source: str
    short_available: bool


class _AsOfSeries:
    def __init__(self, rows: Iterable[tuple[str, str, float, str]]) -> None:
        grouped: dict[str, list[tuple[str, float, str]]] = {}
        for ticker, as_of, value, source in rows:
            key = str(ticker).strip().upper()
            day = str(as_of).strip()[:10]
            number = float(value)
            if not key or len(day) != 10 or not math.isfinite(number):
                continue
            grouped.setdefault(key, []).append((day, number, str(source)))
        self._data: dict[str, tuple[list[str], list[float], list[str]]] = {}
        for ticker, values in grouped.items():
            values.sort(key=lambda row: row[0])
            self._data[ticker] = (
                [row[0] for row in values],
                [row[1] for row in values],
                [row[2] for row in values],
            )

    def lookup(self, ticker: str, as_of: str, max_age_days: int) -> AsOfValue | None:
        packed = self._data.get(str(ticker).strip().upper())
        if packed is None:
            return None
        days, values, sources = packed
        pos = bisect.bisect_right(days, as_of) - 1
        if pos < 0:
            return None
        age = (date.fromisoformat(as_of) - date.fromisoformat(days[pos])).days
        if age < 0 or age > max(0, int(max_age_days)):
            return None
        return AsOfValue(values[pos], days[pos], age, sources[pos])


def _read_rows(
    db_path: Path,
    *,
    table: str,
    value_column: str,
    tickers: set[str],
    start_date: str,
    end_date: str,
    max_age_days: int,
) -> list[tuple[str, str, float, str]]:
    if table not in {"ibkr_borrow_fee_rate_daily", "ibkr_shortable_shares_snapshots"}:
        raise ValueError(f"Unsupported market-positioning table: {table}")
    if value_column not in {"borrow_fee_rate", "shortable_shares"}:
        raise ValueError(f"Unsupported market-positioning value column: {value_column}")
    if not db_path.exists():
        raise FileNotFoundError(f"Market-positioning database does not exist: {db_path}")
    query_start = (date.fromisoformat(start_date) - timedelta(days=max(0, max_age_days))).isoformat()
    before = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
    # immutable=1 prevents SQLite from trying to create journal/lock sidecars in the
    # externally owned DB directory. The before/after identity check below makes
    # this safe: a concurrent writer causes a hard failure rather than a mixed read.
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if present is None:
            raise ValueError(f"Required table {table} is missing from {db_path}")
        output: list[tuple[str, str, float, str]] = []
        ordered = sorted(tickers)
        for offset in range(0, len(ordered), 400):
            batch = ordered[offset:offset + 400]
            marks = ",".join("?" for _ in batch)
            sql = (
                f"SELECT ticker, asof_date, {value_column}, source FROM {table} "
                f"WHERE ticker IN ({marks}) AND asof_date BETWEEN ? AND ? "
                "ORDER BY ticker, asof_date"
            )
            params = [*batch, query_start, end_date]
            for ticker, as_of, value, source in conn.execute(sql, params):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number) and number >= 0:
                    output.append((str(ticker), str(as_of), number, str(source)))
        after = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
        if after != before:
            raise RuntimeError(f"Market-positioning database changed during read: {db_path}")
        return output
    finally:
        conn.close()


class PITShortCostModel:
    """Resolve execution and borrow inputs without any forward-looking fill."""

    def __init__(
        self,
        *,
        db_path: Path,
        tickers: set[str],
        start_date: str,
        end_date: str,
        exact_half_spreads: dict[tuple[str, str], float],
        spread_fallback_bps: float,
        borrow_fee_fallback_annual: float,
        max_borrow_fee_age_days: int,
        max_shortable_age_days: int,
        allow_fee_proxy_availability: bool,
        allow_unknown_availability: bool,
        stress_spread_fallback_bps: float,
        stress_spread_multiplier: float,
        stress_borrow_fee_fallback_annual: float,
        stress_borrow_fee_multiplier: float,
        tiered_spread_fallback_bps: list[tuple[float, float]] | None = None,
        reference_price: Callable[[str, str], float | None] | None = None,
    ) -> None:
        self.db_path = db_path
        self.tiered_spread_fallback_bps = list(tiered_spread_fallback_bps or [])
        self.reference_price = reference_price
        self.exact_half_spreads = {
            (str(day)[:10], str(ticker).strip().upper()): float(value)
            for (day, ticker), value in exact_half_spreads.items()
            if math.isfinite(float(value)) and float(value) >= 0
        }
        self.spread_fallback_bps = _nonnegative(spread_fallback_bps, "spread_fallback_bps")
        self.borrow_fee_fallback_annual = _nonnegative(
            borrow_fee_fallback_annual, "borrow_fee_fallback_annual"
        )
        self.max_borrow_fee_age_days = max(0, int(max_borrow_fee_age_days))
        self.max_shortable_age_days = max(0, int(max_shortable_age_days))
        self.allow_fee_proxy_availability = bool(allow_fee_proxy_availability)
        self.allow_unknown_availability = bool(allow_unknown_availability)
        self.stress_spread_fallback_bps = _nonnegative(
            stress_spread_fallback_bps, "stress_spread_fallback_bps"
        )
        self.stress_spread_multiplier = _positive(
            stress_spread_multiplier, "stress_spread_multiplier"
        )
        self.stress_borrow_fee_fallback_annual = _nonnegative(
            stress_borrow_fee_fallback_annual, "stress_borrow_fee_fallback_annual"
        )
        self.stress_borrow_fee_multiplier = _positive(
            stress_borrow_fee_multiplier, "stress_borrow_fee_multiplier"
        )
        db_identity_before = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
        borrow_rows = _read_rows(
            db_path,
            table="ibkr_borrow_fee_rate_daily",
            value_column="borrow_fee_rate",
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            max_age_days=self.max_borrow_fee_age_days,
        )
        shortable_rows = _read_rows(
            db_path,
            table="ibkr_shortable_shares_snapshots",
            value_column="shortable_shares",
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            max_age_days=self.max_shortable_age_days,
        )
        db_identity_after = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
        if db_identity_after != db_identity_before:
            raise RuntimeError(
                f"Market-positioning database changed between borrow and shortability reads: {db_path}"
            )
        self._borrow = _AsOfSeries(borrow_rows)
        self._shortable = _AsOfSeries(shortable_rows)
        self._used: dict[tuple[str, str], ResolvedShortCost] = {}

    def resolve(self, as_of: str, ticker: str) -> ResolvedShortCost:
        day = str(as_of)[:10]
        symbol = str(ticker).strip().upper()
        spread = self.exact_half_spreads.get((day, symbol))
        if spread is None:
            # No exact panel quote. Price-tier the fallback so a $0.004 expert-market print is not
            # charged the same 15bps as a mega-cap. The flat fallback survives only when no tier
            # map is configured (legacy/self-test paths).
            tiered = tier_half_spread_bps(
                self.tiered_spread_fallback_bps,
                self.reference_price(day, symbol) if self.reference_price else None,
            )
            if tiered is None:
                spread = self.spread_fallback_bps
                spread_source = "conservative_fallback"
            else:
                spread, spread_source = tiered
        else:
            spread_source = "ibkr_exact"

        borrow = self._borrow.lookup(symbol, day, self.max_borrow_fee_age_days)
        if borrow is None:
            borrow_fee = self.borrow_fee_fallback_annual
            borrow_source = "conservative_fallback"
        else:
            borrow_fee = borrow.value
            borrow_source = f"{borrow.source}:{borrow.observation_date}"

        shortable = self._shortable.lookup(symbol, day, self.max_shortable_age_days)
        if shortable is not None:
            shares: float | None = shortable.value
            shortable_source = f"{shortable.source}:{shortable.observation_date}"
            available = shares > 0
        elif borrow is not None and self.allow_fee_proxy_availability:
            shares = None
            shortable_source = f"fee_proxy:{borrow.observation_date}"
            available = True
        else:
            shares = None
            shortable_source = "unknown_fallback"
            available = self.allow_unknown_availability

        resolved = ResolvedShortCost(
            as_of_date=day,
            ticker=symbol,
            half_spread_bps=spread,
            spread_source=spread_source,
            borrow_fee_annual=borrow_fee,
            borrow_source=borrow_source,
            shortable_shares=shares,
            shortable_source=shortable_source,
            short_available=available,
        )
        self._used[(day, symbol)] = resolved
        return resolved

    def stressed_half_spread_bps(self, value: ResolvedShortCost) -> float:
        if value.spread_source == "ibkr_exact":
            return value.half_spread_bps * self.stress_spread_multiplier
        if value.spread_source.startswith("price_tier"):
            # A tiered fallback is already a modelled estimate; stress it multiplicatively but never
            # below the flat stress floor.
            return max(
                value.half_spread_bps * self.stress_spread_multiplier,
                self.stress_spread_fallback_bps,
            )
        return max(value.half_spread_bps, self.stress_spread_fallback_bps)

    def spread_source_distribution(self) -> dict[str, int]:
        """Counts of resolved spread provenance over distinct (date, ticker) observations."""
        counts: Counter[str] = Counter(
            resolved.spread_source for resolved in self._used.values()
        )
        return dict(sorted(counts.items()))

    def borrow_source_distribution(self) -> dict[str, int]:
        """Counts of resolved borrow provenance, collapsed to the provider prefix."""
        counts: Counter[str] = Counter(
            resolved.borrow_source.split(":", 1)[0] for resolved in self._used.values()
        )
        return dict(sorted(counts.items()))

    def stressed_borrow_fee_annual(self, value: ResolvedShortCost) -> float:
        if value.borrow_source == "conservative_fallback":
            return max(value.borrow_fee_annual, self.stress_borrow_fee_fallback_annual)
        return value.borrow_fee_annual * self.stress_borrow_fee_multiplier

    def used_rows(self) -> list[dict[str, object]]:
        return [asdict(self._used[key]) for key in sorted(self._used)]


def _nonnegative(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative, got {value!r}")
    return number


def _positive(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive, got {value!r}")
    return number


def selftest_short_cost_model() -> None:
    """Prove exact-date use, backward-only joins, bounded staleness, and fallback behavior."""
    with tempfile.TemporaryDirectory(prefix="short_cost_selftest_") as raw_dir:
        db_path = Path(raw_dir) / "positioning.sqlite"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE ibkr_borrow_fee_rate_daily (
                    ticker TEXT, asof_date TEXT, borrow_fee_rate REAL, source TEXT
                );
                CREATE TABLE ibkr_shortable_shares_snapshots (
                    ticker TEXT, asof_date TEXT, shortable_shares REAL, source TEXT
                );
                INSERT INTO ibkr_borrow_fee_rate_daily
                    VALUES ('A', '2020-01-03', 0.02, 'test');
                INSERT INTO ibkr_shortable_shares_snapshots
                    VALUES ('A', '2020-01-03', 1000, 'test');
                """
            )
            conn.commit()
        finally:
            conn.close()
        model = PITShortCostModel(
            db_path=db_path,
            tickers={"A"},
            start_date="2020-01-01",
            end_date="2020-01-20",
            exact_half_spreads={("2020-01-03", "A"): 4.0},
            spread_fallback_bps=15.0,
            borrow_fee_fallback_annual=0.10,
            max_borrow_fee_age_days=7,
            max_shortable_age_days=7,
            allow_fee_proxy_availability=True,
            allow_unknown_availability=False,
            stress_spread_fallback_bps=30.0,
            stress_spread_multiplier=1.5,
            stress_borrow_fee_fallback_annual=0.25,
            stress_borrow_fee_multiplier=1.5,
        )
        before = model.resolve("2020-01-02", "A")
        assert before.borrow_source == "conservative_fallback"
        assert not before.short_available
        exact = model.resolve("2020-01-03", "A")
        assert exact.spread_source == "ibkr_exact" and exact.half_spread_bps == 4.0
        assert exact.borrow_fee_annual == 0.02 and exact.shortable_shares == 1000
        stale = model.resolve("2020-01-20", "A")
        assert stale.borrow_source == "conservative_fallback"
        assert stale.shortable_source == "unknown_fallback"
        assert model.spread_source_distribution() == {
            "conservative_fallback": 2,
            "ibkr_exact": 1,
        }, model.spread_source_distribution()

        # --- price-tiered fallback spreads (2026-07-25 fix 4) ---
        tiers = parse_spread_tiers(
            [
                {"min_price": 5.0, "half_spread_bps": 25.0},
                {"min_price": 0.0, "half_spread_bps": 300.0},
                {"min_price": 20.0, "half_spread_bps": 10.0},
                {"min_price": 1.0, "half_spread_bps": 75.0},
            ]
        )
        assert tiers == [(20.0, 10.0), (5.0, 25.0), (1.0, 75.0), (0.0, 300.0)], tiers
        assert tier_half_spread_bps(tiers, 50.0) == (10.0, "price_tier_ge_20")
        assert tier_half_spread_bps(tiers, 20.0) == (10.0, "price_tier_ge_20")
        assert tier_half_spread_bps(tiers, 7.5) == (25.0, "price_tier_ge_5")
        assert tier_half_spread_bps(tiers, 2.0) == (75.0, "price_tier_ge_1")
        assert tier_half_spread_bps(tiers, 0.004) == (300.0, "price_tier_ge_0")
        assert tier_half_spread_bps(tiers, None) == (300.0, "price_tier_unpriced")
        assert tier_half_spread_bps([], 7.5) is None
        try:
            parse_spread_tiers([{"min_price": 1.0, "half_spread_bps": 75.0}])
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("tier map without a zero floor must be rejected")

        prices = {("2020-01-05", "A"): 0.004, ("2020-01-06", "A"): 42.0}
        tiered_model = PITShortCostModel(
            db_path=db_path,
            tickers={"A"},
            start_date="2020-01-01",
            end_date="2020-01-20",
            exact_half_spreads={("2020-01-03", "A"): 4.0},
            spread_fallback_bps=15.0,
            borrow_fee_fallback_annual=0.25,
            max_borrow_fee_age_days=7,
            max_shortable_age_days=7,
            allow_fee_proxy_availability=True,
            allow_unknown_availability=False,
            stress_spread_fallback_bps=30.0,
            stress_spread_multiplier=1.5,
            stress_borrow_fee_fallback_annual=0.375,
            stress_borrow_fee_multiplier=1.5,
            tiered_spread_fallback_bps=tiers,
            reference_price=lambda day, ticker: prices.get((day, ticker)),
        )
        penny = tiered_model.resolve("2020-01-05", "A")
        assert penny.half_spread_bps == 300.0 and penny.spread_source == "price_tier_ge_0"
        assert tiered_model.stressed_half_spread_bps(penny) == 450.0
        liquid = tiered_model.resolve("2020-01-06", "A")
        assert liquid.half_spread_bps == 10.0 and liquid.spread_source == "price_tier_ge_20"
        assert tiered_model.stressed_half_spread_bps(liquid) == 30.0
        exact_tiered = tiered_model.resolve("2020-01-03", "A")
        assert exact_tiered.spread_source == "ibkr_exact" and exact_tiered.half_spread_bps == 4.0
        # fail-closed availability: an unknown borrow is not a shortable name
        unknown = tiered_model.resolve("2020-01-20", "A")
        assert unknown.shortable_source == "unknown_fallback" and not unknown.short_available
        assert unknown.borrow_fee_annual == 0.25
        assert tiered_model.spread_source_distribution() == {
            "ibkr_exact": 1,
            "price_tier_ge_0": 1,
            "price_tier_ge_20": 1,
            "price_tier_unpriced": 1,
        }, tiered_model.spread_source_distribution()
    print("short-cost model self-test: PASS")
