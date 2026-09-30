"""
Step 3: Ingest NYC property sales.

Reads the NYC Department of Finance rolling sales export, coerces the numeric
columns that arrive as text, filters out non-market transactions, and writes
data/processed/nyc_property_sales.csv.

The filtering is the point of this script. Roughly two thirds of the rows in
this file are not arm's-length market sales:
  - SALE PRICE is literally "-"        (blank in the source export)
  - SALE PRICE is 0                    (deed transfers between family members)
  - SALE PRICE is nominal (< $10,000)  (nominal-consideration transfers)
  - GROSS SQUARE FEET is 0 or blank    (condos/co-ops don't report footage)

It also removes duplicate records: the export repeats some sales verbatim.
A sale is identified by its natural key (borough, block, lot, sale date,
sale price), and only the first copy of each is kept.

Every drop is counted and logged. An unexplained drop is a bug; an explained
one is a finding.

Run:  python scripts/03_ingest_nyc_property_data.py
"""

from __future__ import annotations

from pathlib import Path

import json

import pandas as pd

from utils import setup_logging, PROJECT_ROOT as _ROOT
from checks import expect_rows, expect_no_nulls, expect_numeric, expect_unique, reconcile

logger = setup_logging(__name__)
PROJECT_ROOT = Path(_ROOT)

RAW_PATH = PROJECT_ROOT / "data" / "raw" / "nyc-rolling-sales.csv"
OUT_PATH = PROJECT_ROOT / "data" / "processed" / "nyc_property_sales.csv"

# A sale below this is a nominal transfer, not a market transaction.
MIN_SALE_PRICE = 10_000

# The source encodes borough as an integer code, not a name.
BOROUGHS = {1: "Manhattan", 2: "Bronx", 3: "Brooklyn", 4: "Queens", 5: "Staten Island"}

# borough + block + lot is NYC's parcel identifier (the "BBL"). With the sale
# date and price it identifies one sale, so it is the table's natural key.
NATURAL_KEY = ["borough", "block", "lot", "sale_date", "sale_price"]

KEEP = {
    "borough": "borough",
    "block": "block",
    "lot": "lot",
    "neighborhood": "neighborhood",
    "building_class_category": "building_class_category",
    "sale_price": "sale_price",
    "sale_date": "sale_date",
    "gross_square_feet": "gross_sqft",
    "year_built": "year_built",
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    DOF exports ship headers with stray spaces and inconsistent case
    ('SALE PRICE', 'GROSS SQUARE FEET'). Normalize once, at the boundary,
    so nothing downstream has to know about the source's formatting.
    """
    df.columns = (df.columns.str.strip().str.lower()
                  .str.replace(" ", "_", regex=False)
                  .str.replace("-", "_", regex=False))
    return df


def coerce_numerics(df: pd.DataFrame) -> pd.DataFrame:
    """
    SALE PRICE and GROSS SQUARE FEET arrive as object dtype because the
    export uses '-' for missing. errors='coerce' turns those into NaN
    instead of raising; the counts are logged so the loss is visible.
    """
    for col in ("sale_price", "gross_square_feet", "land_square_feet"):
        if col in df.columns:
            before_na = df[col].isna().sum()
            df[col] = pd.to_numeric(df[col], errors="coerce")
            introduced = int(df[col].isna().sum() - before_na)
            if introduced:
                logger.info("  coerced %-20s -> %d non-numeric values became NaN",
                            col, introduced)
    return df


def main() -> None:
    logger.info("Reading %s", RAW_PATH)
    df = pd.read_csv(RAW_PATH, low_memory=False)
    total = len(df)
    logger.info("raw rows: %d", total)

    df = normalize_columns(df)
    df = coerce_numerics(df)

    # --- filter to arm's-length market sales, counting each reason --------
    price, sqft = df["sale_price"], df["gross_square_feet"]
    reasons = {
        "price missing ('-')": price.isna(),
        "price == 0": price == 0,
        f"price < ${MIN_SALE_PRICE:,} (nominal)": (price > 0) & (price < MIN_SALE_PRICE),
        "gross_sqft missing or 0": sqft.isna() | (sqft == 0),
    }
    for label, mask in reasons.items():
        logger.info("  drop reason  %-32s %6d (%.1f%%)",
                    label, int(mask.sum()), 100 * mask.mean())

    keep = (price >= MIN_SALE_PRICE) & (sqft > 0)
    df = df[keep].copy()
    reconcile(total, len(df), "nyc_property_sales")

    # --- remove duplicate records of the same sale ------------------------
    before_dedupe = len(df)
    df = df.drop_duplicates(subset=NATURAL_KEY, keep="first")
    n_duplicates = before_dedupe - len(df)
    logger.info("  drop reason  %-32s %6d", "duplicate record (natural key)", n_duplicates)
    reconcile(before_dedupe, len(df), "dedupe on natural key")

    # --- reshape to the schema -------------------------------------------
    df["borough"] = df["borough"].map(BOROUGHS)
    df["sale_date"] = pd.to_datetime(df["sale_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["neighborhood"] = df["neighborhood"].str.strip()
    df["building_class_category"] = df["building_class_category"].str.strip()

    out = df[list(KEEP)].rename(columns=KEEP)

    # --- guardrails -------------------------------------------------------
    expect_rows(out, min_rows=20_000, label="nyc_property_sales")
    expect_no_nulls(out, ["borough", "sale_date", "sale_price", "gross_sqft"],
                    label="nyc_property_sales")
    expect_numeric(out, ["sale_price", "gross_sqft"], label="nyc_property_sales")
    expect_unique(out, NATURAL_KEY, label="nyc_property_sales")

    # Coverage note, not a failure: the sqft requirement removes condos and
    # co-ops, which is most of Manhattan. Surface it so the borough
    # comparison is never read as an unbiased sample.
    kept = out.groupby("borough").size()
    raw_by_boro = pd.read_csv(RAW_PATH, usecols=["BOROUGH"]).BOROUGH.map(BOROUGHS).value_counts()
    logger.info("survival rate by borough (sqft requirement biases this):")
    for b in kept.index:
        logger.info("    %-14s %6d / %6d  (%.1f%%)",
                    b, kept[b], raw_by_boro[b], 100 * kept[b] / raw_by_boro[b])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_PATH, index=False)

    # Lineage: the DB only ever sees surviving rows, so the drop count must
    # travel with the data or the dashboard cannot report it honestly.
    lineage = {
        "source_rows": int(total),
        "kept_rows": int(len(out)),
        "dropped_rows": int(total - len(out)),
        "duplicate_rows": int(n_duplicates),
    }
    # Per-borough source counts let the data-quality layer measure coverage
    # from the database alone, without re-reading the raw file.
    lineage.update({f"source_rows.{b}": int(n) for b, n in raw_by_boro.items()})
    (OUT_PATH.parent / "nyc_property_sales.lineage.json").write_text(
        json.dumps(lineage, indent=1))
    logger.info("Wrote %d rows to %s", len(out), OUT_PATH)


if __name__ == "__main__":
    main()
