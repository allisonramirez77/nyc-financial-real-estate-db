"""
Step 4: Derive daily returns and moving averages.

Builds the daily_returns table from daily_prices. All work is vectorized and
grouped by ticker — a moving average that leaks across ticker boundaries is
the classic silent bug here, so every rolling call is inside a groupby.

Run:  python scripts/04_clean_transform.py
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from utils import setup_logging, PROJECT_ROOT as _ROOT
from checks import expect_rows, expect_unique, expect_numeric, reconcile

logger = setup_logging(__name__)
PROJECT_ROOT = Path(_ROOT)
IN_PATH = PROJECT_ROOT / "data" / "processed" / "daily_prices.csv"
OUT_PATH = PROJECT_ROOT / "data" / "processed" / "daily_returns.csv"


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Per-ticker daily return and 7/30-day moving averages.

    groupby(...).transform(...) keeps each window inside one ticker. A plain
    .rolling() on the whole frame would average MSFT's close into SLG's
    moving average at the boundary and never raise an error.
    """
    prices = prices.sort_values(["ticker", "date"]).copy()
    g = prices.groupby("ticker", sort=False)["close"]

    prices["daily_return"] = g.pct_change()
    prices["ma_7"] = g.transform(lambda s: s.rolling(7, min_periods=7).mean())
    prices["ma_30"] = g.transform(lambda s: s.rolling(30, min_periods=30).mean())
    return prices[["ticker", "date", "daily_return", "ma_7", "ma_30"]]


def main() -> None:
    prices = pd.read_csv(IN_PATH)
    logger.info("read %d price rows", len(prices))

    returns = compute_returns(prices)
    reconcile(len(prices), len(returns), "daily_returns")

    n_tickers = returns.ticker.nunique()
    # First row per ticker has no prior close, so daily_return is NaN by design.
    expected_null_returns = n_tickers
    actual = int(returns.daily_return.isna().sum())
    if actual != expected_null_returns:
        raise AssertionError(
            f"expected {expected_null_returns} null returns (one per ticker), got {actual}"
        )
    logger.info("check ok  %-22s %d null returns == 1 per ticker", "return warmup", actual)

    expect_rows(returns, 1, "daily_returns")
    expect_unique(returns, ["ticker", "date"], "daily_returns")
    expect_numeric(returns, ["daily_return"], "daily_returns")

    logger.info("ma_7 warmup nulls: %d (expect %d)", returns.ma_7.isna().sum(), 6 * n_tickers)
    logger.info("ma_30 warmup nulls: %d (expect %d)", returns.ma_30.isna().sum(), 29 * n_tickers)

    returns.to_csv(OUT_PATH, index=False)
    logger.info("Wrote %d rows to %s", len(returns), OUT_PATH)


if __name__ == "__main__":
    main()
