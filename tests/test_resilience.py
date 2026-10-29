"""Tests for retry behavior, preflight, logging, and secret redaction."""

import io
import json
import os
import sys
import tempfile
import threading
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

    def test_failed_socket_is_closed_before_retry_backoff(self):
        failed = _ScriptedSocket([b'invalid\x00'])
        healthy = _ScriptedSocket([b'{"STATUS":[]}\x00'])

        def check_closed(_delay):
            self.assertTrue(failed.closed)

        with patch("thermal.socket.socket", side_effect=[failed, healthy]), patch(
            "thermal.time.sleep", side_effect=check_closed
        ) as sleep_mock:
            result = thermal.Miner("miner", retries=1, jitter_pct=0).cmd("version")
        self.assertEqual(result, {"STATUS": []})
        sleep_mock.assert_called_once()
        self.assertTrue(healthy.closed)

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

    def test_high_retry_index_stays_bounded(self):
        miner = thermal.Miner("miner", jitter_pct=0)
        self.assertEqual(miner._retry_delay_seconds(100000), 5)


class _ScriptedSocket(_SocketStub):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = iter(chunks)
        self.closed = False
        self.timeouts = []

    def recv(self, _size):
        chunk = next(self.chunks, b"")
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self):
        self.closed = True


class ApiFramingTests(unittest.TestCase):
    def test_fragmented_response_stops_at_first_null(self):
        sock = _ScriptedSocket([b'{"STAT', b'US":[]}\x00trailing bytes'])
        with patch("thermal.socket.socket", return_value=sock):
            self.assertEqual(thermal.Miner("miner").cmd("version"), {"STATUS": []})
        self.assertTrue(sock.closed)

    def test_complete_json_without_null_is_accepted_on_eof_or_timeout(self):
        for end in (b"", thermal.socket.timeout()):
            with self.subTest(end=end):
                sock = _ScriptedSocket([b'{"STATUS":[]}', end])
                with patch("thermal.socket.socket", return_value=sock):
                    self.assertEqual(thermal.Miner("miner").cmd("version"), {"STATUS": []})
                self.assertTrue(sock.closed)

    def test_non_object_and_truncated_responses_are_retryable_for_reads(self):
        for payload in (b'[]\x00', b'null\x00', b'42\x00', b'{"STATUS":'):
            with self.subTest(payload=payload):
                failed = _ScriptedSocket([payload])
                healthy = _ScriptedSocket([b'{"STATUS":[]}\x00'])
                with patch("thermal.socket.socket", side_effect=[failed, healthy]), patch("thermal.time.sleep"):
                    result = thermal.Miner("miner", retries=1).cmd("version")
                self.assertEqual(result, {"STATUS": []})
                self.assertTrue(failed.closed)
                self.assertTrue(healthy.closed)

    def test_partial_mutating_response_is_not_replayed(self):
        sock = _ScriptedSocket([b'{"STATUS":', thermal.socket.timeout()])
        with patch("thermal.socket.socket", return_value=sock) as create, redirect_stderr(io.StringIO()):
            result = thermal.Miner("miner", retries=3).ascset("0,reboot,1")
        self.assertIsNone(result)
        self.assertEqual(create.call_count, 1)
        self.assertTrue(sock.closed)

    def test_continuous_data_cannot_extend_response_deadline(self):
        sock = _ScriptedSocket([b'{"STATUS":', b'[]'])
        with patch("thermal.socket.socket", return_value=sock), patch(
            "thermal.time.monotonic", side_effect=[0, 0, 0.25, 0.5, 1.1]
        ), redirect_stderr(io.StringIO()):
            result = thermal.Miner("miner", timeout=1).cmd("version")
        self.assertIsNone(result)
        self.assertTrue(sock.closed)
        self.assertEqual(sock.timeouts, [1, 1, 0.75, 0.5])

    def test_response_size_limit_closes_socket(self):
        sock = _ScriptedSocket([b'{"large":"', b'01234567890123456789'])
        with patch("thermal.socket.socket", return_value=sock), patch(
            "thermal.MAX_API_RESPONSE_BYTES", 16
        ), redirect_stderr(io.StringIO()):
            result = thermal.Miner("miner").cmd("version")
        self.assertIsNone(result)
        self.assertTrue(sock.closed)


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

    def test_preflight_failure_is_isolated_to_the_affected_host(self):
        class UnreliableMiner(_PreflightMiner):
            def cmd(self, command, param=None):
                if command == "version" and self.host == "raises":
                    raise ValueError("bad response")
                if command == "version" and self.host == "empty_entry":
                    return {"VERSION": [{}]}
                if command == "version" and self.host == "api_error":
                    return {"VERSION": [{"PROD": "Avalon Q"}], "STATUS": [{"STATUS": "E"}]}
                return super().cmd(command, param)

        _PreflightMiner.executed = []
        out = io.StringIO()
        argv = ["thermal.py", "-H", "good,raises,empty_entry,api_error", "fan", "80"]
        with patch.object(sys, "argv", argv), patch("thermal.Miner", UnreliableMiner), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(thermal.main(), 1)
        self.assertEqual(_PreflightMiner.executed, [("good", "0,fan-spd,80")])
        for host in ("raises", "empty_entry", "api_error"):
            self.assertIn(f"[{host}]\nskipped: preflight failed", out.getvalue())

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

    def test_unknown_get_suffix_does_not_bypass_preflight_or_allow_retries(self):
        for param in ("0,reboot,get", "0,unknown,get", "0,workmode,set,1,get", "0,loop,get,extra"):
            with self.subTest(param=param):
                self.assertFalse(thermal.is_retry_safe_command("ascset", param))
        for param in (
            "0,help", "0,voltage", "0,workmode,get", "0,worklevel,get",
            "0,work_mode_lvl,get", "0,loop,get", "0,time,get",
        ):
            with self.subTest(param=param):
                self.assertTrue(thermal.is_retry_safe_command("ascset", param))


