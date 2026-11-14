import asyncio

import pytest

from policies import PolicyError, PolicyStore, parse_policy_document

DATA = b"policy-routed-bytes " * 3000

OUTAGE_S3 = {"storage": "s3", "encryption": False, "deduplicate": False, "priority": "high"}


def _login(client, email, pw):
    t = client.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def admin(client):
    return _login(client, "admin@ppc.ro", "pw-a")


def _ingest(client, auth, category="outage_records"):
    return client.post(
        "/api/v1/ingest",
        headers=auth,
        files={"file": ("f.bin", DATA)},
        data={"category": category},
    )


def _outage_policy(storage):
    return {"outage_records": {**OUTAGE_S3, "storage": storage}}


def test_get_policies_matches_frontend_shape(client, auth):
    body = client.get("/api/v1/admin/policies", headers=auth).json()
    assert set(body) == {"meter_readings", "outage_records", "connection_docs", "drone_videos"}
    assert body["outage_records"] == OUTAGE_S3


def test_put_requires_admin(client, auth):
    assert client.put("/api/v1/admin/policies", headers=auth, json={}).status_code == 403


def test_put_unauthenticated_401(client):
    assert client.put("/api/v1/admin/policies", json={}).status_code == 401


@pytest.mark.parametrize("target", ["postgres", "ftp", "s3"])
def test_hot_reload_routes_new_uploads_to_new_storage(
    client, admin, storage, pg_storage, ftp_storage, target
):
    stores = {"s3": storage, "postgres": pg_storage, "ftp": ftp_storage}
    assert client.put(
        "/api/v1/admin/policies", headers=admin, json=_outage_policy(target)
    ).status_code == 200
    r = _ingest(client, admin)
    assert r.status_code == 202 and r.json()["storage"] == target
    assert len(stores[target].objects) == 1
    d = client.get(f"/api/v1/files/{r.json()['file_id']}/download", headers=admin)
    assert d.content == DATA and d.headers["X-Integrity-Verified"] == "true"


def test_old_files_stay_readable_after_policy_change(client, admin):
    fid = _ingest(client, admin).json()["file_id"]  # stored in s3
    client.put("/api/v1/admin/policies", headers=admin, json=_outage_policy("ftp"))
    d = client.get(f"/api/v1/files/{fid}/download", headers=admin)
    assert d.status_code == 200 and d.content == DATA  # still served from s3


def test_put_accepts_yaml_in_file_shape(client, admin):
    yaml_doc = (
        "categories:\n  drone_videos:\n    storage: postgres\n    encryption: false\n"
        "    deduplicate: false\n    priority: low\n"
    )
    r = client.put("/api/v1/admin/policies", headers=admin, content=yaml_doc)
    assert r.status_code == 200
    assert r.json()["drone_videos"]["storage"] == "postgres"


@pytest.mark.parametrize(
    "bad",
    [
        {"outage_records": {**OUTAGE_S3, "storage": "tape"}},
        {"outage_records": {**OUTAGE_S3, "encryption": "yes"}},
        {"outage_records": {**OUTAGE_S3, "priority": "urgent"}},
        {"outage_records": {"storage": "s3"}},
        {"nonsense": {}},
    ],
)
def test_invalid_policy_is_rejected_and_not_applied(client, admin, bad):
    assert client.put("/api/v1/admin/policies", headers=admin, json=bad).status_code == 422
    got = client.get("/api/v1/admin/policies", headers=admin).json()
    assert got["outage_records"]["storage"] == "s3"


def test_policy_persists_and_is_seen_by_other_replica(client, admin, repo):
    client.put("/api/v1/admin/policies", headers=admin, json=_outage_policy("ftp"))
    other_replica = PolicyStore(
        parse_policy_document(_outage_policy("s3")), persistence=repo, ttl=0
    )
    assert asyncio.run(other_replica.get("outage_records")).storage == "ftp"


def test_policy_store_seeds_db_from_defaults_when_empty(repo):
    store = PolicyStore(parse_policy_document(_outage_policy("s3")), persistence=repo)
    asyncio.run(store.load())
    assert repo.policies["outage_records"]["storage"] == "s3"


def test_parse_rejects_empty_document():
    with pytest.raises(PolicyError):
        parse_policy_document({})


def test_degraded_health_stays_200_when_one_storage_down(client, ftp_storage):
    ftp_storage.down = True
    r = client.get("/health")  # internal path; /api/v1/health needs a user (slice D)
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["ftp"] is False


def test_other_category_unaffected_when_ftp_down(client, admin, ftp_storage):
    ftp_storage.down = True
    assert _ingest(client, admin, "outage_records").status_code == 202  # s3 path
