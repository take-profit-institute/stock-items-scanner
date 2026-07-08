#!/usr/bin/env python3
"""네이버 검색 API(뉴스)로 뉴스 데이터를 가져오는 스크립트.

Java 쪽 RestNaverNewsClient / NaverNewsMapper 와 동일한 엔드포인트, 헤더,
파라미터, 응답 정제 로직을 따른다.

환경변수 (news-service 의 것과 동일):
    NAVER_NEWS_CLIENT_ID       필수
    NAVER_NEWS_CLIENT_SECRET   필수
    NAVER_NEWS_BASE_URL        기본값 https://openapi.naver.com

사용 예시:
    export NAVER_NEWS_CLIENT_ID=OsShZpD0JTQ2ncfUtQkf
    export NAVER_NEWS_CLIENT_SECRET=_Xg5MMTaYq
    python naver_news_fetch.py "삼성전자" --display 20 --sort date

    # 여러 페이지를 한 번에 (최대 1000건까지 자동 페이지네이션)
    python naver_news_fetch.py "코스피" --max 100 --format jsonl > news.jsonl
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

SEARCH_NEWS_PATH = "/v1/search/news.json"
CLIENT_ID_HEADER = "X-Naver-Client-Id"
CLIENT_SECRET_HEADER = "X-Naver-Client-Secret"
DEFAULT_BASE_URL = "https://openapi.naver.com"

# 네이버 검색 API 제약 (Java NaverNewsSearchRequest 와 동일)
MAX_DISPLAY = 100
MAX_START = 1000

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


@dataclass
class NaverNewsItem:
    title: str
    original_link: Optional[str]
    link: Optional[str]
    description: str
    published_at: Optional[str]  # ISO-8601 (UTC)
    source: Optional[str]  # 기사 원문 호스트 (news.articles.source 용)


class NaverNewsApiError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _clean(value: Optional[str]) -> Optional[str]:
    """Java NaverNewsMapper.clean 과 동일: 태그 제거 + HTML 엔티티 디코딩 + 공백 정규화."""
    if value is None:
        return None
    text = _TAG_RE.sub("", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _host(url: Optional[str]) -> Optional[str]:
    """URL 에서 호스트를 뽑아 news.articles.source 값으로 사용한다."""
    if not url:
        return None
    host = urlsplit(url).netloc
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def _parse_published_at(value: Optional[str]) -> Optional[str]:
    """RFC 1123 형식(pubDate)을 ISO-8601(UTC) 문자열로 변환. 실패 시 None."""
    if not value or not value.strip():
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_sort(value: Optional[str]) -> str:
    if not value or not value.strip():
        return "date"
    normalized = value.strip().lower()
    if normalized not in ("sim", "date"):
        raise ValueError("sort must be sim or date")
    return normalized


def search_once(
    query: str,
    *,
    client_id: str,
    client_secret: str,
    base_url: str = DEFAULT_BASE_URL,
    display: int = 10,
    start: int = 1,
    sort: str = "date",
    timeout: float = 5.0,
) -> dict:
    """네이버 뉴스 검색 API 를 1회 호출하고 원시 JSON(dict)을 반환한다."""
    if not query or not query.strip():
        raise ValueError("query is required")
    if not (1 <= display <= MAX_DISPLAY):
        raise ValueError(f"display must be between 1 and {MAX_DISPLAY}")
    if not (1 <= start <= MAX_START):
        raise ValueError(f"start must be between 1 and {MAX_START}")

    params = {
        "query": query.strip(),
        "display": display,
        "start": start,
        "sort": _normalize_sort(sort),
    }
    url = f"{base_url.rstrip('/')}{SEARCH_NEWS_PATH}?{urlencode(params)}"
    req = Request(url, method="GET")
    req.add_header(CLIENT_ID_HEADER, client_id)
    req.add_header(CLIENT_SECRET_HEADER, client_secret)

    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise NaverNewsApiError(
            f"Naver API request failed (HTTP {e.code})", status=e.code, body=body
        ) from e
    except URLError as e:
        raise NaverNewsApiError(f"Naver API request failed: {e.reason}") from e


def _to_items(raw: dict) -> list[NaverNewsItem]:
    items = raw.get("items") or []
    result = []
    for it in items:
        original_link = _clean(it.get("originallink"))
        link = _clean(it.get("link"))
        result.append(
            NaverNewsItem(
                title=_clean(it.get("title")),
                original_link=original_link,
                link=link,
                description=_clean(it.get("description")),
                published_at=_parse_published_at(it.get("pubDate")),
                source=_host(original_link or link),
            )
        )
    return result


def fetch(
    query: str,
    *,
    client_id: str,
    client_secret: str,
    base_url: str = DEFAULT_BASE_URL,
    max_results: int = 10,
    sort: str = "date",
    timeout: float = 5.0,
    pause: float = 0.1,
) -> list[NaverNewsItem]:
    """max_results 만큼 정제된 뉴스 아이템을 페이지네이션으로 수집한다 (최대 1000건)."""
    max_results = min(max_results, MAX_START)
    collected: list[NaverNewsItem] = []
    start = 1
    total = None

    while len(collected) < max_results and start <= MAX_START:
        display = min(MAX_DISPLAY, max_results - len(collected))
        raw = search_once(
            query,
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            display=display,
            start=start,
            sort=sort,
            timeout=timeout,
        )
        if total is None:
            total = raw.get("total", 0)
        batch = _to_items(raw)
        if not batch:
            break
        collected.extend(batch)
        start += display
        if start <= MAX_START and len(collected) < max_results:
            time.sleep(pause)

    return collected[:max_results]


def _sql_str(value: Optional[str]) -> str:
    """문자열을 PostgreSQL 리터럴로 변환. None 이면 NULL."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _sql_ts(value: Optional[str]) -> str:
    """ISO-8601 문자열을 TIMESTAMPTZ 리터럴로. None 이면 NULL."""
    if value is None:
        return "NULL"
    return "TIMESTAMPTZ '" + value.replace("'", "''") + "'"


