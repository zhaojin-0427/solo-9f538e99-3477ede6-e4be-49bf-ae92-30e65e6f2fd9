"""Daily settlement bills: custody accounting, adjustments, immutability."""
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import handover, make_waybill


def issue(client, day: str):
    return client.post("/api/bills", json={"service_date": day})


def get_bill(client, day: str):
    return client.get(f"/api/bills/{day}")


def lines_by_kind(bill: dict, kind: str):
    return bill["custody"] if kind == "CUSTODY" else bill["adjustments"]


def find(lines, waybill="AWB-1", custodian=None, origin=None):
    out = [
        line
        for line in lines
        if line["waybill_id"] == waybill
        and (custodian is None or line["custodian"] == custodian)
        and (origin is None or str(line["origin_service_date"]) == origin)
    ]
    assert len(out) == 1, out
    return out[0]


def test_bill_counts_interval_between_adjacent_trusted_handovers(client):
    """Only time between two verified handovers counts, and it belongs to the
    receiver of the earlier one."""
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
        handover("S3", 1, "2026-01-01T13:30:00Z", "B", "C"),
    ]})

    r = issue(client, "2026-01-01")
    assert r.status_code == 200, r.text
    bill = r.json()

    # 08:00-10:00 belongs to A, 10:00-13:30 belongs to B.
    assert find(bill["custody"], custodian="A")["duration_seconds"] == 2 * 3600
    assert find(bill["custody"], custodian="B")["duration_seconds"] == 3.5 * 3600
    # No line for the final receiver C: no adjacent later handover yet.
    assert all(line["custodian"] != "C" for line in bill["custody"])
    assert bill["adjustments"] == []
    assert bill["total_seconds"] == 5.5 * 3600


def test_interval_split_at_utc_midnight(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T22:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-02T02:00:00Z", "A", "B"),
    ]})

    day1 = issue(client, "2026-01-01").json()
    assert find(day1["custody"], custodian="A")["duration_seconds"] == 2 * 3600

    day2 = issue(client, "2026-01-02").json()
    assert find(day2["custody"], custodian="A")["duration_seconds"] == 2 * 3600


def test_untrusted_tail_does_not_accrue_custody_time(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "WRONG", "B"),  # GAP
    ]})
    bill = issue(client, "2026-01-01").json()
    assert bill["custody"] == []
    assert bill["total_seconds"] == 0


def test_first_day_free_then_day_by_day_required(client):
    make_waybill(client)

    # The first bill may start on any ended day.
    assert issue(client, "2026-01-05").status_code == 200
    # ...but afterwards days must be consecutive.
    r = issue(client, "2026-01-07")
    assert r.status_code == 409
    assert "2026-01-06" in r.json()["detail"]
    assert get_bill(client, "2026-01-07").status_code == 404

    assert issue(client, "2026-01-06").status_code == 200


def test_future_or_today_rejected(client):
    make_waybill(client)
    today = datetime.now(timezone.utc).date()
    assert issue(client, today.isoformat()).status_code == 409
    assert issue(client, (today + timedelta(days=1)).isoformat()).status_code == 409


def test_same_day_retry_returns_original_unchanged(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
    ]})
    first = issue(client, "2026-01-01")
    assert first.status_code == 200
    original = first.json()

    # A late correction that would change the day's own truth...
    client.post("/api/waybills/AWB-1/scans/batch", json={
        "scans": [handover("S2", 2, "2026-01-01T12:00:00Z", "A", "B")]
    })

    # ...does not rewrite the frozen bill; a retry returns it byte-for-byte.
    again = issue(client, "2026-01-01")
    assert again.status_code == 200
    assert again.json() == original
    assert get_bill(client, "2026-01-01").json() == original


def test_late_scan_on_billed_day_becomes_signed_adjustment(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
    ]})
    original = issue(client, "2026-01-01").json()
    assert find(original["custody"], custodian="A")["duration_seconds"] == 7200

    # A late scan arrives after settlement: A's custody shrinks by one hour.
    client.post("/api/waybills/AWB-1/scans/batch", json={
        "scans": [handover("S2", 2, "2026-01-01T09:00:00Z", "A", "B")]
    })

    r = issue(client, "2026-01-02")
    assert r.status_code == 200, r.text
    bill = r.json()
    assert bill["custody"] == []  # nothing new on day 2
    adj = find(bill["adjustments"], custodian="A", origin="2026-01-01")
    assert adj["duration_seconds"] == -3600
    assert bill["adjustment_total_seconds"] == -3600
    assert bill["total_seconds"] == -3600

    # The original bill is untouched.
    assert get_bill(client, "2026-01-01").json() == original


def test_adjustment_not_back_billed_twice(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
    ]})
    issue(client, "2026-01-01")
    client.post("/api/waybills/AWB-1/scans/batch", json={
        "scans": [handover("S2", 2, "2026-01-01T09:00:00Z", "A", "B")]
    })

    day2 = issue(client, "2026-01-02").json()
    assert find(day2["adjustments"], custodian="A")["duration_seconds"] == -3600

    # Day 3: the same delta must not be repeated.
    day3 = issue(client, "2026-01-03").json()
    assert day3["adjustments"] == []
    assert day3["total_seconds"] == 0


