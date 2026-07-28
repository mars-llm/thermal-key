"""Tests for retry behavior, preflight, logging, and secret redaction."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import thermal


class _SocketStub:
    def __init__(self, fail_connect=False, connect_error=None):
        self.fail_connect = fail_connect
        self.connect_error = connect_error
        self._recv_count = 0

    def settimeout(self, _timeout):
        return None

    def connect(self, _addr):
        if self.connect_error:
            raise self.connect_error
        if self.fail_connect:
            raise ConnectionRefusedError("refused")

    def sendall(self, _payload):
        return None

    def recv(self, _size):
        if self._recv_count == 0:
            self._recv_count += 1
            return b'{"STATUS":[{"STATUS":"S"}]}\x00'
        return b""

    def close(self):
        return None


class RetryBehaviorTests(unittest.TestCase):
    def test_retry_then_success(self):
        miner = thermal.Miner("192.168.0.10", retries=1, backoff_ms=100, jitter_pct=0)
        with patch("thermal.socket.socket", side_effect=[_SocketStub(fail_connect=True), _SocketStub()]), patch(
            "thermal.time.sleep"
        ) as sleep_mock:
            result = miner.cmd("version")
        self.assertIsNotNone(result)
        sleep_mock.assert_called_once_with(0.1)

    def test_retry_then_success_for_generic_network_oserror(self):
        miner = thermal.Miner("192.168.0.10", retries=1, backoff_ms=100, jitter_pct=0)
        sockets = [
            _SocketStub(connect_error=OSError("network unreachable")),
            _SocketStub(),
        ]
        with patch("thermal.socket.socket", side_effect=sockets), patch("thermal.time.sleep") as sleep_mock:
            result = miner.cmd("version")
        self.assertIsNotNone(result)
        sleep_mock.assert_called_once_with(0.1)

    def test_mutating_command_is_not_replayed(self):
        miner = thermal.Miner("192.168.0.10", retries=3, backoff_ms=100, jitter_pct=0)
        with patch("thermal.socket.socket", return_value=_SocketStub(fail_connect=True)) as socket_mock, patch(
            "thermal.time.sleep"
        ) as sleep_mock, redirect_stderr(io.StringIO()):
            result = miner.ascset("0,reboot,1")
        self.assertIsNone(result)
        self.assertEqual(socket_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_retry_delay_bounds_with_jitter(self):
        miner = thermal.Miner("192.168.0.10", retries=2, backoff_ms=100, jitter_pct=20)
        for _ in range(200):
            delay = miner._retry_delay_seconds(0)
            self.assertGreaterEqual(delay, 0.08)
            self.assertLessEqual(delay, 0.12)


class _PreflightMiner:
    executed = []

    def __init__(self, host, port=4028, timeout=10, **_kwargs):
        self.host = host
        self.port = port
        self.timeout = timeout

    def cmd(self, command, param=None):
        if command == "version":
            if "offline" in self.host:
                return None
            if "empty" in self.host:
                return {"VERSION": []}
            return {
                "VERSION": [
                    {"PROD": "Avalon Mini3", "DNA": "0201000046d3803b", "MAC": "e0e1a93ed589", "LVERSION": "fw", "CGMiner": "4.11.1"}
                ]
            }
        return {"STATUS": [{"STATUS": "S"}]}

    def ascset(self, param):
        _PreflightMiner.executed.append((self.host, param))
        return {"STATUS": [{"STATUS": "S", "Msg": "ASC 0 set OK"}]}

    def parse_stats(self, device_key=None, version_entry=None):
        return {
            "hashrate": 40.0,
            "hashrate_max": 41.0,
            "uptime": 60,
            "temp": 60,
            "temp_max": 70,
            "fan_rpm": 3000,
            "fan_pct": 80,
            "freq": 500,
            "voltage": 2100,
            "power_in": 1200,
            "power_out": 700,
            "workmode": 1,
            "worklevel": 0,
            "hw_errors": 0,
            "reject_rate": 1.0,
            "device_dh_percent": 1.0,
            "dna": "0201000046d3803b",
        }


class PreflightTests(unittest.TestCase):
    def test_preflight_skips_offline_hosts_for_mutating_commands(self):
        _PreflightMiner.executed = []
        out = io.StringIO()
        err = io.StringIO()
        argv = ["thermal.py", "-H", "192.168.0.10,offline", "fan", "80"]
        with patch.object(sys, "argv", argv), patch("thermal.Miner", _PreflightMiner), redirect_stdout(out), redirect_stderr(err):
            thermal.main()
        output = out.getvalue()
        self.assertIn("warning: preflight failed", output)
        self.assertIn("[offline]\nskipped: preflight failed", output)
        self.assertEqual(_PreflightMiner.executed, [("192.168.0.10", "0,fan-spd,80")])

    def test_preflight_rejects_empty_version_list(self):
        _PreflightMiner.executed = []
        out = io.StringIO()
        argv = ["thermal.py", "-H", "192.168.0.10,empty", "fan", "80"]
        with patch.object(sys, "argv", argv), patch("thermal.Miner", _PreflightMiner), redirect_stdout(out), redirect_stderr(
            io.StringIO()
        ):
            thermal.main()
        self.assertIn("[empty]\nskipped: preflight failed", out.getvalue())
        self.assertEqual(_PreflightMiner.executed, [("192.168.0.10", "0,fan-spd,80")])

    def test_raw_and_ascset_mutation_classification(self):
        class RawRead:
            cmd = "raw"
            command = "summary"
            param = None

        class RawWrite:
            cmd = "raw"
            command = "restart"
            param = None

        class AscsetRead:
            cmd = "ascset"
            param = "0,loop,get"

        class AscsetWrite:
            cmd = "ascset"
            param = "0,fan-spd,80"

        self.assertFalse(thermal.is_mutating_command(RawRead))
        self.assertTrue(thermal.is_mutating_command(RawWrite))
        self.assertFalse(thermal.is_mutating_command(AscsetRead))
        self.assertTrue(thermal.is_mutating_command(AscsetWrite))
        for command in ("coin", "config", "devdetails", "edevs", "estats"):
            with self.subTest(command=command):
                self.assertTrue(thermal.is_retry_safe_command(command))


class _LoggingMiner:
    def __init__(self, host, port=4028, timeout=10, **_kwargs):
        self.host = host
        self.port = port
        self.timeout = timeout

    def cmd(self, command, param=None):
        if command == "version":
            return {
                "VERSION": [
                    {"PROD": "Avalon Mini3", "DNA": "0201000046d3803b", "MAC": "e0e1a93ed589", "LVERSION": "fw", "CGMiner": "4.11.1"}
                ]
            }
        if command == "stats":
            mm = "GHSavg[40000] GHSmm[41000] Elapsed[120] HBTemp[60] TMax[70] Fan1[2500] FanR[80%] SF0[500 520 540 560] ATA1[700-70-2100-492-20] PS[0 1200 2050 34 700 2050 737] WORKMODE[1] WORKLVL[0] HW[0] DH[1.0%] DNA[0201000046d3803b]"
            return {"STATS": [{"MM ID0": mm}]}
        return {"STATUS": [{"STATUS": "S"}]}

    def ascset(self, param):
        return {"STATUS": [{"STATUS": "S", "Msg": "ASC 0 set OK"}]}

    def parse_stats(self, device_key=None, version_entry=None):
        return thermal.parse_mm_id0(
            "GHSavg[40000] GHSmm[41000] Elapsed[120] HBTemp[60] TMax[70] Fan1[2500] FanR[80%] SF0[500 520 540 560] ATA1[700-70-2100-492-20] PS[0 1200 2050 34 700 2050 737] WORKMODE[1] WORKLVL[0] HW[0] DH[1.0%] DNA[0201000046d3803b]"
        )


class LoggingAndSecretsTests(unittest.TestCase):
    def test_jsonl_logging_contains_required_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "run.jsonl")
            argv = ["thermal.py", "--log-jsonl", log_path, "-H", "192.168.0.10", "status"]
            with patch.object(sys, "argv", argv), patch("thermal.Miner", _LoggingMiner), redirect_stdout(io.StringIO()):
                thermal.main()
            with open(log_path, encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle if line.strip()]
            self.assertTrue(lines)
            last = lines[-1]
            for key in ("timestamp", "schema_version", "host", "command", "status"):
                self.assertIn(key, last)
            self.assertEqual(last["schema_version"], "1")

    def test_single_host_failure_is_logged_as_failed(self):
        class _OfflineMiner:
            def __init__(self, host, port=4028, **_kwargs):
                self.host = host

            def cmd(self, command, param=None):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "run.jsonl")
            argv = ["thermal.py", "--log-jsonl", log_path, "-H", "offline", "status"]
            with patch.object(sys, "argv", argv), patch("thermal.Miner", _OfflineMiner), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(io.StringIO()):
                thermal.main()
            with open(log_path, encoding="utf-8") as handle:
                event = json.loads(handle.read())
        self.assertEqual(event["status"], "failed")

    def test_empty_version_status_reports_error_without_crashing(self):
        class _EmptyVersionMiner:
            host = "192.168.0.10"

            def cmd(self, command, param=None):
                return {"VERSION": []}

        err = io.StringIO()
        with redirect_stderr(err):
            result = thermal.do_status(_EmptyVersionMiner(), object())
        self.assertFalse(result)
        self.assertIn("failed to get version", err.getvalue())

    def test_cli_rejects_nonpositive_parallelism(self):
        err = io.StringIO()
        argv = ["thermal.py", "-j", "0", "-H", "host-a,host-b", "status"]
        with patch.object(sys, "argv", argv), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                thermal.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("--parallel must be >= 1", err.getvalue())

    def test_auth_output_is_redacted_by_default(self):
        class _AuthMiner:
            host = "192.168.0.10"

            def compute_auth(self, _password):
                return {
                    "dna": "0201000046d3803b",
                    "auth": "aaaaaaaa",
                    "verify": "ff0000ee1234567890abcdef12345678",
                    "cookie": "aaaaaaaa1234567890abcdef12345678",
                }

            def web_auth(self, _password):
                return "aaaaaaaa1234567890abcdef12345678"

        class Args:
            password = "secret"
            show_secrets = False

        out = io.StringIO()
        with redirect_stdout(out):
            thermal.do_auth(_AuthMiner(), Args)
        output = out.getvalue()
        self.assertIn("Cookie: aaaa...5678", output)
        self.assertIn("<redacted>", output)
        self.assertNotIn("aaaaaaaa1234567890abcdef12345678", output)

    def test_auth_output_shows_full_values_with_flag(self):
        class _AuthMiner:
            host = "192.168.0.10"

            def compute_auth(self, _password):
                return {
                    "dna": "0201000046d3803b",
                    "auth": "aaaaaaaa",
                    "verify": "ff0000ee1234567890abcdef12345678",
                    "cookie": "aaaaaaaa1234567890abcdef12345678",
                }

            def web_auth(self, _password):
                return "aaaaaaaa1234567890abcdef12345678"

        class Args:
            password = "secret"
            show_secrets = True

        out = io.StringIO()
        with redirect_stdout(out):
            thermal.do_auth(_AuthMiner(), Args)
        output = out.getvalue()
        self.assertIn("Cookie: aaaaaaaa1234567890abcdef12345678", output)
        self.assertNotIn("<redacted>", output)


if __name__ == "__main__":
    unittest.main()
