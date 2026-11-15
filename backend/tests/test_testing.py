"""Testing overlay: smoke tests only (guards, validation, generator state, live endpoint)."""

import asyncio
import dataclasses

import testing


def run(coro):
    return asyncio.run(coro)


def deps_of(client):
    return client.app.state.deps


def enable(client):
    d = deps_of(client)
    d.settings = dataclasses.replace(d.settings, testing_enabled=True)
    return d


def test_off_by_default_and_admin_only(client, admin, auth):
    assert client.get("/api/v1/admin/testing", headers=admin).json() == {"enabled": False}
    assert client.put("/api/v1/admin/testing/generator", headers=admin, json={"on": True}).status_code == 403
    enable(client)
    assert client.get("/api/v1/admin/testing", headers=auth).status_code == 403
    assert client.post("/api/v1/admin/testing/jobs/outage-flood", headers=auth).status_code == 403


def test_the_generator_validates_and_toggles(client, admin):
    enable(client)
    put = lambda b: client.put("/api/v1/admin/testing/generator", headers=admin, json=b)  # noqa: E731
    assert put({"on": True, "categories": []}).status_code == 422
    assert put({"on": True, "categories": ["meter_readings"]}).status_code == 422
    assert put({"on": True, "categories": ["outage_records"], "rate": 99}).status_code == 422
    assert put({"on": True, "categories": ["outage_records", "drone_videos"], "rate": 2}).json()["on"] is True
    s = client.get("/api/v1/admin/testing", headers=admin).json()
    assert s["generator"]["on"] and s["generator"]["categories"] == ["outage_records", "drone_videos"]
    assert put({"on": False}).json() == {"on": False}
    assert client.get("/api/v1/admin/testing", headers=admin).json()["generator"]["on"] is False


def test_only_one_worker_generates_and_a_stopped_generator_sends_nothing(client, admin, repo, monkeypatch):
    d = enable(client)
    sent = []

    async def fake(deps, category, n):
        sent.append(category)
        return 202
    monkeypatch.setattr(testing, "send_one", fake)
    assert run(testing.generator_tick(d, {})) == 0                       # off: nothing
    client.put("/api/v1/admin/testing/generator", headers=admin,
               json={"on": True, "categories": ["outage_records", "connection_docs"], "rate": 2})
    assert run(testing.generator_tick(d, {})) == 4 and sorted(sent) == ["connection_docs"] * 2 + ["outage_records"] * 2
    repo.leases["generator"] = ("another-worker", __import__("time").time() + 5)   # someone else holds it
    assert run(testing.generator_tick(d, {})) == 0


def test_a_job_runs_once_at_a_time_and_reports(client, admin, monkeypatch):
    enable(client)

    async def quick(deps):
        return {"summary": "gata"}
    monkeypatch.setitem(testing.RUNNERS, "duplicates", quick)

    async def go():
        assert await testing.start_job(deps_of(client), "duplicates", "admin@ppc.ro") is True
        assert await testing.start_job(deps_of(client), "duplicates", "admin@ppc.ro") is False   # still running
        await asyncio.sleep(0.1)
        return await deps_of(client).repo.get_testing("job")
    job = run(go())
    assert job["running"] is False and job["summary"] == "gata"
    assert client.post("/api/v1/admin/testing/jobs/nope", headers=admin).status_code == 404


def test_synthetic_files_are_labelled_and_live_endpoint_survives_no_prometheus(client, auth):
    for cat in testing.GENERATOR_CATEGORIES:
        name, data, meta = testing.synthetic_file(cat, 1)
        assert name.startswith("gen_") and "sintetic" in meta["description"] and data
    r = client.get("/api/v1/analytics/live", headers=auth)
    assert r.status_code == 200 and r.json()["available"] is False


def test_quiet_mode_pauses_the_canary_and_the_scrubber_and_expires(client, admin, auth, repo, metric, storage):
    import canary
    import scrubber
    d = enable(client)
    d.settings = dataclasses.replace(d.settings, canary_interval_seconds=1.0, scrub_interval_seconds=1.0)
    client.post("/api/v1/ingest", headers=auth, files={"file": ("a.pdf", b"x" * 500)},
                data={"category": "outage_records"})
    assert client.put("/api/v1/admin/testing/quiet", headers=admin, json={"on": True, "duration_seconds": 5}).status_code == 422
    assert client.put("/api/v1/admin/testing/quiet", headers=auth, json={"on": True}).status_code == 403
    assert client.put("/api/v1/admin/testing/quiet", headers=admin, json={"on": True}).json()["on"] is True
    assert client.get("/api/v1/admin/testing", headers=admin).json()["quiet"]["on"] is True
    before = metric("filegrid_canary_runs_total", storage="s3", outcome="ok")

    async def go(seconds):
        stop = asyncio.Event()
        tasks = [asyncio.create_task(canary.run_canary(d, stop)), asyncio.create_task(scrubber.run_scrubber(d, stop))]
        await asyncio.sleep(seconds)
        stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
    run(go(0.4))
    assert metric("filegrid_canary_runs_total", storage="s3", outcome="ok") == before          # paused
    assert all(s["status"] is None for s in repo.scrub.values())                               # nothing scrubbed
    client.put("/api/v1/admin/testing/quiet", headers=admin, json={"on": False})
    assert client.get("/api/v1/admin/testing", headers=admin).json()["quiet"]["on"] is False
    run(go(0.4))
    assert metric("filegrid_canary_runs_total", storage="s3", outcome="ok") > before           # running again
