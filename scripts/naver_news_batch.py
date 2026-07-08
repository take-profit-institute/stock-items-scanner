#!/usr/bin/env python3
"""stocks 테이블을 순회하며 종목별 네이버 뉴스를 페치해 하나의 적재 SQL로 취합한다.

단일 종목 페치/변환은 naver_news_fetch.py 를 그대로 재사용한다(같은 정제·SQL 로직).
이 스크립트가 추가하는 것은 (1) stocks 테이블 순회, (2) 종목 간 딜레이,
(3) 429/5xx 레이트리밋 백오프 재시도, (4) 종목별 SQL 취합이다.

출력은 news.articles / news.article_stock_mappings 적재용 SQL(각 종목당 BEGIN..COMMIT)
이며, ON CONFLICT DO NOTHING 이라 여러 번 돌려도 안전하다.

주의: SOURCE(stocks) DB 와 TARGET(news) DB 는 다를 수 있다. 이 스크립트는 SOURCE 에서
종목만 읽고, 생성된 SQL 은 TARGET(news-service) DB 에 직접 psql 로 넣어야 한다:

    export NAVER_NEWS_CLIENT_ID=... NAVER_NEWS_CLIENT_SECRET=...
    # 종목은 candle_stock 에서 읽고 결과 SQL 을 파일로
    python scripts/naver_news_batch.py \
        --database-url postgresql://candle:candle@localhost:5432/candle_stock \
        --market KOSPI KOSDAQ --max 10 --output sql/news_articles.sql

    # 생성된 SQL 을 news-service DB(포트포워딩한 접속 문자열)에 반영
    psql "$NEWS_DATABASE_URL" -f sql/news_articles.sql

건수 확인용(소수 종목만):
    python scripts/naver_news_batch.py --limit 5 --max 5 --output -
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Optional

# 같은 scripts/ 디렉터리의 단일 종목 모듈 재사용
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from naver_news_fetch import (  # noqa: E402
    DEFAULT_BASE_URL,
    MAX_START,
    NaverNewsApiError,
    NaverNewsItem,
    fetch,
    to_sql,
)

DEFAULT_DATABASE_URL = "postgresql://candle:candle@localhost:5432/candle_stock"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def load_db_module():
    """psycopg(3) 우선, 없으면 psycopg2. (기존 스크립트들과 동일 규약)"""
    try:
        import psycopg  # type: ignore

        return psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return psycopg2
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError(
            'Install a PostgreSQL driver first: pip install "psycopg[binary]"'
        ) from exc


def load_stocks(
    database_url: str,
    *,
    markets: Optional[list[str]] = None,
    codes: Optional[list[str]] = None,
    include_delisted: bool = False,
    limit: Optional[int] = None,
    schema: Optional[str] = None,
) -> list[tuple[str, str]]:
    """stocks 테이블에서 (stock_code, stock_name) 목록을 stock_code 순으로 읽는다.

    RDS(candle DB)는 서비스별 schema 분리라 stocks 는 `stock` schema 에 있다.
    schema 인자로 search_path 를 지정하면 unqualified `stocks` 가 해석된다.
    """
    db = load_db_module()

    where: list[str] = []
    params: list[object] = []
    if markets:
        where.append("market_type = ANY(%s)")
        params.append([m.upper() for m in markets])
    if codes:
        where.append("stock_code = ANY(%s)")
        params.append(codes)
    if not include_delisted:
        # listing_status 가 NULL 인 구행도 포함(초기 seed 는 status 미설정일 수 있음)
        where.append("(listing_status = 'LISTED' OR listing_status IS NULL)")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f" LIMIT {int(limit)}" if limit else ""
    sql = (
        "SELECT stock_code, stock_name FROM stocks"
        f"{where_sql} ORDER BY stock_code{limit_sql}"
    )

    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            if schema:
                # search_path 는 파라미터 바인딩이 안 돼 식별자 화이트리스트로 검증 후 삽입
                parts = [p.strip() for p in schema.split(",") if p.strip()]
                if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts):
                    raise ValueError(f"invalid schema name in --source-schema: {schema!r}")
                cur.execute("SET search_path TO " + ", ".join(parts))
            cur.execute(sql, params)
            rows = cur.fetchall()

    # 이름이 비어있는 종목은 검색어를 만들 수 없어 제외
    return [(str(code), str(name)) for code, name in rows if name and str(name).strip()]


def fetch_with_retry(
    query: str,
    *,
    client_id: str,
    client_secret: str,
    base_url: str,
    max_results: int,
    sort: str,
    timeout: float,
    max_retries: int,
    backoff_base: float,
) -> list[NaverNewsItem]:
    """fetch() 를 감싸 429/5xx 시 지수 백오프로 종목 단위 재시도한다."""
    attempt = 0
    while True:
        try:
            return fetch(
                query,
                client_id=client_id,
                client_secret=client_secret,
                base_url=base_url,
                max_results=max_results,
                sort=sort,
                timeout=timeout,
            )
        except NaverNewsApiError as e:
            retryable = e.status in RETRYABLE_STATUS or e.status is None
            if not retryable or attempt >= max_retries:
                raise
            wait = backoff_base * (2 ** attempt)
            attempt += 1
            print(
                f"    [retry {attempt}/{max_retries}] {query!r} "
                f"(HTTP {e.status}) → {wait:.1f}s 대기",
                file=sys.stderr,
            )
            time.sleep(wait)


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="stocks 테이블을 순회하며 종목별 네이버 뉴스 적재 SQL 을 취합한다."
    )
    parser.add_argument(
        "--database-url", default=_env("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="종목을 읽을 SOURCE DB (stocks 테이블). 기본 candle_stock",
    )
    parser.add_argument(
        "--output", default="-",
        help="취합 SQL 출력 경로. '-' 면 stdout (기본)",
    )
    parser.add_argument(
        "--market", nargs="*", default=None, metavar="MARKET",
        help="시장 필터 (예: KOSPI KOSDAQ). 미지정 시 전체",
    )
    parser.add_argument(
        "--codes", nargs="*", default=None, metavar="CODE",
        help="특정 종목코드만 (stocks 에서 이름 조회). 미지정 시 전체",
    )
    parser.add_argument(
        "--source-schema", default="stock,public",
        help="stocks 를 읽을 search_path (RDS candle DB는 'stock,public'). "
             "로컬 candle_stock(public)이면 'public'",
    )
    parser.add_argument(
        "--include-delisted", action="store_true",
        help="상장폐지/거래정지 종목도 포함",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="순회할 종목 수 상한 (테스트용)",
    )
    parser.add_argument(
        "--max", type=int, default=10,
        help=f"종목당 뉴스 건수 (최대 {MAX_START})",
    )
    parser.add_argument("--sort", default="date", choices=["date", "sim"], help="정렬 기준")
    parser.add_argument(
        "--query-template", default="{name}",
        help="검색어 템플릿. {name}/{code} 치환. 기본 '{name}'",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="요청 타임아웃(초)")
    parser.add_argument(
        "--stock-sleep", type=float, default=0.3,
        help="종목 사이 대기(초) — 레이트리밋 완화",
    )
    parser.add_argument("--max-retries", type=int, default=4, help="429/5xx 재시도 횟수")
    parser.add_argument(
        "--backoff-base", type=float, default=1.0,
        help="재시도 지수 백오프 기준(초): base*2^n",
    )
    args = parser.parse_args(argv)

    client_id = _env("NAVER_NEWS_CLIENT_ID")
    client_secret = _env("NAVER_NEWS_CLIENT_SECRET")
    base_url = _env("NAVER_NEWS_BASE_URL", DEFAULT_BASE_URL)
    if not client_id or not client_secret:
        print(
            "환경변수 NAVER_NEWS_CLIENT_ID / NAVER_NEWS_CLIENT_SECRET 가 필요합니다.",
            file=sys.stderr,
        )
        return 2

    try:
        stocks = load_stocks(
            args.database_url,
            markets=args.market,
            codes=args.codes,
            include_delisted=args.include_delisted,
            limit=args.limit,
            schema=args.source_schema,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[error] stocks 조회 실패: {e}", file=sys.stderr)
        return 1

    if not stocks:
        print("[warn] 조건에 맞는 종목이 없습니다.", file=sys.stderr)
        return 0

    out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8")
    total_stocks = len(stocks)
    total_items = 0
    failed: list[tuple[str, str]] = []

    try:
        out.write(
            f"-- Generated by naver_news_batch.py — stocks={total_stocks} "
            f"max/stock={args.max} sort={args.sort}\n\n"
        )
        for idx, (code, name) in enumerate(stocks, start=1):
            query = args.query_template.format(name=name, code=code)
            print(f"[{idx}/{total_stocks}] {code} {name} …", file=sys.stderr, flush=True)
            try:
                items = fetch_with_retry(
                    query,
                    client_id=client_id,
                    client_secret=client_secret,
                    base_url=base_url,
                    max_results=args.max,
                    sort=args.sort,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                    backoff_base=args.backoff_base,
                )
            except NaverNewsApiError as e:
                print(f"    [skip] {code} {name}: {e}", file=sys.stderr)
                failed.append((code, name))
                continue

            if not items:
                continue

            out.write(to_sql(items, query=query, stock_code=code, matched_keyword=name))
            out.write("\n")
            total_items += len(items)

            if args.stock_sleep > 0 and idx < total_stocks:
                time.sleep(args.stock_sleep)
    finally:
        if out is not sys.stdout:
            out.close()

    print(
        f"\n완료: 종목 {total_stocks - len(failed)}/{total_stocks} 성공, "
        f"기사 {total_items}건 취합"
        + (f", 실패 {len(failed)}종목" if failed else ""),
        file=sys.stderr,
    )
    if failed:
        print(
            "실패 종목: " + ", ".join(f"{c}({n})" for c, n in failed[:20])
            + (" …" if len(failed) > 20 else ""),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
