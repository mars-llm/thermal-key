"""Unit tests for Avalon Q-specific parsing and auth behavior."""

import unittest
from unittest.mock import patch

import thermal


class QApiMiner(thermal.Miner):
    """Mock miner with Avalon Q-like payloads (no MM ID0 in stats)."""

    def __init__(self):
        super().__init__("192.168.130.53", 4028, timeout=5)

    def cmd(self, command, param=None):
        if command == "stats":
            return {
                "STATS": [
                    {
                        "STATS": 0,
                        "ID": "AVALON0",
                        "Elapsed": 1000,
                        "MM ID0:Summary": "",
                    }
                ]
            }
        if command == "summary":
            return {
                "SUMMARY": [
                    {
                        "Elapsed": 1000,
                        "MHS av": 52_000_000.0,
                        "MHS 5s": 55_000_000.0,
                        "MHS 1m": 53_000_000.0,
                        "MHS 5m": 51_000_000.0,
                        "MHS 15m": 49_000_000.0,
                        "Hardware Errors": 2,
                        "Device Rejected%": 1.5,
                    }
                ]
            }
        if command == "devs":
            return {
                "DEVS": [
                    {
                        "Temperature": 67.0,
                        "Hardware Errors": 3,
                        "Device Rejected%": 1.7,
                    }
                ]
            }
        if command == "version":
            return {"VERSION": [{"PROD": "Avalon Q", "DNA": "020100003b70fee3"}]}
        return {"STATUS": [{"STATUS": "S"}]}

    def ascset(self, param):
        if param == "0,voltage":
            return {
                "STATUS": [
                    {
                        "STATUS": "I",
                        "Msg": "ASC 0 set info: PS[0 1209 2251 7 166 2251 199]",
                    }
                ]
            }
        if param == "0,work_mode_lvl,get":
            return {
                "STATUS": [
                    {"STATUS": "I", "Msg": "ASC 0 set info: workmode 1 worklevel 2"}
                ]
            }
        if param == "0,loop,get":
            return {
                "STATUS": [
                    {"STATUS": "I", "Msg": "ASC 0 set info: LOOP[160 ]"}
                ]
            }
        return {"STATUS": [{"STATUS": "S", "Msg": "ASC 0 set OK"}]}


class QSupportTests(unittest.TestCase):
    def test_parse_stats_fallback_for_q(self):
        miner = QApiMiner()
        stats = miner.parse_stats("q")
        self.assertAlmostEqual(stats["hashrate"], 52.0, places=2)
        self.assertAlmostEqual(stats["hashrate_max"], 55.0, places=2)
        self.assertEqual(stats["uptime"], 1000)
        self.assertEqual(stats["temp"], 67)
        self.assertEqual(stats["workmode"], 1)
        self.assertEqual(stats["worklevel"], 2)
        self.assertEqual(stats["power_in"], 1209)
        self.assertEqual(stats["voltage"], 2251)
        self.assertEqual(stats["power_out"], 166)
        self.assertEqual(stats["freq"], 160)
        self.assertEqual(stats["hw_errors"], 3)
        self.assertAlmostEqual(stats["dh_rate"], 1.7, places=2)
        self.assertEqual(stats["dna"], "020100003b70fee3")

    def test_get_dna_falls_back_to_version(self):
        miner = QApiMiner()
        self.assertEqual(miner.get_dna(), "020100003b70fee3")

    def test_web_auth_parses_spaced_jsonp(self):
        miner = thermal.Miner("192.168.130.53")

        class _Resp:
            def read(self):
                return b'getCookieCallback ({ "auth":"aaaabbbb", "code":"ccccdddd", });'

        with patch.object(
            thermal.Miner,
            "compute_auth",
            return_value={"auth": "a", "verify": "b", "cookie": "fallback-cookie"},
        ), patch.object(thermal.Miner, "ascset", return_value={"STATUS": [{"STATUS": "S"}]}), patch(
            "urllib.request.urlopen", return_value=_Resp()
        ):
            cookie = miner.web_auth("dummy-password")
        self.assertEqual(cookie, "aaaabbbbccccdddd")


if __name__ == "__main__":
    unittest.main()
