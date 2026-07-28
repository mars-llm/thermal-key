"""Unit tests for Avalon Q-specific parsing and auth behavior."""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    def test_device_key_from_product_handles_spaced_names(self):
        self.assertEqual(thermal.device_key_from_product("Avalon Nano 3S"), "nano3s")
        self.assertEqual(thermal.device_key_from_product("Avalon Mini 3"), "mini3")
        self.assertEqual(thermal.device_key_from_product("Avalon Q"), "q")

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
        self.assertAlmostEqual(stats["reject_rate"], 1.5, places=2)
        self.assertIsNone(stats["device_dh_percent"])
        self.assertEqual(stats["dna"], "020100003b70fee3")

    def test_parse_stats_prefers_q_estats_telemetry(self):
        class ExtendedQMiner(QApiMiner):
            def cmd(self, command, param=None):
                if command == "estats":
                    mm = (
                        "Ver[Q-live] DNA[020100003b70fee3] Elapsed[2435] "
                        "ITemp[26] HBITemp[58] HBOTemp[62] TMax[70] TAvg[63] TarT[65] "
                        "Fan1[1415] Fan2[1417] Fan3[1411] Fan4[1408] FanR[41%] FanErr[0] "
                        "PS[0 1208 2150 33 731 2151 810] GHSmm[65948.76] "
                        "GHSavg[53537.13] Freq[338.96] SF0[0 0 0 0] "
                        "ATA0[800-65-2256-276-20] WORKMODE[0] WORKLEVEL[0] "
                        "HW[0] DH[3.038%] RSSI[-55] PING[208] "
                        "NETFAIL[0 1 0 0] Filter[51338] MEMFREE[65324]"
                    )
                    return {"STATS": [{"MM ID0:Summary": mm}]}
                return super().cmd(command, param)

        stats = ExtendedQMiner().parse_stats("q")
        self.assertAlmostEqual(stats["hashrate"], 53.53713, places=5)
        self.assertAlmostEqual(stats["hashrate_max"], 65.94876, places=5)
        self.assertEqual(stats["temp"], 63)
        self.assertEqual(stats["temp_max"], 70)
        self.assertEqual(stats["fan_rpm"], 1415)
        self.assertEqual(stats["fan_pct"], 41)
        self.assertEqual(stats["freq"], 338)
        self.assertEqual(stats["voltage"], 2256)
        self.assertEqual(stats["power_in"], 1208)
        self.assertEqual(stats["power_out"], 731)
        self.assertEqual(stats["temp_inlet"], 26)
        self.assertEqual(stats["temp_board_inlet"], 58)
        self.assertEqual(stats["temp_board_outlet"], 62)
        self.assertEqual(stats["target_temp"], 65)
        self.assertEqual(stats["fan_rpms"], [1415, 1417, 1411, 1408])
        self.assertEqual(stats["fan_error"], 0)
        self.assertEqual(stats["rssi"], -55)
        self.assertEqual(stats["ping_ms"], 208)
        self.assertEqual(stats["net_failures_total"], 1)
        self.assertEqual(stats["filter_value"], 51338)
        self.assertEqual(stats["memory_free_kb"], 65324)
        self.assertAlmostEqual(stats["reject_rate"], 1.5, places=2)
        self.assertAlmostEqual(stats["device_dh_percent"], 3.038, places=3)

    def test_parse_stats_fallback_preserves_valid_zero_values(self):
        class IdleQMiner(QApiMiner):
            def cmd(self, command, param=None):
                if command == "summary":
                    return {"SUMMARY": [{"Elapsed": 0, "MHS av": 0, "MHS 5s": 0}]}
                if command == "devs":
                    return {"DEVS": [{"Temperature": 0}]}
                return super().cmd(command, param)

            def ascset(self, param):
                if param == "0,loop,get":
                    return {
                        "STATUS": [
                            {"STATUS": "I", "Msg": "ASC 0 set info: LOOP[0 ]"}
                        ]
                    }
                return super().ascset(param)

        stats = IdleQMiner().parse_stats("q")
        self.assertEqual(stats["hashrate"], 0)
        self.assertEqual(stats["hashrate_max"], 0)
        self.assertEqual(stats["uptime"], 0)
        self.assertEqual(stats["freq"], 0)
        self.assertIsNone(stats["temp"])

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

    def test_qinfo_accepts_model_only_version_entry(self):
        class QModelOnlyMiner(QApiMiner):
            def cmd(self, command, param=None):
                if command == "version":
                    return {"VERSION": [{"MODEL": "Q"}]}
                return super().cmd(command, param)

        miner = QModelOnlyMiner()
        out = io.StringIO()
        err = io.StringIO()

        class Args:
            pass

        with redirect_stdout(out), redirect_stderr(err):
            thermal.do_qinfo(miner, Args)

        self.assertEqual(err.getvalue(), "")
        self.assertIn("Device", out.getvalue())

    def test_status_uses_model_when_prod_missing(self):
        class QModelStatusMiner:
            host = "192.168.130.53"

            def cmd(self, command, param=None):
                if command == "version":
                    return {"VERSION": [{"MODEL": "Avalon Q", "DNA": "020100003b70fee3", "LVERSION": "fw", "CGMiner": "4.11.1"}]}
                return None

            def parse_stats(self, device_key=None, version_entry=None):
                return {}

        out = io.StringIO()

        class Args:
            pass

        with redirect_stdout(out):
            thermal.do_status(QModelStatusMiner(), Args)

        self.assertIn("Avalon Q @ 192.168.130.53", out.getvalue())

    def test_watch_recovers_after_initial_version_failure(self):
        class FlakyVersionMiner:
            def __init__(self):
                self.host = "192.168.130.53"
                self.version_calls = 0
                self.device_keys = []

            def cmd(self, command, param=None):
                if command == "version":
                    self.version_calls += 1
                    if self.version_calls == 1:
                        return None
                    return {"VERSION": [{"MODEL": "Q"}]}
                return None

            def parse_stats(self, device_key=None, version_entry=None):
                self.device_keys.append(device_key)
                if device_key != "q":
                    return {}
                return {
                    "hashrate": 1.0,
                    "temp": 50,
                    "fan_pct": 20,
                    "power_in": 100,
                    "workmode": 1,
                    "uptime": 10,
                }

        miner = FlakyVersionMiner()
        out = io.StringIO()

        class Args:
            interval = 0

        with patch("thermal.time.sleep", side_effect=KeyboardInterrupt):
            with redirect_stdout(out):
                thermal.do_watch(miner, Args)

        self.assertIn("q", miner.device_keys)
        self.assertIn("TH/s", out.getvalue())


if __name__ == "__main__":
    unittest.main()
