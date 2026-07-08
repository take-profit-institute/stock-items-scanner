#!/usr/bin/env python3
"""
Backfill one daily candle date from Kiwoom REST OpenAPI directly into PostgreSQL.

This is an operational migration script for stock-service data. It avoids the
batch gRPC path and writes:
  - candles(stock_code, interval='1d', open_time=<date UTC midnight>, ...)
  - stocks.close_price from the target date close

Required env, unless KIWOOM_ACCESS_TOKEN is provided:
  KIWOOM_APP_KEY
  KIWOOM_APP_SECRET

Optional env:
  DATABASE_URL (default postgresql://candle:candle@localhost:5432/candle_stock)
  KIWOOM_ACCESS_TOKEN
  KIWOOM_BASE_URL=https://api.kiwoom.com
  KIWOOM_TOKEN_PATH=/oauth2/token
  KIWOOM_CHART_PATH=/api/dostk/chart

Example:
  DATABASE_URL='postgresql://stock:...@host:5432/candle?options=-csearch_path%3Dstock,public' \\
  KIWOOM_APP_KEY='...' KIWOOM_APP_SECRET='...' \\
  python3 scripts/migrate_daily_candles_from_kiwoom.py --date 2026-07-08
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_DATABASE_URL = "postgresql://candle:candle@localhost:5432/candle_stock"
DEFAULT_BASE_URL = "https://api.kiwoom.com"
DEFAULT_TOKEN_PATH = "/oauth2/token"
DEFAULT_CHART_PATH = "/api/dostk/chart"
DAILY_CHART_TR = "ka10081"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class DailyCandle:
    code: str
    open_time: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int


class KiwoomChartClient:
    def __init__(
        self,
        base_url: str,
        token_path: str,
        chart_path: str,
        timeout: float,
        sleep_seconds: float,
        app_key: str | None,
        app_secret: str | None,
        access_token: str | None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.token_path = token_path
        self.chart_path = chart_path
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.app_key = app_key
        self.app_secret = app_secret
        self._static_token = access_token or None
        self._token: str | None = None
        self._token_expires_at = 0.0

    def fetch_daily_candle(self, code: str, target_date: date) -> DailyCandle | None:
        body, _ = self._post(
            self.chart_path,
            api_id=DAILY_CHART_TR,
            payload={
                "stk_cd": code,
                "base_dt": target_date.strftime("%Y%m%d"),
                "upd_stkpc_tp": "1",
            },
        )
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

        target_open_time = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        for candle in parse_candles(body, code):
            if candle.open_time == target_open_time:
                return candle
        return None

    def _post(
        self,
        path: str,
        api_id: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "authorization": f"Bearer {self._access_token()}",
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

        if not self.app_key or not self.app_secret:
            raise RuntimeError("Set KIWOOM_APP_KEY/KIWOOM_APP_SECRET or KIWOOM_ACCESS_TOKEN")

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


def parse_candles(body: dict[str, Any], code: str) -> list[DailyCandle]:
    rows = find_first_list(body)
    candles: list[DailyCandle] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        open_time = parse_open_time(item)
        open_price = first_int(item, "open", "open_pric", "stck_oprc")
        high = first_int(item, "high", "high_pric", "stck_hgpr")
        low = first_int(item, "low", "low_pric", "stck_lwpr")
        close = first_int(item, "close", "cur_prc", "clos_pric", "stck_clpr")
        volume = first_int(item, "volume", "trde_qty", "acml_vol")
        if None in (open_time, open_price, high, low, close, volume):
            continue
        candles.append(
            DailyCandle(
                code=code,
                open_time=open_time,
                open=abs(open_price),
                high=abs(high),
                low=abs(low),
                close=abs(close),
                volume=abs(volume),
            )
        )
    return candles


def find_first_list(body: dict[str, Any]) -> list[Any]:
    for key in ("output", "list", "chart", "candles", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    for value in body.values():
        if isinstance(value, list):
            return value
    return []


def parse_open_time(row: dict[str, Any]) -> datetime | None:
    raw = first_non_empty(row, "date", "dt", "stck_bsop_date")
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 8:
        return None
    parsed = datetime.strptime(digits[:8], "%Y%m%d").date()
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def first_int(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = parse_int(row.get(key))
        if value is not None:
            return value
    return None


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
    text = str(value).strip().replace(",", "").replace("+", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def clean_code(value: str) -> str:
    text = value.strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    return text.zfill(6) if text.isdigit() else text


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


def fetch_target_codes(conn: Any, target_date: date, include_existing: bool, limit: int | None) -> list[str]:
    target_open_time = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    existing_filter = "" if include_existing else """
        AND NOT EXISTS (
            SELECT 1
            FROM candles c
            WHERE c.stock_code = s.stock_code
              AND c.interval = '1d'
              AND c.open_time = %s
        )
    """
    limit_sql = " LIMIT %s" if limit else ""
    params: list[Any] = []
    if not include_existing:
        params.append(target_open_time)
    if limit:
        params.append(limit)

    sql = f"""
        SELECT s.stock_code
        FROM stocks s
        WHERE s.listing_status = 'LISTED'
        {existing_filter}
        ORDER BY s.stock_code
        {limit_sql}
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [clean_code(row[0]) for row in cur.fetchall()]


