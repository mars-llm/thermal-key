"""Unit tests for CLI parameter formatting and command dispatch."""

import io
import unittest
from contextlib import redirect_stdout, redirect_stderr

import thermal


class RecordingMiner:
    def __init__(self, host="192.168.0.10", port=4028, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.last_cmd = None
        self.last_param = None
        self.last_ascset = None

    def cmd(self, command, param=None):
        self.last_cmd = command
        self.last_param = param
        if command == "version":
            return {
                "VERSION": [
                    {
                        "PROD": "Avalon Mini3",
                        "DNA": "0201000046d3803b",
                        "MAC": "e0e1a93ed589",
                        "LVERSION": "25022401_cb28ba7",
                        "CGMiner": "4.11.1",
                    }
                ]
            }
        return {"STATUS": [{"STATUS": "S"}]}

    def ascset(self, param):
        self.last_ascset = param
        return {"STATUS": [{"STATUS": "S", "Msg": "ASC 0 set OK"}]}

    def parse_stats(self):
        return {
            "hashrate": 40.0,
            "hashrate_max": 41.0,
            "uptime": 60,
            "temp": 60,
            "temp_max": 70,
            "fan_rpm": 3000,
            "fan_pct": 80,
            "freq": 500,
            "voltage": 2100,
            "power_in": 1200,
            "power_out": 700,
            "workmode": 1,
            "worklevel": 0,
            "hw_errors": 0,
            "dh_rate": 1.0,
            "dna": "0201000046d3803b",
        }


class CliParamTests(unittest.TestCase):
    def test_do_fan_auto(self):
        miner = RecordingMiner()
        class Args: speed = "auto"
        thermal.do_fan(miner, Args)
        self.assertEqual(miner.last_ascset, "0,fan-spd,-1")

    def test_do_fan_percent(self):
        miner = RecordingMiner()
        class Args: speed = "80"
        thermal.do_fan(miner, Args)
        self.assertEqual(miner.last_ascset, "0,fan-spd,80")

    def test_do_fan_invalid(self):
        miner = RecordingMiner()
        class Args: speed = "10"
        err = io.StringIO()
        with redirect_stderr(err):
            thermal.do_fan(miner, Args)
        self.assertIn("fan speed must be 15-100", err.getvalue())
        self.assertIsNone(miner.last_ascset)

    def test_do_freq(self):
        miner = RecordingMiner()
        class Args: freq = 500
        thermal.do_freq(miner, Args)
        self.assertEqual(miner.last_ascset, "0,frequency,500:500:500:500")

    def test_do_mode(self):
        miner = RecordingMiner()
        class Args: mode = 2
        thermal.do_mode(miner, Args)
        self.assertEqual(miner.last_ascset, "0,workmode,set,2")

    def test_do_level(self):
        miner = RecordingMiner()
        class Args: level = 3
        thermal.do_level(miner, Args)
        self.assertEqual(miner.last_ascset, "0,worklevel,set,3")

    def test_switchpool(self):
        miner = RecordingMiner()
        class Args: id = 1
        thermal.do_switchpool(miner, Args)
        self.assertEqual(miner.last_cmd, "switchpool")
        self.assertEqual(miner.last_param, "1")

    def test_enablepool(self):
        miner = RecordingMiner()
        class Args: id = 2
        thermal.do_enablepool(miner, Args)
        self.assertEqual(miner.last_cmd, "enablepool")
        self.assertEqual(miner.last_param, "2")

    def test_disablepool(self):
        miner = RecordingMiner()
        class Args: id = 3
        thermal.do_disablepool(miner, Args)
        self.assertEqual(miner.last_cmd, "disablepool")
        self.assertEqual(miner.last_param, "3")

    def test_do_raw_with_param(self):
        miner = RecordingMiner()
        class Args:
            command = "switchpool"
            param = "0"
        out = io.StringIO()
        with redirect_stdout(out):
            thermal.do_raw(miner, Args)
        self.assertEqual(miner.last_cmd, "switchpool")
        self.assertEqual(miner.last_param, "0")

    def test_do_raw_without_param(self):
        miner = RecordingMiner()
        class Args:
            command = "summary"
            param = None
        out = io.StringIO()
        with redirect_stdout(out):
            thermal.do_raw(miner, Args)
        self.assertEqual(miner.last_cmd, "summary")
        self.assertIsNone(miner.last_param)

    def test_do_ascset_pass_through(self):
        miner = RecordingMiner()
        class Args: param = "0,fan-spd,80"
        out = io.StringIO()
        with redirect_stdout(out):
            thermal.do_ascset(miner, Args)
        self.assertEqual(miner.last_ascset, "0,fan-spd,80")

    def test_status_compact_output(self):
        miner = RecordingMiner()
        class Args: pass
        out = io.StringIO()
        with redirect_stdout(out):
            thermal.do_status(miner, Args, compact=True)
        self.assertIn("TH/s", out.getvalue())


if __name__ == "__main__":
    unittest.main()
