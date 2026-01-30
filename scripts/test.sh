#!/usr/bin/env bash
set -euo pipefail

python3 -m unittest \
  tests.test_cli \
  tests.test_cli_params \
  tests.test_parse_properties \
  tests.test_devices
