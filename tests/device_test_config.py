"""Configuration helpers for device integration tests."""

import os
import re

DEFAULT_HOSTS = ["192.168.130.132", "192.168.130.83"]


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

    return hosts or list(DEFAULT_HOSTS)


HOSTS = load_hosts()
PORT = int(os.getenv("TK_PORT", "4028"))
TIMEOUT = int(os.getenv("TK_TIMEOUT", "5"))

SKIP_OFFLINE = os.getenv("TK_SKIP_OFFLINE", "").lower() in {"1", "true", "yes"}
ALLOW_WRITE = os.getenv("TK_ALLOW_WRITE", "").lower() in {"1", "true", "yes"}
