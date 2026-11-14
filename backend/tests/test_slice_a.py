import hashlib

PAYLOAD = b"avarie trafo T12 " * 5000


def _ingest(client, auth, data=PAYLOAD, category="outage_records", name="a.pdf"):
    return client.post(
        "/api/v1/ingest",
        headers=auth,
        files={"file": (name, data, "application/pdf")},
        data={"category": category, "metadata": '{"zone": "Dobrogea"}'},
    )


def test_health_ok(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_ok_and_bad(client):
    ok = client.post(
        "/api/v1/auth/login", json={"email": "admin@ppc.ro", "password": "pw-a"}
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["role"] == "admin"
    bad = client.post(
        "/api/v1/auth/login", json={"email": "admin@ppc.ro", "password": "nope"}
    )
    assert bad.status_code == 401


def test_ingest_requires_auth(client):
    r = client.post(
        "/api/v1/ingest",
        files={"file": ("a", b"x")},
        data={"category": "outage_records"},
    )
    assert r.status_code == 401


def test_service_token_accepted(client):
    r = client.post(
        "/api/v1/ingest",
        headers={"Authorization": "Bearer sim-token"},
        files={"file": ("r.json", b'{"meter_id":"MTR-1","timestamp":"2026-11-14T10:00:00Z"}')},
        data={"category": "meter_readings"},
    )
    assert r.status_code == 202  # the service token is meter-only (see test_slice_d)


def test_ingest_returns_202_sha_and_trace(client, auth, storage):
    r = _ingest(client, auth)
    assert r.status_code == 202
    body = r.json()
    assert body["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert body["storage"] == "s3"
    assert body["trace_id"] == r.headers["X-Trace-Id"]
    assert len(storage.objects) == 1


def test_trace_id_is_propagated_from_request(client, auth):
    tid = "11111111-1111-4111-8111-111111111111"
    r = client.get("/api/v1/files", headers={**auth, "X-Trace-Id": tid})
    assert r.headers["X-Trace-Id"] == tid


def test_download_is_byte_identical_and_verified(client, auth):
    fid = _ingest(client, auth).json()["file_id"]
    r = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert r.status_code == 200
    assert r.content == PAYLOAD
    assert r.headers["X-Integrity-Verified"] == "true"
    assert r.headers["X-File-Sha256"] == hashlib.sha256(PAYLOAD).hexdigest()


def test_corrupt_object_returns_409_and_serves_nothing(client, auth, storage):
    fid = _ingest(client, auth).json()["file_id"]
    key = next(iter(storage.objects))
    storage.objects[key] = storage.objects[key][:-1] + b"X"  # flip last byte
    r = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert r.status_code == 409
    assert r.headers["X-Integrity-Verified"] == "false"
    assert PAYLOAD[:50] not in r.content


def test_missing_object_returns_409(client, auth, storage):
    fid = _ingest(client, auth).json()["file_id"]
    storage.objects.clear()
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).status_code == 409


def test_verify_endpoint(client, auth, storage):
    fid = _ingest(client, auth).json()["file_id"]
    assert client.get(f"/api/v1/files/{fid}/verify", headers=auth).json()["verified"]
    storage.objects[next(iter(storage.objects))] = b"tampered"
    assert not client.get(f"/api/v1/files/{fid}/verify", headers=auth).json()["verified"]


def test_storage_down_means_503_and_no_metadata(client, auth, storage, repo):
    storage.down = True
    r = _ingest(client, auth)
    assert r.status_code == 503
    assert repo.files == {}  # never acknowledge what was not stored


def test_list_filters_and_shape(client, auth):
    _ingest(client, auth, name="one.pdf")
    _ingest(client, auth, category="drone_videos", name="two.mp4")
    allf = client.get("/api/v1/files", headers=auth).json()
    assert len(allf) == 2
    only = client.get("/api/v1/files?category=drone_videos", headers=auth).json()
    assert [f["filename"] for f in only] == ["two.mp4"]
    assert client.get("/api/v1/files?storage=ftp", headers=auth).json() == []
    f = allf[0]
    assert set(f) >= {"id", "filename", "category", "storage", "size_bytes",
                      "sha256", "created_at", "encrypted", "trace_id"}
    assert "storage_key" not in f


def test_audit_trail(client, auth):
    fid = _ingest(client, auth).json()["file_id"]
    client.get(f"/api/v1/files/{fid}/download", headers=auth)
    actions = [a["action"] for a in client.get(f"/api/v1/files/{fid}/audit", headers=auth).json()]
    assert "upload" in actions and "download" in actions


def test_unknown_category_and_unimplemented_storage(client, auth):
    assert _ingest(client, auth, category="bogus").status_code == 422
    # connection_docs needs encryption, which arrives in slice F
    assert _ingest(client, auth, category="connection_docs").status_code == 501
