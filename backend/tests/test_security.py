"""improvements.md 2.5: login lockout, auditor role, per-file keys, erasure, key rotation."""

import asyncio
import dataclasses
import time

import pytest

import keystore
import scrubber
from crypto_envelope import decrypt_stream

DATA = b"cerere de racordare, date personale " * 200


def run(coro):
    return asyncio.run(coro)


async def _collect(stream):
    return [c async for c in stream]


async def _one(data):
    yield data


def deps_of(client):
    return client.app.state.deps


def login(client, email, password):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def upload_doc(client, auth, data=DATA):
    r = client.post("/api/v1/ingest", headers=auth, files={"file": ("cerere.pdf", data)},
                    data={"category": "connection_docs"})
    assert r.status_code == 202, r.text
    return r.json()["file_id"]


@pytest.fixture
def auditor(client):
    d = deps_of(client)
    d.settings = dataclasses.replace(d.settings, users={
        **d.settings.users, "auditor@ppc.ro": {"password": "pw-u", "role": "auditor", "name": "Auditor"}})
    token = login(client, "auditor@ppc.ro", "pw-u").json()["token"]
    return {"Authorization": f"Bearer {token}"}


# =============================== login lockout ===============================

def test_five_failures_lock_the_account_even_for_the_right_password(client):
    for _ in range(5):
        assert login(client, "dispecer@ppc.ro", "wrong").status_code == 401
    locked = login(client, "dispecer@ppc.ro", "pw-d")
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) >= 1


def test_a_success_resets_the_counter(client):
    for _ in range(4):
        login(client, "dispecer@ppc.ro", "wrong")
    assert login(client, "dispecer@ppc.ro", "pw-d").status_code == 200
    for _ in range(4):
        assert login(client, "dispecer@ppc.ro", "wrong").status_code == 401   # not locked yet
    assert login(client, "dispecer@ppc.ro", "pw-d").status_code == 200


def test_the_lock_is_per_account(client):
    for _ in range(5):
        login(client, "dispecer@ppc.ro", "wrong")
    assert login(client, "admin@ppc.ro", "pw-a").status_code == 200


def test_unknown_accounts_are_tracked_too_so_nothing_is_revealed(client):
    for _ in range(5):
        assert login(client, "nobody@ppc.ro", "x").status_code == 401
    assert login(client, "nobody@ppc.ro", "x").status_code == 429


def test_the_lock_expires_and_doubles_with_each_further_failure(client, repo):
    d = deps_of(client)
    d.settings = dataclasses.replace(d.settings, login_lock_base_seconds=10.0)
    for _ in range(5):
        login(client, "dispecer@ppc.ro", "wrong")
    assert login(client, "dispecer@ppc.ro", "pw-d").status_code == 429
    repo.logins["dispecer@ppc.ro"]["last"] -= 11          # the first 10 s have passed
    assert login(client, "dispecer@ppc.ro", "wrong").status_code == 401   # counts: 6 failures
    assert repo.logins["dispecer@ppc.ro"]["failures"] == 6
    repo.logins["dispecer@ppc.ro"]["last"] -= 11          # 6 failures lock for 20 s: 11 s is not enough
    assert login(client, "dispecer@ppc.ro", "pw-d").status_code == 429
    repo.logins["dispecer@ppc.ro"]["last"] -= 10
    assert login(client, "dispecer@ppc.ro", "pw-d").status_code == 200


def test_the_lock_is_capped(client, repo):
    d = deps_of(client)
    d.settings = dataclasses.replace(d.settings, login_lock_max_seconds=60.0)
    repo.logins["dispecer@ppc.ro"] = {"failures": 50, "last": time.time()}
    r = login(client, "dispecer@ppc.ro", "pw-d")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) <= 61


def test_locking_is_audited_once_and_counted(client, repo, metric):
    before = metric("filegrid_logins_total", outcome="locked")
    for _ in range(8):
        login(client, "dispecer@ppc.ro", "wrong")
    assert sum(1 for a in repo.audit if a["action"] == "login_locked") == 1
    assert metric("filegrid_logins_total", outcome="locked") == before + 3


# =============================== auditor ===============================

