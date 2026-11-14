"""Slice H: observability. Metric contract (CLAUDE.md section 4), instrumentation of every
file operation, storage-call timings, state gauges and the /metrics endpoint."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid

import pytest
from fastapi import HTTPException

import metrics
from bulkhead import Bulkheads
from conftest import SETTINGS, FakeStorage
from deps import Deps
from logging_setup import JsonFormatter
from storage.base import ObjectNotFound, StorageError
from storage.breaker import BreakerStorage

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = b"raport avarie " * 700
SIM = {"Authorization": "Bearer sim-token"}


def run(coro):
    return asyncio.run(coro)


async def _chunks(*pieces, delay=0.0):
    for p in pieces:
        if delay:
            await asyncio.sleep(delay)
        yield p


@contextlib.contextmanager
def capture_json(logger_name="filegrid.metrics", level=logging.DEBUG):
    """Collect log lines as the JSON the backend emits, formatted AT EMIT TIME (that is when
    the trace id is read from the request context)."""
    lines = []
    handler = logging.Handler()
    handler.emit = lambda rec: lines.append((rec.levelno, json.loads(JsonFormatter().format(rec))))
    lg = logging.getLogger(logger_name)
    old_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield lines
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)


def _reading(meter="MTR-H-1", ts="2026-11-15T10:00:00.000Z"):
    return json.dumps({"meter_id": meter, "timestamp": ts, "zone": "Banat",
                       "instant": {"voltage_v": 229.5}}).encode()


def _ingest_reading(client, raw, headers=SIM):
    return client.post("/api/v1/ingest", headers=headers,
                       files={"file": ("r.json", raw)}, data={"category": "meter_readings"})


def _upload(client, auth, data=DATA, category="outage_records", name="avarie.pdf"):
    return client.post("/api/v1/ingest", headers=auth, files={"file": (name, data)},
                       data={"category": category})


# ===================== the contract: names, types, labels, buckets =====================

CONTRACT = {
    "filegrid_requests": ("counter", {"category", "storage", "operation", "outcome"}),
    "filegrid_request_duration_seconds": ("histogram", {"category", "storage", "operation"}),
    "filegrid_bytes": ("counter", {"category", "storage", "operation"}),
    "filegrid_storage_operation_duration_seconds": ("histogram", {"storage", "op", "outcome"}),
    "filegrid_integrity_failures": ("counter", {"category", "storage", "reason"}),
    "filegrid_bulkhead_rejections": ("counter", {"category"}),
    "filegrid_outbox_queued": ("gauge", set()),
    "filegrid_breaker_open": ("gauge", {"storage"}),
}


def _families():
    from prometheus_client import REGISTRY

    return {f.name: f for f in REGISTRY.collect()}


def _request_samples():
    return {(s.name, tuple(sorted(s.labels.items()))): s.value
            for f in _families().values() for s in f.samples
            if s.name == "filegrid_requests_total"}


def test_contract_names_types_and_labels():
    metrics.record("outage_records", "s3", "ingest", "ok", 0.1, 10)
    metrics.integrity_failure("outage_records", "s3", "checksum_mismatch")
    metrics.bulkhead_rejected("outage_records")
    metrics.storage_op("s3", "put", "ok", 0.01)
    metrics.OUTBOX.set(0)
    metrics.BREAKER_OPEN.labels("s3").set(0)
    fams = _families()
    for name, (mtype, labels) in CONTRACT.items():
        assert name in fams, f"missing metric {name}"
        fam = fams[name]
        assert fam.type == mtype, f"{name}: {fam.type} != {mtype}"
        seen = set()
        for s in fam.samples:
            seen |= set(s.labels) - {"le"}
        assert seen == labels, f"{name}: labels {seen} != {labels}"


@pytest.mark.parametrize("name", ["filegrid_request_duration_seconds",
                                  "filegrid_storage_operation_duration_seconds"])
def test_histogram_buckets_are_the_contract_buckets(name):
    metrics.record("outage_records", "s3", "ingest", "ok", 0.1)
    metrics.storage_op("s3", "put", "ok", 0.01)
    les = {float(s.labels["le"]) for s in _families()[name].samples if s.name.endswith("_bucket")}
    assert les == set(metrics.BUCKETS) | {float("inf")}
    assert metrics.BUCKETS == (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


def test_alert_critical_series_exist_from_the_start():
    """increase()/rate() need two samples: a counter that first appears at 1 would hide the
    very first corrupt file from its alert. These series must therefore exist at 0 already."""
    from policies import CATEGORIES

    get = metrics.REGISTRY.get_sample_value
    for category in CATEGORIES:
        assert get("filegrid_bulkhead_rejections_total", {"category": category}) is not None
        for storage in metrics.STORAGES:
            for reason in metrics.INTEGRITY_REASONS:
                assert get("filegrid_integrity_failures_total",
                           {"category": category, "storage": storage, "reason": reason}) is not None
    for storage in metrics.STORAGES:
        for outcome in ("ok", "duplicate", "queued", "client_error", "server_error"):
            assert get("filegrid_requests_total", {
                "category": "meter_readings", "storage": storage,
                "operation": "ingest", "outcome": outcome}) is not None


def test_the_alerts_integrity_reasons_are_the_ones_that_can_occur():
    """The alert rule lists the reasons that mean 'damaged in storage'; keep it in step with
    the reasons the code can emit."""
    rules = open(os.path.join(BACKEND_DIR, "..", "infrastructure", "prometheus", "alerts.yml"),
                 encoding="utf-8").read() if os.path.exists(
        os.path.join(BACKEND_DIR, "..", "infrastructure", "prometheus", "alerts.yml")) else None
    if rules is None:  # only backend/ is mounted
        return
    for reason in ("checksum_mismatch", "object_missing", "decryption_failed", "staging_corrupted"):
        assert reason in rules and reason in metrics.INTEGRITY_REASONS


def test_label_values_stay_inside_the_contract_sets():
    assert set(metrics.OPERATIONS) == {"ingest", "chunk", "download", "verify", "drain"}
    assert set(metrics.OUTCOMES) == {"ok", "duplicate", "queued", "client_error",
                                     "server_error", "integrity_error"}
    assert set(metrics.STORAGES) == {"postgres", "s3", "ftp"}


# ===================== observe(): outcome rules =====================

def _labels(category="outage_records", storage="s3", operation="ingest", outcome="ok"):
    return dict(category=category, storage=storage, operation=operation, outcome=outcome)


def test_observe_records_ok_with_storage_and_bytes(metric):
    lab = _labels()
    before = metric("filegrid_requests_total", **lab)
    bytes_before = metric("filegrid_bytes_total", category="outage_records", storage="s3",
                          operation="ingest")
    count_before = metric("filegrid_request_duration_seconds_count", category="outage_records",
                          storage="s3", operation="ingest")

    async def go():
        async with metrics.observe("outage_records", "ingest") as obs:
            obs.note(storage="s3")
            obs.add_bytes(1234)

    run(go())
    assert metric("filegrid_requests_total", **lab) == before + 1
    assert metric("filegrid_bytes_total", category="outage_records", storage="s3",
                  operation="ingest") == bytes_before + 1234
    assert metric("filegrid_request_duration_seconds_count", category="outage_records",
                  storage="s3", operation="ingest") == count_before + 1


@pytest.mark.parametrize("status,outcome", [(503, "server_error"), (500, "server_error"),
                                            (422, "client_error"), (409, "client_error"),
                                            (404, "client_error")])
def test_http_exception_outcome(metric, status, outcome):
    lab = _labels(storage="none", outcome=outcome)  # rejected before any storage was chosen
    before = metric("filegrid_requests_total", **lab)

    async def go():
        with pytest.raises(HTTPException):
            async with metrics.observe("outage_records", "ingest"):
                raise HTTPException(status, "x")

    run(go())
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_unexpected_exception_is_a_server_error(metric):
    lab = _labels(storage="none", outcome="server_error")
    before = metric("filegrid_requests_total", **lab)

    async def go():
        with pytest.raises(RuntimeError):
            async with metrics.observe("outage_records", "ingest"):
                raise RuntimeError("boom")

    run(go())
    assert metric("filegrid_requests_total", **lab) == before + 1


@pytest.mark.parametrize("operation,status,outcome", [
    ("ingest", 202, "ok"), ("ingest", 409, "duplicate"), ("chunk", 409, "duplicate"),
    ("download", 409, "client_error"), ("ingest", 503, "server_error"),
    ("ingest", 413, "client_error"), ("download", 200, "ok"),
])
def test_response_status_to_outcome(operation, status, outcome):
    obs = metrics.Observation("outage_records", operation)
    obs.from_response(status)
    assert obs.outcome == outcome


def test_an_explicit_outcome_wins_over_the_status():
    obs = metrics.Observation("meter_readings", "ingest")
    obs.note(outcome="queued")
    obs.from_response(202)
    assert obs.outcome == "queued"


def test_unknown_category_records_nothing_and_creates_no_series():
    async def go():
        async with metrics.observe("../../etc/passwd", "ingest") as obs:
            obs.note(storage="s3")

    run(go())
    metrics.integrity_failure("bogus", "s3", "x")
    metrics.bulkhead_rejected("bogus")
    for fam in _families().values():
        for s in fam.samples:
            assert s.labels.get("category") not in ("bogus", "../../etc/passwd")


def test_unknown_storage_label_collapses_to_none(metric):
    lab = _labels(storage="none")
    before = metric("filegrid_requests_total", **lab)
    metrics.record("outage_records", "gopher", "ingest", "ok", 0.0)
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_note_outside_an_operation_is_harmless():
    metrics.note(storage="s3", outcome="ok")
    metrics.add_bytes(10)
    assert metrics.current() is None


# ===================== streams =====================

def test_counted_stream_counts_bytes_and_finishes_ok(metric):
    lab = _labels(operation="download")
    before = metric("filegrid_requests_total", **lab)

    async def go():
        obs = metrics.Observation("outage_records", "download")
        obs.note(storage="s3")
        out = [p async for p in metrics.counted(obs, _chunks(b"aaa", b"bb"))]
        assert out == [b"aaa", b"bb"] and obs.bytes == 5

    run(go())
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_counted_stream_abandoned_by_the_client_is_a_client_error(metric):
    lab = _labels(operation="download", outcome="client_error")
    before = metric("filegrid_requests_total", **lab)

    async def go():
        obs = metrics.Observation("outage_records", "download")
        obs.note(storage="s3")
        gen = metrics.counted(obs, _chunks(b"a", b"b", b"c"))
        await gen.__anext__()
        await gen.aclose()  # the HTTP client disconnected

    run(go())
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_deferred_observation_is_recorded_only_when_its_stream_ends(metric):
    lab = _labels(operation="download")
    before = metric("filegrid_requests_total", **lab)

    async def go():
        async with metrics.observe("outage_records", "download") as obs:
            obs.note(storage="s3")
            obs.defer()
            stream = metrics.counted(obs, _chunks(b"x"))
        assert metric("filegrid_requests_total", **lab) == before  # handler returned: not yet
        _ = [p async for p in stream]

    run(go())
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_an_observation_is_recorded_exactly_once(metric):
    lab = _labels()
    before = metric("filegrid_requests_total", **lab)
    obs = metrics.Observation("outage_records", "ingest")
    obs.note(storage="s3")
    obs.finish()
    obs.finish("server_error")
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_timed_source_measures_only_the_wait():
    async def go():
        src = metrics.TimedSource(_chunks(b"a", b"b", delay=0.05))
        assert [p async for p in src] == [b"a", b"b"]
        assert 0.08 < src.wait < 0.5

    run(go())


# ===================== HTTP instrumentation =====================

def test_ingest_is_recorded_with_storage_and_bytes(client, auth, metric):
    lab = _labels()
    before = metric("filegrid_requests_total", **lab)
    bytes_before = metric("filegrid_bytes_total", category="outage_records", storage="s3",
                          operation="ingest")
    assert _upload(client, auth).status_code == 202
    assert metric("filegrid_requests_total", **lab) == before + 1
    assert metric("filegrid_bytes_total", category="outage_records", storage="s3",
                  operation="ingest") == bytes_before + len(DATA)


def test_rejected_ingests_are_client_errors(client, auth, metric):
    lab = _labels(storage="none", outcome="client_error")
    before = metric("filegrid_requests_total", **lab)
    bad = client.post("/api/v1/ingest", headers=auth, files={"file": ("a", b"x")},
                      data={"category": "outage_records", "metadata": "not json"})
    assert bad.status_code == 422
    assert metric("filegrid_requests_total", **lab) == before + 1
    forbidden = client.post("/api/v1/ingest", headers=SIM, files={"file": ("a", b"x")},
                            data={"category": "outage_records"})
    assert forbidden.status_code == 403
    assert metric("filegrid_requests_total", **lab) == before + 2


def test_storage_down_is_a_server_error(client, auth, storage, metric):
    lab = _labels(storage="s3", outcome="server_error")
    before = metric("filegrid_requests_total", **lab)
    storage.down = True
    assert _upload(client, auth).status_code == 503
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_duplicate_file_is_a_duplicate(client, auth, admin, metric):
    client.put("/api/v1/admin/policies", headers=admin, json={"outage_records": {
        "storage": "s3", "encryption": False, "deduplicate": True, "priority": "high"}})
    lab = _labels(outcome="duplicate")
    before = metric("filegrid_requests_total", **lab)
    assert _upload(client, auth, b"same bytes").status_code == 202
    assert _upload(client, auth, b"same bytes").status_code == 409
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_download_counts_bytes_and_duration_of_the_whole_stream(client, auth, metric):
    fid = _upload(client, auth).json()["file_id"]
    lab = _labels(operation="download")
    before = metric("filegrid_requests_total", **lab)
    bytes_before = metric("filegrid_bytes_total", category="outage_records", storage="s3",
                          operation="download")
    r = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert r.status_code == 200 and r.content == DATA
    assert metric("filegrid_requests_total", **lab) == before + 1
    assert metric("filegrid_bytes_total", category="outage_records", storage="s3",
                  operation="download") == bytes_before + len(DATA)


@pytest.mark.parametrize("damage,reason", [
    (lambda s, k: s.objects.__setitem__(k, s.objects[k][:-1] + b"X"), "checksum_mismatch"),
    (lambda s, k: s.objects.pop(k), "object_missing"),
])
def test_corrupt_download_counts_an_integrity_failure(client, auth, storage, metric, damage, reason):
    fid = _upload(client, auth).json()["file_id"]
    damage(storage, next(iter(storage.objects)))
    failures = metric("filegrid_integrity_failures_total", category="outage_records",
                      storage="s3", reason=reason)
    lab = _labels(operation="download", outcome="integrity_error")
    before = metric("filegrid_requests_total", **lab)
    assert client.get(f"/api/v1/files/{fid}/download", headers=auth).status_code == 409
    assert metric("filegrid_integrity_failures_total", category="outage_records",
                  storage="s3", reason=reason) == failures + 1
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_verify_is_recorded_for_good_and_bad_files(client, auth, storage, metric):
    fid = _upload(client, auth).json()["file_id"]
    ok_lab = _labels(operation="verify")
    ok_before = metric("filegrid_requests_total", **ok_lab)
    assert client.get(f"/api/v1/files/{fid}/verify", headers=auth).json()["verified"]
    assert metric("filegrid_requests_total", **ok_lab) == ok_before + 1

    storage.objects[next(iter(storage.objects))] = b"tampered"
    bad_lab = _labels(operation="verify", outcome="integrity_error")
    bad_before = metric("filegrid_requests_total", **bad_lab)
    failures = metric("filegrid_integrity_failures_total", category="outage_records",
                      storage="s3", reason="checksum_mismatch")
    assert not client.get(f"/api/v1/files/{fid}/verify", headers=auth).json()["verified"]
    assert metric("filegrid_requests_total", **bad_lab) == bad_before + 1
    assert metric("filegrid_integrity_failures_total", category="outage_records",
                  storage="s3", reason="checksum_mismatch") == failures + 1


def test_download_of_an_unknown_file_is_not_recorded(client, auth):
    snapshot = _request_samples()
    r = client.get(f"/api/v1/files/{uuid.uuid4()}/download", headers=auth)
    assert r.status_code == 404
    assert _request_samples() == snapshot


# ----- meter readings -----

def test_meter_reading_outcomes_ok_duplicate_queued(client, pg_storage, metric):
    ok = _labels("meter_readings", "postgres", "ingest", "ok")
    dup = _labels("meter_readings", "postgres", "ingest", "duplicate")
    queued = _labels("meter_readings", "postgres", "ingest", "queued")
    b_ok, b_dup, b_q = (metric("filegrid_requests_total", **x) for x in (ok, dup, queued))

    raw = _reading(meter="MTR-H-OUT", ts="2026-11-15T11:00:00.000Z")
    assert _ingest_reading(client, raw).status_code == 202
    assert _ingest_reading(client, raw).status_code == 409
    assert metric("filegrid_requests_total", **ok) == b_ok + 1
    assert metric("filegrid_requests_total", **dup) == b_dup + 1

    pg_storage.down = True  # the reading is kept in the outbox and acknowledged
    queued_raw = _reading(meter="MTR-H-Q", ts="2026-11-15T11:00:01.000Z")
    r = _ingest_reading(client, queued_raw)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert metric("filegrid_requests_total", **queued) == b_q + 1


def test_a_duplicate_that_arrives_while_the_original_is_in_flight_is_not_a_server_error(
        client, repo, metric):
    """The 503 'still storing this exact reading, retry' is a duplicate delivery; counting it as
    a server error would trip the error-rate alert during any duplicate burst."""
    ts = "2026-11-15T14:00:00.000Z"
    repo.seed_pending_claim("MTR-H-IF", ts, age_seconds=1)  # another request is mid-write
    dup = _labels("meter_readings", "postgres", "ingest", "duplicate")
    err = _labels("meter_readings", "postgres", "ingest", "server_error")
    d_before, e_before = metric("filegrid_requests_total", **dup), metric("filegrid_requests_total", **err)
    r = _ingest_reading(client, _reading(meter="MTR-H-IF", ts=ts))
    assert r.status_code == 503 and r.headers["Retry-After"] == "1"  # the sender still retries
    assert metric("filegrid_requests_total", **dup) == d_before + 1
    assert metric("filegrid_requests_total", **err) == e_before


def test_meter_bytes_are_counted(client, metric):
    before = metric("filegrid_bytes_total", category="meter_readings", storage="postgres",
                    operation="ingest")
    raw = _reading(meter="MTR-H-B", ts="2026-11-15T11:30:00.000Z")
    assert _ingest_reading(client, raw).status_code == 202
    assert metric("filegrid_bytes_total", category="meter_readings", storage="postgres",
                  operation="ingest") == before + len(raw)


def test_outbox_drain_is_recorded(client, pg_storage, metric):
    from meter_outbox import drain_once

    pg_storage.down = True
    raw = _reading(meter="MTR-H-D", ts="2026-11-15T12:00:00.000Z")
    assert _ingest_reading(client, raw).json()["status"] == "queued"
    pg_storage.down = False
    lab = _labels("meter_readings", "postgres", "drain", "ok")
    before = metric("filegrid_requests_total", **lab)
    assert run(drain_once(client.app.state.deps, retry_after=0)) == 1
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_a_drain_that_fails_is_recorded_as_a_server_error(client, pg_storage, metric):
    from meter_outbox import drain_once

    pg_storage.down = True
    assert _ingest_reading(client, _reading(meter="MTR-H-DF", ts="2026-11-15T12:10:00.000Z")
                           ).json()["status"] == "queued"
    lab = _labels("meter_readings", "postgres", "drain", "server_error")
    before = metric("filegrid_requests_total", **lab)
    assert run(drain_once(client.app.state.deps, retry_after=0)) == 0  # storage still down
    assert metric("filegrid_requests_total", **lab) == before + 1


def test_a_drained_readings_storage_span_carries_the_original_trace_id(
        breaker_client, pg_storage):
    from meter_outbox import drain_once

    pg_storage.down = True
    trace = str(uuid.uuid4())
    r = breaker_client.post(
        "/api/v1/ingest", headers={**SIM, "X-Trace-Id": trace},
        files={"file": ("r.json", _reading(meter="MTR-H-T", ts="2026-11-15T12:30:00.000Z"))},
        data={"category": "meter_readings"})
    assert r.json()["trace_id"] == trace and r.json()["status"] == "queued"
    pg_storage.down = False
    deps = breaker_client.app.state.deps
    for st in deps.storages.values():  # the breaker opened on the failed write: let it retry
        st._opened_at = None
    with capture_json() as lines:
        assert run(drain_once(deps, retry_after=0)) == 1
    spans = [line for _, line in lines if line["msg"] == "storage op"]
    assert spans, "the drain's storage write was not logged"
    assert all(line["trace_id"] == trace for line in spans)


# ----- chunked uploads -----

def _chunk(client, auth, upload_id, index, data, total):
    return client.post(
        "/api/v1/upload/chunk", headers=auth, files={"file_chunk": ("blob", data)},
        data={"upload_id": upload_id, "chunk_index": str(index), "total_chunks": str(total),
              "filename": "video.mp4", "category": "drone_videos", "metadata": "{}",
              "chunk_sha256": hashlib.sha256(data).hexdigest()})


def test_chunks_and_the_assembled_file_are_recorded(client, auth, metric):
    upload = str(uuid.uuid4())
    pieces = [b"A" * 2000, b"B" * 1500]
    chunk_lab = _labels("drone_videos", "s3", "chunk", "ok")
    ingest_lab = _labels("drone_videos", "s3", "ingest", "ok")
    c_before = metric("filegrid_requests_total", **chunk_lab)
    i_before = metric("filegrid_requests_total", **ingest_lab)
    cb_before = metric("filegrid_bytes_total", category="drone_videos", storage="s3",
                       operation="chunk")
    ib_before = metric("filegrid_bytes_total", category="drone_videos", storage="s3",
                       operation="ingest")
    for i, p in enumerate(pieces):
        assert _chunk(client, auth, upload, i, p, 2).status_code == 200
    assert metric("filegrid_requests_total", **chunk_lab) == c_before + 2
    assert metric("filegrid_bytes_total", category="drone_videos", storage="s3",
                  operation="chunk") == cb_before + 3500
    # the finished file is one ingest, on the storage the policy chose
    assert metric("filegrid_requests_total", **ingest_lab) == i_before + 1
    assert metric("filegrid_bytes_total", category="drone_videos", storage="s3",
                  operation="ingest") == ib_before + 3500


def test_a_bad_chunk_checksum_is_an_integrity_failure(client, auth, metric):
    before = metric("filegrid_integrity_failures_total", category="drone_videos",
                    storage="s3", reason="chunk_checksum_mismatch")
    r = client.post(
        "/api/v1/upload/chunk", headers=auth, files={"file_chunk": ("blob", b"abc")},
        data={"upload_id": str(uuid.uuid4()), "chunk_index": "0", "total_chunks": "2",
              "filename": "v.mp4", "category": "drone_videos", "metadata": "{}",
              "chunk_sha256": "0" * 64})
    assert r.status_code == 422
    assert metric("filegrid_integrity_failures_total", category="drone_videos",
                  storage="s3", reason="chunk_checksum_mismatch") == before + 1


def test_a_file_that_does_not_match_the_clients_checksum_is_an_integrity_failure(
        client, auth, metric):
    upload = str(uuid.uuid4())
    before = metric("filegrid_integrity_failures_total", category="drone_videos",
                    storage="s3", reason="file_checksum_mismatch")
    for i, p in enumerate((b"AAAA", b"BBBB")):
        r = client.post(
            "/api/v1/upload/chunk", headers=auth, files={"file_chunk": ("blob", p)},
            data={"upload_id": upload, "chunk_index": str(i), "total_chunks": "2",
                  "filename": "v.mp4", "category": "drone_videos", "metadata": "{}",
                  "sha256": "f" * 64, "chunk_sha256": hashlib.sha256(p).hexdigest()})
    assert r.status_code == 422
    assert metric("filegrid_integrity_failures_total", category="drone_videos",
                  storage="s3", reason="file_checksum_mismatch") == before + 1


# ===================== bulkhead =====================

def test_bulkhead_rejection_is_counted():
    before = metrics.REGISTRY.get_sample_value(
        "filegrid_bulkhead_rejections_total", {"category": "drone_videos"}) or 0.0

    async def go():
        bh = Bulkheads({"low": 1}, wait_seconds=0.05)
        async with bh.slot("drone_videos", "low"):
            with pytest.raises(HTTPException) as err:
                async with bh.slot("drone_videos", "low"):
                    pass
            assert err.value.status_code == 503

    run(go())
    after = metrics.REGISTRY.get_sample_value(
        "filegrid_bulkhead_rejections_total", {"category": "drone_videos"})
    assert after == before + 1


# ===================== storage-call timings (BreakerStorage) =====================

def _op(storage, op, outcome):
    return dict(storage=storage, op=op, outcome=outcome)


_COUNT = "filegrid_storage_operation_duration_seconds_count"
_SUM = "filegrid_storage_operation_duration_seconds_sum"


def test_storage_put_and_get_are_timed(metric):
    wrapped = BreakerStorage(FakeStorage("s3"))
    put_before = metric(_COUNT, **_op("s3", "put", "ok"))
    get_before = metric(_COUNT, **_op("s3", "get", "ok"))

    async def go():
        await wrapped.put_stream("k", _chunks(b"hello"))
        return [p async for p in wrapped.get_stream("k")]

    assert run(go()) == [b"hello"]
    assert metric(_COUNT, **_op("s3", "put", "ok")) == put_before + 1
    assert metric(_COUNT, **_op("s3", "get", "ok")) == get_before + 1


def test_delete_is_timed(metric):
    wrapped = BreakerStorage(FakeStorage("s3"))
    before = metric(_COUNT, **_op("s3", "delete", "ok"))
    run(wrapped.delete("k"))
    assert metric(_COUNT, **_op("s3", "delete", "ok")) == before + 1


def test_a_missing_object_is_a_healthy_answer_not_an_error(metric):
    wrapped = BreakerStorage(FakeStorage("ftp"))
    ok_before = metric(_COUNT, **_op("ftp", "get", "ok"))
    err_before = metric(_COUNT, **_op("ftp", "get", "error"))

    async def go():
        with pytest.raises(ObjectNotFound):
            async for _ in wrapped.get_stream("nope"):
                pass

    run(go())
    assert metric(_COUNT, **_op("ftp", "get", "ok")) == ok_before + 1
    assert metric(_COUNT, **_op("ftp", "get", "error")) == err_before


def test_storage_failures_and_open_circuit_are_errors(metric):
    inner = FakeStorage("postgres")
    inner.down = True
    wrapped = BreakerStorage(inner, threshold=1, cooldown=60)
    before = metric(_COUNT, **_op("postgres", "put", "error"))

    async def go():
        for _ in range(3):  # 1st: the adapter fails and opens the circuit; 2nd, 3rd: fast-fail
            with pytest.raises(StorageError):
                await wrapped.put_stream("k", _chunks(b"x"))

    run(go())
    assert metric(_COUNT, **_op("postgres", "put", "error")) == before + 3


def test_put_latency_excludes_the_clients_upload_pace():
    """A slow client must not look like a slow storage."""
    wrapped = BreakerStorage(FakeStorage("s3"))
    before = metrics.REGISTRY.get_sample_value(_SUM, _op("s3", "put", "ok")) or 0.0
    start = time.perf_counter()
    run(wrapped.put_stream("slow", _chunks(b"a", b"b", b"c", delay=0.1)))
    wall = time.perf_counter() - start
    storage_time = metrics.REGISTRY.get_sample_value(_SUM, _op("s3", "put", "ok")) - before
    assert wall > 0.25
    assert storage_time < 0.1


def test_get_latency_excludes_the_consumers_pace():
    """A slow download client must not look like a slow storage."""
    inner = FakeStorage("s3")
    inner.objects["k"] = b"x" * 5000  # five 1000-byte pieces
    wrapped = BreakerStorage(inner)
    before = metrics.REGISTRY.get_sample_value(_SUM, _op("s3", "get", "ok")) or 0.0

    async def go():
        async for _ in wrapped.get_stream("k"):
            await asyncio.sleep(0.05)  # the client reads slowly

    start = time.perf_counter()
    run(go())
    wall = time.perf_counter() - start
    storage_time = metrics.REGISTRY.get_sample_value(_SUM, _op("s3", "get", "ok")) - before
    assert wall > 0.2
    assert storage_time < 0.1


def test_breaker_behaviour_is_unchanged_by_the_timing():
    inner = FakeStorage("s3")
    wrapped = BreakerStorage(inner, threshold=2, cooldown=60)
    inner.down = True

    async def go():
        for _ in range(2):
            with pytest.raises(StorageError):
                await wrapped.put_stream("k", _chunks(b"x"))
        assert wrapped.state == "open"
        inner.down = False
        with pytest.raises(StorageError):  # still open: fails fast without touching the storage
            await wrapped.put_stream("k", _chunks(b"x"))
        assert inner.objects == {}

    run(go())


def test_reading_a_stream_to_the_end_after_an_early_exit_does_not_leak(metric):
    """A client that stops downloading must not leave the breaker thinking a trial is running."""
    inner = FakeStorage("s3")
    inner.objects["k"] = b"x" * 5000
    wrapped = BreakerStorage(inner, threshold=1, cooldown=0)

    async def go():
        gen = wrapped.get_stream("k")
        await gen.__anext__()
        await gen.aclose()
        assert [p async for p in wrapped.get_stream("k")]  # still usable

    run(go())


# ===================== logs: storage spans carry the trace id =====================

def test_storage_span_log_line_has_the_trace_id(breaker_client, auth):
    trace = str(uuid.uuid4())
    with capture_json() as lines:
        r = breaker_client.post("/api/v1/ingest", headers={**auth, "X-Trace-Id": trace},
                                files={"file": ("a.pdf", DATA)},
                                data={"category": "outage_records"})
    assert r.status_code == 202 and r.headers["X-Trace-Id"] == trace
    spans = [line for _, line in lines if line["msg"] == "storage op"]
    assert spans, "no storage span was logged"
    span = spans[-1]
    assert span["trace_id"] == trace
    assert (span["storage"], span["op"], span["outcome"]) == ("s3", "put", "ok")
    assert span["category"] == "outage_records" and span["operation"] == "ingest"
    assert span["duration_ms"] >= 0


def test_one_trace_id_links_request_storage_span_and_download(breaker_client, auth):
    """The whole story of one operation can be found with a single trace id."""
    trace = str(uuid.uuid4())
    fid = _upload(breaker_client, auth).json()["file_id"]
    with capture_json(logger_name="filegrid") as lines:
        r = breaker_client.get(f"/api/v1/files/{fid}/download", headers={**auth, "X-Trace-Id": trace})
    assert r.status_code == 200
    mine = [line for _, line in lines if line.get("trace_id") == trace]
    loggers = {line["logger"] for line in mine}
    assert {"filegrid.http", "filegrid.metrics"} <= loggers
    assert any(line["msg"] == "storage op" and line["op"] == "get" for line in mine)


def test_fast_meter_reading_spans_are_not_logged_at_info():
    wrapped = BreakerStorage(FakeStorage("postgres"))

    async def go():
        async with metrics.observe("meter_readings", "ingest"):
            await wrapped.put_stream("k", _chunks(b"{}"))

    with capture_json(level=logging.INFO) as lines:
        run(go())
    assert not [line for _, line in lines if line["msg"] == "storage op"]


def test_a_failed_meter_span_is_logged_as_a_warning():
    inner = FakeStorage("postgres")
    inner.down = True
    wrapped = BreakerStorage(inner)

    async def go():
        async with metrics.observe("meter_readings", "ingest"):
            with pytest.raises(StorageError):
                await wrapped.put_stream("k", _chunks(b"{}"))

    with capture_json(level=logging.INFO) as lines:
        run(go())
    spans = [(lvl, line) for lvl, line in lines if line["msg"] == "storage op"]
    assert spans and spans[-1][0] == logging.WARNING


def test_the_trace_id_travels_from_the_request_header_to_the_response(client, auth):
    trace = str(uuid.uuid4())
    r = client.post("/api/v1/ingest", headers={**auth, "X-Trace-Id": trace},
                    files={"file": ("a.pdf", DATA)}, data={"category": "outage_records"})
    assert r.headers["X-Trace-Id"] == trace and r.json()["trace_id"] == trace


# ===================== state gauges (sampler) =====================

def _deps(storage, pg_storage, ftp_storage, repo, policies):
    return Deps(SETTINGS, repo, {"s3": storage, "postgres": pg_storage, "ftp": ftp_storage}, policies)


def test_sampler_reports_open_breakers(storage, pg_storage, ftp_storage, repo, policies):
    pg = BreakerStorage(pg_storage, threshold=1, cooldown=60)
    deps = Deps(SETTINGS, repo, {"s3": BreakerStorage(storage), "postgres": pg,
                                 "ftp": BreakerStorage(ftp_storage)}, policies)
    pg_storage.down = True

    async def go():
        with pytest.raises(StorageError):
            await pg.put_stream("k", _chunks(b"x"))
        await metrics.sample_once(deps)

    run(go())
    assert metrics.REGISTRY.get_sample_value("filegrid_breaker_open", {"storage": "postgres"}) == 1
    assert metrics.REGISTRY.get_sample_value("filegrid_breaker_open", {"storage": "s3"}) == 0


def test_sampler_reports_the_outbox_backlog(client, pg_storage, repo, storage, ftp_storage, policies):
    pg_storage.down = True
    for i in range(3):
        _ingest_reading(client, _reading(meter=f"MTR-H-OB{i}", ts="2026-11-15T13:00:00.000Z"))
    run(metrics.sample_once(_deps(storage, pg_storage, ftp_storage, repo, policies)))
    assert metrics.REGISTRY.get_sample_value("filegrid_outbox_queued") == run(repo.count_queued())
    assert metrics.REGISTRY.get_sample_value("filegrid_outbox_queued") == 3


def test_sampler_reports_unencrypted_personal_data(storage, pg_storage, ftp_storage, repo, policies):
    from repo import FileRecord

    deps = _deps(storage, pg_storage, ftp_storage, repo, policies)
    run(metrics.sample_once(deps))
    assert metrics.REGISTRY.get_sample_value("filegrid_pii_unencrypted_files") == 0

    for i, enc in enumerate((True, False, False)):
        run(repo.insert_file(FileRecord(
            id=str(uuid.uuid4()), filename=f"doc{i}.pdf", category="connection_docs",
            storage="ftp", storage_key=f"k{i}", size_bytes=1, sha256="0" * 64,
            encrypted=enc, trace_id="t")))
    run(repo.insert_file(FileRecord(  # unencrypted but not personal data: not counted
        id=str(uuid.uuid4()), filename="x.pdf", category="outage_records", storage="s3",
        storage_key="x", size_bytes=1, sha256="0" * 64, encrypted=False, trace_id="t")))
    run(metrics.sample_once(deps))
    assert metrics.REGISTRY.get_sample_value("filegrid_pii_unencrypted_files") == 2


def test_sampler_reports_stalled_uploads(storage, pg_storage, ftp_storage, repo, policies):
    deps = _deps(storage, pg_storage, ftp_storage, repo, policies)
    repo.stall_stats = (4, 1)
    run(metrics.sample_once(deps))
    assert metrics.REGISTRY.get_sample_value("filegrid_uploads_in_progress") == 4
    assert metrics.REGISTRY.get_sample_value("filegrid_uploads_stalled") == 1
    repo.stall_stats = (0, 0)
    run(metrics.sample_once(deps))
    assert metrics.REGISTRY.get_sample_value("filegrid_uploads_stalled") == 0


def test_the_sampler_survives_a_failing_dependency(storage, pg_storage, ftp_storage, repo, policies):
    deps = _deps(storage, pg_storage, ftp_storage, repo, policies)

    async def boom(*_):
        raise RuntimeError("db down")

    repo.count_unencrypted_pii = boom
    repo.upload_stall_stats = boom
    run(metrics.sample_once(deps))  # must not raise; the other gauges are still refreshed


def test_run_sampler_stops_when_asked(storage, pg_storage, ftp_storage, repo, policies):
    deps = _deps(storage, pg_storage, ftp_storage, repo, policies)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(metrics.run_sampler(deps, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)

    run(go())


# ===================== /metrics =====================

def test_metrics_endpoint_serves_prometheus_text(client, auth):
    _upload(client, auth)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    for name in ("filegrid_requests_total", "filegrid_request_duration_seconds_bucket",
                 "filegrid_bytes_total", "filegrid_outbox_queued", "filegrid_breaker_open"):
        assert name in r.text, name


def test_metrics_is_internal_only(client):
    assert client.get("/api/v1/metrics").status_code == 404  # not a public API path
    nginx_conf = os.path.join(BACKEND_DIR, "..", "infrastructure", "nginx", "default.conf")
    if os.path.exists(nginx_conf):  # absent when only backend/ is mounted (CLAUDE.md test command)
        assert "location = /metrics" in open(nginx_conf, encoding="utf-8").read()


def test_metrics_reveal_no_personal_data(client, admin):
    client.put("/api/v1/admin/policies", headers=admin, json={"connection_docs": {
        "storage": "ftp", "encryption": False, "deduplicate": False, "priority": "normal"}})
    _upload(client, admin, name="cerere_Ion_Popescu_CNP1800101.pdf", category="connection_docs")
    body = client.get("/metrics").text
    assert "Popescu" not in body and "CNP" not in body and "cerere_" not in body


# ===================== multiprocess mode =====================

def _py(code, env_dir):
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": env_dir, "PYTHONPATH": BACKEND_DIR}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=60)


def test_multiprocess_mode_aggregates_all_workers(tmp_path):
    """Two worker processes record; the process serving /metrics sees the sum."""
    d = str(tmp_path)
    worker = (
        "import metrics; "
        "[metrics.record('outage_records','s3','ingest','ok',0.01,100) for _ in range({n})]; "
        "metrics.OUTBOX.set({g})"
    )
    for n, g in ((3, 5), (4, 7)):
        r = _py(worker.format(n=n, g=g), d)
        assert r.returncode == 0, r.stderr
    r = _py("import metrics; body, _ = metrics.render(); print(body.decode())", d)
    assert r.returncode == 0, r.stderr
    from prometheus_client.parser import text_string_to_metric_families

    total = bytes_total = 0.0
    for fam in text_string_to_metric_families(r.stdout):
        for s in fam.samples:
            if s.name == "filegrid_requests_total" and s.labels.get("outcome") == "ok" \
                    and s.labels.get("category") == "outage_records":
                total += s.value
            if s.name == "filegrid_bytes_total" and s.labels.get("category") == "outage_records":
                bytes_total += s.value
    assert total == 7
    assert bytes_total == 700


@pytest.mark.skipif(os.name != "posix", reason="process liveness is only probed on POSIX")
def test_gauges_of_dead_workers_are_dropped_but_counters_are_kept(tmp_path):
    d = str(tmp_path)
    r = _py("import metrics; metrics.record('outage_records','s3','ingest','ok',0.01); "
            "metrics.OUTBOX.set(9)", d)
    assert r.returncode == 0, r.stderr
    r = _py("import metrics; print(metrics.reap_dead_workers()); "
            "body = metrics.render()[0].decode(); "
            "print('outbox=' + str('filegrid_outbox_queued 9' in body)); "
            "print('counter=' + str('filegrid_requests_total' in body))", d)
    assert r.returncode == 0, r.stderr
    out = r.stdout.split()
    assert int(out[0]) >= 1
    assert "outbox=False" in out and "counter=True" in out


def test_without_the_env_var_reaping_is_a_no_op():
    assert metrics.MULTIPROC_DIR is None
    assert metrics.reap_dead_workers() == 0
