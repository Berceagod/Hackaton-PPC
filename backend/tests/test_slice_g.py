"""Slice G: isolation between categories and resilience to a storage going down."""

import asyncio
import hashlib
import json
import time
import uuid

import pytest
from fastapi import HTTPException

from crypto_envelope import MAGIC

SIM = {"Authorization": "Bearer sim-token"}


def _reading(meter="RO-MUN-000001", ts="2026-11-14T10:00:00.000Z", extra=None):
    return json.dumps({"meter_id": meter, "timestamp": ts, "zone": "Muntenia",
                       "instant": {"voltage_v": 229.5}, **(extra or {})}).encode()


def _send(client, body):
    return client.post("/api/v1/ingest", headers=SIM, files={"file": ("r.json", body)},
                       data={"category": "meter_readings"})


def _drain(client, retry_after=0.0):
    from meter_outbox import drain_once

    deps = client.app.state.deps
    return asyncio.run(drain_once(deps, retry_after=retry_after))


# ---------------- outbox: a storage outage loses no reading ----------------

def test_reading_is_accepted_durably_while_its_storage_is_down(client, pg_storage, repo, auth):
    pg_storage.down = True
    body = _reading()
    r = _send(client, body)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued" and r.json()["sha256"] == hashlib.sha256(body).hexdigest()
    row = next(iter(repo.readings.values()))
    assert row["status"] == "queued" and row["raw"] == body
    assert repo.files == {}                       # not in a storage yet, so not listed
    assert _send(client, body).status_code == 409  # accepted once: a resend is a duplicate

    assert _drain(client) == 0                    # still down: stays queued, nothing lost
    assert row["status"] == "queued"

    pg_storage.down = False
    assert _drain(client) == 1
    assert row["status"] == "stored" and row["raw"] is None
    fid = r.json()["file_id"]
    assert pg_storage.objects[repo.files[fid].storage_key] == body   # byte-identical
    d = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert d.status_code == 200 and d.content == body
    assert _send(client, body).status_code == 409
    assert _drain(client) == 0


def test_drain_respects_the_policy_and_encryption_at_write_time(client, pg_storage, storage,
                                                              admin, repo):
    pg_storage.down = True
    fid = _send(client, _reading()).json()["file_id"]
    client.put("/api/v1/admin/policies", headers=admin, json={"meter_readings": {
        "storage": "s3", "encryption": True, "deduplicate": True, "priority": "normal"}})
    assert _drain(client) == 1
    rec = repo.files[fid]
    assert rec.storage == "s3" and rec.encrypted is True
    assert storage.objects[rec.storage_key].startswith(MAGIC)
    assert client.get(f"/api/v1/files/{fid}/download", headers=admin).content == _reading()


def test_drain_waits_before_retrying_the_same_reading(client, pg_storage):
    pg_storage.down = True
    _send(client, _reading())
    pg_storage.down = False
    assert _drain(client, retry_after=60) == 0    # just queued: not retried immediately
    assert _drain(client, retry_after=0) == 1


def test_many_queued_readings_all_arrive_once(client, pg_storage, repo):
    pg_storage.down = True
    sent = [_reading(ts=f"2026-11-14T10:00:{i:02d}.000Z") for i in range(30)]
    assert all(_send(client, b).status_code == 202 for b in sent)
    pg_storage.down = False
    total = 0
    while (n := _drain(client)) > 0:
        total += n
    assert total == 30
    assert sorted(pg_storage.objects.values()) == sorted(sent)
    assert all(r["status"] == "stored" for r in repo.readings.values())


# ---------------- circuit breaker: fail fast instead of waiting on timeouts ----------------

def test_breaker_opens_after_repeated_failures_then_tries_again():
    from storage.base import StorageError
    from storage.breaker import BreakerStorage

    calls = {"n": 0}

    class Flaky:
        name = "ftp"
        fail = True

        async def put_stream(self, key, chunks):
            calls["n"] += 1
            if self.fail:
                raise StorageError("down")
            return 0

        def get_stream(self, key):
            raise NotImplementedError

        async def delete(self, key):
            pass

        async def healthy(self):
            return not self.fail

    async def empty():
        if False:
            yield b""

    inner = Flaky()
    b = BreakerStorage(inner, threshold=3, cooldown=0.2)
    assert b.name == "ftp"

    async def scenario():
        for _ in range(3):
            with pytest.raises(StorageError):
                await b.put_stream("k", empty())
        assert calls["n"] == 3 and b.state == "open"
        with pytest.raises(StorageError):          # open: no call reaches the storage
            await b.put_stream("k", empty())
        assert calls["n"] == 3
        await asyncio.sleep(0.25)
        inner.fail = False                         # half-open: one trial, it succeeds
        await b.put_stream("k", empty())
        assert calls["n"] == 4 and b.state == "closed"

    asyncio.run(scenario())


