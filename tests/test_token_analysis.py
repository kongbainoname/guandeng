"""合成样本仅验证分层统计和阈值搜索逻辑。"""
import unittest
from analyze_token import high_token, standardized, token_best


class TokenAnalysisTests(unittest.TestCase):
    def test_high_token_boundary(self):
        self.assertFalse(high_token({"token_usage": 65}))
        self.assertTrue(high_token({"token_usage": 66}))

    def test_standardization_only_uses_overlap(self):
        rows = [
            {"group": "a", "token_usage": 70, "failed": False},
            {"group": "a", "token_usage": 50, "failed": False},
            {"group": "b", "token_usage": 70, "failed": True},
            {"group": "b", "token_usage": 50, "failed": True},
            {"group": "no_overlap", "token_usage": 70, "failed": True},
        ]
        result = standardized(rows, [lambda x: x["group"]])
        self.assertEqual(result["overlap_strata"], 2)
        self.assertEqual(result["covered_records"], 4)
        self.assertEqual(result["standardized_high_failure_rate"], .5)
        self.assertEqual(result["standardized_other_failure_rate"], .5)

    def test_no_benefit_prefers_no_alert(self):
        rows = [{"failed": False, "token_usage": 70, "p_timely_fail": 0}]
        self.assertIsNone(token_best(rows)[0])
