"""Property-based parsing tests for Mini 3 and Nano 3S MM ID0 strings."""

import random
import unittest
from unittest.mock import patch

import thermal


def _build_mm(fields):
    parts = []
    for key, value in fields.items():
        parts.append(f"{key}[{value}]")
    return " ".join(parts)


def _rand_range(rng, low, high):
    return rng.randint(low, high)


class ParseMini3Properties(unittest.TestCase):
    def test_decimal_ps_fields_do_not_shift_power_indexes(self):
        stats = thermal.parse_mm_id0("PS[0 1200.5 2150.0 33 730.5 2151 810]")
        self.assertEqual(stats["power_in"], 1200)
        self.assertEqual(stats["power_out"], 730)

    def test_out_of_range_numeric_payloads_do_not_crash(self):
        huge = "9" * 400
        stats = thermal.parse_mm_id0(f"Fan1[{huge}] Freq[{huge}] GHSavg[{huge}]")
        self.assertIsNone(stats["fan_rpm"])
        self.assertIsNone(stats["freq"])
        self.assertIsNone(stats["hashrate"])
        for value in ("nan", "inf", "-inf", "1e1000"):
            with self.subTest(value=value):
                self.assertIsNone(thermal._to_int(value, None))
                self.assertIsNone(thermal._to_float(value, None))

    def test_malformed_stats_entries_do_not_prevent_dna_version_fallback(self):
        for entries in (None, 123, [None, 123, {"MM ID0": 123}], [{"MM ID0": []}]):
            with self.subTest(entries=entries):
                miner = thermal.Miner("miner")
                with patch.object(miner, "cmd", side_effect=[
                    {"STATS": entries}, {"VERSION": [{"DNA": "ABCDEF"}]},
                ]):
                    self.assertEqual(miner.get_dna(), "abcdef")

    def test_dna_summary_variant_and_invalid_version_dna(self):
        miner = thermal.Miner("miner")
        with patch.object(miner, "cmd", return_value={"STATS": [{"MM ID0:Summary": "DNA[ABCDEF]"}]}):
            self.assertEqual(miner.get_dna(), "abcdef")
        for dna in (123, "not-hex", None):
            with self.subTest(dna=dna):
                miner = thermal.Miner("miner")
                with patch.object(miner, "cmd", side_effect=[{}, {"VERSION": [{"DNA": dna}]}]):
                    self.assertIsNone(miner.get_dna())

    def test_nontext_mm_payload_is_skipped(self):
        miner = thermal.Miner("miner")
        with patch.object(miner, "cmd", return_value={"STATS": [{"MM ID0": 123}]}):
            self.assertEqual(miner.parse_stats("mini3"), {})

    def test_random_mini3_fields(self):
        rng = random.Random(1337)
        for _ in range(50):
            hbtemp = _rand_range(rng, 40, 90)
            tmax = hbtemp + _rand_range(rng, 1, 10)
            fan_pct = _rand_range(rng, 15, 100)
            fan_rpm = _rand_range(rng, 600, 4000)
            freq = _rand_range(rng, 400, 700)
            voltage = _rand_range(rng, 1800, 2200)
            ghsavg = rng.uniform(10000, 50000)
            ghsmm = ghsavg + rng.uniform(0, 5000)
            uptime = _rand_range(rng, 10, 100000)
            dh = rng.uniform(0, 10)
            hw = _rand_range(rng, 0, 10)
            workmode = rng.choice([0, 1, 2])
            worklevel = _rand_range(rng, 0, 5)
            power_in = _rand_range(rng, 600, 1500)
            power_out = _rand_range(rng, 400, 1000)
            dna = "02" + "%014x" % _rand_range(rng, 0, 0xFFFFFFFFFFFF)

            fields = {
                "GHSavg": f"{ghsavg:.2f}",
                "GHSmm": f"{ghsmm:.2f}",
                "Elapsed": str(uptime),
                "HBTemp": str(hbtemp),
                "TMax": str(tmax),
                "Fan1": str(fan_rpm),
                "FanR": f"{fan_pct}%",
                "SF0": f"{freq} {freq+18} {freq+39} {freq+60}",
                "ATA1": f"750-75-{voltage}-492-20",
                "PS": f"0 {power_in} 2050 34 {power_out} 2050 737",
                "WORKMODE": str(workmode),
                "WORKLVL": str(worklevel),
                "HW": str(hw),
                "DH": f"{dh:.3f}%",
                "DNA": dna,
            }

            mm = _build_mm(fields)
            stats = thermal.parse_mm_id0(mm)

            self.assertEqual(stats["temp"], hbtemp)
            self.assertEqual(stats["temp_max"], tmax)
            self.assertEqual(stats["fan_pct"], fan_pct)
            self.assertEqual(stats["fan_rpm"], fan_rpm)
            self.assertEqual(stats["freq"], freq)
            self.assertEqual(stats["voltage"], voltage)
            self.assertEqual(stats["power_in"], power_in)
            self.assertEqual(stats["power_out"], power_out)
            self.assertEqual(stats["workmode"], workmode)
            self.assertEqual(stats["worklevel"], worklevel)
            self.assertEqual(stats["hw_errors"], hw)
            self.assertAlmostEqual(stats["device_dh_percent"], dh, places=3)
            self.assertEqual(stats["dna"], dna.lower())
            self.assertAlmostEqual(stats["hashrate"], ghsavg / 1000, places=3)
            self.assertAlmostEqual(stats["hashrate_max"], ghsmm / 1000, places=3)


