-- Update stocks.close_price from already-loaded daily candles.
--
-- Usage:
--   psql "$DATABASE_URL" -v business_date=2026-07-08 \
--     -f sql/004_update_stock_close_price_from_daily_candles.sql
--
-- Notes:
--   - Run with search_path including the stock schema, e.g.
--     postgresql://stock:...@host:5432/candle?options=-csearch_path%3Dstock,public
--   - This does not call Kiwoom and does not create candles. It only copies
--     candles.close into stocks.close_price for the exact business_date.

\set ON_ERROR_STOP on

\if :{?business_date}
\else
  \set business_date '2026-07-08'
\endif

\echo Updating stocks.close_price from daily candles for :business_date

BEGIN;

CREATE TEMP TABLE tmp_daily_close_price AS
SELECT
    c.stock_code,
    c.close AS close_price
FROM candles c
JOIN stocks s ON s.stock_code = c.stock_code
WHERE s.listing_status = 'LISTED'
  AND c.interval = '1d'
  AND c.open_time = (:'business_date' || ' 00:00:00+00')::timestamptz;

\echo Loaded candidate close prices:
SELECT count(*) AS candidate_count FROM tmp_daily_close_price;

\echo Stocks needing close_price update:
SELECT count(*) AS update_target_count
FROM stocks s
JOIN tmp_daily_close_price p ON p.stock_code = s.stock_code
WHERE s.close_price IS DISTINCT FROM p.close_price;

UPDATE stocks s
SET close_price = p.close_price,
    data_source = 'KIWOOM',
    synced_at = now(),
    updated_at = now()
FROM tmp_daily_close_price p
WHERE s.stock_code = p.stock_code
  AND s.close_price IS DISTINCT FROM p.close_price;

\echo Updated rows:
SELECT count(*) AS updated_listed_stocks
FROM stocks s
JOIN tmp_daily_close_price p ON p.stock_code = s.stock_code
WHERE s.close_price = p.close_price;

COMMIT;
