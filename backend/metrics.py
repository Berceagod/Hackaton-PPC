"""Prometheus metrics for FileGrid (slice H).

Names, types, labels and histogram buckets are the contract in CLAUDE.md section 4:
slice I reads them through the Prometheus HTTP API, so they must not drift.

The backend runs several Uvicorn workers per container. A scrape hits ONE worker, so
plain in-process counters would jump around between scrapes. prometheus_client's
*multiprocess mode* (PROMETHEUS_MULTIPROC_DIR, set by docker-compose) makes every worker
write into shared memory-mapped files and `/metrics` aggregate all of them. Without that
variable (unit tests, local runs) the ordinary single-process registry is used.

Known limitation: prometheus_client does not support exemplars in multiprocess mode, so a
trace id cannot ride on a histogram sample. Traceability is done with the trace id in
every structured log line instead (see ADR-0015).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from contextvars import ContextVar

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from policies import CATEGORIES

log = logging.getLogger("filegrid.metrics")

MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR") or os.environ.get(
    "prometheus_multiproc_dir"
)
if MULTIPROC_DIR:
    os.makedirs(MULTIPROC_DIR, exist_ok=True)

# Fixed by the contract (seconds).
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

STORAGES = ("postgres", "s3", "ftp")
OPERATIONS = ("ingest", "chunk", "download", "verify", "drain")
OUTCOMES = ("ok", "duplicate", "queued", "client_error", "server_error", "integrity_error")

# ---------------------------------------------------------------------------
# The contract metrics
# ---------------------------------------------------------------------------
REQUESTS = Counter(
    "filegrid_requests_total",
    "File operations handled, by category, storage, operation and outcome",
    ["category", "storage", "operation", "outcome"],
)
DURATION = Histogram(
    "filegrid_request_duration_seconds",
    "Time to complete a file operation (downloads: until the last byte is sent)",
    ["category", "storage", "operation"],
    buckets=BUCKETS,
)
BYTES = Counter(
    "filegrid_bytes_total",
    "Plaintext bytes handled by file operations",
    ["category", "storage", "operation"],
)
STORAGE_OP = Histogram(
    "filegrid_storage_operation_duration_seconds",
    "Time spent inside a storage adapter, excluding the client's upload pace and "
    "the caller's processing (hashing, encryption)",
    ["storage", "op", "outcome"],
    buckets=BUCKETS,
)
INTEGRITY = Counter(
    "filegrid_integrity_failures_total",
    "Files or chunks whose SHA-256/authentication check failed",
    ["category", "storage", "reason"],
)
BULKHEAD_REJECTIONS = Counter(
    "filegrid_bulkhead_rejections_total",
    "Requests refused with 503 because their category was at capacity",
    ["category"],
)
OUTBOX = Gauge(
    "filegrid_outbox_queued",
    "Meter readings accepted (202) but not yet written to their storage",
    multiprocess_mode="livemax",
)
BREAKER_OPEN = Gauge(
    "filegrid_breaker_open",
    "1 while the circuit breaker of a storage is open or half-open",
    ["storage"],
    multiprocess_mode="livemax",
)

# ---------------------------------------------------------------------------
# Additional metrics (slice H) that feed the business-guarantee alerts
# ---------------------------------------------------------------------------
PII_UNENCRYPTED = Gauge(
    "filegrid_pii_unencrypted_files",
    "connection_docs files stored without encryption (personal data at risk)",
    multiprocess_mode="livemax",
)
UPLOADS_IN_PROGRESS = Gauge(
    "filegrid_uploads_in_progress",
    "Chunked uploads that are open or being assembled",
    multiprocess_mode="livemax",
)
UPLOADS_STALLED = Gauge(
    "filegrid_uploads_stalled",
    "Chunked uploads with no chunk received for longer than the stall threshold",
    multiprocess_mode="livemax",
)

_GAUGE_FILE = re.compile(r"^gauge_live(?:sum|max|min|all|mostrecent)_(\d+)\.db$")

INTEGRITY_REASONS = (
    # damaged or missing INSIDE a storage (these page the team)
    "checksum_mismatch", "object_missing", "decryption_failed", "staging_corrupted",
    # damaged in transit between the client and us (the client resends; no page)
    "chunk_checksum_mismatch", "file_checksum_mismatch",
)


def _start_alert_series_at_zero() -> None:
    """Create the series that alerts depend on, at 0, when the process starts.

    Prometheus' increase() and rate() need two samples. A counter that first appears
    already at 1 (the first-ever corrupt file) has one sample, so its alert would see an
    increase of 0 and never fire. Starting these series at 0 makes the first event count.
    """
    for category in CATEGORIES:
        BULKHEAD_REJECTIONS.labels(category)
        for storage in STORAGES:
            for reason in INTEGRITY_REASONS:
                INTEGRITY.labels(category, storage, reason)
    for storage in STORAGES:
        for outcome in ("ok", "duplicate", "queued", "client_error", "server_error"):
            REQUESTS.labels("meter_readings", storage, "ingest", outcome)


_start_alert_series_at_zero()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def _storage_label(storage: str | None) -> str:
    return storage if storage in STORAGES else "none"


def record(
    category: str,
    storage: str | None,
    operation: str,
    outcome: str,
    seconds: float,
    nbytes: int = 0,
) -> None:
    """Record one finished file operation. Unknown categories are ignored so a client can
    never create new label values (and with them unbounded series)."""
    if category not in CATEGORIES:
        return
    storage = _storage_label(storage)
    REQUESTS.labels(category, storage, operation, outcome).inc()
    DURATION.labels(category, storage, operation).observe(max(seconds, 0.0))
    if nbytes > 0:
        BYTES.labels(category, storage, operation).inc(nbytes)


def integrity_failure(category: str, storage: str | None, reason: str) -> None:
    if category in CATEGORIES:
        INTEGRITY.labels(category, _storage_label(storage), reason).inc()


def bulkhead_rejected(category: str) -> None:
    if category in CATEGORIES:
        BULKHEAD_REJECTIONS.labels(category).inc()


def storage_op(storage: str, op: str, outcome: str, seconds: float) -> None:
    """One call into a storage adapter; also logs a span line carrying the trace id."""
    if storage in STORAGES:
        STORAGE_OP.labels(storage, op, outcome).observe(max(seconds, 0.0))
    obs = _current.get()
    category = obs.category if obs else None
    # A reading is a few KB at 10-100 per second: log its storage span only when it is
    # slow or failed, so Loki is not flooded. Every other category logs every call.
    quiet = category == "meter_readings" and outcome == "ok" and seconds < 1.0
    level = logging.DEBUG if quiet else (logging.WARNING if outcome == "error" else logging.INFO)
    log.log(
        level,
        "storage op",
        extra={
            "storage": storage,
            "op": op,
            "outcome": outcome,
            "duration_ms": round(seconds * 1000, 2),
            "category": category,
            "operation": obs.operation if obs else None,
        },
    )


def _outcome_for_status(status: int, operation: str) -> str:
    if status >= 500:
        return "server_error"
    if status == 409 and operation in ("ingest", "chunk"):
        return "duplicate"
    if status >= 400:
        return "client_error"
    return "ok"


class Observation:
    """One in-flight file operation. `finish` records it exactly once."""

    def __init__(self, category: str, operation: str) -> None:
        self.category = category
        self.operation = operation
        self.storage: str = "none"
        self.outcome: str | None = None
        self.bytes = 0
        self.deferred = False
        self._done = False
        self._t0 = time.perf_counter()

    def note(self, *, storage: str | None = None, outcome: str | None = None) -> None:
        if storage is not None:
            self.storage = storage
        if outcome is not None:
            self.outcome = outcome

    def add_bytes(self, n: int) -> None:
        self.bytes += max(int(n), 0)

    def defer(self) -> None:
        """The operation ends later (when a response stream finishes), not when the
        handler returns."""
        self.deferred = True

    def from_response(self, status: int) -> None:
        if self.outcome is None:
            self.outcome = _outcome_for_status(status, self.operation)

    def finish(self, outcome: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        record(
            self.category,
            self.storage,
            self.operation,
            outcome or self.outcome or "ok",
            time.perf_counter() - self._t0,
            self.bytes,
        )


_current: ContextVar[Observation | None] = ContextVar("filegrid_observation", default=None)


def current() -> Observation | None:
    return _current.get()


def note(*, storage: str | None = None, outcome: str | None = None) -> None:
    """Annotate the operation being handled; a no-op outside one (tests, background work)."""
    obs = _current.get()
    if obs is not None:
        obs.note(storage=storage, outcome=outcome)


def add_bytes(n: int) -> None:
    obs = _current.get()
    if obs is not None:
        obs.add_bytes(n)


@contextlib.asynccontextmanager
async def observe(category: str, operation: str):
    """Wrap a request handler. Records the operation when the block ends, with the
    outcome taken from the exception, from `obs.from_response(status)`, or "ok"."""
    obs = Observation(category, operation)
    token = _current.set(obs)
    try:
        yield obs
    except StarletteHTTPException as exc:
        obs.finish(obs.outcome or ("server_error" if exc.status_code >= 500 else "client_error"))
        raise
    except asyncio.CancelledError:
        obs.finish("client_error")  # the caller went away
        raise
    except Exception:
        obs.finish("server_error")
        raise
    else:
        if not obs.deferred:
            obs.finish()
    finally:
        _current.reset(token)


async def counted(obs: Observation, source):
    """Pass a byte stream through, counting its bytes, and finish `obs` when it ends:
    ok if fully read, client_error if the client stopped early, server_error on failure."""
    outcome = "ok"
    # While the body streams, the handler's own context is long gone: make this operation
    # the current one again so the storage spans of the delivery carry its category.
    token = _current.set(obs)
    try:
        async for piece in source:
            obs.add_bytes(len(piece))
            yield piece
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "client_error"
        raise
    except BaseException:
        outcome = "server_error"
        raise
    finally:
        obs.finish(obs.outcome or outcome)
        try:
            _current.reset(token)
        except ValueError:  # finalised from another context (garbage collection)
            pass


class TimedSource:
    """Iterate `source` while accumulating the time spent waiting for it. A storage's
    `put_stream` pulls its input from the client's upload (and the hashing/encryption
    on the way); that wait is not storage latency and is subtracted from it."""

    def __init__(self, source) -> None:
        self._it = source.__aiter__()
        self.wait = 0.0

    def __aiter__(self) -> "TimedSource":
        return self

    async def __anext__(self):
        t0 = time.perf_counter()
        try:
            return await self._it.__anext__()
        finally:
            self.wait += time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if os.name != "posix":
        return True  # os.kill(pid, 0) would terminate the process on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def reap_dead_workers() -> int:
    """Gauges of a worker that died (crash, container restart) must stop being reported.
    Counters and histograms keep their history on purpose."""
    if not MULTIPROC_DIR:
        return 0
    removed = 0
    try:
        names = os.listdir(MULTIPROC_DIR)
    except OSError:
        return 0
    for name in names:
        m = _GAUGE_FILE.match(name)
        if m and int(m.group(1)) != os.getpid() and not _pid_alive(int(m.group(1))):
            multiprocess.mark_process_dead(int(m.group(1)), MULTIPROC_DIR)
            removed += 1
    return removed


def render() -> tuple[bytes, str]:
    if MULTIPROC_DIR:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


async def metrics_endpoint() -> Response:
    """GET /metrics: internal only (nginx answers 404 for it; Prometheus reaches the
    backend directly on the Docker network)."""
    reap_dead_workers()
    body, content_type = render()
    return Response(content=body, headers={"Content-Type": content_type})


# ---------------------------------------------------------------------------
# Sampler: gauges that come from state (breakers, outbox, database), not from requests
# ---------------------------------------------------------------------------
async def sample_once(deps) -> None:
    for name, storage in deps.storages.items():
        state = getattr(storage, "state", "closed")
        BREAKER_OPEN.labels(name).set(0.0 if state == "closed" else 1.0)
    try:
        OUTBOX.set(await deps.meters().count_queued())
    except Exception:
        log.warning("outbox gauge not updated", exc_info=True)
    try:
        PII_UNENCRYPTED.set(await deps.repo.count_unencrypted_pii())
    except Exception:
        log.warning("pii gauge not updated", exc_info=True)
    try:
        in_progress, stalled = await deps.repo.upload_stall_stats(
            deps.settings.prometheus_upload_stall_seconds
        )
        UPLOADS_IN_PROGRESS.set(in_progress)
        UPLOADS_STALLED.set(stalled)
    except Exception:
        log.warning("upload gauges not updated", exc_info=True)


async def run_sampler(deps, stop: asyncio.Event) -> None:
    interval = max(float(deps.settings.prometheus_sample_seconds), 1.0)
    while not stop.is_set():
        try:
            await sample_once(deps)
            reap_dead_workers()
        except Exception:
            log.exception("metrics sampler failed")
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except (asyncio.TimeoutError, TimeoutError):
            pass
