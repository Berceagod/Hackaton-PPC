"""Generates the Grafana dashboards as JSON (slice H): `python build_dashboards.py`.

The JSON files in ./dashboards are what Grafana loads (provisioned as code); this script
is how they are kept consistent and reviewable. Panels use the metric contract of
CLAUDE.md section 4 and the simulator's own sim_* metrics.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "dashboards")

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

REQ = "filegrid_requests_total"
DUR = "filegrid_request_duration_seconds"
BYT = "filegrid_bytes_total"
OPD = "filegrid_storage_operation_duration_seconds"


def q(expr: str) -> str:
    """`[RI]` stands for Grafana's $__rate_interval (keeps the PromQL below readable)."""
    return " ".join(expr.split()).replace("[RI]", "[$__rate_interval]")


def quantile(p: float, metric: str, by: str, selector: str = "") -> str:
    return q(f"histogram_quantile({p}, sum by (le, {by}) (rate({metric}_bucket{{{selector}}}[RI])))")


def t(expr: str, legend: str = "", ref: str = "A", ds=PROM, **extra) -> dict:
    return {"refId": ref, "datasource": ds, "expr": q(expr) if ds is PROM else expr,
            "legendFormat": legend, **extra}


def pct_targets(metric: str, by: str, selector: str, legend: str) -> list[dict]:
    return [t(quantile(p, metric, by, selector), legend.format(p=name), ref)
            for p, name, ref in ((0.5, "p50", "A"), (0.95, "p95", "B"), (0.99, "p99", "C"))]


class Board:
    def __init__(self, uid: str, title: str, description: str, tags=("filegrid",)):
        self.uid, self.title, self.description, self.tags = uid, title, description, list(tags)
        self.panels: list[dict] = []
        self.templating: list[dict] = []
        self._x = self._y = self._row_h = 0
        self._id = 0

    def _place(self, w: int, h: int) -> dict:
        if self._x + w > 24:
            self._x, self._y, self._row_h = 0, self._y + self._row_h, 0
        pos = {"h": h, "w": w, "x": self._x, "y": self._y}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def _add(self, panel: dict, w: int, h: int) -> None:
        self._id += 1
        panel.update(id=self._id, gridPos=self._place(w, h))
        self.panels.append(panel)

    def row(self, title: str) -> None:
        self._x, self._y, self._row_h = 0, self._y + self._row_h, 0
        self._id += 1
        self.panels.append({"id": self._id, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._y}, "panels": []})
        self._y += 1

    def text(self, title: str, md: str, w=24, h=3) -> None:
        self._add({"type": "text", "title": title, "options": {"mode": "markdown", "content": md}}, w, h)

    def stat(self, title: str, expr: str, w=4, h=4, unit="short", description="",
             steps=None, no_value="0") -> None:
        steps = steps or [{"color": "green", "value": None}, {"color": "red", "value": 1}]
        self._add({
            "type": "stat", "title": title, "description": description, "datasource": PROM,
            "targets": [t(expr)],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": no_value, "mappings": [],
                                         "thresholds": {"mode": "absolute", "steps": steps}},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "area", "textMode": "auto",
                        "justifyMode": "center"},
        }, w, h)

    def series(self, title: str, targets: list[dict], w=12, h=8, unit="short", description="",
               stack=False, minimum=0, maximum=None, limit_line=None) -> None:
        custom = {"lineWidth": 1, "fillOpacity": 25 if stack else 8, "showPoints": "never",
                  "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                  "spanNulls": False}
        defaults = {"unit": unit, "custom": custom, "min": minimum}
        if maximum is not None:
            defaults["max"] = maximum
        if limit_line is not None:  # a red dashed line at the threshold from the alert rule
            custom["thresholdsStyle"] = {"mode": "dashed"}
            defaults["thresholds"] = {"mode": "absolute", "steps": [
                {"color": "transparent", "value": None}, {"color": "red", "value": limit_line}]}
        self._add({
            "type": "timeseries", "title": title, "description": description, "datasource": PROM,
            "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "bottom",
                                   "calcs": ["lastNotNull", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
        }, w, h)

    def table(self, title: str, expr: str, w=24, h=7, description="", columns=None) -> None:
        self._add({
            "type": "table", "title": title, "description": description, "datasource": PROM,
            "targets": [t(expr, ref="A", instant=True, format="table")],
            "transformations": [
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, "__name__": True, "Value": True,
                                      "system": True, "job": True, "instance": True},
                    "renameByName": columns or {}}}],
            "fieldConfig": {"defaults": {"noValue": "nothing"}, "overrides": []},
            "options": {"showHeader": True},
        }, w, h)

    def logs(self, title: str, expr: str, w=24, h=9, description="") -> None:
        self._add({
            "type": "logs", "title": title, "description": description, "datasource": LOKI,
            "targets": [{"refId": "A", "datasource": LOKI, "expr": expr, "queryType": "range"}],
            "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True,
                        "sortOrder": "Ascending", "dedupStrategy": "none", "prettifyLogMessage": False},
        }, w, h)

    def build(self, default_range="now-30m") -> dict:
        return {
            "uid": self.uid, "title": self.title, "description": self.description,
            "tags": self.tags, "schemaVersion": 39, "version": 1, "editable": False,
            "timezone": "browser", "refresh": "5s", "graphTooltip": 1,
            "time": {"from": default_range, "to": "now"},
            "templating": {"list": self.templating}, "annotations": {"list": []},
            "panels": self.panels, "links": [
                {"title": "Operations", "type": "link", "url": "/grafana/d/filegrid-ops", "icon": "dashboard"},
                {"title": "Business guarantees", "type": "link", "url": "/grafana/d/filegrid-guarantees", "icon": "dashboard"},
                {"title": "Trace one operation", "type": "link", "url": "/grafana/d/filegrid-trace", "icon": "dashboard"}],
        }


