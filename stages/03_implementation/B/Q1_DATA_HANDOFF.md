# Stage 03 · Q1-B 数据接入与标准化交接

2026-11-27 · 分支 `stage03-q1-b-data` · 基线 `699690f270829a7a76dd559f6f0047f973c6eb5b`（`origin/main`）

本轮把已收敛的 Stage 02 事件与候选腿转成 `mcm-uestc.dispatch/1` 标准化输入。它不实现调度、不运行 Q2/Q3，也不把缺失参数补成默认值。真实工作簿、逐炉次明细、精确时点和真实输入 JSON 全部留在本机 `problem/data/processed/`，不进入 Git。

## 1. 数据完整性与身份更正

| 项目 | 结果 |
| --- | --- |
| 原始工作簿 | 12 份、12 张工作表、6,758 行；SHA-256 与批准审计逐份一致 |
| Stage 02 更正版 | 58,049 条事件观测、39,700 条规范事件、9,040 条候选腿、8,870 条校准样本 |
| 有效计划 | 1,266 炉；1,051 炉匹配到唯一主表记录 |
| 三处身份批准 | 3#LF、RH1、CC3 各一处；已核对原值、规范值、工作表、行列、源哈希和批准依据 |
| 身份复跑 | 目标炉次规范事件 35→37→42，身份冲突任务 4→3→0；其他炉次三张表逐行一致；与 PR #4 审批审计的汇总字段一致 |

身份审计原件在本机 `problem/data/processed/stage02_b_ladle_identity_approved/`；差异复跑在 `problem/data/processed/stage03_q1_b/source_rebuild/`。旧 `stage02_b/` 未改动。三处更正只在入模映射中应用，不做全局字符串替换。

## 2. 处理流程

1. 读取更正版 `events.csv`、`tasks.csv`、`audit.json`，保留 `source_*`、匹配状态和异常标记。
2. 规范站点键：`BOFA/B/C`、`LF1/LF3`、`RH1/RH2`、`CC1/2/3`、`SLAG:n#`、`REPAIR:n#`；`TRANSFER:unknown`、`BOF_return:unknown` 保留为未知。
3. 事件标准化：区分计划、实际和推导事件；保留原表行列、列位、解析状态、事件冲突和链接状态；同刻事件只按来源排序，不声明物理先后。
4. 从 LF/RH/CC 设备行的 `放包天车`、`吊包天车` 提取实际行车标签，作为 EF 范围证据；没有标签的腿标为 `ef_crane_assignment_not_confirmed`。
5. 任务标准化：物理释放只用区间。前序工序完成给下界，历史起吊给上界代理；`physical_release_s` 保持 `null`。计划开浇只作为交付外侧目标，不当作硬窗口。
6. 吨位筛选写入 `capacity_candidates`；站点可达、许可和窗口未核实，`eligible_cranes` 保持 `null`。已确认没有合格车才用 `[]`。
7. 按模型 §1.2 逐任务给出 A/B/C 准入和 `blocker_codes`；C 任务保留，不静默删除。
8. 用规范事件建跨炉钢包持有链；无法从事件确认时，仅以任务起点作为明确标注的派生持有者。
9. 在单一浇次内搜索至少两个炉次重叠的真实竞争窗口，生成候选与主实例。
10. 运行 schema 校验、覆盖审计、参数证据表和单元测试。

## 3. 输入包结构

`q1_instance.json` 顶层字段：

| 字段 | 口径 |
| --- | --- |
| `schema` | `mcm-uestc.dispatch/1` |
| `instance_id`, `scenario`, `mode` | Q1、`strict_plan` |
| `clock` | 窗口原点、秒单位、本地朴素时间轴、`horizon_s`；不做时区转换 |
| `plan_version` | 基于更正版审计哈希的版本键 |
| `scope` | 目标炉次/任务、窗口、到期规则、`process_mode`、恢复授权 |
| `evidence` | 证据模式、源哈希、构建器版本、假设和全局阻塞 |
| `device_catalog` | 五台 EF 行车的吨位、派车候选、窗口可用和占轨状态 |
| `topology` | 仅 EF；工位相对顺序；坐标、轨界、净距、速度均为 `null` |
| `resources` | 工位、接受位、倒渣位、渡车、热修位；容量未知为 `null` |
| `events` | 窗口内标准事件；计划事件另注可见性假设 |
| `tasks` | 窗口内标准任务；外部服务腿作为边界保留 |
| `initial_state` | 窗口起点快照：五车、持有者、活动工序、占用、浇次、任务状态、锁定预约 |

