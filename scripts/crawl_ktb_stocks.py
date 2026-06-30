#!/usr/bin/env python3
import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence
from urllib.request import Request, urlopen


DEFAULT_URL = "https://www.ktb.co.kr/trading/popup/itemPop.jspx"
VALID_MARKETS = {"KOSPI", "KOSDAQ"}


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    market: str


def fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_js_args(raw: str) -> List[str]:
    args: List[str] = []
    current: List[str] = []
    in_quote = False
    escape = False

    for char in raw:
        if escape:
            current.append(char)
            escape = False
            continue

        if char == "\\" and in_quote:
            escape = True
            continue

        if char == "'":
            in_quote = not in_quote
            continue

        if char == "," and not in_quote:
            args.append("".join(current).strip())
            current = []
            continue

        current.append(char)

    args.append("".join(current).strip())
    return args


def parse_stocks(page_html: str) -> List[Stock]:
    stocks = {}
    for match in re.finditer(r"fnOnClick\((.*?)\)", page_html, flags=re.DOTALL):
        args = parse_js_args(html.unescape(match.group(1)))
        if len(args) < 7:
            continue

        code = args[1].strip()
        name = " ".join(args[2].split())
        market = args[6].strip().upper()

        if not code or not name or market not in VALID_MARKETS:
            continue

        stocks[code] = Stock(code=code, name=name, market=market)

    return sorted(stocks.values(), key=lambda stock: (stock.market, stock.code))


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def batched(items: Sequence[Stock], size: int) -> Iterable[Sequence[Stock]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def render_upsert_sql(stocks: Sequence[Stock], batch_size: int) -> str:
    if not stocks:
        raise ValueError("No stocks parsed from KTB page.")

    lines = [
        "-- Generated from https://www.ktb.co.kr/trading/popup/itemPop.jspx",
        "-- Apply sql/001_create_stocks.sql before running this file.",
        "BEGIN;",
        "",
    ]

    for batch in batched(stocks, batch_size):
        values = []
        for stock in batch:
            values.append(
                f"    ({sql_literal(stock.code)}, {sql_literal(stock.name)}, {sql_literal(stock.market)})"
            )

        lines.extend(
            [
                "INSERT INTO stocks (stock_code, stock_name, market_type)",
                "VALUES",
                ",\n".join(values),
                "ON CONFLICT (stock_code) DO UPDATE SET",
                "    stock_name = EXCLUDED.stock_name,",
                "    market_type = EXCLUDED.market_type,",
                "    updated_at = now();",
                "",
            ]
        )

    lines.extend(["COMMIT;", ""])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl KTB stock codes and generate TimescaleDB/PostgreSQL upsert SQL."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"KTB stock popup URL. Default: {DEFAULT_URL}")
    parser.add_argument(
        "--output",
        default="sql/stocks_upsert.sql",
        help="Path for generated upsert SQL. Use '-' to print to stdout.",
    )
    parser.add_argument("--batch-size", type=int, default=500, help="INSERT rows per statement.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.batch_size < 1:
        print("--batch-size must be greater than 0.", file=sys.stderr)
        return 2

    page_html = fetch_html(args.url)
    stocks = parse_stocks(page_html)
    sql = render_upsert_sql(stocks, args.batch_size)

    if args.output == "-":
        print(sql, end="")
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(sql, encoding="utf-8")

    market_counts = {}
    for stock in stocks:
        market_counts[stock.market] = market_counts.get(stock.market, 0) + 1

    summary = ", ".join(f"{market}={count}" for market, count in sorted(market_counts.items()))
    print(f"Parsed {len(stocks)} stocks ({summary}).", file=sys.stderr)
    if args.output != "-":
        print(f"Wrote {args.output}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
