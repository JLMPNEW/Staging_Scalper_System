from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


STATEMENT_META_FIELDS = [
    "source_sha256",
    "source_file",
    "broker_name",
    "title",
    "period",
    "period_start",
    "period_end",
    "when_generated",
    "account",
    "account_id",
    "accounts_included",
    "base_currency",
]

OPEN_POSITION_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "asset_category",
    "currency",
    "symbol",
    "quantity",
    "multiplier",
    "cost_price",
    "cost_basis",
    "close_price",
    "market_value",
    "unrealized_pl",
    "code",
]

NET_STOCK_POSITION_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "currency",
    "symbol",
    "description",
    "shares_at_ib",
    "shares_borrowed",
    "shares_lent",
    "net_shares",
]

TRADE_FIELDS = [
    "trade_key",
    "source_sha256",
    "statement_end_date",
    "source_row",
    "asset_category",
    "currency",
    "account",
    "symbol",
    "date_time",
    "trade_date",
    "quantity",
    "trade_price",
    "close_price",
    "proceeds",
    "commission_fee",
    "basis",
    "realized_pl",
    "mtm_pl",
    "code",
]

INSTRUMENT_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "asset_category",
    "symbol",
    "description",
    "conid",
    "security_id",
    "underlying",
    "listing_exchange",
    "multiplier",
    "instrument_type",
    "expiry",
    "delivery_month",
    "strike",
    "code",
]

CASH_REPORT_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "line_item",
    "currency",
    "total",
    "securities",
    "futures",
    "paxos",
    "month_to_date",
    "year_to_date",
]

DIVIDEND_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "currency",
    "date",
    "description",
    "amount",
]

CASH_TRANSACTION_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "currency",
    "settle_date",
    "description",
    "amount",
]

FEE_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "subtitle",
    "currency",
    "date",
    "description",
    "amount",
]

SECURITIES_LENDING_FIELDS = [
    "source_sha256",
    "statement_end_date",
    "source_row",
    "section",
    "asset_category",
    "currency",
    "account",
    "symbol",
    "date",
    "activity",
    "transaction_id",
    "quantity",
    "rate",
    "collateral_amount",
    "price",
    "value",
    "fee_amount",
    "interest_amount",
    "code",
]

HOLDING_LOT_FIELDS = [
    "run_as_of",
    "asset_category",
    "symbol",
    "lot_id",
    "quantity",
    "entry_date",
    "cost_basis",
    "cost_price",
    "entry_date_unknown",
    "source",
    "provenance",
    "source_sha256",
]

HOLDING_STATE_FIELDS = [
    "run_as_of",
    "asset_category",
    "currency",
    "symbol",
    "quantity",
    "multiplier",
    "cost_price",
    "cost_basis",
    "close_price",
    "market_value",
    "unrealized_pl",
    "source_sha256",
]

RECONCILIATION_FIELDS = [
    "run_as_of",
    "check",
    "status",
    "detail",
]

NUMERIC_OPEN_POSITION_COLUMNS = {
    "quantity",
    "multiplier",
    "cost_price",
    "cost_basis",
    "close_price",
    "market_value",
    "unrealized_pl",
}
NUMERIC_TRADE_COLUMNS = {"quantity", "trade_price", "close_price", "proceeds", "commission_fee", "basis", "realized_pl", "mtm_pl"}


@dataclass(frozen=True)
class ParsedStatement:
    meta: dict[str, str]
    open_positions: list[dict[str, str]]
    net_stock_positions: list[dict[str, str]]
    trades: list[dict[str, str]]
    instruments: list[dict[str, str]]
    cash_report: list[dict[str, str]]
    dividends: list[dict[str, str]]
    cash_transactions: list[dict[str, str]]
    fees: list[dict[str, str]]
    securities_lending: list[dict[str, str]]


