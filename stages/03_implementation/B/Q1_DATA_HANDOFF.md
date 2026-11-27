# Stage 03 · Q1-B 数据接入与标准化交接

2026-11-27 · 分支 `stage03-q1-b-data` · 基线 `699690f270829a7a76dd559f6f0047f973c6eb5b` · 返工版 `stage03-q1-b/1.1.0`

本轮把已收敛的 Stage 02 事件与候选腿转成 `mcm-uestc.dispatch/1` 标准化输入。它不实现调度、不运行 Q2/Q3，也不把缺失参数补成默认值。真实工作簿、逐炉次明细、精确时点、真实输入 JSON 和身份映射值全部留在本机 `problem/data/processed/`，不进入 Git。

## 1. 数据完整性与身份更正

| 项目 | 结果 |
| --- | --- |
| 原始工作簿 | 12 份、12 张工作表、6,758 行；SHA-256 与批准审计逐份一致 |
| Stage 02 更正版 | 58,049 条事件观测、39,700 条规范事件、9,040 条候选腿、8,870 条校准样本 |
| 有效计划 | 1,266 炉；1,051 炉匹配到唯一主表记录 |
| 三处身份批准 | 3#LF、RH1、CC3 各一处；原值、规范值、工作表、行列、源哈希和批准依据已核对 |
| 身份复跑 | 目标炉次规范事件 35→37→42，身份冲突任务 4→3→0；其他炉次三张表逐行一致 |

身份审计原件在本机 `problem/data/processed/stage02_b_ladle_identity_approved/`；差异复跑在 `problem/data/processed/stage03_q1_b/source_rebuild/`。旧 `stage02_b/` 未改动。三处更正只在入模映射中应用，不做全局字符串替换。

## 2. 返工修复摘要

| 问题 | 修复 |
| --- | --- |
| 初始状态泄露未来实际值 | `initial_state` 只保留策略可见状态；`active_operations` 不再含 `end_s`；`task_states` 不再含历史 `target_end_s`。实际起吊、到达和工序结束移入独立的 `simulator_truth`，并声明 `hidden_from_policy`。 |
| 钢包身份多数投票 | `heat_identity_map` 不再按出现频次选择。只有规范/一致记录唯一、或已批准映射，才给出物理包号；多值保留 `identity_conflict`，无规范值保留 `unknown`。原始值、来源、命名空间、临时罐号和冲突候选全部保留。 |
| 同刻事件强行排序 | `build_holder_chain` 按“钢包 + 时刻”分组；同刻不同候选持有者记为 `ambiguous`，不用 `event_id` 决定先后。同类型记录的已知站点优先于未知站点；`slag_start` 只作 `operation_start_proxy`，状态为 `conditional`，不当作已证实交接。 |
| 行车记录跨工位错配 | `heat_device_rows` 按炉次 + 具体工位（LF1/LF3、RH1/RH2、CC1–CC3）建索引；`task_crane_evidence` 只取任务对应工位的记录。多个不同标签保留为 `conflict`，不任选。重包用 `放包天车`，空包用 `吊包天车`，不合并两种作业。 |
| EF 拓扑侧别和顺序 | 按具体工位固定侧别：LF1 F、LF3 E、RH1/RH2 F、CC1–CC3 E。E 侧顺序 LF3→CC1→CC2→CC3；F 侧顺序 RH2→RH1→（LF2 排除）→LF1。仅 EF 工位进入 `topology`；BOF、倒渣、渡车、热修进入资源表，不作为 EF 轨道节点。坐标、轨界、净距、速度仍为 `null`。 |
| 资源需求被空列表伪装 | 已知起讫站的任务写 `resource_requirements`；未知任务为 `null` 并记 `resource_requirements_unverified`。只有已确认没有额外需求才用 `[]`。 |
| `required` 语义混用 | 拆成 `required_in_process`（生产流程必需）和 `required_in_ef_scope`（EF 派车必需）；`required` 保留为生产流程必需的兼容别名。 |
| `pending_ef_scope` 契约外枚举 | `scope` 只保留 `ef_crane` / `external_service`；待确认范围改用 `scope_status="pending_ef_confirmation"`，`required_in_ef_scope=null`，并保持 C 准入。该字段是待契约所有者批准的本地扩展，不静默改变正式枚举。 |
| `null` / `[]` 语义 | `eligible_cranes=null` 表示资格未核实；`[]` 表示已确认没有合法行车，必须配 `no_eligible_crane` 且为 C。`capacity_candidates=null` 表示无法吨位筛选；`[]` 表示已确认无吨位合格车，必须配 `no_capacity_compatible_crane`。 |
| A/B/C 准入偏宽 | A 必须同时有非空且已核实的 `eligible_cranes`、无 blocker、EF 几何和初态可实例化。当前真实数据不满足，A=0。B 必须有可构建的条件实例；C 保留任务和 blocker。 |
| Schema 校验不足 | 增加资源、设备、拓扑、`simulator_truth`、任务/事件引用、唯一 ID、`scope_status`/`required`/资格/blocker 一致性校验；移除 `visible_at_s <= time_s` 的错误限制，允许延迟公告。 |
| 窗口零长度冒充竞争 | 区分 `release_to_plan` / `release_to_observed` / `start_point_only`；只有至少两个有证据区间的任务重叠才标 `competitive_interval`，仅起始时点接近标 `time_cluster_only`。 |
| 测试覆盖不足 | 新增身份冲突、同刻交接、LF1/LF3 隔离、初始状态隐藏未来值、延迟公告、空资格、超吨位、EF 拓扑、资源需求、全合成端到端等测试。 |

