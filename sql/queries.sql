-- ============================================================================
-- Query portfolio — NYC Financial & Real Estate Data Pipeline
--
-- Each query leads with the business question it answers; the SQL is the
-- evidence. Data-quality queries live in sql/dq_queries.sql.
--
-- Window: 2016-09-01 .. 2017-08-31 (set by the DOF rolling sales export)
-- Run:  sqlite3 data/processed/financial_re.db < sql/queries.sql
-- ============================================================================


-- ---------------------------------------------------------------- SECTION 1
-- JOINS — connecting the fact tables to the company dimension
-- ---------------------------------------------------------------------------

-- Q1. What did each NYC-headquartered company close at on the last trading day?
--     Basic star-schema join: fact table (prices) to dimension (companies).
SELECT c.name, c.sector, p.date, p.close
FROM daily_prices p
JOIN companies c ON c.ticker = p.ticker
WHERE c.is_nyc_hq = 1
  AND p.date = (SELECT MAX(date) FROM daily_prices)
ORDER BY p.close DESC;


-- Q2. How did the three NYC REITs perform end to end?
--     Self-join on first/last close per ticker — the shape of every
--     "change over a period" question.
WITH bounds AS (
    SELECT ticker, MIN(date) AS first_day, MAX(date) AS last_day
    FROM daily_prices GROUP BY ticker
)
SELECT c.name,
       ROUND(f.close, 2) AS open_price,
       ROUND(l.close, 2) AS close_price,
       ROUND(100.0 * (l.close - f.close) / f.close, 2) AS pct_change
FROM bounds b
JOIN companies    c ON c.ticker = b.ticker
JOIN daily_prices f ON f.ticker = b.ticker AND f.date = b.first_day
JOIN daily_prices l ON l.ticker = b.ticker AND l.date = b.last_day
WHERE c.sector = 'Real Estate'
ORDER BY pct_change DESC;


-- ---------------------------------------------------------------- SECTION 2
-- AGGREGATION
-- ---------------------------------------------------------------------------

-- Q3. Which sectors of NYC-headquartered companies gained, and which lost?
--     AVG over daily returns; the ×100 converts to percent for readability.
SELECT c.sector,
       COUNT(DISTINCT c.ticker)                AS tickers,
       ROUND(AVG(r.daily_return) * 100, 4)     AS avg_daily_return_pct,
       ROUND(AVG(r.daily_return) * 252 * 100, 2) AS annualized_pct
FROM daily_returns r
JOIN companies c ON c.ticker = r.ticker
WHERE r.daily_return IS NOT NULL AND c.is_nyc_hq = 1
GROUP BY c.sector
ORDER BY avg_daily_return_pct DESC;


-- Q4. Was SL Green's decline steady or concentrated in particular months?
--     Monthly aggregation reveals timing that an endpoint comparison hides.
SELECT substr(r.date, 1, 7)                    AS month,
       ROUND(AVG(r.daily_return) * 100, 4)     AS avg_daily_return_pct,
       ROUND(MIN(r.daily_return) * 100, 2)     AS worst_day_pct,
       ROUND(MAX(r.daily_return) * 100, 2)     AS best_day_pct
FROM daily_returns r
WHERE r.ticker = 'SLG' AND r.daily_return IS NOT NULL
GROUP BY month
ORDER BY month;


-- ---------------------------------------------------------------- SECTION 3
-- WINDOW FUNCTIONS
-- ---------------------------------------------------------------------------

-- Q5. Where did each REIT's short-term trend cross its long-term trend?
--     PARTITION BY keeps each rolling window inside one ticker — without it
--     the average leaks across companies at the boundary and no error is raised.
SELECT ticker, date, ROUND(close, 2) AS close,
       ROUND(AVG(close) OVER w7,  2) AS ma_7,
       ROUND(AVG(close) OVER w30, 2) AS ma_30,
       CASE WHEN AVG(close) OVER w7 > AVG(close) OVER w30
            THEN 'above' ELSE 'below' END      AS short_vs_long
