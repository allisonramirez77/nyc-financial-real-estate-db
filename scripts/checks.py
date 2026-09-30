"""
Data-quality guardrails.

Call these at every pipeline boundary — after each transform and each load.
They raise DataQualityError on failure so a bad run stops instead of writing
partial or wrong data. Every check also logs, so the run history in
logs/pipeline.log shows what was verified, not just what was executed.

Usage:
    from checks import expect_rows, expect_no_nulls, expect_unique, expect_fk

    expect_rows(df, min_rows=1000, label="daily_prices")
    expect_no_nulls(df, ["ticker", "date"], label="daily_prices")
    expect_unique(df, ["ticker", "date"], label="daily_prices")
"""

from __future__ import annotations

import logging
from typing import Sequence

log = logging.getLogger(__name__)


class DataQualityError(AssertionError):
    """Raised when a pipeline boundary check fails. Stops the run."""


def expect_rows(df, min_rows: int = 1, label: str = "frame") -> None:
    """Guard against a silently empty result — the most common pipeline bug."""
    n = len(df)
    if n < min_rows:
        raise DataQualityError(f"{label}: expected >= {min_rows} rows, got {n}")
    log.info("check ok  %-22s %d rows", label, n)


def expect_no_nulls(df, cols: Sequence[str], label: str = "frame") -> None:
    """Null join keys silently drop rows in every join downstream."""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise DataQualityError(f"{label}: missing columns {missing}")
    bad = {c: int(df[c].isna().sum()) for c in cols if df[c].isna().any()}
    if bad:
        raise DataQualityError(f"{label}: nulls in key columns {bad}")
    log.info("check ok  %-22s no nulls in %s", label, list(cols))


def expect_unique(df, cols: Sequence[str], label: str = "frame") -> None:
    """The natural key must be unique or the loader is not idempotent."""
    dupes = int(df.duplicated(subset=list(cols)).sum())
    if dupes:
        sample = df[df.duplicated(subset=list(cols), keep=False)].head(3)
        raise DataQualityError(
            f"{label}: {dupes} duplicate rows on {list(cols)}\n{sample}"
        )
    log.info("check ok  %-22s unique on %s", label, list(cols))


def expect_numeric(df, cols: Sequence[str], label: str = "frame") -> None:
    """Catches '$1,200' / '-' placeholders arriving as object dtype."""
    import pandas as pd

    bad = [c for c in cols if not pd.api.types.is_numeric_dtype(df[c])]
    if bad:
        raise DataQualityError(
            f"{label}: expected numeric dtype for {bad}; "
            "coerce with pd.to_numeric(..., errors='coerce') and count the NaNs"
        )
    log.info("check ok  %-22s numeric %s", label, list(cols))


def expect_fk(child_df, child_col: str, parent_df, parent_col: str,
              label: str = "fk") -> None:
    """No orphan foreign keys — an inner join returning 0 rows is a bug."""
    orphans = set(child_df[child_col].unique()) - set(parent_df[parent_col].unique())
    if orphans:
        raise DataQualityError(
            f"{label}: {len(orphans)} {child_col} values not in parent "
            f"{parent_col}: {sorted(orphans)[:10]}"
        )
    log.info("check ok  %-22s all %s present in parent", label, child_col)


def reconcile(before: int, after: int, label: str, allow_drop: bool = True) -> int:
    """
    Log row counts across a transform. Returns the number dropped.

    Every intentional drop is a data-quality finding and is logged with
    its count, so no row disappears without a recorded reason.
    """
    dropped = before - after
    pct = 100 * dropped / before if before else 0.0
    log.info("reconcile %-22s %d -> %d  (dropped %d, %.1f%%)",
             label, before, after, dropped, pct)
    if dropped < 0:
        raise DataQualityError(f"{label}: row count grew {before} -> {after}")
    if dropped and not allow_drop:
        raise DataQualityError(f"{label}: unexpected drop of {dropped} rows")
    return dropped
