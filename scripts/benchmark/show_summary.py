#!/usr/bin/env python3
"""Print GET /analytics/summary and exit 1 when the shape, or the expected run, is missing.

Usage: show_summary.py summary.json [benchmark|normal|peak]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from analytics import ZONES  # noqa: E402

# The verdicts contain Romanian letters; a Windows console (cp1252) cannot print them and the
# script used to crash AFTER the measurements were already stored.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CATEGORIES = ("meter_readings", "outage_records", "connection_docs", "drone_videos")


def main(argv):
    doc = json.load(open(argv[1], encoding="utf-8"))
    expect = argv[2] if len(argv) > 2 else ""
    matrix = doc.get("storage_matrix") or []
    zones = doc.get("zone_insights") or []
    narrative = doc.get("narrative") or {}
    if [row.get("category") for row in matrix] != list(CATEGORIES):
        return "storage_matrix is missing a category"
    if [zone.get("zone") for zone in zones] != list(ZONES):
        return "zone_insights must list every Romanian county"
    for key in ("storage_verdict", "storm_isolation", "network"):
        if not narrative.get(key):
            return f"narrative.{key} is empty"
    print(narrative["storage_verdict"])
    print()
    print(narrative["storm_isolation"])
    print()
    print(narrative["network"])
    print()
    for row in matrix:
        print(
            f"{row['category']:20} {row['storage']:8} "
            f"avg={row['latency_avg_ms']} ms  p95={row['latency_p95_ms']} ms  "
            f"{row['throughput_mbps']} MB/s  err={row['error_rate']}"
        )
        print(f"  {row['verdict']}")
    print()
    for point in doc.get("load_comparison") or []:
        print(
            f"{point['label']}: contoare {point['meter_readings_ms']} ms, "
            f"avarii {point['outage_records_ms']} ms, "
            f"racordări {point['connection_docs_ms']} ms, "
            f"dronă {point['drone_videos_ms']} ms"
        )
    if expect == "benchmark" and "Nu există încă măsurători" in narrative["storage_verdict"]:
        return "benchmark did not reach the summary"
    load = doc.get("load_comparison") or [{}, {}]
    if expect == "normal" and load[0].get("meter_readings_ms", 0) <= 0:
        return "normal day recorded no meter latency"
    if expect == "peak":
        peak = load[1] if len(load) > 1 else {}
        if peak.get("meter_readings_ms", 0) <= 0 and peak.get("outage_records_ms", 0) <= 0:
            return "peak recorded no latency"
    return None


if __name__ == "__main__":
    error = main(sys.argv)
    if error:
        print(error, file=sys.stderr)
        sys.exit(1)