# ---------------------------------------------------------------------------------------
def operations() -> Board:
    b = Board("filegrid-ops", "FileGrid — Operations",
              "Latency (p50/p95/p99), throughput and error rate per category and per storage.")
    b.stat("Backend instances up", 'count(up{job="backend"} == 1)', unit="short",
           steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
           description="Replicas Prometheus can scrape. `docker compose up -d --scale backend=N` adds more.")
    b.stat("Requests / s", f"sum(rate({REQ}[RI]))", unit="reqps",
           steps=[{"color": "blue", "value": None}])
    b.stat("Failure rate", f'sum(rate({REQ}{{outcome=~"server_error|integrity_error",operation!="drain"}}[RI])) / sum(rate({REQ}{{operation!="drain"}}[RI]))',
           unit="percentunit", steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.01},
                                      {"color": "red", "value": 0.05}],
           description="Server errors and integrity errors as a share of what clients asked for (outbox drain retries excluded: nothing is lost by them).")
    b.stat("Alerts firing", 'count(ALERTS{alertstate="firing"}) or vector(0)')
    b.stat("Meter outbox", "max(filegrid_outbox_queued) or vector(0)",
           steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 1000}],
           description="Readings accepted while their storage was down, waiting to be written out.")
    b.stat("Storages with open breaker", "sum(max by (storage) (filegrid_breaker_open)) or vector(0)")

    b.row("Per category")
    b.series("Ingest latency — p50 / p95 / p99", pct_targets(DUR, "category", 'operation="ingest"', "{{{{category}}}} {p}"),
             unit="s", description="Time to store a file and acknowledge it, by category.")
    b.series("Download latency — p50 / p95 / p99", pct_targets(DUR, "category", 'operation="download"', "{{{{category}}}} {p}"),
             unit="s", description="Integrity check plus delivery of the last byte, by category.")
    b.series("Throughput — files per second", [t(
        f'sum by (category) (rate({REQ}{{operation="ingest",outcome=~"ok|queued"}}[RI]))', "{{category}}")],
        unit="ops", description="Files accepted per second (a chunked upload counts once, when assembled).")
    b.series("Throughput — bytes per second", [
        t(f'sum by (category) (rate({BYT}{{operation="ingest"}}[RI]))', "{{category}} in", "A"),
        t(f'sum by (category) (rate({BYT}{{operation="download"}}[RI]))', "{{category}} out", "B")],
        unit="Bps")
    b.series("Error rate", [t(
        f'sum by (category) (rate({REQ}{{outcome=~"server_error|integrity_error",operation!="drain"}}[RI])) / sum by (category) (rate({REQ}{{operation!="drain"}}[RI]))',
        "{{category}}")], unit="percentunit",
        description="Server errors and integrity errors as a share of the category's operations.")
    b.series("Outcomes (stacked)", [t(f"sum by (category, outcome) (rate({REQ}[RI])) > 0", "{{category}} · {{outcome}}")],
             unit="ops", stack=True)

    b.row("Per storage")
    b.series("Storage latency, writes — p50 / p95 / p99",
             pct_targets(OPD, "storage", 'op="put",outcome="ok"', "{{{{storage}}}} {p}"), unit="s",
             description="Time inside the storage adapter only: a slow client's upload pace is excluded.")
    b.series("Storage latency, reads — p50 / p95 / p99",
             pct_targets(OPD, "storage", 'op="get",outcome="ok"', "{{{{storage}}}} {p}"), unit="s",
             description="Time waiting for the storage to produce data: a slow downloader is excluded.")
    b.series("Storage throughput — bytes per second", [t(
        f'sum by (storage) (rate({BYT}{{operation=~"ingest|download|drain"}}[RI]))', "{{storage}}")], unit="Bps")
    b.series("Storage calls per second", [t(
        f"sum by (storage, op) (rate({OPD}_count[RI]))", "{{storage}} {{op}}")], unit="ops")
    b.series("Storage error rate", [t(
        f'sum by (storage) (rate({OPD}_count{{outcome="error"}}[RI])) / sum by (storage) (rate({OPD}_count[RI]))',
        "{{storage}}")], unit="percentunit",
        description="Failed storage calls, including calls refused at once by an open circuit breaker.")
    b.series("Ingest p95 — category × storage",
             [t(quantile(0.95, DUR, "category, storage", 'operation="ingest"'), "{{category}} on {{storage}}")],
             unit="s", description="Which storage serves which category, and how fast.")

    b.row("Isolation and resilience")
    b.series("Ingest rate by category", [t(
        f'sum by (category) (rate({REQ}{{operation=~"ingest|chunk"}}[RI]))', "{{category}}")], unit="ops",
        description="A 10x spike in one category should leave the others' latency (above) unchanged.")
    b.series("Bulkhead rejections / s", [t(
        "sum by (category) (rate(filegrid_bulkhead_rejections_total[RI]))", "{{category}}")], unit="ops",
        description="Requests refused (503) because their own category was full.")
    b.series("Circuit breakers (1 = open)", [t("max by (storage) (filegrid_breaker_open)", "{{storage}}")],
             maximum=1)
    b.series("Integrity failures (last 5 min)", [t(
        "sum by (category, storage, reason) (increase(filegrid_integrity_failures_total[5m])) > 0",
        "{{category}} · {{storage}} · {{reason}}")], description="Files whose checksum no longer matched (nothing listed = none).")

    b.row("Meter simulator (its own sim_* metrics)")
    b.series("Target vs achieved rate", [
        t("sim_target_rate", "target", "A"),
        t("sum(rate(sim_requests_total[RI]))", "sent", "B")], unit="ops")
    b.series("Results", [t("sum by (result) (rate(sim_requests_total[RI]))", "{{result}}")], unit="ops", stack=True)
    b.series("Backlog and drops at the meters", [
        t("max(sim_backlog)", "waiting for FileGrid", "A"),
        t("increase(sim_dropped_total[5m])", "dropped (5 min)", "B")])
    return b


