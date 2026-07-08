#!/usr/bin/env python3
"""
Fill stocks.sector and stocks.sector_id from Kiwoom ka10099 stock list.

Required env:
  KIWOOM_ACCESS_TOKEN

Optional env:
  DATABASE_URL (default postgresql://candle:candle@localhost:5432/candle_stock)
  KIWOOM_BASE_URL=https://api.kiwoom.com
  KIWOOM_STOCK_LIST_PATH=/api/dostk/stkinfo

Example:
  export KIWOOM_ACCESS_TOKEN="..."
  python3 scripts/fill_stock_sectors_from_kiwoom.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_DATABASE_URL = "postgresql://candle:candle@localhost:5432/candle_stock"
DEFAULT_BASE_URL = "https://api.kiwoom.com"
DEFAULT_STOCK_LIST_PATH = "/api/dostk/stkinfo"
STOCK_LIST_TR = "ka10099"
MARKET_CODES = {
    "KOSPI": "0",
    "KOSDAQ": "10",
    "KOTC": "30",
    "KONEX": "50",
    "ETF": "8",
    "ETN": "60",
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class StockSector:
    code: str
    name: str | None
    sector: str


class KiwoomClient:
    def __init__(self, base_url: str, stock_list_path: str, access_token: str, timeout: float, sleep: float) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.stock_list_path = stock_list_path
        self.access_token = access_token
        self.timeout = timeout
        self.sleep = sleep

    def fetch_market(self, market_code: str, max_pages: int) -> list[StockSector]:
        rows: dict[str, StockSector] = {}
        cont_yn = "N"
        next_key = ""
        pages = 0

        while True:
            body, headers = self._post(
                self.stock_list_path,
                {"mrkt_tp": market_code},
                extra_headers={"cont-yn": cont_yn, "next-key": next_key},
            )
            for item in parse_stock_list(body):
                rows[item.code] = item

            pages += 1
            cont_yn = header_value(headers, "cont-yn")
            next_key = header_value(headers, "next-key")
            if self.sleep > 0:
                time.sleep(self.sleep)
            if cont_yn.upper() != "Y" or not next_key or pages >= max_pages:
                break

        print(f"market={market_code}: fetched {len(rows)} sector row(s) from {pages} page(s)", file=sys.stderr)
        return sorted(rows.values(), key=lambda row: row.code)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "authorization": f"Bearer {self.access_token}",
            "api-id": STOCK_LIST_TR,
        }
        if extra_headers:
            headers.update(extra_headers)

        raw, response_headers = http_post_json(url, headers, payload, self.timeout)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Unexpected Kiwoom response for {STOCK_LIST_TR}: {type(raw).__name__}")
        return raw, response_headers


def http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    max_retries: int = 5,
    backoff: float = 1.0,
) -> tuple[Any, dict[str, str]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    attempt = 0
    while True:
        request = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                text = response.read().decode(charset, errors="replace")
                return json.loads(text) if text else {}, dict(response.headers.items())
        except HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            if exc.code in RETRYABLE_STATUS and attempt < max_retries:
                wait = retry_after_seconds(exc) or backoff * (2**attempt)
                print(f"HTTP {exc.code}; retry {attempt + 1}/{max_retries} in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                attempt += 1
                continue
            raise RuntimeError(f"HTTP {exc.code} from {url}: {text[:500]}") from exc
        except URLError as exc:
            if attempt < max_retries:
                wait = backoff * (2**attempt)
                print(f"Network error: {exc}; retry {attempt + 1}/{max_retries} in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                attempt += 1
                continue
            raise RuntimeError(f"Failed to call {url}: {exc}") from exc


def retry_after_seconds(exc: HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_stock_list(body: dict[str, Any]) -> list[StockSector]:
    parsed: dict[str, StockSector] = {}
    for item in find_first_list(body):
        if not isinstance(item, dict):
            continue
        code = clean_code(first_non_empty(item, "code", "stk_cd", "isu_cd", "종목코드"))
        sector = first_non_empty(item, "upName", "sector", "upjong", "bstp_nm", "업종", "업종명")
        if not code or not sector:
            continue
        parsed[code] = StockSector(
            code=code,
            name=first_non_empty(item, "name", "stk_nm", "isu_nm", "종목명"),
            sector=sector,
        )
    return sorted(parsed.values(), key=lambda row: row.code)


def find_first_list(body: dict[str, Any]) -> list[Any]:
    for key in ("list", "stk_list", "output", "items", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    for value in body.values():
        if isinstance(value, list):
            return value
    return []


def first_non_empty(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def clean_code(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    return text.zfill(6) if text.isdigit() else text


def header_value(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value.strip()
    return ""


def parse_codes(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [code for part in re.split(r"[\s,]+", raw) if (code := clean_code(part))]


def load_db_module():
    try:
        import psycopg  # type: ignore

        return psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return psycopg2
    except ImportError as exc:
        raise RuntimeError('Install a PostgreSQL driver first: pip install "psycopg[binary]"') from exc


def fetch_target_codes(database_url: str, include_existing: bool, limit: int | None) -> set[str]:
    db = load_db_module()
    where = "" if include_existing else "WHERE sector_id IS NULL OR sector IS NULL OR btrim(sector) = ''"
    limit_sql = " LIMIT %s" if limit else ""
    sql = f"SELECT stock_code FROM stocks {where} ORDER BY stock_code{limit_sql}"
    params = (limit,) if limit else ()

    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return {clean_code(row[0]) for row in cur.fetchall()}


def upsert_sector_names(cur: Any, items: list[StockSector], batch_size: int) -> None:
    sector_names = sorted({item.sector.strip() for item in items if item.sector.strip()})
    if not sector_names:
        return

    sql = """
        INSERT INTO stock_sectors (sector_name)
        VALUES (%s)
        ON CONFLICT (sector_name) DO NOTHING
    """
    values = [(sector_name,) for sector_name in sector_names]
    for index in range(0, len(values), batch_size):
        cur.executemany(sql, values[index : index + batch_size])


def update_sectors(database_url: str, items: list[StockSector], batch_size: int) -> int:
    if not items:
        return 0

    db = load_db_module()
    sql = """
        UPDATE stocks
        SET sector = %s,
            sector_id = (
                SELECT sector_id
                FROM stock_sectors
                WHERE sector_name = NULLIF(btrim(%s), '')
            )
        WHERE stock_code = %s
    """
    values = [(item.sector, item.sector, item.code) for item in items]
    updated = 0

    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            upsert_sector_names(cur, items, batch_size)
            for index in range(0, len(values), batch_size):
                batch = values[index : index + batch_size]
                cur.executemany(sql, batch)
                updated += cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(batch)
        conn.commit()
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill stocks.sector and stocks.sector_id from Kiwoom ka10099 upName.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--base-url", default=os.getenv("KIWOOM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--stock-list-path", default=os.getenv("KIWOOM_STOCK_LIST_PATH", DEFAULT_STOCK_LIST_PATH))
    parser.add_argument("--access-token", default=os.getenv("KIWOOM_ACCESS_TOKEN"))
    parser.add_argument("--market", choices=[*MARKET_CODES.keys(), "ALL"], default="ALL")
    parser.add_argument("--market-code", action="append", help="Raw Kiwoom mrkt_tp value. Can be repeated.")
    parser.add_argument("--code", action="append", help="Only update this stock code if it appears in ka10099. Can be repeated.")
    parser.add_argument("--codes", help="Comma/space/newline separated stock codes to filter.")
    parser.add_argument("--include-existing", action="store_true", help="Refill sectors even when sector already has a value.")
    parser.add_argument("--limit", type=int, help="Limit DB-selected targets before matching ka10099 rows.")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--sleep", type=float, default=0.25, help="Delay between Kiwoom calls.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print sectors without updating DB.")
    return parser.parse_args()


def requested_codes(args: argparse.Namespace) -> set[str]:
    codes: list[str] = []
    for code in args.code or []:
        codes.extend(parse_codes(code))
    codes.extend(parse_codes(args.codes))
    return set(codes)


def requested_market_codes(args: argparse.Namespace) -> list[str]:
    if args.market_code:
        return sorted(set(args.market_code))
    if args.market == "ALL":
        return [MARKET_CODES["KOSPI"], MARKET_CODES["KOSDAQ"]]
    return [MARKET_CODES[args.market]]


def main() -> int:
    args = parse_args()
    if not args.access_token:
        print("KIWOOM_ACCESS_TOKEN or --access-token is required.", file=sys.stderr)
        return 2

    target_codes = requested_codes(args)
    if not target_codes:
        target_codes = fetch_target_codes(args.database_url, args.include_existing, args.limit)
    if not target_codes:
        print("No target stock codes.", file=sys.stderr)
        return 0

    client = KiwoomClient(args.base_url, args.stock_list_path, args.access_token, args.timeout, args.sleep)
    fetched: dict[str, StockSector] = {}
    for market_code in requested_market_codes(args):
        for item in client.fetch_market(market_code, args.max_pages):
            if item.code in target_codes:
                fetched[item.code] = item

    items = sorted(fetched.values(), key=lambda row: row.code)
    missing = len(target_codes - set(fetched.keys()))

    if args.dry_run:
        for item in items:
            print(f"{item.code}\t{item.name or ''}\t{item.sector}")
        print(f"Fetched {len(items)} sector(s); missing={missing}; target={len(target_codes)}", file=sys.stderr)
        return 0

    updated = update_sectors(args.database_url, items, args.batch_size)
    print(f"Updated {updated} sector(s); missing={missing}; target={len(target_codes)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