## 3. 输入包结构

`q1_instance.json` 顶层字段：

| 字段 | 口径 |
| --- | --- |
| `schema` | `mcm-uestc.dispatch/1` |
| `instance_id`, `scenario`, `mode` | Q1、`strict_plan` |
| `clock` | 窗口原点、秒单位、本地朴素时间轴、`horizon_s`；不做时区转换 |
| `plan_version` | 基于更正版审计哈希的版本键 |
| `scope` | 目标炉次/任务、窗口、到期规则、策略可见性规则、待确认 EF 任务 |
| `evidence` | 证据模式、源哈希、构建器版本、假设、全局阻塞、契约扩展提案 |
| `device_catalog` | 五台 EF 行车的吨位、派车候选、窗口可用和占轨状态 |
| `topology` | 仅 EF 工位；侧别、相对顺序；坐标/轨界/净距/速度均为 `null` |
| `resources` | EF 工位、外部节点、倒渣/渡车/热修；容量未知为 `null` |
| `events` | 窗口内标准事件；实际事件保留 `visible_at_s`；计划事件注明可见性假设 |
| `tasks` | 策略可见任务记录；历史实际起吊/到达不在此暴露 |
| `simulator_truth` | 隐藏真值：实际起吊、到达、工序结束和未来事件 ID，策略不得读取 |
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
| `release_proxy_s` / `observed_lift_s` | 台账中的 `release_interval.hi_s`、`historical_pickup_proxy_s` | 历史起吊只作上界代理；策略可见实例中置 `null`，真值进 `simulator_truth` |
| `observed_arrival_s` | 台账中的 `observed_arrival_s` | 策略可见实例中置 `null`，真值进 `simulator_truth` |
| `plan_outer_deadline_s` | `delivery_window.plan_latest_s` | 计划开浇外侧目标；座包提前量未证实 |
| `gross_weight_t` | `mass_t.lo/hi` | 点值代理；未知保持 `null` |
| `cranes_by_capacity` | `capacity_candidates` | 仅吨位筛选，不是资格集 |
| `eligible_cranes` | `eligible_cranes` | 未核实为 `null`；确认无车为 `[]` |
| `flags`, `link_status` | `flags`, `blocker_codes`, `link_status` | 原始异常与准入阻塞分开 |
| `source_*` | `source_refs` | 计划、前序、起吊、到达、重量、截止来源 |
| `event`, `record_type` | `event_type`, `event_category` | 计划/实际/推导 |
| `equipment` | `resource_id` | 规范站点；原标签保留 |
| `event_status` | `source_event_status`, `source_status` | 规范/一致/冲突/保留分别记录 |

`pickup_window` 绑定起吊事件（`bound_event_role="pickup_start"`），`delivery_window` 绑定交付/座包事件（`bound_event_role="drop_complete_or_seat_complete"`）。硬窗口、计划窗口和工艺截止分别记录；`process_deadline_s` 未证实为 `null`，不写成不适用。

