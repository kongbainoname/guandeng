"""Token关联、分层及离线增量对照。Token不是核验前已知变量。"""
import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from analyze import ROOT, SPLIT, grouped, load_data, write_csv
from simulate_timely import best, cost, decide, fit, reference, selected, timely_references


def high_token(row):
    return row["token_usage"] > 65


def token_best(train):
    trials = []
    for threshold in [None] + list(range(0, 126, 5)):
        result = cost(train, lambda x, t=threshold: t is not None and x["token_usage"] > t)
        trials.append({"threshold": threshold, **result})
    best_row = min(trials, key=lambda x: (x["simulated_total_hours"], x["flagged"], x["threshold"] is not None))
    return best_row["threshold"], trials


def add_token_fit(train, base_mode, min_support=50):
    base, _ = fit(train, base_mode, "p_timely_fail", min_support)
    groups = defaultdict(list)
    for row in train:
        if row["failed"] is not None:
            groups[(row["team_code"], high_token(row), row["match_score"] < 5 if base_mode == "team_score" else None)].append(row)
    rules, details = {}, []
    for key, rows in sorted(groups.items()):
        own = len(rows) >= min_support
        if own:
            rules[key] = best(rows, "p_timely_fail")
        details.append({"base_mode": base_mode, "min_support": min_support, "team_code": key[0],
                        "token_gt65": key[1], "score_lt5": key[2], "train_n": len(rows),
                        "source": "own" if own else "base_fallback",
                        "threshold": (rules[key] if rules[key] is not None else "disabled") if own else "fallback"})

    def predicate(row):
        key = (row["team_code"], high_token(row), row["match_score"] < 5 if base_mode == "team_score" else None)
        return selected(row, rules[key]) if key in rules else decide(row, base)
    return predicate, details


def standardized(rows, dimensions):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(dim(row) for dim in dimensions)].append(row)
    high_sum = low_sum = weight = overlap = 0
    for group in groups.values():
        high = [x for x in group if high_token(x)]
        low = [x for x in group if not high_token(x)]
        if high and low:
            w = len(group)
            high_sum += w * sum(x["failed"] for x in high) / len(high)
            low_sum += w * sum(x["failed"] for x in low) / len(low)
            weight += w
            overlap += 1
    return {"overlap_strata": overlap, "covered_records": weight,
            "standardized_high_failure_rate": high_sum / weight if weight else None,
            "standardized_other_failure_rate": low_sum / weight if weight else None,
            "difference_pp": 100 * (high_sum - low_sum) / weight if weight else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/verification_logs.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/token")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rows, audit = load_data(args.input)
    timely = timely_references([x for x in rows if x["local_time"] < SPLIT])
    for x in rows:
        x.update(reference(x, timely))
    train = [x for x in rows if x["local_time"] < SPLIT]
    test = [x for x in rows if x["local_time"] >= SPLIT]
    known = [x for x in rows if x["failed"] is not None]
    lag_key = lambda x: "lag>15" if x["lag_minutes"] > 15 else "lag<=15"
    for name, key in {
        "team_token": lambda x: x["team_code"] + (" / token>65" if high_token(x) else " / token<=65"),
        "clause_token": lambda x: x["clause_type"] + (" / token>65" if high_token(x) else " / token<=65"),
        "team_lag_token": lambda x: x["team_code"] + " / " + lag_key(x) + (" / token>65" if high_token(x) else " / token<=65"),
    }.items():
        write_csv(args.out / f"{name}.csv", grouped(rows, name, key))
    standards = []
    for name, dims in [("unadjusted", [lambda x: "all"]), ("lag", [lag_key]),
                       ("team_lag", [lambda x: x["team_code"], lag_key]),
                       ("team_clause_lag", [lambda x: x["team_code"], lambda x: x["clause_type"], lag_key])]:
        standards.append({"stratification": name, **standardized(known, dims)})
    write_csv(args.out / "standardized_association.csv", standards)
    threshold, trials = token_best(train)
    write_csv(args.out / "token_threshold_search_train.csv", trials)
    team, _ = fit(train, "team", "p_timely_fail")
    score, _ = fit(train, "team_score", "p_timely_fail")
    added_team, details1 = add_token_fit(train, "team")
    added_score, details2 = add_token_fit(train, "team_score")
    predicates = [("no_alert", lambda x: False), ("token_gt65", high_token),
                  ("token_threshold_train", lambda x: threshold is not None and x["token_usage"] > threshold),
                  ("team_sync", lambda x: decide(x, team)), ("team_token_sync", added_team),
                  ("team_score_sync", lambda x: decide(x, score)), ("team_score_token_sync", added_score)]
    comparisons = []
    for name, predicate in predicates:
        for window, data in [("train", train), ("test", test)]:
            m = cost(data, predicate)
            comparisons.append({"strategy": name, "window": window,
                                "false_flags": m["flagged"] - m["original_flag_failures"], **m})
    write_csv(args.out / "strategy_comparison.csv", comparisons)
    write_csv(args.out / "added_token_rules.csv", details1 + details2)
    sensitivity = []
    for minimum in (20, 50, 100):
        for base_mode in ("team", "team_score"):
            predicate, _ = add_token_fit(train, base_mode, minimum)
            sensitivity.append({"min_support": minimum, "base_mode": base_mode, **cost(test, predicate)})
    write_csv(args.out / "support_sensitivity.csv", sensitivity)
    stats = {"raw_sha256": audit["sha256"], "high_token_definition": ">65",
             "token_threshold_selected_train": threshold,
             "mean_lag_high_token": statistics.mean(x["lag_minutes"] for x in known if high_token(x)),
             "mean_lag_other_token": statistics.mean(x["lag_minutes"] for x in known if not high_token(x)),
             "pearson_token_lag": statistics.correlation([x["token_usage"] for x in known], [x["lag_minutes"] for x in known]),
             "availability": "token_usage is measured during/after current check; retrospective only"}
    (args.out / "protocol.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    lines = ["# Token增量研究", "", "运行`python3 analyze_token.py`重建。只用发放数据；阈值与规则仅用前五天拟合。", "",
             "Token>65为此前探索分组，不能解释为模型异常。模拟沿用≤1分钟历史参考率，未换干预动作或参考人群。", "",
             "|策略|后两天标记|覆盖失效|误标|漏失效|模拟总成本|模拟净收益|", "|---|---:|---:|---:|---:|---:|---:|"]
    for x in comparisons:
        if x["window"] == "test":
            lines.append(f"|{x['strategy']}|{x['flagged']}|{x['original_flag_failures']}|{x['false_flags']}|{x['unflagged_observed_failures']}|{x['simulated_total_hours']:.2f}|{x['simulated_net_hours']:.2f}|")
    lines += ["", "标准化仅在高低Token都有记录的层内采用共同样本权重；细分可能丢弃无重叠层，不能把不同覆盖人群的数值直接当成同一总体的因果效果。", "",
              "分层说明关联，成本比较说明指定参考率假设下的离线策略表现，都不能证明Token消耗导致失效。", "",
              "Token为本次推理消耗，不能直接用于本次核验开始前预警；未部署Token策略，也不将核验后的人工复核成本套用待同步10工时。", "",
              "高Token不等于异常或复杂度。缺少文档长度、模型配置、重试和输入/输出Token明细，尚无法定位具体机制。"]
    (args.out / "summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
