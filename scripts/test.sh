#!/usr/bin/env bash
set -euo pipefail

test_modules=(
  tests.test_cli
  tests.test_cli_params
  tests.test_health
  tests.test_password
  tests.test_resilience
  tests.test_device_test_config
  tests.test_parse_properties
  tests.test_q_support
  tests.test_telemetry
)

if [[ $# -eq 0 ]]; then
  test_modules+=(tests.test_devices)
elif [[ $# -ne 1 || "$1" != "--offline" ]]; then
  echo "usage: $0 [--offline]" >&2
  exit 2
fi

python3 -m unittest "${test_modules[@]}"
