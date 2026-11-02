"""Contract tests for model-aware labels and read-only JSON snapshots."""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import telemetry
import thermal


def responses(text=None):
    return [
        {"STATUS": [{"STATUS": "S"}], "VERSION": [{"PROD": "Avalon Q", "API": "3.7", "LVERSION": "1.2.3"}]},
        {"STATUS": [{"STATUS": "S"}], "STATS": [{"MM ID0:Summary": text or (
            "STATE[1] ITemp[24] HBITemp[55] HBOTemp[61] TAvg[60.5] TMax[66] "
            "Fan1[1200] Fan2[1210] Fan3[1220] Fan4[1230] FanR[40%] FanErr[0] WORKMODE[0]"
        )}]},
        {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"Elapsed": 120, "MHS av": 50_000_000,
          "Accepted": 5, "Rejected": 0, "Hardware Errors": 0}]},
    ]


class TelemetryTests(unittest.TestCase):
    def test_snapshot_units_and_exact_output_schema(self):
        result = telemetry.normalize_snapshot(*responses())
        self.assertEqual(set(result), {"schema_version", "observed_at", "model", "firmware_version", "api_version", "metrics"})
        values = result["metrics"]
        self.assertEqual(values["hashrate_th_s"], 50)
        self.assertEqual(values["uptime_minutes"], 2)
        self.assertEqual(values["work_mode"], "Eco")
        self.assertEqual(values["asic_average_temperature_c"], 60.5)
        self.assertEqual(values["fan_4_rpm"], 1230)
        self.assertEqual(values["fan_error"], 0)
        json.dumps(result, allow_nan=False)

    def test_missing_and_boot_sentinel_values_are_null_not_zero(self):
        result = telemetry.normalize_snapshot(*responses("STATE[0] HBITemp[-273] TAvg[0] FanR[101%]"))
        for field in ("inlet_temperature_c", "board_inlet_temperature_c", "asic_average_temperature_c", "work_mode", "fan_speed_percent"):
            self.assertIsNone(result["metrics"][field])
        self.assertEqual(result["metrics"]["operating_state"], "Initializing")

    def test_valid_zero_counters_survive(self):
        values = responses()
        values[2]["SUMMARY"][0].update({"MHS av": 0, "Elapsed": 0})
        metrics = telemetry.normalize_snapshot(*values)["metrics"]
        self.assertEqual(metrics["hashrate_th_s"], 0)
        self.assertEqual(metrics["uptime_minutes"], 0)
        self.assertEqual(metrics["rejected_shares"], 0)

    def test_invalid_required_responses_fail_closed(self):
        for index in range(3):
            for status in (None, [], [{}], [{"STATUS": []}], [{"STATUS": "E"}], [{"STATUS": "S"}, {"STATUS": "E"}]):
                values = responses()
                values[index]["STATUS"] = status
                with self.subTest(index=index, status=status), self.assertRaises(telemetry.TelemetryError):
                    telemetry.normalize_snapshot(*values)
        for value in (None, True, -1, float("nan"), float("inf")):
            values = responses()
            values[2]["SUMMARY"][0]["MHS av"] = value
            with self.subTest(value=value), self.assertRaises(telemetry.TelemetryError):
                telemetry.normalize_snapshot(*values)

    def test_extension_fields_do_not_change_output(self):
        values = responses()
        original = copy.deepcopy(values)
        values[0]["VERSION"][0].update({"DNA": "test-extension", "MAC": "test-extension"})
        values[1]["STATS"][0]["extra"] = {"arbitrary": "extension"}
        left, right = telemetry.normalize_snapshot(*values), telemetry.normalize_snapshot(*original)
        left.pop("observed_at")
        right.pop("observed_at")
        self.assertEqual(left, right)

    def test_duplicate_enum_or_temperature_fields_are_unknown(self):
        metrics = telemetry.normalize_snapshot(*responses("WORKMODE[0] WORKMODE[2] ITemp[20] ITemp[30]"))["metrics"]
        self.assertIsNone(metrics["work_mode"])
        self.assertIsNone(metrics["inlet_temperature_c"])

    def test_client_sends_only_three_allowed_reads_without_retry(self):
        client = telemetry.QTelemetryClient("miner.example")
        with patch.object(thermal.Miner, "cmd", side_effect=responses()) as send:
            self.assertEqual(client.read_snapshot()["metrics"]["work_mode"], "Eco")
        self.assertEqual([call.args for call in send.call_args_list], [("version",), ("litestats",), ("summary",)])
        self.assertEqual(client.miner.retries, 0)
        for command, param in (("ascset", "0,softoff,1"), ("restart", None), ("version", "extra"), ("pools", None)):
            with self.subTest(command=command), patch.object(thermal.Miner, "cmd") as send:
                with self.assertRaises(telemetry.TelemetryError):
                    client.miner.cmd(command, param)
                send.assert_not_called()

    def test_client_stops_at_first_failed_or_wrong_model_response(self):
        for response in (None, {"STATUS": [{"STATUS": "E"}]},
                         {"STATUS": [{"STATUS": "S"}], "VERSION": [{"PROD": "Avalon Mini3"}]}):
            with patch.object(thermal.Miner, "cmd", return_value=response) as send:
                with self.assertRaises(telemetry.TelemetryError):
                    telemetry.QTelemetryClient("miner.example").read_snapshot()
                self.assertEqual(send.call_count, 1)

    def test_invalid_configuration_rejected_before_connection(self):
        for args in (("",), ("two hosts",), ("miner", 0), ("miner", 4028, 0), ("miner", 4028, 11)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                telemetry.QTelemetryClient(*args)

    def test_q_and_mini_modes_are_separate(self):
        self.assertEqual([thermal.mode_name(i, "q") for i in range(3)], ["Eco", "Standard", "Super"])
        self.assertEqual([thermal.mode_name(i, "mini3") for i in range(3)], ["Heater", "Mining", "Night"])
        self.assertEqual(thermal.mode_abbreviation(2, "q"), "Su")
        self.assertEqual(thermal.mode_name(None, "q"), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
