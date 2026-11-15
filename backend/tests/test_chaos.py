"""improvements.md 2.1: the chaos console (guards, storage faults, network, reaper)."""

import asyncio
import dataclasses
import time

import pytest

import chaos
from chaos import CHAOS_AUDIT_ID
from storage import chaos as storage_chaos
from storage.base import StorageError
from storage.chaos import ChaosStorage

DATA = b"raport avarie " * 400
SIM = {"Authorization": "Bearer sim-token"}


def run(coro):
    return asyncio.run(coro)


class FakeToxiproxy:
    def __init__(self):
        self.bad = False
        self.fail = False
        self.calls = []

    async def degraded(self):
        if self.fail:
            raise OSError("unreachable")
        return self.bad

    async def degrade(self):
        if self.fail:
            raise OSError("unreachable")
        self.bad = True
        self.calls.append("degrade")

    async def restore(self):
        if self.fail:
            raise OSError("unreachable")
        self.bad = False
        self.calls.append("restore")


@pytest.fixture
def on(client):
    """Chaos enabled, with a fake toxiproxy attached."""
    deps = client.app.state.deps
    deps.settings = dataclasses.replace(deps.settings, chaos_enabled=True)
    deps.toxiproxy = FakeToxiproxy()
    return deps


def put(client, admin, path, body):
    return client.put(f"/api/v1/admin/chaos/{path}", headers=admin, json=body)


# =============================== guards ===============================

def test_the_console_is_off_by_default(client, admin):
    assert client.get("/api/v1/admin/chaos", headers=admin).json()["enabled"] is False
    assert put(client, admin, "storage/s3", {"kind": "down"}).status_code == 403
    assert put(client, admin, "network", {"mode": "bad"}).status_code == 403
    assert client.delete("/api/v1/admin/chaos", headers=admin).status_code == 403
    assert client.delete("/api/v1/admin/chaos/storage/s3", headers=admin).status_code == 403


def test_only_an_admin_may_use_it(client, auth, on):
    for method, path in [("get", ""), ("put", "/storage/s3"), ("delete", "")]:
        r = getattr(client, method)(f"/api/v1/admin/chaos{path}", headers=auth, json={"kind": "down"}) \
            if method == "put" else getattr(client, method)(f"/api/v1/admin/chaos{path}", headers=auth)
        assert r.status_code == 403
    assert client.get("/api/v1/admin/chaos").status_code == 401
    assert client.get("/api/v1/admin/chaos", headers=SIM).status_code == 403


def test_the_status_describes_the_current_experiment(client, admin, on):
    put(client, admin, "storage/ftp", {"kind": "down", "duration_seconds": 30})
    body = client.get("/api/v1/admin/chaos", headers=admin).json()
    assert body["enabled"] is True and body["max_seconds"] == 600.0
    assert [f["target"] for f in body["faults"]] == ["ftp"]
    assert body["faults"][0]["kind"] == "down" and 0 < body["faults"][0]["remaining_seconds"] <= 30
    assert set(body["storages"]) == {"s3", "postgres", "ftp"}
    assert body["network_degraded"] is False
    assert body["recent"][0]["action"] == "chaos"


# =============================== storage faults ===============================

def test_a_storage_can_be_taken_down_and_brought_back(client, admin, on, repo):
    r = put(client, admin, "storage/s3", {"kind": "down", "duration_seconds": 30})
    assert r.status_code == 200 and r.json() == {"target": "s3", "kind": "down", "seconds": 0.0,
                                                  "duration_seconds": 30.0}
    assert run(repo.active_faults())[0]["target"] == "s3"
    assert client.delete("/api/v1/admin/chaos/storage/s3", headers=admin).json()["cleared"] == 1
    assert run(repo.active_faults()) == []


def test_a_slow_storage_carries_its_delay(client, admin, on, repo):
    put(client, admin, "storage/postgres", {"kind": "slow", "seconds": 1.5})
    fault = run(repo.active_faults())[0]
    assert fault["kind"] == "slow" and fault["seconds"] == 1.5
    assert 55 < fault["remaining_seconds"] <= 60        # the default duration


@pytest.mark.parametrize("body", [
    {}, {"kind": "explode"}, {"kind": "slow", "seconds": 0},  {"kind": "slow", "seconds": 31},
    {"kind": "slow", "seconds": "2"}, {"kind": "down", "duration_seconds": 1},
    {"kind": "down", "duration_seconds": 100000}, {"kind": "down", "duration_seconds": True},
])
def test_nonsense_is_refused(client, admin, on, body):
    assert put(client, admin, "storage/s3", body).status_code == 422


def test_unknown_storage_and_non_json_bodies(client, admin, on):
    assert put(client, admin, "storage/tape", {"kind": "down"}).status_code == 404
    assert client.delete("/api/v1/admin/chaos/storage/tape", headers=admin).status_code == 404
    bad = client.put("/api/v1/admin/chaos/storage/s3", headers=admin, content="nope")
    assert bad.status_code == 422
    assert client.put("/api/v1/admin/chaos/storage/s3", headers=admin, json=[1]).status_code == 422


