"""
Data-quality checks for the NYC financial & real estate database.

checks.py guards the pipeline while it runs: a failure stops the load.
This module measures the data after it has landed. Every check returns a
score, and flagged records go to a review queue instead of stopping anything.

Two kinds of check:

  Rule checks         A SQL query that selects the rows breaking a rule.
                      Zero rows back means the rule holds. (Same idea as a
                      dbt test.)
  Statistical checks  For values with no hard rule to test against: flag
                      what is implausible compared with its peers.

Every check is tagged with a data-quality dimension:
  completeness  is everything that should be there, there?
  validity      do values obey the rules of their domain?
  uniqueness    is each real-world thing recorded once?
  consistency   do related tables and sources agree with each other?
  accuracy      is the value plausible? (There is no golden source to
                compare against, so statistical plausibility is the proxy.)

08_run_data_quality.py runs everything here and stores the results.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

BOROUGH_NAMES = ("Manhattan", "Bronx", "Brooklyn", "Queens", "Staten Island")


# ---------------------------------------------------------------------------
# Result of one check
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    check_id: str
    table_name: str
    dimension: str
    severity: str          # "critical" fails the run; "warning" flags for review
    description: str
    rows_checked: int
    threshold: float       # minimum pass rate for the data to be fit for use
    exceptions: pd.DataFrame   # one row per flagged record: record_key, value

    @property
    def rows_failed(self) -> int:
        return len(self.exceptions)

    @property
    def pass_rate(self) -> float:
        # An empty table is not a clean table: nothing checked scores zero.
        if self.rows_checked == 0:
            return 0.0
        return 1 - self.rows_failed / self.rows_checked

    @property
    def status(self) -> str:
        if self.pass_rate >= self.threshold:
            return "PASS"
        return "FAIL" if self.severity == "critical" else "WARN"


# ---------------------------------------------------------------------------
# Rule checks: a query that returns the rows breaking the rule
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleCheck:
    check_id: str
    table_name: str
    dimension: str
    severity: str
    description: str
    failing_rows_sql: str             # must return two columns: record_key, value
    population_sql: str | None = None # defaults to COUNT(*) of table_name
    threshold: float = 1.0

    def run(self, conn: sqlite3.Connection) -> CheckResult:
        population_sql = self.population_sql or f"SELECT COUNT(*) FROM {self.table_name}"
        rows_checked = conn.execute(population_sql).fetchone()[0]
        exceptions = pd.read_sql(self.failing_rows_sql, conn)
        return CheckResult(self.check_id, self.table_name, self.dimension,
                           self.severity, self.description, rows_checked,
                           self.threshold, exceptions)


RULE_CHECKS = [
    # --- market data ------------------------------------------------------
    RuleCheck(
        check_id="prices.required_fields",
        table_name="daily_prices", dimension="completeness", severity="critical",
        description="open, high, low, close and volume are all populated",
        failing_rows_sql="""
            SELECT ticker || ' ' || date AS record_key, 'null price or volume' AS value
            FROM daily_prices
            WHERE open IS NULL OR high IS NULL OR low IS NULL
               OR close IS NULL OR volume IS NULL""",
    ),
    RuleCheck(
        check_id="prices.full_trading_calendar",
        table_name="daily_prices", dimension="completeness", severity="critical",
        description="every ticker has a price on every trading day in the window",
        # Expected rows = every ticker x every trading day. Anything missing
        # from that grid is a gap: a halt, a late listing, or a truncated file.
        population_sql="""
            SELECT (SELECT COUNT(DISTINCT ticker) FROM daily_prices)
                 * (SELECT COUNT(DISTINCT date)   FROM daily_prices)""",
        failing_rows_sql="""
            WITH tickers      AS (SELECT DISTINCT ticker FROM daily_prices),
                 trading_days AS (SELECT DISTINCT date   FROM daily_prices)
            SELECT t.ticker || ' ' || d.date AS record_key, 'missing' AS value
            FROM tickers t
            CROSS JOIN trading_days d
            LEFT JOIN daily_prices p ON p.ticker = t.ticker AND p.date = d.date
            WHERE p.ticker IS NULL""",
    ),
    RuleCheck(
        check_id="prices.ohlc_consistent",
        table_name="daily_prices", dimension="validity", severity="critical",
        description="low <= open, close <= high, and low > 0",
        failing_rows_sql="""
            SELECT ticker || ' ' || date AS record_key,
                   printf('O=%.2f H=%.2f L=%.2f C=%.2f', open, high, low, close) AS value
            FROM daily_prices
            WHERE low <= 0
               OR low  > min(open, close)
               OR high < max(open, close)""",
    ),
    RuleCheck(
        check_id="prices.volume_positive",
        table_name="daily_prices", dimension="validity", severity="warning",
        description="traded volume is above zero",
        failing_rows_sql="""
            SELECT ticker || ' ' || date AS record_key, CAST(volume AS TEXT) AS value
            FROM daily_prices
            WHERE volume <= 0""",
    ),
    RuleCheck(
        check_id="prices.unique_ticker_date",
        table_name="daily_prices", dimension="uniqueness", severity="critical",
        description="one row per ticker per day",
        failing_rows_sql="""
            SELECT ticker || ' ' || date AS record_key, COUNT(*) || ' copies' AS value
            FROM daily_prices
            GROUP BY ticker, date
            HAVING COUNT(*) > 1""",
    ),
    RuleCheck(
        check_id="prices.ticker_in_companies",
        table_name="daily_prices", dimension="consistency", severity="critical",
        description="every priced ticker has a row in companies",
        population_sql="SELECT COUNT(DISTINCT ticker) FROM daily_prices",
        failing_rows_sql="""
            SELECT DISTINCT p.ticker AS record_key, 'no row in companies' AS value
            FROM daily_prices p
            LEFT JOIN companies c ON c.ticker = p.ticker
            WHERE c.ticker IS NULL""",
    ),
    RuleCheck(
        check_id="returns.reconcile_to_prices",
        table_name="daily_returns", dimension="consistency", severity="critical",
        description="stored daily_return matches close / previous close - 1",
        # daily_returns is derived from daily_prices in 04. Recomputing it in
        # SQL, independently of the pandas code, proves the two tables agree.
        failing_rows_sql="""
            WITH recomputed AS (
                SELECT ticker, date,
                       close / LAG(close) OVER (PARTITION BY ticker ORDER BY date) - 1
                           AS expected
                FROM daily_prices
            )
            SELECT r.ticker || ' ' || r.date AS record_key,
                   printf('stored=%.6f expected=%.6f', r.daily_return, x.expected) AS value
            FROM daily_returns r
            LEFT JOIN recomputed x ON x.ticker = r.ticker AND x.date = r.date
            WHERE (r.daily_return IS NULL) != (x.expected IS NULL)
               OR ABS(r.daily_return - x.expected) > 1e-9""",
    ),

    # --- property sales ---------------------------------------------------
    RuleCheck(
        check_id="sales.required_fields",
        table_name="nyc_property_sales", dimension="completeness", severity="critical",
        description="parcel id, date, price and square footage are all populated",
        failing_rows_sql="""
            SELECT CAST(sale_id AS TEXT) AS record_key, 'null required field' AS value
            FROM nyc_property_sales
            WHERE borough IS NULL OR block IS NULL OR lot IS NULL
               OR sale_date IS NULL OR sale_price IS NULL OR gross_sqft IS NULL""",
    ),
    RuleCheck(
        check_id="sales.valid_values",
        table_name="nyc_property_sales", dimension="validity", severity="critical",
        description="known borough, price >= $10,000, square footage > 0",
        # These are the ingest filter's rules, re-verified on what actually
        # landed in the database.
        failing_rows_sql=f"""
            SELECT CAST(sale_id AS TEXT) AS record_key,
                   printf('%s | $%.0f | %.0f sqft', borough, sale_price, gross_sqft) AS value
            FROM nyc_property_sales
            WHERE borough NOT IN {BOROUGH_NAMES}
               OR sale_price < 10000
               OR gross_sqft <= 0""",
    ),
    RuleCheck(
        check_id="sales.year_built_plausible",
        table_name="nyc_property_sales", dimension="validity", severity="warning",
        description="year built between 1800 and the year of sale",
        threshold=0.999,
        failing_rows_sql="""
            SELECT CAST(sale_id AS TEXT) AS record_key, CAST(year_built AS TEXT) AS value
            FROM nyc_property_sales
            WHERE year_built IS NULL
               OR year_built < 1800
               OR year_built > CAST(substr(sale_date, 1, 4) AS INTEGER)""",
    ),
    RuleCheck(
        check_id="sales.unique_natural_key",
        table_name="nyc_property_sales", dimension="uniqueness", severity="critical",
        description="one row per parcel (borough, block, lot) per sale date and price",
        failing_rows_sql="""
            SELECT borough || ' ' || block || '-' || lot || ' ' || sale_date AS record_key,
                   COUNT(*) || ' copies' AS value
            FROM nyc_property_sales
            GROUP BY borough, block, lot, sale_date, sale_price
            HAVING COUNT(*) > 1""",
    ),

    # --- across sources and the pipeline ----------------------------------
    RuleCheck(
        check_id="cross.period_alignment",
        table_name="daily_prices + nyc_property_sales", dimension="consistency",
        severity="critical",
        description="prices and sales cover the same months (the REIT comparison joins on month)",
        population_sql="""
            SELECT COUNT(*) FROM (
                SELECT substr(date, 1, 7)      FROM daily_prices
                UNION
                SELECT substr(sale_date, 1, 7) FROM nyc_property_sales)""",
        failing_rows_sql="""
            WITH price_months AS (SELECT DISTINCT substr(date, 1, 7) AS month FROM daily_prices),
                 sale_months  AS (SELECT DISTINCT substr(sale_date, 1, 7) AS month FROM nyc_property_sales)
            SELECT month AS record_key, 'prices only' AS value
            FROM price_months WHERE month NOT IN (SELECT month FROM sale_months)
            UNION ALL
            SELECT month, 'sales only'
            FROM sale_months WHERE month NOT IN (SELECT month FROM price_months)""",
    ),
    RuleCheck(
        check_id="pipeline.lineage_reconciles",
        table_name="pipeline_meta", dimension="consistency", severity="critical",
        description="source rows - dropped rows = kept rows = rows in the table",
        # The ingest step records what it read and what it dropped. If those
        # numbers don't add up to what's in the table, a row went missing
        # somewhere without a recorded reason.
        population_sql="SELECT 1",
        failing_rows_sql="""
            WITH meta AS (
                SELECT MAX(CASE WHEN key = 'source_rows'  THEN CAST(value AS INTEGER) END) AS source_rows,
                       MAX(CASE WHEN key = 'dropped_rows' THEN CAST(value AS INTEGER) END) AS dropped_rows,
                       MAX(CASE WHEN key = 'kept_rows'    THEN CAST(value AS INTEGER) END) AS kept_rows
                FROM pipeline_meta
            )
            SELECT 'nyc_property_sales' AS record_key,
                   printf('source %d - dropped %d, kept %d, loaded %d',
                          source_rows, dropped_rows, kept_rows,
                          (SELECT COUNT(*) FROM nyc_property_sales)) AS value
            FROM meta
            WHERE source_rows IS NULL
               OR source_rows - dropped_rows != kept_rows
               OR kept_rows != (SELECT COUNT(*) FROM nyc_property_sales)""",
    ),
]


# ---------------------------------------------------------------------------
# Statistical checks
# ---------------------------------------------------------------------------

def robust_z(values: pd.Series) -> pd.Series:
    """
    How unusual each value is, in standard-deviation-like units, using the
    median and the median absolute deviation (MAD) instead of mean and std.

    Mean and std are pulled toward the very outliers you are hunting for,
    which lets extreme values hide. Median and MAD barely move. Multiplying
    MAD by 1.4826 makes it equal the standard deviation for normal data, so
    the result reads like an ordinary z-score.
    """
    median = values.median()
    mad = (values - median).abs().median()
    if mad == 0:
        return pd.Series(0.0, index=values.index)
    return (values - median) / (1.4826 * mad)


def return_outliers(conn: sqlite3.Connection, z_limit: float = 5.0) -> CheckResult:
    """Flag daily moves that are extreme for that particular stock."""
    returns = pd.read_sql(
        "SELECT ticker, date, daily_return FROM daily_returns "
        "WHERE daily_return IS NOT NULL", conn)

    # Score each ticker against its own history: a 4% day is routine for
    # one stock and extreme for another.
    returns["robust_z"] = returns.groupby("ticker")["daily_return"].transform(robust_z)
    flagged = returns[returns["robust_z"].abs() > z_limit]
    flagged = flagged.sort_values("robust_z", key=abs, ascending=False)   # most extreme first

    exceptions = pd.DataFrame({
        "record_key": flagged["ticker"] + " " + flagged["date"],
        "value": [f"{ret:+.2%} (z={z:+.1f})"
                  for ret, z in zip(flagged["daily_return"], flagged["robust_z"])],
    })
    return CheckResult(
        "prices.return_outliers", "daily_returns", "accuracy", "warning",
        f"daily return within {z_limit:g} robust z-scores of the stock's median",
        rows_checked=len(returns), threshold=0.99, exceptions=exceptions)


def price_per_sqft_outliers(conn: sqlite3.Connection, fence: float = 3.0,
                            min_peers: int = 30) -> CheckResult:
    """Flag sales whose price per square foot is implausible for their peers."""
    sales = pd.read_sql(
        "SELECT sale_id, borough, building_class_category, sale_price, gross_sqft "
        "FROM nyc_property_sales", conn)

    # $/sqft is heavily right-skewed. On a log scale it is close to
    # symmetric, so one fence rule catches both too-cheap and too-expensive.
    sales["psf"] = sales["sale_price"] / sales["gross_sqft"]
    sales["log_psf"] = np.log10(sales["psf"])

    # Peers = same borough and building class: $900/sqft is normal for a
    # Manhattan walk-up and extreme for a Staten Island warehouse.
    peers = sales.groupby(["borough", "building_class_category"])["log_psf"]
    q1 = peers.transform(lambda s: s.quantile(0.25))
    q3 = peers.transform(lambda s: s.quantile(0.75))
    iqr = q3 - q1
    n_peers = peers.transform("size")

    # Tukey's "far out" fences: more than 3 IQRs beyond the middle half.
    outside = (sales["log_psf"] < q1 - fence * iqr) | (sales["log_psf"] > q3 + fence * iqr)
    # Quartiles from a handful of sales are noise, so small groups are skipped.
    assessed = n_peers >= min_peers
    flagged = sales[assessed & outside]

    exceptions = pd.DataFrame({
        "record_key": flagged["sale_id"].astype(str),
        "value": [f"${psf:,.2f}/sqft" for psf in flagged["psf"]],
    })
    return CheckResult(
        "sales.price_per_sqft_outliers", "nyc_property_sales", "accuracy", "warning",
        f"$/sqft inside {fence:g}x IQR fences for its borough and building class "
        f"(groups of {min_peers}+ sales)",
        rows_checked=int(assessed.sum()), threshold=0.98, exceptions=exceptions)


def borough_coverage(conn: sqlite3.Connection, min_share: float = 0.20) -> CheckResult:
    """
    What share of each borough's source records survived cleaning?

    A clean table can still be unfit for a question. If a borough keeps only
    a sliver of its records, borough-level comparisons are biased no matter
    how clean each surviving row is. Source counts come from the lineage the
    ingest step recorded in pipeline_meta.
    """
    source = pd.read_sql(
        "SELECT substr(key, 13) AS borough, CAST(value AS INTEGER) AS source_rows "
        "FROM pipeline_meta WHERE key LIKE 'source_rows.%'", conn)
    kept = pd.read_sql(
        "SELECT borough, COUNT(*) AS kept_rows FROM nyc_property_sales GROUP BY borough", conn)

    coverage = source.merge(kept, on="borough", how="left").fillna({"kept_rows": 0})
    coverage["share"] = coverage["kept_rows"] / coverage["source_rows"]
    thin = coverage[coverage["share"] < min_share]

    exceptions = pd.DataFrame({
        "record_key": thin["borough"],
        "value": [f"{k:,.0f} of {s:,} source records kept ({sh:.1%})"
                  for k, s, sh in zip(thin["kept_rows"], thin["source_rows"], thin["share"])],
    })
    return CheckResult(
        "sales.borough_coverage", "nyc_property_sales", "completeness", "warning",
        f"each borough keeps at least {min_share:.0%} of its source records",
        rows_checked=len(coverage), threshold=1.0, exceptions=exceptions)


STATISTICAL_CHECKS = [return_outliers, price_per_sqft_outliers, borough_coverage]


def run_all_checks(conn: sqlite3.Connection) -> list[CheckResult]:
    results = [rule.run(conn) for rule in RULE_CHECKS]
    results += [check(conn) for check in STATISTICAL_CHECKS]
    return results