# ---------------------------------------------------------------------------------------
def guarantees() -> Board:
    b = Board("filegrid-guarantees", "FileGrid — Business guarantees",
              "The four promises of PDF section 2.1, each with the signal that shows it is at risk.")
    b.row("Meter readings — never lost, never duplicated")
    b.stat("Readings accepted / s", 'sum(rate(filegrid_requests_total{category="meter_readings",operation="ingest",outcome=~"ok|queued"}[RI]))',
           unit="ops", steps=[{"color": "blue", "value": None}])
    b.stat("Dropped at the meters (5 min)", "sum(increase(sim_dropped_total[5m])) or vector(0)",
           description="A reading a meter had to throw away: lost for good.")
    b.stat("Waiting at the meters", "max(sim_backlog) or vector(0)",
           steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}])
    b.stat("Outbox (accepted, not yet stored)", "max(filegrid_outbox_queued) or vector(0)",
           steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 1000}])
    b.stat("Duplicates rejected (5 min)", 'sum(increase(filegrid_requests_total{category="meter_readings",outcome="duplicate"}[5m])) or vector(0)',
           steps=[{"color": "blue", "value": None}],
           description="Each was answered 409 and stored nowhere: none can reach the bill.")
    b.stat("Server errors (5 min)", 'sum(increase(filegrid_requests_total{category="meter_readings",operation="ingest",outcome="server_error"}[5m])) or vector(0)',
           description="Readings a meter was refused with a server error (it retries). Failed outbox drain attempts are not counted: nothing is lost by them.")
    b.series("Accepted, duplicate and failed readings", [
        t('sum by (outcome) (rate(filegrid_requests_total{category="meter_readings",operation="ingest"}[RI])) > 0', "{{outcome}}")],
        unit="ops", stack=True, description="ok = stored; queued = accepted and kept until the storage is back; duplicate = rejected.")
    b.series("Outbox and sender backlog", [
        t("max(filegrid_outbox_queued)", "outbox (accepted, not stored)", "A"),
        t("max(sim_backlog)", "waiting at the meters", "B")])

    b.row("Outage records — available immediately")
    b.series("Ingest p95 (alert above 2 s)", [t(quantile(0.95, DUR, "category", 'category="outage_records",operation="ingest"'), "p95 save")],
             unit="s", limit_line=2, description="How long until a dispatcher can see a new outage record.")
    b.series("Download p95 (alert above 3 s)", [t(quantile(0.95, DUR, "category", 'category="outage_records",operation="download"'), "p95 open")],
             unit="s", limit_line=3, description="How long a dispatcher waits to open one.")

    b.row("Connection documents — personal data protected and auditable")
    b.stat("Documents stored UNENCRYPTED", "max(filegrid_pii_unencrypted_files) or vector(0)", w=6, h=5,
           description="Must always be 0. Anything above 0 means personal data is readable by someone with direct storage access.")
    b.series("Connection-document operations", [
        t('sum by (operation, outcome) (rate(filegrid_requests_total{category="connection_docs"}[RI]))', "{{operation}} · {{outcome}}")],
        w=18, h=5, unit="ops", stack=True)

    b.row("Integrity — a corrupt file is never served silently")
    b.stat("Integrity failures (1 h)", "sum(increase(filegrid_integrity_failures_total[1h])) or vector(0)", w=6, h=6)
    b.series("Failures by reason", [t(
        "sum by (category, storage, reason) (increase(filegrid_integrity_failures_total[5m])) > 0",
        "{{category}} · {{storage}} · {{reason}}")], w=18, h=6,
        description="checksum_mismatch / object_missing / decryption_failed: stored data is damaged. chunk_/file_checksum_mismatch: damaged in transit (the client resends).")

    b.row("Drone videos — transfer survives slow or unstable links")
    b.stat("Uploads in progress", "max(filegrid_uploads_in_progress) or vector(0)", w=6, h=5,
           steps=[{"color": "blue", "value": None}])
    b.stat("Stalled uploads", "max(filegrid_uploads_stalled) or vector(0)", w=6, h=5,
           description="No chunk received within the stall threshold (5 minutes by default). They can be resumed.")
    b.series("Chunk upload rate", [t('sum(rate(filegrid_bytes_total{operation="chunk"}[RI]))', "chunks in")],
             w=12, h=5, unit="Bps")

    b.row("Alerts")
    b.table("Alerts firing or pending", 'ALERTS{alertstate=~"firing|pending"}', h=6,
            description="Delivered to the team webhook by Alertmanager.",
            columns={"alertname": "Alert", "severity": "Severity", "guarantee": "Guarantee at risk",
                     "alertstate": "State", "category": "Category", "storage": "Storage"})
    return b


