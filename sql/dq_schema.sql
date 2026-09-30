-- ============================================================================
-- Data-quality results model
--
-- Created by scripts/08_run_data_quality.py (CREATE IF NOT EXISTS), so results
-- accumulate across runs and quality can be compared run over run. These
-- tables are deliberately NOT in schema.sql: reloading the data must not wipe
-- the history of how good the data was.
--
--   dq_runs        one row per run: when, on what data, overall result
--   dq_results     one row per check per run: the scorecard
--   dq_exceptions  one row per flagged record: the review queue
-- ============================================================================

CREATE TABLE IF NOT EXISTS dq_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL,      -- ISO timestamp, UTC
    data_source     TEXT,               -- 'real' or 'demo', copied from pipeline_meta
    checks_run      INTEGER NOT NULL,
    checks_passed   INTEGER NOT NULL,
    checks_warned   INTEGER NOT NULL,
    checks_failed   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dq_results (
    run_id          INTEGER NOT NULL REFERENCES dq_runs(run_id),
    check_id        TEXT NOT NULL,      -- e.g. 'prices.ohlc_consistent'
    table_name      TEXT NOT NULL,
    dimension       TEXT NOT NULL,      -- completeness | validity | uniqueness | consistency | accuracy
    severity        TEXT NOT NULL,      -- critical (fails the run) | warning (flags for review)
    description     TEXT NOT NULL,
    rows_checked    INTEGER NOT NULL,
    rows_failed     INTEGER NOT NULL,
    pass_rate       REAL NOT NULL,      -- 1 - rows_failed / rows_checked
    threshold       REAL NOT NULL,      -- minimum pass_rate for the data to be fit for use
    status          TEXT NOT NULL,      -- PASS | WARN | FAIL
    PRIMARY KEY (run_id, check_id)
);

CREATE TABLE IF NOT EXISTS dq_exceptions (
    run_id          INTEGER NOT NULL REFERENCES dq_runs(run_id),
    check_id        TEXT NOT NULL,
    record_key      TEXT NOT NULL,      -- sale_id, 'TICKER YYYY-MM-DD', borough, ...
    value           TEXT,               -- the offending value, for the reviewer
    FOREIGN KEY (run_id, check_id) REFERENCES dq_results(run_id, check_id)
);

CREATE INDEX IF NOT EXISTS idx_dq_exceptions_run_check
    ON dq_exceptions(run_id, check_id);

-- Remediation: an analysis-ready view of property sales that excludes the
-- sales the latest run flagged as having an implausible price per square
-- foot. Price analysis reads this view; the flagged rows stay in the table
-- for review instead of being deleted.
DROP VIEW IF EXISTS v_sales_analysis_ready;
CREATE VIEW v_sales_analysis_ready AS
SELECT s.*
FROM nyc_property_sales s
WHERE CAST(s.sale_id AS TEXT) NOT IN (
    SELECT e.record_key
    FROM dq_exceptions e
    WHERE e.run_id = (SELECT MAX(run_id) FROM dq_runs)
      AND e.check_id = 'sales.price_per_sqft_outliers'
);