schema 校验入口：`code/data/q1_schema.py::validate_instance`。`null` 表示未知，`[]` 表示已确认空集，`0` 只表示数值零；三者不可互换。

## 4. 字段映射

| Stage 02 | Q1 字段 | 说明 |
| --- | --- | --- |
| `task_id`, `heat_id` | 同名 | 保留原任务键，便于回溯 |
| `campaign`, `plan_order` | `campaign_id`, `campaign_order` | 浇次与炉序 |
| `leg`, `loaded_state` | `leg`, `load_state` | 重包/空包 |
| `origin`, `destination` | `origin`, `destination` | 规范站点键；原标签保留在 `*_label` |
| `process_predecessor_s` | `release_interval.lo_s` | 物理可吊下界，来源为前序工序完成 |
| `release_proxy_s` / `observed_lift_s` | `release_interval.hi_s`、`historical_pickup_proxy_s` | 历史起吊只作上界代理；上界低于下界时置空并记阻塞 |
| `observed_arrival_s` | `observed_arrival_s` | 到卸点/放下代理 |
| `plan_outer_deadline_s` | `delivery_window.plan_latest_s` | 计划开浇外侧目标；座包提前量未证实 |
| `gross_weight_t` | `mass_t.lo/hi` | 点值代理；未知保持 `null` |
| `cranes_by_capacity` | `capacity_candidates` | 仅吨位筛选，不是资格集 |
| `eligible_cranes` | `eligible_cranes` | 未核实为 `null`；确认无车才为 `[]` |
| `flags`, `link_status` | `flags`, `blocker_codes`, `link_status` | 原始异常与准入阻塞分开 |
| `source_*` | `source_refs` | 计划、前序、起吊、到达、重量、截止来源 |
| `event`, `record_type` | `event_type`, `event_category` | 计划/实际/推导 |
| `equipment` | `resource_id` | 规范站点；原标签保留 |
| `event_status` | `source_event_status`, `source_status` | 规范/一致/冲突/保留分别记录 |

事件表中的 `visible_at_s`：实际事件等于发生时刻；计划事件在实例中注为“窗口起点前已知”，并保留 `visibility_assumption`。持有链中的 `holder_after` 在起吊后为 `EF_CRANE:unknown`，在座包/落车后为对应站点；未知外部资源保留 `TRANSFER:unknown` 等显式键。

## 5. 设备准入与 EF 工位

| 设备 | 吨位 | 派车候选 | 证据与状态 |
| --- | ---: | --- | --- |
| 1#150 | 150 t | 否 | 题面与图 1 有该车；12 份附件无作业标签；窗口是否在轨未知 |
| 2#150 | 150 t | 否 | 同上 |
| 3#450 | 450 t | 是 | LF/RH/CC 吊放包记录有标签；窗口可用与初始位置未知 |
| 4#450 | 450 t | 是 | 同上；本批任务中实际标签较多 |
| 5#450 | 450 t | 是 | 同上 |

工艺工位保留 LF1、LF3、RH1、RH2、CC1–CC3；LF2 按附件无数据排除。倒渣位、渡车、热修位只作为有据外部交接边界，不建渡车轨道网络，不优化渡车数量或 L0/L1/L2 布局。

## 6. 初始状态与持有链

- 五台 EF 行车均进入 `initial_state.cranes`；`x`、`v`、`gear`、`carried_task_id` 为 `null`，`track_presence` 与 `dispatch_allowed` 为 `unknown`。没有窗口级位置或占轨记录，不填虚构坐标。
- 主实例包含 16 名钢包持有者，其中 4 名状态未知；持有链来自规范事件，事件不足时仅以任务起点作 `derived_from_task_origin` 标注。
- 窗口起点有一个连铸工序在进行，写入 `active_operations`；连铸接受位容量按“每台连铸机一个接受位”的结构性事实记为 1，倒渣/渡车/热修容量保持 `null`。
- 浇次状态按炉序保留当前炉与下一炉；`locked_reservations` 和 `visible_notices` 为空，因为没有已确认的锁定预约或公告记录。

## 7. 外部交接

