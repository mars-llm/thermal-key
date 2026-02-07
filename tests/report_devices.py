"""Emit a JSON report describing device capabilities and stats."""

import argparse
import json
import os
import sys
import time

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import thermal

from tests.device_test_config import HOSTS, PORT, TIMEOUT


def _ascset_commands(miner):
    result = miner.ascset("0,help")
    if not result or "STATUS" not in result:
        return []
    msg = result["STATUS"][0].get("Msg", "")
    if ": " in msg:
        msg = msg.split(": ", 1)[1]
    return [cmd.strip() for cmd in msg.split("|") if cmd.strip()]


def _summarize_stats_raw(stats_raw):
    if not stats_raw or "STATS" not in stats_raw:
        return {}
    mm_ids = [s.get("MM ID0") for s in stats_raw["STATS"] if "MM ID0" in s]
    sample_keys = list(stats_raw["STATS"][0].keys()) if stats_raw["STATS"] else []
    return {
        "mm_id0": mm_ids[0] if mm_ids else "",
        "stat_keys": sorted(sample_keys),
    }


def collect_host(host):
    miner = thermal.Miner(host, PORT, timeout=TIMEOUT)
    entry = {
        "host": host,
        "port": PORT,
        "reachable": False,
        "device_key": "unknown",
        "version": {},
        "summary": {},
        "stats_parsed": {},
        "stats_raw": {},
        "ascset_commands": [],
        "errors": [],
    }

    version = miner.cmd("version")
    if not version or "VERSION" not in version:
        entry["errors"].append("no version response")
        return entry

    entry["reachable"] = True
    entry["version"] = version["VERSION"][0]
    prod = entry["version"].get("PROD") or entry["version"].get("MODEL") or entry["version"].get("Model")
    entry["device_key"] = thermal.device_key_from_product(prod)

    summary = miner.cmd("summary")
    if summary and "SUMMARY" in summary and summary["SUMMARY"]:
        entry["summary"] = summary["SUMMARY"][0]
    else:
        entry["errors"].append("no summary response")

    stats_parsed = miner.parse_stats(entry["device_key"], version_entry=entry["version"])
    if stats_parsed:
        entry["stats_parsed"] = stats_parsed
    else:
        entry["errors"].append("parse_stats empty")

    stats_raw = miner.cmd("stats")
    entry["stats_raw"] = _summarize_stats_raw(stats_raw)
    if not entry["stats_raw"]:
        entry["errors"].append("no stats response")

    entry["ascset_commands"] = _ascset_commands(miner)
    if not entry["ascset_commands"]:
        entry["errors"].append("no ascset help response")

    return entry


def main():
    parser = argparse.ArgumentParser(description="Emit JSON report for Canaan Avalon devices")
    parser.add_argument("--out", help="Write JSON to file instead of stdout")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hosts": [collect_host(host) for host in HOSTS],
    }

    payload = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)

    if args.out:
        out_path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    else:
        print(payload)


if __name__ == "__main__":
    main()