## 5. 身份与持有链语义

**身份。** `ladle_identity` 保存 `ladle_id`、`status`、`canonical_ids`、`all_ids`、`unresolved_raw_ids`、候选来源和已批准来源。状态取 `unique`、`project_approved`、`identity_conflict`、`ambiguous`、`unknown`。只有 `unique` 和 `project_approved` 给出任务级 `ladle_id`；其他状态保持 `null` 并阻塞任务。任务原始 `flags` 含 `ambiguous_or_conflicting_source` 时同样阻塞，不因规范记录唯一而放行。

**持有链。** 只使用规范/一致的实际事件。同刻分组：
- 同一事件类型且只有一个已知站点：采用已知站点，未知站点作佐证；
- 同一事件类型但已知站点不同，或不同事件类型候选持有者不同：记 `ambiguous`，不按 `event_id` 排序；
- `slag_start` / `slag_second_start` 只作 `operation_start_proxy`，状态 `conditional`，不当作已证实交接；
- 首事件不是 `bof_empty_car` / `bof_lift` 时链为 `partial`；
- 任务起点推导的持有者标 `derived_from_task_origin`，是条件假设，不能获得 A 类资格。

任务 `holder_status` 为 `unknown` / `partial` / `ambiguous` 时记 `holder_chain_unconfirmed` 并进入 C；`conditional` 记 `holder_chain_conditional`，可按 B 条件使用；`derived_from_task_origin` 同样只是条件假设。

## 6. 设备准入与 EF 工位

| 设备 | 吨位 | 派车候选 | 证据与状态 |
| --- | ---: | --- | --- |
| 1#150 | 150 t | 否 | 题面与图 1 有该车；12 份附件无作业标签；窗口是否在轨未知 |
| 2#150 | 150 t | 否 | 同上 |
| 3#450 | 450 t | 是 | LF/RH/CC 吊放包记录有标签；窗口可用与初始位置未知 |
| 4#450 | 450 t | 是 | 同上 |
| 5#450 | 450 t | 是 | 同上 |

工艺工位保留 LF1、LF3、RH1、RH2、CC1–CC3；LF2 按附件无数据排除。倒渣位、渡车、热修位只作为有据外部交接边界，不建渡车轨道网络，不优化渡车数量或 L0/L1/L2 布局。

EF 拓扑只含上述七个工位：LF1 F 侧第 4 位、LF3 E 侧第 1 位、RH2 F 侧第 1 位、RH1 F 侧第 2 位、CC1 E 侧第 2 位、CC2 E 侧第 3 位、CC3 E 侧第 4 位。LF2 排除后不重编号。BOF、倒渣、渡车、热修在 `resources` 中标 `in_ef_topology=false`，不进入 EF 轨道节点。

## 7. 资源需求、外部服务与 `required`

- 已知起讫站的任务建立 `resource_requirements`，每站一条：`resource_id`、`role`、`demand=1`、起止事件（有唯一来源时）、`authorization=unknown`、`source_status=derived`。
- 没有已知站点或无法建立需求时，`resource_requirements=null`，`resource_requirement_status="unverified"`，任务记 `resource_requirements_unverified` 并进入 C。`[]` 只表示已确认没有额外资源需求。
- 已知资源容量只有连铸接受位按“每台一个接受位”的结构性事实记为 1；倒渣、渡车、热修容量保持 `null`，不默认无限也不默认等于 1。
- `required_in_process` 表示生产流程必需；`required_in_ef_scope` 表示 EF 派车范围内必需。确认 EF 为 true，外部服务为 false，待确认 EF 为 null。`required` 保留为生产流程必需的兼容别名。
- `scope_status="pending_ef_confirmation"` 保留 `SLAG→TRANSFER` 的待确认语义：`scope` 仍为 `ef_crane`，但不参与 EF 派车，`required_in_ef_scope=null`，准入 C。该本地扩展待契约所有者批准；未批准前不改变正式 `scope` 枚举。

## 8. 初始状态与策略可见性

`initial_state` 只包含窗口起点可见信息：

