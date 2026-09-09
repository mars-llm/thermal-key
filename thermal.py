#!/usr/bin/env python3
"""Thermal Key device control tool via CGMiner API (port 4028)."""

import argparse
import hashlib
import io
import json
import math
import os
import random
import re
import socket
import statistics
import sys
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from typing import Any, Deque, Dict, List, Optional, Tuple

# Work mode name mappings (0=Heater, 1=Mining, 2=Night)
MODE_NAMES = {0: "Heater", 1: "Mining", 2: "Night"}
MODE_ABBREV = {0: "H", 1: "M", 2: "N"}

DEVICE_PROFILES = {
    "mini3": {"short": "Mini3", "label": "Avalon Mini 3"},
    "nano3s": {"short": "Nano3s", "label": "Avalon Nano 3S"},
    "q": {"short": "Q", "label": "Avalon Q"},
}

DEFAULT_TEMP_KEYS = ["HBTemp", "OTemp", "TAvg", "MTavg", "TarT", "ITemp"]
DEVICE_TEMP_KEYS = {
    "mini3": DEFAULT_TEMP_KEYS,
    "nano3s": DEFAULT_TEMP_KEYS,
    "q": DEFAULT_TEMP_KEYS,
}

REJECT_RATE_WARNING_PERCENT = 5.0
REJECT_RATE_CRITICAL_PERCENT = 20.0
HASHRATE_DRIFT_WARNING_PERCENT = 15.0
HASHRATE_DRIFT_CRITICAL_PERCENT = 35.0
DEFAULT_BASELINE_WINDOW_SAMPLES = 12
DEFAULT_BASELINE_MINIMUM_SAMPLES = 3
ABSOLUTE_MINIMUM_BASELINE_SAMPLES = 2
BASELINE_SAMPLE_MINIMUM_RATIO = 1.0 - (HASHRATE_DRIFT_WARNING_PERCENT / 100.0)
THERMAL_HEADROOM_WARNING_C = 5.0
TEMPERATURE_WARNING_C = 90.0
TEMPERATURE_CRITICAL_C = 100.0
FAN_IMBALANCE_WARNING_RATIO = 0.25
RSSI_WARNING_DBM = -70
RSSI_CRITICAL_DBM = -80
POOL_LATENCY_WARNING_MS = 500
POOL_LATENCY_CRITICAL_MS = 1000
NETWORK_FAILURES_CRITICAL_COUNT = 100
HARDWARE_ERRORS_CRITICAL_COUNT = 10
MINIMUM_OK_HEALTH_SCORE = 90
MINIMUM_NONCRITICAL_HEALTH_SCORE = 60

HEALTH_SCORE_PENALTIES = {
    "reject_rate": {"warning": 15, "critical": 35},
    "hardware_errors": {"warning": 10, "critical": 20},
    "hashrate_drift": {"warning": 15, "critical": 35},
    "temperature": {"warning": 15, "critical": 30},
    "fan_error": {"critical": 30},
    "fan_stopped": {"critical": 30},
    "fan_imbalance": {"warning": 10},
    "rssi": {"warning": 10, "critical": 20},
    "latency": {"warning": 10, "critical": 20},
    "network_failures": {"warning": 5, "critical": 20},
}

READ_ONLY_COMMANDS = {
    "coin",
    "config",
    "version",
    "stats",
    "estats",
    "summary",
    "devs",
    "edevs",
    "devdetails",
    "pools",
}

READ_ONLY_ASCSET_PARAMETERS = {
    "0,help",
    "0,voltage",
    "0,workmode,get",
    "0,worklevel,get",
    "0,work_mode_lvl,get",
    "0,loop,get",
    "0,time,get",
}
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024

_LOG_WARNED_PATHS = set()
_LOG_WRITE_LOCK = threading.Lock()


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_jsonl(path: Optional[str], event: Dict[str, Any]):
    if not path:
        return
    log_path = os.path.expanduser(path)
    try:
        directory = os.path.dirname(log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {"timestamp": utc_timestamp(), "schema_version": "1"}
        payload.update(event)
        line = json.dumps(payload, sort_keys=True) + "\n"
        with _LOG_WRITE_LOCK:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError as exc:
        if log_path in _LOG_WARNED_PATHS:
            return
        _LOG_WARNED_PATHS.add(log_path)
        print(f"warning: cannot write log file {log_path}: {exc}", file=sys.stderr)


def redact_secret(value: Optional[str], show_secrets: bool, keep: int = 4) -> str:
    text = "" if value is None else str(value)
    if show_secrets:
        return text
    if not text:
        return "N/A"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}...{text[-keep:]}"


def fmt_unknown(value: Optional[Any], fmt_spec: str = "", unknown: str = "UNKNOWN") -> str:
    if value is None:
        return unknown
    if not fmt_spec:
        return str(value)
    try:
        return format(value, fmt_spec)
    except (TypeError, ValueError):
        return unknown