def upsert_candles(conn: Any, candles: Sequence[DailyCandle], update_stock_close: bool) -> None:
    if not candles:
        return
    values = [
        (c.code, "1d", c.open_time, c.open, c.high, c.low, c.close, c.volume, True, "KIWOOM")
        for c in candles
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO candles (stock_code, interval, open_time, open, high, low, close, volume, closed, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (stock_code, interval, open_time) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                closed = EXCLUDED.closed,
                source = EXCLUDED.source,
                updated_at = now()
            """,
            values,
        )
        if update_stock_close:
            cur.executemany(
                """
                UPDATE stocks
                SET close_price = %s,
                    data_source = 'KIWOOM',
                    synced_at = now(),
                    updated_at = now()
                WHERE stock_code = %s
                """,
                [(c.close, c.code) for c in candles],
            )


def chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill one DAY_1 candle date into stock.candles.")
    parser.add_argument("--date", required=True, help="Business date to backfill, YYYY-MM-DD.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--include-existing", action="store_true", help="Re-fetch and overwrite existing candles too.")
    parser.add_argument("--no-stock-close-update", action="store_true", help="Do not update stocks.close_price.")
    parser.add_argument("--limit", type=int, help="Limit target stock codes for a small run.")
    parser.add_argument("--batch-size", type=int, default=50, help="DB commit batch size.")
    parser.add_argument("--sleep", type=float, default=0.25, help="Sleep seconds between Kiwoom requests.")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true", help="Fetch target code count only; no Kiwoom or DB writes.")
    parser.add_argument("--base-url", default=os.getenv("KIWOOM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token-path", default=os.getenv("KIWOOM_TOKEN_PATH", DEFAULT_TOKEN_PATH))
    parser.add_argument("--chart-path", default=os.getenv("KIWOOM_CHART_PATH", DEFAULT_CHART_PATH))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_date = date.fromisoformat(args.date)
    if args.batch_size < 1:
        print("--batch-size must be greater than 0", file=sys.stderr)
        return 2

    db = load_db_module()
    client = KiwoomChartClient(
        base_url=args.base_url,
        token_path=args.token_path,
        chart_path=args.chart_path,
        timeout=args.timeout,
        sleep_seconds=args.sleep,
        app_key=os.getenv("KIWOOM_APP_KEY"),
        app_secret=os.getenv("KIWOOM_APP_SECRET"),
        access_token=os.getenv("KIWOOM_ACCESS_TOKEN"),
    )

    with db.connect(args.database_url) as conn:
        codes = fetch_target_codes(conn, target_date, args.include_existing, args.limit)
        print(f"target_date={target_date} target_codes={len(codes)} include_existing={args.include_existing}")
        if args.dry_run:
            return 0

        total_upserted = 0
        total_empty = 0
        total_failed = 0
        for batch_no, code_batch in enumerate(chunks(codes, args.batch_size), start=1):
            fetched: list[DailyCandle] = []
            for code in code_batch:
                try:
                    candle = client.fetch_daily_candle(code, target_date)
                    if candle is None:
                        total_empty += 1
                    else:
                        fetched.append(candle)
                except Exception as exc:
                    total_failed += 1
                    print(f"failed code={code}: {exc}", file=sys.stderr)

            upsert_candles(conn, fetched, update_stock_close=not args.no_stock_close_update)
            conn.commit()
            total_upserted += len(fetched)
            print(
                f"batch={batch_no} requested={len(code_batch)} upserted={len(fetched)} "
                f"empty_total={total_empty} failed_total={total_failed}",
                file=sys.stderr,
            )

        print(f"done target_codes={len(codes)} upserted={total_upserted} empty={total_empty} failed={total_failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
