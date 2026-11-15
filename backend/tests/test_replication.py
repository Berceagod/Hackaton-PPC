"""improvements.md 2.2: second copies on another storage, and self-healing."""

import asyncio
import hashlib
import uuid

import pytest

import scrubber

DATA = b"raport avarie transformator " * 300
OUTAGE = {"storage": "s3", "encryption": False, "deduplicate": False, "priority": "high"}


def run(coro):
    return asyncio.run(coro)


def deps_of(client):
    return client.app.state.deps


def set_policy(client, admin, category="outage_records", **changes):
    base = {"outage_records": OUTAGE,
            "connection_docs": {"storage": "ftp", "encryption": True, "deduplicate": False,
                                "priority": "normal"}}[category]
    return client.put("/api/v1/admin/policies", headers=admin, json={category: {**base, **changes}})


def upload(client, auth, data=DATA, category="outage_records", name="avarie.pdf"):
    r = client.post("/api/v1/ingest", headers=auth, files={"file": (name, data)},
                    data={"category": category})
    assert r.status_code == 202, r.text
    return r.json()["file_id"]


def download(client, auth, fid):
    return client.get(f"/api/v1/files/{fid}/download", headers=auth)


@pytest.fixture
def replicated(client, admin):
    """outage_records with one replica (auto-placed: s3 primary -> ftp copy)."""
    assert set_policy(client, admin, replicas=1).status_code == 200


def flip(store, key):
    data = bytearray(store.objects[key])
    data[5] ^= 0xFF
    store.objects[key] = bytes(data)


# =============================== policy ===============================

def test_replicas_are_part_of_the_policy_only_when_switched_on(client, admin):
    before = client.get("/api/v1/admin/policies", headers=admin).json()
    assert all("replicas" not in p for p in before.values())  # documented shape stays 4 fields
    assert set_policy(client, admin, replicas=1).json()["outage_records"]["replicas"] == 1
    assert client.get("/api/v1/admin/policies", headers=admin).json()["outage_records"]["replicas"] == 1


def test_an_update_that_does_not_mention_replicas_keeps_them(client, admin):
    set_policy(client, admin, replicas=1, replica_storage="postgres")
    got = client.put("/api/v1/admin/policies", headers=admin, json={"outage_records": OUTAGE}).json()
    assert got["outage_records"]["replicas"] == 1
    assert got["outage_records"]["replica_storage"] == "postgres"  # the UI never sends these


def test_replicas_can_be_switched_off_explicitly(client, admin):
    set_policy(client, admin, replicas=1)
    got = set_policy(client, admin, replicas=0).json()
    assert "replicas" not in got["outage_records"]


@pytest.mark.parametrize("changes", [
    {"replicas": 2}, {"replicas": -1}, {"replicas": True}, {"replicas": "1"},
    {"replicas": 1, "replica_storage": "s3"},         # same storage as the primary
    {"replicas": 1, "replica_storage": "tape"},
])
def test_invalid_replica_settings_are_refused(client, admin, changes):
    assert set_policy(client, admin, **changes).status_code == 422


def test_meter_readings_cannot_have_replicas(client, admin):
    r = client.put("/api/v1/admin/policies", headers=admin, json={"meter_readings": {
        "storage": "postgres", "encryption": False, "deduplicate": True, "priority": "normal",
        "replicas": 1}})
    assert r.status_code == 422


# =============================== writing the copy ===============================

def test_without_replicas_nothing_is_copied(client, auth, repo, ftp_storage, pg_storage):
    upload(client, auth)
    assert repo.replicas == {} and not ftp_storage.objects and not pg_storage.objects


