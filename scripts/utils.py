"""
Shared helpers for the pipeline scripts: logging setup and a DB connection
helper. Import from here instead of repeating setup in every script.
"""

import logging
import os
import sqlite3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "processed", "financial_re.db")
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "pipeline.log")


def setup_logging(script_name: str) -> logging.Logger:
    """
    Call this at the top of every script:
        logger = setup_logging(__name__)
        logger.info("Starting ingest...")

    Writes to both the console and logs/pipeline.log, so every run leaves
    a permanent record.
    """
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    # Configure the ROOT logger, not just this module's. Helper modules such
    # as checks.py use logging.getLogger(__name__); without handlers on the
    # root they run silently and you lose every guardrail line.
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

        file_handler = logging.FileHandler(LOG_PATH)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logging.getLogger(script_name)


def get_connection() -> sqlite3.Connection:
    """
    Returns a SQLite connection to the processed database.
    Phase 2: swap this out for a SQLAlchemy engine pointed at RDS —
    keep the function signature/usage the same in the other scripts
    so the swap is a one-file change.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)
