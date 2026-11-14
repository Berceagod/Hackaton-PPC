"""Slice E: chunked, resumable uploads (drone videos and anything > 5 MB)."""

import hashlib
import json
import uuid

import pytest

SIM = {"Authorization": "Bearer sim-token"}

PIECES = [b"A" * 3000, b"B" * 3000, b"C" * 1234]
WHOLE = b"".join(PIECES)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chunk(client, auth, upload_id, index, data, *, total=3, category="drone_videos",
           filename="inspectie_LEA20.mp4", meta=None, extra=None):
    form = {
        "upload_id": upload_id,
        "chunk_index": str(index),
        "total_chunks": str(total),
        "filename": filename,
        "category": category,
        "metadata": json.dumps(meta or {"zone": "Muntenia", "station": "LEA-20"}),
        "chunk_sha256": _sha(data),
        **(extra or {}),
    }
    return client.post("/api/v1/upload/chunk", headers=auth,
                       files={"file_chunk": ("blob", data)}, data=form)


def _upload_all(client, auth, upload_id, order=(0, 1, 2), **kw):
    r = None
    for i in order:
        r = _chunk(client, auth, upload_id, i, PIECES[i], **kw)
        assert r.status_code == 200, r.text
    return r


def _staged(storage):
    return [k for k in storage.objects if k.startswith("_staging/")]


# ---------------- happy path ----------------

def test_chunks_are_assembled_into_one_verified_file(client, auth, storage, repo):
    uid = str(uuid.uuid4())
    first = _chunk(client, auth, uid, 0, PIECES[0])
    assert first.json() == {"upload_id": uid, "received_chunks": 1, "total_chunks": 3,
                            "status": "open"}
    last = _upload_all(client, auth, uid, order=(1, 2), extra={"sha256": _sha(WHOLE),
                                                             "size_bytes": str(len(WHOLE))})
    body = last.json()
    assert body["status"] == "complete" and body["received_chunks"] == 3
    assert body["sha256"] == _sha(WHOLE) and body["storage"] == "s3"
    assert {"file_id", "trace_id"} <= set(body)

    rec = repo.files[body["file_id"]]
    assert (rec.filename, rec.category, rec.zone, rec.station, rec.size_bytes) == (
        "inspectie_LEA20.mp4", "drone_videos", "Muntenia", "LEA-20", len(WHOLE))
    assert _staged(storage) == []  # staging cleaned up
    down = client.get(f"/api/v1/files/{body['file_id']}/download", headers=auth)
    assert down.status_code == 200 and down.content == WHOLE
    assert down.headers["x-integrity-verified"] == "true"
    events = [e["event"] for e in client.get(f"/api/v1/files/{body['file_id']}/audit",
                                             headers=auth).json()]
    assert "upload" in events


def test_out_of_order_and_repeated_chunks_still_give_the_right_bytes(client, auth, repo):
    uid = str(uuid.uuid4())
    _chunk(client, auth, uid, 2, PIECES[2])
    _chunk(client, auth, uid, 0, PIECES[0])
    _chunk(client, auth, uid, 0, PIECES[0])  # network retry of the same chunk
    r = _chunk(client, auth, uid, 1, PIECES[1])
    assert r.json()["status"] == "complete"
    rec = repo.files[r.json()["file_id"]]
    assert rec.sha256 == _sha(WHOLE) and rec.size_bytes == len(WHOLE)


def test_resending_a_chunk_after_completion_returns_the_same_file(client, auth, repo):
    uid = str(uuid.uuid4())
    done = _upload_all(client, auth, uid).json()
    again = _chunk(client, auth, uid, 2, PIECES[2])
    assert again.status_code == 200
    assert again.json()["file_id"] == done["file_id"] and again.json()["status"] == "complete"
    assert len([f for f in repo.files.values() if f.category == "drone_videos"]) == 1


def test_policy_decides_the_final_storage_staging_stays_on_s3(client, auth, ftp_storage,
                                                               storage, admin):
    r = client.put("/api/v1/admin/policies", headers=admin,
                   json={"drone_videos": {"storage": "ftp", "encryption": False,
                                          "deduplicate": False, "priority": "low"}})
    assert r.status_code == 200
    body = _upload_all(client, auth, str(uuid.uuid4())).json()
    assert body["storage"] == "ftp"
    assert list(ftp_storage.objects.values()) == [WHOLE]
    assert _staged(storage) == []


# ---------------- resume ----------------

def test_status_tells_the_client_which_chunks_to_resend(client, auth):
    uid = str(uuid.uuid4())
    _chunk(client, auth, uid, 0, PIECES[0])
    _chunk(client, auth, uid, 2, PIECES[2])
    st = client.get(f"/api/v1/upload/{uid}", headers=auth).json()
    assert st["status"] == "open" and st["received"] == [0, 2] and st["missing"] == [1]
    assert _chunk(client, auth, uid, 1, PIECES[1]).json()["status"] == "complete"
    st = client.get(f"/api/v1/upload/{uid}", headers=auth).json()
    assert st["status"] == "complete" and st["missing"] == [] and "file_id" in st


def test_status_of_unknown_or_foreign_upload_is_404(client, auth, admin):
    uid = str(uuid.uuid4())
    assert client.get(f"/api/v1/upload/{uid}", headers=auth).status_code == 404
    _chunk(client, auth, uid, 0, PIECES[0])
    assert client.get(f"/api/v1/upload/{uid}", headers=admin).status_code == 404
    assert _chunk(client, admin, uid, 1, PIECES[1]).status_code == 404


