"""Alert webhook receiver (slice H).

Alertmanager POSTs its notifications here. For each alert the receiver
  * logs one JSON line to stdout (the log collector ships it to Loki),
  * keeps the last 200 in memory so a person or a script can read them: GET /alerts.
It stands in for "the team's channel". Standard library only; internal network only.
"""

import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 9099
MAX_BODY = 1024 * 1024
_alerts: deque = deque(maxlen=200)
_lock = threading.Lock()


def _log(record: dict) -> None:
    print(json.dumps(record, ensure_ascii=False), flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "alert-webhook"

    def log_message(self, *_args) -> None:  # silence the default access log
        pass

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self._send(200, {"status": "ok"})
        if url.path == "/alerts":
            name = parse_qs(url.query).get("name", [None])[0]
            with _lock:
                items = [a for a in _alerts if name is None or a["alertname"] == name]
            return self._send(200, items)
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/alerts":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"error": "payload too large"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        received = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for alert in payload.get("alerts", []):
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            record = {
                "received_at": received,
                "status": alert.get("status"),  # firing | resolved
                "alertname": labels.get("alertname"),
                "severity": labels.get("severity"),
                "guarantee": labels.get("guarantee"),
                "category": labels.get("category"),
                "storage": labels.get("storage"),
                "summary": annotations.get("summary"),
                "starts_at": alert.get("startsAt"),
                "ends_at": alert.get("endsAt"),
            }
            with _lock:
                _alerts.append(record)
            _log({"ts": received, "service": "alert-webhook",
                  "level": "warning" if record["status"] == "firing" else "info",
                  "msg": "alert", **record})
        self._send(200, {"received": len(payload.get("alerts", []))})


if __name__ == "__main__":
    _log({"service": "alert-webhook", "level": "info", "msg": f"listening on :{PORT}"})
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
