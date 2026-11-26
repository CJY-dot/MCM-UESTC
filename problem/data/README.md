# 数据与复现入口

| 路径 | 用途 |
| --- | --- |
| `docs/校赛题目/`、`problem/data/raw/` | 本机授权原始输入。现有 B 题附件继续从 `docs/校赛题目/` 读取 |
| `problem/data/processed/` | 脚本生成的本机数据。事件、运输任务和校准表可能包含炉次、精确时刻以及源表位置，因此整个目录按本机数据管理 |
| [B 题相对时间算例](../../stages/01_topic_selection/validation/b_crane_dispatch/problem/case_relative.json) | 随仓库共享。用于短验证与 Q1–Q3 原型，保留相对时差、容量档位和归一化布局 |
| [合成校准表](../../stages/02_modeling/B/fixtures/calibration_synthetic.csv) | 随仓库共享。用于检查 Q2 统计与模拟流程，样本均为合成数据 |

[短验证来源清单](../../stages/01_topic_selection/validation/source_manifest.json)记录原附件路径和 SHA-256。B 题完整历史校准需从授权工作簿重建，预处理生成的审计文件会记录这 12 份工作簿的 SHA-256。A 题原附件缺失时，可查阅 Git 中的已归档结果。复跑入口见[短验证说明](../../stages/01_topic_selection/validation/README.md)和 [B 阶段交接](../../stages/02_modeling/B/B_HANDOFF.md)。