- 五台 EF 行车均登记；`x`、`v`、`gear`、`carried_task_id` 为 `null`，`track_presence` 与 `dispatch_allowed` 为 `unknown`；
- 持有者只使用窗口起点前可见的持有链转移；同刻冲突记 `ambiguous`；
- 进行中的工序只记录 `start_s` 和 `source_event_ids`，`end_s=null`、`end_s_visible=false`、`remaining_service_s=null`；
- 任务状态只用可见事件判断，`visible_release_lo_s` 在释放时点晚于窗口起点时为 `null`；`plan_target_s` 是计划值，不是历史实际；
- `locked_reservations`、`visible_notices` 为空，因为没有已确认的锁定预约或公告。

`simulator_truth` 是仿真隐藏真值，显式标记 `visibility="hidden_from_policy"`，包含任务实际起吊、到达、前序工序完成、计划外侧目标和未来事件 ID。`scope.policy_visibility_rule` 要求策略只读取 `visible_at_s <= initial_state.now_s` 的事件。策略可见任务中的 `historical_pickup_proxy_s`、`observed_arrival_s` 和 `release_interval.hi_s` 均为 `null`；真实值只在 `simulator_truth`。

## 9. A/B/C 准入与覆盖

| 准入 | 任务数 | 口径 |
| --- | ---: | --- |
| A · 正式可调度 | 0 | 缺 EF 工位坐标、轨界、净距、速度和五车窗口初态，不能实例化正式约束 |
| B · 条件可调度 | 3,766 | 身份唯一、站点与重量可用、释放下界存在；缺少几何/资格/硬窗口，按条件实例使用 |
| C · 阻塞 | 5,274 | 关键站点、重量、释放、身份、持有链、资源需求或 EF 范围缺失/冲突 |

| 范围 | 任务数 |
| --- | ---: |
| `ef_crane` | 5,242 |
| 其中 `scope_status=confirmed` | 3,976 |
| 其中 `scope_status=pending_ef_confirmation` | 1,266 |
| `external_service` | 3,798 |
| 全部候选腿 | 9,040 |

`required_in_ef_scope`：true 3,976、false 3,798、null 1,266。

主要阻塞计数（可重叠，分母为 9,040）：

| 阻塞 | 任务数 |
| --- | ---: |
| 至少一个站点未知 | 6,075 |
| 重量未知 | 5,080 |
| 释放下界缺失 | 3,344 |
| 资格未核实 | 5,242 |
| 身份冲突（含来源冲突标记） | 1,706 |
| 边界接收未证实 | 2,101 |
| EF 范围未确认 | 1,266 |
| 行车指派未证实 | 1,828 |
| 持有链未确认 | 2,071 |
| 持有链条件性 | 5,529 |
| 资源需求未核实 | 2,095 |
| 行车证据冲突 | 10 |
| 释放上界低于下界 | 450 |
| 到达早于起吊 | 20 |

关键字段覆盖：起点已知 5,423；终点已知 4,487；重量已知 3,960；释放下界 5,696；释放上界代理 4,754；物理释放 0；取吊硬窗口 0；计划交付外侧目标 1,266；已验证资格 0；确认空资格 0；资源需求已派生 6,945；资源需求未核实 2,095；行车证据确认 3,414；行车证据冲突 10。

身份状态（任务数）：unique 8,544、identity_conflict 425、unknown 63、project_approved 8。持有状态（任务数）：complete 47、conditional 5,529、derived_from_task_origin 1,393、unknown 2,025、ambiguous 43、partial 3。

A 类为 0 是本轮数据边界的直接结果，不是任务被丢弃：所有 9,040 条候选腿都在台账中，C 类保留依赖和原因。

## 10. Q1 窗口候选

在单一浇次内搜索有证据的释放—交付区间重叠，得到 8 个候选；日期、精确时点、炉次和浇次标识只在本机 `window_candidates.json`，不进入 Git。8 个候选均为 `competitive_interval`，没有仅靠起始时点接近的窗口。

