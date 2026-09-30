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