def to_sql(
    items: list[NaverNewsItem],
    *,
    query: str,
    stock_code: Optional[str] = None,
    matched_keyword: Optional[str] = None,
) -> str:
    """news.articles (옵션으로 news.article_stock_mappings) 적재용 SQL 문자열 생성.

    - url 은 originallink(없으면 link)를 사용하며 uq_articles_url 로 중복 무시.
    - stock_code 지정 시, url 로 article 을 찾아 매핑 행을 함께 upsert.
    """
    keyword = matched_keyword if matched_keyword is not None else query
    lines: list[str] = [
        "-- Generated by naver_news_fetch.py",
        f"-- query={query!r}  count={len(items)}  "
        f"generated_at={datetime.now(timezone.utc).isoformat()}",
        "BEGIN;",
        "",
    ]

    for item in items:
        url = item.original_link or item.link
        if not url or not item.title:
            continue  # url/title 은 NOT NULL 이라 없는 행은 스킵

        lines.append(
            "INSERT INTO news.articles "
            "(title, content_summary, url, source, published_at) VALUES ("
        )
        lines.append(f"    {_sql_str(item.title)},")
        lines.append(f"    {_sql_str(item.description)},")
        lines.append(f"    {_sql_str(url)},")
        lines.append(f"    {_sql_str(item.source)},")
        lines.append(f"    {_sql_ts(item.published_at)}")
        lines.append(") ON CONFLICT (url) DO NOTHING;")

        if stock_code:
            lines.append(
                "INSERT INTO news.article_stock_mappings "
                "(article_id, stock_code, matched_keyword)"
            )
            lines.append(
                f"    SELECT a.id, {_sql_str(stock_code)}, {_sql_str(keyword)} "
                f"FROM news.articles a WHERE a.url = {_sql_str(url)}"
            )
            lines.append(
                "    ON CONFLICT (article_id, stock_code) DO NOTHING;"
            )
        lines.append("")

    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="네이버 검색 API 로 뉴스 데이터를 가져온다."
    )
    parser.add_argument("query", help="검색어")
    parser.add_argument("--max", type=int, default=10, help="가져올 총 건수 (최대 1000)")
    parser.add_argument(
        "--display", type=int, default=None,
        help="단일 호출 건수(1-100). 지정 시 페이지네이션 없이 1회만 호출",
    )
    parser.add_argument("--start", type=int, default=1, help="검색 시작 위치(1-1000)")
    parser.add_argument("--sort", default="date", choices=["date", "sim"], help="정렬 기준")
    parser.add_argument(
        "--format", default="json", choices=["json", "jsonl", "sql"],
        help="출력 형식 (sql: news.articles 적재용 INSERT)",
    )
    parser.add_argument(
        "--stock-code", default=None,
        help="지정 시 news.article_stock_mappings 매핑 SQL 도 함께 생성 (--format sql)",
    )
    parser.add_argument(
        "--keyword", default=None,
        help="매핑의 matched_keyword (기본값: 검색어)",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="요청 타임아웃(초)")
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
        if args.display is not None:
            raw = search_once(
                args.query,
                client_id=client_id,
                client_secret=client_secret,
                base_url=base_url,
                display=args.display,
                start=args.start,
                sort=args.sort,
                timeout=args.timeout,
            )
            items = _to_items(raw)
        else:
            items = fetch(
                args.query,
                client_id=client_id,
                client_secret=client_secret,
                base_url=base_url,
                max_results=args.max,
                sort=args.sort,
                timeout=args.timeout,
            )
    except NaverNewsApiError as e:
        print(f"[error] {e}", file=sys.stderr)
        if e.body:
            print(e.body[:300], file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    if args.format == "sql":
        sys.stdout.write(
            to_sql(
                items,
                query=args.query,
                stock_code=args.stock_code,
                matched_keyword=args.keyword,
            )
        )
    elif args.format == "jsonl":
        for item in items:
            print(json.dumps(asdict(item), ensure_ascii=False))
    else:
        payload = {
            "query": args.query,
            "count": len(items),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "items": [asdict(item) for item in items],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
