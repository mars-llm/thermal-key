"""Tests for device integration test environment configuration."""

import ast
import importlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import device_test_config


class DeviceTestConfigTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(device_test_config)

    def test_skip_offline_defaults_true_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TK_SKIP_OFFLINE", None)
            os.environ.pop("TK_ALLOW_WRITE", None)
            config = importlib.reload(device_test_config)

        self.assertTrue(config.SKIP_OFFLINE)
        self.assertFalse(config.ALLOW_WRITE)

    def test_skip_offline_false_values_override_default(self):
        false_values = ["0", "false", "no", "FALSE", "No"]
        for value in false_values:
            with self.subTest(value=value):
                with patch.dict(os.environ, {"TK_SKIP_OFFLINE": value}, clear=False):
                    config = importlib.reload(device_test_config)
                self.assertFalse(config.SKIP_OFFLINE)

    def test_skip_offline_true_values_explicitly_enable(self):
        true_values = ["1", "true", "yes", "TRUE", "Yes"]
        for value in true_values:
            with self.subTest(value=value):
                with patch.dict(os.environ, {"TK_SKIP_OFFLINE": value}, clear=False):
                    config = importlib.reload(device_test_config)
                self.assertTrue(config.SKIP_OFFLINE)

    def test_write_smoke_contains_only_an_unconditional_skip(self):
        source = Path(__file__).with_name("test_devices.py").read_text(encoding="utf-8")
        write_tests = next(
            node for node in ast.parse(source).body
            if isinstance(node, ast.ClassDef) and node.name == "DeviceWriteSmokeTests"
        )
        self.assertEqual(len(write_tests.body), 1)
        method = write_tests.body[0]
        self.assertIsInstance(method, ast.FunctionDef)
        self.assertEqual(method.name, "test_device_writes_require_verified_snapshot_and_restore")
        self.assertEqual(len(method.body), 1)
        call = method.body[0].value
        self.assertIsInstance(call, ast.Call)
        self.assertEqual(call.func.value.id, "self")
        self.assertEqual(call.func.attr, "skipTest")
        self.assertIn("TK_ALLOW_WRITE cannot enable", call.args[0].value)

    @unittest.skipUnless(shutil.which("bash"), "Bash is needed to test scripts/test.sh")
    def test_offline_script_selects_all_unit_modules_without_device_integration(self):
        tests_dir = Path(__file__).resolve().parent
        script = tests_dir.parent / "scripts" / "test.sh"
        with tempfile.TemporaryDirectory() as directory:
            interpreter = Path(directory) / "python3"
            interpreter.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n', encoding="utf-8")
            interpreter.chmod(0o755)
            environment = dict(os.environ, PATH=directory + os.pathsep + os.environ.get("PATH", ""))
            result = subprocess.run(
                ["bash", str(script), "--offline"], env=environment,
                capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.splitlines()
        self.assertEqual(arguments[:2], ["-m", "unittest"])
        expected = {
            "tests." + path.stem for path in tests_dir.glob("test_*.py")
            if path.name != "test_devices.py"
        }
        self.assertCountEqual(arguments[2:], expected)


if __name__ == "__main__":
    unittest.main()
