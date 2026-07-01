#!/usr/bin/env python3
"""
Sync stock catalog enrichment fields from Kiwoom REST OpenAPI into PostgreSQL.

This script intentionally lives outside stock-service. It updates only catalog
fields that are currently missing from the seed data:
  sector, market_cap, shares_outstanding, data_source, synced_at

Required env:
  KIWOOM_APP_KEY
  KIWOOM_APP_SECRET
  DATABASE_URL, for example postgresql://candle:candle@localhost:5432/candle_stock

Optional env:
  KIWOOM_BASE_URL=https://api.kiwoom.com
  KIWOOM_TOKEN_PATH=/oauth2/token
  KIWOOM_STOCK_LIST_PATH=/api/dostk/stkinfo
  KIWOOM_STOCK_INFO_PATH=/api/dostk/stkinfo
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://api.kiwoom.com"
DEFAULT_TOKEN_PATH = "/oauth2/token"
DEFAULT_STOCK_LIST_PATH = "/api/dostk/stkinfo"
DEFAULT_STOCK_INFO_PATH = "/api/dostk/stkinfo"
STOCK_LIST_TR = "ka10099"
STOCK_INFO_TR = "ka10100"
MARKET_CODES = {"KOSPI": "0", "KOSDAQ": "10"}


@dataclass(frozen=True)
class StockCatalogRow:
    code: str
    name: str
    market: str
    sector: str | None
    market_cap: int | None
    shares_outstanding: int | None


class KiwoomClient:
    def __init__(
        self,
        base_url: str,
        app_key: str,
        app_secret: str,
        token_path: str,
        stock_list_path: str,
        stock_info_path: str,
        timeout: float,
        sleep_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.app_key = app_key
        self.app_secret = app_secret
        self.token_path = token_path
        self.stock_list_path = stock_list_path
        self.stock_info_path = stock_info_path
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self._token: str | None = None
        self._token_expires_at = 0.0

    def fetch_market(self, market: str, max_pages: int) -> list[StockCatalogRow]:
        rows: list[StockCatalogRow] = []
        cont_yn = "N"
        next_key = ""
        pages = 0

        while True:
            body, headers = self._post(
                self.stock_list_path,
                api_id=STOCK_LIST_TR,
                payload={"mrkt_tp": MARKET_CODES[market]},
                extra_headers={"cont-yn": cont_yn, "next-key": next_key},
            )
            rows.extend(parse_stock_rows(body, market))

            cont_yn = header_value(headers, "cont-yn")
            next_key = header_value(headers, "next-key")
            pages += 1
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)

            if cont_yn.upper() != "Y" or not next_key or pages >= max_pages:
                break

        print(f"{market}: fetched {len(rows)} rows from {pages} page(s)", file=sys.stderr)
        return rows

    def fetch_one(self, code: str) -> StockCatalogRow | None:
        body, _ = self._post(self.stock_info_path, api_id=STOCK_INFO_TR, payload={"stk_cd": code})
        row = parse_stock_detail(body, code)
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return row

    def _post(
        self,
        path: str,
        api_id: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        token = self._access_token()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
        }
        if extra_headers:
            headers.update(extra_headers)

        raw, response_headers = http_post_json(urljoin(self.base_url, path.lstrip("/")), headers, payload, self.timeout)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Unexpected Kiwoom response for {api_id}: {type(raw).__name__}")
        return raw, response_headers

    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token

        body, _ = http_post_json(
            urljoin(self.base_url, self.token_path.lstrip("/")),
            {"Content-Type": "application/json", "Accept": "application/json"},
            {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "secretkey": self.app_secret,
            },
            self.timeout,
        )
        if not isinstance(body, dict):
            raise RuntimeError("Unexpected Kiwoom token response")

        token = first_non_empty(body, "token", "access_token")
        if not token:
            raise RuntimeError(f"Kiwoom token response has no token field: keys={sorted(body.keys())}")

        ttl = parse_int(body.get("expires_in")) or 3600
        self._token = token
        self._token_expires_at = now + max(60, ttl - 60)
        return token


def http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> tuple[Any, dict[str, str]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace")
            return json.loads(text) if text else {}, dict(response.headers.items())
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {text[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Failed to call {url}: {exc}") from exc


def parse_stock_rows(body: dict[str, Any], market: str) -> list[StockCatalogRow]:
    rows = find_first_list(body)
    parsed: dict[str, StockCatalogRow] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        code = clean_code(first_non_empty(item, "code", "stk_cd", "isu_cd", "종목코드"))
        name = first_non_empty(item, "name", "stk_nm", "isu_nm", "종목명")
        if not code or not name:
            continue
        parsed[code] = StockCatalogRow(
            code=code,
            name=name,
            market=market,
            sector=first_non_empty(item, "sector", "upjong", "bstp_nm", "업종", "업종명"),
            market_cap=parse_int(first_non_empty(item, "mac", "market_cap", "시가총액")),
            shares_outstanding=parse_int(first_non_empty(item, "lst_stk", "shares_outstanding", "상장주식수")),
        )
    return sorted(parsed.values(), key=lambda row: row.code)


def parse_stock_detail(body: dict[str, Any], code: str) -> StockCatalogRow | None:
    name = first_non_empty(body, "stk_nm", "name", "종목명")
    if not name:
        return None
    market = normalize_market(first_non_empty(body, "mrkt_tp", "market", "시장구분"))
    return StockCatalogRow(
        code=clean_code(code),
        name=name,
        market=market,
        sector=first_non_empty(body, "sector", "upjong", "bstp_nm", "업종", "업종명"),
        market_cap=parse_int(first_non_empty(body, "mac", "market_cap", "시가총액")),
        shares_outstanding=parse_int(first_non_empty(body, "lst_stk", "shares_outstanding", "상장주식수")),
    )


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


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("-")
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return None
    number = int(digits)
    return -number if negative else number


def clean_code(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    return text


def normalize_market(value: str | None) -> str:
    if value and "KOSDAQ" in value.upper():
        return "KOSDAQ"
    return "KOSPI"


def header_value(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value.strip()
    return ""


def load_db_module():
    try:
        import psycopg  # type: ignore

        return "psycopg", psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return "psycopg2", psycopg2
    except ImportError as exc:
        raise RuntimeError('Install a PostgreSQL driver first: pip install "psycopg[binary]"') from exc


def upsert_rows(database_url: str, rows: Iterable[StockCatalogRow], dry_run: bool, batch_size: int) -> int:
    materialized = list(rows)
    if dry_run:
        for row in materialized[:10]:
            print(row, file=sys.stderr)
        return len(materialized)

    driver, db = load_db_module()
    sql = """
        INSERT INTO stocks (
            stock_code, stock_name, market_type, sector, market_cap,
            shares_outstanding, listing_status, data_source, synced_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'LISTED', 'KIWOOM', now())
        ON CONFLICT (stock_code) DO UPDATE SET
            stock_name = EXCLUDED.stock_name,
            market_type = EXCLUDED.market_type,
            sector = COALESCE(EXCLUDED.sector, stocks.sector),
            market_cap = COALESCE(EXCLUDED.market_cap, stocks.market_cap),
            shares_outstanding = COALESCE(EXCLUDED.shares_outstanding, stocks.shares_outstanding),
            listing_status = EXCLUDED.listing_status,
            data_source = EXCLUDED.data_source,
            synced_at = EXCLUDED.synced_at,
            updated_at = now()
    """
    values = [
        (row.code, row.name, row.market, row.sector, row.market_cap, row.shares_outstanding)
        for row in materialized
    ]

    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            for index in range(0, len(values), batch_size):
                chunk = values[index : index + batch_size]
                if driver == "psycopg":
                    cur.executemany(sql, chunk)
                else:
                    cur.executemany(sql, chunk)
            conn.commit()
    return len(materialized)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Kiwoom stock catalog fields into PostgreSQL.")
    parser.add_argument("--market", choices=["KOSPI", "KOSDAQ", "ALL"], default="ALL")
    parser.add_argument("--code", action="append", help="Sync one stock code. Can be repeated.")
    parser.add_argument("--codes", help="Comma/space/newline separated stock codes, e.g. 000120,005930.")
    parser.add_argument("--codes-file", help="Text file containing stock codes separated by comma/space/newline.")
    parser.add_argument(
        "--enrich-details",
        action="store_true",
        help="After market list fetch, call stock detail once per code to fill fields missing from list rows.",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--base-url", default=os.getenv("KIWOOM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token-path", default=os.getenv("KIWOOM_TOKEN_PATH", DEFAULT_TOKEN_PATH))
    parser.add_argument("--stock-list-path", default=os.getenv("KIWOOM_STOCK_LIST_PATH", DEFAULT_STOCK_LIST_PATH))
    parser.add_argument("--stock-info-path", default=os.getenv("KIWOOM_STOCK_INFO_PATH", DEFAULT_STOCK_INFO_PATH))
    parser.add_argument("--app-key", default=os.getenv("KIWOOM_APP_KEY"))
    parser.add_argument("--app-secret", default=os.getenv("KIWOOM_APP_SECRET"))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--sleep", type=float, default=0.25, help="Delay between Kiwoom calls.")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def parse_codes(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [clean_code(part) for part in re.split(r"[\s,]+", raw) if clean_code(part)]


def requested_codes(args: argparse.Namespace) -> list[str]:
    codes: list[str] = []
    for code in args.code or []:
        codes.extend(parse_codes(code))
    codes.extend(parse_codes(args.codes))
    if args.codes_file:
        with open(args.codes_file, "r", encoding="utf-8") as file:
            codes.extend(parse_codes(file.read()))
    return sorted(set(codes))


def merge_prefer_detail(base: StockCatalogRow, detail: StockCatalogRow | None) -> StockCatalogRow:
    if detail is None:
        return base
    return StockCatalogRow(
        code=base.code,
        name=detail.name or base.name,
        market=detail.market or base.market,
        sector=detail.sector or base.sector,
        market_cap=detail.market_cap if detail.market_cap is not None else base.market_cap,
        shares_outstanding=(
            detail.shares_outstanding
            if detail.shares_outstanding is not None
            else base.shares_outstanding
        ),
    )


def main() -> int:
    args = parse_args()
    if not args.app_key or not args.app_secret:
        print("KIWOOM_APP_KEY and KIWOOM_APP_SECRET are required.", file=sys.stderr)
        return 2
    if not args.database_url and not args.dry_run:
        print("DATABASE_URL is required unless --dry-run is set.", file=sys.stderr)
        return 2

    client = KiwoomClient(
        base_url=args.base_url,
        app_key=args.app_key,
        app_secret=args.app_secret,
        token_path=args.token_path,
        stock_list_path=args.stock_list_path,
        stock_info_path=args.stock_info_path,
        timeout=args.timeout,
        sleep_seconds=args.sleep,
    )

    rows: list[StockCatalogRow] = []
    codes = requested_codes(args)
    if codes:
        for code in codes:
            row = client.fetch_one(code)
            if row:
                rows.append(row)
            else:
                print(f"{code}: no Kiwoom detail row", file=sys.stderr)
    else:
        markets = ["KOSPI", "KOSDAQ"] if args.market == "ALL" else [args.market]
        for market in markets:
            rows.extend(client.fetch_market(market, args.max_pages))
        if args.enrich_details:
            enriched: list[StockCatalogRow] = []
            for index, row in enumerate(rows, start=1):
                detail = client.fetch_one(row.code)
                enriched.append(merge_prefer_detail(row, detail))
                if index % 100 == 0:
                    print(f"detail-enriched {index}/{len(rows)} rows", file=sys.stderr)
            rows = enriched

    if not rows:
        print("No rows fetched.", file=sys.stderr)
        return 1

    count = upsert_rows(args.database_url, rows, args.dry_run, args.batch_size)
    action = "Would upsert" if args.dry_run else "Upserted"
    print(f"{action} {count} stock catalog row(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