| 窗口 | 炉次 | 竞争任务 | 有区间任务 | 仅起始时点 | 证据级别 |
| --- | ---: | ---: | ---: | ---: | --- |
| Q1W01 | 7 | 12 | 12 | 0 | 竞争区间 |
| Q1W02 | 7 | 12 | 12 | 0 | 竞争区间 |
| Q1W03 | 7 | 12 | 12 | 0 | 竞争区间 |
| Q1W04 | 6 | 12 | 12 | 0 | 竞争区间 |
| Q1W05 | 7 | 11 | 11 | 0 | 竞争区间 |
| Q1W06 | 7 | 11 | 11 | 0 | 竞争区间（主实例） |
| Q1W07 | 7 | 11 | 11 | 0 | 竞争区间 |
| Q1W08 | 7 | 11 | 11 | 0 | 竞争区间 |

主实例 `stage03-q1-b-Q1W06` 包含 7 炉、23 条任务、770 条事件；其中 17 条 EF 任务、3 条待确认 EF 范围、3 条外部边界；20 条 B、3 条 C。窗口包含此前手工核对的 LF1→CC3 连续两炉及其前一炉空包回程，便于旧短窗回归对照。

窗口候选没有因为返工而改变数量或主窗口；变化只在候选内部增加了 `interval_task_count`、`point_only_task_count`、`competition_basis` 和 `evidence_level`，并保持 8 个都可用的结论。

## 11. 参数证据与关键未知项

完整表见本机 `parameter_evidence.csv`。要点：

| 参数 | 状态 | 影响 |
| --- | --- | --- |
| 行车吨位 150/450 t | 题面/图示已观测 | 只用于吨位筛选 |
| EF 站点坐标、轨界、车体包络、净距、速度 | `null` | 无法生成正式轨迹或安全证书 |
| 五车窗口初态、占轨、可派状态 | `null`/`unknown` | 初始状态只能条件化 |
| 物理可吊 | 0 条已证实 | 只能用前序完成下界；历史起吊上界只进隐藏真值 |
| 计划座包提前量 900/120/180 s | `assumed` | 仅旧回归假设，不作为现场硬界 |
| 质量 | 观测端点代理 | 未知保持 `null` |
| 倒渣/渡车/热修容量与释放 | `null` | 外部交接只能条件验收 |
| `scope_status` / `required` 拆分 / `simulator_truth` | 本地契约扩展提案 | 需契约所有者批准后才能成为正式接口 |

## 12. 复现与检查

```powershell
conda activate mcm-modeling
python -B code/data/q1_builder.py --primary-window Q1W06
python -B -m unittest discover -s code/data/tests -v
python -B experiments/smoke.py
python -B .agents/skills/mathodology-workbench/scripts/mtd.py check --no-sync
```

构建器默认读取 `problem/data/processed/stage03_q1_b/source_rebuild/corrected/`，默认原始目录为本机 B 题附件目录，默认输出 `problem/data/processed/stage03_q1_b/`。可用 `--processed`、`--raw`、`--output`、`--primary-window` 覆盖；输出目录必须是 `problem/data/processed/` 的子目录。

本轮结果：单元测试 27 项通过；`experiments/smoke.py` 通过；Workbench 静态检查 0 错误、0 警告、0 提示；同一输入重复构建的实例、事件表、任务表和审计哈希一致。

## 13. A 的使用方法

1. 读取 `q1_instance.json`，先调用 `code/data/q1_schema.py::validate_instance`。错误为空才进入求解。
2. 只把 `scope.target_tasks` 中 `scope_status="confirmed"` 的 `ef_crane` 任务作为派车决策；`pending_ef_confirmation` 是待核台账，`external_service` 是边界。
3. 策略只能读取 `visible_at_s <= initial_state.now_s` 的事件；不要读取 `simulator_truth`。任务中的 `historical_pickup_proxy_s`、`observed_arrival_s` 和 `release_interval.hi_s` 已置空。
4. `release_interval.lo_s` 是条件释放下界；`applied_release_s.type` 说明本次采用的规则。`delivery_window.plan_latest_s` 是计划开浇外侧目标，不是硬截止。
5. `eligible_cranes=null` 表示资格未核实，不得当作空集；`[]` 只表示已确认没有合法行车且必为 C。`capacity_candidates` 只是吨位筛选。
6. `topology.geometry_status` 为 `unverified`，`position_m`、`rail_limits`、`min_gap_m`、`speed_mps` 均为 `null`。不要用图示像素或旧归一布局替代本实例坐标。
7. `initial_state.cranes` 的 `x`/`v` 为 `null`；A 需要先用合成样例开发接口，正式轨迹必须等几何和初态证据。
8. 对 B 类任务，输出必须带条件标签（代理释放、条件持有链、未知资格、条件几何）；对 C 类任务，输出阻塞原因和依赖后继，不能静默删除。