class FleetResultTests(unittest.TestCase):
    def run_cli(self, *arguments):
        out = io.StringIO()
        err = io.StringIO()
        with patch.object(sys, "argv", ["thermal.py"] + list(arguments)), patch(
            "thermal.Miner", _PreflightMiner
        ), redirect_stdout(out), redirect_stderr(err):
            result = thermal.main()
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
        return result, out.getvalue(), err.getvalue()

    def test_parallel_output_is_captured_per_host_and_streams_are_restored(self):
        barrier = threading.Barrier(2)
        alpha_completed = threading.Event()

        def logged(_path, event):
            if event.get("event") == "run" and event.get("host") == "alpha":
                alpha_completed.set()

        def command(miner, _args):
            print(f"{miner.host} begin")
            barrier.wait(timeout=5)
            if miner.host == "beta":
                self.assertTrue(alpha_completed.wait(timeout=5))
            print(f"{miner.host} diagnostic", file=sys.stderr)
            print(f"{miner.host} end")
            return True

        with patch("thermal.do_timezone", side_effect=command), patch("thermal.append_jsonl", side_effect=logged):
            code, output, errors = self.run_cli("-H", "alpha,beta", "timezone")
        self.assertEqual(errors, "")
        self.assertEqual(
            output,
            "[alpha]\nalpha begin\nalpha end\nalpha diagnostic\n\n"
            "[beta]\nbeta begin\nbeta end\nbeta diagnostic\n\n",
        )
        self.assertEqual(code, 0)

    def test_failure_exit_codes_cover_single_fleet_and_preflight(self):
        for arguments in (
            ("-H", "offline", "status"),
            ("-H", "alpha,offline", "status"),
            ("-H", "alpha,offline", "fan", "80"),
            ("-H", "alpha", "fan", "invalid"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments)[0], 1)
        self.assertEqual(self.run_cli("-H", "alpha,beta", "status")[0], 0)

    def test_failed_handler_and_exception_are_propagated_in_fleet(self):
        for outcome in (False, ValueError("invalid payload")):
            with self.subTest(outcome=outcome):
                with patch("thermal.do_timezone", side_effect=[True, outcome]):
                    self.assertEqual(self.run_cli("-H", "alpha,beta", "timezone")[0], 1)

    def test_duplicate_hosts_are_only_mutated_once(self):
        _PreflightMiner.executed = []
        code, _, _ = self.run_cli("-H", "alpha,beta,alpha", "fan", "80")
        self.assertCountEqual(_PreflightMiner.executed, [("alpha", "0,fan-spd,80"), ("beta", "0,fan-spd,80")])
        self.assertEqual(code, 0)

    def test_missing_stats_is_not_a_successful_status_collection(self):
        with patch.object(_PreflightMiner, "parse_stats", return_value={}):
            self.assertEqual(self.run_cli("-H", "alpha", "status")[0], 1)
            self.assertEqual(self.run_cli("-H", "alpha,beta", "status")[0], 1)

    def test_raw_api_error_is_printed_but_not_reported_as_success(self):
        for status in ("E", "F"):
            with self.subTest(status=status), redirect_stdout(io.StringIO()):
                self.assertFalse(thermal.print_json_result({"STATUS": [{"STATUS": status}]}))

    def test_malformed_response_status_cannot_report_success(self):
        for statuses in (None, [], {}, [None], [{}], [{"STATUS": []}], [{"STATUS": "W"}],
                         [{"STATUS": "S"}, {"STATUS": "E"}]):
            with self.subTest(statuses=statuses), redirect_stdout(io.StringIO()):
                self.assertFalse(thermal.print_json_result({"STATUS": statuses}))
        with redirect_stdout(io.StringIO()):
            self.assertTrue(thermal.print_json_result({"SUMMARY": [{"Elapsed": 1}]}))
            self.assertFalse(thermal.print_json_result({"SUMMARY": [{"Elapsed": 1}]}, require_status=True))
            self.assertTrue(thermal.print_json_result({"STATUS": [{"STATUS": "I"}]}))

    def test_raw_mutation_and_ascset_require_acknowledgment(self):
        class Miner:
            def cmd(self, *_args):
                return {"result": "unconfirmed"}

            def ascset(self, *_args):
                return {"result": "unconfirmed"}

        class RawArgs:
            command = "restart"
            param = None

        class AscsetArgs:
            param = "0,reboot,1"

        with redirect_stdout(io.StringIO()):
            self.assertFalse(thermal.do_raw(Miner(), RawArgs))
            self.assertFalse(thermal.do_ascset(Miner(), AscsetArgs))

    def test_command_handlers_reject_later_error_or_malformed_status(self):
        for extra in ({"STATUS": "E"}, {}, None, {"STATUS": []}):
            with self.subTest(extra=extra):
                result = {"STATUS": [{"STATUS": "S", "Msg": "success"}, extra]}
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    self.assertFalse(thermal.check_result(result, "completed"))
                    self.assertFalse(thermal._print_info_status(result, "completed"))
                self.assertNotIn("ok:", out.getvalue())
                self.assertIn("did not confirm", err.getvalue())

    def test_web_auth_does_not_accept_a_later_error_status(self):
        miner = thermal.Miner("miner")
        credentials = {"auth": "a", "verify": "b", "cookie": "c"}
        with patch.object(miner, "ascset", return_value={"STATUS": [{"STATUS": "S"}, {"STATUS": "E"}]}), \
                patch("thermal.urllib.request.urlopen") as request:
            self.assertIsNone(miner.web_auth("test", credentials))
        request.assert_not_called()

    def test_ascset_help_rejects_error_status(self):
        for statuses in ([{"STATUS": "E", "Msg": "permission denied"}],
                         [{"STATUS": "I", "Msg": "fan-spd"}, {"STATUS": "E"}]):
            with self.subTest(statuses=statuses):
                miner = _PreflightMiner("miner")
                with patch.object(miner, "ascset", return_value={"STATUS": statuses}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    self.assertFalse(thermal.do_help_ascset(miner, object()))
                self.assertNotIn("Available ascset commands", out.getvalue())

    def test_cli_rejects_invalid_port_and_poll_interval_before_connections(self):
        for arguments in (("--port", "0", "status"), ("--port", "65536", "status"), ("watch", "-i", "0")):
            with self.subTest(arguments=arguments):
                with patch.object(sys, "argv", ["thermal.py", "-H", "alpha"] + list(arguments)), patch(
                    "thermal.Miner"
                ) as miner, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        thermal.main()
                self.assertEqual(raised.exception.code, 2)
                miner.assert_not_called()


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
