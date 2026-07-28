#!/usr/bin/env bash
set -euo pipefail

python3 -m unittest \
  tests.test_cli \
  tests.test_cli_params \
  tests.test_health \
  tests.test_password \
  tests.test_resilience \
  tests.test_device_test_config \
  tests.test_parse_properties \
  tests.test_q_support \
  tests.test_devices