def test_adjustment_carries_origin_waybill_custodian_and_sign(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
    ]})
    issue(client, "2026-01-01")

    # Backfill S0 and re-time S1/S2; in the rebuilt chain Z newly holds
    # 09:00-10:00 while A now only holds 10:00-11:00 (was 08:00-10:00).
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S0", 1, "2026-01-01T09:00:00Z", "ORIG", "Z"),
        handover("S1", 2, "2026-01-01T10:00:00Z", "Z", "A"),
        handover("S2", 2, "2026-01-01T11:00:00Z", "A", "B"),
    ]})

    bill = issue(client, "2026-01-02").json()
    adj_a = find(bill["adjustments"], custodian="A", origin="2026-01-01")
    assert adj_a["duration_seconds"] == -3600  # A only holds 10:00-11:00 now
    assert find(bill["adjustments"], custodian="Z", origin="2026-01-01")[
        "duration_seconds"
    ] == 3600  # newly seen 09:00-10:00 interval
    assert bill["adjustment_total_seconds"] == 0  # net zero, both signs present
    for line in bill["adjustments"]:
        assert line["origin_service_date"] == "2026-01-01"
        assert line["waybill_id"] == "AWB-1"
        assert line["custodian"]


def test_bill_spans_all_waybills(client):
    make_waybill(client, wid="AWB-1", origin="ORIG")
    make_waybill(client, wid="AWB-2", origin="ORIG2")
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("A1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("A2", 1, "2026-01-01T09:00:00Z", "A", "B"),
    ]})
    client.post("/api/waybills/AWB-2/scans/batch", json={"scans": [
        handover("B1", 1, "2026-01-01T08:00:00Z", "ORIG2", "X"),
        handover("B2", 1, "2026-01-01T10:00:00Z", "X", "Y"),
    ]})

    bill = issue(client, "2026-01-01").json()
    assert find(bill["custody"], waybill="AWB-1", custodian="A")["duration_seconds"] == 3600
    assert find(bill["custody"], waybill="AWB-2", custodian="X")["duration_seconds"] == 7200
    assert bill["total_seconds"] == 3 * 3600


def test_list_and_get_after_restart(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T09:00:00Z", "A", "B"),
    ]})
    original = issue(client, "2026-01-01").json()

    listing = client.get("/api/bills").json()
    assert [b["service_date"] for b in listing] == ["2026-01-01"]
    assert listing[0]["sequence"] == 1

    # Simulate a restart with a fresh app against the same database file.
    import importlib

    from fastapi.testclient import TestClient

    from app import main

    importlib.reload(main)
    with TestClient(main.app) as fresh:
        assert fresh.get("/api/bills/2026-01-01").json() == original
        assert issue(fresh, "2026-01-01").json() == original


def test_failed_issue_leaves_no_bill_and_delta_pending(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
    ]})
    issue(client, "2026-01-01")
    client.post("/api/waybills/AWB-1/scans/batch", json={
        "scans": [handover("S2", 2, "2026-01-01T09:00:00Z", "A", "B")]
    })

    # A gap in the sequence fails atomically: no bill row for the skipped day.
    assert issue(client, "2026-01-04").status_code == 409
    assert get_bill(client, "2026-01-04").status_code == 404
    assert len(client.get("/api/bills").json()) == 1

    # The pending -1h delta is still there for the legitimate next day.
    day2 = issue(client, "2026-01-02").json()
    assert find(day2["adjustments"], custodian="A")["duration_seconds"] == -3600


def test_bill_lines_ordered_and_deterministic(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
    ]})
    b1 = issue(client, "2026-01-01").json()
    b2 = issue(client, "2026-01-01").json()
    assert b1 == b2


def test_bad_service_date_format_rejected(client):
    assert client.post("/api/bills", json={"service_date": "01/01/2026"}).status_code == 422
    assert client.post("/api/bills", json={"service_date": "2026-13-01"}).status_code == 422
    assert client.post("/api/bills", json={}).status_code == 422


def test_scan_racing_with_settlement_is_not_lost(client):
    """Scans submitted during settlement land either in this bill or in the
    next day's adjustment -- never silently dropped."""
    import threading

    from app import config, database, schemas, services

    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T10:00:00Z", "A", "B"),
    ]})
    db_path = config.DB_PATH
    done = threading.Event()

    def late_writer():
        conn = database.connect(db_path)
        try:
            services.apply_batch(conn, "AWB-1", schemas.BatchSubmit(scans=[
                schemas.ScanEntry(
                    scan_id="S2", revision=2,
                    ts_utc=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
                    giver="A", receiver="B",
                )
            ]))
        finally:
            conn.close()
            done.set()

    t = threading.Thread(target=late_writer)
    t.start()
    first = issue(client, "2026-01-01")
    t.join(timeout=30)
    assert done.is_set()

    # A concurrent-write conflict is retryable and leaves no partial bill.
    if first.status_code == 409:
        assert get_bill(client, "2026-01-01").status_code == 404
        first = issue(client, "2026-01-01")
    assert first.status_code == 200, first.text

    day1 = first.json()
    # Either the late correction was included, or it will arrive as adjustment.
    a_secs = sum(
        l["duration_seconds"] for l in day1["custody"] if l["custodian"] == "A"
    )
    assert a_secs in (3600, 7200)

    day2 = issue(client, "2026-01-02").json()
    a_adjust = sum(
        l["duration_seconds"]
        for l in day2["adjustments"]
        if l["custodian"] == "A"
    )
    # Settled truth eventually matches the chain: A held 08:00-09:00.
    assert a_secs + a_adjust == 3600


def test_issue_via_path_alias(client):
    make_waybill(client)
    client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [
        handover("S1", 1, "2026-01-01T08:00:00Z", "ORIG", "A"),
        handover("S2", 1, "2026-01-01T09:00:00Z", "A", "B"),
    ]})
    r = client.post("/api/bills/2026-01-01")
    assert r.status_code == 200, r.text
    assert r.json() == issue(client, "2026-01-01").json()
    assert client.post("/api/bills/2026-01-02").status_code == 200
