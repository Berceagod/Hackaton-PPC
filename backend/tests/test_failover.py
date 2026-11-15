"""Automatic failover to the fallback storage, and the in-app alert feed (compact)."""

import asyncio
import json

import pytest

import failover

DATA = b"raport avarie " * 200
OUTAGE = {"storage": "s3", "encryption": False, "deduplicate": False, "priority": "high"}
METER = {"storage": "postgres", "encryption": False, "deduplicate": True, "priority": "normal"}
SIM = {"Authorization": "Bearer sim-token"}


@pytest.fixture(autouse=True)
def fresh():
    failover._last.clear()


def put(client, admin, category, base, **extra):
    return client.put("/api/v1/admin/policies", headers=admin, json={category: {**base, **extra}})


def outage(client, auth):
    return client.post("/api/v1/ingest", headers=auth, files={"file": ("a.pdf", DATA)},
                       data={"category": "outage_records"})


def reading(client, n):
    ts = f"2026-11-15T10:00:0{n}.000Z"
    body = json.dumps({"meter_id": f"MTR-F-{n}", "timestamp": ts, "zone": "Banat",
                       "instant": {"voltage_v": 229.7}, "events": [], "sim_label": None}).encode()
    return client.post("/api/v1/ingest", headers={**SIM, "Idempotency-Key": f"MTR-F-{n}:{ts}"},
                       files={"file": ("r.json", body, "application/json")}, data={"category": "meter_readings"})


def test_policy_validation_and_keeping_the_fallback(client, admin):
    assert put(client, admin, "outage_records", OUTAGE, fallback_storage="s3").status_code == 422
    assert put(client, admin, "outage_records", OUTAGE, fallback_storage="tape").status_code == 422
    assert put(client, admin, "outage_records", OUTAGE, fallback_storage="ftp").json()["outage_records"]["fallback_storage"] == "ftp"
    kept = client.put("/api/v1/admin/policies", headers=admin, json={"outage_records": OUTAGE}).json()   # UI never sends it
    assert kept["outage_records"]["fallback_storage"] == "ftp"
    cleared = put(client, admin, "outage_records", OUTAGE, fallback_storage=None).json()
    assert "fallback_storage" not in cleared["outage_records"]


def test_a_down_primary_sends_new_files_to_the_fallback_and_back(client, admin, auth, repo, storage, ftp_storage):
    put(client, admin, "outage_records", OUTAGE, fallback_storage="ftp")
    assert outage(client, auth).json()["storage"] == "s3"                       # healthy: primary
    storage.state = "open"                                                       # circuit breaker open
    r = outage(client, auth)
    assert r.status_code == 202 and r.json()["storage"] == "ftp" and len(ftp_storage.objects) == 1
    alert = repo.app_alerts["failover:outage_records"]
    assert alert["severity"] == "critical" and alert["resolved_at"] is None and "ftp" in alert["title"]
    storage.state = "closed"                                                     # the primary is back
    assert outage(client, auth).json()["storage"] == "s3"
    assert repo.app_alerts["failover:outage_records"]["resolved_at"] is not None


def test_no_fallback_means_no_failover(client, storage, repo):
    storage.state = "open"
    deps = client.app.state.deps
    assert failover.choose(deps, "outage_records", deps.policies._policies["outage_records"]) == ("s3", None)
    assert repo.app_alerts == {}


def test_an_overloaded_category_overflows_to_the_fallback(client, admin):
    put(client, admin, "outage_records", OUTAGE, fallback_storage="ftp")
    deps = client.app.state.deps

    class Busy:
        def utilisation(self, category, priority):
            return 0.9
    deps.bulkheads = Busy()
    policy = deps.policies._policies["outage_records"]
    assert failover.choose(deps, "outage_records", policy) == ("ftp", "overload")
    deps.bulkheads = None
    assert failover.choose(deps, "outage_records", policy) == ("s3", None)


def test_when_the_fallback_is_down_too_the_primary_is_kept(client, admin, storage, ftp_storage):
    put(client, admin, "outage_records", OUTAGE, fallback_storage="ftp")
    storage.state = ftp_storage.state = "open"
    deps = client.app.state.deps
    assert failover.choose(deps, "outage_records", deps.policies._policies["outage_records"]) == ("s3", None)


def test_a_meter_write_that_fails_is_retried_on_the_fallback_not_queued(client, admin, repo, pg_storage, storage):
    put(client, admin, "meter_readings", METER, fallback_storage="s3")
    pg_storage.down = True                                  # the write fails before any breaker opens
    r = reading(client, 1)
    assert r.status_code == 202 and r.json()["storage"] == "s3" and "status" not in r.json()
    assert len(storage.objects) == 1
    assert repo.app_alerts["failover:meter_readings"]["resolved_at"] is None


def test_the_alert_feed_merges_application_alerts_and_survives_no_prometheus(client, auth, repo):
    asyncio.run(repo.raise_alert("failover:drone_videos", "warning", "Failover activ", "detaliu", "drone_videos"))
    body = client.get("/api/v1/alerts", headers=auth).json()
    assert body["prometheus_ok"] is False
    assert body["counts"] == {"critical": 0, "warning": 1, "info": 0}
    assert body["alerts"][0]["source"] == "app" and body["alerts"][0]["category"] == "drone_videos"
    assert client.get("/api/v1/alerts").status_code == 401
    assert client.get("/api/v1/alerts", headers=SIM).status_code == 403   # the simulator token cannot read


def test_the_same_alert_for_several_storages_is_one_card_with_a_count():
    from routes.alerts import group, _from_prometheus
    raw = lambda st: {"labels": {"alertname": "FileIntegrityFailure", "severity": "critical", "category": "outage_records",  # noqa: E731
                                 "storage": st, "reason": "checksum_mismatch"},
                      "annotations": {"summary": f"failed on {st}"}, "state": "firing", "activeAt": "2026-11-15T09:46:29Z"}
    cards = group([_from_prometheus(raw(s)) for s in ("s3", "postgres", "ftp")])
    assert len(cards) == 1 and cards[0]["count"] == 3
    assert cards[0]["targets"] == ["outage_records · ftp", "outage_records · postgres", "outage_records · s3"]
    single = group([_from_prometheus(raw("s3"))])
    assert single[0]["count"] == 1 and single[0]["targets"] == [] and single[0]["title"] == "failed on s3"