# ---------------------------------------------------------------------------------------
def trace() -> Board:
    b = Board("filegrid-trace", "FileGrid — Trace one operation",
              "Follow one request from the gateway to the storage and back with its trace id.")
    b.templating = [{
        "name": "trace_id", "label": "Trace id", "type": "textbox", "query": "",
        "current": {"text": "", "value": ""}, "hide": 0,
        "description": "The X-Trace-Id of the operation (returned in every response header and in the upload confirmation)."}]
    b.text("How to use", (
        "Every request gets a **trace id** (`X-Trace-Id` response header; the UI shows it after an upload). "
        "Paste it above to see, in time order, the gateway access line, the backend request, the file "
        "being stored, **each storage call** (which storage, which operation, how long) and the "
        "download. A meter reading accepted during a storage outage keeps its original trace id when "
        "it is written out later. Click any trace id in a log line to jump to all of its logs."), h=3)
    # Only the services that log trace ids. Loki and Grafana log the queries they receive, which
    # contain the id, so matching every service would fill this panel with the dashboard's own traffic.
    b.logs("Everything that happened under this trace id", '{service=~"backend|nginx"} |= "$trace_id"', h=11)
    b.logs("Storage calls only (spans)", '{service="backend"} |= "storage op" |= "$trace_id"', h=7,
           description="One line per call into a storage: storage, operation, outcome and duration_ms.")
    b.logs("Gateway access log", '{service="nginx"} |= "$trace_id"', h=6)
    return b


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for board in (operations(), guarantees(), trace()):
        path = os.path.join(OUT, f"{board.uid}.json")
        data = json.dumps(board.build(), indent=2, ensure_ascii=False) + "\n"
        with open(path, "wb") as fh:  # bytes: never CRLF, on any OS
            fh.write(data.encode("utf-8"))
        print(f"wrote {os.path.relpath(path, HERE)} ({len(board.panels)} panels)")


if __name__ == "__main__":
    main()
