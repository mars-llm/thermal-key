"""Unit tests for bounded password recovery scheduling."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import password


class PasswordDeviceInfoTests(unittest.TestCase):
    def test_spaced_uppercase_auth_and_q_dna_use_shared_client(self):
        with patch("password.urllib.request.urlopen") as request, patch("thermal.Miner") as miner:
            request.return_value.read.return_value = b'callback({ "auth" : "AABBCCDD" });'
            miner.return_value.get_dna.return_value = "abcd"
            self.assertEqual(password.fetch_device_info("miner"), ("aabbccdd", "abcd"))
        miner.assert_called_once_with("miner", timeout=5)

    def test_missing_device_dna_reports_a_clear_error(self):
        with patch("password.urllib.request.urlopen") as request, patch("thermal.Miner") as miner:
            request.return_value.read.return_value = b'{"auth":"aabbccdd"}'
            miner.return_value.get_dna.return_value = None
            with self.assertRaisesRegex(ValueError, "Failed to fetch DNA"):
                password.fetch_device_info("miner")

    def test_invalid_auth_is_rejected_before_dna_query(self):
        with patch("password.urllib.request.urlopen") as request, patch("thermal.Miner") as miner:
            request.return_value.read.return_value = b'{"auth":"not an auth hash"}'
            with self.assertRaisesRegex(ValueError, "Could not parse auth"):
                password.fetch_device_info("miner")
            miner.assert_not_called()


class StandalonePasswordTests(unittest.TestCase):
    def run_standalone(self, *arguments):
        with tempfile.TemporaryDirectory() as directory:
            script = os.path.join(directory, "password.py")
            shutil.copyfile(password.__file__, script)
            return subprocess.run(
                [sys.executable, "-I", "-S", script] + list(arguments),
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=20,
            )

    def test_offline_recovery_runs_without_thermal_module(self):
        result = self.run_standalone(
            "--target", password.compute_auth("standalone-test", "abcd"),
            "--dna", "abcd", "--mask", "standalone-test", "--workers", "1", "--batch-size", "1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AUTH HASH MATCH: standalone-test", result.stdout)
        self.assertIn("truncated hash can collide", result.stdout)
        self.assertIn("CANDIDATE CREDENTIALS (not verified)", result.stdout)

    def test_device_recovery_without_thermal_reports_dependency_before_network_access(self):
        result = self.run_standalone("--device", "miner.invalid", "--mask", "test")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--device requires thermal.py beside password.py", result.stdout)
        self.assertIn("--target and --dna", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_utf8_wordlist_fails_without_a_traceback(self):
        with tempfile.NamedTemporaryFile("wb") as wordlist:
            wordlist.write(b"bad\xffcandidate\n")
            wordlist.flush()
            result = self.run_standalone(
                "--target", "aaaaaaaa", "--dna", "abcd", "--wordlist", wordlist.name,
                "--workers", "1", "--batch-size", "1",
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Input error", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class _ImmediateFuture:
    def __init__(self, value):
        self.value = value
        self.cancelled = False

    def result(self):
        return self.value

    def cancel(self):
        self.cancelled = True
        return True


class _ImmediateExecutor:
    last = None

    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.futures = []
        self.shutdown_wait = None
        _ImmediateExecutor.last = self

    def submit(self, function, args):
        future = _ImmediateFuture(function(args))
        self.futures.append(future)
        return future

    def shutdown(self, wait=True):
        self.shutdown_wait = wait


def _ordered_as_completed(futures):
    return iter(list(futures))


class PasswordSchedulingTests(unittest.TestCase):
    def test_batch_reports_exact_checked_count(self):
        dna = b"abcd"
        target = password.compute_auth_fast("match", dna)
        result, checked = password.check_password_batch((["miss", "match", "later"], target, dna))
        self.assertEqual(result, "match")
        self.assertEqual(checked, 2)

    def test_cracker_bounds_in_flight_work_and_cancels_after_match(self):
        seen = 0

        def candidates():
            nonlocal seen
            for candidate in ["match"] + ["miss"] * 9999:
                seen += 1
                yield candidate

        dna = "abcd"
        target = password.compute_auth("match", dna)
        with patch("password.ProcessPoolExecutor", _ImmediateExecutor), patch(
            "password.as_completed", _ordered_as_completed
        ), redirect_stdout(io.StringIO()):
            result = password.crack_generator(
                target,
                dna,
                candidates(),
                total=10000,
                num_workers=1,
                batch_size=100,
            )

        executor = _ImmediateExecutor.last
        self.assertEqual(result, "match")
        self.assertEqual(seen, 200)
        self.assertEqual(len(executor.futures), 2)
        self.assertTrue(executor.futures[1].cancelled)
        self.assertTrue(executor.shutdown_wait)

    def test_cracker_counts_partial_final_batch(self):
        dna = "abcd"
        output = io.StringIO()
        with patch("password.ProcessPoolExecutor", _ImmediateExecutor), patch(
            "password.as_completed", _ordered_as_completed
        ), redirect_stdout(output):
            result = password.crack_generator(
                "ffffffff",
                dna,
                ("miss" for _ in range(250)),
                total=250,
                num_workers=1,
                batch_size=100,
            )
        self.assertIsNone(result)
        self.assertIn("Not found in 250 candidates", output.getvalue())

    def test_wordlist_preserves_spaces_and_rejects_invalid_utf8(self):
        with tempfile.NamedTemporaryFile("wb") as handle:
            handle.write(b" leading\r\ntrailing \n  \n\nlast")
            handle.flush()
            self.assertEqual(list(password.read_wordlist(handle.name)), [" leading", "trailing ", "  ", "last"])
        with tempfile.NamedTemporaryFile("wb") as handle:
            handle.write(b"candidate\xff\n")
            handle.flush()
            with self.assertRaises(UnicodeDecodeError):
                list(password.read_wordlist(handle.name))

    def test_wordlist_recovery_finds_whitespace_candidate(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write(" leading \n")
            handle.flush()
            with patch("password.ProcessPoolExecutor", _ImmediateExecutor), patch(
                "password.as_completed", _ordered_as_completed
            ), redirect_stdout(io.StringIO()) as output:
                result = password.crack_wordlist(
                    password.compute_auth(" leading ", "abcd"), "abcd", handle.name,
                    num_workers=1, batch_size=1,
                )
        self.assertEqual(result, " leading ")
        self.assertIn("truncated hash can collide", output.getvalue())

    def test_hybrid_counts_only_candidates_it_can_generate(self):
        words = ["x" * 16, "y" * 15, "z" * 17]
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write("\n".join(words) + "\n")
            handle.flush()
            with patch("password.crack_generator") as crack:
                password.crack_hybrid("ffffffff", "abcd", handle.name, 4, num_workers=1)
        generated = list(crack.call_args.args[2])
        self.assertEqual(crack.call_args.args[3], len(generated))
        self.assertEqual(len(generated), 3 + len(password.CHARSET_DIGITS + password.CHARSET_SPECIAL_COMMON))

    def test_match_with_zero_elapsed_time_does_not_crash(self):
        with patch("password.ProcessPoolExecutor", _ImmediateExecutor), patch(
            "password.as_completed", _ordered_as_completed
        ), patch("password.time.time", return_value=1), redirect_stdout(io.StringIO()):
            result = password.crack_generator(
                password.compute_auth("match", "abcd"), "abcd", iter(["match"]),
                num_workers=1, batch_size=1,
            )
        self.assertEqual(result, "match")

    def test_cli_rejects_zero_workers(self):
        err = io.StringIO()
        argv = ["password.py", "--workers", "0", "-t", "aaaaaaaa", "--dna", "abcd", "--bruteforce"]
        with patch.object(sys, "argv", argv), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                password.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--workers must be >= 1", err.getvalue())


if __name__ == "__main__":
    unittest.main()
