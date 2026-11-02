Device integration tests

These tests exercise real miners over the CGMiner API. They are meant to
compare Avalon Mini 3, Avalon Nano 3S, and Avalon Q (when available) behavior
and highlight gaps needed for premium support.

Quick start

- Run all read-only tests (unit + property + device):
  - python -m unittest tests.test_cli tests.test_cli_params tests.test_health tests.test_password tests.test_resilience tests.test_device_test_config tests.test_parse_properties tests.test_q_support tests.test_devices

- Full suite shortcut:
  - ./scripts/test.sh
- Offline-only tests (no device integration imports or LAN access):
  - ./scripts/test.sh --offline
- Configure test targets with `TK_HOSTS` or `TK_HOSTS_FILE`.
  Without configured targets, device integration tests skip.

Environment variables

- TK_HOSTS: Comma/space-separated host list (overrides defaults)
- TK_HOSTS_FILE: Path to file with one host per line (appends)
- TK_PORT: CGMiner port (default 4028)
- TK_TIMEOUT: Socket timeout in seconds (default 5)
- TK_SKIP_OFFLINE: Offline-host policy for device tests (default: skip)
  - Unset / `1` / `true` / `yes`: skip hosts that do not respond
  - `0` / `false` / `no`: fail tests when a configured host is offline

Device write tests (disabled)

The write smoke test always skips, including with `TK_ALLOW_WRITE=1`.
Observed fan speed and frequency do not preserve fan automation or individual
PLL settings when written back. Device write testing requires a verified,
device-specific configuration snapshot and restore procedure.

JSON report emitter

Generate a structured snapshot for comparing Mini 3 vs Nano 3S (and Q):

- python tests/report_devices.py --pretty
- python tests/report_devices.py --pretty --out reports/device_report.json

Avalon Q validation

- Run `tests.test_q_support` for hardware-free Q parsing and command checks.
- Set `TK_HOSTS` to the Q address when running the read-only device suite.