def test_the_copy_lands_on_another_storage_with_identical_bytes(
        client, auth, replicated, repo, storage, ftp_storage, metric):
    before = metric("filegrid_replica_writes_total", category="outage_records",
                    storage="ftp", outcome="ok")
    fid = upload(client, auth)
    rep = repo.replicas[fid]
    assert rep["storage"] == "ftp" and rep["status"] == "ok"   # s3 is the primary: next is ftp
    assert ftp_storage.objects[rep["storage_key"]] == storage.objects[f"outage_records/{fid}"] == DATA
    assert any(a["action"] == "replicated" for a in repo.audit)
    assert metric("filegrid_replica_writes_total", category="outage_records",
                  storage="ftp", outcome="ok") == before + 1


def test_the_policy_can_name_the_copy_storage(client, admin, auth, repo, pg_storage, ftp_storage):
    set_policy(client, admin, replicas=1, replica_storage="postgres")
    fid = upload(client, auth)
    assert repo.replicas[fid]["storage"] == "postgres" and pg_storage.objects and not ftp_storage.objects


def test_a_failing_copy_never_fails_the_upload(client, auth, replicated, repo, ftp_storage, metric):
    ftp_storage.down = True
    before = metric("filegrid_replica_writes_total", category="outage_records",
                    storage="ftp", outcome="pending")
    fid = upload(client, auth)  # 202 even though the copy could not be written
    assert repo.replicas[fid]["status"] == "pending"
    assert metric("filegrid_replica_writes_total", category="outage_records",
                  storage="ftp", outcome="pending") == before + 1
    assert download(client, auth, fid).content == DATA


def test_an_encrypted_document_is_copied_as_ciphertext(client, admin, auth, repo, storage, ftp_storage):
    set_policy(client, admin, "connection_docs", replicas=1)   # ftp primary -> s3 copy
    fid = upload(client, auth, category="connection_docs", name="cerere.pdf")
    rep = repo.replicas[fid]
    assert rep["storage"] == "s3" and rep["status"] == "ok"
    assert DATA not in storage.objects[rep["storage_key"]]      # not plaintext in the copy either
    assert storage.objects[rep["storage_key"]] == ftp_storage.objects[f"connection_docs/{fid}"]
    assert download(client, auth, fid).content == DATA


def test_a_chunked_upload_is_copied_too(client, admin, auth, repo, ftp_storage):
    pieces = [b"A" * 3000, b"B" * 3000, b"C" * 1234]
    uid = str(uuid.uuid4())
    client.put("/api/v1/admin/policies", headers=admin, json={"drone_videos": {
        "storage": "s3", "encryption": False, "deduplicate": False, "priority": "low", "replicas": 1}})
    for i, piece in enumerate(pieces):
        r = client.post("/api/v1/upload/chunk", headers=auth, files={"file_chunk": ("b", piece)}, data={
            "upload_id": uid, "chunk_index": str(i), "total_chunks": "3", "filename": "v.mp4",
            "category": "drone_videos", "metadata": "{}",
            "chunk_sha256": hashlib.sha256(piece).hexdigest()})
        assert r.status_code == 200, r.text
    fid = r.json()["file_id"]
    assert repo.replicas[fid]["status"] == "ok"
    assert ftp_storage.objects[repo.replicas[fid]["storage_key"]] == b"".join(pieces)


# =============================== self-healing on download ===============================

def test_a_damaged_primary_is_repaired_from_the_copy_and_served(
        client, auth, replicated, repo, storage, metric):
    fid = upload(client, auth)
    key = f"outage_records/{fid}"
    flip(storage, key)
    fails = metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                   reason="checksum_mismatch")
    fixed = metric("filegrid_repairs_total", category="outage_records", storage="s3",
                   kind="primary", outcome="ok")
    r = download(client, auth, fid)
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["X-Repaired"] == "true" and r.headers["X-Integrity-Verified"] == "true"
    assert storage.objects[key] == DATA                      # the primary itself was rewritten
    assert any(a["action"] == "repaired" for a in repo.audit)
    assert metric("filegrid_repairs_total", category="outage_records", storage="s3",
                  kind="primary", outcome="ok") == fixed + 1
    assert metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                  reason="checksum_mismatch") == fails          # repaired damage does not page
    again = download(client, auth, fid)                         # and the next one is ordinary
    assert again.status_code == 200 and "X-Repaired" not in again.headers


