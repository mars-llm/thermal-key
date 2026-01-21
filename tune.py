#!/usr/bin/env python3
"""Quick frequency tuning script for Avalon Mini 3."""

import re
import subprocess
import sys
import time

DEFAULT_HOST = "192.168.130.132"
SETTLE_TIME_SECONDS = 90
SAMPLE_COUNT = 3
SAMPLE_INTERVAL_SECONDS = 5
TEST_FREQUENCIES_MHZ = [400, 450, 500, 550, 600, 650, 700, 750]


def run_command(host: str, cmd: str) -> str:
    """Execute a mini3.py command and return combined output."""
    result = subprocess.run(
        f"python3 mini3.py -H {host} {cmd}",
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def extract_match(pattern: str, text: str, group: int = 1, default: float = 0) -> float:
    """Extract a numeric value from text using regex pattern."""
    match = re.search(pattern, text)
    return float(match.group(group)) if match else default


def get_stats(host: str) -> dict:
    """Fetch current miner statistics."""
    output = run_command(host, "status")
    return {
        "hashrate": extract_match(r"Hashrate\s+([0-9.]+)", output),
        "efficiency": extract_match(r"\(([0-9.]+) J/TH\)", output),
        "temp": int(extract_match(r"Temp\s+(\d+)C", output)),
        "temp_max": int(extract_match(r"Temp\s+\d+C \(max (\d+)C\)", output)),
        "freq": int(extract_match(r"Freq\s+(\d+)", output)),
    }


def collect_samples(host: str, count: int, interval: float) -> list[dict]:
    """Collect multiple stat samples with specified interval."""
    samples = []
    for i in range(count):
        samples.append(get_stats(host))
        if i < count - 1:
            time.sleep(interval)
    return samples


def average_samples(target_freq: int, samples: list[dict]) -> dict:
    """Calculate averaged/max stats from samples."""
    return {
        "freq": target_freq,
        "hashrate": sum(s["hashrate"] for s in samples) / len(samples),
        "efficiency": sum(s["efficiency"] for s in samples) / len(samples),
        "temp": max(s["temp"] for s in samples),
        "temp_max": max(s["temp_max"] for s in samples),
    }


def print_result(result: dict) -> None:
    """Print a single frequency test result."""
    print(
        f"\r{result['freq']:>6} "
        f"{result['hashrate']:>10.2f} "
        f"{result['efficiency']:>10.1f} J/TH "
        f"{result['temp']:>5}C "
        f"{result['temp_max']:>5}C"
    )


def main() -> None:
    """Run frequency tuning tests."""
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST

    print("Avalon Mini 3 Frequency Tuning")
    print(f"Host: {host}")
    print(f"Settle time: {SETTLE_TIME_SECONDS}s per test")
    print()

    print("Setting fan to 100%...")
    run_command(host, "fan 100")
    time.sleep(2)

    print()
    print(f"{'Freq':>6} {'Hashrate':>10} {'Efficiency':>12} {'Temp':>6} {'TMax':>6}")
    print("-" * 50)

    results = []
    for target_freq in TEST_FREQUENCIES_MHZ:
        print(f"Testing {target_freq} MHz...", end=" ", flush=True)
        run_command(host, f"freq {target_freq}")
        time.sleep(SETTLE_TIME_SECONDS)

        samples = collect_samples(host, SAMPLE_COUNT, SAMPLE_INTERVAL_SECONDS)
        result = average_samples(target_freq, samples)
        results.append(result)
        print_result(result)

    print()
    print("Summary:")
    print("-" * 50)

    best_hashrate = max(results, key=lambda x: x["hashrate"])
    best_efficiency = min(results, key=lambda x: x["efficiency"] if x["efficiency"] > 0 else 999)

    print(
        f"Best hashrate:   {best_hashrate['freq']} MHz -> "
        f"{best_hashrate['hashrate']:.2f} TH/s ({best_hashrate['efficiency']:.1f} J/TH)"
    )
    print(
        f"Best efficiency: {best_efficiency['freq']} MHz -> "
        f"{best_efficiency['hashrate']:.2f} TH/s ({best_efficiency['efficiency']:.1f} J/TH)"
    )


if __name__ == "__main__":
    main()
