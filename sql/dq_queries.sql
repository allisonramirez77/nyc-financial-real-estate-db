-- ============================================================================
-- Data-quality queries — run after scripts/08_run_data_quality.py
--
-- The DQ layer stores three things: dq_runs (one row per run), dq_results
-- (the scorecard) and dq_exceptions (the review queue). These queries read
-- them. Every query targets the latest run unless it says otherwise.
--
-- Run:  sqlite3 data/processed/financial_re.db < sql/dq_queries.sql
-- ============================================================================


-- DQ1. How did the latest run go?
SELECT run_id, run_at, data_source,
       checks_run, checks_passed, checks_warned, checks_failed
FROM dq_runs
ORDER BY run_id DESC
LIMIT 1;


-- DQ2. The scorecard, worst first.
--      FAIL before WARN before PASS, then lowest pass rate first.
SELECT status, check_id, dimension, rows_checked, rows_failed,
       ROUND(100 * pass_rate, 2) AS pass_pct,
       ROUND(100 * threshold, 1) AS threshold_pct
FROM dq_results
WHERE run_id = (SELECT MAX(run_id) FROM dq_runs)
ORDER BY CASE status WHEN 'FAIL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         pass_rate;


-- DQ3. Which quality dimension is weakest?
SELECT dimension,
       COUNT(*)                            AS checks,
       ROUND(100 * AVG(pass_rate), 2)      AS avg_pass_pct,
       SUM(CASE WHEN status != 'PASS' THEN 1 ELSE 0 END) AS not_passing
FROM dq_results
WHERE run_id = (SELECT MAX(run_id) FROM dq_runs)
GROUP BY dimension
ORDER BY avg_pass_pct;


-- DQ4. Are the extreme stock moves data errors or real events?
--      If several unrelated tickers spike on the same day, that is a market
--      event, not a bad print. Grouping the flags by date shows which.
SELECT substr(record_key, instr(record_key, ' ') + 1)           AS trade_date,
       COUNT(*)                                                 AS tickers_flagged,
       GROUP_CONCAT(substr(record_key, 1, instr(record_key, ' ') - 1)
                    || ' ' || value, '; ')                      AS moves
FROM dq_exceptions
WHERE run_id = (SELECT MAX(run_id) FROM dq_runs)
  AND check_id = 'prices.return_outliers'
GROUP BY trade_date
ORDER BY tickers_flagged DESC, trade_date;


-- DQ5. Review queue: the sales with implausible price per square foot,
--      with the context a reviewer needs to judge them.
SELECT e.value AS price_per_sqft,
       s.borough, s.neighborhood, s.building_class_category,
       s.sale_price, s.gross_sqft, s.sale_date, s.block, s.lot
FROM dq_exceptions e
JOIN nyc_property_sales s ON CAST(s.sale_id AS TEXT) = e.record_key
WHERE e.run_id = (SELECT MAX(run_id) FROM dq_runs)
  AND e.check_id = 'sales.price_per_sqft_outliers'
ORDER BY s.sale_price / s.gross_sqft DESC
LIMIT 25;


-- DQ6. Which building classes produce the most implausible prices?
--      Where flags concentrate is where the source data needs attention.
SELECT s.building_class_category,
       COUNT(*)                                       AS sales,
       SUM(CASE WHEN e.record_key IS NOT NULL THEN 1 ELSE 0 END) AS flagged,
       ROUND(100.0 * SUM(CASE WHEN e.record_key IS NOT NULL THEN 1 ELSE 0 END)
             / COUNT(*), 1)                           AS flagged_pct
FROM nyc_property_sales s
LEFT JOIN dq_exceptions e
       ON e.record_key = CAST(s.sale_id AS TEXT)
      AND e.check_id = 'sales.price_per_sqft_outliers'
      AND e.run_id = (SELECT MAX(run_id) FROM dq_runs)
GROUP BY s.building_class_category
HAVING COUNT(*) >= 100
ORDER BY flagged_pct DESC
LIMIT 10;


-- DQ7. Does the headline finding survive the cleanup?
--      Q14 in queries.sql rerun on v_sales_analysis_ready, which excludes the
--      flagged $/sqft outliers. If the story changes, the finding was an
--      artifact of bad records; if it holds, it is robust.
WITH reit_monthly AS (
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
)
SELECT r.month,
       ROUND(100.0 * r.v / (SELECT v FROM reit_monthly ORDER BY month LIMIT 1), 1) AS reit_index,
       ROUND(100.0 * a.v / (SELECT v FROM prop_all     ORDER BY month LIMIT 1), 1) AS property_index_all,
       ROUND(100.0 * c.v / (SELECT v FROM prop_clean   ORDER BY month LIMIT 1), 1) AS property_index_clean
FROM reit_monthly r
JOIN prop_all   a ON a.month = r.month
JOIN prop_clean c ON c.month = r.month
ORDER BY r.month;


-- DQ8. Is quality improving run over run? (All runs, not just the latest.)
SELECT r.run_id, r.run_at, d.check_id, d.rows_failed,
       ROUND(100 * d.pass_rate, 2) AS pass_pct, d.status
FROM dq_results d
JOIN dq_runs r ON r.run_id = d.run_id
ORDER BY d.check_id, r.run_id;


-- DQ9. Are the implausible $/sqft sales too cheap or too expensive, and do
--      any look like portfolio sales?
--      A portfolio sale is recorded on every lot it covers, so the same price
--      and date appear on several lots in the borough. Side is judged against
--      the average $/sqft of the sale's peer group (borough + building class).
WITH flagged AS (
    SELECT CAST(record_key AS INTEGER) AS sale_id
    FROM dq_exceptions
    WHERE run_id = (SELECT MAX(run_id) FROM dq_runs)
      AND check_id = 'sales.price_per_sqft_outliers'
),
peers AS (
    SELECT borough, building_class_category, AVG(sale_price / gross_sqft) AS avg_psf
    FROM nyc_property_sales
    GROUP BY borough, building_class_category
),
described AS (
    SELECT s.sale_price / s.gross_sqft > p.avg_psf AS above_peers,
           EXISTS (SELECT 1 FROM nyc_property_sales o
                   WHERE o.borough = s.borough AND o.sale_date = s.sale_date
                     AND o.sale_price = s.sale_price AND o.sale_id != s.sale_id)
               AS shares_price_with_other_lots
    FROM flagged f
    JOIN nyc_property_sales s ON s.sale_id = f.sale_id
    JOIN peers p ON p.borough = s.borough
                AND p.building_class_category = s.building_class_category
)
SELECT CASE WHEN above_peers THEN 'too expensive' ELSE 'too cheap' END AS side,
       COUNT(*)                          AS flagged,
       SUM(shares_price_with_other_lots) AS same_price_on_other_lots
FROM described
GROUP BY side;