def _parse_optional_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_optional_int(value: Optional[str]) -> Optional[int]:
    parsed = _parse_optional_float(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (TypeError, ValueError, OverflowError):
        return None


def _to_int(value, default: Optional[int] = 0) -> Optional[int]:
    """Best-effort integer parser for mixed API payloads."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _to_float(value, default: Optional[float] = 0.0) -> Optional[float]:
    parsed = _parse_optional_float(value)
    return default if parsed is None else parsed


def _first_dict_entry(result: Optional[Dict], key: str) -> Optional[Dict]:
    if not isinstance(result, dict):
        return None
    entries = result.get(key)
    if not isinstance(entries, list) or not entries:
        return None
    entry = entries[0]
    return entry if isinstance(entry, dict) else None


def _status_msg(result: Optional[Dict]) -> str:
    status = _first_dict_entry(result, "STATUS")
    message = status.get("Msg") if status else None
    return message if isinstance(message, str) else ""


def _is_read_only_ascset(param: Optional[str]) -> bool:
    if not param:
        return False
    normalized = param.strip().lower()
    return normalized in READ_ONLY_ASCSET_PARAMETERS


def is_retry_safe_command(command: str, param: Optional[str] = None) -> bool:
    normalized = (command or "").strip().lower()
    if normalized in READ_ONLY_COMMANDS:
        return True
    return normalized == "ascset" and _is_read_only_ascset(param)


def _extract_ps_values(msg: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Extract (power_in, voltage, power_out) from a PS[...] status message."""
    match = re.search(r"PS\[([^\]]+)\]", msg)
    if not match:
        return None, None, None
    values = [_parse_optional_int(p) for p in re.findall(r"-?\d+(?:\.\d+)?", match.group(1))]
    if len(values) > 4:
        return values[1], values[2], values[4]
    return None, None, None


def _extract_loop_value(msg: str) -> Optional[int]:
    match = re.search(r"LOOP\[\s*(\d+)", msg)
    if not match:
        return None
    return _to_int(match.group(1), 0)


def _extract_work_mode_level(msg: str) -> Tuple[int, int]:
    mode_match = re.search(r"workmode\s+(\d+)", msg)
    level_match = re.search(r"worklevel\s+(\d+)", msg)
    return _to_int(mode_match.group(1), 0) if mode_match else 0, _to_int(level_match.group(1), 0) if level_match else 0


def _hashrate_to_th(entry: Dict, key: str) -> Optional[float]:
    value = _to_float(entry.get(key), None)
    if value is None:
        return None
    if key.startswith("MHS"):
        return value / 1_000_000
    if key.startswith("GHS"):
        return value / 1_000
    if key.startswith("KHS"):
        return value / 1_000_000_000
    return None


def _extract_hashrate_th(entry: Dict, keys: List[str]) -> Optional[float]:
    for key in keys:
        if key in entry:
            value = _hashrate_to_th(entry, key)
            if value is not None:
                return value
    return None


def _extract_reject_rate(summary: Dict, dev: Optional[Dict] = None) -> Optional[float]:
    """Extract actual rejected pool work percentage from CGMiner counters."""
    dev = dev or {}
    for entry, keys in (
        (summary, ["Pool Rejected%", "Device Rejected%"]),
        (dev, ["Pool Rejected%", "Device Rejected%"]),
    ):
        for key in keys:
            if key in entry:
                value = _to_float(entry.get(key), None)
                if value is not None and 0 <= value <= 100:
                    return value

    difficulty_accepted = _to_float(summary.get("Difficulty Accepted"), None)
    difficulty_rejected = _to_float(summary.get("Difficulty Rejected"), None)
    difficulty_stale = _to_float(summary.get("Difficulty Stale"), 0.0)
    if difficulty_accepted is not None and difficulty_rejected is not None:
        total = difficulty_accepted + difficulty_rejected + (difficulty_stale or 0.0)
        return difficulty_rejected / total * 100 if total > 0 else 0.0

    accepted = _to_float(summary.get("Accepted"), None)
    rejected = _to_float(summary.get("Rejected"), None)
    stale = _to_float(summary.get("Stale"), 0.0)
    if accepted is not None and rejected is not None:
        total = accepted + rejected + (stale or 0.0)
        return rejected / total * 100 if total > 0 else 0.0
    return None


def _normalize_product_text(prod: Optional[str]) -> str:
    text = (prod or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def device_key_from_product(prod: Optional[str]) -> str:
    text = _normalize_product_text(prod)
    if "nano3" in text or re.search(r"\bnano\s*3s?\b", text) or re.search(r"\bnano\s*3\s*s\b", text):
        return "nano3s"
    if "mini3" in text or re.search(r"\bmini\s*3\b", text):
        return "mini3"
    if "avalonq" in text or re.search(r"\bavalon\s*q\b", text):
        return "q"
    if re.search(r"\bq\b", text) and ("avalon" in text or "canaan" in text or "miner" in text or text == "q"):
        return "q"
    return "unknown"


def device_short_name(prod: Optional[str]) -> str:
    key = device_key_from_product(prod)
    profile = DEVICE_PROFILES.get(key)
    if profile:
        return profile["short"]
    if not prod:
        return "Unknown"
    return prod[:8]


def parse_mm_id0(mm: str, temp_keys: Optional[List[str]] = None) -> Dict:
    """Parse MM ID0 stats payload into normalized metrics."""
    def get(pattern) -> Optional[str]:
        match = re.search(pattern, mm)
        return match.group(1) if match else None

    def get_field(key: str) -> Optional[str]:
        match = re.search(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"\[([^\]]+)\]", mm)
        return match.group(1) if match else None

    def parse_number(text: Optional[str]) -> Optional[float]:
        if text is None:
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return _parse_optional_float(match.group(0)) if match else None

    def parse_int(text: Optional[str]) -> Optional[int]:
        value = parse_number(text)
        return int(value) if value is not None else None

    def select_temp(keys: List[str]) -> Optional[int]:
        for key in keys:
            value = parse_number(get_field(key))
            if value is None:
                continue
            if -40 <= value <= 200:
                return int(value)
        return None

    power_in: Optional[int] = None
    power_out: Optional[int] = None
    ps = get_field("PS")
    if ps:
        parts = [_parse_optional_int(p) for p in re.findall(r"-?\d+(?:\.\d+)?", ps)]
        if len(parts) > 4 and parts[1] is not None and parts[4] is not None:
            power_in, power_out = parts[1], parts[4]

    # Get SF0 base frequency (actual set value) - SF0[600 618 639 660]
    freq: Optional[int] = None
    sf0 = get_field("SF0")
    if sf0:
        parts = sf0.split()
        if parts:
            freq = _parse_optional_int(parts[0])
    if freq is None or freq <= 0:
        freq = parse_int(get_field("Freq"))

    # GHSmm = theoretical (chip capability), GHSavg = actual (submitted work)
    # ATA0/ATA1[power-temp-voltage-freq-?] - extract power (1st) and voltage (3rd)
    voltage: Optional[int] = None
    ata_power: Optional[int] = None
    ata = get_field("ATA1") or get_field("ATA0")
    if ata:
        parts = ata.split('-')
        if len(parts) >= 3:
            ata_power = _parse_optional_int(parts[0])
            voltage = _parse_optional_int(parts[2])

    # Use ATA power as fallback when PS reports zero (Nano 3S).
    if (power_in is None or power_in == 0) and ata_power is not None and ata_power > 0:
        power_in = ata_power
        if power_out == 0:
            power_out = None

    temp = select_temp(temp_keys or DEFAULT_TEMP_KEYS)
    temp_max = select_temp(["TMax", "MTmax"])
    temp_inlet = select_temp(["ITemp"])
    temp_board_inlet = select_temp(["HBITemp"])
    temp_board_outlet = select_temp(["HBOTemp"])
    target_temp = select_temp(["TarT"])
    fan_rpms = []
    for index in range(1, 5):
        rpm = parse_int(get_field(f"Fan{index}"))
        if rpm is not None and rpm >= 0:
            fan_rpms.append(rpm)
    fan_rpm = fan_rpms[0] if fan_rpms else None
    fan_pct = parse_int(get_field("FanR"))
    if fan_pct is not None and not 0 <= fan_pct <= 100:
        fan_pct = None
    fan_error = parse_int(get_field("FanErr"))

    rssi = parse_int(get_field("RSSI"))
    ping_ms = parse_int(get_field("PING"))
    net_failures_text = get_field("NETFAIL")
    net_failures = (
        [_parse_optional_int(value) for value in re.findall(r"-?\d+", net_failures_text)]
        if net_failures_text
        else []
    )
    net_failures = [value for value in net_failures if value is not None]
    net_failures_total = sum(net_failures) if net_failures else 0
    filter_value = parse_int(get_field("Filter"))
    memory_free_kb = parse_int(get_field("MEMFREE"))
    pf_count = parse_int(get_field("PFCnt"))

    hashrate_val = _parse_optional_float(get(r'GHSavg\[([0-9.]+)\]'))
    hashrate_max_val = _parse_optional_float(get(r'GHSmm\[([0-9.]+)\]'))
    if hashrate_max_val == 0 and hashrate_val is not None and hashrate_val > 0:
        hashrate_max_val = None
    uptime = _parse_optional_int(get(r'Elapsed\[(\d+)\]'))
    workmode = _parse_optional_int(get(r'WORKMODE\[(\d+)\]'))
    worklevel = _parse_optional_int(get(r'WORKLVL\[(\d+)\]'))
    if worklevel is None:
        worklevel = _parse_optional_int(get(r'WORKLEVEL\[(\d+)\]'))
    hw_errors = _parse_optional_int(get(r'HW\[(\d+)\]'))
    device_dh_percent = _parse_optional_float(get(r'DH\[([0-9.]+)%?\]'))
    dna = (get(r'DNA\[([0-9a-fA-F]+)\]') or '').lower()

    return {
        "hashrate": hashrate_val / 1000 if hashrate_val is not None else None,
        "hashrate_max": hashrate_max_val / 1000 if hashrate_max_val is not None else None,
        "uptime": uptime,
        "temp": temp,
        "temp_max": temp_max,
        "temp_inlet": temp_inlet,
        "temp_board_inlet": temp_board_inlet,
        "temp_board_outlet": temp_board_outlet,
        "target_temp": target_temp,
        "fan_rpm": fan_rpm,
        "fan_rpms": fan_rpms,
        "fan_pct": fan_pct,
        "fan_error": fan_error,
        "freq": freq,
        "voltage": voltage,
        "power_in": power_in,
        "power_out": power_out,
        "rssi": rssi,
        "ping_ms": ping_ms,
        "net_failures": net_failures,
        "net_failures_total": net_failures_total,
        "filter_value": filter_value,
        "memory_free_kb": memory_free_kb,
        "pf_count": pf_count,
        "workmode": workmode if workmode is not None else 1,
        "worklevel": worklevel if worklevel is not None else 0,
        "hw_errors": hw_errors if hw_errors is not None else 0,
        "reject_rate": None,
        "device_dh_percent": device_dh_percent if device_dh_percent is not None else 0.0,
        "dna": dna,
    }


def evaluate_health(stats: Dict, baseline_hashrate: Optional[float] = None) -> Dict[str, Any]:
    """Score a normalized stats snapshot and explain detected health risks."""
    score = 100
    issues: List[Dict[str, str]] = []

    def add_issue(code: str, severity: str, message: str):
        nonlocal score
        issues.append({"code": code, "severity": severity, "message": message})
        score = max(0, score - HEALTH_SCORE_PENALTIES[code][severity])

    reject_rate = _to_float(stats.get("reject_rate"), None)
    if reject_rate is not None:
        if reject_rate >= REJECT_RATE_CRITICAL_PERCENT:
            add_issue(
                "reject_rate",
                "critical",
                f"reject rate is {reject_rate:.1f}% "
                f"(critical >= {REJECT_RATE_CRITICAL_PERCENT:.0f}%)",
            )
        elif reject_rate >= REJECT_RATE_WARNING_PERCENT:
            add_issue(
                "reject_rate",
                "warning",
                f"reject rate is {reject_rate:.1f}% "
                f"(warning >= {REJECT_RATE_WARNING_PERCENT:.0f}%)",
            )

    hw_errors = _to_int(stats.get("hw_errors"), None)
    if hw_errors is not None and hw_errors > 0:
        if hw_errors >= HARDWARE_ERRORS_CRITICAL_COUNT:
            add_issue("hardware_errors", "critical", f"{hw_errors} hardware errors reported")
        else:
            add_issue("hardware_errors", "warning", f"{hw_errors} hardware errors reported")

    current_hashrate = _to_float(stats.get("hashrate"), None)
    hashrate_drop_pct = None
    if baseline_hashrate is not None and baseline_hashrate > 0 and current_hashrate is not None:
        hashrate_drop_pct = max(0.0, (baseline_hashrate - current_hashrate) / baseline_hashrate * 100)
        if hashrate_drop_pct >= HASHRATE_DRIFT_CRITICAL_PERCENT:
            add_issue(
                "hashrate_drift",
                "critical",
                f"hashrate is {hashrate_drop_pct:.1f}% below the {baseline_hashrate:.2f} TH/s baseline",
            )
        elif hashrate_drop_pct >= HASHRATE_DRIFT_WARNING_PERCENT:
            add_issue(
                "hashrate_drift",
                "warning",
                f"hashrate is {hashrate_drop_pct:.1f}% below the {baseline_hashrate:.2f} TH/s baseline",
            )

    observed_temp = _to_float(stats.get("temp_max"), None)
    if observed_temp is None:
        observed_temp = _to_float(stats.get("temp"), None)
    target_temp = _to_float(stats.get("target_temp"), None)
    thermal_headroom = None
    if observed_temp is not None and target_temp is not None:
        thermal_headroom = target_temp - observed_temp
        if thermal_headroom < 0:
            add_issue(
                "temperature",
                "critical",
                f"temperature is {-thermal_headroom:.1f}C above the {target_temp:.0f}C target",
            )
        elif thermal_headroom <= THERMAL_HEADROOM_WARNING_C:
            add_issue(
                "temperature",
                "warning",
                f"only {thermal_headroom:.1f}C thermal headroom remains",
            )
    elif observed_temp is not None:
        if observed_temp >= TEMPERATURE_CRITICAL_C:
            add_issue("temperature", "critical", f"temperature is {observed_temp:.0f}C")
        elif observed_temp >= TEMPERATURE_WARNING_C:
            add_issue("temperature", "warning", f"temperature is {observed_temp:.0f}C")

    fan_error = _to_int(stats.get("fan_error"), None)
    if fan_error is not None and fan_error > 0:
        add_issue("fan_error", "critical", f"firmware reports fan error {fan_error}")

    fan_rpms = [
        rpm
        for rpm in (_to_int(value, None) for value in (stats.get("fan_rpms") or []))
        if rpm is not None and rpm >= 0
    ]
    if len(fan_rpms) >= 2 and max(fan_rpms) > 0:
        if min(fan_rpms) == 0:
            add_issue("fan_stopped", "critical", "one or more fans report 0 RPM")
        else:
            fan_spread = (max(fan_rpms) - min(fan_rpms)) / max(fan_rpms)
            if fan_spread >= FAN_IMBALANCE_WARNING_RATIO:
                add_issue(
                    "fan_imbalance",
                    "warning",
                    f"fan RPMs differ by {fan_spread * 100:.0f}%",
                )

    rssi = _to_int(stats.get("rssi"), None)
    if rssi is not None:
        if rssi <= RSSI_CRITICAL_DBM:
            add_issue("rssi", "critical", f"Wi-Fi RSSI is weak at {rssi} dBm")
        elif rssi <= RSSI_WARNING_DBM:
            add_issue("rssi", "warning", f"Wi-Fi RSSI is low at {rssi} dBm")

    ping_ms = _to_int(stats.get("ping_ms"), None)
    if ping_ms is not None:
        if ping_ms >= POOL_LATENCY_CRITICAL_MS:
            add_issue("latency", "critical", f"pool latency is {ping_ms} ms")
        elif ping_ms >= POOL_LATENCY_WARNING_MS:
            add_issue("latency", "warning", f"pool latency is {ping_ms} ms")

    net_failures = _to_int(stats.get("net_failures_total"), None)
    if net_failures is not None and net_failures > 0:
        if net_failures >= NETWORK_FAILURES_CRITICAL_COUNT:
            add_issue("network_failures", "critical", f"{net_failures} network failures recorded")
        else:
            add_issue("network_failures", "warning", f"{net_failures} network failures recorded")

    has_critical = any(issue["severity"] == "critical" for issue in issues)
    if has_critical or score < MINIMUM_NONCRITICAL_HEALTH_SCORE:
        status = "CRITICAL"
    elif issues or score < MINIMUM_OK_HEALTH_SCORE:
        status = "WARN"
    else:
        status = "OK"

    return {
        "score": score,
        "status": status,
        "issues": issues,
        "baseline_hashrate": baseline_hashrate,
        "hashrate_drop_pct": hashrate_drop_pct,
        "thermal_headroom": thermal_headroom,
    }


class HealthTracker:
    """Maintain a mode-aware rolling baseline for live hashrate drift detection."""

    def __init__(
        self,
        window: int = DEFAULT_BASELINE_WINDOW_SAMPLES,
        minimum_samples: int = DEFAULT_BASELINE_MINIMUM_SAMPLES,
    ):
        self.minimum_samples = max(ABSOLUTE_MINIMUM_BASELINE_SAMPLES, minimum_samples)
        self.samples: Deque[float] = deque(maxlen=max(self.minimum_samples, window))
        self.mode_signature: Optional[Tuple[Any, Any]] = None
        self.baseline_hashrate: Optional[float] = None
        self.last_uptime: Optional[int] = None

    def evaluate(self, stats: Dict) -> Dict[str, Any]:
        mode_signature = (stats.get("workmode"), stats.get("worklevel"))
        uptime = _to_int(stats.get("uptime"), None)
        restarted = uptime is not None and self.last_uptime is not None and uptime < self.last_uptime
        if restarted or (self.mode_signature is not None and mode_signature != self.mode_signature):
            self.samples.clear()
            self.baseline_hashrate = None
        self.mode_signature = mode_signature
        if uptime is not None:
            self.last_uptime = uptime

        if len(self.samples) >= self.minimum_samples:
            candidate = float(statistics.median(self.samples))
            if self.baseline_hashrate is None or candidate > self.baseline_hashrate:
                self.baseline_hashrate = candidate

        health = evaluate_health(stats, baseline_hashrate=self.baseline_hashrate)
        current_hashrate = _to_float(stats.get("hashrate"), None)
        if current_hashrate is not None and current_hashrate > 0:
            if (
                self.baseline_hashrate is None
                or current_hashrate >= self.baseline_hashrate * BASELINE_SAMPLE_MINIMUM_RATIO
            ):
                self.samples.append(current_hashrate)
        return health


def append_health_event(
    path: Optional[str],
    host: str,
    command: str,
    health: Dict[str, Any],
    stats: Dict,
):
    append_jsonl(
        path,
        {
            "event": "health",
            "host": host,
            "command": command,
            "health_status": health["status"],
            "health_score": health["score"],
            "issue_codes": [issue["code"] for issue in health["issues"]],
            "baseline_hashrate": health.get("baseline_hashrate"),
            "hashrate_drop_pct": health.get("hashrate_drop_pct"),
            "thermal_headroom": health.get("thermal_headroom"),
            "hashrate": stats.get("hashrate"),
            "reject_rate": stats.get("reject_rate"),
            "device_dh_percent": stats.get("device_dh_percent"),
            "temp_max": stats.get("temp_max"),
            "target_temp": stats.get("target_temp"),
            "fan_error": stats.get("fan_error"),
            "rssi": stats.get("rssi"),
            "ping_ms": stats.get("ping_ms"),
            "net_failures_total": stats.get("net_failures_total"),
        },
    )


class Miner:
    """CGMiner API client."""

    def __init__(
        self,
        host: str,
        port: int = 4028,
        timeout: int = 10,
        retries: int = 0,
        backoff_ms: int = 200,
        jitter_pct: int = 20,
        log_jsonl: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff_ms = max(1, backoff_ms)
        self.jitter_pct = max(0, jitter_pct)
        self.log_jsonl = log_jsonl
        self._dna = None

    def _retry_delay_seconds(self, retry_index: int) -> float:
        # Exponential backoff with a small cap to keep retries bounded.
        base = (self.backoff_ms / 1000.0) * (2 ** min(16, max(0, retry_index)))
        capped = min(base, 5.0)
        if self.jitter_pct <= 0:
            return capped
        spread = self.jitter_pct / 100.0
        low = max(0.0, capped * (1.0 - spread))
        high = capped * (1.0 + spread)
        return random.uniform(low, high)

    def _log_cmd_event(self, event: str, command: str, attempt: int, **extra: Any):
        append_jsonl(
            self.log_jsonl,
            {
                "event": event,
                "host": self.host,
                "command": command,
                "attempt": attempt,
                **extra,
            },
        )

    def cmd(self, command: str, param: Optional[str] = None) -> Optional[Dict]:
        """Send command to CGMiner API."""
        retry_safe = is_retry_safe_command(command, param)
        max_attempts = self.retries + 1 if retry_safe else 1
        last_error = ""

        for attempt in range(1, max_attempts + 1):
            sock = None
            try:
                deadline = time.monotonic() + self.timeout
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))

                payload = {"command": command}
                if param:
                    payload["parameter"] = param

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("command deadline exceeded")
                sock.settimeout(remaining)
                sock.sendall(json.dumps(payload).encode())

                response = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout("response deadline exceeded")
                    sock.settimeout(remaining)
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        frame, separator, _ = chunk.partition(b"\x00")
                        response.extend(frame)
                        if len(response) > MAX_API_RESPONSE_BYTES:
                            raise ValueError("response exceeds size limit")
                        if separator:
                            break
                    except socket.timeout:
                        break

                parsed = json.loads(response.decode())
                if not isinstance(parsed, dict):
                    raise ValueError("response is not a JSON object")
                if attempt > 1:
                    self._log_cmd_event("retry_success", command, attempt)
                return parsed

            except (OSError, UnicodeDecodeError, ValueError) as exc:
                if isinstance(exc, ConnectionRefusedError):
                    last_error = f"connection refused ({self.host}:{self.port})"
                elif isinstance(exc, socket.timeout):
                    last_error = "connection timeout"
                elif isinstance(exc, ConnectionResetError):
                    last_error = "connection reset"
                elif isinstance(exc, socket.gaierror):
                    last_error = f"name resolution failed ({self.host})"
                elif isinstance(exc, (UnicodeDecodeError, ValueError)):
                    last_error = "invalid response"
                else:
                    last_error = str(exc) or exc.__class__.__name__

                if attempt < max_attempts:
                    delay = self._retry_delay_seconds(attempt - 1)
                    self._log_cmd_event("retry", command, attempt, error=last_error, delay_ms=int(delay * 1000))
                    time.sleep(delay)
                    continue
                print(f"error: {last_error}", file=sys.stderr)
            except Exception as exc:
                last_error = str(exc)
                print(f"error: {last_error}", file=sys.stderr)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            break

        self._log_cmd_event("command_error", command, max_attempts, error=last_error or "unknown")
        return None

    def ascset(self, param: str) -> Optional[Dict]:
        return self.cmd("ascset", param)

    def get_dna(self) -> Optional[str]:
        if self._dna:
            return self._dna
        stats = self.cmd("stats")
        entries = stats.get("STATS") if isinstance(stats, dict) else None
        for stat in entries if isinstance(entries, list) else []:
            if not isinstance(stat, dict):
                continue
            mm_payload = stat.get("MM ID0") or stat.get("MM ID0:Summary")
            if not isinstance(mm_payload, str):
                continue
            match = re.search(r'DNA\[([0-9a-fA-F]+)\]', mm_payload)
            if match:
                self._dna = match.group(1).lower()
                return self._dna

        version_entry = _first_dict_entry(self.cmd("version"), "VERSION")
        if version_entry:
            dna = version_entry.get("DNA")
            if isinstance(dna, str) and re.fullmatch(r"[0-9a-fA-F]+", dna):
                self._dna = dna.lower()
                return self._dna
        return None

    def _parse_fallback_stats(self, version_entry: Optional[Dict] = None) -> Dict:
        """Build metrics from summary/devs + ascset info when MM ID0 is unavailable."""
        summary = _first_dict_entry(self.cmd("summary"), "SUMMARY") or {}
        dev = _first_dict_entry(self.cmd("devs"), "DEVS") or {}
        if not summary and not dev:
            return {}

        hashrate = _extract_hashrate_th(summary, ["MHS av", "GHS av", "KHS av"])
        if hashrate is None:
            hashrate = _extract_hashrate_th(dev, ["MHS av", "GHS av", "KHS av"])

        hashrate_max = hashrate
        for key in ["MHS 5s", "MHS 1m", "MHS 5m", "MHS 15m", "GHS 5s", "GHS 1m", "GHS 5m", "GHS 15m"]:
            if key in summary:
                candidate = _hashrate_to_th(summary, key)
                if candidate is not None:
                    hashrate_max = candidate if hashrate_max is None else max(hashrate_max, candidate)
            if key in dev:
                candidate = _hashrate_to_th(dev, key)
                if candidate is not None:
                    hashrate_max = candidate if hashrate_max is None else max(hashrate_max, candidate)

        power_in = power_out = voltage = None
        ps_msg = _status_msg(self.ascset("0,voltage"))
        if "PS[" in ps_msg:
            power_in, voltage, power_out = _extract_ps_values(ps_msg)

        workmode = worklevel = 0
        work_msg = _status_msg(self.ascset("0,work_mode_lvl,get"))
        if "workmode" in work_msg and "worklevel" in work_msg:
            workmode, worklevel = _extract_work_mode_level(work_msg)
        else:
            workmode = _extract_work_mode_level(_status_msg(self.ascset("0,workmode,get")))[0]
            worklevel = _extract_work_mode_level(_status_msg(self.ascset("0,worklevel,get")))[1]

        freq = _extract_loop_value(_status_msg(self.ascset("0,loop,get")))

        if not version_entry:
            version_entry = _first_dict_entry(self.cmd("version"), "VERSION") or {}

        temperature = _to_int(dev.get("Temperature"), None)
        if temperature is not None and temperature <= 0:
            temperature = None

        return {
            "hashrate": hashrate,
            "hashrate_max": hashrate_max,
            "uptime": _to_int(summary.get("Elapsed", dev.get("Device Elapsed")), None),
            "temp": temperature,
            "temp_max": temperature,
            "temp_inlet": None,
            "temp_board_inlet": None,
            "temp_board_outlet": None,
            "target_temp": None,
            "fan_rpm": None,
            "fan_rpms": [],
            "fan_pct": None,
            "fan_error": None,
            "freq": freq,
            "voltage": voltage,
            "power_in": power_in,
            "power_out": power_out,
            "rssi": None,
            "ping_ms": None,
            "net_failures": [],
            "net_failures_total": 0,
            "filter_value": None,
            "memory_free_kb": None,
            "pf_count": None,
            "workmode": workmode,
            "worklevel": worklevel,
            "hw_errors": _to_int(dev.get("Hardware Errors", summary.get("Hardware Errors", 0)), 0),
            "reject_rate": _extract_reject_rate(summary, dev),
            "device_dh_percent": None,
            "dna": (version_entry.get("DNA", "") or "").lower(),
        }

    def parse_stats(self, device_key: Optional[str] = None, version_entry: Optional[Dict] = None) -> Dict:
        """Extract metrics from stats response."""
        def parse_entries(result: Optional[Dict]) -> Dict:
            entries = result.get("STATS") if isinstance(result, dict) else None
            if not isinstance(entries, list):
                return {}
            for stat in entries:
                if not isinstance(stat, dict):
                    continue
                mm_payload = stat.get("MM ID0") or stat.get("MM ID0:Summary")
                if not isinstance(mm_payload, str) or not mm_payload:
                    continue
                temp_keys = DEVICE_TEMP_KEYS.get(device_key, DEFAULT_TEMP_KEYS)
                return parse_mm_id0(mm_payload, temp_keys=temp_keys)
            return {}

        parsed = parse_entries(self.cmd("stats"))
        if parsed:
            summary = _first_dict_entry(self.cmd("summary"), "SUMMARY") or {}
            parsed["reject_rate"] = _extract_reject_rate(summary)
            return parsed

        if device_key == "q":
            parsed = parse_entries(self.cmd("estats"))
            if parsed:
                summary = _first_dict_entry(self.cmd("summary"), "SUMMARY") or {}
                parsed["reject_rate"] = _extract_reject_rate(summary)
                return parsed

        # Avalon Q and some newer firmwares may omit MM ID0 and expose metrics via summary/devs.
        if device_key == "q":
            return self._parse_fallback_stats(version_entry=version_entry)
        return {}

    def compute_auth(self, password: str) -> Dict[str, str]:
        """Compute web auth credentials."""
        dna = self.get_dna()
        if not dna:
            raise ValueError("Could not get DNA from device")

        webpass = hashlib.sha256(password.encode()).hexdigest()
        code = hashlib.sha256(dna.encode()).hexdigest()[:24]
        auth = hashlib.sha256((webpass[:8] + dna).encode()).hexdigest()[:8]
        verify = "ff0000ee" + hashlib.sha256((code + webpass[:24]).encode()).hexdigest()[:24]

        return {
            "dna": dna,
            "auth": auth,
            "verify": verify,
            "cookie": auth + webpass[:24],
        }

    def web_auth(self, password: str, creds: Optional[Dict[str, str]] = None) -> Optional[str]:
        """Authenticate and return session cookie."""
        creds = creds or self.compute_auth(password)
        auth_result = self.ascset(f"0,qr_auth,{creds['auth']},{creds['verify']}")
        auth_status = _first_dict_entry(auth_result, "STATUS")
        if not auth_status or auth_status.get("STATUS") not in {"S", "I"}:
            return None
        try:
            resp = urllib.request.urlopen(f"http://{self.host}/is_login.cgi", timeout=5).read().decode()
            match = re.search(r'"auth"\s*:\s*"([^"]+)"\s*,\s*"code"\s*:\s*"([^"]+)"', resp)
            if match:
                return match.group(1) + match.group(2)
        except (urllib.error.URLError, socket.timeout):
            pass
        return creds['cookie']


def parse_hosts(host_arg: str) -> List[str]:
    """Parse hosts from comma-separated list or file."""
    if os.path.isfile(host_arg):
        try:
            with open(host_arg, encoding='utf-8') as f:
                hosts = []
                for raw_line in f:
                    line = raw_line.strip()
                    if line and not line.startswith('#'):
                        hosts.append(line)
                return hosts
        except (IOError, UnicodeDecodeError) as e:
            print(f"error: cannot read hosts file: {e}", file=sys.stderr)
            sys.exit(1)
    return [h.strip() for h in host_arg.split(',') if h.strip()]


def fmt_uptime(s: Optional[int]) -> str:
    if s is None:
        return "UNKNOWN"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    return " ".join(parts) or "0m"


def print_ok(msg: str):
    print(f"ok: {msg}")


def print_err(msg: str):
    print(f"error: {msg}", file=sys.stderr)


def check_result(result: Optional[Dict], ok_msg: str) -> bool:
    status = _first_dict_entry(result, "STATUS")
    if not status:
        print_err("no response")
        return False
    if status.get("STATUS") == "S":
        print_ok(ok_msg)
        return True
    print_err(status.get("Msg", "unknown error"))
    return False


# Commands

def do_status(m: Miner, args, compact: bool = False):
    ver = _first_dict_entry(m.cmd("version"), "VERSION")
    if not ver:
        if compact:
            print(f"{m.host:<16} OFFLINE")
        else:
            print_err("failed to get version")
        return False
    prod = ver.get("PROD") or ver.get("MODEL") or ver.get("Model")
    device_key = device_key_from_product(prod)
    stats = m.parse_stats(device_key, version_entry=ver)
    health = evaluate_health(stats) if stats else None
    if health:
        append_health_event(
            getattr(args, "log_jsonl", None) or getattr(m, "log_jsonl", None),
            m.host,
            "status",
            health,
            stats,
        )

    if compact:
        # Single-line output for fleet view
        if stats:
            mode = MODE_ABBREV.get(stats['workmode'], '?')
            hr = fmt_unknown(stats.get("hashrate"), "6.1f", "   UNK")
            temp = fmt_unknown(stats.get("temp"), "3.0f", "UNK")
            fan = fmt_unknown(stats.get("fan_pct"), "3.0f", "UNK")
            power = fmt_unknown(stats.get("power_in"), "4.0f", " UNK")
            health_text = f"{health['status']} {health['score']}" if health else "UNKNOWN"
            print(
                f"{m.host:<16} {hr} TH/s  {temp}C  {fan}%  {power}W  {mode}  "
                f"{fmt_uptime(stats.get('uptime'))}  {health_text}"
            )
        else:
            print(f"{m.host:<16} CONNECTED (no stats)")
        return bool(stats)

    display_prod = prod or "Avalon Miner"
    print(f"\n  {display_prod} @ {m.host}")
    print(f"  DNA: {ver.get('DNA', 'N/A')}  MAC: {ver.get('MAC', 'N/A')}")
    print(f"  FW: {ver.get('LVERSION', 'N/A')}  CGMiner: {ver.get('CGMiner', 'N/A')}")

    if stats:
        efficiency = None
        if stats.get("hashrate") is not None and stats.get("power_in") is not None and stats['hashrate'] > 0 and stats['power_in'] > 0:
            efficiency = stats['power_in'] / stats['hashrate']
        hashrate = "UNKNOWN" if stats.get("hashrate") is None else f"{stats['hashrate']:.2f} TH/s"
        hashrate_max = "UNKNOWN" if stats.get("hashrate_max") is None else f"{stats['hashrate_max']:.2f}"
        temp = "UNKNOWN" if stats.get("temp") is None else f"{stats['temp']}C"
        temp_max = "UNKNOWN" if stats.get("temp_max") is None else f"{stats['temp_max']}C"
        fan_rpm = "UNKNOWN" if stats.get("fan_rpm") is None else str(stats["fan_rpm"])
        fan_pct = "UNKNOWN" if stats.get("fan_pct") is None else f"{stats['fan_pct']}%"
        power_in = "UNKNOWN" if stats.get("power_in") is None else f"{stats['power_in']}W"
        power_out = "UNKNOWN" if stats.get("power_out") is None else f"{stats['power_out']}W"
        freq = "UNKNOWN" if stats.get("freq") is None else f"{stats['freq']:.0f} MHz"
        voltage = "UNKNOWN" if stats.get("voltage") is None else f"{stats['voltage']} mV"
        print()
        print(f"  Hashrate   {hashrate} (max {hashrate_max})")
        reject_rate = stats.get("reject_rate")
        reject_text = "UNKNOWN" if reject_rate is None else f"{reject_rate:.1f}%"
        device_dh_percent = stats.get("device_dh_percent")
        dh_text = "UNKNOWN" if device_dh_percent is None else f"{device_dh_percent:.1f}%"
        print(f"  Errors     {reject_text} reject, {stats['hw_errors']} HW, {dh_text} DH")
        print(f"  Temp       {temp} (max {temp_max})")
        print(f"  Fan        {fan_rpm} RPM ({fan_pct})")
        eff_str = f"{efficiency:.1f} J/TH" if efficiency is not None else "N/A"
        print(f"  Power      {power_in} in, {power_out} out ({eff_str})")
        print(f"  Freq       {freq} @ {voltage}")
        print(f"  Mode       {MODE_NAMES.get(stats['workmode'], '?')} (level {stats['worklevel']})")
        print(f"  Uptime     {fmt_uptime(stats.get('uptime'))}")
        thermal_parts = []
        if stats.get("target_temp") is not None:
            thermal_parts.append(f"target {stats['target_temp']}C")
        if health and health.get("thermal_headroom") is not None:
            thermal_parts.append(f"headroom {health['thermal_headroom']:.0f}C")
        if stats.get("temp_inlet") is not None:
            thermal_parts.append(f"inlet {stats['temp_inlet']}C")
        if stats.get("temp_board_inlet") is not None:
            thermal_parts.append(f"board-in {stats['temp_board_inlet']}C")
        if stats.get("temp_board_outlet") is not None:
            thermal_parts.append(f"board-out {stats['temp_board_outlet']}C")
        if thermal_parts:
            print(f"  Thermal    {', '.join(thermal_parts)}")

        fan_rpms = stats.get("fan_rpms") or []
        if len(fan_rpms) > 1:
            print(f"  Fan array  {'/'.join(str(value) for value in fan_rpms)} RPM")

        network_parts = []
        if stats.get("rssi") is not None:
            network_parts.append(f"RSSI {stats['rssi']} dBm")
        if stats.get("ping_ms") is not None:
            network_parts.append(f"ping {stats['ping_ms']} ms")
        if stats.get("net_failures_total") is not None:
            network_parts.append(f"failures {stats['net_failures_total']}")
        if network_parts:
            print(f"  Network    {', '.join(network_parts)}")

        if health:
            print(f"  Health     {health['score']}/100 {health['status']}")
            for issue in health["issues"]:
                print(f"             - {issue['message']}")
    print()
    return bool(stats)


def do_pools(m: Miner, args):
    pools = m.cmd("pools")
    pool_entries = pools.get("POOLS") if isinstance(pools, dict) else None
    if not isinstance(pool_entries, list):
        print_err("failed to get pools")
        return False

    print(f"\n{'ID':<3} {'Status':<10} {'Active':<7} {'Accepted':<10} URL")
    print("-" * 70)
    for p in pool_entries:
        active = "*" if p.get("Stratum Active") else ""
        print(f"{p['POOL']:<3} {p['Status']:<10} {active:<7} {p['Accepted']:<10} {p['URL']}")
        print(f"    Worker: {p['User']}")
    print()
    return True


def do_watch(m: Miner, args):
    version_entry = _first_dict_entry(m.cmd("version"), "VERSION")
    device_key = "unknown"
    if version_entry:
        prod = version_entry.get("PROD") or version_entry.get("MODEL") or version_entry.get("Model")
        device_key = device_key_from_product(prod)
    health_tracker = HealthTracker()
    previous_line_width = 0
    print(f"Monitoring {m.host} (Ctrl+C to stop)\n")
    try:
        while True:
            # Recover if the initial version probe failed or could not be classified.
            if version_entry is None or device_key == "unknown":
                refreshed_entry = _first_dict_entry(m.cmd("version"), "VERSION")
                if refreshed_entry:
                    version_entry = refreshed_entry
                    prod = version_entry.get("PROD") or version_entry.get("MODEL") or version_entry.get("Model")
                    device_key = device_key_from_product(prod)

            stats = m.parse_stats(device_key, version_entry=version_entry)
            if stats:
                health = health_tracker.evaluate(stats)
                append_health_event(
                    getattr(args, "log_jsonl", None) or getattr(m, "log_jsonl", None),
                    m.host,
                    "watch",
                    health,
                    stats,
                )
                timestamp = time.strftime("%H:%M:%S")
                mode = MODE_ABBREV.get(stats['workmode'], '?')
                hashrate = "UNKNOWN" if stats.get("hashrate") is None else f"{stats['hashrate']:.1f} TH/s"
                temp = "UNKNOWN" if stats.get("temp") is None else f"{stats['temp']}C"
                fan = "UNKNOWN" if stats.get("fan_pct") is None else f"{stats['fan_pct']}%"
                power = "UNKNOWN" if stats.get("power_in") is None else f"{stats['power_in']}W"
                line = (
                    f"[{timestamp}] {hashrate} | {temp} | Fan {fan} | {power} | "
                    f"Mode {mode} | Health {health['status']} {health['score']} | "
                    f"Up {fmt_uptime(stats.get('uptime'))}"
                )
                print("\r" + line.ljust(previous_line_width), end='', flush=True)
                previous_line_width = len(line)
            else:
                timestamp = time.strftime("%H:%M:%S")
                line = f"[{timestamp}] {m.host} | UNAVAILABLE (no current stats)"
                print("\r" + line.ljust(previous_line_width), flush=True)
                previous_line_width = 0
                append_jsonl(
                    getattr(args, "log_jsonl", None) or getattr(m, "log_jsonl", None),
                    {"event": "watch_unavailable", "host": m.host, "command": "watch"},
                )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n")
    return True


def do_reboot(m: Miner, args):
    return check_result(m.ascset("0,reboot,1"), "rebooting")


def do_fan(m: Miner, args):
    try:
        speed = -1 if args.speed == "auto" else int(args.speed)
    except (TypeError, ValueError):
        print_err("fan speed must be 15-100 or 'auto'")
        return False
    if speed != -1 and not 15 <= speed <= 100:
        print_err("fan speed must be 15-100 or 'auto'")
        return False
    msg = "fan set to auto" if speed == -1 else f"fan set to {speed}%"
    return check_result(m.ascset(f"0,fan-spd,{speed}"), msg)


def do_freq(m: Miner, args):
    # Frequency format: pll0:pll1:pll2:pll3 (set all 4 PLLs to same value)
    f = args.freq
    return check_result(m.ascset(f"0,frequency,{f}:{f}:{f}:{f}"), f"frequency set to {f} MHz")


def do_mode(m: Miner, args):
    return check_result(m.ascset(f"0,workmode,set,{args.mode}"), f"mode set to {MODE_NAMES.get(args.mode, args.mode)}")


def do_level(m: Miner, args):
    return check_result(m.ascset(f"0,worklevel,set,{args.level}"), f"level set to {args.level}")


def do_work_mode_level(m: Miner, args):
    return check_result(
        m.ascset(f"0,work_mode_lvl,set,{args.mode},{args.level}"),
        f"mode set to {MODE_NAMES.get(args.mode, args.mode)} (level {args.level})",
    )


def do_voltage(m: Miner, args):
    if not 2150 <= args.mv <= 2600:
        print_err("voltage must be in range 2150-2600 mV")
        return False
    return check_result(m.ascset(f"0,voltage,{args.mv}"), f"voltage set to {args.mv} mV")


def do_solo_allowed(m: Miner, args):
    value_text = str(args.enabled).strip().lower()
    if value_text in {"1", "on", "true", "yes"}:
        value = 1
    elif value_text in {"0", "off", "false", "no"}:
        value = 0
    else:
        print_err("solo must be 0/1 (or off/on)")
        return False
    return check_result(m.ascset(f"0,solo-allowed,{value}"), f"solo-allowed set to {value}")


def do_qinfo(m: Miner, args):
    version_entry = _first_dict_entry(m.cmd("version"), "VERSION")
    if not version_entry:
        print_err("failed to get version")
        return False
    prod = version_entry.get("PROD") or version_entry.get("MODEL") or version_entry.get("Model") or "Unknown"
    if device_key_from_product(prod) != "q":
        print_err("qinfo is only available for Avalon Q devices")
        return False

    work_msg = _status_msg(m.ascset("0,work_mode_lvl,get"))
    loop_msg = _status_msg(m.ascset("0,loop,get"))
    ps_msg = _status_msg(m.ascset("0,voltage"))
    tz_msg = _status_msg(m.ascset("0,time,get"))

    mode, level = _extract_work_mode_level(work_msg)
    loop = _extract_loop_value(loop_msg)
    power_in, voltage, power_out = _extract_ps_values(ps_msg)

    print()
    print(f"  Device     {prod} @ {m.host}")
    print(f"  Work       {MODE_NAMES.get(mode, mode)} (level {level})")
    print(f"  Loop       {'UNKNOWN' if loop is None else loop}")
    power_in_text = "UNKNOWN" if power_in is None else f"{power_in}W"
    power_out_text = "UNKNOWN" if power_out is None else f"{power_out}W"
    voltage_text = "UNKNOWN" if voltage is None else f"{voltage} mV"
    print(f"  Power/PSU  {power_in_text} in, {power_out_text} out @ {voltage_text}")
    if tz_msg:
        print(f"  Timezone   {tz_msg.replace('ASC 0 set info: ', '')}")
    print()
    return True


def _print_info_status(result: Optional[Dict], ok_message: str = "") -> bool:
    status = _first_dict_entry(result, "STATUS")
    if not status:
        print_err("no response")
        return False
    state = status.get("STATUS")
    msg = status.get("Msg", "unknown error")
    if state in {"S", "I"}:
        if ok_message:
            print_ok(ok_message)
        else:
            print_ok(msg.replace("ASC 0 set info: ", ""))
        return True
    print_err(msg)
    return False


def do_loop(m: Miner, args):
    if args.value is None:
        result = m.ascset("0,loop,get")
        msg = _status_msg(result)
        loop = _extract_loop_value(msg)
        text = f"loop {loop}" if loop is not None else msg.replace("ASC 0 set info: ", "")
        return _print_info_status(result, text)

    result = m.ascset(f"0,loop,set,{args.value}")
    msg = _status_msg(result)
    loop = _extract_loop_value(msg)
    text = f"loop set to {loop}" if loop is not None else f"loop set request sent ({args.value})"
    return _print_info_status(result, text)


def do_timezone(m: Miner, args):
    result = m.ascset("0,time,get")
    msg = _status_msg(result).replace("ASC 0 set info: ", "")
    return _print_info_status(result, msg)


def do_switchpool(m: Miner, args):
    return check_result(m.cmd("switchpool", str(args.id)), f"switched to pool {args.id}")


def do_enablepool(m: Miner, args):
    return check_result(m.cmd("enablepool", str(args.id)), f"pool {args.id} enabled")


def do_disablepool(m: Miner, args):
    return check_result(m.cmd("disablepool", str(args.id)), f"pool {args.id} disabled")


def do_auth(m: Miner, args):
    try:
        show_secrets = getattr(args, "show_secrets", False)
        creds = m.compute_auth(args.password)
        cookie = m.web_auth(args.password)
        if not cookie:
            print_err("authentication failed")
            return False
        dna = redact_secret(creds["dna"], show_secrets)
        auth = redact_secret(creds["auth"], show_secrets)
        verify = redact_secret(creds["verify"], show_secrets)
        cookie_text = redact_secret(cookie, show_secrets)
        curl_cookie = cookie if show_secrets else "<redacted>"
        print(f"\n  DNA:    {dna}")
        print(f"  Auth:   {auth}")
        print(f"  Verify: {verify}")
        print(f"  Cookie: {cookie_text}")
        print(f"\n  curl -b 'auth={curl_cookie}' http://{m.host}/dashboard.cgi\n")
        return True
    except ValueError as e:
        print_err(str(e))
        return False


def do_getauth(m: Miner, args):
    try:
        show_secrets = getattr(args, "show_secrets", False)
        resp = urllib.request.urlopen(f"http://{m.host}/get_auth.cgi", timeout=5).read().decode()
        match = re.search(r'"auth"\s*:\s*"([^"]+)"', resp)
        if match:
            auth_value = match.group(1)
            print(f"auth: {redact_secret(auth_value, show_secrets)}")
            dna = m.get_dna()
            if dna:
                print(f"dna:  {redact_secret(dna, show_secrets)}")
                if show_secrets:
                    print(f"\nCrack with: python3 password.py -t {auth_value} --dna {dna} -w wordlist.txt")
                else:
                    print("\nCrack with: python3 password.py -t <auth> --dna <dna> -w wordlist.txt --show-secrets")
            return True
        else:
            print_err("could not parse auth")
            return False
    except Exception as e:
        print_err(str(e))
        return False


def print_json_result(result: Optional[Dict]) -> bool:
    """Print result as JSON or error if no response."""
    if result:
        print(json.dumps(result, indent=2))
        statuses = result.get("STATUS", [])
        return not any(
            isinstance(status, dict) and status.get("STATUS") in {"E", "F"}
            for status in (statuses if isinstance(statuses, list) else [])
        )
    else:
        print_err("no response")
        return False


def do_raw(m: Miner, args):
    return print_json_result(m.cmd(args.command, args.param))


def do_ascset(m: Miner, args):
    return print_json_result(m.ascset(args.param))


def do_help_ascset(m: Miner, args):
    result = m.ascset("0,help")
    status = _first_dict_entry(result, "STATUS")
    if status:
        msg = status.get("Msg", "")
        # Strip prefix like "ASC 0 set info: "
        if ": " in msg:
            msg = msg.split(": ", 1)[1]
        print("\nAvailable ascset commands:")
        for cmd in msg.split("|"):
            if cmd.strip():
                print(f"  {cmd}")
        print()
        return True
    else:
        print_err("failed to get command list")
        return False


MUTATING_COMMANDS = {
    "reboot",
    "fan",
    "freq",
    "mode",
    "level",
    "work-mode-level",
    "voltage",
    "solo",
    "switchpool",
    "enablepool",
    "disablepool",
    "auth",
}


def is_mutating_command(args) -> bool:
    if args.cmd in MUTATING_COMMANDS:
        return True
    if args.cmd == "loop":
        return args.value is not None
    if args.cmd == "raw":
        return not is_retry_safe_command(args.command, args.param)
    if args.cmd == "ascset":
        return not _is_read_only_ascset(args.param)
    return False


class ThreadLocalOutput:
    """Route output without letting workers replace process-global streams."""

    def __init__(self, fallback):
        self.fallback = fallback
        self.local = threading.local()

    def write(self, text):
        return getattr(self.local, "stream", self.fallback).write(text)

    def flush(self):
        return getattr(self.local, "stream", self.fallback).flush()

    def __getattr__(self, name):
        return getattr(self.fallback, name)

    @contextmanager
    def capture(self):
        previous = getattr(self.local, "stream", self.fallback)
        stream = io.StringIO()
        self.local.stream = stream
        try:
            yield stream
        finally:
            self.local.stream = previous


def main():
    prog = os.path.basename(sys.argv[0]) if sys.argv else 'thermal'
    parser = argparse.ArgumentParser(
        prog=prog,
        description='Thermal Key control tool (Avalon Mini 3 / Nano 3S / Avalon Q)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  status              Device status and metrics
  pools               List configured pools
  watch               Live monitoring

  reboot              Reboot device
  fan SPEED           Set fan (15-100 or 'auto')
  freq MHZ            Set chip frequency
  mode MODE           Set mode (0=Heater, 1=Mining, 2=Night)
  level N             Set performance level
  work-mode-level M L Set mode + level in one command (Avalon Q)
  voltage MV          Set PSU voltage in mV (Avalon Q: 2150-2600)
  solo VALUE          Set solo mining allowed (Avalon Q: 0/1)
  loop [VALUE]        Read/set Avalon Q loop parameter
  timezone            Show Avalon Q timezone from firmware
  qinfo               Show Avalon Q runtime info (mode, loop, PSU)

  switchpool ID       Switch active pool
  enablepool ID       Enable pool
  disablepool ID      Disable pool

  auth PASSWORD       Authenticate to web UI
  getauth             Get auth hash (for recovery)

  raw COMMAND         Send raw CGMiner command
  ascset PARAM        Send raw ascset command
  ascset-help         List ascset commands

Examples:
  thermal -H 192.168.1.100 status
  thermal -H 192.168.1.100 fan 80
  thermal -H 192.168.1.100 mode 1
  thermal -H 192.168.1.100 auth mypassword

Fleet management (multiple hosts):
  thermal -H 192.168.1.100,192.168.1.101,192.168.1.102 status
  thermal -H miners.txt reboot
  thermal -H miners.txt fan 100
""")

    parser.add_argument('-H', '--host', metavar='IP', help='Miner IP(s): single, comma-separated, or file path')
    parser.add_argument('-p', '--port', type=int, default=4028, help='API port (default: 4028)')
    parser.add_argument('-j', '--parallel', type=int, default=10, metavar='N', help='Max parallel connections (default: 10)')
    parser.add_argument('--retries', type=int, default=2, help='Retries for transient network errors (default: 2)')
    parser.add_argument('--backoff-ms', type=int, default=200, help='Initial retry backoff in ms (default: 200)')
    parser.add_argument('--jitter-pct', type=int, default=20, help='Retry jitter percentage (default: 20)')
    parser.add_argument('--no-preflight', action='store_true', help='Skip health-check preflight for mutating multi-host commands')
    parser.add_argument('--log-jsonl', metavar='PATH', help='Append structured run events to a JSONL file')
    parser.add_argument('--show-secrets', action='store_true', help='Show full auth/hash/cookie values in output')

    sub = parser.add_subparsers(dest='cmd', metavar='COMMAND')

    sub.add_parser('status', help='Device status')
    sub.add_parser('pools', help='List pools')
    w = sub.add_parser('watch', help='Live monitoring')
    w.add_argument('-i', '--interval', type=int, default=5, help='Update interval (default: 5)')

    sub.add_parser('reboot', help='Reboot device')
    f = sub.add_parser('fan', help='Set fan speed')
    f.add_argument('speed', help="15-100 or 'auto'")
    fr = sub.add_parser('freq', help='Set frequency')
    fr.add_argument('freq', type=int, help='MHz')
    mo = sub.add_parser('mode', help='Set work mode')
    mo.add_argument('mode', type=int, choices=[0, 1, 2], help='0=Heater, 1=Mining, 2=Night')
    lv = sub.add_parser('level', help='Set performance level')
    lv.add_argument('level', type=int, help='Level')
    ml = sub.add_parser('work-mode-level', help='Set mode and level together (Avalon Q)')
    ml.add_argument('mode', type=int, choices=[0, 1, 2], help='0=Heater, 1=Mining, 2=Night')
    ml.add_argument('level', type=int, help='Level')
    vo = sub.add_parser('voltage', help='Set PSU voltage (Avalon Q)')
    vo.add_argument('mv', type=int, help='2150-2600 mV')
    so = sub.add_parser('solo', help='Set solo-allowed (Avalon Q)')
    so.add_argument('enabled', help='0/1 or off/on')
    lo = sub.add_parser('loop', help='Read or set Avalon Q loop value')
    lo.add_argument('value', nargs='?', type=int, help='Optional loop value')
    sub.add_parser('timezone', help='Show Avalon Q timezone')
    sub.add_parser('qinfo', help='Show Avalon Q runtime info')

    sp = sub.add_parser('switchpool', help='Switch pool')
    sp.add_argument('id', type=int, help='Pool ID')
    ep = sub.add_parser('enablepool', help='Enable pool')
    ep.add_argument('id', type=int, help='Pool ID')
    dp = sub.add_parser('disablepool', help='Disable pool')
    dp.add_argument('id', type=int, help='Pool ID')

    au = sub.add_parser('auth', help='Web authentication')
    au.add_argument('password', help='Device password')
    sub.add_parser('getauth', help='Get auth hash for recovery')

    ra = sub.add_parser('raw', help='Raw CGMiner command')
    ra.add_argument('command', help='Command name')
    ra.add_argument('param', nargs='?', help='Parameter')
    asc = sub.add_parser('ascset', help='Raw ascset command')
    asc.add_argument('param', help="e.g. '0,fan-spd,80'")
    sub.add_parser('ascset-help', help='List ascset commands')

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.retries < 0:
        print("error: --retries must be >= 0", file=sys.stderr)
        sys.exit(1)
    if args.backoff_ms < 1:
        print("error: --backoff-ms must be >= 1", file=sys.stderr)
        sys.exit(1)
    if args.jitter_pct < 0:
        print("error: --jitter-pct must be >= 0", file=sys.stderr)
        sys.exit(1)
    if args.parallel < 1:
        print("error: --parallel must be >= 1", file=sys.stderr)
        sys.exit(1)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1-65535")
    if args.cmd == "watch" and args.interval < 1:
        parser.error("--interval must be >= 1")

    if not args.host:
        print("error: -H/--host required", file=sys.stderr)
        sys.exit(1)

    hosts = list(dict.fromkeys(parse_hosts(args.host)))
    if not hosts:
        print("error: no valid hosts specified", file=sys.stderr)
        sys.exit(1)

    cmds = {
        'status': do_status,
        'pools': do_pools,
        'watch': do_watch,
        'reboot': do_reboot,
        'fan': do_fan,
        'freq': do_freq,
        'mode': do_mode,
        'level': do_level,
        'work-mode-level': do_work_mode_level,
        'voltage': do_voltage,
        'solo': do_solo_allowed,
        'loop': do_loop,
        'timezone': do_timezone,
        'qinfo': do_qinfo,
        'switchpool': do_switchpool,
        'enablepool': do_enablepool,
        'disablepool': do_disablepool,
        'auth': do_auth,
        'getauth': do_getauth,
        'raw': do_raw,
        'ascset': do_ascset,
        'ascset-help': do_help_ascset,
    }

    if args.cmd not in cmds:
        parser.print_help()
        return

    def build_miner(host: str) -> Miner:
        return Miner(
            host,
            args.port,
            retries=args.retries,
            backoff_ms=args.backoff_ms,
            jitter_pct=args.jitter_pct,
            log_jsonl=args.log_jsonl,
        )

    def log_run_event(host: str, status: str, message: str):
        append_jsonl(
            args.log_jsonl,
            {
                "event": "run",
                "host": host,
                "command": args.cmd,
                "status": status,
                "message": message,
            },
        )

    # Single host - simple execution
    if len(hosts) == 1:
        m = build_miner(hosts[0])
        try:
            succeeded = cmds[args.cmd](m, args)
        except Exception as exc:
            print_err(str(exc))
            log_run_event(hosts[0], "failed", str(exc))
            return 1
        if succeeded is False:
            log_run_event(hosts[0], "failed", "single-host command failed")
        else:
            log_run_event(hosts[0], "completed", "single-host run finished")
        return 1 if succeeded is False else 0

    # Multiple hosts - parallel execution with fleet view
    if args.cmd == 'watch':
        print("error: watch command not supported with multiple hosts", file=sys.stderr)
        sys.exit(1)

    def get_fleet_status(host: str) -> Tuple[str, bool]:
        """Get compact status line for a single host."""
        m = build_miner(host)
        version_entry = _first_dict_entry(m.cmd("version"), "VERSION")
        if not version_entry:
            log_run_event(host, "offline", "version probe failed")
            return f"{host:<16} {'':>8}  OFFLINE", False
        prod = version_entry.get("PROD") or version_entry.get("MODEL") or version_entry.get("Model")
        dev = device_short_name(prod)
        stats = m.parse_stats(device_key_from_product(prod), version_entry=version_entry)
        if stats:
            health = evaluate_health(stats)
            append_health_event(args.log_jsonl, host, "status", health, stats)
            mode = MODE_ABBREV.get(stats['workmode'], '?')
            hashrate = fmt_unknown(stats.get("hashrate"), "6.1f", "   UNK")
            temp = fmt_unknown(stats.get("temp"), "3.0f", "UNK")
            fan = fmt_unknown(stats.get("fan_pct"), "3.0f", "UNK")
            power = fmt_unknown(stats.get("power_in"), "4.0f", " UNK")
            health_text = f"{health['status']:<8} {health['score']:>3}"
            log_run_event(host, "ok", "status collected")
            return (f"{host:<16} {dev:>8}  {hashrate} TH/s  {temp}C  "
                    f"{fan}%  {power}W  {mode}  {fmt_uptime(stats.get('uptime')):<10}  {health_text}"), True
        log_run_event(host, "degraded", "connected without stats")
        return f"{host:<16} {dev:>8}  CONNECTED (no stats)", False

    stdout = ThreadLocalOutput(sys.stdout)
    stderr = ThreadLocalOutput(sys.stderr)

    def run_command_on_host(host: str) -> Tuple[str, bool]:
        """Run command on host and capture output."""
        m = build_miner(host)
        with stdout.capture() as out, stderr.capture() as err:
            try:
                succeeded = cmds[args.cmd](m, args)
            except SystemExit:
                succeeded = False
            except Exception as exc:
                print_err(str(exc))
                succeeded = False
        result = out.getvalue()
        errors = err.getvalue()
        failed = succeeded is False
        if failed:
            message = errors.strip().splitlines()[-1] if errors.strip() else "command reported failure"
            log_run_event(host, "failed", message)
        else:
            log_run_event(host, "ok", "command completed")
        return f"[{host}]\n{result}{errors}", not failed

    preflight_health = {host: True for host in hosts}
    if is_mutating_command(args) and not args.no_preflight:
        def preflight_host(host: str) -> Tuple[str, bool]:
            miner = build_miner(host)
            version_entry = _first_dict_entry(miner.cmd("version"), "VERSION")
            healthy = version_entry is not None
            append_jsonl(
                args.log_jsonl,
                {
                    "event": "preflight",
                    "host": host,
                    "command": args.cmd,
                    "status": "ok" if healthy else "failed",
                },
            )
            return host, healthy

        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = [executor.submit(preflight_host, host) for host in hosts]
            for future in as_completed(futures):
                host, healthy = future.result()
                preflight_health[host] = healthy

        failed_hosts = [host for host, ok in preflight_health.items() if not ok]
        if failed_hosts:
            print("warning: preflight failed, skipping hosts:")
            for host in failed_hosts:
                print(f"  - {host}")

    if args.cmd == 'status':
        # Fleet status - parallel fetch, ordered output
        print(
            f"\n{'HOST':<16} {'TYPE':>8}  {'HASHRATE':>10}  {'TEMP':>4}  "
            f"{'FAN':>4}  {'POWER':>5}  {'M':>2}  {'UPTIME':<10}  HEALTH"
        )
        print("-" * 100)

        results = {}
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {executor.submit(get_fleet_status, host): host for host in hosts}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    results[host] = future.result()
                except Exception as e:
                    results[host] = (f"{host:<16} ERROR: {e}", False)
                    log_run_event(host, "failed", str(e))

        # Print in original order
        for host in hosts:
            print(results[host][0])
        print()
    else:
        # Other commands - parallel fetch, ordered output
        results = {}
        with redirect_stdout(stdout), redirect_stderr(stderr), ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {
                executor.submit(run_command_on_host, host): host
                for host in hosts
                if preflight_health.get(host, True)
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    results[host] = future.result()
                except Exception as e:
                    results[host] = (f"[{host}]\nerror: {e}\n", False)
                    log_run_event(host, "failed", str(e))
            for host in hosts:
                if not preflight_health.get(host, True):
                    results[host] = (f"[{host}]\nskipped: preflight failed\n", False)

        # Print in original order
        for host in hosts:
            print(results[host][0])

    return 0 if all(succeeded for _, succeeded in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