# ---------------- bulkheads: one category cannot take the others' capacity ----------------

def test_a_full_category_does_not_block_another():
    from bulkhead import Bulkheads

    heads = Bulkheads({"high": 1, "normal": 1, "low": 1}, wait_seconds=0.05)

    async def scenario():
        async with heads.slot("meter_readings", "normal"):
            with pytest.raises(HTTPException) as exc:     # meter readings are full ...
                async with heads.slot("meter_readings", "normal"):
                    pass
            assert exc.value.status_code == 503 and "Retry-After" in exc.value.headers
            async with heads.slot("outage_records", "high"):  # ... outages still get in
                pass
        async with heads.slot("meter_readings", "normal"):    # released afterwards
            pass

    asyncio.run(scenario())


def test_bulkhead_size_follows_policy_priority():
    from bulkhead import Bulkheads

    heads = Bulkheads({"high": 3, "normal": 2, "low": 1}, wait_seconds=0.01)
    assert heads.limit("high") == 3 and heads.limit("low") == 1


def test_meter_readings_use_their_own_database_pool(storage, pg_storage, ftp_storage,
                                                    repo, policies):
    from fastapi.testclient import TestClient

    from conftest import SETTINGS, FakeRepo
    from deps import Deps
    from main import create_app

    meter_repo = FakeRepo()
    deps = Deps(SETTINGS, repo, {"s3": storage, "postgres": pg_storage, "ftp": ftp_storage},
                policies, meter_repo=meter_repo)
    with TestClient(create_app(deps)) as c:
        assert _send(c, _reading()).status_code == 202
    assert len(meter_repo.readings) == 1 and repo.readings == {}


# ---------------- abandoned chunked uploads are cleaned up ----------------

def test_stale_uploads_are_purged_with_their_staged_chunks(client, auth, storage, repo):
    from chunked_upload import purge_stale_uploads

    def start(uid):
        r = client.post("/api/v1/upload/chunk", headers=auth, files={"file_chunk": ("b", b"x" * 100)},
                        data={"upload_id": uid, "chunk_index": "0", "total_chunks": "2",
                              "filename": "v.mp4", "category": "drone_videos", "metadata": "{}"})
        assert r.status_code == 200

    old, fresh = str(uuid.uuid4()), str(uuid.uuid4())
    start(old)
    start(fresh)
    repo.uploads[old]["updated_at"] = time.time() - 3 * 86400
    deps = client.app.state.deps
    assert asyncio.run(purge_stale_uploads(deps, older_than_seconds=86400)) == 1
    assert old not in repo.uploads and fresh in repo.uploads
    staged = [k for k in storage.objects if k.startswith("_staging/")]
    assert staged == [f"_staging/{fresh}/000000"]


# ---------------- policy changes reach every worker at once ----------------

def test_invalidate_makes_the_next_read_see_another_workers_change(repo):
    """Each worker process caches policies; a NOTIFY from the worker that saved a change
    calls invalidate() everywhere, so the next request reads the new policy at once."""
    from policies import CategoryPolicy, PolicyStore

    async def scenario():
        worker_a = PolicyStore({"outage_records": CategoryPolicy("s3", False, False, "high")},
                               persistence=repo, ttl=3600)
        worker_b = PolicyStore({"outage_records": CategoryPolicy("s3", False, False, "high")},
                               persistence=repo, ttl=3600)
        await worker_a.load()
        await worker_b.load()
        await worker_a.update({"outage_records": {"storage": "ftp", "encryption": False,
                                                  "deduplicate": False, "priority": "high"}})
        assert (await worker_b.get("outage_records")).storage == "s3"   # stale cache
        worker_b.invalidate()                                          # NOTIFY received
        assert (await worker_b.get("outage_records")).storage == "ftp"

    asyncio.run(scenario())