class ParseNano3sProperties(unittest.TestCase):
    def test_random_nano3s_fields(self):
        rng = random.Random(4242)
        for _ in range(50):
            otemp = _rand_range(rng, 40, 90)
            tmax = otemp + _rand_range(rng, 1, 10)
            fan_pct = _rand_range(rng, 15, 100)
            fan_rpm = _rand_range(rng, 600, 2500)
            freq = _rand_range(rng, 450, 650)
            voltage = _rand_range(rng, 3000, 3800)
            ghsavg = rng.uniform(1000, 8000)
            ghsmm = ghsavg + rng.uniform(0, 2000)
            uptime = _rand_range(rng, 10, 100000)
            dh = rng.uniform(0, 40)
            hw = _rand_range(rng, 0, 5)
            workmode = rng.choice([0, 1, 2])
            worklevel = _rand_range(rng, 0, 3)
            power_in = _rand_range(rng, 0, 150)
            power_out = _rand_range(rng, 0, 150)
            dna = "02" + "%014x" % _rand_range(rng, 0, 0xFFFFFFFFFFFF)

            fields = {
                "GHSavg": f"{ghsavg:.2f}",
                "GHSmm": f"{ghsmm:.2f}",
                "Elapsed": str(uptime),
                "OTemp": str(otemp),
                "TMax": str(tmax),
                "Fan1": str(fan_rpm),
                "FanR": f"{fan_pct}%",
                "SF0": f"{freq} {freq+18} {freq+39} {freq+60}",
                "ATA1": f"95-85-{voltage}-332-20",
                "PS": f"0 {power_in} 27601 4 {power_out} 3210 114",
                "WORKMODE": str(workmode),
                "WORKLEVEL": str(worklevel),
                "HW": str(hw),
                "DH": f"{dh:.3f}%",
                "DNA": dna.upper(),
            }

            mm = _build_mm(fields)
            stats = thermal.parse_mm_id0(mm)

            self.assertEqual(stats["temp"], otemp)
            self.assertEqual(stats["temp_max"], tmax)
            self.assertEqual(stats["fan_pct"], fan_pct)
            self.assertEqual(stats["fan_rpm"], fan_rpm)
            self.assertEqual(stats["freq"], freq)
            self.assertEqual(stats["voltage"], voltage)
            self.assertEqual(stats["power_in"], power_in)
            self.assertEqual(stats["power_out"], power_out)
            self.assertEqual(stats["workmode"], workmode)
            self.assertEqual(stats["worklevel"], worklevel)
            self.assertEqual(stats["hw_errors"], hw)
            self.assertAlmostEqual(stats["device_dh_percent"], dh, places=3)
            self.assertEqual(stats["dna"], dna.lower())
            self.assertAlmostEqual(stats["hashrate"], ghsavg / 1000, places=3)
            self.assertAlmostEqual(stats["hashrate_max"], ghsmm / 1000, places=3)

    def test_live_nano3s_ata0_fallback_and_unknown_zero_max(self):
        fields = {
            "GHSavg": "820.56",
            "GHSmm": "0.00",
            "Elapsed": "268",
            "OTemp": "40",
            "ITemp": "-273",
            "TMax": "46",
            "TarT": "80",
            "Fan1": "1100",
            "FanR": "25%",
            "PS": "0 0 27923 1 0 3236 39",
            "SF0": "618 639 660 678",
            "ATA0": "62-80-3314-210-20",
            "WORKMODE": "0",
            "WORKLEVEL": "0",
            "HW": "0",
            "DH": "40.262%",
            "PING": "233",
            "NETFAIL": "0 0 0 0",
            "MEMFREE": "64748",
            "PFCnt": "2",
            "DNA": "0000000000000003",
        }
        stats = thermal.parse_mm_id0(_build_mm(fields))
        self.assertEqual(stats["power_in"], 62)
        self.assertIsNone(stats["power_out"])
        self.assertEqual(stats["voltage"], 3314)
        self.assertIsNone(stats["hashrate_max"])
        self.assertIsNone(stats["temp_inlet"])
        self.assertEqual(stats["target_temp"], 80)
        self.assertEqual(stats["fan_rpms"], [1100])
        self.assertEqual(stats["ping_ms"], 233)
        self.assertEqual(stats["net_failures"], [0, 0, 0, 0])
        self.assertEqual(stats["net_failures_total"], 0)
        self.assertEqual(stats["memory_free_kb"], 64748)
        self.assertEqual(stats["pf_count"], 2)

    def test_invalid_itemp_is_unknown(self):
        fields = {
            "GHSavg": "0.00",
            "GHSmm": "0.00",
            "Elapsed": "10",
            "ITemp": "-273",
            "Fan1": "800",
            "FanR": "20%",
            "SF0": "500 518 539 560",
            "ATA1": "95-85-3522-332-20",
            "PS": "0 0 27601 4 0 3210 114",
            "WORKMODE": "1",
            "WORKLEVEL": "0",
            "HW": "0",
            "DH": "0.000%",
            "DNA": "0000000000000003",
        }
        mm = _build_mm(fields)
        stats = thermal.parse_mm_id0(mm)
        self.assertIsNone(stats["temp"])


if __name__ == "__main__":
    unittest.main()
