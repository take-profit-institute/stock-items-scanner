# Stock Items Scanner

KTB 종목코드 팝업 페이지에서 종목코드, 종목명, 시장 구분을 크롤링해서 TimescaleDB/PostgreSQL `stocks` 테이블에 넣을 SQL을 생성합니다.

## 사용법

테이블을 먼저 생성합니다.

```bash
psql "$DATABASE_URL" -f sql/001_create_stocks.sql
```

현재 KTB 페이지 기준 UPSERT SQL을 생성합니다.

```bash
.venv/bin/python scripts/crawl_ktb_stocks.py --output sql/stocks_upsert.sql
```

생성된 SQL을 DB에 반영합니다.

```bash
psql "$DATABASE_URL" -f sql/stocks_upsert.sql
```

## 키움 기준정보 보강

KTB seed에는 종목코드/종목명/시장만 있으므로, 키움 REST OpenAPI로 `sector`, `market_cap`,
`shares_outstanding`, `synced_at`을 보강합니다.

```bash
pip install "psycopg[binary]"

export DATABASE_URL="postgresql://candle:candle@localhost:5432/candle_stock"
export KIWOOM_APP_KEY="..."
export KIWOOM_APP_SECRET="..."

python3 scripts/sync_kiwoom_catalog.py --market ALL
```

단일 종목만 확인하려면 dry-run으로 먼저 봅니다.

```bash
python3 scripts/sync_kiwoom_catalog.py --code 000120 --dry-run
```

키움 API 경로가 계정 스펙과 다르면 환경변수로 바꿀 수 있습니다.

```bash
export KIWOOM_BASE_URL="https://api.kiwoom.com"
export KIWOOM_TOKEN_PATH="/oauth2/token"
export KIWOOM_STOCK_LIST_PATH="/api/dostk/stkinfo"
export KIWOOM_STOCK_INFO_PATH="/api/dostk/stkinfo"
```

`stocks`는 시계열 데이터가 아니라 종목 마스터 테이블이라 hypertable로 만들지 않습니다. TimescaleDB에서도 일반 PostgreSQL 테이블로 쓰는 것이 적합합니다.

요청받은 컬럼 구조는 MySQL 문법의 `AUTO_INCREMENT`, `COMMENT`, `ON UPDATE CURRENT_TIMESTAMP`를 사용하지만, 이 프로젝트의 SQL은 TimescaleDB/PostgreSQL 문법에 맞춰 `bigserial`, `COMMENT ON`, update trigger로 동일한 동작을 구현합니다.
