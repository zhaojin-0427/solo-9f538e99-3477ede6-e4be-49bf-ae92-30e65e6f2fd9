"""SQLite connection management and schema.

Concurrency model
-----------------
* The database runs in WAL mode so readers (queries) never block a writer
  (batch submission) and vice versa.
* Every write batch runs inside a single ``BEGIN IMMEDIATE`` transaction.
  Either the whole batch is committed or nothing changes ("failure keeps the
  old chain").  Readers always see a fully committed snapshot, never a mix of
  revisions -- including after a process restart, because uncommitted data is
  never persisted.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = {busy_timeout};

CREATE TABLE IF NOT EXISTS waybills (
    id              TEXT PRIMARY KEY,
    origin_party    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- One row per (scan_id, revision).  A scan's adopted state is the row with
-- the highest revision for that scan_id; withdrawn rows are tombstones.
CREATE TABLE IF NOT EXISTS scans (
    waybill_id      TEXT NOT NULL REFERENCES waybills(id),
    scan_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL CHECK (revision > 0),
    event_type      TEXT NOT NULL CHECK (event_type IN ('HANDOVER', 'WITHDRAWAL')),
    ts_utc          TEXT,
    giver           TEXT,
    receiver        TEXT,
    payload_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (waybill_id, scan_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_scans_waybill ON scans (waybill_id, scan_id, revision);

-- Daily settlement bills.  A bill is immutable once written: one row per
-- ended UTC service day, issued strictly in consecutive-day order.
CREATE TABLE IF NOT EXISTS bills (
    service_date    TEXT PRIMARY KEY,       -- YYYY-MM-DD (UTC service day)
    sequence        INTEGER NOT NULL UNIQUE,
    issued_at       TEXT NOT NULL
);

-- Frozen bill content.  CUSTODY lines are the day's own per-waybill /
-- per-custodian durations; ADJUSTMENT lines carry a signed delta for a
-- service day billed earlier.
CREATE TABLE IF NOT EXISTS bill_lines (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    service_date        TEXT NOT NULL REFERENCES bills(service_date),
    kind                TEXT NOT NULL CHECK (kind IN ('CUSTODY', 'ADJUSTMENT')),
    waybill_id          TEXT NOT NULL,
    custodian           TEXT NOT NULL,
    origin_service_date TEXT,
    duration_seconds    INTEGER NOT NULL,
    CHECK (
        (kind = 'CUSTODY'    AND origin_service_date IS NULL) OR
        (kind = 'ADJUSTMENT' AND origin_service_date IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_bill_lines_bill
    ON bill_lines (service_date, id);

-- Cumulative amount already billed per (service day, waybill, custodian).
-- Adjustments move these values to the recomputed truth so that a later
-- bill never back-bills the same delta twice.
CREATE TABLE IF NOT EXISTS billed_totals (
    service_date    TEXT NOT NULL,
    waybill_id      TEXT NOT NULL,
    custodian       TEXT NOT NULL,
    seconds         INTEGER NOT NULL,
    PRIMARY KEY (service_date, waybill_id, custodian)
);

-- Small key/value store.  ``change_seq`` is bumped by every committed scan
-- batch and every issued bill; billing uses it as an optimistic token so a
-- long read-only aggregation never blocks incoming scans.
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with sane, chain-integrity-preserving pragmas."""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # we manage transactions explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create the schema (idempotent)."""
    conn = connect(db_path)
    try:
        conn.executescript(
            _SCHEMA.format(busy_timeout=config.BUSY_TIMEOUT_MS)
        )
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize writers with BEGIN IMMEDIATE.

    Any exception rolls the whole batch back, leaving the previously
    committed chain untouched.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


# --------------------------------------------------------------------------- #
# Optimistic change token
# --------------------------------------------------------------------------- #
def change_seq(conn: sqlite3.Connection) -> int:
    """Current global change sequence (0 if never bumped)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'change_seq'").fetchone()
    return int(row["value"]) if row is not None else 0


def bump_change_seq(conn: sqlite3.Connection) -> int:
    """Increment the change sequence inside the caller's transaction."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('change_seq', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
    )
    return change_seq(conn)
