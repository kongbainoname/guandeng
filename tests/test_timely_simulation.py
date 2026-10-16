"""合成输入仅验证公式与边界，不进入业务结论。"""
import unittest

from simulate_timely import best, cost, selected, timely_references, wilson_upper


class SimulationTests(unittest.TestCase):
    def test_cost_uses_all_flagged_reference_risks(self):
        rows = [{"failed": True, "lag_minutes": 30, "p_timely_fail": .1},
                {"failed": False, "lag_minutes": 30, "p_timely_fail": .1},
                {"failed": True, "lag_minutes": 1, "p_timely_fail": .1}]
        result = cost(rows, lambda x: x["lag_minutes"] > 15)
        # 2次标记20 + 未标记失效120 + 标记后模拟失效0.2*120。
        self.assertAlmostEqual(result["simulated_total_hours"], 164)
        self.assertAlmostEqual(result["simulated_net_hours"], 76)

    def test_zero_observed_failures_not_guarantee(self):
        self.assertGreater(wilson_upper(0, 10), .27)
        self.assertLess(wilson_upper(0, 100), wilson_upper(0, 10))

    def test_no_benefit_selects_no_sync(self):
        rows = [{"failed": False, "lag_minutes": 30, "p_timely_fail": 0}]
        self.assertIsNone(best(rows, "p_timely_fail"))
        self.assertFalse(selected({"lag_minutes": 999}, None))

    def test_one_minute_baseline_and_missing_labels(self):
        rows = [{"failed": False, "lag_minutes": 1}, {"failed": True, "lag_minutes": 1.01},
                {"failed": None, "lag_minutes": 0}]
        self.assertEqual(len(timely_references(rows)), 1)

    def test_missing_labels_not_free(self):
        rows = [{"failed": None, "lag_minutes": 30, "p_timely_fail": .1}]
        result = cost(rows, lambda x: True)
        self.assertEqual(result["n"], 0)
        self.assertEqual(result["unknown_flags"], 1)
        self.assertEqual(result["net_after_unknown_flag_cost_only"], -10)


if __name__ == "__main__":
    unittest.main()
