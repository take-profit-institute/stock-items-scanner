CREATE TABLE IF NOT EXISTS stocks (
    stock_id bigserial PRIMARY KEY,
    stock_code varchar(6) NOT NULL UNIQUE,
    stock_name varchar(100) NOT NULL,
    market_type varchar(20) NOT NULL CHECK (market_type IN ('KOSPI', 'KOSDAQ')),
    sector varchar(50),
    listing_status varchar(20) NOT NULL DEFAULT 'LISTED' CHECK (listing_status IN ('LISTED', 'DELISTED', 'SUSPENDED')),
    market_cap bigint,
    shares_outstanding bigint,
    listed_at date,
    data_source varchar(20) NOT NULL DEFAULT 'SEED',
    synced_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE stocks IS '종목 마스터';
COMMENT ON COLUMN stocks.stock_id IS '종목 ID';
COMMENT ON COLUMN stocks.stock_code IS '종목코드';
COMMENT ON COLUMN stocks.stock_name IS '종목명';
COMMENT ON COLUMN stocks.market_type IS '시장구분(KOSPI/KOSDAQ)';
COMMENT ON COLUMN stocks.sector IS '업종';
COMMENT ON COLUMN stocks.listing_status IS '상장상태(LISTED/DELISTED/SUSPENDED)';
COMMENT ON COLUMN stocks.market_cap IS '시가총액';
COMMENT ON COLUMN stocks.shares_outstanding IS '상장주식수';
COMMENT ON COLUMN stocks.listed_at IS '상장일';
COMMENT ON COLUMN stocks.data_source IS '데이터 출처(SEED/KIWOOM/BATCH)';
COMMENT ON COLUMN stocks.synced_at IS '마지막 키움 동기화 시각';
COMMENT ON COLUMN stocks.created_at IS '생성일시';
COMMENT ON COLUMN stocks.updated_at IS '수정일시';

CREATE INDEX IF NOT EXISTS idx_stocks_market_type ON stocks (market_type);
CREATE INDEX IF NOT EXISTS idx_stocks_sector ON stocks (sector);
CREATE INDEX IF NOT EXISTS idx_stocks_listing_status ON stocks (listing_status);
CREATE INDEX IF NOT EXISTS idx_stocks_market_cap ON stocks (market_cap DESC);

CREATE OR REPLACE FUNCTION set_stocks_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stocks_updated_at ON stocks;

CREATE TRIGGER trg_stocks_updated_at
BEFORE UPDATE ON stocks
FOR EACH ROW
EXECUTE FUNCTION set_stocks_updated_at();
