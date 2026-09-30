"""
Step 5: Load the processed CSVs into SQLite.

Creates the schema, then bulk-loads all four tables in ONE transaction.
Re-running rebuilds from scratch, so the load is idempotent by construction.

Run:  python scripts/05_load_to_sqlite.py
"""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

from utils import setup_logging, get_connection, PROJECT_ROOT as _ROOT
from checks import expect_fk

logger = setup_logging(__name__)
PROJECT_ROOT = Path(_ROOT)
PROC = PROJECT_ROOT / "data" / "processed"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"

# (table, csv, columns) — order matters: parents before children, so the
# foreign keys resolve as they are inserted.
LOADS = [
    ("companies", "companies.csv",
     ["ticker", "name", "sector", "industry", "market_cap", "is_nyc_hq"]),
    ("daily_prices", "daily_prices.csv",
     ["ticker", "date", "open", "high", "low", "close", "volume"]),
    ("daily_returns", "daily_returns.csv",
     ["ticker", "date", "daily_return", "ma_7", "ma_30"]),
    ("nyc_property_sales", "nyc_property_sales.csv",
     ["borough", "block", "lot", "neighborhood", "building_class_category",
      "sale_price", "sale_date", "gross_sqft", "year_built"]),
]


def main() -> None:
    companies = pd.read_csv(PROC / "companies.csv")
    for name in ("daily_prices", "daily_returns"):
        child = pd.read_csv(PROC / f"{name}.csv", usecols=["ticker"])
        expect_fk(child, "ticker", companies, "ticker", label=f"{name}.ticker")

    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = ON")

    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())          # DROP + CREATE => idempotent
    logger.info("schema rebuilt from %s", SCHEMA_PATH)

    try:
        for table, csv_name, cols in LOADS:
            df = pd.read_csv(PROC / csv_name)[cols]
            df.to_sql(table, conn, if_exists="append", index=False, chunksize=5_000)
            logger.info("loaded %-22s %6d rows", table, len(df))
        conn.execute("CREATE TABLE IF NOT EXISTS pipeline_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO pipeline_meta VALUES ('data_source', 'real')")
        lineage_path = PROC / "nyc_property_sales.lineage.json"
        if lineage_path.exists():
            for k, v in json.loads(lineage_path.read_text()).items():
                conn.execute("INSERT OR REPLACE INTO pipeline_meta VALUES (?, ?)", (k, str(v)))
            logger.info("stamped lineage from %s", lineage_path.name)
        conn.commit()                          # one transaction for the whole load
    except Exception:
        conn.rollback()
        logger.error("load failed; rolled back — database left empty, not partial")
        raise

    # Indexes AFTER load: maintaining them during insert is pure overhead.
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON daily_prices(ticker, date);
        CREATE INDEX IF NOT EXISTS idx_returns_ticker_date ON daily_returns(ticker, date);
        CREATE INDEX IF NOT EXISTS idx_sales_boro_date ON nyc_property_sales(borough, sale_date);
    """)
    conn.commit()
    logger.info("indexes created")

    for table, _, _ in LOADS:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        logger.info("  verify %-22s %6d rows", table, n)
    conn.close()
    logger.info("Database ready.")


if __name__ == "__main__":
    main()
