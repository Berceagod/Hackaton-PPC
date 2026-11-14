import hashlib
import json

import pytest

SIM = {"Authorization": "Bearer sim-token"}


def reading(meter="MTR-000001", ts="2026-11-14T10:00:00.000Z", voltage=230.1, label=None):
    return {
        "reading_id": "r-1",
        "meter_id": meter,
        "timestamp": ts,
        "zone": "Muntenia",
        "instant": {"voltage_v": voltage},
        "events": [],
        "sim_label": label,
    }


def send(client, payload, key="auto", headers=SIM, raw=None):
    body = raw if raw is not None else json.dumps(payload, separators=(",", ":")).encode()
    h = dict(headers)
    if key == "auto":
        key = f"{payload['meter_id']}:{payload['timestamp']}"
    if key:
        h["Idempotency-Key"] = key
    return client.post(
        "/api/v1/ingest",
        headers=h,
        files={"file": ("r.json", body, "application/json")},
        data={"category": "meter_readings", "metadata": "{}"},
    )


def test_new_reading_is_stored_and_acknowledged(client, pg_storage, repo):
    p = reading()
    r = send(client, p)
    assert r.status_code == 202
    body = r.json()
    raw = json.dumps(p, separators=(",", ":")).encode()
    assert body["storage"] == "postgres"
    assert body["sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(pg_storage.objects) == 1
    assert [x["status"] for x in repo.readings.values()] == ["stored"]
    assert body["file_id"] in repo.files


def test_duplicate_is_rejected_with_409_and_nothing_grows(client, pg_storage, repo):
    p = reading()
    first = send(client, p)
    for _ in range(5):
        dup = send(client, p)
        assert dup.status_code == 409
        assert dup.json()["file_id"] == first.json()["file_id"]
    assert len(repo.readings) == 1
    assert len(repo.files) == 1
    assert len(pg_storage.objects) == 1


def test_dedup_uses_payload_even_without_header(client, repo):
    p = reading()
    assert send(client, p, key=None).status_code == 202
    assert send(client, p, key=None).status_code == 409
    assert len(repo.readings) == 1


def test_different_timestamp_or_meter_is_a_new_reading(client, repo):
    assert send(client, reading()).status_code == 202
    assert send(client, reading(ts="2026-11-14T10:00:01.000Z")).status_code == 202
    assert send(client, reading(meter="MTR-000002")).status_code == 202
    assert len(repo.readings) == 3


def test_same_instant_in_other_timezone_notation_is_a_duplicate(client, repo):
    assert send(client, reading(ts="2026-11-14T10:00:00.000Z")).status_code == 202
    again = reading(ts="2026-11-14T12:00:00.000+02:00")
    assert send(client, again).status_code == 409


def test_header_must_match_payload(client, repo):
    r = send(client, reading(), key="MTR-999999:2026-01-01T00:00:00.000Z")
    assert r.status_code == 422
    assert repo.readings == {}


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1,2,3]",
        json.dumps({"timestamp": "2026-11-14T10:00:00.000Z"}).encode(),
        json.dumps({"meter_id": "MTR-1"}).encode(),
        json.dumps({"meter_id": "MTR-1", "timestamp": "yesterday"}).encode(),
        json.dumps({"meter_id": "../../etc/passwd", "timestamp": "2026-11-14T10:00:00.000Z"}).encode(),
    ],
)
def test_malformed_reading_is_422_and_stores_nothing(client, repo, pg_storage, raw):
    assert send(client, None, key=None, raw=raw).status_code == 422
    assert repo.readings == {} and pg_storage.objects == {}


def test_oversized_reading_is_413(client, repo):
    big = reading()
    big["padding"] = "x" * (1024 * 1024 + 10)
    assert send(client, big).status_code == 413
    assert repo.readings == {}


def test_storage_failure_is_503_and_the_retry_then_succeeds(client, pg_storage, repo):
    """Never lost: a failed write must not leave a claim that turns the retry into a 409.
    Since slice G a storage outage alone queues the reading (202, test_slice_g.py); this
    covers the case where the outbox cannot take it either."""
    p = reading()
    pg_storage.down = True

    async def outbox_down(*args):
        raise ConnectionError("metadata DB unreachable")

    repo.queue_reading = outbox_down
    assert send(client, p).status_code == 503
    assert repo.readings == {} and repo.files == {}
    pg_storage.down = False
    assert send(client, p).status_code == 202
    assert send(client, p).status_code == 409


def test_duplicate_while_original_in_flight_gets_503_not_409(client, repo):
    """A concurrent duplicate must not be told 'stored' before the original is durable."""
    p = reading()
    repo.seed_pending_claim("MTR-000001", "2026-11-14T10:00:00.000Z", age_seconds=1)
    r = send(client, p)
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"


def test_stale_claim_from_a_crashed_request_is_taken_over(client, repo, pg_storage):
    p = reading()
    repo.seed_pending_claim("MTR-000001", "2026-11-14T10:00:00.000Z", age_seconds=120)
    r = send(client, p)
    assert r.status_code == 202
    assert [x["status"] for x in repo.readings.values()] == ["stored"]
    assert len(pg_storage.objects) == 1


def test_stored_reading_downloads_byte_identical(client, auth):
    p = reading()
    fid = send(client, p).json()["file_id"]
    d = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert d.status_code == 200
    assert json.loads(d.content) == p
    assert d.headers["X-Integrity-Verified"] == "true"


def test_structured_fields_are_extracted_for_analytics(client, repo):
    send(client, reading(voltage=188.4, label="voltage_sag"))
    row = next(iter(repo.readings.values()))
    assert row["voltage_v"] == 188.4
    assert row["zone"] == "Muntenia"
    assert row["sim_label"] == "voltage_sag"


def _admin(client):
    token = client.post(
        "/api/v1/auth/login", json={"email": "admin@ppc.ro", "password": "pw-a"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_policy_can_move_meter_readings_and_idempotency_still_holds(client, storage):
    client.put("/api/v1/admin/policies", headers=_admin(client), json={"meter_readings": {
        "storage": "s3", "encryption": False, "deduplicate": True, "priority": "normal"}})
    p = reading()
    r = send(client, p)
    assert r.status_code == 202 and r.json()["storage"] == "s3"
    assert len(storage.objects) == 1
    assert send(client, p).status_code == 409


def test_meter_ingest_requires_auth(client):
    assert send(client, reading(), headers={}).status_code == 401


# ---- generic sha256 dedup for categories with deduplicate: true (ADR-0004) ----

def _upload(client, auth, data):
    return client.post("/api/v1/ingest", headers=auth,
                       files={"file": ("a.pdf", data)}, data={"category": "outage_records"})


def test_sha_dedup_when_policy_enables_it(client, auth, storage):
    client.put("/api/v1/admin/policies", headers=_admin(client), json={"outage_records": {
        "storage": "s3", "encryption": False, "deduplicate": True, "priority": "high"}})
    first = _upload(client, auth, b"same-bytes")
    assert first.status_code == 202
    dup = _upload(client, auth, b"same-bytes")
    assert dup.status_code == 409
    assert dup.json()["file_id"] == first.json()["file_id"]
    assert len(storage.objects) == 1
    assert _upload(client, auth, b"other-bytes").status_code == 202


def test_no_sha_dedup_when_policy_disables_it(client, auth, storage):
    assert _upload(client, auth, b"same").status_code == 202
    assert _upload(client, auth, b"same").status_code == 202
    assert len(storage.objects) == 2
