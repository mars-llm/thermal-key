Device integration tests

These tests exercise real miners over the CGMiner API. They are meant to
compare Avalon Mini 3, Avalon Nano 3S, and Avalon Q (when available) behavior
and highlight gaps needed for premium support.

Quick start

- Run all read-only tests (unit + property + device):
  - python -m unittest tests.test_cli tests.test_cli_params tests.test_parse_properties tests.test_q_support tests.test_devices

- Full suite shortcut:
  - ./scripts/test.sh
- Run with default hosts (preconfigured in tests/device_test_config.py):
  - 192.168.130.132 (Mini 3)
  - 192.168.130.83 (Nano 3S)

Environment variables

- TK_HOSTS: Comma/space-separated host list (overrides defaults)
- TK_HOSTS_FILE: Path to file with one host per line (appends)
- TK_PORT: CGMiner port (default 4028)
- TK_TIMEOUT: Socket timeout in seconds (default 5)
- TK_SKIP_OFFLINE: If set, skip hosts that do not respond

Write tests (opt-in)

These modify device settings and should be run only when it is safe.
Tuning is experimental and not recommended for Nano 3S at this time.

- TK_ALLOW_WRITE=1 enables write smoke tests (noop set to current values)
  - python -m unittest tests.test_devices.DeviceWriteSmokeTests
Safety notes

- Write tests only set current values back onto the device.

JSON report emitter

Generate a structured snapshot for comparing Mini 3 vs Nano 3S (and Q):

- python tests/report_devices.py --pretty
- python tests/report_devices.py --pretty --out reports/device_report.json

Avalon Q validation

- Follow docs/avalon-q.md for the current Avalon Q API profile and command checks.
