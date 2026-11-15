"""improvements.md: tamper-evident audit chain, signed integrity receipts, background scrubber."""

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import audit_chain
import receipts
import scrubber
from conftest import SETTINGS

DATA = b"raport avarie " * 400
SIM = {"Authorization": "Bearer sim-token"}


def run(coro):
    return asyncio.run(coro)


def upload(client, auth, data=DATA, category="outage_records", name="avarie.pdf"):
    return client.post("/api/v1/ingest", headers=auth, files={"file": (name, data)},
                       data={"category": category})


def deps_of(client):
    return client.app.state.deps


def set_scrub(client, **changes):
    deps = deps_of(client)
    deps.settings = dataclasses.replace(deps.settings, **changes)


# =============================== audit chain ===============================

def three_events(client, auth):
    fid = upload(client, auth).json()["file_id"]                       # upload
    client.get(f"/api/v1/files/{fid}/download", headers=auth)          # download
    client.get(f"/api/v1/files/{fid}/verify", headers=auth)            # verify
    return fid


def test_an_empty_chain_is_valid(client, admin):
    body = client.get("/api/v1/audit/verify", headers=admin).json()
    assert body == {"valid": True, "checked": 0, "head_hash": None, "first_break": None}


def test_the_chain_is_valid_after_normal_activity(client, auth, admin):
    three_events(client, auth)
    body = client.get("/api/v1/audit/verify", headers=admin).json()
    assert body["valid"] is True and body["checked"] == 3 and body["first_break"] is None
    assert len(body["head_hash"]) == 64


def test_the_head_hash_moves_with_every_event(client, auth, admin):
    fid = upload(client, auth).json()["file_id"]
    before = client.get("/api/v1/audit/verify", headers=admin).json()["head_hash"]
    client.get(f"/api/v1/files/{fid}/download", headers=auth)
    after = client.get("/api/v1/audit/verify", headers=admin).json()["head_hash"]
    assert before != after


def test_editing_a_row_is_detected(client, auth, admin, repo):
    three_events(client, auth)
    repo.chain[1]["username"] = "intruder@example.com"  # someone rewrites who downloaded
    body = client.get("/api/v1/audit/verify", headers=admin).json()
    assert body["valid"] is False
    assert body["first_break"]["reason"] == "row_hash_mismatch"
    assert body["first_break"]["seq"] == 2


def test_hiding_a_blocked_download_is_detected(client, auth, admin, repo, storage):
    fid = upload(client, auth).json()["file_id"]
    storage.objects[next(iter(storage.objects))] = b"damaged"
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).status_code == 409
    blocked = next(r for r in repo.chain if r["action"] == "download_blocked")
    blocked["details"] = "Descărcare reușită"  # rewrite history to hide the incident
    assert client.get("/api/v1/audit/verify", headers=admin).json()["valid"] is False


def test_deleting_a_row_is_detected(client, auth, admin, repo):
    three_events(client, auth)
    del repo.chain[1]
    body = client.get("/api/v1/audit/verify", headers=admin).json()
    assert body["valid"] is False and body["first_break"]["reason"] == "chain_broken"


def test_reordering_rows_is_detected(client, auth, admin, repo):
    three_events(client, auth)
    repo.chain[0], repo.chain[1] = repo.chain[1], repo.chain[0]
    assert client.get("/api/v1/audit/verify", headers=admin).json()["valid"] is False


def test_a_forged_row_appended_with_a_made_up_hash_is_detected(client, auth, admin, repo):
    three_events(client, auth)
    repo.chain.append({**repo.chain[-1], "seq": 4, "action": "download",
                       "prev_hash": repo.chain[-1]["row_hash"], "row_hash": "f" * 64})
    body = client.get("/api/v1/audit/verify", headers=admin).json()
    assert body["first_break"]["reason"] == "row_hash_mismatch"


def test_only_an_admin_can_verify_the_chain(client, auth):
    assert client.get("/api/v1/audit/verify", headers=auth).status_code == 403
    assert client.get("/api/v1/audit/verify", headers=SIM).status_code == 403
    assert client.get("/api/v1/audit/verify").status_code == 401