def test_every_use_is_in_the_audit_trail_and_the_chain_stays_valid(client, admin, on, repo):
    put(client, admin, "storage/s3", {"kind": "down"})
    put(client, admin, "network", {"mode": "bad"})
    client.delete("/api/v1/admin/chaos/storage/s3", headers=admin)
    client.delete("/api/v1/admin/chaos", headers=admin)
    trail = run(repo.list_audit(CHAOS_AUDIT_ID))
    assert len(trail) == 4 and all(e["user"] == "admin@ppc.ro" for e in trail)
    assert client.get("/api/v1/audit/verify", headers=admin).json()["valid"] is True


# =============================== network ===============================

def test_the_network_can_be_degraded_and_restored(client, admin, on, repo):
    assert put(client, admin, "network", {"mode": "bad", "duration_seconds": 20}).json() == {
        "mode": "bad", "duration_seconds": 20.0}
    assert on.toxiproxy.bad and run(repo.active_faults())[0]["target"] == "network"
    assert client.get("/api/v1/admin/chaos", headers=admin).json()["network_degraded"] is True
    assert put(client, admin, "network", {"mode": "ok"}).json() == {"mode": "ok"}
    assert not on.toxiproxy.bad and run(repo.active_faults()) == []


@pytest.mark.parametrize("body", [{}, {"mode": "weird"}, {"mode": "bad", "duration_seconds": 1}])
def test_network_nonsense_is_refused(client, admin, on, body):
    assert put(client, admin, "network", body).status_code == 422


def test_an_unreachable_toxiproxy_is_a_clear_error(client, admin, on):
    on.toxiproxy.fail = True
    assert put(client, admin, "network", {"mode": "bad"}).status_code == 502
    status = client.get("/api/v1/admin/chaos", headers=admin).json()
    assert status["network_degraded"] is None                 # unknown, not a guess


def test_the_panic_button_clears_everything(client, admin, on, repo):
    put(client, admin, "storage/s3", {"kind": "down"})
    put(client, admin, "storage/ftp", {"kind": "slow", "seconds": 1})
    put(client, admin, "network", {"mode": "bad"})
    body = client.delete("/api/v1/admin/chaos", headers=admin).json()
    assert body == {"cleared": 3, "network_restored": True}
    assert run(repo.active_faults()) == [] and not on.toxiproxy.bad


def test_the_panic_button_works_even_if_toxiproxy_is_unreachable(client, admin, on, repo):
    put(client, admin, "storage/s3", {"kind": "down"})
    on.toxiproxy.fail = True
    body = client.delete("/api/v1/admin/chaos", headers=admin).json()
    assert body["cleared"] == 1 and body["network_restored"] is False


# =============================== the storage wrapper ===============================

@pytest.fixture
def fast_refresh(monkeypatch):
    monkeypatch.setattr(storage_chaos, "REFRESH_SECONDS", 0.0)


async def _drain(stream):
    return b"".join([c async for c in stream])


def test_without_a_fault_the_wrapper_is_transparent(storage, repo, fast_refresh):
    async def go():
        w = ChaosStorage(storage, repo)
        await w.put_stream("k", _one(b"abc"))
        assert await _drain(w.get_stream("k")) == b"abc" and await w.healthy()
        await w.delete("k")
        assert w.name == "s3"
    run(go())


async def _one(data):
    yield data


def test_a_down_fault_fails_every_operation(storage, repo, fast_refresh):
    async def go():
        w = ChaosStorage(storage, repo)
        await w.put_stream("k", _one(b"abc"))
        await repo.set_fault("s3", "down", 0, 30, "a")
        with pytest.raises(StorageError):
            await w.put_stream("k2", _one(b"x"))
        with pytest.raises(StorageError):
            await _drain(w.get_stream("k"))
        with pytest.raises(StorageError):
            await w.delete("k")
        assert await w.healthy() is False
        assert "k2" not in storage.objects and "k" in storage.objects   # nothing was touched
    run(go())


def test_a_slow_fault_adds_its_delay(storage, repo, fast_refresh):
    async def go():
        w = ChaosStorage(storage, repo)
        await repo.set_fault("s3", "slow", 0.3, 30, "a")
        start = time.monotonic()
        await w.put_stream("k", _one(b"abc"))
        assert time.monotonic() - start >= 0.3 and storage.objects["k"] == b"abc"
        assert await w.healthy() is True                      # slow is not down
    run(go())


def test_a_fault_for_another_storage_or_an_expired_one_is_ignored(storage, repo, fast_refresh):
    async def go():
        w = ChaosStorage(storage, repo)
        await repo.set_fault("ftp", "down", 0, 30, "a")
        await w.put_stream("k", _one(b"abc"))
        await repo.set_fault("s3", "down", 0, -1, "a")        # already expired
        await w.put_stream("k", _one(b"abc"))
    run(go())