## 14. 尚未解决与对 A 的影响

| 阻塞 | 影响 |
| --- | --- |
| EF 工位坐标、段长、轨界、净距、速度 | 无法生成正式轨迹、无法证明共轨安全；A 可先用合成样例开发 |
| 五车窗口初态与两台 150 t 占轨 | 初始状态不完整，正式可行结论受限 |
| 物理可吊与真实座包/供钢硬界 | 只能用条件释放区间和计划外侧目标；不能作现场断供结论 |
| 倒渣/渡车/热修接点、容量、释放 | `SLAG→TRANSFER` 及返程只能待核或外部边界 |
| 站点可达性与作业许可 | `eligible_cranes` 全为 `null`；A 类为 0 |
| 6,075 条任务至少一端站点未知 | 这些任务保留为 C 或条件任务，不能进入正式派车 |
| 450 条起吊早于前序完成、20 条到达早于起吊 | 已保留原始证据并标阻塞，未静默纠正 |
| 1,706 条任务带身份冲突或来源冲突标记 | 仅三处批准映射外，其余保持待核 |
| 5,529 条任务持有链为条件性、2,071 条未确认 | 条件任务可作 B，未确认任务为 C |

**A 能否直接开始 Q1 调度开发：** 可以开始接口、数据消费、候选生成器和合成样例上的开发；不能在本实例上产出可验收的现场轨迹或安全证书，直到上述几何、初态和资格证据补齐。主实例是 B 类条件实例，不是正式 A 类现场实例。

## 15. PR 审查重点

- 基线 SHA 与只改授权目录：`code/data/`、`stages/03_implementation/B/`。
- 没有真实工作簿、逐炉次 CSV、真实输入 JSON 或身份映射值进入 Git。
- `q1_schema.py` 的 `null`/`[]`/`0` 语义、A 类资格、引用校验和 `simulator_truth` 结构。
- `q1_builder.py` 的身份不投票、同刻持有链、具体工位匹配、EF 拓扑、资源需求和 A/B/C 规则。
- `scope_status`、`required_in_process`/`required_in_ef_scope`、`simulator_truth` 是本地契约扩展提案，需契约所有者确认；未批准前不改变正式枚举。
- 合成样例覆盖跨炉钢包、身份冲突、同刻交接、跨午夜、未知资源、外部交接、LF1/LF3 隔离、未来工序结束、延迟公告、空资格、超吨位和端到端构建。

### 返工报告要点

- 修复了未来信息泄露、身份多数投票、同刻事件 tie-break、工位跨匹配、EF 拓扑侧别/顺序、资源需求空列表、`required` 混用、`pending_ef_scope` 契约外枚举、`null`/`[]` 语义、A/B/C 偏宽、schema 校验和窗口零长度竞争。
- 主要文件与函数：`q1_builder.py` 的 `heat_identity_map`、`heat_device_rows`、`task_crane_evidence`、`build_holder_chain`、`attach_task_holders`、`task_blockers`、`admit`、`station_catalog`、`resource_catalog`、`attach_resource_requirements`、`window_candidates`、`build_instance`；`q1_schema.py` 的 `validate_task`、`validate_event`、`validate_instance`；测试与合成样例。
- A/B/C 变化：B 3,829→3,766，C 5,211→5,274，A 仍为 0。原因是来源身份冲突标记、条件/未确认持有链和未核实资源需求被更严格地计入准入；窗口数量仍为 8，主窗口仍为 23 条任务、770 条事件。
- 未修复项：EF 几何、五车初态、物理可吊/硬窗口、倒渣/渡车/热修容量、站点可达性和完整资格。它们仍是 A 开始正式 Q1 求解的接口阻塞；不影响 A 先做接口和合成样例开发。
- 实际测试：27 项单元测试通过；`experiments/smoke.py` 通过；`mtd.py check --no-sync` 为 0 错误、0 警告、0 提示；重复构建哈希一致。
