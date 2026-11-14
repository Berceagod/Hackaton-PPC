"""Slice J: the backend is stateless, so capacity grows by adding instances.

Two independent app instances share only the database (FakeRepo) and the storages, as
N replicas behind nginx do. Any instance must be able to serve any request.
"""

import asyncio
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import SETTINGS
from deps import Deps
from main import create_app
from policies import PolicyStore

SIM = {"Authorization": "Bearer sim-token"}
DOC = b"raport avarie replica " * 2000


@pytest.fixture
def two_replicas(storage, pg_storage, ftp_storage, repo, policies):
    """Replica A and replica B: separate processes in production, shared state only in
    Postgres and the storages. Each gets its own PolicyStore (its own cache)."""
    storages = {"s3": storage, "postgres": pg_storage, "ftp": ftp_storage}
    b_policies = PolicyStore(dict(policies._policies), persistence=repo, ttl=3600)
    a = TestClient(create_app(Deps(SETTINGS, repo, storages, policies)))
    b = TestClient(create_app(Deps(SETTINGS, repo, storages, b_policies)))
    with a, b:
        yield a, b, b_policies


def _login(client, email="dispecer@ppc.ro", pw="pw-d"):
    token = client.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_upload_on_one_replica_downloads_from_another(two_replicas):
    a, b, _ = two_replicas
    auth = _login(a)  # a JWT issued by one replica is valid on every replica
    r = a.post("/api/v1/ingest", headers=auth, files={"file": ("avarie.pdf", DOC)},
               data={"category": "outage_records", "metadata": "{}"})
    assert r.status_code == 202
    d = b.get(f"/api/v1/files/{r.json()['file_id']}/download", headers=auth)
    assert d.status_code == 200 and d.content == DOC
    assert [f["id"] for f in b.get("/api/v1/files", headers=auth).json()] == [r.json()["file_id"]]


def test_chunks_of_one_upload_can_land_on_different_replicas(two_replicas):
    a, b, _ = two_replicas
    auth = _login(a)
    uid, parts = str(uuid.uuid4()), [DOC[:20000], DOC[20000:]]
    for i, (replica, part) in enumerate(zip((a, b), parts)):
        r = replica.post("/api/v1/upload/chunk", headers=auth,
                         files={"file_chunk": ("b", part)},
                         data={"upload_id": uid, "chunk_index": str(i), "total_chunks": "2",
                               "filename": "drona.mp4", "category": "drone_videos",
                               "metadata": "{}",
                               "chunk_sha256": hashlib.sha256(part).hexdigest()})
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "complete"
    assert a.get(f"/api/v1/upload/{uid}", headers=auth).json()["status"] == "complete"
    assert a.get(f"/api/v1/files/{r.json()['file_id']}/download", headers=auth).content == DOC


def test_a_reading_retried_on_another_replica_is_still_a_duplicate(two_replicas):
    a, b, _ = two_replicas
    body = json.dumps({"meter_id": "RO-BAN-000042", "timestamp": "2026-11-14T10:00:00.000Z",
                       "zone": "Banat"}).encode()

    def send(replica):
        return replica.post("/api/v1/ingest", headers=SIM, files={"file": ("r.json", body)},
                            data={"category": "meter_readings"})

    assert send(a).status_code == 202
    assert send(b).status_code == 409


def test_policy_change_on_one_replica_reaches_the_other(two_replicas, repo):
    a, b, b_policies = two_replicas
    admin = _login(a, "admin@ppc.ro", "pw-a")
    r = a.put("/api/v1/admin/policies", headers=admin, json={"outage_records": {
        "storage": "ftp", "encryption": False, "deduplicate": False, "priority": "high"}})
    assert r.status_code == 200
    b_policies.invalidate()  # what the Postgres NOTIFY does on every replica (slice G)
    up = b.post("/api/v1/ingest", headers=_login(b), files={"file": ("x.pdf", DOC)},
                data={"category": "outage_records", "metadata": "{}"})
    assert up.json()["storage"] == "ftp"


def test_queued_readings_are_drained_by_whichever_replica_runs_first(two_replicas, pg_storage,
                                                                      repo):
    from meter_outbox import drain_once

    a, b, _ = two_replicas
    pg_storage.down = True
    body = json.dumps({"meter_id": "RO-DOB-000007", "timestamp": "2026-11-14T11:00:00.000Z",
                       "zone": "Dobrogea"}).encode()
    r = a.post("/api/v1/ingest", headers=SIM, files={"file": ("r.json", body)},
               data={"category": "meter_readings"})
    assert r.json()["status"] == "queued"
    pg_storage.down = False
    stored = asyncio.run(drain_once(b.app.state.deps, retry_after=0))  # replica B drains it
    assert stored == 1 and asyncio.run(drain_once(a.app.state.deps, retry_after=0)) == 0
    assert list(pg_storage.objects.values()) == [body]
