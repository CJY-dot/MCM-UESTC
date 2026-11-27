# Stage 03 · Q1-B 数据接入与标准化交接

2026-11-27 · 分支 `stage03-q1-b-data` · 基线 `699690f270829a7a76dd559f6f0047f973c6eb5b` · 接口冻结版 `stage03-q1-b/1.2.0`

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

## 2. 接口冻结：正式采纳的三项扩展

三项扩展已由项目负责人于 2026-11-27 正式采纳，不再是待审批提案。

### 2.1 `scope_status`

`scope` 仍只取 `ef_crane` / `external_service`。新增 `scope_status`：

| 值 | 含义 |
| --- | --- |
| `confirmed` | 已确认属于 EF 派车范围 |
| `pending_ef_confirmation` | 待确认 EF 范围；不是 EF 派车决策，`required_in_ef_scope=null`，准入 C |
| `external_boundary` | 外部服务/交接边界，不是 EF 派车决策 |

`pending_ef_confirmation` 用于 `SLAG→TRANSFER` 等没有独立 EF 起吊记录的腿。它不把任务直接归为 `external_service`，也不参与 EF 派车。

### 2.2 `required_in_process` / `required_in_ef_scope`

| 字段 | 含义 |
| --- | --- |
| `required_in_process` | 生产流程是否需要这条腿 |
| `required_in_ef_scope` | EF 调度范围是否需要这条腿；`confirmed` 为 true，`external_boundary` 为 false，`pending_ef_confirmation` 为 null |
| `required` | 兼容字段，必须与 `required_in_process` 相等 |

schema 会校验 `required == required_in_process`；不允许再把两种语义混用。

### 2.3 `simulator_truth` 与独立文件

`simulator_truth` 保存仿真隐藏真值，与策略可见输入严格隔离。构建器输出三个文件：

| 文件 | 用途 | 读取方 |
| --- | --- | --- |
| `q1_policy_view.json` | 策略可见输入；只含 `visible_at_s <= now_s` 的事件和已去除历史实际值的任务 | A 的策略/调度代码 |
| `q1_simulator_truth.json` | 隐藏真值；含实际起吊、到达、工序结束和未来事件 | 仿真器，不得给策略 |
| `q1_instance.json` | 完整数据包；含完整事件/任务和 `simulator_truth`，用于审计和仿真配置 | 数据/仿真管理，不作策略输入 |

策略可见性不靠文档提醒，而由接口和测试落实：

- `q1_policy_view.json` 顶层没有 `simulator_truth`；
- `q1_policy_view.json` 的事件全部满足 `visible_at_s <= initial_state.now_s`；
- 策略任务的 `historical_pickup_proxy_s`、`observed_arrival_s`、`release_interval.hi_s` 全部为 `null`；
- `q1_schema.validate_policy_view` 会拒绝隐藏真值、未来事件和泄漏的历史实际字段；
- `q1_schema.validate_simulator_truth` 校验隐藏文件的 `hidden_from_policy` 标记、未来事件和实际动作。

## 3. 输入包结构

`q1_policy_view.json` 与 `q1_instance.json` 共用以下结构和字段；策略输入使用前者。

| 字段 | 口径 |
| --- | --- |
| `schema` | `mcm-uestc.dispatch/1`（完整包）或 `mcm-uestc.dispatch.policy-view/1`（策略视图） |
| `instance_id`, `scenario`, `mode` | Q1、`strict_plan` |
| `clock` | 窗口原点、秒单位、本地朴素时间轴、`horizon_s`；不做时区转换 |
| `plan_version` | 基于更正版审计哈希的版本键 |
| `scope` | 目标炉次/任务、窗口、到期规则、策略可见性规则、待确认 EF 任务 |
| `evidence` | 证据模式、源哈希、构建器版本、假设、全局阻塞、正式接口扩展 |
| `device_catalog` | 五台 EF 行车的吨位、派车候选、窗口可用和占轨状态 |
| `topology` | 仅 EF 工位；侧别、相对顺序；坐标/轨界/净距/速度均为 `null` |
| `resources` | EF 工位、外部节点、倒渣/渡车/热修；容量未知为 `null` |
| `events` | 策略视图只含已可见事件；完整包保留全部事件和 `visible_at_s` |
| `tasks` | 策略视图不含历史实际值；完整包/审计台账保留代理值 |
| `simulator_truth` | 仅完整包含；策略视图不得出现 |
| `initial_state` | 窗口起点快照：五车、持有者、活动工序、占用、浇次、任务状态、锁定预约 |

schema 校验入口：

- 完整包：`code/data/q1_schema.py::validate_instance`；
- 策略视图：`code/data/q1_schema.py::validate_policy_view`；
- 隐藏真值：`code/data/q1_schema.py::validate_simulator_truth`。

`null` 表示未知，`[]` 表示已确认空集，`0` 只表示数值零；三者不可互换。

## 4. 字段映射

