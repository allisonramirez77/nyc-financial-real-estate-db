"""
Live ingest: NYC property sales from the NYC Open Data (Socrata) API.

This is the API-backed twin of 03_ingest_nyc_property_data.py. It writes the
SAME output shape, so everything downstream is unchanged — the pipeline does
not know or care which source produced the rows.

Dataset: NYC Citywide Rolling Calendar Sales (usep-8jbt)
    https://data.cityofnewyork.us/dataset/NYC-Citywide-Rolling-Calendar-Sales/usep-8jbt

Why this exists: the Kaggle file is a frozen 2016-2017 snapshot. This endpoint
is the same data, maintained by the Department of Finance, updated on a rolling
basis. Same schema, current rows.

Auth: works with no key at a lower rate limit. For a higher limit, register a
free app token and put it in .env as SOCRATA_APP_TOKEN (never commit it).

Run:  python scripts/07_ingest_live_nyc_sales.py
      python scripts/07_ingest_live_nyc_sales.py --since 2024-01-01
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd
import requests

from utils import setup_logging, PROJECT_ROOT as _ROOT
from checks import expect_rows, expect_no_nulls, expect_numeric, reconcile

logger = setup_logging(__name__)
PROJECT_ROOT = Path(_ROOT)

ENDPOINT = "https://data.cityofnewyork.us/resource/usep-8jbt.json"
OUT_PATH = PROJECT_ROOT / "data" / "processed" / "nyc_property_sales_live.csv"

PAGE_SIZE = 50_000        # Socrata's max per request
MAX_RETRIES = 4
MIN_SALE_PRICE = 10_000
BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

FIELDS = ["borough", "neighborhood", "building_class_category",
          "sale_price", "sale_date", "gross_square_feet", "year_built"]


def _session() -> requests.Session:
    """One session for connection reuse; app token raises the rate limit."""
    s = requests.Session()
    token = os.getenv("SOCRATA_APP_TOKEN")
    if token:
        s.headers["X-App-Token"] = token
        logger.info("using SOCRATA_APP_TOKEN (higher rate limit)")
    else:
        logger.warning("no SOCRATA_APP_TOKEN set — throttled tier")
    return s


def fetch_page(sess: requests.Session, offset: int, since: str | None) -> list[dict]:
    """
    One page, with exponential backoff.

    Public APIs fail transiently. A pipeline that dies on the first 503 is
    not a pipeline — retry with widening gaps, then give up loudly.
    """
    params = {
        "$select": ",".join(FIELDS),
        "$limit": PAGE_SIZE,
        "$offset": offset,
        "$order": "sale_date",          # stable order => safe pagination
    }
    if since:
        params["$where"] = f"sale_date >= '{since}T00:00:00.000'"

    for attempt in range(MAX_RETRIES):
        try:
            r = sess.get(ENDPOINT, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            wait = 2 ** attempt
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"failed after {MAX_RETRIES} attempts: {exc}") from exc
            logger.warning("  page@%d failed (%s); retrying in %ds", offset, exc, wait)
            time.sleep(wait)
    return []


def fetch_all(since: str | None) -> pd.DataFrame:
    """Page until a short page comes back — that's the end of the result set."""
    sess, rows, offset = _session(), [], 0
    while True:
        page = fetch_page(sess, offset, since)
        rows.extend(page)
        logger.info("  fetched %6d rows (total %7d)", len(page), len(rows))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return pd.DataFrame(rows)


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """Identical cleaning contract to 03 — plus commas, which the API adds."""
    total = len(df)

    # The API returns numbers as strings, some with thousands separators
    # ("2,021"). Strip separators BEFORE coercing or every one becomes NaN.
    for col in ("sale_price", "gross_square_feet"):
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )

    price, sqft = df["sale_price"], df["gross_square_feet"]
    for label, mask in {
        "price missing": price.isna(),
        "price == 0": price == 0,
        f"price < ${MIN_SALE_PRICE:,}": (price > 0) & (price < MIN_SALE_PRICE),
        "gross_sqft missing or 0": sqft.isna() | (sqft == 0),
    }.items():
        logger.info("  drop reason  %-28s %6d (%.1f%%)", label, int(mask.sum()), 100 * mask.mean())

    df = df[(price >= MIN_SALE_PRICE) & (sqft > 0)].copy()
    reconcile(total, len(df), "nyc_property_sales_live")

    df["borough"] = df["borough"].astype(str).map(BOROUGHS)
    df["sale_date"] = pd.to_datetime(df["sale_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["gross_sqft"] = df.pop("gross_square_feet")
    df["year_built"] = pd.to_numeric(df["year_built"], errors="coerce").astype("Int64")
    for c in ("neighborhood", "building_class_category"):
        df[c] = df[c].str.strip()

    return df[["borough", "neighborhood", "building_class_category",
               "sale_price", "sale_date", "gross_sqft", "year_built"]]


def watermark() -> str | None:
    """
    Incremental load: resume from the newest row already stored instead of
    re-pulling history every run. This is what makes a scheduled job cheap.
    """
    if not OUT_PATH.exists():
        return None
    prev = pd.read_csv(OUT_PATH, usecols=["sale_date"])
    return None if prev.empty else prev["sale_date"].max()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD; default = incremental from stored max")
    ap.add_argument("--full", action="store_true", help="ignore watermark, refetch all")
    args = ap.parse_args()

    since = None if args.full else (args.since or watermark())
    logger.info("fetching from %s", since or "beginning of dataset")

    raw = fetch_all(since)
    if raw.empty:
        logger.info("no new rows — already current")
        return

    out = transform(raw)

    expect_rows(out, 1, "nyc_property_sales_live")
    expect_no_nulls(out, ["borough", "sale_date", "sale_price", "gross_sqft"],
                    label="nyc_property_sales_live")
    expect_numeric(out, ["sale_price", "gross_sqft"], label="nyc_property_sales_live")

    # Union with what we already have, then drop exact duplicates. The
    # dedupe is what makes re-running safe — an overlapping fetch window
    # must never double-count a sale.
    if OUT_PATH.exists() and not args.full:
        prior = pd.read_csv(OUT_PATH)
        before = len(prior) + len(out)
        out = (pd.concat([prior, out], ignore_index=True)
                 .drop_duplicates(subset=["borough", "neighborhood", "sale_date",
                                          "sale_price", "gross_sqft"]))
        reconcile(before, len(out), "dedupe after merge")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_PATH, index=False)
    logger.info("Wrote %d rows to %s (%s..%s)",
                len(out), OUT_PATH, out.sale_date.min(), out.sale_date.max())


if __name__ == "__main__":
    main()
