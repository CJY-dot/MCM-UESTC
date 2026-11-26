# 定题前短验证

核验日期为 2026-11-26。目标是判断能否交付一条可信的全题建模路线。工作区按候选题隔离，保留原始附件、个人报告及正式赛题目录。

1. 先验证 B 题的确定性场景。锁定一个同浇次的真实计划和与之重叠的空包回程。记录来源、时间口径和轨道假设。采用小规模的轨迹构造和独立约束检查，检验吊重、到位窗口、空包前序及全程不跨越。
2. 用历史可观察量限定扰动，再改变未知的轨道布局、行程时间及座包提前量。只有能在可辩护的布局上通过检查，才把 B 题列为可推荐。条件模型有解和实际现场可实施分开判断。
3. 若 B 的关键几何仍未知且结果对合理扰动翻转，再验证 A 题。仅检查墙体与金属板的留出测距、一行天线的墙体 2D 剖面。A 同时通过两项才建议改选。

各题的 `problem/` 存放算例或配置，`code/` 存放脚本，`results/` 与 `figures/` 保存验证证据。阶段结论见[选题决定](../topic_decision.md)和[短验证总结](VALIDATION_SUMMARY.md)。

## B 题复跑

[相对时间算例](b_crane_dispatch/problem/case_relative.json)保留求解所用的相对时差、容量资格和归一化布局。炉次、浇次、原表行号、绝对日期时刻和实际吊重均留在原始本机算例。共享算例的吨位是同一容量档位的代表值。与历史输出对比，两种车型规则各自的 36 个情景分类、名义轨迹、等待及运行指标均一致。

在项目根目录激活 `mcm-modeling` 后运行：

~~~powershell
python -B stages/01_topic_selection/validation/b_crane_dispatch/code/run_pilot.py
python -B stages/01_topic_selection/validation/b_crane_dispatch/code/run_pilot.py --heavy-only-empty
~~~

输出在 B 短验证工作区的 `results/replay/relative_fixture/`。持有授权原算例时，可显式传入 `--case <原始 case.json 路径>`，输出写入 `results/replay/authorized_original/`。两个目录按生成物忽略，历史输出可保留在本机原位置。阶段二 Q1–Q3 的复跑入口见 [B 交接](../../02_modeling/B/B_HANDOFF.md)。

## A 题复跑

[雷达配置](a_radar_echo/problem/pilot.json)、代码、两套 JSON 结果、逐组测距 CSV 和两幅剖面图都已纳入 Git。重新计算需要原始雷达附件，当前工作区缺少该附件。取得授权原件后，按[来源与校验清单](source_manifest.json)中的相对路径和 SHA-256 核对，再从 A 短验证工作区运行：

~~~powershell
python -B code/run_pilot.py
python -B code/run_pilot.py --gain
~~~