| Stage 02 | Q1 字段 | 说明 |
| --- | --- | --- |
| `task_id`, `heat_id` | 同名 | 保留原任务键，便于回溯 |
| `campaign`, `plan_order` | `campaign_id`, `campaign_order` | 浇次与炉序 |
| `leg`, `loaded_state` | `leg`, `load_state` | 重包/空包 |
| `origin`, `destination` | `origin`, `destination` | 规范站点键；原标签保留在 `*_label` |
| `process_predecessor_s` | `release_interval.lo_s` | 物理可吊下界，来源为前序工序完成 |
| `release_proxy_s` / `observed_lift_s` | 台账中的 `release_interval.hi_s`、`historical_pickup_proxy_s` | 历史起吊只作上界代理；策略视图置 `null`，真值进隐藏文件 |
| `observed_arrival_s` | 台账中的 `observed_arrival_s` | 策略视图置 `null`，真值进隐藏文件 |
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

**身份。** `ladle_identity` 保存 `ladle_id`、`status`、`canonical_ids`、`all_ids`、`unresolved_raw_ids`、候选来源和已批准来源。状态取 `unique`、`project_approved`、`identity_conflict`、`ambiguous`、`unknown`。只有 `unique` 和 `project_approved` 给出任务级 `ladle_id`；其他状态保持 `null` 并阻塞任务。任务原始 `flags` 含 `ambiguous_or_conflicting_source` 时同样阻塞。

**持有链。** 只使用规范/一致的实际事件。同刻分组：

- 同一事件类型且只有一个已知站点：采用已知站点，未知站点作佐证；
- 同一事件类型但已知站点不同，或不同事件类型候选持有者不同：记 `ambiguous`，不按 `event_id` 排序；
- `slag_start` / `slag_second_start` 只作 `operation_start_proxy`，状态 `conditional`，不当作已证实交接；
- 首事件不是 `bof_empty_car` / `bof_lift` 时链为 `partial`；
- 任务起点推导的持有者标 `derived_from_task_origin`，是条件假设，不能获得 A 类资格。

任务 `holder_status` 为 `unknown` / `partial` / `ambiguous` 时记 `holder_chain_unconfirmed` 并进入 C；`conditional` 记 `holder_chain_conditional`，可按 B 条件使用。

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
- `required_in_process` 表示生产流程必需；`required_in_ef_scope` 表示 EF 派车范围内必需。`required` 必须等于 `required_in_process`。
- `scope_status="pending_ef_confirmation"` 保留 `SLAG→TRANSFER` 的待确认语义：`scope` 仍为 `ef_crane`，但不参与 EF 派车，`required_in_ef_scope=null`，准入 C。

## 8. 初始状态与策略可见性

`initial_state` 只包含窗口起点可见信息：

- 五台 EF 行车均登记；`x`、`v`、`gear`、`carried_task_id` 为 `null`，`track_presence` 与 `dispatch_allowed` 为 `unknown`；
- 持有者只使用窗口起点前可见的持有链转移；同刻冲突记 `ambiguous`；
- 进行中的工序只记录 `start_s` 和 `source_event_ids`，`end_s=null`、`end_s_visible=false`、`remaining_service_s=null`；
- 任务状态只用可见事件判断，`visible_release_lo_s` 在释放时点晚于窗口起点时为 `null`；`plan_target_s` 是计划值，不是历史实际；
- `locked_reservations`、`visible_notices` 为空，因为没有已确认的锁定预约或公告。

策略隔离由文件与校验器共同落实：

- A 只加载 `q1_policy_view.json`，先调用 `validate_policy_view`；
- 仿真器单独加载 `q1_simulator_truth.json`，不得把该对象传入策略；
- `q1_instance.json` 是完整数据包，不作为策略输入；
- `scope.policy_visibility_rule` 要求策略只读取 `visible_at_s <= initial_state.now_s` 的事件；
- `validate_policy_view` 拒绝 `simulator_truth`、未来事件和任何非空历史实际字段。

本轮主实例：策略视图含 238 条可见事件、23 条任务；隐藏文件含 532 条未来事件、23 条任务实际值；策略任务的历史实际字段全部为 `null`。

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

A 类为 0 是本轮数据边界的直接结果，不是任务被丢弃：所有 9,040 条候选腿都在台账中，C 类保留依赖和原因。接口冻结没有改变 A/B/C 数量。

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

## 11. 参数证据与关键未知项

完整表见本机 `parameter_evidence.csv`。要点：

| 参数 | 状态 | 影响 |
| --- | --- | --- |
| 行车吨位 150/450 t | 题面/图示已观测 | 只用于吨位筛选 |
| EF 站点坐标、轨界、车体包络、净距、速度 | `null` | 无法生成正式轨迹或安全证书 |
| 五车窗口初态、占轨、可派状态 | `null`/`unknown` | 初始状态只能条件化 |
| 物理可吊 | 0 条已证实 | 只能用前序完成下界；历史起吊上界只进隐藏文件 |
| 计划座包提前量 900/120/180 s | `assumed` | 仅旧回归假设，不作为现场硬界 |
| 质量 | 观测端点代理 | 未知保持 `null` |
| 倒渣/渡车/热修容量与释放 | `null` | 外部交接只能条件验收 |
| `scope_status` / `required` 拆分 / `simulator_truth` | 已正式采纳 | 由 interface 与测试保证隔离 |

