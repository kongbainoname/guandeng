"""Python 3.10+，仅标准库。重建 reports 下的审计、回放与业务清单。"""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from policy import lag_minutes, parse_time, route

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
SPLIT = datetime(2026, 9, 6, tzinfo=TZ)
FAIL_COST = 120
FLAG_COST = 10
# 没有观察到同步干预后的结果；0.5 是设计情景，非数据估计。
DESIGN_Q = 0.5
CANDIDATES = list(range(0, 66, 5)) + [90]


def load_data(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        raw = list(csv.DictReader(handle))
    required = {"item_id", "check_time", "doc_synced_at", "team_code", "clause_type",
                "match_score", "token_usage", "is_verified"}
    if not raw or set(raw[0]) != required:
        raise ValueError("空文件或字段不符合数据契约")
    seen, ids, clean = set(), set(), []
    duplicates = 0
    for line, source in enumerate(raw, 2):
        key = tuple(sorted(source.items()))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if not source["item_id"] or source["item_id"] in ids:
            raise ValueError(f"第 {line} 行 ID 为空或同一 ID 内容冲突，不静默选取")
        ids.add(source["item_id"])
        x = dict(source)
        x["lag_minutes"] = lag_minutes(x["check_time"], x["doc_synced_at"])
        x["local_time"] = parse_time(x["check_time"]).astimezone(TZ)
        x["match_score"] = int(x["match_score"])
        x["token_usage"] = int(x["token_usage"])
        if not 0 <= x["match_score"] <= 100 or x["token_usage"] < 0:
            raise ValueError(f"第 {line} 行数值越界")
        if x["is_verified"] not in ("", "0", "1"):
            raise ValueError(f"第 {line} 行标签非法")
        x["failed"] = None if x["is_verified"] == "" else x["is_verified"] == "0"
        clean.append(x)
    clean.sort(key=lambda x: (x["local_time"], x["item_id"]))
    return clean, {"raw_rows": len(raw), "exact_duplicate_rows": duplicates,
                   "unique_records": len(clean), "missing_labels": sum(x["failed"] is None for x in clean),
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def metrics(rows, predicate):
    labeled = [x for x in rows if x["failed"] is not None]
    flags = [x for x in labeled if predicate(x)]
    failures = sum(x["failed"] for x in labeled)
    tp = sum(x["failed"] for x in flags)
    n = len(flags)
    return {"n": len(labeled), "failures": failures, "flagged": n, "caught_failures": tp,
            "false_flags": n - tp, "missed_failures": failures - tp,
            "baseline_hours": failures * FAIL_COST,
            "net_hours_q50": tp * FAIL_COST * DESIGN_Q - n * FLAG_COST,
            "net_hours_q100_upper_bound": tp * FAIL_COST - n * FLAG_COST,
            "break_even_q": n * FLAG_COST / (tp * FAIL_COST) if tp else None}


def calibrate(train):
    trials = [{"threshold_minutes": t, **metrics(train, lambda x, t=t: x["lag_minutes"] > t)}
              for t in CANDIDATES]
    # 同收益时，先少打扰正常业务，再选更小的阈值。只读取训练窗口。
    best = min(trials, key=lambda x: (-x["net_hours_q50"], x["flagged"], x["threshold_minutes"]))
    return best["threshold_minutes"], trials


def write_csv(path, rows, fields=None):
    if not fields:
        fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def grouped(rows, name, key):
    groups = defaultdict(list)
    for x in rows:
        groups[key(x)].append(x)
    result = []
    for k, values in sorted(groups.items()):
        labeled = [x for x in values if x["failed"] is not None]
        f = sum(x["failed"] for x in labeled)
        result.append({name: k, "records": len(values), "labeled": len(labeled),
                       "missing_labels": len(values) - len(labeled), "failures": f,
                       "failure_rate": f / len(labeled) if labeled else ""})
    return result


def pct(n, d):
    return f"{n / d:.1%}" if d else "n.a."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/verification_logs.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    rows, audit = load_data(args.input)
    train = [x for x in rows if x["local_time"] < SPLIT]
    test = [x for x in rows if x["local_time"] >= SPLIT]
    threshold, trials = calibrate(train)
    lag_pred = lambda x: x["lag_minutes"] > threshold
    # 适用范围只用前五天确定：至少30条标记，且q=50%情景收益严格为正。
    eligibility = []
    for team in sorted({x["team_code"] for x in train}):
        m = metrics([x for x in train if x["team_code"] == team], lag_pred)
        eligibility.append({"team_code": team, "eligible": m["flagged"] >= 30 and m["net_hours_q50"] > 0, **m})
    eligible_teams = [x["team_code"] for x in eligibility if x["eligible"]]
    pred = lambda x: lag_pred(x) and x["team_code"] in eligible_teams
    audit.update({"labeled_records": sum(x["failed"] is not None for x in rows),
                  "failures": sum(x["failed"] is True for x in rows),
                  "period_start": rows[0]["local_time"].isoformat(),
                  "period_end": rows[-1]["local_time"].isoformat(),
                  "lag_min": min(x["lag_minutes"] for x in rows),
                  "lag_max": max(x["lag_minutes"] for x in rows)})
    policy = {"threshold_minutes": threshold, "comparison": ">", "calibration_q_assumption": DESIGN_Q,
              "train_before": SPLIT.isoformat(), "candidate_thresholds": CANDIDATES,
              "failure_hours": FAIL_COST, "flag_hours": FLAG_COST,
              "eligible_teams": eligible_teams, "min_training_flags_per_team": 30}
    result = {"audit": audit, "policy": policy,
              "windows": {k: metrics(v, pred) for k, v in [("train", train), ("test", test), ("all", rows)]}}
    (out / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (out / "policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
    write_csv(out / "threshold_search_train.csv", trials)
    write_csv(out / "team_eligibility_train.csv", eligibility)
    write_csv(out / "team_backtest.csv", [
        {"window": window, "team_code": team, **metrics([x for x in data if x['team_code'] == team], lag_pred)}
        for window, data in [("train", train), ("test", test), ("all", rows)]
        for team in sorted({x['team_code'] for x in rows})])

    tables = {
        "by_lag": grouped(rows, "lag_group", lambda x: "over_threshold" if lag_pred(x) else "within_threshold"),
        "by_team": grouped(rows, "team_code", lambda x: x["team_code"]),
        "by_clause": grouped(rows, "clause_type", lambda x: x["clause_type"]),
        "by_day": grouped(rows, "local_day", lambda x: x["local_time"].date().isoformat()),
        "by_score_lag": grouped(rows, "score_lag", lambda x: ("lag_high" if lag_pred(x) else "lag_low") +
                                ("/score_le3" if x["match_score"] <= 3 else "/score_gt3")),
        "by_team_score_lag": grouped(rows, "team_score_lag", lambda x: x["team_code"] + "/" +
                                ("lag_high" if lag_pred(x) else "lag_low") +
                                ("/score_le3" if x["match_score"] <= 3 else "/score_gt3")),
        "by_token": grouped(rows, "token_band", lambda x: f"{x['token_usage'] // 20 * 20:03d}-{x['token_usage'] // 20 * 20 + 19:03d}"),
    }
    for name, table in tables.items():
        write_csv(out / f"{name}.csv", table)
    strata = []
    for col in ("team_code", "clause_type"):
        for group in sorted({x[col] for x in rows}):
            subset = [x for x in rows if x[col] == group]
            for band in (False, True):
                z = [x for x in subset if lag_pred(x) == band and x["failed"] is not None]
                strata.append({"dimension": col, "group": group, "over_threshold": band,
                               "labeled": len(z), "failures": sum(x["failed"] for x in z),
                               "failure_rate": sum(x["failed"] for x in z) / len(z) if z else ""})
    write_csv(out / "stratified_lag.csv", strata)

    comparisons = []
    baselines = [("不分流", lambda x: False), ("全部待同步", lambda x: True),
                 ("统一同步间隔规则", lag_pred), ("按训练小组筛选的同步试点", pred),
                 ("低分≤3（诊断性基线）", lambda x: x["match_score"] <= 3),
                 ("统一同步或低分（诊断性基线）", lambda x: lag_pred(x) or x["match_score"] <= 3)]
    for window, data in [("train", train), ("test", test), ("all", rows)]:
        for name, fn in baselines:
            comparisons.append({"window": window, "rule": name, **metrics(data, fn)})
    write_csv(out / "strategy_comparison.csv", comparisons)
    sensitivity = []
    for window, m in result["windows"].items():
        for q in (0, 0.25, 0.5, 0.75, 1):
            net = FAIL_COST * q * m["caught_failures"] - FLAG_COST * m["flagged"]
            sensitivity.append({"window": window, "assumed_repair_rate_q": q,
                                "expected_avoided_failures": q * m["caught_failures"],
                                "net_hours": net, "remaining_plus_flag_hours": m["baseline_hours"] - net})
    write_csv(out / "sensitivity.csv", sensitivity)
    missing_bounds = []
    for window, data in [("train", train), ("test", test), ("all", rows)]:
        missing_flagged = sum(x["failed"] is None and pred(x) for x in data)
        m = result["windows"][window]
        missing_bounds.append({"window": window, "missing_flagged": missing_flagged,
                               "known_label_net_hours_q50": m["net_hours_q50"],
                               "net_hours_q50_if_unknown_flags_never_fail": m["net_hours_q50"] - missing_flagged * FLAG_COST,
                               "net_hours_q50_if_unknown_flags_all_fail": m["net_hours_q50"] + missing_flagged * (FAIL_COST * DESIGN_Q - FLAG_COST)})
    write_csv(out / "missing_label_bounds.csv", missing_bounds)

    clean = []
    for x in rows:
        y = {k: v for k, v in x.items() if k not in ("local_time", "failed")}
        y.update(route(x["check_time"], x["doc_synced_at"], threshold, x["team_code"], eligible_teams))
        y["label_status"] = "missing" if x["failed"] is None else "observed"
        clean.append(y)
    write_csv(out / "records.csv", clean)
    queue_fields = ["item_id", "check_time", "doc_synced_at", "team_code", "clause_type",
                    "lag_minutes", "action", "reason", "label_status"]
    write_csv(out / "sync_queue.csv", [x for x in clean if x["action"] == "WAIT_SYNC"], queue_fields)
    write_csv(out / "missing_label_queue.csv", [x for x in clean if x["label_status"] == "missing"], queue_fields)

    lines = ["# 自动生成的数据核对报告", "", "来源：`data/verification_logs.csv`；运行 `python3 analyze.py` 重建。",
             "", "## 数据审计", "", "```json", json.dumps(audit, ensure_ascii=False, indent=2), "```", "",
             "## 同步间隔分组", "", "|分组|有标签|失效|失效率|缺标签|", "|---|---:|---:|---:|---:|"]
    for x in tables["by_lag"]:
        lines.append(f"|{x['lag_group']}|{x['labeled']}|{x['failures']}|{pct(x['failures'], x['labeled'])}|{x['missing_labels']}|")
    lines += ["", f"## 时间回放（阈值 >{threshold} 分钟，限定训练期筛选的小组）", "", "|窗口|有标签|失效|标记|命中失效|误标|漏标失效|q=50%情景净工时|q=100%上界|",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k, m in result["windows"].items():
        lines.append(f"|{k}|{m['n']}|{m['failures']}|{m['flagged']}|{m['caught_failures']}|{m['false_flags']}|{m['missed_failures']}|{m['net_hours_q50']:g}|{m['net_hours_q100_upper_bound']}|")
    lines += ["", "这里的命中指历史失效被标记，并不代表已经避免失效。q 是假设修复率。", "",
              "低分基线仅为探索性比较：score≤3 是诊断选择，且初筛得分不保证在核验前可用。", "",
              "## 分层与诊断", ""]
    for name in ("by_team", "by_clause", "by_day", "by_score_lag", "by_token"):
        table = tables[name]
        label = list(table[0])[0]
        lines += [f"### {name}", "", "|分组|有标签|失效|失效率|缺标签|", "|---|---:|---:|---:|---:|"]
        for x in table:
            lines.append(f"|{x[label]}|{x['labeled']}|{x['failures']}|{pct(x['failures'],x['labeled'])}|{x['missing_labels']}|")
        lines.append("")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