def test_list_audit_output_is_unchanged_by_the_chain(client, auth):
    fid = three_events(client, auth)
    rows = client.get(f"/api/v1/files/{fid}/audit", headers=auth).json()
    assert all("prev_hash" not in r and "row_hash" not in r for r in rows)


BASE_ROW = {"id": "i1", "file_id": "f1", "action": "upload", "username": "u@ppc.ro",
            "trace_id": "t1", "details": "detalii",
            "created_at": datetime(2026, 11, 15, tzinfo=timezone.utc)}


@pytest.mark.parametrize("field,value", [
    ("id", "i2"), ("file_id", "f2"), ("action", "download"), ("username", "x@ppc.ro"),
    ("trace_id", "t2"), ("details", "altceva"), ("details", None),
    ("created_at", datetime(2026, 11, 16, tzinfo=timezone.utc)),
])
def test_every_field_is_covered_by_the_hash(field, value):
    base = audit_chain.row_hash(audit_chain.GENESIS, BASE_ROW)
    if field == "details" and value is None:
        assert audit_chain.row_hash(audit_chain.GENESIS, {**BASE_ROW, field: value}) != base
    else:
        assert audit_chain.row_hash(audit_chain.GENESIS, {**BASE_ROW, field: value}) != base


def test_the_previous_hash_is_covered_too():
    assert audit_chain.row_hash("a" * 64, BASE_ROW) != audit_chain.row_hash("b" * 64, BASE_ROW)


def test_romanian_text_and_missing_details_hash_fine():
    h = audit_chain.row_hash(audit_chain.GENESIS, {**BASE_ROW, "details": "Descărcare BLOCATĂ ș ț ă â î"})
    assert len(h) == 64
    assert audit_chain.row_hash(audit_chain.GENESIS, {**BASE_ROW, "details": None}) == \
        audit_chain.row_hash(audit_chain.GENESIS, {**BASE_ROW, "details": ""})


def test_a_timestamp_hashes_the_same_as_its_iso_string():
    as_text = BASE_ROW["created_at"].isoformat(timespec="microseconds")
    assert audit_chain.row_hash("0" * 64, BASE_ROW) == \
        audit_chain.row_hash("0" * 64, {**BASE_ROW, "created_at": as_text})


def test_timestamps_in_other_zones_hash_the_same_instant():
    bucharest = BASE_ROW["created_at"].astimezone(timezone(timedelta(hours=2)))
    assert audit_chain.row_hash("0" * 64, BASE_ROW) == \
        audit_chain.row_hash("0" * 64, {**BASE_ROW, "created_at": bucharest})


# =============================== receipts ===============================

def get_receipt(client, auth, fid, **params):
    return client.get(f"/api/v1/files/{fid}/receipt", headers=auth, params=params)


def test_a_receipt_describes_the_file_and_verifies_against_the_public_key(client, auth):
    fid = upload(client, auth).json()["file_id"]
    r = get_receipt(client, auth, fid)
    assert r.status_code == 200
    rec = r.json()
    f = rec["payload"]["file"]
    assert (f["id"], f["category"], f["storage"], f["size_bytes"]) == (fid, "outage_records", "s3", len(DATA))
    assert f["sha256"] == hashlib.sha256(DATA).hexdigest()
    ver = rec["payload"]["verification"]
    assert ver["performed"] is True and ver["ok"] is True
    # an outside party needs nothing but the published key
    pub = client.get("/api/v1/receipts/public-key").json()  # no login
    assert pub["alg"] == "Ed25519" and len(base64.b64decode(pub["public_key"])) == 32
    assert receipts.verify_with_public_key(pub["public_key"], rec) is True
    assert rec["key_id"] == pub["key_id"]


def test_the_server_also_verifies_a_receipt_without_a_login(client, auth):
    fid = upload(client, auth).json()["file_id"]
    rec = get_receipt(client, auth, fid).json()
    ok = client.post("/api/v1/receipts/verify", content=json.dumps(rec))
    assert ok.status_code == 200 and ok.json() == {"valid": True, "reason": "ok"}


