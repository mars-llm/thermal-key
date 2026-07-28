"""Unit tests for bounded password recovery scheduling."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import password


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
