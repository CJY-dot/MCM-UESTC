# Stage 03-Q2-A · 实施交接

本阶段使用已合并 PR #9 的 `6d8aa34`。项目负责人已批准 B 的动力学参数用于条件仿真。Q1 输入格式和已验收结果沿用，Q2 通过 B 的六份版本化侧车接入。原始工作簿、身份、逐炉次结果和训练捐赠池继续保留在本机。 [建模决策](Q2_MODEL_DECISIONS.md)

## 准备输入并复现

从仓库根目录使用项目环境。以下命令使用已批准身份更正后的输入。若目标目录已生成，核对其输入 SHA-256 后复用，或为重建指定新目录。

```powershell
python -B code/data/q1_builder.py --processed problem/data/processed/stage02_b_ladle_identity_approved --primary-window Q1W01 --output problem/data/processed/stage03_a_q2_w01
python -B code/data/q1_builder.py --processed problem/data/processed/stage02_b_ladle_identity_approved --primary-window Q1W07 --output problem/data/processed/stage03_a_q2_w07
python -B code/dispatch/run_q2.py --pilot --output local/stage03_a_q2/reproduction_pilot
python -B code/dispatch/run_q2.py --output local/stage03_a_q2/reproduction --workers 3
python -B -m unittest discover -s code/dispatch/tests -p "test_q2*.py" -v
python -B -m unittest discover -s code/dispatch/tests -p "test_q1_dispatch.py"
python -B -m unittest discover -s code/data/tests -p "test_q1*.py"
python -B experiments/smoke.py
python -B .agents/skills/mathodology-workbench/scripts/mtd.py check --no-sync
```

W01、W07 的重建路径可通过 `--w01` 和 `--w07` 指定。B 的本机交付由 `local/stage03_q2_b/delivery` 加载。入口检查本机 SHA 清单、B 公开版本、正式侧车 schema、批规划输入的历史来源和验证选型。

## 验收和复算文件

| 私有运行相对路径 | C 的用途 |
| --- | --- |
| `Q1W01_case.json`、`Q1W07_case.json` | 固定名册、归一化 Q1 约束、前序与条件阻塞 |
| 各窗和条件的 `settings.json` | 条件化 SI 参数、输入摘要、种子基准和重复数 |
| 各窗和条件的 `replicates.jsonl.gz` | 每行一组配对重复。包含共用随机历时、三份策略轨迹、资源、事件、未完成原因和指标 |
| 各窗和条件的 `paired.json` | 每种策略的条件汇总和以重复编号配对的差值 |
| `summary.json`、`manifest.json` | 全部条件聚合结果、Q1/Q2 输入、代码、参数及已生成文件的 SHA-256 |

配对核对先确认三种策略的共同输入哈希、相同随机历时和固定分母，再从每份轨迹独立重算安全、资源、持有与事件约束。连续运动检查须配合原 C 内部极值反例。现场运动参数、真实竞争和连续供钢的证据边界见 B 交接和建模决策。 [B 的交接](../B/Q2_DATA_HANDOFF.md)

正式实验汇总和配对差图可公开复验。公开清单以仓库根目录为相对路径，哈希覆盖 Git 的 LF 文本内容及图表原字节。完整私有轨迹继续由 `local/stage03_a_q2/delivery/manifest.json` 校验。 [实验报告](Q2_EXPERIMENT_REPORT.md) · [公开 SHA-256](Q2_SHA256SUMS.txt)
