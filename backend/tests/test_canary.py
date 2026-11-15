"""improvements.md 2.8: the storage canary (put/get/verify/delete on every storage)."""

import asyncio
import dataclasses

import canary
from storage.base import StorageError


def run(coro):
    return asyncio.run(coro)


def deps_of(client):
    return client.app.state.deps


class Corrupting:
    """A storage that silently returns different bytes than it was given."""
    name = "ftp"

    def __init__(self):
        self.data = {}

    async def put_stream(self, key, chunks):
        self.data[key] = b"".join([c async for c in chunks])

    async def get_stream(self, key):
        yield self.data[key][:-1] + b"X"

    async def delete(self, key):
        self.data.pop(key, None)


def test_every_storage_is_checked_and_left_clean(client, storage, pg_storage, ftp_storage, metric):
    before = {n: metric("filegrid_canary_runs_total", storage=n, outcome="ok") for n in ("s3", "postgres", "ftp")}
    assert run(canary.canary_once(deps_of(client))) == {"s3": True, "postgres": True, "ftp": True}
    for name, store in (("s3", storage), ("postgres", pg_storage), ("ftp", ftp_storage)):
        assert store.objects == {}                              # the object was removed again
        assert metric("filegrid_canary_runs_total", storage=name, outcome="ok") == before[name] + 1


def test_a_down_storage_is_reported_and_the_others_are_not_affected(
        client, ftp_storage, metric):
    fails = metric("filegrid_canary_runs_total", storage="ftp", outcome="failed")
    ftp_storage.down = True
    done = run(canary.canary_once(deps_of(client)))
    assert done == {"s3": True, "postgres": True, "ftp": False}
    assert metric("filegrid_canary_runs_total", storage="ftp", outcome="failed") == fails + 1


def test_silently_different_bytes_are_a_failure(metric):
    bad = Corrupting()
    fails = metric("filegrid_canary_runs_total", storage="ftp", outcome="failed")
    assert run(canary.check_storage("ftp", bad)) is False
    assert bad.data == {}                                         # cleaned up even on failure
    assert metric("filegrid_canary_runs_total", storage="ftp", outcome="failed") == fails + 1


def test_the_canary_touches_no_user_data_or_database(client, repo, storage):
    run(canary.canary_once(deps_of(client)))
    assert repo.files == {} and repo.audit == []


def test_a_failing_delete_does_not_fail_the_check(storage):
    async def boom(key):
        raise StorageError("cannot delete")
    storage.delete = boom
    assert run(canary.check_storage("s3", storage)) is True


def test_the_loop_runs_and_stops(client, metric):
    set_interval = dataclasses.replace(deps_of(client).settings, canary_interval_seconds=1.0)
    deps_of(client).settings = set_interval
    before = metric("filegrid_canary_runs_total", storage="s3", outcome="ok")

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(canary.run_canary(deps_of(client), stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 3)
    run(go())
    assert metric("filegrid_canary_runs_total", storage="s3", outcome="ok") >= before + 1


def test_a_disabled_canary_does_nothing(client):
    deps_of(client).settings = dataclasses.replace(deps_of(client).settings, canary_enabled=False)

    async def go():
        await asyncio.wait_for(canary.run_canary(deps_of(client), asyncio.Event()), 2)
    run(go())                                                     # returns at once


def test_a_crashing_pass_does_not_stop_the_loop(client, monkeypatch):
    calls = []

    async def boom(deps):
        calls.append(1)
        raise RuntimeError("unexpected")
    monkeypatch.setattr(canary, "canary_once", boom)
    deps_of(client).settings = dataclasses.replace(deps_of(client).settings, canary_interval_seconds=1.0)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(canary.run_canary(deps_of(client), stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 3)
    run(go())
    assert calls


def test_the_canary_goes_through_the_chaos_wrapper(client, repo, storage):
    from storage.chaos import ChaosStorage
    import storage.chaos as chaos_mod
    chaos_mod.REFRESH_SECONDS, saved = 0.0, chaos_mod.REFRESH_SECONDS
    try:
        deps_of(client).storages = {"s3": ChaosStorage(storage, repo)}
        run(repo.set_fault("s3", "down", 0, 30, "admin"))
        assert run(canary.canary_once(deps_of(client))) == {"s3": False}
        run(repo.clear_faults("s3"))
        assert run(canary.canary_once(deps_of(client))) == {"s3": True}
    finally:
        chaos_mod.REFRESH_SECONDS = saved