@pytest.mark.parametrize("edit", [
    lambda p: p["file"].__setitem__("size_bytes", 1),
    lambda p: p["file"].__setitem__("sha256", "0" * 64),
    lambda p: p["file"].__setitem__("storage", "ftp"),
    lambda p: p["verification"].__setitem__("ok", False),
    lambda p: p.__setitem__("issued_at", "2020-01-01T00:00:00+00:00"),
])
def test_a_changed_receipt_no_longer_verifies(client, auth, edit):
    fid = upload(client, auth).json()["file_id"]
    rec = get_receipt(client, auth, fid).json()
    edit(rec["payload"])
    body = client.post("/api/v1/receipts/verify", content=json.dumps(rec)).json()
    assert body == {"valid": False, "reason": "signature_mismatch"}
    pub = client.get("/api/v1/receipts/public-key").json()["public_key"]
    assert receipts.verify_with_public_key(pub, rec) is False


def test_a_receipt_signed_with_another_key_is_rejected(client, auth):
    fid = upload(client, auth).json()["file_id"]
    rec = get_receipt(client, auth, fid).json()
    forged = receipts.sign(bytes(reversed(range(32))), rec["payload"])  # someone else's key
    assert client.post("/api/v1/receipts/verify", content=json.dumps(forged)).json()["valid"] is False
    rec["key_id"] = "0123456789abcdef"
    assert client.post("/api/v1/receipts/verify", content=json.dumps(rec)).json() == {
        "valid": False, "reason": "unknown_key"}


@pytest.mark.parametrize("body,status", [
    ("not json", 422), ("[1,2]", 422), ('{"payload": 3}', 422),
    ('{"payload": {}, "alg": "Ed25519", "signature": "***"}', 422),
])
def test_malformed_receipts_are_refused(client, body, status):
    assert client.post("/api/v1/receipts/verify", content=body).status_code == status


def test_an_unsupported_algorithm_is_not_valid(client):
    body = client.post("/api/v1/receipts/verify",
                       content=json.dumps({"payload": {}, "alg": "none", "signature": ""})).json()
    assert body == {"valid": False, "reason": "unsupported_algorithm"}


def test_an_oversized_receipt_is_refused(client):
    assert client.post("/api/v1/receipts/verify", content="x" * (70 * 1024)).status_code == 413


def test_no_receipt_for_a_damaged_file(client, auth, storage, repo, metric):
    fid = upload(client, auth).json()["file_id"]
    storage.objects[next(iter(storage.objects))] = b"damaged"
    before = metric("filegrid_integrity_failures_total", category="outage_records",
                    storage="s3", reason="checksum_mismatch")
    r = get_receipt(client, auth, fid)
    assert r.status_code == 409 and r.json()["reason"] == "checksum_mismatch"
    assert "payload" not in r.json()
    assert r.headers["X-Integrity-Verified"] == "false"
    assert metric("filegrid_integrity_failures_total", category="outage_records",
                  storage="s3", reason="checksum_mismatch") == before + 1
    assert any(a["action"] == "receipt_refused" for a in repo.audit)


def test_a_receipt_without_reverification_says_so(client, auth, storage):
    fid = upload(client, auth).json()["file_id"]
    storage.objects[next(iter(storage.objects))] = b"damaged"  # not noticed: not re-checked
    r = get_receipt(client, auth, fid, verify="false")
    assert r.status_code == 200
    assert r.json()["payload"]["verification"] == {"performed": False, "ok": None,
                                                    "checked_at": None, "reason": None}


def test_issuing_a_receipt_is_audited(client, auth):
    fid = upload(client, auth).json()["file_id"]
    get_receipt(client, auth, fid)
    events = [e["event"] for e in client.get(f"/api/v1/files/{fid}/audit", headers=auth).json()]
    assert "receipt" in events


