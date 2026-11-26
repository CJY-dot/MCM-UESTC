# MCM-UESTC · B 成员建模交接

**阶段 02 · B 题炼钢厂钢包周转与排产优化。** 由 A 代完成 B 的独立建模交付，工作分支 `stage02/b`。建模基线 `b06c8d04ea05668eebceb351147b29433a963b23`，为启动时的 `origin/main`。独立初稿先提交为 `cc4126ed5e60c541bb7c09186bdc2e994d2fb08d`，随后对照 A 的方案。启动时远端未见 C 的建模分支，C 的实际基线请以团队确认记录为准。

## 建议先读

| 交付 | 用途 |
| --- | --- |
| [讨论简报](B_discussion_brief.md) | 推荐路线、实测结论和 A/C 需要接手的判断 |
| [模型方案](B_model_proposal.md) | 从生产事件链到三场景的变量、约束、目标、求解步骤及与 A 的对照 |
| [数据审计](B_data_audit.md) | 12 份工作簿、字段语义、身份消歧、覆盖及候选腿入模资格 |
| [参数摘要](B_parameter_summary.md) | 历史汇总、分布留出、共同样本对照和故障注入条件 |
| [预处理脚本](B_preprocess.py) | 建立带原表来源、类型、缺失与冲突标记的事件和运输表 |
| [短窗原型](B_probe.py) | 重新生成分布比较、同扰动策略对照、运动回放和故障前缀检查 |

## 已完成与实测

- 全量读取 12 份工作簿的 6,758 行。形成 58,049 条事件观测、9,040 条候选腿及 8,868 个校准样本。有效计划 1,266 份，其中 1,051 份匹配到选定的兼容主表行。保留所有附带时序、身份、重量与站点缺口。候选腿进入正式求解前要按数据审计补齐物理可吊、窗口和资格。
- 同义的连铸计划与实际正历时可用于检验均值锚定。LF 设备历时做了按日留出的经验、Gamma 和对数正态对照。共同样本原型复用 LF 训练池的波动形状，以明示情景量转为就绪扰动。
- 历史四任务窗内，Q1 两规则均完成 4 项运输、3 项按时并通过连续轨迹回放。Q2 三个种子、每设定每种子 200 对，未测得策略差异。合成竞争样例验证两规则确实会做出不同选择。Q3 长时注入下已完成首项保持不变，仍完成 4 项运输，按时数从 3 降到 2。

## 复现

在仓库根目录激活团队 `mcm-modeling` 环境。以下命令可从干净克隆运行：

~~~powershell
conda activate mcm-modeling
python -B stages/02_modeling/B/B_probe.py --seed 20261126 --samples 200
~~~

默认读取[相对时间算例](../../01_topic_selection/validation/b_crane_dispatch/problem/case_relative.json)和[合成校准表](fixtures/calibration_synthetic.csv)，生成 `problem/data/processed/stage02_b/probe_fixture_summary.json`。Q1 和 Q3 的原型指标与历史报告一致。Q2 会真实执行留出、拟合及同样本策略比较，校准样本与该次随机输出属于合成验证。

要复现 [参数摘要](B_parameter_summary.md) 中的真实参数与 Q1–Q3 历史数字，使用授权的 B 题 12 份工作簿：

~~~powershell
python -B stages/02_modeling/B/B_preprocess.py
python -B stages/02_modeling/B/B_probe.py --calibration problem/data/processed/stage02_b/calibration.csv --seed 20261126 --samples 200
~~~

第二条仍使用共享算例，所需生产信息从第一条重新生成。原始附件放在其他位置时，给预处理传 `--raw <附件目录>`。`--output` 接受 `problem/data/processed/` 内的独立子目录。原型可用 `--case` 指定原有授权算例，用 `--out` 选择本机输出。PowerShell 无法激活 Conda 时直接调用该环境的解释器。

预处理默认在 `problem/data/processed/stage02_b/` 生成 `audit.json`、`events.csv`、`tasks.csv` 和 `calibration.csv`。授权复跑的原型结果写入同目录下的 `probe_replay_summary.json`，保留原历史文件 `probe_summary.json`。审计 JSON 记录源工作簿、脚本及 Python 包版本，原型 JSON 记录输入模式、SHA-256、假设、种子和结果。逐炉次数据留在本机。

## 接手时先确认

- **A 的接口。** 两套模型对事件链、连续安全和滚动恢复的结构一致。采用 A 的完整预约、预调位和分项等待定义，接入 B 的来源标记与统计校验。审查候选腿如何变成调度输入，避免用历史起吊代替所有物理可吊时点。
- **C 的审查。** 确认连续净距计算、同批模拟的因果信息边界、未交任务分母和跨日留出。对场景三优先核对轨道上的故障车辆、非中断动作及恢复后的继承状态。
- **参数边界。** 速度档、加速度、特殊限速区、合法待避点、完整净距、收包提前量、设备映射、实际故障率和修复时间仍待确认。当前四任务算例使用归一化布局和明示参数，诊断回放可送达迟到任务。全厂调度、并行运输、轨道故障回放及窗口级 MILP 属下一阶段实施和核验。