def test_a_vanished_primary_is_rebuilt(client, auth, replicated, storage):
    fid = upload(client, auth)
    del storage.objects[f"outage_records/{fid}"]
    r = download(client, auth, fid)
    assert r.status_code == 200 and r.content == DATA and r.headers["X-Repaired"] == "true"
    assert storage.objects[f"outage_records/{fid}"] == DATA


def test_an_encrypted_primary_is_repaired_too(client, admin, auth, ftp_storage):
    set_policy(client, admin, "connection_docs", replicas=1)
    fid = upload(client, auth, category="connection_docs", name="cerere.pdf")
    flip(ftp_storage, f"connection_docs/{fid}")
    r = download(client, auth, fid)
    assert r.status_code == 200 and r.content == DATA and r.headers["X-Repaired"] == "true"


def test_with_the_primary_storage_down_the_copy_is_served(
        client, auth, replicated, storage, repo):
    fid = upload(client, auth)
    storage.down = True
    r = download(client, auth, fid)
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["X-Served-From"] == "ftp" and "X-Repaired" not in r.headers
    assert r.headers["X-Integrity-Verified"] == "true"
    assert any("copia secundară" in (a["details"] or "") for a in repo.audit)


def test_a_damaged_copy_does_not_disturb_a_good_primary(client, auth, replicated, repo, ftp_storage):
    fid = upload(client, auth)
    flip(ftp_storage, repo.replicas[fid]["storage_key"])
    r = download(client, auth, fid)
    assert r.status_code == 200 and r.content == DATA and "X-Repaired" not in r.headers


def test_when_both_copies_are_damaged_nothing_is_served(
        client, auth, replicated, repo, storage, ftp_storage, metric):
    fid = upload(client, auth)
    flip(storage, f"outage_records/{fid}")
    flip(ftp_storage, repo.replicas[fid]["storage_key"])
    fails = metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                   reason="checksum_mismatch")
    r = download(client, auth, fid)
    assert r.status_code == 409 and r.headers["X-Integrity-Verified"] == "false"
    assert metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                  reason="checksum_mismatch") == fails + 1
    assert repo.replicas[fid]["status"] == "failed"


def test_a_copy_that_was_never_written_cannot_repair(client, auth, replicated, storage, ftp_storage):
    ftp_storage.down = True
    fid = upload(client, auth)                       # copy left pending
    ftp_storage.down = False
    flip(storage, f"outage_records/{fid}")
    assert download(client, auth, fid).status_code == 409


def test_without_a_copy_a_damaged_file_still_answers_409(client, auth, storage):
    fid = upload(client, auth)
    flip(storage, f"outage_records/{fid}")
    assert download(client, auth, fid).status_code == 409


def test_without_a_copy_a_down_storage_still_answers_503(client, auth, storage):
    fid = upload(client, auth)
    storage.down = True
    assert download(client, auth, fid).status_code == 503


def test_the_repair_is_audited_and_the_chain_stays_valid(client, auth, admin, replicated, storage):
    fid = upload(client, auth)
    flip(storage, f"outage_records/{fid}")
    download(client, auth, fid)
    events = [e["event"] for e in client.get(f"/api/v1/files/{fid}/audit", headers=auth).json()]
    assert {"replicated", "repaired", "download"} <= set(events)
    assert client.get("/api/v1/audit/verify", headers=admin).json()["valid"] is True


# =============================== self-healing in the background ===============================

