#!/usr/bin/env python3
"""Build the POST /analytics/measurements body from a TSV of ingest timings.

Columns, tab-separated, no header: category, storage, http_code, seconds, bytes.
Usage: post_measurements.py benchmark < samples.tsv
       post_measurements.py scenario normal < samples.tsv
"""

import json
import sys
from collections import defaultdict


def _percentile(sorted_ms, q):
    # Same index as scripts/demo/spike-10x.sh: int(q * n), capped at the last sample.
    if not sorted_ms:
        return 0.0
    return sorted_ms[min(len(sorted_ms) - 1, int(q * len(sorted_ms)))]


def summarize(samples):
    groups = defaultdict(list)
    for row in samples:
        groups[(row["category"], row["storage"])].append(row)
    out = []
    for (category, storage), rows in sorted(groups.items()):
        ok = [row for row in rows if row["code"] == 202]
        latencies = sorted(row["seconds"] * 1000.0 for row in ok)
        elapsed = sum(row["seconds"] for row in ok)
        total_bytes = sum(row["bytes"] for row in ok)
        mbps = (total_bytes / elapsed / 1_000_000) if elapsed > 0 else 0.0
        average = sum(latencies) / len(latencies) if latencies else 0.0
        out.append({
            "category": category,
            "storage": storage,
            "samples": len(rows),
            "ok": len(ok),
            "errors": len(rows) - len(ok),
            "latency_avg_ms": round(average, 3),
            "latency_p50_ms": round(_percentile(latencies, 0.50), 3),
            "latency_p95_ms": round(_percentile(latencies, 0.95), 3),
            "latency_p99_ms": round(_percentile(latencies, 0.99), 3),
            "throughput_mbps": round(mbps, 4),
            "bytes_total": int(total_bytes),
        })
    return out


def main(argv):
    kind = argv[1]
    scenario = argv[2] if len(argv) > 2 else None
    if kind not in ("benchmark", "scenario"):
        return "kind must be benchmark or scenario"
    if kind == "scenario" and scenario not in ("normal", "peak"):
        return "scenario must be normal or peak"
    samples = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        category, storage, code, seconds, nbytes = line.split("\t")
        samples.append({
            "category": category,
            "storage": storage,
            "code": int(code),
            "seconds": float(seconds),
            "bytes": int(float(nbytes)),
        })
    if not samples:
        return "no samples"
    body = {"kind": kind, "rows": summarize(samples)}
    if kind == "scenario":
        body["scenario"] = scenario
    json.dump(body, sys.stdout)
    sys.stdout.write("\n")
    return None


if __name__ == "__main__":
    error = main(sys.argv)
    if error:
        print(error, file=sys.stderr)
        sys.exit(1)
