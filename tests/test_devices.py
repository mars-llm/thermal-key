"""Integration tests for Avalon Mini 3 and Nano 3S devices."""

import unittest

import thermal

from tests.device_test_config import HOSTS, PORT, TIMEOUT, SKIP_OFFLINE, ALLOW_WRITE
import re


def _status_ok(result):
    if not result:
        return False
    status = result.get("STATUS", [{}])[0]
    return status.get("STATUS") == "S"


def _ascset_commands(miner):
    result = miner.ascset("0,help")
    if not result or "STATUS" not in result:
        return set(), result
    msg = result["STATUS"][0].get("Msg", "")
    if ": " in msg:
        msg = msg.split(": ", 1)[1]
    cmds = {cmd.strip() for cmd in msg.split("|") if cmd.strip()}
    return cmds, result


def _extract_mm_value(mm_text, key):
    pattern = re.escape(key) + r"\[([^\]]+)\]"
    match = re.search(pattern, mm_text)
    if not match:
        return None
    return match.group(1)


class DeviceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HOSTS:
            raise unittest.SkipTest("No hosts configured")

    def _miner(self, host):
        return thermal.Miner(host, PORT, timeout=TIMEOUT)

    def _require_online(self, host):
        miner = self._miner(host)
        ver = miner.cmd("version")
        if not ver or "VERSION" not in ver:
            msg = f"Host {host} offline or no version response"
            if SKIP_OFFLINE:
                raise unittest.SkipTest(msg)
            self.fail(msg)
        return miner, ver["VERSION"][0]

    def test_version_fields(self):
        for host in HOSTS:
            with self.subTest(host=host):
                _, ver = self._require_online(host)
                prod = ver.get("PROD") or ver.get("MODEL") or ver.get("Model")
                self.assertTrue(prod, f"missing PROD/MODEL in version: {ver}")

                dna = ver.get("DNA", "")
                self.assertRegex(dna, r"^[0-9a-fA-F]+$", f"invalid DNA: {dna}")

                cgminer = ver.get("CGMiner") or ver.get("CGMINER")
                self.assertTrue(cgminer, f"missing CGMiner version in: {ver}")

                firmware = ver.get("LVERSION") or ver.get("FIRMWARE") or ver.get("FW")
                self.assertTrue(firmware, f"missing firmware version in: {ver}")

    def test_dna_extraction(self):
        for host in HOSTS:
            with self.subTest(host=host):
                miner, _ = self._require_online(host)
                dna = miner.get_dna()
                self.assertTrue(dna, "get_dna() returned empty")

    def test_summary(self):
        for host in HOSTS:
            with self.subTest(host=host):
                miner, _ = self._require_online(host)
                summary = miner.cmd("summary")
                self.assertTrue(summary and "SUMMARY" in summary, "summary missing SUMMARY")
                s0 = summary["SUMMARY"][0]
                has_rate_key = any(k in s0 for k in ("MHS av", "GHS av", "KHS av"))
                self.assertTrue(has_rate_key, f"summary missing hashrate field: {list(s0.keys())}")
                self.assertIn("Elapsed", s0, f"summary missing Elapsed: {s0}")

    def test_pools(self):
        for host in HOSTS:
            with self.subTest(host=host):
                miner, _ = self._require_online(host)
                pools = miner.cmd("pools")
                self.assertTrue(pools and "POOLS" in pools, "pools missing POOLS")
                self.assertGreaterEqual(len(pools["POOLS"]), 1, "no pools configured")
                p0 = pools["POOLS"][0]
                for key in ("POOL", "URL", "User", "Status"):
                    self.assertIn(key, p0, f"pool entry missing {key}: {p0}")

    def test_stats_parsing(self):
        for host in HOSTS:
            with self.subTest(host=host):
                miner, ver = self._require_online(host)
                prod = ver.get("PROD") or ver.get("MODEL") or ver.get("Model")
                stats = miner.parse_stats(thermal.device_key_from_product(prod), version_entry=ver)
                if not stats:
                    raw = miner.cmd("stats") or {}
                    mm_ids = [s.get("MM ID0") for s in raw.get("STATS", []) if "MM ID0" in s]
                    self.fail(f"parse_stats returned empty. MM ID0 sample: {mm_ids[:1]}")

                required = {
                    "hashrate",
                    "hashrate_max",
                    "uptime",
                    "temp",
                    "temp_max",
                    "fan_rpm",
                    "fan_pct",
                    "freq",
                    "voltage",
                    "power_in",
                    "power_out",
                    "workmode",
                    "worklevel",
                    "hw_errors",
                    "dh_rate",
                    "dna",
                }
                for key in required:
                    self.assertIn(key, stats, f"stats missing {key}: {stats}")

                self.assertGreaterEqual(stats["hashrate"], 0)
                self.assertGreaterEqual(stats["temp"], 0)
                self.assertLessEqual(stats["temp"], 130)
                self.assertGreaterEqual(stats["fan_pct"], 0)
                self.assertLessEqual(stats["fan_pct"], 100)
                self.assertGreaterEqual(stats["uptime"], 0)

    def test_stats_parse_consistency_with_raw(self):
        for host in HOSTS:
            with self.subTest(host=host):
                miner, ver = self._require_online(host)
                prod = (ver.get("PROD") or ver.get("MODEL") or "").lower()
                raw = miner.cmd("stats") or {}
                mm_list = [s.get("MM ID0") for s in raw.get("STATS", []) if "MM ID0" in s]
                if "avalon q" in prod or prod.strip() == "q":
                    if not mm_list:
                        # Avalon Q firmware omits MM ID0; fallback parsing is validated in test_stats_parsing.
                        continue
                if not mm_list:
                    self.fail("stats missing MM ID0")
                mm = mm_list[0]
                stats = thermal.parse_mm_id0(mm)

                temp_value = _extract_mm_value(mm, "HBTemp")
                if temp_value is None:
                    temp_value = _extract_mm_value(mm, "OTemp")
                if temp_value is not None:
                    expected_temp = int(float(temp_value.split()[0]))
                    self.assertEqual(
                        stats.get("temp"),
                        expected_temp,
                        f"temp parse mismatch: expected {expected_temp} from MM ID0, got {stats.get('temp')}",
                    )

                fan_rpm = _extract_mm_value(mm, "Fan1")
                if fan_rpm is not None:
                    expected_rpm = int(float(fan_rpm.split()[0]))
                    self.assertEqual(
                        stats.get("fan_rpm"),
                        expected_rpm,
                        f"fan_rpm parse mismatch: expected {expected_rpm}, got {stats.get('fan_rpm')}",
                    )

                fan_pct = _extract_mm_value(mm, "FanR")
                if fan_pct is not None:
                    expected_pct = int(float(fan_pct.replace("%", "").split()[0]))
                    self.assertEqual(
                        stats.get("fan_pct"),
                        expected_pct,
                        f"fan_pct parse mismatch: expected {expected_pct}, got {stats.get('fan_pct')}",
                    )

    def test_ascset_help_core_commands(self):
        expected = {"fan-spd", "frequency", "workmode", "worklevel"}
        for host in HOSTS:
            with self.subTest(host=host):
                miner, _ = self._require_online(host)
                cmds, result = _ascset_commands(miner)
                self.assertTrue(result and "STATUS" in result, "ascset help missing STATUS")
                self.assertTrue(cmds, "ascset help missing commands list")
                missing = sorted(expected - cmds)
                self.assertFalse(missing, f"ascset help missing {missing}. got: {sorted(cmds)}")

    def test_ascset_help_product_specific(self):
        for host in HOSTS:
            with self.subTest(host=host):
                miner, ver = self._require_online(host)
                prod = (ver.get("PROD") or ver.get("MODEL") or "").lower()
                cmds, result = _ascset_commands(miner)
                self.assertTrue(result and "STATUS" in result, "ascset help missing STATUS")
                self.assertTrue(cmds, "ascset help missing commands list")

                if "nano3" in prod:
                    for required in ("ledmode", "ledset"):
                        self.assertIn(required, cmds, f"Nano3s missing {required}: {sorted(cmds)}")
                elif "mini3" in prod:
                    for required in ("smart-speed", "target-temp"):
                        self.assertIn(required, cmds, f"Mini3 missing {required}: {sorted(cmds)}")
                elif "avalon q" in prod or prod.strip() == "q":
                    for required in ("voltage", "solo-allowed", "work_mode_lvl", "time"):
                        self.assertIn(required, cmds, f"Avalon Q missing {required}: {sorted(cmds)}")


class DeviceWriteSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ALLOW_WRITE:
            raise unittest.SkipTest("Set TK_ALLOW_WRITE=1 to enable write tests")
        if not HOSTS:
            raise unittest.SkipTest("No hosts configured")

    def _miner(self, host):
        return thermal.Miner(host, PORT, timeout=TIMEOUT)

    def _require_online(self, host):
        miner = self._miner(host)
        ver = miner.cmd("version")
        if not ver or "VERSION" not in ver:
            msg = f"Host {host} offline or no version response"
            if SKIP_OFFLINE:
                raise unittest.SkipTest(msg)
            self.fail(msg)
        return miner

    def _assert_status_success(self, result, label):
        self.assertTrue(_status_ok(result), f"{label} failed: {result}")

    def test_write_noop_commands(self):
        for host in HOSTS:
            with self.subTest(host=host):
                miner = self._require_online(host)
                ver = miner.cmd("version")
                ver0 = ver["VERSION"][0] if ver and "VERSION" in ver and ver["VERSION"] else {}
                prod = ver0.get("PROD") or ver0.get("MODEL") or ver0.get("Model")
                stats = miner.parse_stats(thermal.device_key_from_product(prod), version_entry=ver0)
                if not stats:
                    self.fail("parse_stats returned empty; cannot run write tests")

                freq = stats.get("freq", 0)
                if freq > 0:
                    cmd = f"0,frequency,{freq}:{freq}:{freq}:{freq}"
                    self._assert_status_success(miner.ascset(cmd), "frequency")

                workmode = stats.get("workmode", 1)
                cmd = f"0,workmode,set,{workmode}"
                self._assert_status_success(miner.ascset(cmd), "workmode")

                worklevel = stats.get("worklevel", 0)
                cmd = f"0,worklevel,set,{worklevel}"
                self._assert_status_success(miner.ascset(cmd), "worklevel")

                fan_pct = stats.get("fan_pct", 0)
                if 15 <= fan_pct <= 100:
                    cmd = f"0,fan-spd,{fan_pct}"
                    self._assert_status_success(miner.ascset(cmd), "fan-spd")


if __name__ == "__main__":
    unittest.main()
