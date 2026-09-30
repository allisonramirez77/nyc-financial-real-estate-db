"""
Step 2: Ingest company metadata (the dimension table).

Reads the S&P 500 fundamentals export, keeps the NYC-weighted sample, and
attaches the is_nyc_hq flag that the source does not provide.

Run:  python scripts/02_ingest_company_data.py
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from utils import setup_logging, PROJECT_ROOT as _ROOT
from checks import expect_rows, expect_no_nulls, expect_unique

logger = setup_logging(__name__)
PROJECT_ROOT = Path(_ROOT)
RAW_PATH = PROJECT_ROOT / "data" / "raw" / "financials.csv"
OUT_PATH = PROJECT_ROOT / "data" / "processed" / "companies.csv"

COLS = {"Symbol": "ticker", "Name": "name", "Sector": "sector", "Market Cap": "market_cap"}

# Hand-tagged: the S&P 500 export has no headquarters field. Verified by hand;
# a 13-row manual join is cheaper and more accurate than scraping HQ addresses.
NYC_HQ = {"SLG", "VNO", "ESRT", "JPM", "GS", "MS", "C", "BLK", "AXP", "VZ", "PFE"}

# ESRT is a mid-cap REIT and is absent from the S&P 500 universe. It is central
# to the NYC REIT thesis, so it is added explicitly rather than silently dropped.
MANUAL_ROWS = [
    {"ticker": "ESRT", "name": "Empire State Realty Trust",
     "sector": "Real Estate", "market_cap": 3_500_000_000.0},
]

TICKERS = ["SLG", "VNO", "ESRT", "JPM", "GS", "MS", "C", "BLK", "AXP",
           "VZ", "PFE", "AAPL", "MSFT"]


def main() -> None:
    df = pd.read_csv(RAW_PATH, usecols=list(COLS)).rename(columns=COLS)
    logger.info("source universe: %d companies", len(df))

    df = df[df.ticker.isin(TICKERS)].copy()

    missing = sorted(set(TICKERS) - set(df.ticker))
    if missing:
        logger.warning("absent from S&P 500 source: %s -> adding manually", missing)
        manual = pd.DataFrame([r for r in MANUAL_ROWS if r["ticker"] in missing])
        df = pd.concat([df, manual], ignore_index=True)

    still_missing = sorted(set(TICKERS) - set(df.ticker))
    if still_missing:
        raise RuntimeError(f"no metadata for {still_missing}; add to MANUAL_ROWS")

    df["industry"] = df["sector"]          # source has no finer granularity
    df["is_nyc_hq"] = df.ticker.isin(NYC_HQ).astype(int)

    expect_rows(df, len(TICKERS), "companies")
    expect_unique(df, ["ticker"], "companies")
    expect_no_nulls(df, ["ticker", "name", "sector"], "companies")

    logger.info("NYC-HQ: %d of %d", df.is_nyc_hq.sum(), len(df))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df[["ticker", "name", "sector", "industry", "market_cap", "is_nyc_hq"]].to_csv(OUT_PATH, index=False)
    logger.info("Wrote %d rows to %s", len(df), OUT_PATH)


if __name__ == "__main__":
    main()
