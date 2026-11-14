import json
import time

import jwt
import pytest

from conftest import SETTINGS

SIM = {"Authorization": "Bearer sim-token"}
DATA = b"raport avarie " * 500


def _upload(client, auth, name="avarie.pdf", category="outage_records", zone="Banat"):
    return client.post(
        "/api/v1/ingest", headers=auth,
        files={"file": (name, DATA)},
        data={"category": category, "metadata": json.dumps({"zone": zone})},
    )


def _reading(client, meter="MTR-000001", ts="2026-11-14T10:00:00.000Z"):
    body = json.dumps({"meter_id": meter, "timestamp": ts, "zone": "Muntenia"}).encode()
    return client.post(
        "/api/v1/ingest", headers=SIM,
        files={"file": ("r.json", body)}, data={"category": "meter_readings"},
    )


# ---------------- roles: the simulator token is least-privilege ----------------

def test_service_token_may_only_ingest_meter_readings(client):
    assert _reading(client).status_code == 202
    assert _upload(client, SIM).status_code == 403
    assert _upload(client, SIM, category="connection_docs").status_code == 403


def test_service_token_cannot_read_anything(client, auth):
    fid = _upload(client, auth).json()["file_id"]
    for path in ("/files", f"/files/{fid}/download", f"/files/{fid}/verify",
                 f"/files/{fid}/audit", "/admin/policies"):
        assert client.get(f"/api/v1{path}", headers=SIM).status_code == 403, path


def test_dispecer_can_use_files_but_not_change_policies(client, auth):
    fid = _upload(client, auth).json()["file_id"]
    assert client.get("/api/v1/files", headers=auth).status_code == 200
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).status_code == 200
    assert client.get("/api/v1/admin/policies", headers=auth).status_code == 200
    assert client.put("/api/v1/admin/policies", headers=auth, json={}).status_code == 403


# ---------------- JWT validation ----------------

def _claims(**over):
    now = int(time.time())
    return {"sub": "admin@ppc.ro", "role": "admin", "name": "A", "iat": now, "exp": now + 600, **over}


def _get_files(client, token):
    return client.get("/api/v1/files", headers={"Authorization": f"Bearer {token}"})


def test_valid_token_is_accepted(client):
    assert _get_files(client, jwt.encode(_claims(), SETTINGS.jwt_secret, "HS256")).status_code == 200


def test_token_signed_with_another_secret_is_rejected(client):
    forged = jwt.encode(_claims(), "attacker-secret-attacker-secret-0000", "HS256")
    assert _get_files(client, forged).status_code == 401


def test_expired_token_is_rejected(client):
    old = jwt.encode(_claims(exp=int(time.time()) - 10), SETTINGS.jwt_secret, "HS256")
    assert _get_files(client, old).status_code == 401


def test_unsigned_token_is_rejected(client):
    assert _get_files(client, jwt.encode(_claims(), None, algorithm="none")).status_code == 401


def test_tampered_payload_is_rejected(client, auth):
    head, _payload, sig = auth["Authorization"].split(" ")[1].split(".")
    admin_claims = jwt.encode(_claims(), SETTINGS.jwt_secret, "HS256").split(".")[1]
    assert _get_files(client, f"{head}.{admin_claims}.{sig}").status_code == 401


@pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", "Bearer not.a.jwt"])
def test_missing_or_malformed_authorization(client, header):
    r = client.get("/api/v1/files", headers={"Authorization": header} if header else {})
    assert r.status_code == 401


def test_token_with_unknown_role_gets_403(client):
    weird = jwt.encode(_claims(role="intern"), SETTINGS.jwt_secret, "HS256")
    assert _get_files(client, weird).status_code == 403


# ---------------- /files listing ----------------

def test_default_list_shows_only_manually_uploaded_categories(client, auth):
    _upload(client, auth)
    _reading(client)
    rows = client.get("/api/v1/files", headers=auth).json()
    assert [r["category"] for r in rows] == ["outage_records"]


def test_meter_readings_are_listed_when_asked_for(client, auth):
    _upload(client, auth)
    _reading(client)
    rows = client.get("/api/v1/files?category=meter_readings", headers=auth).json()
    assert [r["category"] for r in rows] == ["meter_readings"]


def test_search_covers_all_categories_by_filename_trace_and_zone(client, auth):
    up = _upload(client, auth, name="trafo_T12.pdf", zone="Dobrogea")
    rd = _reading(client)
    by_name = client.get("/api/v1/files?q=TRAFO_t12", headers=auth).json()
    assert [r["id"] for r in by_name] == [up.json()["file_id"]]
    by_trace = client.get(f"/api/v1/files?q={rd.json()['trace_id']}", headers=auth).json()
    assert [r["id"] for r in by_trace] == [rd.json()["file_id"]]
    by_zone = client.get("/api/v1/files?q=dobro", headers=auth).json()
    assert [r["id"] for r in by_zone] == [up.json()["file_id"]]