def test_receipts_need_a_user_login_and_an_existing_file(client, auth):
    fid = upload(client, auth).json()["file_id"]
    assert client.get(f"/api/v1/files/{fid}/receipt").status_code == 401
    assert client.get(f"/api/v1/files/{fid}/receipt", headers=SIM).status_code == 403
    assert get_receipt(client, auth, str(uuid.uuid4())).status_code == 404


def test_a_receipt_for_an_encrypted_document_verifies(client, auth):
    r = upload(client, auth, category="connection_docs", name="cerere.pdf")
    assert r.status_code == 202
    rec = get_receipt(client, auth, r.json()["file_id"])
    assert rec.status_code == 200 and rec.json()["payload"]["file"]["encrypted"] is True


def test_the_same_master_key_gives_the_same_signing_key():
    a = receipts.public_key_info(SETTINGS.master_key)
    assert a == receipts.public_key_info(SETTINGS.master_key)
    assert receipts.public_key_info(bytes(32))["public_key"] != a["public_key"]


def test_the_signature_covers_the_canonical_bytes_not_the_key_order():
    one = receipts.sign(SETTINGS.master_key, {"a": 1, "b": {"c": 2, "d": 3}})
    two = receipts.sign(SETTINGS.master_key, {"b": {"d": 3, "c": 2}, "a": 1})
    assert one["signature"] == two["signature"]


# =============================== scrubber ===============================

def test_a_pass_verifies_new_files_and_counts_them(client, auth, repo, metric):
    for i in range(2):
        upload(client, auth, data=DATA + bytes([i]), name=f"a{i}.pdf")
    before = metric("filegrid_scrub_files_total", category="outage_records", storage="s3", outcome="ok")
    done = run(scrubber.scrub_once(deps_of(client)))
    assert done == {"ok": 2, "failed": 0, "skipped": 0}
    assert metric("filegrid_scrub_files_total", category="outage_records", storage="s3",
                  outcome="ok") == before + 2
    assert {s["status"] for s in repo.scrub.values()} == {"ok"}


def test_the_scrubber_finds_a_damaged_file_before_anyone_asks(client, auth, repo, storage, metric):
    fid = upload(client, auth).json()["file_id"]
    storage.objects[next(iter(storage.objects))] = DATA[:-1] + b"X"  # one byte changed in the storage
    fails = metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                   reason="checksum_mismatch")
    done = run(scrubber.scrub_once(deps_of(client)))
    assert done["failed"] == 1 and done["ok"] == 0
    # reported exactly like a failed download, so the existing alert fires
    assert metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                  reason="checksum_mismatch") == fails + 1
    entry = next(a for a in repo.audit if a["action"] == "scrub_failed")
    assert entry["file_id"] == fid and entry["user"] == "scrubber"
    assert repo.scrub[fid]["status"] == "failed"


def test_a_file_that_vanished_from_the_storage_is_found(client, auth, storage, metric):
    upload(client, auth)
    storage.objects.clear()
    before = metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                    reason="object_missing")
    assert run(scrubber.scrub_once(deps_of(client)))["failed"] == 1
    assert metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                  reason="object_missing") == before + 1


def test_a_storage_outage_is_not_the_files_fault(client, auth, repo, storage):
    fid = upload(client, auth).json()["file_id"]
    storage.down = True
    done = run(scrubber.scrub_once(deps_of(client)))
    assert done == {"ok": 0, "failed": 0, "skipped": 1}
    assert repo.scrub[fid] == {"verified_at": None, "status": None}   # back at the front of the queue
    assert not any(a["action"] == "scrub_failed" for a in repo.audit)
    storage.down = False
    assert run(scrubber.scrub_once(deps_of(client)))["ok"] == 1


def test_meter_readings_are_not_scrubbed_by_default(client, repo):
    body = json.dumps({"meter_id": "MTR-S-1", "timestamp": "2026-11-15T10:00:00.000Z"}).encode()
    r = client.post("/api/v1/ingest", headers=SIM, files={"file": ("r.json", body)},
                    data={"category": "meter_readings"})
    assert r.status_code == 202
    assert run(scrubber.scrub_once(deps_of(client))) == {"ok": 0, "failed": 0, "skipped": 0}