def test_the_auditor_reads_the_trail_and_the_list_but_never_the_content(client, auth, auditor):
    fid = upload_doc(client, auth)
    client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert client.get("/api/v1/files?category=connection_docs", headers=auditor).status_code == 200
    trail = client.get(f"/api/v1/files/{fid}/audit", headers=auditor)
    assert trail.status_code == 200 and any(e["event"] == "download" for e in trail.json())
    assert client.get("/api/v1/audit/verify", headers=auditor).json()["valid"] is True
    for path in (f"/files/{fid}/download", f"/files/{fid}/verify", f"/files/{fid}/receipt"):
        assert client.get(f"/api/v1{path}", headers=auditor).status_code == 403
    assert client.post("/api/v1/ingest", headers=auditor, files={"file": ("a", b"x")},
                       data={"category": "outage_records"}).status_code == 403
    assert client.get("/api/v1/admin/policies", headers=auditor).status_code == 403
    assert client.put("/api/v1/admin/policies", headers=auditor, json={}).status_code == 403
    assert client.post(f"/api/v1/files/{fid}/erase", headers=auditor).status_code == 403


def test_no_auditor_account_exists_unless_configured(client):
    assert login(client, "auditor@ppc.ro", "anything").status_code == 401


# =============================== per-file keys ===============================

def test_a_new_encrypted_file_gets_its_own_wrapped_key(client, auth, repo, ftp_storage):
    fid = upload_doc(client, auth)
    row = repo.file_keys[fid]
    master = deps_of(client).settings.master_key
    assert row["status"] == "active" and len(row["wrapped"]) == 12 + 32 + 16   # nonce + key + tag
    stored = ftp_storage.objects[f"connection_docs/{fid}"]
    file_key = keystore.unwrap(master, fid, row["wrapped"])
    assert b"".join(run(_collect(decrypt_stream(_one(stored), file_key, fid)))) == DATA
    with pytest.raises(Exception):   # the master-derived key does NOT open it any more
        run(_collect(decrypt_stream(_one(stored), master, fid)))
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).content == DATA


def test_two_files_never_share_a_key(client, auth, repo):
    a, b = upload_doc(client, auth), upload_doc(client, auth)
    assert repo.file_keys[a]["wrapped"] != repo.file_keys[b]["wrapped"]


def test_unencrypted_files_get_no_key(client, auth, repo):
    client.post("/api/v1/ingest", headers=auth, files={"file": ("a.pdf", DATA)},
                data={"category": "outage_records"})
    assert repo.file_keys == {}


def test_a_file_stored_before_per_file_keys_still_opens(client, auth, repo, ftp_storage):
    """Legacy: encrypted with the key derived from the master key, no row in file_keys."""
    from crypto_envelope import encrypt_stream
    fid = upload_doc(client, auth)
    del repo.file_keys[fid]
    master = deps_of(client).settings.master_key
    ftp_storage.objects[f"connection_docs/{fid}"] = b"".join(
        run(_collect(encrypt_stream(_one(DATA), master, fid))))
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).content == DATA


def test_a_tampered_wrapped_key_is_not_silently_accepted(client, auth, repo):
    fid = upload_doc(client, auth)
    blob = bytearray(repo.file_keys[fid]["wrapped"])
    blob[20] ^= 0xFF
    repo.file_keys[fid]["wrapped"] = bytes(blob)
    with pytest.raises(keystore.KeyUnwrapError):
        client.get(f"/api/v1/files/{fid}/download", headers=auth)


def test_a_wrapped_key_is_bound_to_its_file():
    master = bytes(range(32))
    blob = keystore.wrap(master, "file-a", b"k" * 32)
    assert keystore.unwrap(master, "file-a", blob) == b"k" * 32
    with pytest.raises(keystore.KeyUnwrapError):
        keystore.unwrap(master, "file-b", blob)                 # moved to another file
    with pytest.raises(keystore.KeyUnwrapError):
        keystore.unwrap(bytes(32), "file-a", blob)              # wrong master key


# =============================== erasure ===============================

def test_erasing_a_document_destroys_its_key_and_objects(client, admin, auth, repo, ftp_storage):
    fid = upload_doc(client, auth)
    r = client.post(f"/api/v1/files/{fid}/erase", headers=admin)
    assert r.status_code == 200
    assert r.json() == {"file_id": fid, "erased": True, "method": "key_destroyed",
                        "objects_deleted_from": ["ftp"]}
    assert repo.file_keys[fid] == {"wrapped": None, "status": "erased", "erased_by": "admin@ppc.ro"}
    assert f"connection_docs/{fid}" not in ftp_storage.objects
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).status_code == 410
    assert client.get(f"/api/v1/files/{fid}/verify", headers=auth).status_code == 410
    assert client.get(f"/api/v1/files/{fid}/receipt", headers=auth).status_code == 410
    assert client.post(f"/api/v1/files/{fid}/erase", headers=admin).status_code == 410   # once


