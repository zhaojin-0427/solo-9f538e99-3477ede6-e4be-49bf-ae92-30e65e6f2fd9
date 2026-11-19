"""Daily settlement bills (日结账单).

Rules implemented here
----------------------
* A bill covers exactly one **ended** UTC service day.  The first bill may be
  issued for any ended day; every following bill must be the next calendar
  day.  Re-issuing a day that already has a bill returns the *original*
  frozen document unchanged.
* Custody time is counted only between two **adjacent trusted handovers**
  (the verified prefix computed by the chain service).  The interval belongs
  to the receiver of the earlier handover and is split at UTC midnight.
* Everything already billed is tracked cumulatively per
  ``(service_date, waybill, custodian)``.  When a later bill is issued, each
  previously billed day is recomputed against that cumulative truth; any
  non-zero delta becomes a signed ``ADJUSTMENT`` line carrying the original
  service day, so nothing is back-billed twice.
* Issuance runs in one ``BEGIN IMMEDIATE`` transaction.  Scans submitted
  before the transaction starts are therefore fully included; scans racing
  with it are serialized and land either wholly before or wholly after the
  snapshot.  If the write lock cannot be acquired the call fails with a
  conflict and **no bill rows are written**, leaving all pending deltas for a
  retry.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .database import bump_change_seq, change_seq, transaction
from .schemas import utcnow_iso
from .services import DomainConflict, DomainValidation, compute_chain


# --------------------------------------------------------------------------- #
# Wire models
# --------------------------------------------------------------------------- #
class BillIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_date: date = Field(
        validation_alias=AliasChoices(
            "service_date", "date", "bill_date", "business_date"
        ),
        description="已结束的 UTC 服务日，格式 YYYY-MM-DD",
    )


class CustodyLine(BaseModel):
    waybill_id: str
    custodian: str
    duration_seconds: int


class AdjustmentLine(BaseModel):
    origin_service_date: date
    waybill_id: str
    custodian: str
    duration_seconds: int = Field(description="带正负号的差额秒数")


class BillResponse(BaseModel):
    service_date: date
    sequence: int
    issued_at: datetime
    custody: list[CustodyLine]
    adjustments: list[AdjustmentLine]
    custody_total_seconds: int
    adjustment_total_seconds: int
    total_seconds: int


class BillSummary(BaseModel):
    service_date: date
    sequence: int
    issued_at: datetime
    custody_total_seconds: int
    adjustment_total_seconds: int
    total_seconds: int


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_day(text: str) -> date:
    return date.fromisoformat(text)


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _custody_truth(
    conn: sqlite3.Connection, day: date
) -> dict[tuple[str, str], int]:
    """Recompute the day's per-(waybill, custodian) custody seconds.

    Only adjacent pairs inside each waybill's verified prefix contribute; the
    interval belongs to the receiver of the earlier handover and is clipped to
    the UTC day.
    """
    start = _day_start(day)
    end = start + timedelta(days=1)

    totals: dict[tuple[str, str], int] = {}
    waybills = conn.execute("SELECT id FROM waybills ORDER BY id").fetchall()
    for wb in waybills:
        prefix = compute_chain(conn, wb["id"]).verified_prefix
        for earlier, later in zip(prefix, prefix[1:]):
            assert earlier.ts_utc is not None and later.ts_utc is not None
            seg_start = max(earlier.ts_utc, start)
            seg_end = min(later.ts_utc, end)
            if seg_end <= seg_start:
                continue
            seconds = int((seg_end - seg_start).total_seconds())
            if seconds <= 0:
                continue
            key = (wb["id"], earlier.receiver or "")
            totals[key] = totals.get(key, 0) + seconds
    return totals


def _read_billed_totals(
    conn: sqlite3.Connection, day: date
) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        "SELECT waybill_id, custodian, seconds FROM billed_totals "
        "WHERE service_date = ?",
        (day.isoformat(),),
    ).fetchall()
    return {(r["waybill_id"], r["custodian"]): r["seconds"] for r in rows}


def _replace_billed_totals(
    conn: sqlite3.Connection, day: date, truth: dict[tuple[str, str], int]
) -> None:
    conn.execute("DELETE FROM billed_totals WHERE service_date = ?", (day.isoformat(),))
    conn.executemany(
        "INSERT INTO billed_totals (service_date, waybill_id, custodian, seconds) "
        "VALUES (?, ?, ?, ?)",
        [(day.isoformat(), wb, cust, secs) for (wb, cust), secs in truth.items()],
    )


def _load_bill(conn: sqlite3.Connection, service_date: str) -> BillResponse:
    head = conn.execute(
        "SELECT service_date, sequence, issued_at FROM bills WHERE service_date = ?",
        (service_date,),
    ).fetchone()
    if head is None:  # pragma: no cover - callers guarantee existence
        raise DomainValidation(f"bill for {service_date!r} not found")

    rows = conn.execute(
        "SELECT kind, waybill_id, custodian, origin_service_date, duration_seconds "
        "FROM bill_lines WHERE service_date = ? ORDER BY id",
        (service_date,),
    ).fetchall()

    custody = [
        CustodyLine(
            waybill_id=r["waybill_id"],
            custodian=r["custodian"],
            duration_seconds=r["duration_seconds"],
        )
        for r in rows
        if r["kind"] == "CUSTODY"
    ]
    adjustments = [
        AdjustmentLine(
            origin_service_date=_parse_day(r["origin_service_date"]),
            waybill_id=r["waybill_id"],
            custodian=r["custodian"],
            duration_seconds=r["duration_seconds"],
        )
        for r in rows
        if r["kind"] == "ADJUSTMENT"
    ]

    custody_total = sum(line.duration_seconds for line in custody)
    adjustment_total = sum(line.duration_seconds for line in adjustments)
    return BillResponse(
        service_date=_parse_day(head["service_date"]),
        sequence=head["sequence"],
        issued_at=datetime.fromisoformat(head["issued_at"]),
        custody=custody,
        adjustments=adjustments,
        custody_total_seconds=custody_total,
        adjustment_total_seconds=adjustment_total,
        total_seconds=custody_total + adjustment_total,
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def issue_bill(conn: sqlite3.Connection, service_date: date) -> BillResponse:
    """Issue (or return the frozen original of) one daily bill.

    Two phases keep incoming scans flowing during a long aggregation:

    1. Read-only snapshot: recompute every previously billed day and today's
       truth, plus the ``change_seq`` token they were computed at.
    2. Short write transaction: re-check the token.  If any scan was committed
       in between, abort with a retryable conflict and write nothing; otherwise
       persist the frozen bill atomically.
    """
    today = datetime.now(timezone.utc).date()
    if service_date >= today:
        raise DomainConflict(
            f"service_date {service_date.isoformat()} has not ended yet "
            f"(current UTC date is {today.isoformat()})"
        )

    for _attempt in range(3):
        prepared = _prepare(conn, service_date)
        if prepared is None:  # already billed -> return the frozen original
            return _load_bill(conn, service_date.isoformat())
        try:
            if _commit_bill(conn, service_date, *prepared):
                return _load_bill(conn, service_date.isoformat())
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
        # Truth moved while aggregating (or the lock was busy): retry.
    raise DomainConflict(
        "billing keeps racing with concurrent scan submissions; retry"
    )


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _prepare(
    conn: sqlite3.Connection, service_date: date
) -> tuple[
    int,
    int,
    list[tuple[str, str, str, int]],
    list[tuple[str, dict[tuple[str, str], int]]],
    dict[tuple[str, str], int],
] | None:
    """Phase 1: read-only snapshot of everything the bill will contain.

    Returns ``None`` when the day already has a bill.  The snapshot is a
    consistent read (a single statement stream against WAL) and carries the
    ``change_seq`` token it was built at.
    """
    existing = conn.execute(
        "SELECT 1 FROM bills WHERE service_date = ?", (service_date.isoformat(),)
    ).fetchone()
    if existing is not None:
        return None

    latest = conn.execute(
        "SELECT service_date, sequence FROM bills ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        sequence = 1
    else:
        expected = _parse_day(latest["service_date"]) + timedelta(days=1)
        if service_date != expected:
            raise DomainConflict(
                "bills must be issued day by day: expected "
                f"{expected.isoformat()} after {latest['service_date']}, "
                f"got {service_date.isoformat()}"
            )
        sequence = latest["sequence"] + 1

    token = change_seq(conn)

    # Recompute every previously billed day (and today) from the trusted
    # chain, then derive signed deltas against what was actually billed.
    adjustments: list[tuple[str, str, str, int]] = []
    prior_rows = conn.execute(
        "SELECT service_date FROM bills ORDER BY sequence"
    ).fetchall()
    prior_truth: list[tuple[str, dict[tuple[str, str], int]]] = []
    for row in prior_rows:
        prior_day = _parse_day(row["service_date"])
        truth = _custody_truth(conn, prior_day)
        billed = _read_billed_totals(conn, prior_day)
        for key in sorted(set(truth) | set(billed)):
            delta = truth.get(key, 0) - billed.get(key, 0)
            if delta:
                adjustments.append(
                    (prior_day.isoformat(), key[0], key[1], delta)
                )
        prior_truth.append((row["service_date"], truth))

    today_truth = _custody_truth(conn, service_date)
    return token, sequence, adjustments, prior_truth, today_truth


def _commit_bill(
    conn: sqlite3.Connection,
    service_date: date,
    token: int,
    sequence: int,
    adjustments: list[tuple[str, str, str, int]],
    prior_truth: list[tuple[str, dict[tuple[str, str], int]]],
    today_truth: dict[tuple[str, str], int],
) -> bool:
    """Phase 2: short BEGIN IMMEDIATE that persists the prepared snapshot.

    Returns False (writing nothing) when the change token moved, i.e. a scan
    committed after the snapshot was taken.
    """
    with transaction(conn):
        if change_seq(conn) != token:
            return False

        conn.execute(
            "INSERT INTO bills (service_date, sequence, issued_at) VALUES (?, ?, ?)",
            (service_date.isoformat(), sequence, utcnow_iso()),
        )

        line_rows = [
            (service_date.isoformat(), "ADJUSTMENT", wb, cust, origin_day, secs)
            for (origin_day, wb, cust, secs) in adjustments
        ]
        line_rows.extend(
            (service_date.isoformat(), "CUSTODY", wb, cust, None, secs)
            for (wb, cust), secs in today_truth.items()
        )
        conn.executemany(
            "INSERT INTO bill_lines (service_date, kind, waybill_id, custodian, "
            "origin_service_date, duration_seconds) VALUES (?, ?, ?, ?, ?, ?)",
            line_rows,
        )
        # Cumulative truth for every billed day now matches this snapshot.
        for day_text, truth in prior_truth:
            _replace_billed_totals(conn, _parse_day(day_text), truth)
        _replace_billed_totals(conn, service_date, today_truth)
        bump_change_seq(conn)
    return True


def get_bill(conn: sqlite3.Connection, service_date: date) -> BillResponse:
    row = conn.execute(
        "SELECT service_date FROM bills WHERE service_date = ?",
        (service_date.isoformat(),),
    ).fetchone()
    if row is None:
        raise DomainValidation(f"bill for {service_date.isoformat()!r} not found")
    return _load_bill(conn, service_date.isoformat())


def list_bills(conn: sqlite3.Connection) -> list[BillSummary]:
    rows = conn.execute(
        "SELECT b.service_date, b.sequence, b.issued_at, "
        "       COALESCE(SUM(CASE WHEN l.kind = 'CUSTODY' THEN l.duration_seconds END), 0) "
        "         AS custody_total, "
        "       COALESCE(SUM(CASE WHEN l.kind = 'ADJUSTMENT' THEN l.duration_seconds END), 0) "
        "         AS adjustment_total "
        "FROM bills AS b LEFT JOIN bill_lines AS l ON l.service_date = b.service_date "
        "GROUP BY b.service_date, b.sequence, b.issued_at "
        "ORDER BY b.sequence"
    ).fetchall()
    return [
        BillSummary(
            service_date=_parse_day(r["service_date"]),
            sequence=r["sequence"],
            issued_at=datetime.fromisoformat(r["issued_at"]),
            custody_total_seconds=r["custody_total"],
            adjustment_total_seconds=r["adjustment_total"],
            total_seconds=r["custody_total"] + r["adjustment_total"],
        )
        for r in rows
    ]


__all__ = [
    "AdjustmentLine",
    "BillIssueRequest",
    "BillResponse",
    "BillSummary",
    "CustodyLine",
    "get_bill",
    "issue_bill",
    "list_bills",
]
