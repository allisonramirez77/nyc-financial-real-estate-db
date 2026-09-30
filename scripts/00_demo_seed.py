"""
DEMO SEED — SYNTHETIC DATA ONLY.

Populates the SQLite database with FAKE, randomly-generated data that matches
the real schema exactly. It lets the whole pipeline, the quality checks and
the dashboard run without the Kaggle source files, which is also how CI
tests the project.

!! NOTHING PRODUCED FROM THIS DATA IS A REAL FINDING. !!
The dashboard stamps a DEMO banner whenever this seed is loaded. Once the
real datasets are loaded (scripts 01-05), 06 drops the banner.
A seed script like this is a test fixture, not analysis.

The fake data goes through the same contract as the real load: property
sales are generated as a raw export, filtered and deduplicated by 03's rules,
and the lineage is stamped into pipeline_meta. So every critical check in
08_run_data_quality.py passes on the demo for the same reason it passes on
real data.

It also plants a few known defects, one per statistical check, so running 08
on the demo shows each check catching what it was designed for:

  Planted defect                                   Caught by
  -----------------------------------------------  -------------------------------
  Four financials jump together on one day         prices.return_outliers
  One ticker jumps on 3x volume (earnings shape)   prices.return_outliers
  One bad close that reverts the next day          prices.return_outliers
  Multi-lot sales priced against a single lot      sales.price_per_sqft_outliers
  Co-ops and condos with no square footage         sales.borough_coverage (Manhattan)
  Sales repeated verbatim in the export            removed at ingest, counted in lineage
  Year built recorded as 0                         sales.year_built_plausible

The three price events are all flagged, and the flag alone can't tell them
apart. What can: a market event moves several tickers at once, an earnings
move comes with heavy volume, and a bad print has neither and reverses the
next day. sql/dq_queries.sql (DQ4) groups the flags by date to show the first.

Run:  python scripts/00_demo_seed.py
"""

import random
from datetime import date, timedelta

from utils import setup_logging, get_connection, PROJECT_ROOT
import os

logger = setup_logging(__name__)

SEED = 42  # fixed so the demo is reproducible
random.seed(SEED)

SCHEMA_PATH = os.path.join(PROJECT_ROOT, "sql", "schema.sql")

# Matches the NYC-weighted ticker list in README.md.
# (sector, is_nyc_hq) — these company facts are real; the PRICES are fake.
COMPANIES = [
    ("SLG",  "SL Green Realty",         "Real Estate", "REIT - Office",     1),
    ("VNO",  "Vornado Realty Trust",    "Real Estate", "REIT - Diversified",1),
    ("ESRT", "Empire State Realty",     "Real Estate", "REIT - Office",     1),
    ("JPM",  "JPMorgan Chase",          "Financials",  "Banks",             1),
    ("GS",   "Goldman Sachs",           "Financials",  "Capital Markets",   1),
    ("MS",   "Morgan Stanley",          "Financials",  "Capital Markets",   1),
    ("C",    "Citigroup",               "Financials",  "Banks",             1),
    ("BLK",  "BlackRock",               "Financials",  "Asset Management",  1),
    ("AXP",  "American Express",        "Financials",  "Consumer Finance",  1),
    ("VZ",   "Verizon",                 "Telecom",     "Telecom Services",  1),
    ("PFE",  "Pfizer",                  "Healthcare",  "Pharmaceuticals",   1),
    ("AAPL", "Apple",                   "Technology",  "Consumer Devices",  0),
    ("MSFT", "Microsoft",               "Technology",  "Software",          0),
]

# Price events planted in the random walk. Moves are sized in multiples of
# each ticker's own daily volatility, because the outlier check scores every
# stock against its own history.
MARKET_DAY = ("2016-11-09", {"JPM", "GS", "MS", "C"}, 8)   # date, tickers, x volatility
EARNINGS_DAY = ("2016-10-20", "AXP", 10)                    # date, ticker, x volatility
BAD_PRINT = ("2017-03-15", "VZ", 1.12)                      # date, ticker, close multiplier
EVENT_VOLUME = 3                                            # real events trade heavy

BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]
# Rough relative price levels so the demo looks plausible. Still fake.
BOROUGH_PSF = {"Manhattan": 1400, "Brooklyn": 700, "Queens": 520,
               "Bronx": 330, "Staten Island": 300}
BOROUGH_WEIGHT = {"Manhattan": 0.18, "Brooklyn": 0.30, "Queens": 0.28,
                  "Bronx": 0.14, "Staten Island": 0.10}

NEIGHBORHOODS = {
    "Manhattan": ["Upper East Side", "Harlem", "Chelsea", "Financial District"],
    "Brooklyn": ["Park Slope", "Bushwick", "Bay Ridge", "Flatbush"],
    "Queens": ["Astoria", "Flushing", "Jamaica", "Forest Hills"],
    "Bronx": ["Riverdale", "Fordham", "Throgs Neck", "Mott Haven"],
    "Staten Island": ["St. George", "Tottenville", "New Springville", "Great Kills"],
}