# ---------------- integrity ----------------

def test_chunk_corrupted_in_transit_is_rejected_and_can_be_resent(client, auth, storage):
    uid = str(uuid.uuid4())
    bad = _chunk(client, auth, uid, 0, PIECES[0], extra={"chunk_sha256": "0" * 64})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "chunk_checksum_mismatch"
    assert client.get(f"/api/v1/upload/{uid}", headers=auth).json()["received"] == []
    assert _staged(storage) == []
    assert _upload_all(client, auth, uid).json()["status"] == "complete"


def test_whole_file_checksum_mismatch_fails_the_upload_and_stores_nothing(client, auth,
                                                                         storage, repo):
    uid = str(uuid.uuid4())
    _chunk(client, auth, uid, 0, PIECES[0], extra={"sha256": "f" * 64})
    _chunk(client, auth, uid, 1, PIECES[1])
    r = _chunk(client, auth, uid, 2, PIECES[2])
    assert r.status_code == 422 and r.json()["detail"]["error"] == "file_checksum_mismatch"
    assert repo.files == {} and storage.objects == {}
    st = client.get(f"/api/v1/upload/{uid}", headers=auth).json()
    assert st["status"] == "failed"
    assert _chunk(client, auth, uid, 2, PIECES[2]).status_code == 409  # start a new upload


def test_declared_size_mismatch_fails_the_upload(client, auth, repo):
    uid = str(uuid.uuid4())
    _chunk(client, auth, uid, 0, PIECES[0], extra={"size_bytes": "5"})
    _chunk(client, auth, uid, 1, PIECES[1])
    r = _chunk(client, auth, uid, 2, PIECES[2])
    assert r.status_code == 422 and r.json()["detail"]["error"] == "file_size_mismatch"
    assert repo.files == {}


def test_oversized_chunk_is_413_and_not_kept(client, auth, storage, monkeypatch):
    import chunked_upload

    monkeypatch.setattr(chunked_upload, "MAX_CHUNK_BYTES", 2000)
    r = _chunk(client, auth, str(uuid.uuid4()), 0, PIECES[0])
    assert r.status_code == 413
    assert _staged(storage) == []


# ---------------- resilience ----------------

def test_target_storage_down_at_the_end_keeps_the_chunks_for_a_retry(client, auth,
                                                                    storage, repo):
    uid = str(uuid.uuid4())
    _chunk(client, auth, uid, 0, PIECES[0])
    _chunk(client, auth, uid, 1, PIECES[1])

    real_put = storage.put_stream

    async def final_write_fails(key, chunks):
        if not key.startswith("_staging/"):
            raise __import__("storage.base", fromlist=["StorageError"]).StorageError("down")
        return await real_put(key, chunks)

    storage.put_stream = final_write_fails
    r = _chunk(client, auth, uid, 2, PIECES[2])
    assert r.status_code == 503 and repo.files == {}
    assert len(_staged(storage)) == 3  # nothing lost
    storage.put_stream = real_put
    r = _chunk(client, auth, uid, 2, PIECES[2])  # client retry
    assert r.status_code == 200 and r.json()["status"] == "complete"
    assert repo.files[r.json()["file_id"]].sha256 == _sha(WHOLE)


def test_staging_down_is_503(client, auth, storage):
    storage.down = True
    assert _chunk(client, auth, str(uuid.uuid4()), 0, PIECES[0]).status_code == 503


# ---------------- validation and roles ----------------

@pytest.mark.parametrize("override", [
    {"upload_id": "not-a-uuid"},
    {"chunk_index": "3"},
    {"chunk_index": "-1"},
    {"total_chunks": "0"},
    {"category": "bogus"},
    {"category": "meter_readings"},  # readings go through /ingest (idempotent path)
    {"filename": ""},
    {"metadata": "[1,2]"},
    {"sha256": "xyz"},
    {"chunk_sha256": "xyz"},
])
def test_invalid_chunk_requests_are_422(client, auth, override):
    data = {
        "upload_id": str(uuid.uuid4()), "chunk_index": "0", "total_chunks": "3",
        "filename": "v.mp4", "category": "drone_videos", "metadata": "{}", **override,
    }
    r = client.post("/api/v1/upload/chunk", headers=auth,
                    files={"file_chunk": ("blob", b"x")}, data=data)
    assert r.status_code == 422, r.text


def test_missing_file_metadata_is_422(client, auth):
    # the original frontend sent only these four fields: the backend cannot know the file
    r = client.post("/api/v1/upload/chunk", headers=auth, files={"file_chunk": ("b", b"x")},
                    data={"upload_id": str(uuid.uuid4()), "chunk_index": "0", "total_chunks": "1"})
    assert r.status_code == 422


def test_same_upload_id_with_different_shape_is_409(client, auth):
    uid = str(uuid.uuid4())
    _chunk(client, auth, uid, 0, PIECES[0])
    assert _chunk(client, auth, uid, 1, PIECES[1], total=4).status_code == 409
    assert _chunk(client, auth, uid, 1, PIECES[1], filename="other.mp4").status_code == 409


def test_simulator_token_cannot_upload_chunks(client):
    assert _chunk(client, SIM, str(uuid.uuid4()), 0, PIECES[0]).status_code == 403


def test_encrypted_category_is_accepted_since_slice_f(client, auth):
    # was 501 until slice F; encryption details are tested in test_slice_f.py
    r = _chunk(client, auth, str(uuid.uuid4()), 0, PIECES[0], category="connection_docs")
    assert r.status_code == 200
