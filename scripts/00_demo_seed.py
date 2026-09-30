"""
DEMO SEED — SYNTHETIC DATA ONLY.

Populates the SQLite database with FAKE, randomly-generated data that matches
the real schema exactly. Its only purpose is to let you run the whole pipeline
and see the dashboard render BEFORE you have the real Kaggle files.

!! NOTHING PRODUCED FROM THIS DATA IS A REAL FINDING. !!
The dashboard stamps a DEMO banner whenever this seed is loaded. Once the
real datasets are loaded (scripts 01-05), re-run 06 and the banner clears.
A seed script like this is a test fixture, not analysis.

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
    """Random-walk each ticker. Fake, but with realistic-looking drift/vol."""
    rows = []
    for ticker, *_ in COMPANIES:
        price = random.uniform(30, 220)
        # give each ticker its own mild drift so the chart isn't flat
        drift = random.uniform(-0.0004, 0.0007)
        vol = random.uniform(0.008, 0.018)
        for d in dates:
            ret = random.gauss(drift, vol)
            price = max(1.0, price * (1 + ret))
            close = round(price, 2)
            op = round(close * random.uniform(0.995, 1.005), 2)
            hi = round(max(op, close) * random.uniform(1.000, 1.012), 2)
            lo = round(min(op, close) * random.uniform(0.988, 1.000), 2)
            vol_shares = int(random.uniform(4e5, 9e6))
            rows.append((ticker, d.isoformat(), op, hi, lo, close, vol_shares))
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


def make_property_sales(n_real=5200, n_junk=900):
    """
    Real-looking market sales + deliberate junk rows.

    The junk rows mimic a REAL, well-known quirk of the NYC DOF data: a large
    share of recorded "sales" are $0 or nominal-dollar transfers (family
    transfers, deed corrections), not arm's-length market transactions.
    Your 03_ingest script filters these; the dashboard reports how many.
    """
    boroughs = list(BOROUGH_WEIGHT)
    weights = [BOROUGH_WEIGHT[b] for b in boroughs]
    span = (END - START).days
    rows = []

    for _ in range(n_real):
        b = random.choices(boroughs, weights=weights)[0]
        sqft = random.choice([700, 900, 1100, 1400, 1800, 2400, 3200])
        sqft = int(sqft * random.uniform(0.8, 1.25))
        psf = BOROUGH_PSF[b] * random.uniform(0.65, 1.45)
        # mild upward drift across the year so the trend chart has a story
        d = START + timedelta(days=random.randint(0, span))
        months_in = (d - START).days / 365
        psf *= (1 + 0.06 * months_in)
        rows.append((
            b,
            random.choice(NEIGHBORHOODS[b]),
            random.choice(BUILDING_CLASSES),
            round(sqft * psf, 2),
            d.isoformat(),
            float(sqft),
            random.randint(1900, 2015),
        ))

    for _ in range(n_junk):
        b = random.choices(boroughs, weights=weights)[0]
        d = START + timedelta(days=random.randint(0, span))
        rows.append((
            b,
            random.choice(NEIGHBORHOODS[b]),
            random.choice(BUILDING_CLASSES),
            float(random.choice([0, 0, 0, 1, 10, 100, 4500])),  # nominal transfers
            d.isoformat(),
            float(random.choice([0, 0, 850, 1200])),            # some missing sqft too
            random.randint(1900, 2015),
        ))

    random.shuffle(rows)
    return rows


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

    cur.executemany(
        "INSERT INTO nyc_property_sales"
        " (borough, neighborhood, building_class_category, sale_price, sale_date,"
        "  gross_sqft, year_built) VALUES (?, ?, ?, ?, ?, ?, ?)",
        make_property_sales(),
    )

    cur.execute("CREATE TABLE IF NOT EXISTS pipeline_meta (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("INSERT OR REPLACE INTO pipeline_meta VALUES ('data_source', 'demo')")

    conn.commit()

    for table in ["companies", "daily_prices", "daily_returns", "nyc_property_sales"]:
        n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        logger.info("  %-20s %6d rows", table, n)

    conn.close()
    logger.warning("Demo seed complete. Run 06_build_dashboard.py next.")


if __name__ == "__main__":
    main()
