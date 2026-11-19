"""End-to-end HTTP smoke test against a running uvicorn instance."""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:" + os.environ.get("SMOKE_PORT", "8011")


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, payload


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), label, extra)
    if not cond:
        sys.exit(1)


st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-SMOKE", "origin_party": "ORIG"})
check("create waybill", st == 201, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T09:00:00Z", "giver": "B", "receiver": "C"},
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "Y"},
]})
check("batch accepted", st == 200, st)
check("GAP detected", body["chain"]["break_reason"] == "GAP")
check("custodian stays at C", body["chain"]["current_custodian"] == "C")
check("S3 unverified", [s["scan_id"] for s in body["chain"]["unverified_scans"]] == ["S3"])

# same revision divergence -> 409, old chain preserved
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "TAMPERED"},
]})
check("same-revision divergence -> 409", st == 409, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 2, "event_type": "WITHDRAWAL"},
]})
check("withdrawal accepted", st == 200, body)
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 3, "ts_utc": "2026-11-18T10:00:00Z", "giver": "C", "receiver": "D"},
]})
check("correction accepted", st == 200, st)
check("chain healed", body["chain"]["chain_intact"] is True)
check("custodian now D", body["chain"]["current_custodian"] == "D")

# simultaneous timestamps
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S4", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "D", "receiver": "E"},
    {"scan_id": "S5", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "E", "receiver": "F"},
]})
check("simultaneous flagged", body["chain"]["break_reason"] == "SIMULTANEOUS")

st, body = call("GET", "/api/waybills/AWB-SMOKE/chain")
check("GET chain", st == 200 and body["waybill_id"] == "AWB-SMOKE")

st, body = call("GET", "/api/waybills")
check("list waybills", st == 200 and any(w["id"] == "AWB-SMOKE" for w in body["waybills"]))

st, body = call("GET", "/docs", raw=True)
check("swagger docs reachable", st == 200 and b"swagger" in body.lower())
st, body = call("GET", "/openapi.json")
check("openapi schema reachable", st == 200 and "paths" in body)
st, body = call("GET", "/", raw=True)
check("console page reachable", st == 200 and b"<!DOCTYPE html>" in body)

# validation: naive datetime
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S9", "revision": 1, "ts_utc": "2026-11-18T12:00:00", "giver": "D", "receiver": "E"},
]})
check("naive timestamp -> 422", st == 422)

# daily settlement bills
st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-BILL", "origin_party": "ORIG"})
check("create bill waybill", st == 201, body)
call("POST", "/api/waybills/AWB-BILL/scans/batch", {"scans": [
    {"scan_id": "B1", "revision": 1, "ts_utc": "2026-01-01T08:00:00Z", "giver": "ORIG", "receiver": "A"},
    {"scan_id": "B2", "revision": 1, "ts_utc": "2026-01-01T20:00:00Z", "giver": "A", "receiver": "B"},
]})
st, body = call("POST", "/api/bills", {"service_date": "2026-01-01"})
check("issue bill", st == 200, body)
check("custody belongs to earlier receiver", body["custody"][0]["custodian"] == "A")
check("custody duration counted", body["custody"][0]["duration_seconds"] == 12 * 3600)
st, retry = call("POST", "/api/bills", {"service_date": "2026-01-01"})
check("same-day retry returns original", st == 200 and retry == body)
st, skipped = call("POST", "/api/bills", {"service_date": "2026-01-03"})
check("skipped day -> 409", st == 409, skipped)
st, missing = call("GET", "/api/bills/2026-01-03")
check("failed issue leaves no bill", st == 404, missing)
call("POST", "/api/waybills/AWB-BILL/scans/batch", {"scans": [
    {"scan_id": "B2", "revision": 2, "ts_utc": "2026-01-01T18:00:00Z", "giver": "A", "receiver": "B"},
]})
st, day2 = call("POST", "/api/bills", {"service_date": "2026-01-02"})
check("next day issued", st == 200, day2)
adj = [a for a in day2["adjustments"] if a["custodian"] == "A"]
check("late scan becomes signed adjustment",
      len(adj) == 1 and adj[0]["duration_seconds"] == -2 * 3600
      and adj[0]["origin_service_date"] == "2026-01-01", day2["adjustments"])
st, orig = call("GET", "/api/bills/2026-01-01")
check("original bill frozen", st == 200 and orig == body)
st, listing = call("GET", "/api/bills")
check("bill list", st == 200 and len(listing) == 2)

print("ALL SMOKE CHECKS PASSED")
