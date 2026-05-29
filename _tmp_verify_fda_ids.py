from __future__ import annotations

import json
from urllib.parse import quote, urlencode
from urllib.request import urlopen


TARGETS = {
    "DCTH": [("pma", "P200005"), ("pma", "P200004")],
    "ELMD": [("510k", "K132794"), ("510k", "K053248")],
    "OWLT": [("510k", "K231433"), ("510k", "K233434"), ("510k", "K232301")],
    "VREX": [("510k", "K193498"), ("510k", "K173546"), ("510k", "K210214")],
    "SNWV_CONFLICT": [("pma", "P200019"), ("510k", "K203309")],
}


def fetch(kind: str, ident: str) -> dict:
    field = "k_number" if kind == "510k" else "pma_number"
    url = f"https://api.fda.gov/device/{kind}.json?" + urlencode({"search": f'{field}:"{ident}"', "limit": "5"})
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def field(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def main() -> None:
    for ticker, items in TARGETS.items():
        print("\n", ticker)
        for kind, ident in items:
            try:
                payload = fetch(kind, ident)
            except Exception as exc:
                print(kind, ident, "ERROR", exc)
                continue
            results = payload.get("results") or []
            print(kind, ident, "count", len(results))
            for row in results:
                print(
                    " ",
                    {
                        "applicant": field(row, "applicant"),
                        "pma_number": field(row, "pma_number"),
                        "k_number": field(row, "k_number"),
                        "product_code": field(row, "product_code"),
                        "device_name": field(row, "device_name", "trade_name"),
                        "decision_date": field(row, "decision_date"),
                    },
                )


if __name__ == "__main__":
    main()
