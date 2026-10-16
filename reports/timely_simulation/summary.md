# 以≤1分钟同步为基准的成本模拟

`python3 simulate_timely.py` 重建。基准率和阈值都只用前五天拟合。

假设：标记记录及时同步后达到匹配历史及时同步样本的失效率，不能解释成实测干预效果。

|基准情景|策略|窗口|标记|标记工时|模拟失效工时|模拟总成本|模拟净减少工时|
|---|---|---|---:|---:|---:|---:|---:|
|p_timely_fail|global|test|93|930|1266.92|2196.92|1763.08|
|p_timely_fail|team|test|81|810|1144.62|1954.62|2005.38|
|p_timely_fail|team_score|test|74|740|1080.00|1820.00|2140.00|
|p_timely_fail|team_clause|test|81|810|1144.62|1954.62|2005.38|
|p_timely_fail|team_score_clause|test|81|810|1144.62|1954.62|2005.38|
|p_timely_fail_upper95|global|test|10|100|3893.26|3993.26|-33.26|
|p_timely_fail_upper95|team|test|43|430|3096.23|3526.23|433.77|
|p_timely_fail_upper95|team_score|test|40|400|3235.68|3635.68|324.32|
|p_timely_fail_upper95|team_clause|test|43|430|3096.23|3526.23|433.77|
|p_timely_fail_upper95|team_score_clause|test|43|430|3096.23|3526.23|433.77|

风险上界用及时样本失效率的Wilson双侧95%区间上端，仅作保守风险情景，不是政策收益置信区间。

0次失效不证明成功率必为100%；原始经验率与保守情景都完整保留。

样本不足的细分阈值回退团队，及时样本基准按固定层级回退，详见reference_assignments.csv。

此前已查看全量统计，后两天为探索性时间回放；不据其结果挑选支持门槛。