- `CC→SLAG` 有连铸空包吊起和翻渣事件，作为 EF 任务；翻渣开始只是到站上界代理，不单独反推行车运动。
- `SLAG→TRANSFER` 没有独立 EF 起吊记录，且渡车编号未知，统一标 `pending_ef_scope`（C），保留需求台账。
- `TRANSFER→REPAIR`、`REPAIR→TRANSFER`、`TRANSFER→BOF_RETURN` 为 `external_service`，只作为边界；没有证据确认重新进入 EF 的返程不生成 EF 任务。
- 渡车、热修、倒渣容量为 `null`；不默认无限，也不默认等于 1。

## 8. A/B/C 准入与覆盖

| 准入 | 任务数 | 口径 |
| --- | ---: | --- |
| A · 正式可调度 | 0 | 缺 EF 工位坐标、轨界、净距、速度和五车窗口初态，不能实例化正式约束 |
| B · 条件可调度 | 3,829 | 身份唯一、站点与重量可用、释放下界存在；缺少几何/资格/硬窗口，按条件实例使用 |
| C · 阻塞 | 5,211 | 关键站点、重量、释放、身份或 EF 范围缺失/冲突；保留任务和阻塞原因 |

| 范围 | 任务数 |
| --- | ---: |
| `ef_crane` | 3,976 |
| `external_service` | 3,798 |
| `pending_ef_scope` | 1,266 |
| 全部候选腿 | 9,040 |

主要阻塞计数（可重叠，分母为 9,040）：

| 阻塞 | 任务数 |
| --- | ---: |
| 至少一个站点未知 | 6,075 |
| 重量未知 | 5,080 |
| 释放下界缺失 | 3,344 |
| 资格未核实 | 5,242 |
| 身份冲突 | 1,678 |
| 边界接收未证实 | 2,101 |
| EF 范围未确认 | 1,266 |
| 行车指派未证实 | 456 |
| 释放上界低于下界 | 450 |
| 到达早于起吊 | 20 |

关键字段覆盖：起点已知 5,423；终点已知 4,487；重量已知 3,960；释放下界 5,696；释放上界代理 4,754；物理释放 0；取吊硬窗口 0；计划交付外侧目标 1,266；已验证资格 0。A 类为 0 是本轮数据边界的直接结果，不是任务被丢弃：所有 9,040 条候选腿都在台账中，C 类保留依赖和原因。

## 9. Q1 窗口候选

在单一浇次内搜索至少两个炉次释放—交付区间重叠的窗口，得到 8 个候选；日期、精确时点、炉次和浇次标识只在本机 `window_candidates.json`，不进入 Git。

| 窗口 | 炉次 | 竞争任务 | 重包 | 空包 | C 任务 | 证据级别 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Q1W01 | 7 | 12 | 9 | 3 | 0 | B 条件 |
| Q1W02 | 7 | 12 | 8 | 4 | 0 | B 条件 |
| Q1W03 | 7 | 12 | 8 | 4 | 0 | B 条件 |
| Q1W04 | 6 | 12 | 8 | 4 | 0 | B 条件 |
| Q1W05 | 7 | 11 | 8 | 3 | 0 | B 条件 |
| Q1W06 | 7 | 11 | 8 | 3 | 0 | B 条件（主实例） |
| Q1W07 | 7 | 11 | 7 | 4 | 0 | B 条件 |
| Q1W08 | 7 | 11 | 7 | 4 | 0 | B 条件 |

主实例 `stage03-q1-b-Q1W06` 包含 7 炉、23 条任务、770 条事件；其中 17 条 EF 任务、3 条待核 EF 范围、3 条外部边界；20 条 B、3 条 C。该窗口包含此前手工核对的 LF1→CC3 连续两炉及其前一炉空包回程，便于与旧短窗回归对照。

## 10. 参数证据与关键未知项

完整表见本机 `parameter_evidence.csv`。要点：

| 参数 | 状态 | 影响 |
| --- | --- | --- |
| 行车吨位 150/450 t | 题面/图示已观测 | 只用于吨位筛选 |
| EF 站点坐标、轨界、车体包络、净距、速度 | `null` | 无法生成正式轨迹或安全证书 |
| 五车窗口初态、占轨、可派状态 | `null`/`unknown` | 初始状态只能条件化 |
| 物理可吊 | 0 条已证实 | 只能用前序完成下界和历史起吊上界 |
| 计划座包提前量 900/120/180 s | `assumed` | 仅旧回归假设，不作为现场硬界 |
| 质量 | 观测端点代理 | 未知保持 `null` |
| 倒渣/渡车/热修容量与释放 | `null` | 外部交接只能条件验收 |

