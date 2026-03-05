"""Tests for device integration test environment configuration."""

import importlib
import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