def test_files_above_the_size_cap_are_left_alone(client, auth):
    upload(client, auth)
    set_scrub(client, scrub_max_bytes=10)
    assert run(scrubber.scrub_once(deps_of(client))) == {"ok": 0, "failed": 0, "skipped": 0}


def test_a_file_is_not_rechecked_before_the_recheck_interval(client, auth):
    upload(client, auth)
    assert run(scrubber.scrub_once(deps_of(client)))["ok"] == 1
    assert run(scrubber.scrub_once(deps_of(client)))["ok"] == 0
    set_scrub(client, scrub_recheck_seconds=0)
    assert run(scrubber.scrub_once(deps_of(client)))["ok"] == 1


def test_a_batch_is_limited_and_the_rest_waits_for_the_next_pass(client, auth):
    for i in range(7):
        upload(client, auth, data=DATA + bytes([i]), name=f"f{i}.pdf")
    assert run(scrubber.scrub_once(deps_of(client)))["ok"] == 5
    assert run(scrubber.scrub_once(deps_of(client)))["ok"] == 2


def test_an_encrypted_document_is_scrubbed_through_decryption(client, auth):
    upload(client, auth, category="connection_docs", name="cerere.pdf")
    assert run(scrubber.scrub_once(deps_of(client))) == {"ok": 1, "failed": 0, "skipped": 0}


def test_the_status_endpoint_reports_what_the_scrubber_has_seen(client, auth, admin, storage):
    upload(client, auth, data=DATA + b"1", name="ok.pdf")
    upload(client, auth, data=DATA + b"2", name="bad.pdf")
    key = list(storage.objects)[1]
    storage.objects[key] = b"damaged"
    run(scrubber.scrub_once(deps_of(client)))
    body = client.get("/api/v1/admin/scrubber", headers=admin).json()
    assert body["enabled"] is True and body["batch"] == 5
    assert (body["files_total"], body["verified_ok"], body["failed"], body["never_checked"]) == (2, 1, 1, 0)
    assert body["last_checked_at"] and "meter_readings" not in body["categories"]


def test_only_an_admin_sees_the_scrubber_status(client, auth):
    assert client.get("/api/v1/admin/scrubber", headers=auth).status_code == 403
    assert client.get("/api/v1/admin/scrubber").status_code == 401


def test_a_disabled_scrubber_does_nothing(client, auth, repo):
    upload(client, auth)
    set_scrub(client, scrub_enabled=False)

    async def go():
        stop = asyncio.Event()
        await asyncio.wait_for(scrubber.run_scrubber(deps_of(client), stop), 2)  # returns at once

    run(go())
    assert all(s["status"] is None for s in repo.scrub.values())


def test_the_loop_scrubs_anchors_the_audit_head_and_stops(client, auth, repo, caplog):
    three_events(client, auth)
    set_scrub(client, scrub_interval_seconds=1.0)
    caplog.set_level(logging.INFO, logger="filegrid.scrubber")

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(scrubber.run_scrubber(deps_of(client), stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 3)

    run(go())
    assert {s["status"] for s in repo.scrub.values()} == {"ok"}
    anchors = [r for r in caplog.records if r.getMessage() == "audit anchor"]
    assert anchors and anchors[0].chain_valid is True and len(anchors[0].head_hash) == 64


def test_a_broken_chain_is_reported_loudly_by_the_anchor(client, auth, repo, caplog):
    three_events(client, auth)
    repo.chain[0]["details"] = "changed"
    caplog.set_level(logging.INFO, logger="filegrid.scrubber")
    run(scrubber.anchor_audit_head(deps_of(client)))
    assert any(r.getMessage() == "audit chain is BROKEN" for r in caplog.records)


def test_a_failing_pass_does_not_stop_the_loop(client, monkeypatch):
    calls = []

    async def boom(deps):
        calls.append(1)
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(scrubber, "scrub_once", boom)
    set_scrub(client, scrub_interval_seconds=1.0)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(scrubber.run_scrubber(deps_of(client), stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 3)

    run(go())
    assert calls  # it tried, survived the error, and shut down cleanly
