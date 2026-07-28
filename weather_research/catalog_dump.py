from __future__ import annotations

import argparse
import json

from .kalshi_api import KalshiClient


FIELDS = (
    "ticker",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "lower_inclusive",
    "upper_inclusive",
    "status",
    "close_time",
    "title",
    "subtitle",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump structured Kalshi weather market metadata")
    parser.add_argument("--series", required=True)
    args = parser.parse_args()

    client = KalshiClient.from_env()
    markets = client.list_markets(
        series_ticker=args.series,
        statuses=("open", "unopened"),
    )
    rows = [
        {field: market.get(field) for field in FIELDS}
        for market in sorted(markets, key=lambda row: (str(row.get("close_time") or ""), str(row.get("ticker") or "")))
    ]
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