def test_the_scrubber_repairs_a_damaged_primary_before_anyone_asks(
        client, auth, replicated, repo, storage, metric):
    fid = upload(client, auth)
    flip(storage, f"outage_records/{fid}")
    fails = metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                   reason="checksum_mismatch")
    done = run(scrubber.scrub_once(deps_of(client)))
    assert done["repaired"] == 1 and done["failed"] == 0
    assert storage.objects[f"outage_records/{fid}"] == DATA
    assert not any(a["action"] == "scrub_failed" for a in repo.audit)
    assert any(a["action"] == "repaired" and a["user"] == "scrubber" for a in repo.audit)
    assert metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                  reason="checksum_mismatch") == fails
    assert repo.scrub[fid]["status"] == "ok"


def test_the_scrubber_repairs_a_damaged_copy_from_the_primary(
        client, auth, replicated, repo, ftp_storage, metric):
    fid = upload(client, auth)
    key = repo.replicas[fid]["storage_key"]
    flip(ftp_storage, key)
    fixed = metric("filegrid_repairs_total", category="outage_records", storage="ftp",
                   kind="replica", outcome="ok")
    assert run(scrubber.scrub_once(deps_of(client)))["ok"] == 1
    assert ftp_storage.objects[key] == DATA and repo.replicas[fid]["status"] == "ok"
    assert any(a["action"] == "replica_repaired" for a in repo.audit)
    assert metric("filegrid_repairs_total", category="outage_records", storage="ftp",
                  kind="replica", outcome="ok") == fixed + 1


def test_the_scrubber_rewrites_a_vanished_copy(client, auth, replicated, repo, ftp_storage):
    fid = upload(client, auth)
    key = repo.replicas[fid]["storage_key"]
    del ftp_storage.objects[key]
    run(scrubber.scrub_once(deps_of(client)))
    assert ftp_storage.objects[key] == DATA


def test_the_scrubber_completes_a_copy_that_could_not_be_written(
        client, auth, replicated, repo, ftp_storage):
    ftp_storage.down = True
    fid = upload(client, auth)
    assert repo.replicas[fid]["status"] == "pending"
    ftp_storage.down = False
    run(scrubber.scrub_once(deps_of(client)))
    assert repo.replicas[fid]["status"] == "ok"
    assert ftp_storage.objects[repo.replicas[fid]["storage_key"]] == DATA


def test_files_stored_before_replicas_were_switched_on_get_their_copy(
        client, admin, auth, repo):
    fid = upload(client, auth)                      # no policy for a copy yet
    assert fid not in repo.replicas
    set_policy(client, admin, replicas=1)
    run(scrubber.scrub_once(deps_of(client)))
    assert repo.replicas[fid]["status"] == "ok"
    assert any(a["action"] == "replicated" and a["user"] == "scrubber" for a in repo.audit)


def test_the_scrubber_reports_damage_when_both_copies_are_bad(
        client, auth, replicated, repo, storage, ftp_storage, metric):
    fid = upload(client, auth)
    flip(storage, f"outage_records/{fid}")
    flip(ftp_storage, repo.replicas[fid]["storage_key"])
    fails = metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                   reason="checksum_mismatch")
    done = run(scrubber.scrub_once(deps_of(client)))
    assert done["failed"] == 1 and "repaired" not in done
    assert metric("filegrid_integrity_failures_total", category="outage_records", storage="s3",
                  reason="checksum_mismatch") == fails + 1
    assert any(a["action"] == "scrub_failed" for a in repo.audit)


def test_a_copy_storage_outage_is_not_a_repair(client, auth, replicated, repo, ftp_storage):
    fid = upload(client, auth)
    ftp_storage.down = True
    done = run(scrubber.scrub_once(deps_of(client)))
    assert done["ok"] == 1                                    # primary is fine
    assert repo.replicas[fid]["status"] == "ok"               # not marked failed for an outage
    assert not any(a["action"] == "replica_repaired" for a in repo.audit)


def test_a_policy_without_replicas_leaves_the_scrubber_unchanged(client, auth, repo):
    upload(client, auth)
    assert run(scrubber.scrub_once(deps_of(client))) == {"ok": 1, "failed": 0, "skipped": 0}
    assert repo.replicas == {}
