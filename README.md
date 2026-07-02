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

## 키움 기준정보 보강 (자동 upsert)

KTB seed에는 종목코드/종목명/시장만 있으므로, 키움 REST OpenAPI로 재무/시세를 보강합니다.

- 종목 리스트: `ka10099` (종목코드/종목명/시장)
- 종목 상세: `ka10001` 주식기본정보요청 → `market_cap`(시총, 억원), `shares_outstanding`(상장주식수),
  `close_price`(현재가/종가), `change_rate`(등락률), `revenue`(매출액, 억원),
  `operating_profit`(영업이익, 억원), `net_income`(당기순이익, 억원), `per/eps/roe/pbr`

> 상세 재무값은 종목별 `ka10001` 호출로만 채워집니다. 전체 종목에 채우려면 `--enrich-details`를
> 붙여야 하고(종목당 1콜), 리스트만 돌리면 코드/이름/시장만 upsert됩니다.

Postgres는 도커 기본 포트(5432), 계정 `candle` / `candle`을 가정합니다.

```bash
docker run -d --name candle-pg -p 5432:5432 \
  -e POSTGRES_USER=candle -e POSTGRES_PASSWORD=candle -e POSTGRES_DB=candle_stock \
  postgres:16
```

키움 키만 넣으면 나머지는 스크립트가 알아서 합니다. `DATABASE_URL`을 안 주면
`postgresql://candle:candle@localhost:5432/candle_stock`을 사용하고, upsert 전에
`sql/002_migrate_stocks.sql`을 실행해 `stocks` 테이블을 (없으면 생성, seed 구조면 확장) 자동
마이그레이션합니다.

```bash
pip install "psycopg[binary]"

export KIWOOM_APP_KEY="..."
export KIWOOM_APP_SECRET="..."

# 코드/이름/시장만 (빠름)
python3 scripts/sync_kiwoom_catalog.py --market ALL

# 재무/시세까지 (종목당 ka10001 1콜, 시간 걸림)
python3 scripts/sync_kiwoom_catalog.py --market ALL --enrich-details
```

이미 `stocks` 테이블 구조가 확장돼 있다면 마이그레이션을 건너뛸 수 있습니다.

```bash
python3 scripts/sync_kiwoom_catalog.py --market ALL --no-migrate
```

마이그레이션만 따로 돌리고 싶으면 SQL을 직접 실행해도 됩니다. (여러 번 실행해도 안전)

```bash
psql "postgresql://candle:candle@localhost:5432/candle_stock" -f sql/002_migrate_stocks.sql
```

단일 종목만 확인하려면 dry-run으로 먼저 봅니다. (DB 접속 없이 파싱 결과만 출력)

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
