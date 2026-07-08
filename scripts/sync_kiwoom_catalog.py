#!/usr/bin/env python3
"""
Sync stock catalog enrichment fields from Kiwoom REST OpenAPI into PostgreSQL.

This script intentionally lives outside stock-service. It updates only catalog
fields that are currently missing from the seed data:
  sector_id, sector, market_cap, shares_outstanding, data_source, synced_at

Required env:
  KIWOOM_APP_KEY
  KIWOOM_APP_SECRET

Optional env:
  DATABASE_URL (default postgresql://candle:candle@localhost:5432/candle_stock)
  KIWOOM_BASE_URL=https://api.kiwoom.com
  KIWOOM_TOKEN_PATH=/oauth2/token
  KIWOOM_STOCK_LIST_PATH=/api/dostk/stkinfo
  KIWOOM_STOCK_INFO_PATH=/api/dostk/stkinfo

By default the script ensures the `stocks` table exists and is migrated to the
enriched structure (sql/002_migrate_stocks.sql) before upserting, so you can run
it against a fresh Docker Postgres with nothing but the Kiwoom keys set.
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


DEFAULT_DATABASE_URL = "postgresql://candle:candle@localhost:5432/candle_stock"
DEFAULT_MIGRATION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "002_migrate_stocks.sql"
)
DEFAULT_BASE_URL = "https://api.kiwoom.com"
DEFAULT_TOKEN_PATH = "/oauth2/token"
DEFAULT_STOCK_LIST_PATH = "/api/dostk/stkinfo"
DEFAULT_STOCK_INFO_PATH = "/api/dostk/stkinfo"
STOCK_LIST_TR = "ka10099"
STOCK_INFO_TR = "ka10001"  # 주식기본정보요청 (시총/매출/순이익/현재가/등락률 등)
MARKET_CODES = {"KOSPI": "0", "KOSDAQ": "10"}


@dataclass(frozen=True)
class StockCatalogRow:
    code: str
    name: str
    market: str | None
    sector: str | None = None
    # ka10001 주식기본정보. 금액 단위는 키움 응답 그대로 저장한다.
    market_cap: int | None = None  # mac, 억원
    shares_outstanding: int | None = None  # flo_stk, 주
    close_price: int | None = None  # cur_prc, 원 (부호 제거한 절대값)
    change_rate: float | None = None  # flu_rt, %
    revenue: int | None = None  # sale_amt, 억원 (매출액)
    operating_profit: int | None = None  # bus_pro, 억원 (영업이익)
    net_income: int | None = None  # cup_nga, 억원 (당기순이익)
    per: float | None = None
    eps: float | None = None
    roe: float | None = None
    pbr: float | None = None


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
        access_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.app_key = app_key
        self.app_secret = app_secret
        self.token_path = token_path
        self.stock_list_path = stock_list_path
        self.stock_info_path = stock_info_path
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self._static_token = access_token or None
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
        if self._static_token:
            return self._static_token

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


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


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
                wait = retry_after_seconds(exc) or backoff * (2 ** attempt)
                print(
                    f"HTTP {exc.code} from {url}; retry {attempt + 1}/{max_retries} in {wait:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                attempt += 1
                continue
            raise RuntimeError(f"HTTP {exc.code} from {url}: {text[:500]}") from exc
        except URLError as exc:
            if attempt < max_retries:
                wait = backoff * (2 ** attempt)
                print(f"Network error for {url}: {exc}; retry {attempt + 1}/{max_retries} in {wait:.1f}s", file=sys.stderr)
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
    """Parse a ka10001 (주식기본정보요청) response into a catalog row."""
    name = first_non_empty(body, "stk_nm", "name", "종목명")
    if not name:
        return None
    # ka10001 does not return the market type; leave it None so a market-list
    # value (from ka10099) is preferred when merging.
    market = normalize_market_optional(first_non_empty(body, "mrkt_tp", "market", "시장구분"))
    return StockCatalogRow(
        code=clean_code(code),
        name=name,
        market=market,
        sector=first_non_empty(body, "sector", "upjong", "bstp_nm", "업종", "업종명"),
        market_cap=parse_int(first_non_empty(body, "mac", "market_cap", "시가총액")),
        shares_outstanding=parse_int(first_non_empty(body, "flo_stk", "lst_stk", "상장주식", "상장주식수")),
        close_price=parse_price(first_non_empty(body, "cur_prc", "close_pric", "현재가", "종가")),
        change_rate=parse_float(first_non_empty(body, "flu_rt", "등락율", "등락률")),
        revenue=parse_int(first_non_empty(body, "sale_amt", "매출액")),
        operating_profit=parse_int(first_non_empty(body, "bus_pro", "영업이익")),
        net_income=parse_int(first_non_empty(body, "cup_nga", "당기순이익")),
        per=parse_float(first_non_empty(body, "per", "PER")),
        eps=parse_float(first_non_empty(body, "eps", "EPS")),
        roe=parse_float(first_non_empty(body, "roe", "ROE")),
        pbr=parse_float(first_non_empty(body, "pbr", "PBR")),
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


def parse_price(value: Any) -> int | None:
    """Kiwoom price fields carry a +/- direction sign; store the magnitude."""
    number = parse_int(value)
    return abs(number) if number is not None else None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


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


def normalize_market_optional(value: str | None) -> str | None:
    if not value:
        return None
    return normalize_market(value)


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


def ensure_schema(database_url: str, migration_file: str) -> None:
    if not os.path.exists(migration_file):
        raise RuntimeError(f"Migration file not found: {migration_file}")
    with open(migration_file, "r", encoding="utf-8") as file:
        migration_sql = file.read()

    _, db = load_db_module()
    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(migration_sql)
        conn.commit()
    print(f"Ensured stocks schema via {os.path.basename(migration_file)}", file=sys.stderr)


def upsert_sector_names(cur: Any, rows: Iterable[StockCatalogRow], batch_size: int) -> None:
    sector_names = sorted({row.sector.strip() for row in rows if row.sector and row.sector.strip()})
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


def fetch_synced_codes(database_url: str) -> set[str]:
    """Codes already enriched from Kiwoom (for --resume). Requires a real fundamental value."""
    _, db = load_db_module()
    codes: set[str] = set()
    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code FROM stocks "
                "WHERE synced_at IS NOT NULL AND (market_cap IS NOT NULL OR close_price IS NOT NULL)"
            )
            for (code,) in cur.fetchall():
                codes.add(code)
    return codes


def upsert_rows(database_url: str, rows: Iterable[StockCatalogRow], dry_run: bool, batch_size: int) -> int:
    materialized = list(rows)
    if dry_run:
        for row in materialized[:10]:
            print(row, file=sys.stderr)
        return len(materialized)

    _, db = load_db_module()

    columns = (
        "stock_code, stock_name, market_type, sector, sector_id, market_cap, shares_outstanding, "
        "close_price, change_rate, revenue, operating_profit, net_income, per, eps, roe, pbr, "
        "listing_status, data_source, synced_at"
    )
    placeholders = (
        "%s, %s, %s, %s, "
        "(SELECT sector_id FROM stock_sectors WHERE sector_name = NULLIF(btrim(%s), '')), "
        + ", ".join(["%s"] * 11)
        + ", 'LISTED', 'KIWOOM', now()"
    )
    fundamentals_update = """
            sector = COALESCE(EXCLUDED.sector, stocks.sector),
            sector_id = COALESCE(EXCLUDED.sector_id, stocks.sector_id),
            market_cap = COALESCE(EXCLUDED.market_cap, stocks.market_cap),
            shares_outstanding = COALESCE(EXCLUDED.shares_outstanding, stocks.shares_outstanding),
            close_price = COALESCE(EXCLUDED.close_price, stocks.close_price),
            change_rate = COALESCE(EXCLUDED.change_rate, stocks.change_rate),
            revenue = COALESCE(EXCLUDED.revenue, stocks.revenue),
            operating_profit = COALESCE(EXCLUDED.operating_profit, stocks.operating_profit),
            net_income = COALESCE(EXCLUDED.net_income, stocks.net_income),
            per = COALESCE(EXCLUDED.per, stocks.per),
            eps = COALESCE(EXCLUDED.eps, stocks.eps),
            roe = COALESCE(EXCLUDED.roe, stocks.roe),
            pbr = COALESCE(EXCLUDED.pbr, stocks.pbr),
            listing_status = EXCLUDED.listing_status,
            data_source = EXCLUDED.data_source,
            synced_at = EXCLUDED.synced_at,
            updated_at = now()
    """
    # Rows that know their market can update market_type; rows without a market
    # (e.g. single --code via ka10001) keep the existing market_type on conflict
    # and only fall back to 'KOSPI' for a brand-new insert.
    sql_with_market = f"""
        INSERT INTO stocks ({columns})
        VALUES ({placeholders})
        ON CONFLICT (stock_code) DO UPDATE SET
            stock_name = EXCLUDED.stock_name,
            market_type = EXCLUDED.market_type,
            {fundamentals_update}
    """
    sql_without_market = f"""
        INSERT INTO stocks ({columns})
        VALUES ({placeholders})
        ON CONFLICT (stock_code) DO UPDATE SET
            stock_name = EXCLUDED.stock_name,
            {fundamentals_update}
    """

    def to_values(row: StockCatalogRow, market: str) -> tuple[Any, ...]:
        return (
            row.code, row.name, market, row.sector, row.sector, row.market_cap, row.shares_outstanding,
            row.close_price, row.change_rate, row.revenue, row.operating_profit, row.net_income,
            row.per, row.eps, row.roe, row.pbr,
        )

    with_market = [to_values(row, row.market) for row in materialized if row.market]
    without_market = [to_values(row, "KOSPI") for row in materialized if not row.market]
    if without_market:
        print(
            f"{len(without_market)} row(s) had no market from Kiwoom; keeping existing "
            "market_type, new inserts default to KOSPI.",
            file=sys.stderr,
        )

    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            upsert_sector_names(cur, materialized, batch_size)
            for sql, values in ((sql_with_market, with_market), (sql_without_market, without_market)):
                for index in range(0, len(values), batch_size):
                    cur.executemany(sql, values[index : index + batch_size])
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
        help="After market list fetch, call ka10001 once per code to fill fundamentals. Upserts incrementally per batch.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="With --enrich-details, skip codes already enriched from Kiwoom so a crashed run can continue.",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument(
        "--no-migrate",
        dest="migrate",
        action="store_false",
        help="Skip ensuring/migrating the stocks table before upsert.",
    )
    parser.add_argument("--migration-file", default=DEFAULT_MIGRATION_FILE)
    parser.add_argument("--base-url", default=os.getenv("KIWOOM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token-path", default=os.getenv("KIWOOM_TOKEN_PATH", DEFAULT_TOKEN_PATH))
    parser.add_argument("--stock-list-path", default=os.getenv("KIWOOM_STOCK_LIST_PATH", DEFAULT_STOCK_LIST_PATH))
    parser.add_argument("--stock-info-path", default=os.getenv("KIWOOM_STOCK_INFO_PATH", DEFAULT_STOCK_INFO_PATH))
    parser.add_argument("--app-key", default=os.getenv("KIWOOM_APP_KEY"))
    parser.add_argument("--app-secret", default=os.getenv("KIWOOM_APP_SECRET"))
    parser.add_argument(
        "--access-token",
        default=os.getenv("KIWOOM_ACCESS_TOKEN"),
        help="Use a pre-issued Kiwoom access token instead of requesting one with app key/secret.",
    )
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

    def prefer(attr: str) -> Any:
        detail_value = getattr(detail, attr)
        return detail_value if detail_value is not None else getattr(base, attr)

    return StockCatalogRow(
        code=base.code,
        name=detail.name or base.name,
        # detail (ka10001) has no market; keep the market-list value.
        market=detail.market or base.market,
        sector=detail.sector or base.sector,
        market_cap=prefer("market_cap"),
        shares_outstanding=prefer("shares_outstanding"),
        close_price=prefer("close_price"),
        change_rate=prefer("change_rate"),
        revenue=prefer("revenue"),
        operating_profit=prefer("operating_profit"),
        net_income=prefer("net_income"),
        per=prefer("per"),
        eps=prefer("eps"),
        roe=prefer("roe"),
        pbr=prefer("pbr"),
    )


def report_upsert(count: int, dry_run: bool) -> None:
    action = "Would upsert" if dry_run else "Upserted"
    print(f"{action} {count} stock catalog row(s).", file=sys.stderr)


def main() -> int:
    args = parse_args()
    if not args.access_token and (not args.app_key or not args.app_secret):
        print(
            "Provide either KIWOOM_ACCESS_TOKEN (--access-token) or both "
            "KIWOOM_APP_KEY and KIWOOM_APP_SECRET.",
            file=sys.stderr,
        )
        return 2
    if not args.database_url and not args.dry_run:
        print("DATABASE_URL is required unless --dry-run is set.", file=sys.stderr)
        return 2

    if args.migrate and not args.dry_run:
        ensure_schema(args.database_url, args.migration_file)

    client = KiwoomClient(
        base_url=args.base_url,
        app_key=args.app_key,
        app_secret=args.app_secret,
        token_path=args.token_path,
        stock_list_path=args.stock_list_path,
        stock_info_path=args.stock_info_path,
        timeout=args.timeout,
        sleep_seconds=args.sleep,
        access_token=args.access_token,
    )

    codes = requested_codes(args)
    if codes:
        rows: list[StockCatalogRow] = []
        for code in codes:
            row = client.fetch_one(code)
            if row:
                rows.append(row)
            else:
                print(f"{code}: no Kiwoom detail row", file=sys.stderr)
        if not rows:
            print("No rows fetched.", file=sys.stderr)
            return 1
        count = upsert_rows(args.database_url, rows, args.dry_run, args.batch_size)
        report_upsert(count, args.dry_run)
        return 0

    markets = ["KOSPI", "KOSDAQ"] if args.market == "ALL" else [args.market]
    listed: list[StockCatalogRow] = []
    for market in markets:
        listed.extend(client.fetch_market(market, args.max_pages))
    if not listed:
        print("No rows fetched.", file=sys.stderr)
        return 1

    if not args.enrich_details:
        count = upsert_rows(args.database_url, listed, args.dry_run, args.batch_size)
        report_upsert(count, args.dry_run)
        return 0

    # Enrich per code via ka10001, upserting each batch so progress survives a crash.
    skip: set[str] = set()
    if args.resume and not args.dry_run:
        skip = fetch_synced_codes(args.database_url)
        print(f"resume: skipping {len(skip)} already-enriched code(s)", file=sys.stderr)

    buffer: list[StockCatalogRow] = []
    total = 0
    pending = [row for row in listed if row.code not in skip]
    for index, row in enumerate(pending, start=1):
        buffer.append(merge_prefer_detail(row, client.fetch_one(row.code)))
        if len(buffer) >= args.batch_size:
            total += upsert_rows(args.database_url, buffer, args.dry_run, args.batch_size)
            buffer = []
            print(f"enriched {index}/{len(pending)} (upserted {total})", file=sys.stderr)
        elif index % 100 == 0:
            print(f"enriched {index}/{len(pending)}", file=sys.stderr)
    if buffer:
        total += upsert_rows(args.database_url, buffer, args.dry_run, args.batch_size)

    report_upsert(total, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
