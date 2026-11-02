"""Bounded, read-only Avalon Q snapshots for monitoring consumers."""

import argparse
from datetime import datetime, timezone
import json
import math
import re
import sys

if __package__:
    from .thermal import Miner, device_key_from_product, mode_name
else:
    from thermal import Miner, device_key_from_product, mode_name

SCHEMA_VERSION = 1
CLIENT_VERSION = "1.0.0"


class TelemetryError(Exception):
    """The device did not provide a usable current snapshot."""


class ReadOnlyMiner(Miner):
    def cmd(self, command, param=None):
        if command not in {"version", "litestats", "summary"} or param is not None:
            raise TelemetryError("Unsupported telemetry command")
        return super().cmd(command)


def _entry(response, section):
    if not isinstance(response, dict):
        raise TelemetryError("Telemetry request failed")
    statuses = response.get("STATUS")
    if not isinstance(statuses, list) or not statuses or any(
        not isinstance(row, dict) or row.get("STATUS") not in ("S", "I") for row in statuses
    ):
        raise TelemetryError("Device did not confirm telemetry response")
    rows = response.get(section)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise TelemetryError("Invalid telemetry section")
    return rows[0]


def _number(value, minimum=0, maximum=1e15):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def _integer(value, minimum=0, maximum=1e15):
    number = _number(value, minimum, maximum)
    return int(number) if number is not None and number.is_integer() else None


def _version(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", value) else None


def normalize_snapshot(version, stats, summary):
    """Normalize three confirmed responses without exposing raw response objects."""
    ver = _entry(version, "VERSION")
    product = ver.get("PROD") or ver.get("MODEL")
    if not isinstance(product, str) or device_key_from_product(product) != "q":
        raise TelemetryError("Unsupported telemetry model")
    row = _entry(stats, "STATS")
    totals = _entry(summary, "SUMMARY")
    text = row.get("MM ID0:Summary") or row.get("MM ID0")
    if not isinstance(text, str) or not text or len(text) > 65536:
        raise TelemetryError("Invalid telemetry stats")

    def field(key):
        matches = re.findall(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"\[([^\]]*)\]", text)
        return matches[0].strip() if len(matches) == 1 else None

    def temperature(key, chip=False):
        value = _number(field(key), -40, 150)
        return None if chip and value is not None and value <= 0 else value

    hashrate = _number(totals.get("MHS av"))
    elapsed = _number(totals.get("Elapsed"))
    if hashrate is None or elapsed is None:
        raise TelemetryError("Missing summary measurements")
    mode = _integer(field("WORKMODE"), maximum=2)
    state = _integer(field("STATE"), maximum=3)
    metrics = {
        "hashrate_th_s": hashrate / 1_000_000,
        "uptime_minutes": elapsed / 60,
        "inlet_temperature_c": temperature("ITemp"),
        "board_inlet_temperature_c": temperature("HBITemp", chip=True),
        "board_outlet_temperature_c": temperature("HBOTemp", chip=True),
        "asic_average_temperature_c": temperature("TAvg", chip=True),
        "asic_max_temperature_c": temperature("TMax", chip=True),
        "fan_speed_percent": _number((field("FanR") or "").rstrip("%"), maximum=100),
        "fan_error": _integer(field("FanErr"), maximum=65535),
        "work_mode": mode_name(mode, "q") if mode is not None else None,
        "operating_state": {0: "Initializing", 1: "Working", 2: "Standby", 3: "Fault"}.get(state),
        "accepted_shares": _integer(totals.get("Accepted")),
        "rejected_shares": _integer(totals.get("Rejected")),
        "hardware_errors": _integer(totals.get("Hardware Errors")),
    }
    for index in range(1, 5):
        metrics[f"fan_{index}_rpm"] = _integer(field(f"Fan{index}"), maximum=30000)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "model": "Avalon Q",
        "firmware_version": _version(ver.get("LVERSION")),
        "api_version": _version(ver.get("API")),
        "metrics": metrics,
    }


class QTelemetryClient:
    def __init__(self, host, port=4028, timeout=3):
        if not isinstance(host, str) or not host.strip() or any(c.isspace() for c in host):
            raise ValueError("A single telemetry host is required")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid telemetry port")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10:
            raise ValueError("Telemetry timeout must be between zero and ten seconds")
        self.miner = ReadOnlyMiner(host, port, timeout=timeout, retries=0)

    def read_snapshot(self):
        version = self.miner.cmd("version")
        ver = _entry(version, "VERSION")
        product = ver.get("PROD") or ver.get("MODEL")
        if not isinstance(product, str) or device_key_from_product(product) != "q":
            raise TelemetryError("Unsupported telemetry model")
        stats = self.miner.cmd("litestats")
        _entry(stats, "STATS")
        summary = self.miner.cmd("summary")
        return normalize_snapshot(version, stats, summary)


def main():
    parser = argparse.ArgumentParser(description="Read an Avalon Q telemetry snapshot as JSON")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=4028)
    parser.add_argument("--timeout", type=float, default=3)
    args = parser.parse_args()
    try:
        print(json.dumps(QTelemetryClient(args.host, args.port, args.timeout).read_snapshot(), allow_nan=False))
    except (TelemetryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
