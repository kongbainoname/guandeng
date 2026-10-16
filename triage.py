"""将冻结规则用于包含 item_id/check_time/doc_synced_at/team_code 的新 CSV。"""
import argparse
import csv
import json
from pathlib import Path

from analyze import ROOT, write_csv
from policy import route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--policy", type=Path, default=ROOT / "reports/policy.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.out.resolve():
        parser.error("输出不能覆盖输入")
    policy = json.loads(args.policy.read_text())
    with args.input.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"item_id", "check_time", "doc_synced_at", "team_code"} <= rows[0].keys():
        parser.error("CSV 为空或缺少必要字段")
    result = []
    ids = set()
    for x in rows:
        if not x["item_id"] or x["item_id"] in ids:
            parser.error("ID 缺失或重复，请先校验")
        ids.add(x["item_id"])
        result.append({"item_id": x["item_id"], **route(
            x["check_time"], x["doc_synced_at"], policy["threshold_minutes"],
            x["team_code"], policy["eligible_teams"])})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.out, result)
    print(f"已写入 {len(result)} 条分流结果：{args.out}")


if __name__ == "__main__":
    main()