BUILDING_CLASSES = [
    "01 ONE FAMILY DWELLINGS",
    "02 TWO FAMILY DWELLINGS",
    "10 COOPS - ELEVATOR APARTMENTS",
    "13 CONDOS - ELEVATOR APARTMENTS",
    "21 OFFICE BUILDINGS",
]
# Co-ops and condos sell as units, and the city records no square footage
# for them. Manhattan is mostly units, which is why it loses most of its
# rows at the sqft filter (the real data keeps about 5%).
UNIT_CLASSES = {"10 COOPS - ELEVATOR APARTMENTS", "13 CONDOS - ELEVATOR APARTMENTS"}
CLASS_WEIGHTS = {"Manhattan": [0.03, 0.02, 0.45, 0.40, 0.10],
                 "other":     [0.45, 0.30, 0.10, 0.10, 0.05]}

# 03's filter rules, applied to the fake export exactly as to the real one.
MIN_SALE_PRICE = 10_000
NATURAL_KEY = ("borough", "block", "lot", "sale_date", "sale_price")

# Window chosen to match the real datasets' overlap (both are 2016-2017 era).
START = date(2016, 9, 1)
END = date(2017, 8, 31)


def business_days(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_prices(dates):
    """Random-walk each ticker, with the planted events from MARKET_DAY etc."""
    market_date, market_tickers, market_size = MARKET_DAY
    earnings_date, earnings_ticker, earnings_size = EARNINGS_DAY
    bad_date, bad_ticker, bad_multiplier = BAD_PRINT

    rows = []
    for ticker, *_ in COMPANIES:
        price = random.uniform(30, 220)
        # give each ticker its own mild drift so the chart isn't flat
        drift = random.uniform(-0.0004, 0.0007)
        vol = random.uniform(0.008, 0.018)
        for d in dates:
            day = d.isoformat()
            ret = random.gauss(drift, vol)
            volume_multiplier = 1
            if day == market_date and ticker in market_tickers:
                ret, volume_multiplier = market_size * vol, EVENT_VOLUME
            elif day == earnings_date and ticker == earnings_ticker:
                ret, volume_multiplier = earnings_size * vol, EVENT_VOLUME
            price = max(1.0, price * (1 + ret))

            close = price
            if day == bad_date and ticker == bad_ticker:
                # A bad print: the recorded close is wrong, the stock never moved.
                # The walk continues from the true price, so it reverts next day.
                close = price * bad_multiplier
            close = round(close, 2)
            op = round(close * random.uniform(0.995, 1.005), 2)
            hi = round(max(op, close) * random.uniform(1.000, 1.012), 2)
            lo = round(min(op, close) * random.uniform(0.988, 1.000), 2)
            vol_shares = int(random.uniform(4e5, 9e6) * volume_multiplier)
            rows.append((ticker, day, op, hi, lo, close, vol_shares))
    return rows


def make_returns(price_rows):
    """daily_return + 7/30-day moving averages, per ticker."""
    by_ticker = {}
    for t, d, o, h, l, c, v in price_rows:
        by_ticker.setdefault(t, []).append((d, c))
    out = []
    for t, series in by_ticker.items():
        series.sort()
        closes = [c for _, c in series]
        for i, (d, c) in enumerate(series):
            ret = None if i == 0 else (c - closes[i - 1]) / closes[i - 1]
            ma7 = sum(closes[max(0, i - 6):i + 1]) / len(closes[max(0, i - 6):i + 1]) if i >= 6 else None
            ma30 = sum(closes[max(0, i - 29):i + 1]) / len(closes[max(0, i - 29):i + 1]) if i >= 29 else None
            out.append((t, d, ret, ma7, ma30))
    return out


def make_raw_sales(n_market=6000, n_nominal=1500, n_multi_lot=12,
                   n_year_zero=3, n_repeats=40):
    """
    A fake raw DOF export: market sales mixed with the rows 03 exists to remove.

    The junk mimics REAL, well-known quirks of the NYC DOF data: $0 and
    nominal-dollar transfers (family transfers, deed corrections), unit sales
    with no square footage, and sales the export repeats verbatim.
    """
    boroughs = list(BOROUGH_WEIGHT)
    weights = [BOROUGH_WEIGHT[b] for b in boroughs]
    span = (END - START).days

    def base_row(b):
        class_weights = CLASS_WEIGHTS["Manhattan" if b == "Manhattan" else "other"]
        building_class = random.choices(BUILDING_CLASSES, weights=class_weights)[0]
        sqft = random.choice([700, 900, 1100, 1400, 1800, 2400, 3200])
        sqft = int(sqft * random.uniform(0.8, 1.25))
        return {
            "borough": b,
            "block": random.randint(1, 9999),   # borough + block + lot = parcel id
            "lot": random.randint(1, 150),
            "neighborhood": random.choice(NEIGHBORHOODS[b]),
            "building_class_category": building_class,
            "sale_date": (START + timedelta(days=random.randint(0, span))).isoformat(),
            "gross_sqft": 0.0 if building_class in UNIT_CLASSES else float(sqft),
            "year_built": random.randint(1900, 2015),
        }

    market = []
    for _ in range(n_market):
        row = base_row(random.choices(boroughs, weights=weights)[0])
        sqft = row["gross_sqft"] or 1000.0   # units have no footage but still have a price
        psf = BOROUGH_PSF[row["borough"]] * random.uniform(0.65, 1.45)
        # mild upward drift across the year so the trend chart has a story
        months_in = (date.fromisoformat(row["sale_date"]) - START).days / 365
        psf *= (1 + 0.06 * months_in)
        row["sale_price"] = round(sqft * psf, 2)
        market.append(row)

    # Multi-lot sales: the city records a portfolio's full price against each
    # lot, so one lot's footage carries the whole price.
    with_footage = [r for r in market if r["gross_sqft"] > 0]
    for row in random.sample(with_footage, n_multi_lot):
        row["sale_price"] = round(row["sale_price"] * 25, 2)
    for row in random.sample(with_footage, n_year_zero):
        row["year_built"] = 0

    nominal = []
    for _ in range(n_nominal):
        row = base_row(random.choices(boroughs, weights=weights)[0])
        row["sale_price"] = float(random.choice([0, 0, 0, 1, 10, 100, 4500]))
        nominal.append(row)

    raw = market + nominal
    raw += [dict(r) for r in random.sample(raw, n_repeats)]   # verbatim repeats
    random.shuffle(raw)
    return raw


def ingest_filter(raw):
    """
    03's rules on the fake export: keep market sales with footage, then keep
    the first copy of each natural key. Returns the kept rows and the lineage
    03 would have written, so the load reconciles the same way a real one does.
    """
    market = [r for r in raw if r["sale_price"] >= MIN_SALE_PRICE and r["gross_sqft"] > 0]

    seen, kept = set(), []
    for row in market:
        key = tuple(row[col] for col in NATURAL_KEY)
        if key not in seen:
            seen.add(key)
            kept.append(row)

    lineage = {
        "source_rows": len(raw),
        "kept_rows": len(kept),
        "dropped_rows": len(raw) - len(kept),
        "duplicate_rows": len(market) - len(kept),
    }
    for b in BOROUGHS:
        lineage[f"source_rows.{b}"] = sum(1 for r in raw if r["borough"] == b)

    logger.info("sales: %d raw -> %d market with footage -> %d after dedupe",
                len(raw), len(market), len(kept))
    return kept, lineage


def main():
    logger.warning("SEEDING SYNTHETIC DEMO DATA — not real, do not cite as findings")
    conn = get_connection()
    cur = conn.cursor()

    with open(SCHEMA_PATH) as f:
        cur.executescript(f.read())
    logger.info("Schema created from %s", SCHEMA_PATH)

    cur.executemany(
        "INSERT INTO companies (ticker, name, sector, industry, market_cap, is_nyc_hq)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(t, n, s, i, round(random.uniform(8e9, 4e11), 2), hq) for t, n, s, i, hq in COMPANIES],
    )

    dates = business_days(START, END)
    price_rows = make_prices(dates)
    cur.executemany(
        "INSERT INTO daily_prices (ticker, date, open, high, low, close, volume)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        price_rows,
    )

    cur.executemany(
        "INSERT INTO daily_returns (ticker, date, daily_return, ma_7, ma_30)"
        " VALUES (?, ?, ?, ?, ?)",
        make_returns(price_rows),
    )

    sales, lineage = ingest_filter(make_raw_sales())
    cur.executemany(
        "INSERT INTO nyc_property_sales"
        " (borough, block, lot, neighborhood, building_class_category, sale_price,"
        "  sale_date, gross_sqft, year_built)"
        " VALUES (:borough, :block, :lot, :neighborhood, :building_class_category,"
        "  :sale_price, :sale_date, :gross_sqft, :year_built)",
        sales,
    )

    cur.execute("CREATE TABLE IF NOT EXISTS pipeline_meta (key TEXT PRIMARY KEY, value TEXT)")
    cur.executemany(
        "INSERT INTO pipeline_meta VALUES (?, ?)",
        [("data_source", "demo")] + [(k, str(v)) for k, v in lineage.items()],
    )

    conn.commit()

    for table in ["companies", "daily_prices", "daily_returns", "nyc_property_sales"]:
        n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        logger.info("  %-20s %6d rows", table, n)

    conn.close()
    logger.warning("Demo seed complete. Run 08_run_data_quality.py, then 06_build_dashboard.py.")


if __name__ == "__main__":
    main()
