-- Idempotent migration: bring an existing `stocks` table up to the enriched
-- catalog structure used by the Kiwoom sync (sector_id, sector, market_cap,
-- shares_outstanding, data_source, synced_at, ...).
--
-- Safe to run whether the table is:
--   * missing entirely,
--   * a minimal seed table (stock_code / stock_name / market_type only), or
--   * already fully migrated.
--
-- Running it repeatedly is a no-op.

BEGIN;

-- 1) Minimal tables if nothing exists yet.
CREATE TABLE IF NOT EXISTS stock_sectors (
    sector_id bigserial PRIMARY KEY,
    sector_name varchar(100) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stocks (
    stock_id bigserial PRIMARY KEY,
    stock_code varchar(6) NOT NULL,
    stock_name varchar(100) NOT NULL,
    market_type varchar(20) NOT NULL
);

-- 2) Enrichment columns (added only if missing).
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS sector_id bigint;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS sector varchar(50);
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS listing_status varchar(20) NOT NULL DEFAULT 'LISTED';
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS market_cap bigint;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS shares_outstanding bigint;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS listed_at date;
-- 키움 ka10001(주식기본정보) 보강 컬럼
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS close_price bigint;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS change_rate numeric(8, 2);
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS revenue bigint;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS operating_profit bigint;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS net_income bigint;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS per numeric(12, 2);
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS eps numeric(18, 2);
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS roe numeric(12, 2);
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS pbr numeric(12, 2);
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS data_source varchar(20) NOT NULL DEFAULT 'SEED';
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS synced_at timestamptz;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- 3) Unique key on stock_code so ON CONFLICT (stock_code) works.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stocks_stock_code ON stocks (stock_code);

-- 4) CHECK constraints (added only if not already present).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'stocks'::regclass AND conname = 'stocks_market_type_check'
    ) THEN
        ALTER TABLE stocks
            ADD CONSTRAINT stocks_market_type_check CHECK (market_type IN ('KOSPI', 'KOSDAQ'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'stocks'::regclass AND conname = 'stocks_listing_status_check'
    ) THEN
        ALTER TABLE stocks
            ADD CONSTRAINT stocks_listing_status_check CHECK (listing_status IN ('LISTED', 'DELISTED', 'SUSPENDED'));
    END IF;
END $$;

-- 5) Normalize existing sector strings into stock_sectors.
INSERT INTO stock_sectors (sector_name)
SELECT DISTINCT btrim(sector)
FROM stocks
WHERE sector IS NOT NULL
  AND btrim(sector) <> ''
ON CONFLICT (sector_name) DO NOTHING;

UPDATE stocks AS s
SET sector_id = ss.sector_id
FROM stock_sectors AS ss
WHERE s.sector_id IS NULL
  AND s.sector IS NOT NULL
  AND btrim(s.sector) = ss.sector_name;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'stocks'::regclass
          AND conname = 'stocks_sector_id_fkey'
    ) THEN
        ALTER TABLE stocks
            ADD CONSTRAINT stocks_sector_id_fkey
            FOREIGN KEY (sector_id) REFERENCES stock_sectors (sector_id);
    END IF;
END $$;

-- 6) Column comments.
COMMENT ON TABLE stock_sectors IS '업종 마스터';
COMMENT ON COLUMN stock_sectors.sector_id IS '업종 ID';
COMMENT ON COLUMN stock_sectors.sector_name IS '업종명';
COMMENT ON COLUMN stock_sectors.created_at IS '생성일시';
COMMENT ON COLUMN stock_sectors.updated_at IS '수정일시';
COMMENT ON TABLE stocks IS '종목 마스터';
COMMENT ON COLUMN stocks.stock_id IS '종목 ID';
COMMENT ON COLUMN stocks.stock_code IS '종목코드';
COMMENT ON COLUMN stocks.stock_name IS '종목명';
COMMENT ON COLUMN stocks.market_type IS '시장구분(KOSPI/KOSDAQ)';
COMMENT ON COLUMN stocks.sector_id IS '업종 ID';
COMMENT ON COLUMN stocks.sector IS '업종명(전환 호환용)';
COMMENT ON COLUMN stocks.listing_status IS '상장상태(LISTED/DELISTED/SUSPENDED)';
COMMENT ON COLUMN stocks.market_cap IS '시가총액(억원, 키움 mac)';
COMMENT ON COLUMN stocks.shares_outstanding IS '상장주식수(주, 키움 flo_stk)';
COMMENT ON COLUMN stocks.listed_at IS '상장일';
COMMENT ON COLUMN stocks.close_price IS '현재가/종가(원, 키움 cur_prc)';
COMMENT ON COLUMN stocks.change_rate IS '등락률(%, 키움 flu_rt)';
COMMENT ON COLUMN stocks.revenue IS '매출액(억원, 키움 sale_amt)';
COMMENT ON COLUMN stocks.operating_profit IS '영업이익(억원, 키움 bus_pro)';
COMMENT ON COLUMN stocks.net_income IS '당기순이익(억원, 키움 cup_nga)';
COMMENT ON COLUMN stocks.per IS 'PER(키움 per)';
COMMENT ON COLUMN stocks.eps IS 'EPS(키움 eps)';
COMMENT ON COLUMN stocks.roe IS 'ROE(%, 키움 roe)';
COMMENT ON COLUMN stocks.pbr IS 'PBR(키움 pbr)';
COMMENT ON COLUMN stocks.data_source IS '데이터 출처(SEED/KIWOOM/BATCH)';
COMMENT ON COLUMN stocks.synced_at IS '마지막 키움 동기화 시각';
COMMENT ON COLUMN stocks.created_at IS '생성일시';
COMMENT ON COLUMN stocks.updated_at IS '수정일시';

-- 7) Indexes.
CREATE INDEX IF NOT EXISTS idx_stocks_market_type ON stocks (market_type);
CREATE INDEX IF NOT EXISTS idx_stocks_sector_id ON stocks (sector_id);
CREATE INDEX IF NOT EXISTS idx_stocks_sector ON stocks (sector);
CREATE INDEX IF NOT EXISTS idx_stocks_listing_status ON stocks (listing_status);
CREATE INDEX IF NOT EXISTS idx_stocks_market_cap ON stocks (market_cap DESC);

-- 8) updated_at auto-touch triggers.
CREATE OR REPLACE FUNCTION set_stock_sectors_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION set_stocks_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stock_sectors_updated_at ON stock_sectors;
DROP TRIGGER IF EXISTS trg_stocks_updated_at ON stocks;

CREATE TRIGGER trg_stock_sectors_updated_at
BEFORE UPDATE ON stock_sectors
FOR EACH ROW
EXECUTE FUNCTION set_stock_sectors_updated_at();

CREATE TRIGGER trg_stocks_updated_at
BEFORE UPDATE ON stocks
FOR EACH ROW
EXECUTE FUNCTION set_stocks_updated_at();

COMMIT;
