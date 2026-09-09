"""Unit tests for extended health metrics and performance scoring."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import thermal


def _healthy_stats(**overrides):
    stats = {
        "hashrate": 50.0,
        "reject_rate": 1.0,
        "device_dh_percent": 20.0,
        "hw_errors": 0,
        "temp": 60,
        "temp_max": 65,
        "target_temp": 80,
        "fan_rpms": [1400, 1420, 1410, 1390],
        "fan_error": 0,
        "rssi": -55,
        "ping_ms": 120,
        "net_failures_total": 0,
        "workmode": 1,
        "worklevel": 0,
    }
    stats.update(overrides)
    return stats


class HealthScoreTests(unittest.TestCase):
    def test_healthy_snapshot_scores_100(self):
        health = thermal.evaluate_health(_healthy_stats())
        self.assertEqual(health["status"], "OK")
        self.assertEqual(health["score"], 100)
        self.assertEqual(health["issues"], [])
        self.assertEqual(health["thermal_headroom"], 15)

    def test_reject_rate_thresholds(self):
        warning = thermal.evaluate_health(_healthy_stats(reject_rate=5.0))
        critical = thermal.evaluate_health(_healthy_stats(reject_rate=20.0))
        self.assertEqual(warning["status"], "WARN")
        self.assertEqual(warning["score"], 85)
        self.assertEqual(warning["issues"][0]["code"], "reject_rate")
        self.assertEqual(critical["status"], "CRITICAL")
        self.assertEqual(critical["score"], 65)

    def test_device_dh_diagnostic_is_not_treated_as_pool_rejection(self):
        health = thermal.evaluate_health(
            _healthy_stats(reject_rate=0.0, device_dh_percent=40.0)
        )
        self.assertEqual(health["status"], "OK")
        self.assertNotIn("reject_rate", {issue["code"] for issue in health["issues"]})

    def test_reject_rate_falls_back_to_difficulty_or_share_counters(self):
        difficulty = thermal._extract_reject_rate(
            {"Difficulty Accepted": 90, "Difficulty Rejected": 10, "Difficulty Stale": 0}
        )
        shares = thermal._extract_reject_rate({"Accepted": 3, "Rejected": 1, "Stale": 1})
        self.assertAlmostEqual(difficulty, 10.0)
        self.assertAlmostEqual(shares, 20.0)

    def test_thermal_fan_and_network_faults_are_explained(self):
        health = thermal.evaluate_health(
            _healthy_stats(
                temp_max=85,
                fan_rpms=[1400, 0, 1390, 1410],
                fan_error=1,
                rssi=-82,
                ping_ms=1200,
                net_failures_total=5,
            )
        )
        codes = {issue["code"] for issue in health["issues"]}
        self.assertEqual(health["status"], "CRITICAL")
        self.assertEqual(health["score"], 0)
        self.assertTrue(
            {"temperature", "fan_error", "fan_stopped", "rssi", "latency", "network_failures"}.issubset(codes)
        )

    def test_explicit_baseline_detects_performance_drift(self):
        warning = thermal.evaluate_health(_healthy_stats(hashrate=80), baseline_hashrate=100)
        critical = thermal.evaluate_health(_healthy_stats(hashrate=60), baseline_hashrate=100)
        self.assertEqual(warning["status"], "WARN")
        self.assertAlmostEqual(warning["hashrate_drop_pct"], 20)
        self.assertEqual(critical["status"], "CRITICAL")
        self.assertAlmostEqual(critical["hashrate_drop_pct"], 40)

    def test_tracker_learns_baseline_and_resets_on_mode_change(self):
        tracker = thermal.HealthTracker(window=6, minimum_samples=3)
        for value in (100, 102, 98):
            health = tracker.evaluate(_healthy_stats(hashrate=value))
            self.assertIsNone(health["baseline_hashrate"])

        drift = tracker.evaluate(_healthy_stats(hashrate=60))
        self.assertEqual(drift["status"], "CRITICAL")
        self.assertAlmostEqual(drift["baseline_hashrate"], 100)

        reset = tracker.evaluate(_healthy_stats(hashrate=60, workmode=2))
        self.assertIsNone(reset["baseline_hashrate"])
        self.assertNotIn("hashrate_drift", {issue["code"] for issue in reset["issues"]})

    def test_tracker_baseline_does_not_learn_gradual_degradation(self):
        tracker = thermal.HealthTracker(window=4, minimum_samples=3)
        for value in (100, 100, 100):
            tracker.evaluate(_healthy_stats(hashrate=value))

        for value in (95, 90, 86):
            tracker.evaluate(_healthy_stats(hashrate=value))
        drift = tracker.evaluate(_healthy_stats(hashrate=80))

        self.assertAlmostEqual(drift["baseline_hashrate"], 100)
        self.assertAlmostEqual(drift["hashrate_drop_pct"], 20)
        self.assertEqual(drift["status"], "WARN")

    def test_tracker_expands_window_to_fit_required_calibration_samples(self):
        tracker = thermal.HealthTracker(window=2, minimum_samples=4)
        for value in (98, 99, 100, 101):
            tracker.evaluate(_healthy_stats(hashrate=value))

        health = tracker.evaluate(_healthy_stats(hashrate=100))

        self.assertEqual(tracker.samples.maxlen, 4)
        self.assertAlmostEqual(health["baseline_hashrate"], 99.5)

    def test_tracker_relearns_after_device_reboot_in_the_same_mode(self):
        tracker = thermal.HealthTracker()
        for uptime in (100, 105, 110, 115):
            tracker.evaluate(_healthy_stats(hashrate=100, uptime=uptime))
        self.assertEqual(tracker.baseline_hashrate, 100)
        health = tracker.evaluate(_healthy_stats(hashrate=10, uptime=1))
        self.assertIsNone(health["baseline_hashrate"])
        self.assertNotIn("hashrate_drift", {issue["code"] for issue in health["issues"]})
        self.assertEqual(list(tracker.samples), [10])

    def test_invalid_reject_percentage_falls_back_to_valid_counters(self):
        for value in (None, "not a number", "nan", "inf", -1, 101):
            with self.subTest(value=value):
                self.assertAlmostEqual(
                    thermal._extract_reject_rate({"Pool Rejected%": value, "Accepted": 9, "Rejected": 1}),
                    10,
                )

    def test_nonfinite_hashrate_cannot_poison_the_baseline(self):
        tracker = thermal.HealthTracker()
        for value in (float("inf"), float("nan")):
            tracker.evaluate(_healthy_stats(hashrate=value))
        self.assertEqual(list(tracker.samples), [])

    def test_watch_marks_missing_telemetry_and_recovers(self):
        class Miner:
            host = "miner"

            def cmd(self, command):
                return {"VERSION": [{"PROD": "Avalon Nano3s"}]}

            def parse_stats(self, *args, **kwargs):
                return next(self.snapshots)

        class Args:
            interval = 1
            log_jsonl = "watch.jsonl"

        miner = Miner()
        miner.snapshots = iter([_healthy_stats(), {}, _healthy_stats(hashrate=42)])
        output = io.StringIO()
        with patch("thermal.time.sleep", side_effect=[None, None, KeyboardInterrupt]), patch(
            "thermal.append_jsonl"
        ) as log, redirect_stdout(output):
            self.assertTrue(thermal.do_watch(miner, Args))
        self.assertIn("UNAVAILABLE (no current stats)", output.getvalue())
        self.assertIn("42.0 TH/s", output.getvalue())
        self.assertEqual(
            [call.args[1]["event"] for call in log.call_args_list],
            ["health", "watch_unavailable", "health"],
        )

    def test_health_event_is_structured_json(self):
        stats = _healthy_stats()
        health = thermal.evaluate_health(stats)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "health.jsonl")
            thermal.append_health_event(path, "miner", "status", health, stats)
            with open(path, encoding="utf-8") as handle:
                event = json.loads(handle.read())
        self.assertEqual(event["event"], "health")
        self.assertEqual(event["health_status"], "OK")
        self.assertEqual(event["health_score"], 100)
        self.assertEqual(event["issue_codes"], [])
        self.assertEqual(event["reject_rate"], 1.0)
        self.assertEqual(event["device_dh_percent"], 20.0)


if __name__ == "__main__":
    unittest.main()
