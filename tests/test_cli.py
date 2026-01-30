"""Unit tests for CLI argument handling and output formatting."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import thermal


def _make_mm(
    dna="0201000046d3803b",
    ghsavg="40051.26",
    ghsmm="39978.70",
    elapsed="12345",
    hbtemp="65",
    tmax="76",
    fan1="3000",
    fanr="99%",
    sf0="498 516 537 558",
    ata1="750-75-2109-492-20",
    ps="0 1215 2050 34 694 2050 737",
    workmode="1",
    worklvl="0",
    hw="0",
    dh="2.3%",
):
    parts = [
        f"GHSavg[{ghsavg}]",
        f"GHSmm[{ghsmm}]",
        f"Elapsed[{elapsed}]",
        f"HBTemp[{hbtemp}]",
        f"TMax[{tmax}]",
        f"Fan1[{fan1}]",
        f"FanR[{fanr}]",
        f"SF0[{sf0}]",
        f"ATA1[{ata1}]",
        f"PS[{ps}]",
        f"WORKMODE[{workmode}]",
        f"WORKLVL[{worklvl}]",
        f"HW[{hw}]",
        f"DH[{dh}]",
        f"DNA[{dna}]",
    ]
    return " ".join(parts)


class FakeMiner:
    def __init__(self, host, port=4028, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout

    def cmd(self, command, param=None):
        if "offline" in self.host:
            return None
        if command == "version":
            return {
                "VERSION": [
                    {
                        "CGMiner": "4.11.1",
                        "API": "3.7",
                        "PROD": "Avalon Mini3",
                        "MODEL": "Mini3",
                        "HWTYPE": "M_MM1v1_X1",
                        "SWTYPE": "MM319",
                        "LVERSION": "25022401_cb28ba7",
                        "DNA": "0201000046d3803b",
                        "MAC": "e0e1a93ed589",
                    }
                ]
            }
        if command == "summary":
            return {"SUMMARY": [{"MHS av": 40000000.0, "Elapsed": 12345}]}
        if command == "stats":
            return {"STATS": [{"MM ID0": _make_mm()}]}
        if command == "pools":
            return {
                "POOLS": [
                    {
                        "POOL": 0,
                        "Status": "Alive",
                        "Stratum Active": True,
                        "Accepted": 123,
                        "URL": "stratum+tcp://example",
                        "User": "worker",
                    }
                ]
            }
        if command in {"switchpool", "enablepool", "disablepool"}:
            return {"STATUS": [{"STATUS": "S"}]}
        return {"STATUS": [{"STATUS": "S"}]}

    def ascset(self, param):
        return {"STATUS": [{"STATUS": "S", "Msg": "ASC 0 set OK"}]}

    def parse_stats(self):
        return thermal.parse_mm_id0(_make_mm())


class CliUnitTests(unittest.TestCase):
    def test_parse_hosts_file(self):
        with tempfile.NamedTemporaryFile("w+", delete=True) as handle:
            handle.write("192.168.1.10\n")
            handle.write("\n")
            handle.write("# comment\n")
            handle.write("192.168.1.11\n")
            handle.flush()
            hosts = thermal.parse_hosts(handle.name)
        self.assertEqual(hosts, ["192.168.1.10", "192.168.1.11"])

    def test_fmt_uptime(self):
        self.assertEqual(thermal.fmt_uptime(0), "0m")
        self.assertEqual(thermal.fmt_uptime(61), "1m")
        self.assertEqual(thermal.fmt_uptime(3661), "1h 1m")
        self.assertEqual(thermal.fmt_uptime(90061), "1d 1h 1m")

    def test_cli_requires_host(self):
        err = io.StringIO()
        with patch.object(sys, "argv", ["thermal.py", "status"]), redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                thermal.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("-H/--host required", err.getvalue())

    def test_status_single_host(self):
        out = io.StringIO()
        with patch.object(sys, "argv", ["thermal.py", "-H", "192.168.0.10", "status"]), \
            patch("thermal.Miner", FakeMiner), \
            redirect_stdout(out):
            thermal.main()
        output = out.getvalue()
        self.assertIn("Avalon Mini3", output)
        self.assertIn("Hashrate", output)
        self.assertIn("Power", output)

    def test_status_fleet_offline(self):
        out = io.StringIO()
        with patch.object(sys, "argv", ["thermal.py", "-H", "192.168.0.10,offline", "status"]), \
            patch("thermal.Miner", FakeMiner), \
            redirect_stdout(out):
            thermal.main()
        output = out.getvalue()
        self.assertIn("OFFLINE", output)
        self.assertIn("192.168.0.10", output)

    def test_fan_validation(self):
        class Args:
            speed = "10"
        err = io.StringIO()
        with redirect_stderr(err):
            thermal.do_fan(FakeMiner("192.168.0.10"), Args)
        self.assertIn("fan speed must be 15-100", err.getvalue())


if __name__ == "__main__":
    unittest.main()
