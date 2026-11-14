"""Pure-ASGI middleware: assigns/propagates X-Trace-Id, returns it, logs each request."""

import logging
import time
import uuid

from logging_setup import trace_id_var

log = logging.getLogger("filegrid.http")


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


class TraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-trace-id", b"").decode()
        trace_id = incoming if _valid_uuid(incoming) else str(uuid.uuid4())
        token = trace_id_var.set(trace_id)
        scope.setdefault("state", {})["trace_id"] = trace_id
        started = time.perf_counter()
        status = {"code": 500}

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-trace-id", trace_id.encode())
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status["code"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            trace_id_var.reset(token)
