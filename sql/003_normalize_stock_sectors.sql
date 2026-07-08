-- Idempotent migration: normalize stocks.sector into stock_sectors and
-- link stocks through stocks.sector_id.
--
-- stocks.sector is intentionally kept for transition compatibility. Backend
-- reads should prefer stock_sectors.sector_name through sector_id and fall back
-- to stocks.sector while old rows are still being migrated.

BEGIN;

CREATE TABLE IF NOT EXISTS stock_sectors (
    sector_id bigserial PRIMARY KEY,
    sector_name varchar(100) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE stocks ADD COLUMN IF NOT EXISTS sector_id bigint;

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

COMMENT ON TABLE stock_sectors IS '업종 마스터';
COMMENT ON COLUMN stock_sectors.sector_id IS '업종 ID';
COMMENT ON COLUMN stock_sectors.sector_name IS '업종명';
COMMENT ON COLUMN stock_sectors.created_at IS '생성일시';
COMMENT ON COLUMN stock_sectors.updated_at IS '수정일시';
COMMENT ON COLUMN stocks.sector_id IS '업종 ID';
COMMENT ON COLUMN stocks.sector IS '업종명(전환 호환용)';

CREATE INDEX IF NOT EXISTS idx_stocks_sector_id ON stocks (sector_id);

CREATE OR REPLACE FUNCTION set_stock_sectors_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stock_sectors_updated_at ON stock_sectors;

CREATE TRIGGER trg_stock_sectors_updated_at
BEFORE UPDATE ON stock_sectors
FOR EACH ROW
EXECUTE FUNCTION set_stock_sectors_updated_at();

COMMIT;