def test_restoring_the_deleted_ciphertext_does_not_bring_the_document_back(
        client, admin, auth, repo, ftp_storage):
    fid = upload_doc(client, auth)
    saved = ftp_storage.objects[f"connection_docs/{fid}"]       # e.g. a backup of the storage
    client.post(f"/api/v1/files/{fid}/erase", headers=admin)
    ftp_storage.objects[f"connection_docs/{fid}"] = saved       # the backup is restored
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).status_code == 410
    with pytest.raises(Exception):   # and the master key alone cannot open it: the file key is gone
        run(_collect(decrypt_stream(_one(saved), deps_of(client).settings.master_key, fid)))


def test_erasure_is_admin_only_and_only_for_documents(client, admin, auth):
    fid = upload_doc(client, auth)
    assert client.post(f"/api/v1/files/{fid}/erase", headers=auth).status_code == 403
    other = client.post("/api/v1/ingest", headers=auth, files={"file": ("a.pdf", DATA)},
                        data={"category": "outage_records"}).json()["file_id"]
    assert client.post(f"/api/v1/files/{other}/erase", headers=admin).status_code == 422
    assert client.post("/api/v1/files/00000000-0000-0000-0000-000000000001/erase",
                       headers=admin).status_code == 404


def test_erasure_stays_in_the_audit_trail_and_the_chain_is_valid(client, admin, auth):
    fid = upload_doc(client, auth)
    client.post(f"/api/v1/files/{fid}/erase", headers=admin)
    events = [e["event"] for e in client.get(f"/api/v1/files/{fid}/audit", headers=admin).json()]
    assert "erased" in events and "upload" in events
    assert client.get("/api/v1/audit/verify", headers=admin).json()["valid"] is True


def test_an_old_file_without_its_own_key_is_erased_by_deleting_its_objects(
        client, admin, auth, repo, ftp_storage):
    fid = upload_doc(client, auth)
    del repo.file_keys[fid]
    r = client.post(f"/api/v1/files/{fid}/erase", headers=admin).json()
    assert r["method"] == "objects_deleted_only" and f"connection_docs/{fid}" not in ftp_storage.objects


def test_the_replica_is_deleted_with_the_file(client, admin, auth, storage):
    client.put("/api/v1/admin/policies", headers=admin, json={"connection_docs": {
        "storage": "ftp", "encryption": True, "deduplicate": False, "priority": "normal", "replicas": 1}})
    fid = upload_doc(client, auth)
    assert f"connection_docs/{fid}" in storage.objects            # the copy, on s3
    r = client.post(f"/api/v1/files/{fid}/erase", headers=admin).json()
    assert sorted(r["objects_deleted_from"]) == ["ftp", "s3"] and storage.objects == {}


def test_the_scrubber_never_reports_an_erased_file_as_damaged(client, admin, auth, repo, metric):
    fid = upload_doc(client, auth)
    client.post(f"/api/v1/files/{fid}/erase", headers=admin)
    fails = metric("filegrid_integrity_failures_total", category="connection_docs", storage="ftp",
                   reason="object_missing")
    done = run(scrubber.scrub_once(deps_of(client)))
    assert done["failed"] == 0
    assert metric("filegrid_integrity_failures_total", category="connection_docs", storage="ftp",
                  reason="object_missing") == fails
    assert not any(a["action"] == "scrub_failed" for a in repo.audit)


# =============================== master key rotation ===============================

def test_rotating_the_master_key_rewraps_keys_without_touching_files(client, auth, repo, ftp_storage):
    ids = [upload_doc(client, auth, DATA + bytes([i])) for i in range(3)]
    objects_before = dict(ftp_storage.objects)
    old = deps_of(client).settings.master_key
    new = bytes(reversed(old))
    assert run(keystore.rewrap_all(repo, old, new, batch=2)) == 3
    assert ftp_storage.objects == objects_before                  # not one file was rewritten
    for fid in ids:
        assert len(keystore.unwrap(new, fid, repo.file_keys[fid]["wrapped"])) == 32
        with pytest.raises(keystore.KeyUnwrapError):
            keystore.unwrap(old, fid, repo.file_keys[fid]["wrapped"])
    d = deps_of(client)
    d.settings = dataclasses.replace(d.settings, master_key=new)   # restart with the new key
    assert client.get(f"/api/v1/files/{ids[0]}/download", headers=auth).content == DATA + b"\x00"


def test_rotation_skips_erased_keys(client, admin, auth, repo):
    a, b = upload_doc(client, auth), upload_doc(client, auth, DATA + b"2")
    client.post(f"/api/v1/files/{a}/erase", headers=admin)
    old = deps_of(client).settings.master_key
    assert run(keystore.rewrap_all(repo, old, bytes(reversed(old)))) == 1
    assert b in repo.file_keys
