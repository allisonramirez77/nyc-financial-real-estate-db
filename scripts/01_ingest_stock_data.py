"""
Step 1: Ingest stock price data.

Reads per-ticker OHLCV files from the Huge Stock Market Dataset, keeps only the
NYC-weighted ticker sample and only the analysis window, and writes one tidy
CSV to data/processed/daily_prices.csv.

Source files look like:
    Date,Open,High,Low,Close,Volume,OpenInt
    2005-02-25,48.045,49.376,48.045,49.376,223558,0

Where the raw files live:
    default  <project>/data/raw/stockdata/
    override  STOCK_DIR=/path/to/files python scripts/01_ingest_stock_data.py

Run:  python scripts/01_ingest_stock_data.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from utils import setup_logging, PROJECT_ROOT as _ROOT

PROJECT_ROOT = Path(_ROOT)   # utils exposes a str; Path makes the / operator work
from checks import expect_rows, expect_no_nulls, expect_unique, expect_numeric, reconcile

logger = setup_logging(__name__)

# --- configuration -------------------------------------------------------
# The window is set by the NYC property sales data (2016-09-01..2017-08-31).
# Both datasets must cover the same period or the centerpiece comparison is
# meaningless, so we clip here rather than downstream.
WINDOW_START = "2016-09-01"
WINDOW_END = "2017-08-31"

STOCK_DIR = Path(os.getenv("STOCK_DIR", PROJECT_ROOT / "data" / "raw" / "stockdata"))
OUT_PATH = PROJECT_ROOT / "data" / "processed" / "daily_prices.csv"

# NYC-weighted sample. REITs first — they carry the project's core question.
TICKERS = [
    "SLG", "VNO", "ESRT",                                  # NYC REITs
    "JPM", "GS", "MS", "C", "BLK", "AXP",                  # NYC financials
    "VZ", "PFE",                                           # NYC other sectors
    "AAPL", "MSFT",                                        # non-NYC control group
]

# Read narrow and declare dtypes: never let pandas guess on ~12k rows/file.
# OpenInt is always 0 in this dataset — don't carry a dead column.
USECOLS = ["Date", "Open", "High", "Low", "Close", "Volume"]
DTYPES = {"Open": "float64", "High": "float64", "Low": "float64",
          "Close": "float64", "Volume": "int64"}


def load_ticker(ticker: str) -> pd.DataFrame | None:
    """
    Read one ticker file, clip to the analysis window, return tidy rows.

    Returns None when the file is absent so the caller can report every
    missing ticker at once — a partial load that silently skips a REIT is
    worse than a loud failure.
    """
    path = STOCK_DIR / f"{ticker.lower()}.us.txt"
    if not path.exists():
        logger.error("missing file for %s: %s", ticker, path)
        return None

    df = pd.read_csv(path, usecols=USECOLS, dtype=DTYPES, parse_dates=["Date"])
    full_rows = len(df)

    # Vectorized boolean mask, not a loop or a per-row filter.
    window = df[(df["Date"] >= WINDOW_START) & (df["Date"] <= WINDOW_END)].copy()

    if window.empty:
        logger.error("%s has no rows in %s..%s (file spans %s..%s)",
                     ticker, WINDOW_START, WINDOW_END,
                     df["Date"].min().date(), df["Date"].max().date())
        return None

    window.insert(0, "ticker", ticker)
    window = window.rename(columns=str.lower).rename(columns={"date": "date"})
    window["date"] = window["date"].dt.strftime("%Y-%m-%d")

    logger.info("  %-5s %5d rows in file -> %3d in window (%s..%s)",
                ticker, full_rows, len(window),
                window["date"].iloc[0], window["date"].iloc[-1])
    return window


def main() -> None:
    logger.info("Ingesting %d tickers from %s", len(TICKERS), STOCK_DIR)
    if not STOCK_DIR.is_dir():
        raise FileNotFoundError(
            f"{STOCK_DIR} not found. Put the .us.txt files there, or set STOCK_DIR."
        )

    frames, missing = [], []
    for ticker in TICKERS:
        frame = load_ticker(ticker)
        (frames if frame is not None else missing).append(frame if frame is not None else ticker)

    if missing:
        # Fail loudly. A quietly-shrunk ticker universe corrupts every
        # sector average downstream and is nearly impossible to spot later.
        raise RuntimeError(f"missing or empty tickers: {missing}")

    prices = pd.concat(frames, ignore_index=True)

    # --- guardrails at the boundary --------------------------------------
    expect_rows(prices, min_rows=len(TICKERS) * 200, label="daily_prices")
    expect_no_nulls(prices, ["ticker", "date", "close"], label="daily_prices")
    expect_unique(prices, ["ticker", "date"], label="daily_prices")
    expect_numeric(prices, ["open", "high", "low", "close", "volume"], label="daily_prices")

    # Every ticker should share one trading calendar. A ticker with a
    # different count means a halt, a late listing, or a bad file.
    counts = prices.groupby("ticker").size()
    if counts.nunique() != 1:
        logger.warning("uneven trading days per ticker:\n%s", counts.to_string())
    else:
        logger.info("check ok  %-22s all %d tickers have %d trading days",
                    "calendar", len(counts), counts.iloc[0])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    prices.to_csv(OUT_PATH, index=False)   # overwrite => idempotent re-runs
    reconcile(len(prices), len(prices), "daily_prices written")
    logger.info("Wrote %d rows to %s", len(prices), OUT_PATH)


if __name__ == "__main__":
    main()
