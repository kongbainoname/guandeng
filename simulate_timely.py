"""以历史≤1分钟同步的失效率模拟同步后风险；不是因果效果估计。"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from analyze import ROOT, SPLIT, FAIL_COST, FLAG_COST, grouped, load_data, write_csv
from optimize_sync import MODES, segment

THRESHOLDS = [None, 1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65]
MIN_RULE_SUPPORT = 50
MIN_REFERENCE_SUPPORT = 10


def wilson_upper(failures, n):
    if not n:
        return 1.0
    z = 1.959963984540054
    p = failures / n
    return (p + z*z/(2*n) + z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)


def timely_references(train):
    return [x for x in train if x["failed"] is not None and x["lag_minutes"] <= 1]


def reference(row, timely, min_n=MIN_REFERENCE_SUPPORT):
    # 同一基准模型用于所有阈值策略，避免比较时连参考风险也一起改变。
    for level, predicate in [
        ("team_score_clause", lambda x: segment(x, "team_score_clause") == segment(row, "team_score_clause")),
        ("team_score", lambda x: segment(x, "team_score") == segment(row, "team_score")),
        ("team_clause", lambda x: segment(x, "team_clause") == segment(row, "team_clause")),
        ("team", lambda x: x["team_code"] == row["team_code"]),
        ("pooled", lambda x: True),
    ]:
        z = [x for x in timely if predicate(x)]
        if len(z) >= min_n or (level == "pooled" and z):
            f = sum(x["failed"] for x in z)
            return {"reference_level": level, "reference_n": len(z), "reference_failures": f,
                    "p_timely_fail": f / len(z), "p_timely_fail_upper95": wilson_upper(f, len(z))}
    raise ValueError("训练期没有≤1分钟且有标签的记录，不能构造模拟基准")


def selected(row, threshold):
    return threshold is not None and row["lag_minutes"] > threshold


def cost(rows, predicate, risk_field="p_timely_fail"):
    known = [x for x in rows if x["failed"] is not None]
    flagged = [x for x in known if predicate(x)]
    unflagged = [x for x in known if not predicate(x)]
    observed_failures = sum(x["failed"] for x in known)
    original_flag_failures = sum(x["failed"] for x in flagged)
    expected_flag_failures = sum(x[risk_field] for x in flagged)
    remaining = sum(x["failed"] for x in unflagged) + expected_flag_failures
    total = FLAG_COST * len(flagged) + FAIL_COST * remaining
    unknown_flags = sum(x["failed"] is None and predicate(x) for x in rows)
    return {"n": len(known), "observed_failures": observed_failures, "flagged": len(flagged),
            "original_flag_failures": original_flag_failures,
            "unflagged_observed_failures": sum(x["failed"] for x in unflagged),
            "simulated_flag_failures": expected_flag_failures,
            "simulated_total_failures": remaining, "flag_hours": FLAG_COST * len(flagged),
            "simulated_failure_hours": FAIL_COST * remaining, "simulated_total_hours": total,
            "baseline_hours": FAIL_COST * observed_failures,
            "simulated_net_hours": FAIL_COST * observed_failures - total,
            "unknown_flags": unknown_flags,
            "net_after_unknown_flag_cost_only": FAIL_COST * observed_failures - total - FLAG_COST * unknown_flags}


def best(rows, risk_field):
    trials = []
    for threshold in THRESHOLDS:
        c = cost(rows, lambda x, t=threshold: selected(x, t), risk_field)
        rank = (round(c["simulated_total_hours"], 8), c["flagged"], threshold is not None, threshold or 0)
        trials.append((rank, threshold))
    return min(trials, key=lambda x: x[0])[1]


def fit(train, mode, risk_field, min_support=MIN_RULE_SUPPORT):
    known = [x for x in train if x["failed"] is not None]
    global_t = best(known, risk_field)
    team_t = {}
    for team in sorted({x["team_code"] for x in known}):
        z = [x for x in known if x["team_code"] == team]
        team_t[team] = best(z, risk_field) if len(z) >= min_support else global_t
    groups = defaultdict(list)
    for x in known:
        groups[segment(x, mode)].append(x)
    rules, details = {}, []
    for key, z in sorted(groups.items()):
        own = mode == "global" or len(z) >= min_support
        threshold = best(z, risk_field) if own else team_t[z[0]["team_code"]]
        rules[key] = threshold
        details.append({"mode": mode, "risk_scenario": risk_field, "segment": " / ".join(key),
                        "train_n": len(z), "threshold_minutes": threshold if threshold is not None else "disabled",
                        "source": "segment" if own else "team_fallback"})
    return {"mode": mode, "global_threshold": global_t, "team_thresholds": team_t, "rules": rules}, details


def decide(row, model):
    if row["team_code"] not in model["team_thresholds"]:
        return False
    t = model["rules"].get(segment(row, model["mode"]), model["team_thresholds"][row["team_code"]])
    return selected(row, t)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/verification_logs.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/timely_simulation")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rows, audit = load_data(args.input)
    timely = timely_references([x for x in rows if x["local_time"] < SPLIT])
    for x in rows:
        x.update(reference(x, timely))
    train = [x for x in rows if x["local_time"] < SPLIT]
    test = [x for x in rows if x["local_time"] >= SPLIT]
    comparison, rules, sensitivity, models = [], [], [], {}
    for risk in ("p_timely_fail", "p_timely_fail_upper95"):
        for mode in MODES:
            model, details = fit(train, mode, risk)
            models[(risk, mode)] = model
            rules.extend(details)
            for window, data in [("train", train), ("test", test), ("all_descriptive", rows)]:
                comparison.append({"risk_scenario": risk, "mode": mode, "window": window,
                                   **cost(data, lambda x: decide(x, model), risk)})
    for min_n in (20, 50, 100):
        for mode in MODES:
            model, _ = fit(train, mode, "p_timely_fail", min_n)
            sensitivity.append({"min_rule_support": min_n, "mode": mode,
                                **cost(test, lambda x: decide(x, model))})
    # 对同一经验风险拟合策略固定阈值，仅改变模拟风险，区分效果恶化与重新优化。
    fixed_sensitivity = []
    for mode in MODES:
        model = models[("p_timely_fail", mode)]
        for risk in ("p_timely_fail", "p_timely_fail_upper95"):
            fixed_sensitivity.append({"mode": mode, "evaluation_risk": risk,
                                      **cost(test, lambda x: decide(x, model), risk)})
    out = args.out
    write_csv(out / "cost_comparison.csv", comparison)
    write_csv(out / "thresholds.csv", rules)
    write_csv(out / "rule_support_sensitivity.csv", sensitivity)
    write_csv(out / "fixed_policy_reference_sensitivity.csv", fixed_sensitivity)
    write_csv(out / "reference_rates_by_team.csv", grouped(timely, "team_code", lambda x: x["team_code"]))
    write_csv(out / "reference_assignments.csv", [
        {"item_id": x["item_id"], "team_code": x["team_code"], "clause_type": x["clause_type"],
         "match_score": x["match_score"], **{k: x[k] for k in ("reference_level", "reference_n", "reference_failures", "p_timely_fail", "p_timely_fail_upper95")}}
        for x in rows])
    protocol = {"timely_definition": "lag_minutes <= 1", "reference_window": "2026-09-01 to 2026-09-05",
                "reference_n": len(timely), "reference_failures": sum(x["failed"] for x in timely),
                "threshold_candidates": THRESHOLDS, "min_reference_support": MIN_REFERENCE_SUPPORT,
                "min_rule_support": MIN_RULE_SUPPORT, "raw_sha256": audit["sha256"],
                "claim": "observational reference scenario, not an intervention effect or guarantee"}
    (out / "protocol.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 以≤1分钟同步为基准的成本模拟", "", "`python3 simulate_timely.py` 重建。基准率和阈值都只用前五天拟合。", "",
             "假设：标记记录及时同步后达到匹配历史及时同步样本的失效率，不能解释成实测干预效果。", "",
             "|基准情景|策略|窗口|标记|标记工时|模拟失效工时|模拟总成本|模拟净减少工时|", "|---|---|---|---:|---:|---:|---:|---:|"]
    for x in comparison:
        if x["window"] == "test":
            lines.append(f"|{x['risk_scenario']}|{x['mode']}|test|{x['flagged']}|{x['flag_hours']}|{x['simulated_failure_hours']:.2f}|{x['simulated_total_hours']:.2f}|{x['simulated_net_hours']:.2f}|")
    lines += ["", "风险上界用及时样本失效率的Wilson双侧95%区间上端，仅作保守风险情景，不是政策收益置信区间。", "",
              "0次失效不证明成功率必为100%；原始经验率与保守情景都完整保留。", "",
              "样本不足的细分阈值回退团队，及时样本基准按固定层级回退，详见reference_assignments.csv。", "",
              "此前已查看全量统计，后两天为探索性时间回放；不据其结果挑选支持门槛。"]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(protocol, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