def test_the_wrapper_recovers_once_the_fault_is_cleared(storage, repo, fast_refresh):
    async def go():
        w = ChaosStorage(storage, repo)
        await repo.set_fault("s3", "down", 0, 30, "a")
        with pytest.raises(StorageError):
            await w.put_stream("k", _one(b"abc"))
        await repo.clear_faults("s3")
        await w.put_stream("k", _one(b"abc"))
    run(go())


def test_if_the_fault_table_cannot_be_read_the_storage_behaves_normally(storage, fast_refresh):
    class Broken:
        async def active_faults(self):
            raise RuntimeError("db down")

    async def go():
        await ChaosStorage(storage, Broken()).put_stream("k", _one(b"abc"))
    run(go())


def test_the_fault_is_cached_between_refreshes(storage, repo):
    async def go():
        w = ChaosStorage(storage, repo)                     # real 1 s refresh
        await w.put_stream("k", _one(b"abc"))               # reads "no fault"
        await repo.set_fault("s3", "down", 0, 30, "a")
        await w.put_stream("k", _one(b"abc"))               # still within the refresh window
        w._read_at = float("-inf")
        with pytest.raises(StorageError):
            await w.put_stream("k", _one(b"abc"))
    run(go())


def test_attributes_of_the_real_adapter_stay_reachable(repo):
    class Real:
        name = "s3"
        marker = "x"
    assert ChaosStorage(Real(), repo).marker == "x"


# =============================== end to end ===============================

def _wrap(client, repo):
    deps = client.app.state.deps
    deps.storages = {n: ChaosStorage(s, repo) for n, s in deps.storages.items()}
    return deps


def test_a_simulated_outage_is_survived_through_the_replica(client, admin, auth, repo, fast_refresh):
    deps = _wrap(client, repo)
    client.put("/api/v1/admin/policies", headers=admin, json={"outage_records": {
        "storage": "s3", "encryption": False, "deduplicate": False, "priority": "high",
        "replicas": 1}})
    fid = client.post("/api/v1/ingest", headers=auth, files={"file": ("a.pdf", DATA)},
                      data={"category": "outage_records"}).json()["file_id"]
    run(repo.set_fault("s3", "down", 0, 30, "admin@ppc.ro"))
    r = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert r.status_code == 200 and r.content == DATA and r.headers["X-Served-From"] == "ftp"
    # a new upload while s3 is down is refused, not lost silently
    down = client.post("/api/v1/ingest", headers=auth, files={"file": ("b.pdf", DATA)},
                       data={"category": "outage_records"})
    assert down.status_code == 503
    run(repo.clear_faults("s3"))
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).headers.get("X-Served-From") is None
    assert deps


def test_other_categories_keep_working_while_one_storage_is_down(client, auth, repo, fast_refresh):
    _wrap(client, repo)
    run(repo.set_fault("s3", "down", 0, 30, "a"))             # outage_records and drone videos live here
    r = client.post("/api/v1/ingest", headers=auth, files={"file": ("c.pdf", DATA)},
                    data={"category": "connection_docs"})      # ftp
    assert r.status_code == 202


# =============================== the reaper ===============================

def test_the_reaper_restores_an_expired_network_fault(client, on, repo):
    run(repo.set_fault("network", "bad", 0, -1, "admin@ppc.ro"))
    on.toxiproxy.bad = True
    assert run(chaos.reap_once(on)) == ["network"]
    assert on.toxiproxy.bad is False
    assert any("a expirat" in (e["details"] or "") for e in run(repo.list_audit(CHAOS_AUDIT_ID)))


def test_the_reaper_leaves_running_faults_alone(client, on, repo):
    run(repo.set_fault("network", "bad", 0, 30, "admin@ppc.ro"))
    on.toxiproxy.bad = True
    assert run(chaos.reap_once(on)) == []
    assert on.toxiproxy.bad is True


def test_the_reaper_retries_when_toxiproxy_is_unreachable(client, on, repo):
    run(repo.set_fault("network", "bad", 0, -1, "admin@ppc.ro"))
    on.toxiproxy.fail = True
    run(chaos.reap_once(on))
    assert run(repo.active_faults())[0]["target"] == "network"   # still recorded: tried again later


def test_the_reaper_loop_does_nothing_when_chaos_is_off(client):
    async def go():
        await asyncio.wait_for(chaos.run_reaper(client.app.state.deps, asyncio.Event()), 2)
    run(go())                                                    # returns at once


def test_the_reaper_loop_runs_and_stops(client, on, repo):
    run(repo.set_fault("network", "bad", 0, -1, "admin@ppc.ro"))
    on.toxiproxy.bad = True

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(chaos.run_reaper(on, stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 3)
    run(go())
    assert on.toxiproxy.bad is False