def test_limit_and_offset(client, auth):
    ids = [_upload(client, auth, name=f"f{i}.pdf").json()["file_id"] for i in range(5)]
    newest_first = list(reversed(ids))
    page1 = client.get("/api/v1/files?limit=2", headers=auth).json()
    page2 = client.get("/api/v1/files?limit=2&offset=2", headers=auth).json()
    assert [r["id"] for r in page1] == newest_first[:2]
    assert [r["id"] for r in page2] == newest_first[2:4]


@pytest.mark.parametrize("qs", ["limit=0", "limit=5000", "offset=-1", "category=bogus", "storage=tape"])
def test_invalid_list_parameters_are_422(client, auth, qs):
    assert client.get(f"/api/v1/files?{qs}", headers=auth).status_code == 422


# ---------------- audit trail ----------------

def test_audit_uses_the_two_ui_actions_and_keeps_the_exact_event(client, auth, storage):
    fid = _upload(client, auth).json()["file_id"]
    client.get(f"/api/v1/files/{fid}/download", headers=auth)
    client.get(f"/api/v1/files/{fid}/verify", headers=auth)
    key = next(iter(storage.objects))
    storage.objects[key] = b"tampered"
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).status_code == 409

    entries = client.get(f"/api/v1/files/{fid}/audit", headers=auth).json()
    assert {e["action"] for e in entries} <= {"upload", "download"}  # what the UI can render
    assert {e["event"] for e in entries} == {"upload", "download", "verify", "download_blocked"}
    assert all(e["user"] == "dispecer@ppc.ro" and e["file_id"] == fid for e in entries)
    assert all({"id", "timestamp", "details"} <= set(e) for e in entries)
    blocked = next(e for e in entries if e["event"] == "download_blocked")
    assert "BLOCAT" in blocked["details"]


def test_audit_of_unknown_file_is_404(client, auth):
    r = client.get("/api/v1/files/00000000-0000-4000-8000-00000000dead/audit", headers=auth)
    assert r.status_code == 404


# ---------------- liveness vs dependency health ----------------

def test_livez_is_up_even_when_every_storage_is_down(client, storage, pg_storage, ftp_storage):
    for s in (storage, pg_storage, ftp_storage):
        s.down = True
    assert client.get("/livez").json() == {"status": "alive"}
    assert client.get("/health").json()["status"] == "degraded"


def test_hanging_dependency_is_reported_down_not_waited_for(client, ftp_storage, monkeypatch):
    import asyncio

    from routes import health

    async def hang():
        await asyncio.sleep(60)

    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ftp_storage, "healthy", hang)
    body = client.get("/health").json()
    assert body["checks"]["ftp"] is False and body["checks"]["s3"] is True


# ---------------- health: dependency report is not public ----------------

def test_public_health_needs_a_logged_in_user(client, auth):
    # /api/v1/* is what nginx exposes to the internet: the storage map is not for anonymous eyes
    assert client.get("/api/v1/health").status_code == 401
    assert client.get("/api/v1/health", headers=SIM).status_code == 403
    r = client.get("/api/v1/health", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_internal_health_and_liveness_stay_open(client):
    # root paths are reachable only on the Docker network (nginx proxies /api/ only)
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/livez").json() == {"status": "alive"}
    assert client.get("/api/v1/livez").json() == {"status": "alive"}


# ---------------- download of files with Romanian names ----------------

def test_download_keeps_a_non_ascii_filename(client, auth):
    name = "raport_întrerupere_ștație.pdf"
    fid = _upload(client, auth, name=name).json()["file_id"]
    r = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert r.status_code == 200 and r.content == DATA
    cd = r.headers["content-disposition"]
    assert 'filename="raport_intrerupere_statie.pdf"' in cd  # ASCII fallback
    assert "filename*=UTF-8''raport_%C3%AEntrerupere_%C8%99ta%C8%9Bie.pdf" in cd  # exact name


def test_header_breaking_characters_are_removed_from_filename():
    # a stored filename can contain anything a client sent; the header must stay one line
    from routes.files import _content_disposition

    cd = _content_disposition('a"b' + "\\" + 'c;\r\nd/e.pdf')
    assert "\r" not in cd and "\n" not in cd
    assert cd.startswith('attachment; filename="abc;de.pdf"; ')
    cd.encode("latin-1")  # must be a valid HTTP header value