FROM daily_prices
WHERE ticker IN ('SLG', 'VNO', 'ESRT')
WINDOW w7  AS (PARTITION BY ticker ORDER BY date ROWS BETWEEN 6  PRECEDING AND CURRENT ROW),
       w30 AS (PARTITION BY ticker ORDER BY date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
ORDER BY ticker, date;


-- Q6. Rank every ticker by cumulative return over the full window.
--     FIRST_VALUE gives each row its own baseline; RANK orders the result.
WITH indexed AS (
    SELECT ticker, date, close,
           FIRST_VALUE(close) OVER (PARTITION BY ticker ORDER BY date) AS base
    FROM daily_prices
)
SELECT i.ticker, c.name,
       ROUND(100.0 * (i.close - i.base) / i.base, 2) AS cumulative_return_pct,
       RANK() OVER (ORDER BY (i.close - i.base) / i.base DESC) AS rank
FROM indexed i
JOIN companies c ON c.ticker = i.ticker
WHERE i.date = (SELECT MAX(date) FROM daily_prices)
ORDER BY rank;


-- ---------------------------------------------------------------- SECTION 4
-- DATA QUALITY — these queries exist to prove the data is trustworthy
-- ---------------------------------------------------------------------------

-- Q7. Are there duplicate (ticker, date) rows?
--     Should return zero rows. The schema's UNIQUE constraint enforces this;
--     the query is how you *verify* the constraint did its job.
SELECT ticker, date, COUNT(*) AS copies
FROM daily_prices
GROUP BY ticker, date
HAVING COUNT(*) > 1;


-- Q8. Does every ticker share the same trading calendar?
--     A ticker with fewer days means a halt, a late listing, or a truncated
--     file — any of which quietly distorts sector averages.
SELECT ticker, COUNT(*) AS trading_days,
       MIN(date) AS first_day, MAX(date) AS last_day
FROM daily_prices
GROUP BY ticker
ORDER BY trading_days;


-- Q9. What did the arm's-length filter actually remove, and was it even?
--     Reads lineage recorded at load time, because the table itself only ever
--     contains surviving rows — you cannot count what was never inserted.
SELECT key, value FROM pipeline_meta
WHERE key IN ('data_source', 'source_rows', 'kept_rows', 'dropped_rows', 'duplicate_rows');


-- ---------------------------------------------------------------- SECTION 5
-- NYC REAL ESTATE / URBAN DEVELOPMENT
-- ---------------------------------------------------------------------------

-- Q10. What does a square foot cost in each borough — and how much of each
--      borough's market does that number actually represent?
--      The count column is the caveat: Manhattan's figure rests on few sales
--      because condos and co-ops do not report square footage.
SELECT borough,
       COUNT(*)                                       AS sales,
       ROUND(AVG(sale_price / gross_sqft))            AS avg_psf,
       ROUND(AVG(sale_price))                         AS avg_price,
       ROUND(AVG(2017 - year_built))                  AS avg_building_age
FROM nyc_property_sales
WHERE year_built > 1800
GROUP BY borough
ORDER BY avg_psf DESC;


-- Q11. Is transaction volume rising or falling across the year?
--      Volume is the better development signal; price is noisier and lags.
SELECT substr(sale_date, 1, 7) AS month,
       COUNT(*)                                  AS sales,
       ROUND(AVG(sale_price / gross_sqft))       AS avg_psf
FROM nyc_property_sales
GROUP BY month
ORDER BY month;


-- Q12. Which neighborhoods are the busiest, and are they cheap or expensive?
--      HAVING filters on the aggregate so thin neighborhoods can't top the
--      list on two unrepresentative sales.
SELECT borough, neighborhood,
       COUNT(*)                             AS sales,
       ROUND(AVG(sale_price / gross_sqft))  AS avg_psf
FROM nyc_property_sales
GROUP BY borough, neighborhood
HAVING COUNT(*) >= 100
ORDER BY sales DESC
LIMIT 15;


-- Q13. Where is new construction actually happening?
--      Post-2000 building share is a direct proxy for development activity,
--      and it tells a different story than price does.
SELECT borough,
       COUNT(*)                                                            AS total_sales,
       SUM(CASE WHEN year_built >= 2000 THEN 1 ELSE 0 END)                 AS built_since_2000,
       ROUND(100.0 * SUM(CASE WHEN year_built >= 2000 THEN 1 ELSE 0 END)
             / COUNT(*), 1)                                                AS pct_new,
       ROUND(AVG(CASE WHEN year_built >= 2000
                      THEN sale_price / gross_sqft END))                   AS new_psf,
       ROUND(AVG(CASE WHEN year_built BETWEEN 1801 AND 1949
                      THEN sale_price / gross_sqft END))                   AS prewar_psf
FROM nyc_property_sales
WHERE year_built > 1800
GROUP BY borough
ORDER BY pct_new DESC;


-- ---------------------------------------------------------------- SECTION 6
-- THE CENTERPIECE — cross-dataset comparison
-- ---------------------------------------------------------------------------

-- Q14. Do NYC REIT stocks track NYC property prices?
--      Both series are indexed to 100 at the first month so they share ONE
--      axis. A dual-axis chart would invent a correlation that isn't there.
WITH reit_monthly AS (
    SELECT substr(date, 1, 7) AS month, AVG(close) AS v
    FROM daily_prices WHERE ticker IN ('SLG', 'VNO', 'ESRT')
    GROUP BY month
),
prop_monthly AS (
    SELECT substr(sale_date, 1, 7) AS month, AVG(sale_price / gross_sqft) AS v
    FROM nyc_property_sales
    GROUP BY month
),
baselines AS (
    SELECT (SELECT v FROM reit_monthly ORDER BY month LIMIT 1) AS reit_base,
           (SELECT v FROM prop_monthly ORDER BY month LIMIT 1) AS prop_base
)
SELECT r.month,
       ROUND(100.0 * r.v / b.reit_base, 1) AS reit_index,
       ROUND(100.0 * p.v / b.prop_base, 1) AS property_index,
       ROUND(100.0 * p.v / b.prop_base
             - 100.0 * r.v / b.reit_base, 1) AS gap
FROM reit_monthly r
JOIN prop_monthly p ON p.month = r.month
CROSS JOIN baselines b
ORDER BY r.month;


-- ---------------------------------------------------------------- SECTION 7
-- MARKET EVENTS — what the flagged price moves say about the market
--
-- The outlier check in dq_rules.py flags daily moves that are extreme for
-- their stock. These queries ask what caused each one, and what separates a
-- real event from a bad print. Run after 08_run_data_quality.py.
-- ---------------------------------------------------------------------------

-- Q15. What caused each flagged move, and was it the stock or the market?
--      The event list was checked against news coverage and company earnings
--      releases (sources in the README). Each event is scoped: to one ticker,
--      to one sector, or (both NULL) to every stock that day, so a sector
--      selloff can't explain a move in a different sector. volume_x compares
--      the day's volume with the stock's previous 20 days. others_pct is the
--      average move of the other 12 tickers that day, so stock_specific_pct
--      is the part of the move the rest of the basket doesn't explain.
WITH events (date, ticker, sector, kind, what) AS (VALUES
    ('2016-10-06', 'AXP',  NULL, 'analyst',  'Nomura downgrade to Reduce'),
    ('2016-10-20', 'AXP',  NULL, 'earnings', 'Q3 2016 beat, guidance raised'),
    ('2016-10-21', 'MSFT', NULL, 'earnings', 'FY17 Q1, Azure revenue +116%'),
    ('2016-11-09', NULL,   NULL, 'market',   'US election: banks on deregulation, drug makers on price curbs receding'),
    ('2016-11-10', NULL,   NULL, 'market',   'Post-election rally, second day'),
    ('2017-01-17', NULL,   'Financials',
                                 'sector',   'Trump calls the dollar too strong; financials -2%'),
    ('2017-01-24', 'VZ',   NULL, 'earnings', 'Q4 2016 miss on EPS and subscribers'),
    ('2017-02-01', 'AAPL', NULL, 'earnings', 'Q1 FY17 record revenue'),
    ('2017-04-20', 'AXP',  NULL, 'earnings', 'Q1 2017 beat, guidance reaffirmed'),
    ('2017-05-17', NULL,   NULL, 'market',   'Comey memo selloff, worst day since Sep 2016'),
    ('2017-06-09', NULL,   'Information Technology',
                                 'sector',   'Technology selloff'),
    ('2017-07-27', 'VZ',   NULL, 'earnings', 'Q2 2017, first full quarter of unlimited plans'),
    ('2017-08-02', 'AAPL', NULL, 'earnings', 'Q3 FY17 beat')
),
flags AS (
    SELECT substr(record_key, 1, instr(record_key, ' ') - 1)  AS ticker,
           substr(record_key, instr(record_key, ' ') + 1)     AS date
    FROM dq_exceptions
    WHERE run_id = (SELECT MAX(run_id) FROM dq_runs)
      AND check_id = 'prices.return_outliers'
),
volume_vs_normal AS (
    SELECT ticker, date,
           volume / AVG(volume) OVER (PARTITION BY ticker ORDER BY date
                                      ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ratio
    FROM daily_prices
),
market AS (
    SELECT date, SUM(daily_return) AS total, COUNT(daily_return) AS n
    FROM daily_returns GROUP BY date
)
SELECT f.date, f.ticker,
       COALESCE(e.kind, 'UNEXPLAINED')                          AS kind,
       e.what,
       ROUND(100 * r.daily_return, 2)                            AS move_pct,
       ROUND(v.ratio, 1)                                         AS volume_x,
       ROUND(100 * (m.total - r.daily_return) / (m.n - 1), 2)    AS others_pct,
       ROUND(100 * (r.daily_return
                    - (m.total - r.daily_return) / (m.n - 1)), 2) AS stock_specific_pct
FROM flags f
JOIN daily_returns    r ON r.ticker = f.ticker AND r.date = f.date
JOIN volume_vs_normal v ON v.ticker = f.ticker AND v.date = f.date
JOIN market           m ON m.date = f.date
JOIN companies        c ON c.ticker = f.ticker
LEFT JOIN events      e ON e.date = f.date
                       AND (e.ticker = f.ticker
                            OR e.sector = c.sector
                            OR (e.ticker IS NULL AND e.sector IS NULL))
ORDER BY f.date, f.ticker;


-- Q16. Do real events trade on heavy volume?
--      A bad print changes one number in one file; nobody traded on it. A
--      real event brings buyers and sellers. If flagged moves carry far more
--      volume than ordinary days, volume is the second signal that tells an
--      event from a data error.
WITH volume_vs_normal AS (
    SELECT ticker, date,
           volume / AVG(volume) OVER (PARTITION BY ticker ORDER BY date
                                      ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ratio
    FROM daily_prices
),
flags AS (
    SELECT record_key FROM dq_exceptions
    WHERE run_id = (SELECT MAX(run_id) FROM dq_runs)
      AND check_id = 'prices.return_outliers'
)
SELECT CASE WHEN v.ticker || ' ' || v.date IN (SELECT record_key FROM flags)
            THEN 'flagged move' ELSE 'every other day' END             AS days,
       COUNT(*)                                                       AS n,
       ROUND(AVG(v.ratio), 2)                                         AS avg_volume_x,
       ROUND(100.0 * SUM(v.ratio >= 2) / COUNT(*), 1)                 AS pct_at_2x_or_more
FROM volume_vs_normal v
WHERE v.ratio IS NOT NULL
GROUP BY days;


-- Q17. When did the NYC REITs lose value, and did they react to the election
--      the way the NYC banks did?
--      Close-to-close returns per ticker, averaged by sector. (Q14 averages
--      monthly closes instead, which is why its full-window REIT figure
--      differs from the last row here.)
WITH periods (label, start_day, end_day) AS (VALUES
    ('1 before the election',  '2016-09-01', '2016-11-08'),
    ('2 election to year end', '2016-11-08', '2016-12-30'),
    ('3 2017',                 '2016-12-30', '2017-08-31'),
    ('4 two days after',       '2016-11-08', '2016-11-10'),
    ('5 full window',          '2016-09-01', '2017-08-31')
)
SELECT c.sector, pd.label,
       ROUND(AVG(100.0 * (e.close - s.close) / s.close), 1) AS avg_return_pct
FROM periods pd
JOIN daily_prices s ON s.date = pd.start_day
JOIN daily_prices e ON e.date = pd.end_day AND e.ticker = s.ticker
JOIN companies    c ON c.ticker = s.ticker
WHERE c.sector IN ('Real Estate', 'Financials')
GROUP BY c.sector, pd.label
ORDER BY c.sector, pd.label;


-- Q18. Do the NYC REITs trade like NYC financial stocks, or as their own group?
--      Pearson correlation of daily returns, written out from its definition
--      because SQLite has no CORR(). Each REIT is paired with every other
--      ticker, then averaged by that ticker's sector.
WITH pairs AS (
    SELECT a.ticker AS x_ticker, b.ticker AS y_ticker,
           a.daily_return AS x, b.daily_return AS y
    FROM daily_returns a
    JOIN daily_returns b ON b.date = a.date AND b.ticker != a.ticker
    WHERE a.ticker IN ('SLG', 'VNO', 'ESRT')
      AND a.daily_return IS NOT NULL AND b.daily_return IS NOT NULL
),
pair_correlations AS (
    SELECT x_ticker, y_ticker,
           (AVG(x * y) - AVG(x) * AVG(y))
           / sqrt((AVG(x * x) - AVG(x) * AVG(x)) * (AVG(y * y) - AVG(y) * AVG(y))) AS corr
    FROM pairs
    GROUP BY x_ticker, y_ticker
)
SELECT p.x_ticker AS reit, c.sector AS vs_sector,
       ROUND(AVG(p.corr), 2) AS avg_correlation
FROM pair_correlations p
JOIN companies c ON c.ticker = p.y_ticker
GROUP BY p.x_ticker, c.sector
ORDER BY p.x_ticker, avg_correlation DESC;


-- Q19. Month to month, do REIT prices and property prices move together?
--      Correlating price levels would mostly measure the two trends over the
--      year, so this correlates monthly percentage changes instead, for all
--      sales and for v_sales_analysis_ready (flagged $/sqft outliers removed).
--      With 11 monthly changes, a correlation needs to exceed about 0.60 to be
--      statistically significant at the 5% level.
WITH reit AS (
    SELECT substr(date, 1, 7) AS month, AVG(close) AS v
    FROM daily_prices WHERE ticker IN ('SLG', 'VNO', 'ESRT')
    GROUP BY month
),
prop_all AS (
    SELECT substr(sale_date, 1, 7) AS month, AVG(sale_price / gross_sqft) AS v
    FROM nyc_property_sales GROUP BY month
),
prop_clean AS (
    SELECT substr(sale_date, 1, 7) AS month, AVG(sale_price / gross_sqft) AS v
    FROM v_sales_analysis_ready GROUP BY month
),
changes AS (
    SELECT r.month,
           r.v / LAG(r.v) OVER (ORDER BY r.month) - 1 AS reit_change,
           a.v / LAG(a.v) OVER (ORDER BY r.month) - 1 AS all_change,
           c.v / LAG(c.v) OVER (ORDER BY r.month) - 1 AS clean_change
    FROM reit r
    JOIN prop_all   a ON a.month = r.month
    JOIN prop_clean c ON c.month = r.month
),
pairs AS (
    SELECT 'all sales' AS property_series, reit_change AS x, all_change AS y
    FROM changes WHERE reit_change IS NOT NULL
    UNION ALL
    SELECT 'quality flags removed', reit_change, clean_change
    FROM changes WHERE reit_change IS NOT NULL
)
SELECT property_series,
       COUNT(*) AS monthly_changes,
       ROUND((AVG(x * y) - AVG(x) * AVG(y))
             / sqrt((AVG(x * x) - AVG(x) * AVG(x)) * (AVG(y * y) - AVG(y) * AVG(y))), 2)
           AS correlation
FROM pairs
GROUP BY property_series;


-- Q20. What kind of property do the qualifying sales actually represent?
--      The REITs own mostly Manhattan office buildings. If the sales that
--      survive the square-footage filter are mostly outer-borough houses, the
--      two sides of Q14 are measuring different markets.
SELECT ROUND(100.0 * SUM(borough != 'Manhattan') / COUNT(*), 1)         AS pct_outside_manhattan,
       ROUND(100.0 * SUM(borough != 'Manhattan'
                         AND (building_class_category LIKE '01 %'
                              OR building_class_category LIKE '02 %')) / COUNT(*), 1)
                                                                         AS pct_outer_borough_1_2_family,
       SUM(building_class_category LIKE '%OFFICE%')                      AS office_building_sales
FROM nyc_property_sales;
