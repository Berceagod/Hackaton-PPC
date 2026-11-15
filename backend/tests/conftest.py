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
        self.chain: list[dict] = []   # audit hash chain (tests tamper with it directly)
        self.scrub: dict[str, dict] = {}  # file_id -> {verified_at, status}
        self.faults: dict[str, dict] = {}   # chaos faults
        self.file_keys: dict[str, dict] = {}  # per-file wrapped keys
        self.testing: dict[str, tuple] = {}   # testing overlay state
        self.leases: dict[str, tuple] = {}
        self.logins: dict[str, dict] = {}     # failed-login counters
        self.replicas: dict[str, dict] = {}  # file_id -> {storage, storage_key, status}
        self.readings: dict[tuple, dict] = {}
        self.uploads: dict[str, dict] = {}
        self.upload_chunks: dict[str, dict[int, dict]] = {}

    # ---- chunked uploads (mirrors PostgresRepo semantics) ----
    async def open_upload(self, session):
        if session["id"] not in self.uploads:
            self.uploads[session["id"]] = {
                **session, "status": "open", "file_id": None, "reason": None,
                "updated_at": time.time(),
            }
            self.upload_chunks[session["id"]] = {}
        else:
            self.uploads[session["id"]]["updated_at"] = time.time()  # activity keeps it alive
        return dict(self.uploads[session["id"]])

    async def get_upload(self, upload_id):
        row = self.uploads.get(upload_id)
        return dict(row) if row else None

    async def record_chunk(self, upload_id, index, size, sha256):
        self.upload_chunks[upload_id][index] = {"size": size, "sha256": sha256}
        return len(self.upload_chunks[upload_id])

    async def received_chunks(self, upload_id):
        return sorted(self.upload_chunks.get(upload_id, {}))

    async def start_assembly(self, upload_id, stale_after):
        row = self.uploads[upload_id]
        stale = row["status"] == "assembling" and time.time() - row["updated_at"] > stale_after
        if row["status"] == "open" or stale:
            row["status"], row["updated_at"] = "assembling", time.time()
            return True
        return False

    async def complete_upload(self, upload_id, rec):
        self.files[rec.id] = rec
        self.uploads[upload_id].update(status="complete", file_id=rec.id)

    async def reopen_upload(self, upload_id):
        self.uploads[upload_id]["status"] = "open"

    async def fail_upload(self, upload_id, reason):
        self.uploads[upload_id].update(status="failed", reason=reason)

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
        # the same hash chain PostgresRepo keeps (kept apart so list_audit's shape is unchanged)
        import audit_chain

        row = {"id": self.audit[-1]["id"], "file_id": file_id, "action": action,
               "username": user, "trace_id": trace_id, "details": details,
               "created_at": audit_chain.now_utc()}
        prev = self.chain[-1]["row_hash"] if self.chain else audit_chain.GENESIS
        self.chain.append({**row, "seq": len(self.chain) + 1, "prev_hash": prev,
                           "row_hash": audit_chain.row_hash(prev, row)})

    async def verify_audit_chain(self):
        import audit_chain

        verifier = audit_chain.ChainVerifier()
        for row in self.chain:
            verifier.feed(row)
        return verifier.result()

    # ---- background scrubber (mirrors PostgresRepo semantics) ----
    async def claim_for_scrub(self, categories, max_bytes, recheck_seconds, limit):
        now, picked = time.time(), []
        ordered = sorted(self.files.values(),
                         key=lambda r: (self.scrub.get(r.id, {}).get("verified_at") or 0))
        for rec in ordered:
            st = self.scrub.setdefault(rec.id, {"verified_at": None, "status": None})
            due = st["verified_at"] is None or now - st["verified_at"] > recheck_seconds
            if rec.category in categories and rec.size_bytes <= max_bytes and due:
                st.update(verified_at=now, status="checking")
                picked.append(rec)
                if len(picked) == limit:
                    break
        return picked

    async def finish_scrub(self, file_id, status):
        st = self.scrub.setdefault(file_id, {"verified_at": None, "status": None})
        if status is None:
            st.update(verified_at=None, status=None)
        else:
            st.update(verified_at=time.time(), status=status)

    # ---- testing overlay state (mirrors PostgresRepo) ----
    async def set_testing(self, key, value, duration):
        self.testing[key] = (dict(value), time.time() + duration)

    async def get_testing(self, key):
        row = self.testing.get(key)
        if not row or row[1] <= time.time():
            return None
        return {**row[0], "_remaining": round(row[1] - time.time(), 1)}

    async def clear_testing(self, key):
        self.testing.pop(key, None)

    async def try_lease(self, name, holder, seconds):
        cur = self.leases.get(name)
        if cur and cur[1] > time.time() and cur[0] != holder:
            return False
        self.leases[name] = (holder, time.time() + seconds)
        return True

    # ---- per-file keys and login attempts (mirror PostgresRepo) ----
    async def put_file_key(self, file_id, wrapped):
        self.file_keys.setdefault(file_id, {"wrapped": bytes(wrapped), "status": "active"})

    async def get_file_key(self, file_id):
        row = self.file_keys.get(file_id)
        return dict(row) if row else None

    async def erase_file_key(self, file_id, by):
        row = self.file_keys.get(file_id)
        if not row or row["status"] != "active":
            return False
        row.update(wrapped=None, status="erased", erased_by=by)
        return True

    async def list_file_keys(self, after, limit):
        ids = sorted(i for i, r in self.file_keys.items() if r["status"] == "active" and (after is None or i > after))
        return [{"file_id": i, "wrapped": self.file_keys[i]["wrapped"]} for i in ids[:limit]]

    async def replace_file_key(self, file_id, wrapped):
        if self.file_keys.get(file_id, {}).get("status") == "active":
            self.file_keys[file_id]["wrapped"] = bytes(wrapped)

    async def login_state(self, email):
        row = self.logins.get(email)
        return (row["failures"], time.time() - row["last"]) if row else (0, None)

    async def login_failed(self, email):
        row = self.logins.setdefault(email, {"failures": 0, "last": 0.0})
        row["failures"] += 1
        row["last"] = time.time()
        return row["failures"]

    async def login_ok(self, email):
        self.logins.pop(email, None)

    # ---- chaos faults (mirrors PostgresRepo; time is real) ----
    async def set_fault(self, target, kind, seconds, duration, by):
        self.faults[target] = {"target": target, "kind": kind, "seconds": float(seconds),
                               "created_by": by, "until": time.time() + duration}

    async def clear_faults(self, target=None):
        gone = [t for t in self.faults if target in (None, t)]
        for t in gone:
            del self.faults[t]
        return len(gone)

    async def active_faults(self):
        now = time.time()
        return [{"target": f["target"], "kind": f["kind"], "seconds": f["seconds"],
                 "created_by": f["created_by"], "expires_at": datetime.fromtimestamp(f["until"], timezone.utc).isoformat(),
                 "remaining_seconds": round(f["until"] - now, 1)}
                for f in sorted(self.faults.values(), key=lambda f: f["target"]) if f["until"] > now]

    async def expired_faults(self):
        now = time.time()
        gone = [t for t, f in self.faults.items() if f["until"] <= now]
        for t in gone:
            del self.faults[t]
        return gone

    # ---- replicas (mirrors PostgresRepo) ----
    async def set_replica(self, file_id, storage, storage_key, status):
        self.replicas[file_id] = {"storage": storage, "storage_key": storage_key, "status": status}

    async def get_replica(self, file_id):
        r = self.replicas.get(file_id)
        return dict(r) if r else None

    async def replica_stats(self):
        st = [r["status"] for r in self.replicas.values()]
        return {"total": len(st), "ok": st.count("ok"), "pending": st.count("pending"),
                "failed": st.count("failed")}

    async def scrub_stats(self, categories):
        recs = [r for r in self.files.values() if r.category in categories]
        sts = [self.scrub.get(r.id, {}) for r in recs]
        last = max((s.get("verified_at") or 0 for s in sts), default=0)
        return {
            "files_total": len(recs),
            "verified_ok": sum(1 for s in sts if s.get("status") == "ok"),
            "failed": sum(1 for s in sts if s.get("status") == "failed"),
            "never_checked": sum(1 for s in sts if not s.get("verified_at")),
            "last_checked_at": datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
        }

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
            self.readings[key] = {**claim, "status": "pending", "claimed_at": time.time(),
                                  "raw": None, "filename": None, "uploaded_by": None}
            return "new", claim["file_id"]
        if row["status"] in ("stored", "queued"):  # queued = durably accepted (slice G)
            return "duplicate", row["file_id"]
        if time.time() - row["claimed_at"] > stale_after:
            row["claimed_at"] = time.time()
            return "takeover", row["file_id"]
        return "in_flight", row["file_id"]

    async def complete_reading(self, meter_id, reading_ts, rec):
        self.files[rec.id] = rec
        row = self.readings[(meter_id, reading_ts)]
        row["status"], row["raw"] = "stored", None

    # ---- meter outbox (slice G) ----
    async def queue_reading(self, meter_id, reading_ts, raw, filename, uploaded_by):
        row = self.readings[(meter_id, reading_ts)]
        if row["status"] != "pending":
            return False
        row.update(status="queued", raw=raw, filename=filename, uploaded_by=uploaded_by,
                   claimed_at=time.time())
        return True

    async def claim_queued(self, limit, retry_after):
        now, out = time.time(), []
        for row in self.readings.values():
            if row["status"] == "queued" and now - row["claimed_at"] >= retry_after:
                row["claimed_at"] = now
                out.append(dict(row))
                if len(out) == limit:
                    break
        return out

    async def count_queued(self):
        return sum(1 for r in self.readings.values() if r["status"] == "queued")

    # ---- stale chunked uploads (slice G) ----
    async def list_stale_uploads(self, older_than_seconds):
        cutoff = time.time() - older_than_seconds
        return [(u["id"], u["total_chunks"]) for u in self.uploads.values()
                if u["status"] in ("open", "failed") and u["updated_at"] < cutoff]

    async def delete_upload(self, upload_id):
        self.uploads.pop(upload_id, None)
        self.upload_chunks.pop(upload_id, None)

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

    # ---- slice H (observability) FakeRepo methods: add directly below this line ----
    stall_stats = (0, 0)  # (in progress, stalled); tests set it

    async def count_unencrypted_pii(self):
        return sum(
            1 for r in self.files.values() if r.category == "connection_docs" and not r.encrypted
        )

    async def upload_stall_stats(self, stall_seconds):
        return self.stall_stats

    # ---- slice I (analytics) FakeRepo methods: add directly below this line ----

    def _analytics_store(self):
        if not hasattr(self, "_measurements"):
            self._measurements = []

    async def ensure_analytics_schema(self):
        self._analytics_store()

    async def save_measurements(self, kind, scenario, rows):
        self._analytics_store()
        for row in rows:
            self._measurements.append({"kind": kind, "scenario": scenario, **row})

    async def latest_measurements(self):
        self._analytics_store()
        latest = {}
        for row in self._measurements:
            key = (row["kind"], row.get("scenario") or "", row["category"], row["storage"])
            latest[key] = row
        return list(latest.values())

    async def analytics_snapshot(self):
        from analytics import snapshot_from_memory

        self._analytics_store()
        return snapshot_from_memory(self.files, self.readings, self.audit)

    # ---- slice J (scale-out) FakeRepo methods: add directly below this line ----


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
    master_key=bytes(range(32)),
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
def admin(client):
    token = client.post(
        "/api/v1/auth/login", json={"email": "admin@ppc.ro", "password": "pw-a"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth(client):
    token = client.post(
        "/api/v1/auth/login", json={"email": "dispecer@ppc.ro", "password": "pw-d"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ---- slice H (observability) fixtures: add directly below this line ----
@pytest.fixture
def metric():
    """Current value of a Prometheus sample, e.g. metric("filegrid_requests_total", category=...).
    The registry lives for the whole test run, so tests compare before/after values."""
    from prometheus_client import REGISTRY

    def value(name, **labels):
        return REGISTRY.get_sample_value(name, labels) or 0.0

    return value


@pytest.fixture
def breaker_client(storage, pg_storage, ftp_storage, repo, policies):
    """Like `client`, but every storage sits behind the circuit breaker (as in production),
    which is where storage-call timings are recorded."""
    from storage.breaker import BreakerStorage

    storages = {
        "s3": BreakerStorage(storage),
        "postgres": BreakerStorage(pg_storage),
        "ftp": BreakerStorage(ftp_storage),
    }
    app = create_app(Deps(SETTINGS, repo, storages, policies))
    with TestClient(app) as c:
        yield c

# ---- slice I (analytics) fixtures: add directly below this line ----

# ---- slice J (scale-out) fixtures: add directly below this line ----
