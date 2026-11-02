"""Configuration helpers for device integration tests."""

import os
import re


def _parse_host_list(text):
    if not text:
        return []
    parts = re.split(r"[\s,]+", text.strip())
    return [p for p in parts if p]


def load_hosts():
    hosts = []

    env_hosts = os.getenv("TK_HOSTS", "").strip()
    if env_hosts:
        hosts.extend(_parse_host_list(env_hosts))

    hosts_file = os.getenv("TK_HOSTS_FILE", "").strip()
    if hosts_file:
        try:
            with open(hosts_file, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        hosts.append(line)
        except OSError:
            pass

    return list(dict.fromkeys(hosts))


def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default

    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return default


HOSTS = load_hosts()
PORT = int(os.getenv("TK_PORT", "4028"))
TIMEOUT = int(os.getenv("TK_TIMEOUT", "5"))

SKIP_OFFLINE = _env_bool("TK_SKIP_OFFLINE", default=True)
ALLOW_WRITE = _env_bool("TK_ALLOW_WRITE", default=False)
