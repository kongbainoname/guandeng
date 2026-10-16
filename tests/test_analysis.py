import csv
import tempfile
import unittest
from pathlib import Path

from analyze import ROOT, SPLIT, calibrate, load_data, metrics
from policy import lag_minutes, route


class PolicyTests(unittest.TestCase):
    def test_mixed_timezone(self):
        self.assertEqual(lag_minutes("2026-09-01T07:08:00+08:00", "2026-08-31T22:57:00Z"), 11)

    def test_boundary(self):
        sync = "2026-09-01T00:00:00Z"
        self.assertEqual(route("2026-09-01T08:15:00+08:00", sync)["action"], "CONTINUE_CHECK")
        self.assertEqual(route("2026-09-01T08:15:01+08:00", sync)["action"], "WAIT_SYNC")

    def test_bad_times_are_not_passed(self):
        for check, sync in [("", ""), ("bad", "bad"),
                            ("2026-09-01T08:00:00", "2026-09-01T00:00:00Z"),
                            ("2026-09-01T00:00:00Z", "2026-09-01T00:01:00Z")]:
            self.assertEqual(route(check, sync)["action"], "REVIEW_TIME_ERROR")

    def test_scope(self):
        times = ("2026-09-01T01:00:00Z", "2026-09-01T00:00:00Z")
        self.assertEqual(route(*times, team_code="医疗组", eligible_teams=["科技组"])["action"], "CONTINUE_CHECK")
        self.assertEqual(route(*times, team_code="科技组", eligible_teams=["科技组"])["action"], "WAIT_SYNC")
        self.assertEqual(route(*times, eligible_teams=["科技组"])["action"], "REVIEW_TEAM_ERROR")


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.audit = load_data(ROOT / "data/verification_logs.csv")

    def test_source_reconciliation(self):
        self.assertEqual(self.audit["raw_rows"], 1214)
        self.assertEqual(self.audit["exact_duplicate_rows"], 58)
        self.assertEqual(len(self.rows), 1156)
        self.assertEqual(sum(x["failed"] is None for x in self.rows), 52)
        self.assertEqual(sum(x["failed"] is True for x in self.rows), 120)

    def test_duplicate_and_conflict(self):
        with (ROOT / "data/verification_logs.csv").open() as f:
            row = next(csv.DictReader(f))
        def load(records):
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "input.csv"
                with p.open("w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(row))
                    w.writeheader()
                    w.writerows(records)
                return load_data(p)
        records, audit = load([row, row])
        self.assertEqual(len(records), 1)
        self.assertEqual(audit["exact_duplicate_rows"], 1)
        with self.assertRaises(ValueError):
            load([row, {**row, "token_usage": str(int(row["token_usage"]) + 1)}])

    def test_frozen_threshold(self):
        train = [x for x in self.rows if x["local_time"] < SPLIT]
        self.assertEqual(calibrate(train)[0], 15)
        self.assertEqual(calibrate(list(reversed(train)))[0], 15)

    def test_cost_formula_and_missing_labels(self):
        records = [{"failed": True}, {"failed": False}, {"failed": None}]
        m = metrics(records, lambda x: True)
        self.assertEqual(m["n"], 2)
        self.assertEqual(m["flagged"], 2)
        self.assertEqual(m["net_hours_q50"], 40)
        self.assertAlmostEqual(m["break_even_q"], 1 / 6)

    def test_policy_does_not_use_labels_scores_or_tokens(self):
        for row in self.rows[:30]:
            before = route(row["check_time"], row["doc_synced_at"], 15, row["team_code"], ["科技组"])
            changed = {**row, "is_verified": "0", "match_score": 9999, "token_usage": 999999}
            after = route(changed["check_time"], changed["doc_synced_at"], 15, changed["team_code"], ["科技组"])
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
