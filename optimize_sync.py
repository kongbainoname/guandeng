"""其他因素诊断与同步成本优化；仅标准库，不估计同步因果效果。"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from analyze import ROOT, SPLIT, FAIL_COST, FLAG_COST, grouped, load_data, write_csv

# None表示不标记；-1表示该适用组全部标记；其余为同步间隔阈值。
THRESHOLDS = [None, -1] + list(range(0, 66, 5))
MIN_SUPPORT = 50  # 人为防稀疏门槛，非显著性结论。
EXCLUDED_TEAMS = {"医疗组"}  # 未证实低分失效可由同步修复，保留原审查。
MODES = ("global", "team", "team_score", "team_clause", "team_score_clause")


def segment(row, mode):
    key = [] if mode == "global" else [row["team_code"]]
    if "score" in mode:
        key.append("score<5" if row["match_score"] < 5 else "score>=5")
    if "clause" in mode:
        key.append(row["clause_type"])
    return tuple(key) or ("all_eligible",)


def flagged(row, threshold):
    return threshold is not None and row["lag_minutes"] > threshold


def cost(rows, predicate, q):
    if not 0 <= q <= 1:
        raise ValueError("q必须在0与1之间")
    known = [x for x in rows if x["failed"] is not None]
    chosen = [x for x in known if predicate(x)]
    failures = sum(x["failed"] for x in known)
    caught = sum(x["failed"] for x in chosen)
    mark_cost = FLAG_COST * len(chosen)
    remaining = FAIL_COST * (failures - q * caught)
    baseline = FAIL_COST * failures
    unknown_flags = sum(x["failed"] is None and predicate(x) for x in rows)
    return {"n": len(known), "failures": failures, "flagged": len(chosen),
            "caught_failures": caught, "false_flags": len(chosen) - caught,
            "missed_failures": failures - caught, "baseline_hours": baseline,
            "flag_hours": mark_cost, "expected_remaining_failure_hours": remaining,
            "expected_total_hours": mark_cost + remaining,
            "net_hours": baseline - mark_cost - remaining,
            "break_even_q": mark_cost / (FAIL_COST * caught) if caught else None,
            "unknown_flags": unknown_flags,
            "net_hours_after_unknown_flag_cost_only": baseline - mark_cost - remaining - FLAG_COST * unknown_flags}


def best_threshold(rows, q):
    if not rows:
        return None
    options = []
    for t in THRESHOLDS:
        m = cost(rows, lambda x, t=t: flagged(x, t), q)
        # 无效果时不标记；相同成本减少打扰；最后取较小有限阈值。
        rank = (m["expected_total_hours"], m["flagged"], t is not None, t if t is not None else 0)
        options.append((rank, t))
    return min(options, key=lambda x: x[0])[1]


def fit(train, mode, q, min_support=MIN_SUPPORT):
    available = [x for x in train if x["failed"] is not None and x["team_code"] not in EXCLUDED_TEAMS]
    global_t = best_threshold(available, q)
    teams = {}
    for team in sorted({x["team_code"] for x in available}):
        z = [x for x in available if x["team_code"] == team]
        teams[team] = best_threshold(z, q) if len(z) >= min_support else global_t
    buckets = defaultdict(list)
    for x in available:
        buckets[segment(x, mode)].append(x)
    rules = {}
    details = []
    for key, z in sorted(buckets.items()):
        supported = mode == "global" or len(z) >= min_support
        t = best_threshold(z, q) if supported else teams.get(z[0]["team_code"], global_t)
        rules[key] = t
        details.append({"mode": mode, "q_assumed": q, "min_support": min_support,
                        "segment": " / ".join(key), "train_n": len(z),
                        "train_failures": sum(x["failed"] for x in z),
                        "own_threshold_supported": supported,
                        "source": "segment" if supported else "team_fallback",
                        "threshold_minutes": t if t is not None else "disabled"})
    return {"mode": mode, "q": q, "global_threshold": global_t,
            "team_thresholds": teams, "rules": rules}, details


def decide(row, model):
    if row["team_code"] in EXCLUDED_TEAMS or row["team_code"] not in model["team_thresholds"]:
        return False
    t = model["rules"].get(segment(row, model["mode"]), model["team_thresholds"][row["team_code"]])
    return flagged(row, t)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/verification_logs.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/factors")
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    rows, audit = load_data(args.input)
    train = [x for x in rows if x["local_time"] < SPLIT]
    test = [x for x in rows if x["local_time"] >= SPLIT]
    factor_keys = {
        "team": lambda x: x["team_code"],
        "clause": lambda x: x["clause_type"],
        "score_band": lambda x: "30" if x["match_score"] == 30 else f"{x['match_score']//5*5:02d}-{x['match_score']//5*5+4:02d}",
        "token_band": lambda x: next(label for upper, label in [(39,"00-39"),(48,"40-48"),(57,"49-57"),(65,"58-65"),(float('inf'),"66+")] if x["token_usage"] <= upper),
        "team_score": lambda x: x["team_code"] + (" / score<5" if x["match_score"] < 5 else " / score>=5"),
        "team_lag": lambda x: x["team_code"] + (" / lag>15" if x["lag_minutes"] > 15 else " / lag<=15"),
        "clause_lag": lambda x: x["clause_type"] + (" / lag>15" if x["lag_minutes"] > 15 else " / lag<=15"),
        "token_lag": lambda x: ("lag>15" if x["lag_minutes"] > 15 else "lag<=15") + (" / token>65" if x["token_usage"] > 65 else " / token<=65"),
    }
    for name, key in factor_keys.items():
        write_csv(out / f"{name}.csv", grouped(rows, name, key))
    comparisons, rules, sensitivities = [], [], []
    for q in (0, .25, .5, .75, 1):
        for mode in MODES:
            model, details = fit(train, mode, q)
            rules.extend(details)
            for window, data in [("train", train), ("test", test), ("all_descriptive", rows)]:
                comparisons.append({"window": window, "mode": mode, "q_assumed": q,
                                    **cost(data, lambda x: decide(x, model), q)})
    # 支持门槛敏感性，不据后两天结果选择门槛或策略。
    for support in (20, 50, 100):
        for mode in MODES:
            model, details = fit(train, mode, .5, support)
            sensitivities.append({"min_support": support, "mode": mode,
                                  "supported_segments": sum(x["own_threshold_supported"] for x in details),
                                  "observed_segments": len(details),
                                  **cost(test, lambda x: decide(x, model), .5)})
    write_csv(out / "cost_comparison.csv", comparisons)
    write_csv(out / "thresholds.csv", rules)
    write_csv(out / "support_sensitivity.csv", sensitivities)
    selected, _ = fit(train, "team", .5)
    artifact = {"status": "offline_scenario_not_causal_estimate", "q_assumed": .5,
                "min_support": MIN_SUPPORT, "excluded_teams": sorted(EXCLUDED_TEAMS),
                "train_before": SPLIT.isoformat(), "team_thresholds": selected["team_thresholds"],
                "threshold_candidates": THRESHOLDS, "raw_sha256": audit["sha256"]}
    (out / "optimization.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 其他因素与同步成本优化", "", "运行 `python3 optimize_sync.py` 重建。全部效果是描述性关联，q为假设。", "",
             "## q=50%、支持门槛50的时间回放", "", "|策略|窗口|标记|命中失效|标记成本|剩余失效成本|总成本|净减少工时|", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for x in comparisons:
        if x["q_assumed"] == .5 and x["window"] in ("train", "test"):
            lines.append(f"|{x['mode']}|{x['window']}|{x['flagged']}|{x['caught_failures']}|{x['flag_hours']}|{x['expected_remaining_failure_hours']:g}|{x['expected_total_hours']:g}|{x['net_hours']:g}|")
    lines += ["", "医疗组仅作风险诊断，未纳入同步优化；未知小组不自动纳入。", "",
              "分数<5分桶来自此前探索，不是预注册。细分样本不足50条时回退到团队阈值。", "",
              "所有策略仅在前五天拟合；初始探索看过全量，后两天不是盲测。", "",
              "最优指有限候选集合中给定q、给定样本的经验成本最小，不代表真实最优提前分钟数。", "",
              "本分析未调整生产分流工具的字段依赖；match_score和条款细分仅作离线候选比较。"]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(artifact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