## 11. 复现与检查

```powershell
conda activate mcm-modeling
python -B code/data/q1_builder.py --primary-window Q1W06
python -B -m unittest discover -s code/data/tests -v
python -B experiments/smoke.py
python -B .agents/skills/mathodology-workbench/scripts/mtd.py check --no-sync
```

构建器默认读取 `problem/data/processed/stage03_q1_b/source_rebuild/corrected/`，默认原始目录为本机 B 题附件目录，默认输出 `problem/data/processed/stage03_q1_b/`。可用 `--processed`、`--raw`、`--output`、`--primary-window` 覆盖；输出目录必须是 `problem/data/processed/` 的子目录。

本轮结果：单元测试 15 项通过；`experiments/smoke.py` 通过；Workbench 静态检查 0 错误、0 警告、0 提示；同一输入重复构建的实例、事件表、任务表和审计哈希一致。

## 12. A 的使用方法

1. 读取 `q1_instance.json`，先调用 `code/data/q1_schema.py::validate_instance`。错误为空才进入求解。
2. 只把 `scope.target_tasks` 中的 `ef_crane` 任务作为派车决策；`external_service` 是边界，`pending_ef_scope` 是待核台账。
3. `release_interval.lo_s` 是条件释放下界；`applied_release_s.type` 说明本次采用的规则。`historical_pickup_proxy_s` 只能作对照，不能当作物理可吊。
4. `delivery_window.plan_latest_s` 是计划开浇外侧目标，不是硬截止；不要据此宣称现场必然超窗。
5. `eligible_cranes=null` 表示资格未核实，不得当作空集；`[]` 只表示已确认没有合法行车。
6. `topology.geometry_status` 为 `unverified`，`position_m`、`rail_limits`、`min_gap_m`、`speed_mps` 均为 `null`。不要用图示像素或旧归一布局替代本实例坐标。
7. `initial_state.cranes` 的 `x`/`v` 为 `null`；A 需要先用合成样例开发接口，正式轨迹必须等几何和初态证据。
8. 对 B 类任务，输出必须带条件标签（代理释放、未知资格、条件几何）；对 C 类任务，输出阻塞原因和依赖后继，不能静默删除。

## 13. 尚未解决与对 A 的影响

| 阻塞 | 影响 |
| --- | --- |
| EF 工位坐标、段长、轨界、净距、速度 | 无法生成正式轨迹、无法证明共轨安全；A 可先用合成样例开发 |
| 五车窗口初态与两台 150 t 占轨 | 初始状态不完整，正式可行结论受限 |
| 物理可吊与真实座包/供钢硬界 | 只能用条件释放区间和计划外侧目标；不能作现场断供结论 |
| 倒渣/渡车/热修接点、容量、释放 | `SLAG→TRANSFER` 及返程只能待核或外部边界 |
| 站点可达性与作业许可 | `eligible_cranes` 全为 `null`；A 类为 0 |
| 6,075 条任务至少一端站点未知 | 这些任务保留为 C 或条件任务，不能进入正式派车 |
| 450 条起吊早于前序完成、20 条到达早于起吊 | 已保留原始证据并标阻塞，未静默纠正 |
| 1,678 条任务带身份冲突标记 | 仅三处批准映射外，其余保持待核 |

**A 能否直接开始 Q1 调度开发：** 可以开始接口、数据消费、候选生成器和合成样例上的开发；不能在本实例上产出可验收的现场轨迹或安全证书，直到上述几何、初态和资格证据补齐。主实例是 B 类条件实例，不是正式 A 类现场实例。

## 14. PR 审查重点

- 基线 SHA 与只改授权目录：`code/data/`、`stages/03_implementation/B/`。
- 没有真实工作簿、逐炉次 CSV、真实输入 JSON 或身份映射值进入 Git。
- `q1_schema.py` 的 `null`/`[]`/`0` 语义和 `admission=A` 的资格要求。
- `q1_builder.py` 的 EF/外部/待核分类、释放区间、窗口和 A/B/C 规则。
- 合成样例覆盖跨炉钢包、跨午夜、未知资源、外部交接和阻塞输入。
- 主实例窗口只含一个浇次，任务与事件数量可控；旧短窗回归保留。