def latest_ib_report(source_dir: Path, pattern: str = "*.csv") -> Path:
    reports = sorted(source_dir.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        raise FileNotFoundError(f"No IB CSV reports found under {source_dir} with glob {pattern!r}")
    return reports[0]


def peek_statement_period_end(path: Path) -> str | None:
    """Return a statement's period-end date by reading only its header block.

    IB activity statements emit the contiguous ``Statement`` metadata block first, so the
    ``Statement,Data,Period,...`` row appears within the first handful of lines. We return as soon as
    it is found (and bail the moment the block ends), so this stays O(header) even for a multi-MB
    full-year statement -- cheap enough to date every file during a backfill scan without parsing it.
    Returns ``None`` if no period row is found (caller should fall back to a full parse).
    """
    seen_statement = False
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if not row:
                    continue
                section = row[0].strip()
                if section == "Statement":
                    seen_statement = True
                    if len(row) >= 4 and row[1].strip() == "Data" and row[2].strip() == "Period":
                        _, end = parse_period(row[3].strip())
                        return end or None
                elif seen_statement:
                    # The Statement block is contiguous and first; once it ends, Period won't appear.
                    break
    except OSError:
        return None
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_number(raw: Any) -> float | None:
    text = str(raw or "").strip()
    if text in {"", "--"}:
        return None
    parenthesized_negative = text.startswith("(") and text.endswith(")")
    if parenthesized_negative:
        text = text[1:-1].strip()
    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    if parenthesized_negative:
        value = -value
    return value if math.isfinite(value) else None


def fmt_number(raw: Any) -> str:
    value = parse_number(raw)
    if value is None:
        return ""
    return f"{value:.12g}"


def normalize_date(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    if "," in text:
        prefix = text.split(",", 1)[0].strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d", "%b %d"):
            try:
                parsed = datetime.strptime(prefix, fmt)
                if parsed.year == 1900:
                    continue
                return parsed.date().isoformat()
            except ValueError:
                continue
    raise ValueError(f"Unsupported date value: {raw!r}")


def normalize_datetime(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d, %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat(sep=" ")
        except ValueError:
            continue
    return text


def parse_period(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip().strip('"')
    if " - " in text:
        left, right = text.split(" - ", 1)
        return normalize_date(left), normalize_date(right)
    one = normalize_date(text)
    return one, one


def account_id(account: str) -> str:
    text = str(account or "").strip()
    return text.split(" ", 1)[0].strip()


TRADE_IDENTITY_FIELDS = (
    "asset_category", "currency", "account", "symbol", "date_time", "quantity",
    "trade_price", "proceeds", "commission_fee", "basis", "code",
)


def trade_identity(row: dict[str, str]) -> str:
    return "|".join(str(row.get(field, "")).strip() for field in TRADE_IDENTITY_FIELDS)


def csv_trade_key(source_sha: str, row: dict[str, str], *, occurrence: int | None = None) -> str:
    """Stable cross-statement key for an economic fill.

    `source_sha` remains in the signature for backward API compatibility but is deliberately not
    part of the identity. The occurrence ordinal preserves repeated identical fills in one statement.
    """
    del source_sha
    ordinal = int(row.get("source_row", "0") or 0) if occurrence is None else int(occurrence)
    parts = [
        trade_identity(row),
        str(ordinal),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _clean_key(raw: str) -> str:
    return str(raw or "").strip().replace("\n", " ")


def _mapped(header: list[str], row: list[str]) -> dict[str, str]:
    values = row[2:]
    out: dict[str, str] = {}
    for idx, key in enumerate(header[2:]):
        clean = _clean_key(key)
        if clean:
            out[clean] = values[idx].strip() if idx < len(values) else ""
    return out


def _pick(mapped: dict[str, str], *keys: str) -> str:
    for key in keys:
        if key in mapped:
            return mapped.get(key, "")
    return ""


def _base(source_sha: str, statement_end: str, source_row: int) -> dict[str, str]:
    return {"source_sha256": source_sha, "statement_end_date": statement_end, "source_row": str(source_row)}


def parse_ib_activity_statement(path: Path) -> ParsedStatement:
    source_sha = sha256_file(path)
    headers: dict[str, list[str]] = {}
    meta_values: dict[str, str] = {"source_sha256": source_sha, "source_file": str(path.resolve())}
    open_positions: list[dict[str, str]] = []
    net_stock_positions: list[dict[str, str]] = []
    trades: list[dict[str, str]] = []
    instruments: list[dict[str, str]] = []
    cash_report: list[dict[str, str]] = []
    dividends: list[dict[str, str]] = []
    cash_transactions: list[dict[str, str]] = []
    fees: list[dict[str, str]] = []
    securities_lending: list[dict[str, str]] = []
    trade_occurrences: dict[str, int] = {}

    raw_rows: list[list[str]]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.reader(handle))

    # First pass: statement/account metadata.
    for row in raw_rows:
        if len(row) < 4 or row[1].strip() != "Data":
            continue
        section = row[0].strip()
        if section not in {"Statement", "Account Information"}:
            continue
        field = row[2].strip()
        value = row[3].strip()
        if section == "Statement":
            if field == "BrokerName":
                meta_values["broker_name"] = value
            elif field == "Title":
                meta_values["title"] = value
            elif field == "Period":
                meta_values["period"] = value
                start, end = parse_period(value)
                meta_values["period_start"] = start
                meta_values["period_end"] = end
            elif field == "WhenGenerated":
                meta_values["when_generated"] = value
        elif section == "Account Information":
            if field == "Account":
                meta_values["account"] = value
                meta_values["account_id"] = account_id(value)
            elif field == "Accounts Included":
                meta_values["accounts_included"] = value
            elif field == "Base Currency":
                meta_values["base_currency"] = value

    statement_end = meta_values.get("period_end", "")
    if not statement_end:
        raise ValueError(f"Could not determine IB statement period end from {path}")

    for line_no, row in enumerate(raw_rows, start=1):
        if len(row) < 2:
            continue
        section = row[0].strip()
        kind = row[1].strip()
        if kind == "Header":
            headers[section] = row
            continue
        if kind != "Data" or section not in headers:
            continue
        mapped = _mapped(headers[section], row)
        base = _base(source_sha, statement_end, line_no)

        if section == "Open Positions" and _pick(mapped, "DataDiscriminator") == "Summary":
            out = {
                **base,
                "asset_category": _pick(mapped, "Asset Category"),
                "currency": _pick(mapped, "Currency"),
                "symbol": _pick(mapped, "Symbol"),
                "quantity": fmt_number(_pick(mapped, "Quantity")),
                "multiplier": fmt_number(_pick(mapped, "Mult", "Multiplier")),
                "cost_price": fmt_number(_pick(mapped, "Cost Price")),
                "cost_basis": fmt_number(_pick(mapped, "Cost Basis")),
                "close_price": fmt_number(_pick(mapped, "Close Price")),
                "market_value": fmt_number(_pick(mapped, "Value")),
                "unrealized_pl": fmt_number(_pick(mapped, "Unrealized P/L")),
                "code": _pick(mapped, "Code"),
            }
            open_positions.append(out)
        elif section == "Net Stock Position Summary" and _pick(mapped, "Asset Category") == "Stocks":
            net_stock_positions.append({
                **base,
                "currency": _pick(mapped, "Currency"),
                "symbol": _pick(mapped, "Symbol"),
                "description": _pick(mapped, "Description"),
                "shares_at_ib": fmt_number(_pick(mapped, "Shares at IB")),
                "shares_borrowed": fmt_number(_pick(mapped, "Shares Borrowed")),
                "shares_lent": fmt_number(_pick(mapped, "Shares Lent")),
                "net_shares": fmt_number(_pick(mapped, "Net Shares")),
            })
        elif section == "Trades" and _pick(mapped, "DataDiscriminator") == "Order":
            date_time = normalize_datetime(_pick(mapped, "Date/Time"))
            trade_date = date_time.split(" ", 1)[0] if date_time else ""
            out = {
                **base,
                "asset_category": _pick(mapped, "Asset Category"),
                "currency": _pick(mapped, "Currency"),
                "account": _pick(mapped, "Account") or meta_values.get("account_id", ""),
                "symbol": _pick(mapped, "Symbol"),
                "date_time": date_time,
                "trade_date": trade_date,
                "quantity": fmt_number(_pick(mapped, "Quantity")),
                "trade_price": fmt_number(_pick(mapped, "T. Price")),
                "close_price": fmt_number(_pick(mapped, "C. Price")),
                "proceeds": fmt_number(_pick(mapped, "Proceeds")),
                "commission_fee": fmt_number(_pick(mapped, "Comm/Fee")),
                "basis": fmt_number(_pick(mapped, "Basis")),
                "realized_pl": fmt_number(_pick(mapped, "Realized P/L")),
                "mtm_pl": fmt_number(_pick(mapped, "MTM P/L")),
                "code": _pick(mapped, "Code"),
            }
            identity = trade_identity(out)
            occurrence = trade_occurrences.get(identity, 0) + 1
            trade_occurrences[identity] = occurrence
            out["trade_key"] = csv_trade_key(source_sha, out, occurrence=occurrence)
            trades.append(out)
        elif section == "Financial Instrument Information" and _pick(mapped, "Asset Category"):
            instruments.append({
                **base,
                "asset_category": _pick(mapped, "Asset Category"),
                "symbol": _pick(mapped, "Symbol"),
                "description": _pick(mapped, "Description"),
                "conid": _pick(mapped, "Conid"),
                "security_id": _pick(mapped, "Security ID"),
                "underlying": _pick(mapped, "Underlying"),
                "listing_exchange": _pick(mapped, "Listing Exch"),
                "multiplier": fmt_number(_pick(mapped, "Multiplier")),
                "instrument_type": _pick(mapped, "Type"),
                "expiry": normalize_date(_pick(mapped, "Expiry")) if _pick(mapped, "Expiry") else "",
                "delivery_month": _pick(mapped, "Delivery Month"),
                "strike": fmt_number(_pick(mapped, "Strike")),
                "code": _pick(mapped, "Code"),
            })
        elif section == "Cash Report" and _pick(mapped, "Currency Summary") and _pick(mapped, "Currency Summary") != "Total":
            cash_report.append({
                **base,
                "line_item": _pick(mapped, "Currency Summary"),
                "currency": _pick(mapped, "Currency"),
                "total": fmt_number(_pick(mapped, "Total")),
                "securities": fmt_number(_pick(mapped, "Securities")),
                "futures": fmt_number(_pick(mapped, "Futures")),
                "paxos": fmt_number(_pick(mapped, "Paxos")),
                "month_to_date": fmt_number(_pick(mapped, "Month to Date")),
                "year_to_date": fmt_number(_pick(mapped, "Year to Date")),
            })
        elif section == "Dividends" and _pick(mapped, "Currency") and _pick(mapped, "Currency") != "Total":
            dividends.append({
                **base,
                "currency": _pick(mapped, "Currency"),
                "date": normalize_date(_pick(mapped, "Date")) if _pick(mapped, "Date") else "",
                "description": _pick(mapped, "Description"),
                "amount": fmt_number(_pick(mapped, "Amount")),
            })
        elif section == "Deposits & Withdrawals" and _pick(mapped, "Currency") and _pick(mapped, "Currency") != "Total":
            cash_transactions.append({
                **base,
                "currency": _pick(mapped, "Currency"),
                "settle_date": normalize_date(_pick(mapped, "Settle Date")) if _pick(mapped, "Settle Date") else "",
                "description": _pick(mapped, "Description"),
                "amount": fmt_number(_pick(mapped, "Amount")),
            })
        elif section == "Fees" and _pick(mapped, "Currency") and _pick(mapped, "Currency") != "Total":
            fees.append({
                **base,
                "subtitle": _pick(mapped, "Subtitle"),
                "currency": _pick(mapped, "Currency"),
                "date": normalize_date(_pick(mapped, "Date")) if _pick(mapped, "Date") else "",
                "description": _pick(mapped, "Description"),
                "amount": fmt_number(_pick(mapped, "Amount")),
            })
        elif section.startswith("Stock Yield Enhancement Program Securities"):
            securities_lending.append({
                **base,
                "section": section,
                "asset_category": _pick(mapped, "Asset Category"),
                "currency": _pick(mapped, "Currency"),
                "account": _pick(mapped, "Account"),
                "symbol": _pick(mapped, "Symbol"),
                "date": normalize_date(_pick(mapped, "Date")) if _pick(mapped, "Date") else "",
                "activity": _pick(mapped, "Activity"),
                "transaction_id": _pick(mapped, "Transaction ID"),
                "quantity": fmt_number(_pick(mapped, "Quantity")),
                "rate": fmt_number(_pick(mapped, "SYEP Rate on Customer Collateral (%)", "IBKR Rate", "Customer Rate")),
                "collateral_amount": fmt_number(_pick(mapped, "Collateral Amount", "Collateral")),
                "price": fmt_number(_pick(mapped, "Price")),
                "value": fmt_number(_pick(mapped, "Value")),
                "fee_amount": fmt_number(_pick(mapped, "Fee", "Fee Earned")),
                "interest_amount": fmt_number(_pick(mapped, "Interest", "Interest Paid")),
                "code": _pick(mapped, "Code"),
            })

    for key in STATEMENT_META_FIELDS:
        meta_values.setdefault(key, "")
    return ParsedStatement(
        meta=meta_values,
        open_positions=open_positions,
        net_stock_positions=net_stock_positions,
        trades=trades,
        instruments=instruments,
        cash_report=cash_report,
        dividends=dividends,
        cash_transactions=cash_transactions,
        fees=fees,
        securities_lending=securities_lending,
    )


def rows_by_symbol(rows: Iterable[dict[str, str]], *, asset_category: str | None = None) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if asset_category and row.get("asset_category") != asset_category:
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol:
            grouped.setdefault(symbol, []).append(row)
    return grouped
