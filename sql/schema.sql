-- Schema for the NYC Financial & Real Estate Data Pipeline
-- Written for SQLite. For PostgreSQL, swap INTEGER PRIMARY KEY AUTOINCREMENT
-- -> SERIAL PRIMARY KEY, and REAL -> NUMERIC where precision matters.

DROP TABLE IF EXISTS daily_returns;
DROP TABLE IF EXISTS daily_prices;
DROP TABLE IF EXISTS nyc_property_sales;
DROP TABLE IF EXISTS companies;
DROP TABLE IF EXISTS pipeline_meta;

CREATE TABLE companies (
    ticker        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    sector        TEXT,
    industry      TEXT,
    market_cap    REAL,
    is_nyc_hq     INTEGER NOT NULL DEFAULT 0  -- 1 = NYC-headquartered, 0 = not. Manually tagged.
);

CREATE TABLE daily_prices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL REFERENCES companies(ticker),
    date          TEXT NOT NULL,   -- ISO format YYYY-MM-DD
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    volume        INTEGER,
    UNIQUE(ticker, date)
);

CREATE TABLE daily_returns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL REFERENCES companies(ticker),
    date          TEXT NOT NULL,
    daily_return  REAL,   -- (close_t - close_t-1) / close_t-1
    ma_7          REAL,   -- 7-day moving average of close
    ma_30         REAL,   -- 30-day moving average of close
    UNIQUE(ticker, date)
);

CREATE TABLE nyc_property_sales (
    sale_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    borough                  TEXT,
    block                    INTEGER,   -- borough + block + lot = NYC parcel id (BBL)
    lot                      INTEGER,
    neighborhood              TEXT,
    building_class_category  TEXT,
    sale_price                REAL,
    sale_date                  TEXT,   -- ISO format YYYY-MM-DD
    gross_sqft                 REAL,
    year_built                 INTEGER,
    UNIQUE(borough, block, lot, sale_date, sale_price)   -- natural key: one row per sale
);

-- Provenance for the current load: data_source ('real' or 'demo') and the
-- ingest lineage (source, dropped, kept, duplicate and per-borough counts).
-- Rebuilt with the other tables, so a reload never inherits stale counts.
CREATE TABLE pipeline_meta (
    key           TEXT PRIMARY KEY,
    value         TEXT
);

-- Indexes are created by 05_load_to_sqlite.py after the bulk load, because
-- maintaining them row by row during the insert is slower.