## 12. 复现与检查

```powershell
conda activate mcm-modeling
python -B code/data/q1_builder.py --primary-window Q1W06
python -B -m unittest discover -s code/data/tests -v
python -B experiments/smoke.py
python -B .agents/skills/mathodology-workbench/scripts/mtd.py check --no-sync
```

构建器默认读取 `problem/data/processed/stage03_q1_b/source_rebuild/corrected/`，默认原始目录为本机 B 题附件目录，默认输出 `problem/data/processed/stage03_q1_b/`。可用 `--processed`、`--raw`、`--output`、`--primary-window` 覆盖；输出目录必须是 `problem/data/processed/` 的子目录。

本轮结果：单元测试 34 项通过（原 27 项 + 新增 7 项隔离/契约测试）；`experiments/smoke.py` 通过；Workbench 静态检查 0 错误、0 警告、0 提示；同一输入重复构建的实例、策略视图、隐藏真值、事件表、任务表和审计哈希一致。

## 13. A 的使用方法

1. **策略输入：** 读取 `q1_policy_view.json`，先调用 `validate_policy_view`。不要读取 `q1_instance.json` 的 `simulator_truth` 或 `q1_simulator_truth.json`。
2. 只把 `scope.target_tasks` 中 `scope_status="confirmed"` 的 `ef_crane` 任务作为派车决策；`pending_ef_confirmation` 是待核台账，`external_service` 是边界。
3. 策略只能读取 `visible_at_s <= initial_state.now_s` 的事件；策略视图已经过滤好，事件表不需要再自行截断。
4. `release_interval.lo_s` 是条件释放下界；`applied_release_s.type` 说明本次采用的规则。`delivery_window.plan_latest_s` 是计划开浇外侧目标，不是硬截止。
5. `eligible_cranes=null` 表示资格未核实，不得当作空集；`[]` 只表示已确认没有合法行车且必为 C。`capacity_candidates` 只是吨位筛选。
6. `topology.geometry_status` 为 `unverified`，`position_m`、`rail_limits`、`min_gap_m`、`speed_mps` 均为 `null`。不要用图示像素或旧归一布局替代本实例坐标。
7. `initial_state.cranes` 的 `x`/`v` 为 `null`；A 需要先用合成样例开发接口，正式轨迹必须等几何和初态证据。
8. **仿真真值：** 只有仿真器读取 `q1_simulator_truth.json`；该对象不得传入策略函数、策略日志或策略可见状态。
9. 对 B 类任务，输出必须带条件标签（代理释放、条件持有链、未知资格、条件几何）；对 C 类任务，输出阻塞原因和依赖后继，不能静默删除。

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

**A 能否直接开始 Q1 调度开发：** 可以开始策略接口、策略视图消费、候选生成器和合成样例上的开发；不能在本实例上产出可验收的现场轨迹或安全证书，直到上述几何、初态和资格证据补齐。主实例是 B 类条件实例，不是正式 A 类现场实例。

## 15. PR 审查重点

- 基线 SHA 与只改授权目录：`code/data/`、`stages/03_implementation/B/`。
- 没有真实工作簿、逐炉次 CSV、真实输入 JSON 或身份映射值进入 Git。
- `q1_schema.py` 的 `null`/`[]`/`0` 语义、A 类资格、引用校验、`validate_policy_view` 和 `validate_simulator_truth`。
- `q1_builder.py` 的身份不投票、同刻持有链、具体工位匹配、EF 拓扑、资源需求、A/B/C 规则、`build_policy_view` 和 `build_simulator_truth`。
- 三项扩展已正式采纳：`scope_status`、`required_in_process`/`required_in_ef_scope`、`simulator_truth`；`required == required_in_process` 由 schema 校验。
- 合成样例覆盖跨炉钢包、身份冲突、同刻交接、跨午夜、未知资源、外部交接、LF1/LF3 隔离、未来工序结束、延迟公告、空资格、超吨位和端到端构建。
- 策略隔离测试验证：策略视图无隐藏真值、无未来事件、无历史实际任务字段；隐藏文件保留未来事件和实际动作。

### 接口冻结报告要点

- 正式采纳三项扩展，接口状态从“提案”改为“adopted”。
- 新增 `q1_policy_view.json`、`q1_simulator_truth.json` 两个独立文件；`q1_instance.json` 保留为完整数据包。
- 新增 `validate_policy_view`、`validate_simulator_truth` 和 7 项隔离/契约测试；原有 27 项测试仍通过，共 34 项。
- A/B/C 数量未变：A 0、B 3,766、C 5,274；窗口数量仍为 8，主窗口仍为 23 条任务、770 条事件。
- 未解决的现场证据仍是 EF 几何、五车初态、物理可吊/硬窗口、倒渣/渡车/热修容量、站点可达性和完整资格；它们不影响 A 先做接口和合成样例开发，但阻止正式现场轨迹验收。
