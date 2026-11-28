# Stage 03-Q2-B · 数据与参数交接

本次交接基线为 Q1 C 验收合并提交 `56736cff4905914e11287253eac5a06cca15587c`。Q1 的模型、任务集合、结果和 `mcm-uestc.dispatch/1` 均沿用。Q2 文件是独立版本化补充包。A 的调度实现仍待负责人审查合并后启动。

## 接入顺序

| 产物 | 版本及角色 | A 的使用方式 |
| --- | --- | --- |
| `q2_calibration.json` | `mcm-uestc.q2-calibration/1`，授权仿真侧 | 按 `strata` 查训练期分布参数，经验捐赠按权重取样，`model_selection` 固定验证选型。真实样本引用和捐赠值留本机 |
| `q2_policy_parameters.json` | `mcm-uestc.q2-policy-parameters/1`，策略侧 | 读取训练尺度、预测分位和非负缓冲。按层选择已冻结的预测族。字段 `paired_plan_ratio` 乘计划均值，`historical_seconds` 直接使用秒 |
| `q2_motion_profile.json` | `mcm-uestc.q2-motion-profile/1`，条件 SI 运动输入 | 选五车共同米制轨道、起态、载荷档位、加减速、停车点、净距和区段限速。分段运动与内部极值约束沿用统一模型 |
| `q2_external_boundary.json` | `mcm-uestc.q2-external-boundary/1`，外部边界证据 | 上游计划前序、翻渣历时、接收和回程按已核实条件推进。待核点按原阻塞状态或获准条件设置 |
| `q2_window_manifest.json` | `mcm-uestc.q2-window-manifest/1`，公开覆盖摘要 | 供窗口选取和限制记录。正式现场竞争窗口清单为空，候选对只用于条件实验 |
| `q2_audit.json` | `mcm-uestc.q2-audit/1`，授权核查侧 | 输入 SHA、统计分割及评估、原有版本、代码和策略包校验。留出数据不进入策略接口 |
| `q2_parameter_pack.json` | `mcm-uestc.q2-parameter-pack/1`，公开入口 | 训练预测、验证选定分布名字、运动和外部条件摘要。联接字段按文件版本解释 |

调用时仍用 Q1 的规范任务、工位、前序和策略可见状态。Q2 参数按工序、设备、路线键联接，未匹配时执行包内回退顺序。模型派生的开始、完成、就绪、接收经同一事件图推进。由于历史边界缺公告、可见时点及独立外生开工证据，本包没有启用独立时点偏差模型。翻渣正历时与渡车释放须分别处理，保留待核回炉链和 Q1 窗末持有。

## 随机键和信息边界

独立样本使用 `replicate_id + heat_id + process_stage + event_id + device_id`。运行种子置于该元组前，按无空格分隔的 UTF-8 JSON 输入 SHA-256，取前 16 字节的大端整数。实现与固定测试向量在 `q2_schema.py` 和公开包中。冻结的场景下，两种策略读取相同的仿真实现。块抽样先固定组键及组内事件位置，同一块只选一次。

策略可读取已公开计划、已公告且适用的状态、训练期预测和冻结的选择配置。捐赠池、留出明细、真实未来事件和仿真实现值只由授权仿真侧持有。一个事件的先前发生记录不能替代公告时刻。Q2-B 只准备参数与规则，本次未生成调度实验的随机样本。

## 窗口与批准项

复核八个 Q1 候选。Q1W01 与 Q1W06 共享 5 炉，不能计成两份独立证据。Q1W01 与 Q1W07 的炉次、浇次和时间分离，具备经核对的准备代理重叠及共享资源候选，列为条件调查对。已核查资料缺少这些窗口的实际可吊、资格及公告证据，无法确认两个现场真实就绪竞争窗口。缺证结论与代理统计由公开清单分别记录。

米制尺度、分档运动、岔区或倒渣接近区限速、五车初态、停车位和安全裕量均按证据标记。冻结的条件配置待负责人批准后用于 Q2 同口径比较。现场运动许可、实测车体包络、绝对尺度、真正可吊、供钢缓冲及外部接收释放仍需结论前补核。现有侧车包无需改动 Q1 正式接口。如 A 发现主接口无法承载必要状态，再报独立方案审查。

## 复现及文件校验

校准程序只读批准版预处理成果和授权附件。使用项目 Python，从根目录运行。命令默认选唯一 B 附件目录，也可用 `--raw` 明确指定。输出路径应为新的 Git 忽略目录。

```powershell
python -B code/data/q2_calibration.py --output problem/data/processed/stage03_q2_b_reproduction --delivery local/stage03_q2_b/reproduction
python -B -m unittest discover -s code/data/tests -p "test_q2_calibration.py"
python -B -m unittest discover -s code/data/tests -p "test_q1*.py"
python -B -m unittest discover -s code/dispatch/tests
python -B experiments/smoke.py
python -B .agents/skills/mathodology-workbench/scripts/mtd.py check --no-sync
```

授权处理目录保存全部来源、样本账本及窗口筛选明细，授权交付目录保存六个标准侧车 JSON。两处均生成 `SHA256SUMS.txt`。公开复现仅分享聚合、假设及代码。 [校准报告](Q2_CALIBRATION_REPORT.md) · [动力学假设登记](Q2_MOTION_ASSUMPTIONS.md) · [公开参数包](q2_parameter_pack.json) · [窗口清单](q2_window_manifest.json)

本轮 Q2 测试 7 项、Q1-B 测试 47 项、Q1-A 测试 19 项，Smoke 与 Workbench 静态检查均通过。44 组 Q1 排程的 67 份确定性 JSON 与原归档逐字节一致。Q2 两次生成的授权处理及交付文件逐字节一致。公开清单按仓库根目录的相对路径和 Git 的 LF 字节校验。 [公开交付 SHA-256](Q2_SHA256SUMS.txt)
