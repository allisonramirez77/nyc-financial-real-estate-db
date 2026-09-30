"""
Tests for the data-quality checks, on tiny hand-built tables where the right
answer is known in advance. A check that never fires is indistinguishable
from a check that is broken, so each test plants a defect and expects it
to be caught.

Run:  python -m pytest tests/
"""
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from dq_rules import RULE_CHECKS, CheckResult, robust_z, return_outliers

SCHEMA = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text()
RULES = {rule.check_id: rule for rule in RULE_CHECKS}


@pytest.fixture
def conn():
    """An empty in-memory copy of the real schema."""
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    c.execute("INSERT INTO companies (ticker, name, sector) VALUES ('AAA', 'Test Co', 'Tech')")
    yield c
    c.close()


def add_prices(conn, rows):
    conn.executemany(
        "INSERT INTO daily_prices (ticker, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def test_robust_z_is_not_fooled_by_the_outlier_itself():
    values = pd.Series([1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 50.0])
    z = robust_z(values)
    assert z.iloc[-1] > 10                  # the outlier stands out clearly
    assert z.iloc[:-1].abs().max() < 2      # ordinary values stay ordinary


def test_robust_z_of_a_constant_series_is_zero():
    assert (robust_z(pd.Series([5.0, 5.0, 5.0])) == 0).all()


def test_empty_table_is_not_a_clean_table():
    result = CheckResult("x", "t", "completeness", "critical", "", rows_checked=0,
                         threshold=1.0, exceptions=pd.DataFrame(columns=["record_key", "value"]))
    assert result.pass_rate == 0.0
    assert result.status == "FAIL"


def test_ohlc_rule_catches_high_below_close(conn):
    add_prices(conn, [
        ("AAA", "2017-01-03", 10, 11, 9, 10.5, 100),   # fine
        ("AAA", "2017-01-04", 10, 10.2, 9, 10.8, 100), # high < close: impossible
    ])
    result = RULES["prices.ohlc_consistent"].run(conn)
    assert result.rows_failed == 1
    assert result.exceptions.record_key.iloc[0] == "AAA 2017-01-04"
    assert result.status == "FAIL"


def test_calendar_rule_catches_a_missing_day(conn):
    conn.execute("INSERT INTO companies (ticker, name, sector) VALUES ('BBB', 'Other Co', 'Tech')")
    add_prices(conn, [
        ("AAA", "2017-01-03", 10, 11, 9, 10, 100),
        ("AAA", "2017-01-04", 10, 11, 9, 10, 100),
        ("BBB", "2017-01-03", 10, 11, 9, 10, 100),   # BBB has no 2017-01-04
    ])
    result = RULES["prices.full_trading_calendar"].run(conn)
    assert result.rows_checked == 4                  # 2 tickers x 2 days
    assert list(result.exceptions.record_key) == ["BBB 2017-01-04"]


def test_returns_reconciliation_catches_a_wrong_return(conn):
    add_prices(conn, [
        ("AAA", "2017-01-03", 10, 11, 9, 10.0, 100),
        ("AAA", "2017-01-04", 10, 11, 9, 11.0, 100),   # true return is +10%
    ])
    conn.executemany("INSERT INTO daily_returns (ticker, date, daily_return) VALUES (?, ?, ?)",
                     [("AAA", "2017-01-03", None), ("AAA", "2017-01-04", 0.12)])
    result = RULES["returns.reconcile_to_prices"].run(conn)
    assert list(result.exceptions.record_key) == ["AAA 2017-01-04"]


def test_return_outlier_is_flagged_and_normal_days_are_not(conn):
    returns = [0.001, -0.002, 0.0015, -0.001, 0.002, -0.0015, 0.0005, 0.25]  # last: +25%
    conn.executemany("INSERT INTO daily_returns (ticker, date, daily_return) VALUES (?, ?, ?)",
                     [("AAA", f"2017-01-{d:02d}", r) for d, r in enumerate(returns, start=2)])
    result = return_outliers(conn)
    assert list(result.exceptions.record_key) == ["AAA 2017-01-09"]
