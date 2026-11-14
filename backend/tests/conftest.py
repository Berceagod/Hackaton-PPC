import os
import sys
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import Settings  # noqa: E402
from deps import Deps  # noqa: E402
from main import create_app  # noqa: E402
from policies import CategoryPolicy, PolicyStore  # noqa: E402
from repo import FileRecord  # noqa: E402
from storage.base import ObjectNotFound, StorageError  # noqa: E402


class FakeStorage:
    def __init__(self, name="s3"):
        self.name = name
        self.objects: dict[str, bytes] = {}
        self.down = False

    async def put_stream(self, key, chunks):
        if self.down:
            raise StorageError("down")
        data = bytearray()
        async for c in chunks:
            data += c
        self.objects[key] = bytes(data)
        return len(data)

    async def get_stream(self, key):
        if self.down:
            raise StorageError("down")
        if key not in self.objects:
            raise ObjectNotFound(key)
        data = self.objects[key]
        for i in range(0, len(data), 1000):
            yield data[i : i + 1000]

    async def delete(self, key):
        self.objects.pop(key, None)

    async def healthy(self):
        return not self.down


class FakeRepo:
    def __init__(self):
        self.files: dict[str, FileRecord] = {}
        self.audit: list[dict] = []
        self.readings: dict[tuple, dict] = {}

    async def init_schema(self):
        pass

    async def insert_file(self, rec):
        self.files[rec.id] = rec

    async def get_file(self, file_id):
        return self.files.get(file_id)

    async def list_files(self, category, storage, q, limit, offset=0, exclude_category=None):
        rows = list(reversed(self.files.values()))  # newest first
        if category:
            rows = [r for r in rows if r.category == category]
        if exclude_category:
            rows = [r for r in rows if r.category != exclude_category]
        if storage:
            rows = [r for r in rows if r.storage == storage]
        if q:
            needle = q.lower()
            rows = [
                r
                for r in rows
                if needle in r.filename.lower()
                or needle in r.trace_id.lower()
                or needle in (r.zone or "").lower()
            ]
        return rows[offset : offset + limit]

    async def add_audit(self, file_id, action, user, trace_id, details):
        self.audit.append(
            {
                "id": str(len(self.audit)),
                "file_id": file_id,
                "action": action,
                "user": user,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "details": details,
            }
        )

    async def list_audit(self, file_id):
        return [a for a in reversed(self.audit) if a["file_id"] == file_id]

    async def find_by_sha(self, category, sha256):
        for rec in self.files.values():
            if rec.category == category and rec.sha256 == sha256:
                return rec
        return None

    # ---- meter readings (mirrors PostgresRepo semantics) ----
    async def claim_reading(self, claim, stale_after):
        key = (claim["meter_id"], claim["reading_ts"])
        row = self.readings.get(key)
        if row is None:
            self.readings[key] = {**claim, "status": "pending", "claimed_at": time.time()}
            return "new", claim["file_id"]
        if row["status"] == "stored":
            return "duplicate", row["file_id"]
        if time.time() - row["claimed_at"] > stale_after:
            row["claimed_at"] = time.time()
            return "takeover", row["file_id"]
        return "in_flight", row["file_id"]

    async def complete_reading(self, meter_id, reading_ts, rec):
        self.files[rec.id] = rec
        self.readings[(meter_id, reading_ts)]["status"] = "stored"

    async def release_reading(self, meter_id, reading_ts, file_id):
        row = self.readings.get((meter_id, reading_ts))
        if row and row["status"] == "pending" and row["file_id"] == file_id:
            del self.readings[(meter_id, reading_ts)]

    def seed_pending_claim(self, meter_id, ts, age_seconds):
        """Simulate a claim left behind by another (in-flight or crashed) request."""
        reading_ts = datetime.fromisoformat(ts).astimezone(timezone.utc)
        self.readings[(meter_id, reading_ts)] = {
            "meter_id": meter_id,
            "reading_ts": reading_ts,
            "file_id": "00000000-0000-4000-8000-000000000001",
            "status": "pending",
            "claimed_at": time.time() - age_seconds,
        }

    async def load_policies(self):
        return self.policies

    async def save_policies(self, policies):
        self.policies = policies

    policies = None

    async def healthy(self):
        return True


SETTINGS = Settings(
    jwt_secret="test-secret-test-secret-test-secret",
    simulator_token="sim-token",
    database_url="unused",
    s3_endpoint="unused",
    s3_access_key="x",
    s3_secret_key="x",
    s3_bucket="b",
    policies_path="unused",
    blob_database_url="unused",
    ftp_host="unused",
    ftp_port=21,
    ftp_user="u",
    ftp_password="p",
    users={
        "dispecer@ppc.ro": {"password": "pw-d", "role": "dispecer", "name": "D"},
        "admin@ppc.ro": {"password": "pw-a", "role": "admin", "name": "A"},
    },
)

POLICIES = PolicyStore(
    {
        "meter_readings": CategoryPolicy("postgres", False, True, "normal"),
        "outage_records": CategoryPolicy("s3", False, False, "high"),
        "connection_docs": CategoryPolicy("ftp", True, False, "normal"),
        "drone_videos": CategoryPolicy("s3", False, False, "low"),
    }
)


@pytest.fixture
def storage():
    return FakeStorage()


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def pg_storage():
    return FakeStorage("postgres")


@pytest.fixture
def ftp_storage():
    return FakeStorage("ftp")


@pytest.fixture
def policies(repo):
    # fresh store per test, persisted through the fake repo, no refresh delay
    return PolicyStore(dict(POLICIES._policies), persistence=repo, ttl=0)


@pytest.fixture
def client(storage, pg_storage, ftp_storage, repo, policies):
    storages = {"s3": storage, "postgres": pg_storage, "ftp": ftp_storage}
    app = create_app(Deps(SETTINGS, repo, storages, policies))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(client):
    token = client.post(
        "/api/v1/auth/login", json={"email": "dispecer@ppc.ro", "password": "pw-d"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}
