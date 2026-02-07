#!/usr/bin/env python3
"""Thermal Key device control tool via CGMiner API (port 4028)."""

import argparse
import hashlib
import io
import json
import os
import re
import socket
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout, redirect_stderr
from typing import Dict, List, Optional, Tuple

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


def _to_int(value, default: int = 0) -> int:
    """Best-effort integer parser for mixed API payloads."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _status_msg(result: Optional[Dict]) -> str:
    if not result:
        return ""
    return result.get("STATUS", [{}])[0].get("Msg", "")


def _extract_ps_values(msg: str) -> Tuple[int, int, int]:
    """Extract (power_in, voltage, power_out) from a PS[...] status message."""
    match = re.search(r"PS\[([^\]]+)\]", msg)
    if not match:
        return 0, 0, 0
    values = [_to_int(p, 0) for p in re.findall(r"-?\d+(?:\.\d+)?", match.group(1))]
    if len(values) > 4:
        return values[1], values[2], values[4]
    return 0, 0, 0


def _extract_loop_value(msg: str) -> int:
    match = re.search(r"LOOP\[\s*(\d+)", msg)
    if not match:
        return 0
    return _to_int(match.group(1), 0)


def _extract_work_mode_level(msg: str) -> Tuple[int, int]:
    mode_match = re.search(r"workmode\s+(\d+)", msg)
    level_match = re.search(r"worklevel\s+(\d+)", msg)
    return _to_int(mode_match.group(1), 0) if mode_match else 0, _to_int(level_match.group(1), 0) if level_match else 0


def _hashrate_to_th(entry: Dict, key: str) -> float:
    value = _to_float(entry.get(key), 0.0)
    if key.startswith("MHS"):
        return value / 1_000_000
    if key.startswith("GHS"):
        return value / 1_000
    if key.startswith("KHS"):
        return value / 1_000_000_000
    return 0.0


def _extract_hashrate_th(entry: Dict, keys: List[str]) -> float:
    for key in keys:
        if key in entry:
            return _hashrate_to_th(entry, key)
    return 0.0


def _normalize_product_text(prod: Optional[str]) -> str:
    text = (prod or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def device_key_from_product(prod: Optional[str]) -> str:
    text = _normalize_product_text(prod)
    if "nano3" in text:
        return "nano3s"
    if "mini3" in text:
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
    def get(pattern, default="0"):
        m = re.search(pattern, mm)
        return m.group(1) if m else default

    def get_field(key: str) -> Optional[str]:
        match = re.search(re.escape(key) + r"\[([^\]]+)\]", mm)
        return match.group(1) if match else None

    def parse_number(text: Optional[str], default: Optional[float] = None) -> Optional[float]:
        if text is None:
            return default
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(m.group(0)) if m else default

    def parse_int(text: Optional[str], default: int = 0) -> int:
        value = parse_number(text, None)
        if value is None:
            return default
        return int(value)

    def select_temp(keys: List[str]) -> int:
        for key in keys:
            value = parse_number(get_field(key), None)
            if value is None:
                continue
            if -40 <= value <= 200:
                return int(value)
        return 0

    power_in, power_out = 0, 0
    ps = get_field("PS")
    if ps:
        parts = [int(p) for p in re.findall(r"-?\d+", ps)]
        if len(parts) > 4:
            power_in, power_out = parts[1], parts[4]

    # Get SF0 base frequency (actual set value) - SF0[600 618 639 660]
    freq = 0
    sf0 = get_field("SF0")
    if sf0:
        parts = sf0.split()
        if parts:
            freq = int(float(parts[0]))
    if not freq:
        freq = parse_int(get_field("Freq"), 0)

    # GHSmm = theoretical (chip capability), GHSavg = actual (submitted work)
    # ATA1[power-temp-voltage-freq-?] - extract power (1st) and voltage (3rd)
    voltage = 0
    ata_power = 0
    ata = get_field("ATA1")
    if ata:
        parts = ata.split('-')
        if len(parts) >= 3:
            try:
                ata_power = int(float(parts[0]))
                voltage = int(float(parts[2]))
            except ValueError:
                pass

    # Use ATA1 power as fallback when PS reports zero (Nano 3S)
    if power_in == 0 and ata_power > 0:
        power_in = ata_power

    temp = select_temp(temp_keys or DEFAULT_TEMP_KEYS)
    temp_max = select_temp(["TMax", "MTmax"])
    worklevel = parse_int(get(r'WORKLVL\[(\d+)\]', None), 0)
    if worklevel == 0:
        worklevel = parse_int(get(r'WORKLEVEL\[(\d+)\]', None), 0)

    return {
        "hashrate": float(get(r'GHSavg\[([0-9.]+)\]')) / 1000,
        "hashrate_max": float(get(r'GHSmm\[([0-9.]+)\]')) / 1000,
        "uptime": int(get(r'Elapsed\[(\d+)\]')),
        "temp": temp,
        "temp_max": temp_max,
        "fan_rpm": parse_int(get_field("Fan1"), 0),
        "fan_pct": parse_int(get_field("FanR"), 0),
        "freq": freq,
        "voltage": voltage,
        "power_in": power_in,
        "power_out": power_out,
        "workmode": int(get(r'WORKMODE\[(\d+)\]', "1")),
        "worklevel": worklevel,
        "hw_errors": int(get(r'HW\[(\d+)\]')),
        "dh_rate": float(get(r'DH\[([0-9.]+)%?\]', "0")),
        "dna": get(r'DNA\[([0-9a-fA-F]+)\]', '').lower(),
    }


class Miner:
    """CGMiner API client."""

    def __init__(self, host: str, port: int = 4028, timeout: int = 10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._dna = None

    def cmd(self, command: str, param: str = None) -> Optional[Dict]:
        """Send command to CGMiner API."""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))

            payload = {"command": command}
            if param:
                payload["parameter"] = param

            sock.send(json.dumps(payload).encode())

            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    if b'\x00' in response:
                        break
                except socket.timeout:
                    break

            return json.loads(response.rstrip(b'\x00').decode())

        except ConnectionRefusedError:
            print(f"error: connection refused ({self.host}:{self.port})", file=sys.stderr)
        except socket.timeout:
            print("error: connection timeout", file=sys.stderr)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        return None

    def ascset(self, param: str) -> Optional[Dict]:
        return self.cmd("ascset", param)

    def get_dna(self) -> Optional[str]:
        if self._dna:
            return self._dna
        stats = self.cmd("stats")
        if not stats or "STATS" not in stats:
            stats = {"STATS": []}
        for stat in stats.get("STATS", []):
            mm_payload = stat.get("MM ID0")
            if not mm_payload:
                continue
            match = re.search(r'DNA\[([0-9a-fA-F]+)\]', mm_payload)
            if match:
                self._dna = match.group(1).lower()
                return self._dna

        ver = self.cmd("version")
        if ver and "VERSION" in ver and ver["VERSION"]:
            dna = ver["VERSION"][0].get("DNA")
            if dna:
                self._dna = dna.lower()
                return self._dna
        return None

    def _parse_fallback_stats(self, version_entry: Optional[Dict] = None) -> Dict:
        """Build metrics from summary/devs + ascset info when MM ID0 is unavailable."""
        summary_resp = self.cmd("summary")
        devs_resp = self.cmd("devs")
        summary = summary_resp.get("SUMMARY", [{}])[0] if summary_resp and "SUMMARY" in summary_resp else {}
        dev = devs_resp.get("DEVS", [{}])[0] if devs_resp and "DEVS" in devs_resp else {}
        if not summary and not dev:
            return {}

        hashrate = _extract_hashrate_th(summary, ["MHS av", "GHS av", "KHS av"])
        if not hashrate:
            hashrate = _extract_hashrate_th(dev, ["MHS av", "GHS av", "KHS av"])

        hashrate_max = hashrate
        for key in ["MHS 5s", "MHS 1m", "MHS 5m", "MHS 15m", "GHS 5s", "GHS 1m", "GHS 5m", "GHS 15m"]:
            if key in summary:
                hashrate_max = max(hashrate_max, _hashrate_to_th(summary, key))
            if key in dev:
                hashrate_max = max(hashrate_max, _hashrate_to_th(dev, key))

        power_in = power_out = voltage = 0
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
            ver = self.cmd("version")
            if ver and "VERSION" in ver and ver["VERSION"]:
                version_entry = ver["VERSION"][0]
            else:
                version_entry = {}

        return {
            "hashrate": hashrate,
            "hashrate_max": hashrate_max,
            "uptime": _to_int(summary.get("Elapsed", dev.get("Device Elapsed", 0)), 0),
            "temp": _to_int(dev.get("Temperature", 0), 0),
            "temp_max": _to_int(dev.get("Temperature", 0), 0),
            "fan_rpm": 0,
            "fan_pct": 0,
            "freq": freq,
            "voltage": voltage,
            "power_in": power_in,
            "power_out": power_out,
            "workmode": workmode,
            "worklevel": worklevel,
            "hw_errors": _to_int(dev.get("Hardware Errors", summary.get("Hardware Errors", 0)), 0),
            "dh_rate": _to_float(dev.get("Device Rejected%", summary.get("Device Rejected%", summary.get("Pool Rejected%", 0))), 0.0),
            "dna": (version_entry.get("DNA", "") or "").lower(),
        }

    def parse_stats(self, device_key: Optional[str] = None, version_entry: Optional[Dict] = None) -> Dict:
        """Extract metrics from stats response."""
        stats = self.cmd("stats")
        if not stats or "STATS" not in stats:
            if device_key == "q":
                return self._parse_fallback_stats(version_entry=version_entry)
            return {}

        for stat in stats["STATS"]:
            if "MM ID0" not in stat:
                continue
            temp_keys = DEVICE_TEMP_KEYS.get(device_key, DEFAULT_TEMP_KEYS)
            return parse_mm_id0(stat["MM ID0"], temp_keys=temp_keys)

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

    def web_auth(self, password: str) -> Optional[str]:
        """Authenticate and return session cookie."""
        creds = self.compute_auth(password)
        self.ascset(f"0,qr_auth,{creds['auth']},{creds['verify']}")
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
                return [line.strip() for line in f if line.strip() and not line.startswith('#')]
        except (IOError, UnicodeDecodeError) as e:
            print(f"error: cannot read hosts file: {e}", file=sys.stderr)
            sys.exit(1)
    return [h.strip() for h in host_arg.split(',') if h.strip()]


def fmt_uptime(s: int) -> str:
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
    if not result:
        print_err("no response")
        return False
    status = result.get("STATUS", [{}])[0]
    if status.get("STATUS") == "S":
        print_ok(ok_msg)
        return True
    print_err(status.get("Msg", "unknown error"))
    return False


# Commands

def do_status(m: Miner, args, compact: bool = False):
    ver = m.cmd("version")
    if not ver or "VERSION" not in ver:
        if compact:
            print(f"{m.host:<16} OFFLINE")
        else:
            print_err("failed to get version")
        return
    ver = ver["VERSION"][0]
    prod = ver.get("PROD") or ver.get("MODEL") or ver.get("Model")
    device_key = device_key_from_product(prod)
    stats = m.parse_stats(device_key, version_entry=ver)

    if compact:
        # Single-line output for fleet view
        if stats:
            mode = MODE_ABBREV.get(stats['workmode'], '?')
            print(f"{m.host:<16} {stats['hashrate']:>6.1f} TH/s  {stats['temp']:>3}C  "
                  f"{stats['fan_pct']:>3}%  {stats['power_in']:>4}W  {mode}  {fmt_uptime(stats['uptime'])}")
        else:
            print(f"{m.host:<16} CONNECTED (no stats)")
        return

    print(f"\n  {ver.get('PROD', 'Avalon Miner')} @ {m.host}")
    print(f"  DNA: {ver.get('DNA', 'N/A')}  MAC: {ver.get('MAC', 'N/A')}")
    print(f"  FW: {ver.get('LVERSION', 'N/A')}  CGMiner: {ver.get('CGMiner', 'N/A')}")

    if stats:
        efficiency = None
        if stats['hashrate'] > 0 and stats['power_in'] > 0:
            efficiency = stats['power_in'] / stats['hashrate']
        print()
        print(f"  Hashrate   {stats['hashrate']:.2f} TH/s (max {stats['hashrate_max']:.2f})")
        print(f"  Errors     {stats['dh_rate']:.1f}% reject, {stats['hw_errors']} HW")
        print(f"  Temp       {stats['temp']}C (max {stats['temp_max']}C)")
        print(f"  Fan        {stats['fan_rpm']} RPM ({stats['fan_pct']}%)")
        eff_str = f"{efficiency:.1f} J/TH" if efficiency is not None else "N/A"
        print(f"  Power      {stats['power_in']}W in, {stats['power_out']}W out ({eff_str})")
        print(f"  Freq       {stats['freq']:.0f} MHz @ {stats['voltage']} mV")
        print(f"  Mode       {MODE_NAMES.get(stats['workmode'], '?')} (level {stats['worklevel']})")
        print(f"  Uptime     {fmt_uptime(stats['uptime'])}")
    print()


def do_pools(m: Miner, args):
    pools = m.cmd("pools")
    if not pools or "POOLS" not in pools:
        print_err("failed to get pools")
        return

    print(f"\n{'ID':<3} {'Status':<10} {'Active':<7} {'Accepted':<10} URL")
    print("-" * 70)
    for p in pools["POOLS"]:
        active = "*" if p.get("Stratum Active") else ""
        print(f"{p['POOL']:<3} {p['Status']:<10} {active:<7} {p['Accepted']:<10} {p['URL']}")
        print(f"    Worker: {p['User']}")
    print()


def do_watch(m: Miner, args):
    ver = m.cmd("version")
    prod = None
    version_entry = None
    if ver and "VERSION" in ver and ver["VERSION"]:
        version_entry = ver["VERSION"][0]
        prod = version_entry.get("PROD") or version_entry.get("MODEL") or version_entry.get("Model")
    device_key = device_key_from_product(prod)
    print(f"Monitoring {m.host} (Ctrl+C to stop)\n")
    try:
        while True:
            stats = m.parse_stats(device_key, version_entry=version_entry)
            if stats:
                timestamp = time.strftime("%H:%M:%S")
                mode = MODE_ABBREV.get(stats['workmode'], '?')
                print(f"\r[{timestamp}] {stats['hashrate']:.1f} TH/s | {stats['temp']}C | "
                      f"Fan {stats['fan_pct']}% | {stats['power_in']}W | "
                      f"Mode {mode} | Up {fmt_uptime(stats['uptime'])}", end='')
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n")


def do_reboot(m: Miner, args):
    check_result(m.ascset("0,reboot,1"), "rebooting")


def do_fan(m: Miner, args):
    speed = -1 if args.speed == "auto" else int(args.speed)
    if speed != -1 and not 15 <= speed <= 100:
        print_err("fan speed must be 15-100 or 'auto'")
        return
    msg = "fan set to auto" if speed == -1 else f"fan set to {speed}%"
    check_result(m.ascset(f"0,fan-spd,{speed}"), msg)


def do_freq(m: Miner, args):
    # Frequency format: pll0:pll1:pll2:pll3 (set all 4 PLLs to same value)
    f = args.freq
    check_result(m.ascset(f"0,frequency,{f}:{f}:{f}:{f}"), f"frequency set to {f} MHz")


def do_mode(m: Miner, args):
    check_result(m.ascset(f"0,workmode,set,{args.mode}"), f"mode set to {MODE_NAMES.get(args.mode, args.mode)}")


def do_level(m: Miner, args):
    check_result(m.ascset(f"0,worklevel,set,{args.level}"), f"level set to {args.level}")


def do_work_mode_level(m: Miner, args):
    check_result(
        m.ascset(f"0,work_mode_lvl,set,{args.mode},{args.level}"),
        f"mode set to {MODE_NAMES.get(args.mode, args.mode)} (level {args.level})",
    )


def do_voltage(m: Miner, args):
    if not 2150 <= args.mv <= 2600:
        print_err("voltage must be in range 2150-2600 mV")
        return
    check_result(m.ascset(f"0,voltage,{args.mv}"), f"voltage set to {args.mv} mV")


def do_solo_allowed(m: Miner, args):
    value_text = str(args.enabled).strip().lower()
    if value_text in {"1", "on", "true", "yes"}:
        value = 1
    elif value_text in {"0", "off", "false", "no"}:
        value = 0
    else:
        print_err("solo must be 0/1 (or off/on)")
        return
    check_result(m.ascset(f"0,solo-allowed,{value}"), f"solo-allowed set to {value}")


def do_qinfo(m: Miner, args):
    version = m.cmd("version")
    if not version or "VERSION" not in version or not version["VERSION"]:
        print_err("failed to get version")
        return
    prod = version["VERSION"][0].get("PROD", "Unknown")
    if device_key_from_product(prod) != "q":
        print_err("qinfo is only available for Avalon Q devices")
        return

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
    print(f"  Loop       {loop}")
    print(f"  Power/PSU  {power_in}W in, {power_out}W out @ {voltage} mV")
    if tz_msg:
        print(f"  Timezone   {tz_msg.replace('ASC 0 set info: ', '')}")
    print()


def _print_info_status(result: Optional[Dict], ok_message: str = "") -> bool:
    if not result:
        print_err("no response")
        return False
    status = result.get("STATUS", [{}])[0]
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
        text = f"loop {loop}" if loop else msg.replace("ASC 0 set info: ", "")
        _print_info_status(result, text)
        return

    result = m.ascset(f"0,loop,set,{args.value}")
    msg = _status_msg(result)
    loop = _extract_loop_value(msg)
    text = f"loop set to {loop}" if loop else f"loop set request sent ({args.value})"
    _print_info_status(result, text)


def do_timezone(m: Miner, args):
    result = m.ascset("0,time,get")
    msg = _status_msg(result).replace("ASC 0 set info: ", "")
    _print_info_status(result, msg)


def do_switchpool(m: Miner, args):
    check_result(m.cmd("switchpool", str(args.id)), f"switched to pool {args.id}")


def do_enablepool(m: Miner, args):
    check_result(m.cmd("enablepool", str(args.id)), f"pool {args.id} enabled")


def do_disablepool(m: Miner, args):
    check_result(m.cmd("disablepool", str(args.id)), f"pool {args.id} disabled")


def do_auth(m: Miner, args):
    try:
        creds = m.compute_auth(args.password)
        cookie = m.web_auth(args.password)
        print(f"\n  DNA:    {creds['dna']}")
        print(f"  Auth:   {creds['auth']}")
        print(f"  Verify: {creds['verify']}")
        print(f"  Cookie: {cookie}")
        print(f"\n  curl -b 'auth={cookie}' http://{m.host}/dashboard.cgi\n")
    except ValueError as e:
        print_err(str(e))


def do_getauth(m: Miner, args):
    try:
        resp = urllib.request.urlopen(f"http://{m.host}/get_auth.cgi", timeout=5).read().decode()
        match = re.search(r'"auth"\s*:\s*"([^"]+)"', resp)
        if match:
            print(f"auth: {match.group(1)}")
            dna = m.get_dna()
            if dna:
                print(f"dna:  {dna}")
                print(f"\nCrack with: python3 crack.py -t {match.group(1)} --dna {dna} -w wordlist.txt")
        else:
            print_err("could not parse auth")
    except Exception as e:
        print_err(str(e))


def print_json_result(result: Optional[Dict]):
    """Print result as JSON or error if no response."""
    if result:
        print(json.dumps(result, indent=2))
    else:
        print_err("no response")


def do_raw(m: Miner, args):
    print_json_result(m.cmd(args.command, args.param))


def do_ascset(m: Miner, args):
    print_json_result(m.ascset(args.param))


def do_help_ascset(m: Miner, args):
    result = m.ascset("0,help")
    if result and "STATUS" in result:
        msg = result["STATUS"][0].get("Msg", "")
        # Strip prefix like "ASC 0 set info: "
        if ": " in msg:
            msg = msg.split(": ", 1)[1]
        print("\nAvailable ascset commands:")
        for cmd in msg.split("|"):
            if cmd.strip():
                print(f"  {cmd}")
        print()
    else:
        print_err("failed to get command list")


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

    if not args.host:
        print("error: -H/--host required", file=sys.stderr)
        sys.exit(1)

    hosts = parse_hosts(args.host)
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

    # Single host - simple execution
    if len(hosts) == 1:
        m = Miner(hosts[0], args.port)
        cmds[args.cmd](m, args)
        return

    # Multiple hosts - parallel execution with fleet view
    if args.cmd == 'watch':
        print("error: watch command not supported with multiple hosts", file=sys.stderr)
        sys.exit(1)

    def get_fleet_status(host: str) -> str:
        """Get compact status line for a single host."""
        m = Miner(host, args.port)
        ver = m.cmd("version")
        if not ver or "VERSION" not in ver:
            return f"{host:<16} {'':>8}  OFFLINE"
        version_entry = ver["VERSION"][0]
        prod = version_entry.get("PROD") or version_entry.get("MODEL") or version_entry.get("Model")
        dev = device_short_name(prod)
        stats = m.parse_stats(device_key_from_product(prod), version_entry=version_entry)
        if stats:
            mode = MODE_ABBREV.get(stats['workmode'], '?')
            return (f"{host:<16} {dev:>8}  {stats['hashrate']:>6.1f} TH/s  {stats['temp']:>3}C  "
                    f"{stats['fan_pct']:>3}%  {stats['power_in']:>4}W  {mode}  {fmt_uptime(stats['uptime'])}")
        return f"{host:<16} {dev:>8}  CONNECTED (no stats)"

    def run_command_on_host(host: str) -> str:
        """Run command on host and capture output."""
        m = Miner(host, args.port)
        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                cmds[args.cmd](m, args)
        except SystemExit:
            pass  # Ignore sys.exit calls from within commands
        except Exception as e:
            return f"[{host}]\nerror: {e}\n"
        result = out.getvalue()
        errors = err.getvalue()
        return f"[{host}]\n{result}{errors}" if result or errors else f"[{host}]\n"

    if args.cmd == 'status':
        # Fleet status - parallel fetch, ordered output
        print(f"\n{'HOST':<16} {'TYPE':>8}  {'HASHRATE':>10}  {'TEMP':>4}  {'FAN':>4}  {'POWER':>5}  {'M':>2}  UPTIME")
        print("-" * 80)

        results = {}
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {executor.submit(get_fleet_status, host): host for host in hosts}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    results[host] = future.result()
                except Exception as e:
                    results[host] = f"{host:<16} ERROR: {e}"

        # Print in original order
        for host in hosts:
            print(results.get(host, f"{host:<16} UNKNOWN"))
        print()
    else:
        # Other commands - parallel fetch, ordered output
        results = {}
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {executor.submit(run_command_on_host, host): host for host in hosts}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    results[host] = future.result()
                except Exception as e:
                    results[host] = f"[{host}]\nerror: {e}\n"

        # Print in original order
        for host in hosts:
            print(results.get(host, f"[{host}]\nUNKNOWN\n"))


if __name__ == "__main__":
    main()
