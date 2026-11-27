# Stage 02 → 03 实现契约

适用于 Q1–Q3，数学定义见[统一模型](model_spec.md)。首版以 Python 数据类或结构化记录实现。规范输入和状态用 JSON，明细表可导出 CSV。下列是逻辑表与字段约定，当前阶段交付文档和验收定义。

## 1. 输入、字段与准入

输入包版本 `mcm-uestc.dispatch/1`。顶层字段 `instance_id, scenario, mode, clock, plan_version, scope, evidence, topology, transfer_layout, resources, events, tasks, initial_state`。`topology` 为 EF 共轨，`transfer_layout` 为独立渡车网络。`scenario` 取 `Q1|Q2|Q3`，`mode` 取 `strict_plan|authorized_recovery`。Q1/Q2 使用同一严格验证语义，比较中若保留迟到回放，要显式当作诊断并分开统计。`scope` 声明目标炉次/任务、窗界、到期规则、前后窗负担、工艺模式及恢复授权。`evidence_mode` 区分 `shared_regression|historical_replay|synthetic_competition|simulated_fault`，同一运行另记布局、释放、统计校准各自的来源，历史日志与情景条件共用时照实标记。已有四任务回归止于 EF 落渡车，可以把接收方配置为经声明的固定边界，不标记为全链路渡车验收。

| 输入 | 字段与约束 |
| --- | --- |
| `clock` | 窗口原点、秒单位及 `horizon_s`。授权数据可储存日期原点、时区、原始值和 Excel 日期系统。共享算例只用相对时标。跨午夜保留日期差，不能把时分秒直接相减。 |
| `topology` | 长度单位 `m` 或 `normalized`；站点键、纵坐标、轨界、行车固定顺序、各车允许路段和操作、停靠/待避点、档位/限速区、空重包络及相邻安全距离。标明距阈值是参考点间距还是已扣包络的净距，避免重复扣车宽。 |
| `resources` | 资源 ID、类型、位置、物理容量、服务可用和许可日历、已占容量和占用主体。到位但满位的平车不可接包。跨区设备标签带区域键，平车与站位的组合用明确的资源/服务变体。 |
| `events` | `event_id, event_type, heat_id, ladle_id, task_id, resource_id, time_s, visible_at_s, sequence, source_status`。计划、观测、预测与实际实现事件以类型区分。前序边存结束事件及必要滞后 `min_lag_s`，有证据时加 `max_lag_s`。仿真中尚未发生的派生事件可有公式，无实现时点。 |
| `evidence` | 值级 `source_status` 取 `observed`、`derived`、`assumed` 或 `pending`，配 `source_ref, unit, bounds, impact`。题设给定值也标明题面出处，经推导值列原事件与规则。每项约束另记 `applicability`，取 `active`、`not_applicable` 或 `unknown`。未知用 `null`，不代表零、无限容量或无限时窗。文件哈希、相对路径、版本供审计；原表行列及实际值只落授权本机。 |
| `scope` | 以计划需求台账为基数，保留路线未解析、身份冲突、截尾等未生成任务的炉次和缺口。注明统计截止与报表单位。计划版本变更携带公告与生效时刻。 |

### 任务记录

| 字段 | 类型与使用 |
| --- | --- |
| `task_id, heat_id, ladle_id, campaign_id, campaign_order` | 唯一稳定键及炉序。浇次含连铸机键。物理钢包跨炉复用用相同 ID；允许临时 ID 时标明条件与受影响任务。 |
| `route, scope, load_state, origin, destination` | 计划路径、执行范围（`ef_crane`、`transfer_vehicle` 或 `external_service`）、载荷状态（`loaded` 或 `empty_ladle`）、规范站点键。空车调位属于车的轨迹，无钢包任务 ID。多个合法接驳选项用 `service_variants` 明列站点、轨道、允许渡车、许可及下游交接依赖。 |
| `mass_t` | 对象 `lo, hi, source_status`。以毛重上界校容量，点值用相同上下界。未知、量纲或来源冲突按准入处理。 |
| `predecessors, release_interval` | 前序事件键、必要时差和外部约束。释放含 `lo_s, hi_s, rule, source_status`。规则随仿真前序更新，区间给情景依据。`physical_release_s` 只储存该实例已实例化的物理释放，`historical_pickup_proxy_s` 单独存。代理实例用带类型的 `applied_release_s`；同一公式和输出明确本次采用哪种释放。 |
| `pickup_window, delivery_window` | 键 `hard_earliest_s, hard_latest_s, plan_earliest_s, plan_latest_s` 各带适用状态、来源和约束事件引用。`pickup_window` 绑定 `pickup_start`，`delivery_window` 绑定实际交付事件（一般为 `drop_complete`，连铸座包为 `seat_complete`）。动态资源可用另由占用事件约束。计划提前量、硬准备历时和真实供钢截止分别在事件表关联。 |
| `capacity_candidates, eligible_cranes` | 前者仅吨位筛选，后者含起讫站、完整授权路径与载荷类别，按各 `variant_id` 存为资格列表，单变体用唯一键。若待核用 `null`，已确认无合法车用 `[]`。派车与回放按所选变体查其名单并传递交接端点，不能使用跨变体的并集。随故障和预约变化的当前可达性不覆盖静态资格。外部腿存其资源授权。 |
| `eligible_transfer_vehicles` | 同样按变体保存渡车资格，由车辆额定载重、起讫站许可及可达轨道组成。渡车与 EF 行车用不同的 ID 命名空间，封锁导致的临时不可达由快照计算。 |
| `resource_requirements` | 资源/替代组、需求量、开始及释放事件、授权和可能的接驳选择。处理位、物理站位、渡车等不同占用各自入表。 |
| `source_status, admission, blocker_codes, required` | `admission` 取 `A`、`B` 或 `C`，记录值级证据、规则版本与场景假设。是否在目标范围内、是否到期与是否准入是独立标记。C 记录依赖阻塞及缺失来源。 |

**与 B 预处理对接。** 读其 `events.csv, tasks.csv, calibration.csv, audit.json`。保留 `source_*`、计划与实测、缺失标志、身份匹配和原候选腿键。从 `process_predecessor_s` 建待核的工艺前序，从 `release_proxy_s`/`observed_lift_s` 取得历史对照，从 `observed_arrival_s` 找该腿对应的到卸点/放下代理，从 `plan_outer_deadline_s` 找计划外界。`gross_weight_t` 与 `weight_status` 按原毛重口径映射。`cranes_by_capacity` 仅映射 `capacity_candidates`。不自动填 B 留空的 `physical_release_s/window_open_s/window_close_s/eligible_cranes`。原同炉次链不自动生成跨炉强制运输边。同浇次计划由规范炉序、转台和供钢规则另行连接。判定 A 级需要的证据缺失时，有完整授权情景才置 B，否则置 C 并保留需求，不悄悄删行。

**行级身份处理。** 在授权本机记录 `workbook_sha256, sheet, row_1based, column, field, raw_value, normalized_value, correction_reason, authority_ref, status`，区分 `identity_conflict`、`ambiguous` 与已批准更正。用熔炼号收集关联行，核对记号、真实包号与临时罐号、设备及跨日时间，禁止用全局字符串替换。在确认前 `normalized_value=null`，原观测照存，使用该身份的候选腿及不能独立确立持有者的后继都标 C 并保留依赖。批准后逐条核对工作簿版本、原值和唯一坐标，在入模映射应用，不改原表。对目标炉次重做行匹配、规范事件、候选腿、校准和前序，核对相邻持有链；其他炉次做无变化检查。若用现有预处理全表重跑，变化范围仍限定在批准记录及受影响链。输出选择状态与各级计数前后差，并保留原记录及修正规则，未批准的 RH/CC 行不得随 LF 自动重写。

### 渡车网络输入

| `transfer_layout` 组成 | 字段与校验 |
| --- | --- |
| 标识与实体 | `layout_id, track_count, tracks, nodes, arcs, conflict_zones, handoffs, vehicles`。`track_count` 必须等于物理 `track_id` 去重计数。`vehicles` 固定为题设 1#–3#，保持容量和当前状态与布局无关。车数、区段数及有向弧数单独统计。 |
| 走行拓扑 | 节点 `node_id, position, node_kind, stopping_allowed`。有向弧 `arc_id, track_id, section_id, from_node, to_node, length, direction, clearance_zone_ids`。反向弧共用物理区段并作相反方向约束；声明区段占用、道岔/交叉冲突、共用咽喉、允许运行方向、进入与清空条件。无合法横移或接轨时不加连通弧。 |
| 设备与初态 | `vehicle_id, capacity_t, allowed_track_ids, servable_handoff_ids, motion_limits, availability, initial_arc_or_node, initial_s, initial_v, carried_ladle_id`。同一长度单位，区段内 $s$ 从弧起点计。同一物理区段的双向冲突用公共物理坐标或独占锁。 |
| 交接站 | `handoff_id, transfer_node_id, ef_station_id, service_station_id, allowed_vehicle_ids, operation_requirements, lock_ids, capacity, release_events`。将倒渣、热修、转炉及 EF 接口的直连或经吊运关系写明。车辆在位、空闲、额定载重、前序完成及交接许可同时检查；取放完成更新持有者，区段清空后释放轨道锁。 |
| 比较配置 | `baseline_layout_id, candidate_layout_ids, common_initial_state_id, policy_id, demand_id, sample_ids`。固定任务、能力、位置、占用、策略及外生样本，只改布局及其路径条件。逐布局记录 L0/L1/L2 的长度、方向、许可与占地假设。不可共用初态的候选单独报告，不混入配对比较。 |

## 2. 状态、接口与预约

`initial_state` 和每次重调度快照用同一结构。含 `now_s, state_version, plan_version, layout_id, cranes, transfer_vehicles, holders, active_operations, occupancy, campaign_state, task_states, locked_reservations, visible_notices`。EF 行车状态含 `crane_id, track_id, x, v, gear, mode, carried_task_id, held_ladle_id, motion_or_operation_phase, phase_started_s, remaining_service_s`。渡车状态用自己的 `vehicle_id, arc_or_node_id, s, v` 与载荷/动作进度，另保留锁定区段、交叉区及交接预约。停机车还含制动阶段、授权状态和从当前速度可安全到达的占轨后续。资源占用按实体保存，浇次状态保持炉序、正在供钢炉次与有证据的缓冲/剩余工时。未处理、取载中、途中、卸载中、已交、授权延期、输入阻塞可表达；一项任务的进度由实际事件确定。Q1 固定布局，Q2 沿用并替换运动和时间模块，Q3 保持该布局并更新停机区段和状态。

事件回放区分 `pickup_start, pickup_complete, delivery_arrival, drop_start, drop_complete, seat_complete, prepare_complete, supply_start, supply_end`。落包即座包时可共用一个原子事件并记录两种语义。取载完成及放下完成各转移一次持有权。中断发生在取放中间时保存动作进度，按设备规则恢复或者安全处置。以物理顺序或原子交接处理同刻动作，半开区间允许真实的无重叠衔接。对策略不可见的实际故障或时间保留在仿真侧。

| 逻辑接口 | 合约 |
| --- | --- |
| `build_instance(plan, observations, config, boundary) -> instance, coverage` | 校验单位、ID、事件语义、必需字段、图无环和初态。冻结 A/B/C 及未建腿的需求台账。 |
| `make_candidate(task, variant, crane, state, template, reservations) -> candidate_or_rejection` | 输出带完整空驶、待避、装卸、持有与资源边界的候选。同一时域内未改动车辆继承已有轨迹或合法驻车。拒绝给出可定位的原因，不作全局不可行判断。 |
| `policy(visible_state, plan_view, options) -> proposals` | 同一生成器下复现 FCFS 与余量规则，稳定并列规则。只读已公告的事件、许可、信息版本和预测，包括计划已知需求的预调位。策略无法访问未来实际时点或采样器。 |
| `simulate(instance, policy, scenario_stream) -> trace` | 仿真侧根据前序、资源、已承诺动作与外生样本推进真实世界，到对应公告时点公开事件。随机键按情景及事件/工步/设备身份稳定索引，不按策略内调用次数索引。 |
| `repair(visible_state, plan_suffix, limits) -> proposals, diagnostic` | 局部修复再滚动短窗。只允许替换未执行且可解锁动作。返回需替换的预约 ID、理由、快照版本、信息时点与候选安全后续。 |
| `validate(instance, trace, previous_prefix) -> findings, metrics` | 与派车和优化独立。分别检查 EF 连续净距与渡车区段/交叉/方向轨迹，再联合重算交接锁、持有、工艺和容量。不接受求解器的可行标志或自算指标。提交前核对快照仍有效，回放通过才发布可执行动作。 |

## 3. 输出与统计

输出包版本 `mcm-uestc.dispatch-result/1`。全部记录使用相同快照、任务 ID 和时标。输出不确定量或情景值时保留类型，不将观测、假设与证据范围混在一个准入字段。

| 产物 | 最少字段及口径 |
| --- | --- |
| 逐任务结果 | 任务键、准入/范围、承运车/外部资源、前序和取放事件、物理/代理释放及类型、硬/计划界、准时/超窗/未交/推迟/阻塞、已观察等待和截尾、来源。列出到期任务及前后窗统计。 |
| 五车轨迹 | `crane_id, t0_s, t1_s, x0, x1, v0, v1, a, gear, mode, task_id, ladle_id, threshold_context`。片段按车有序，在共同验证时域覆盖无缝。Q1 可从匀速段重算速度，不验切换处速度连续。Q2/Q3 要验 $C^1$，限速区与占轨另有事件/资源关联。 |
| 渡车轨迹与布局结果 | `layout_id, vehicle_id, arc_or_node_id, t0_s, t1_s, s0, s1, v0, v1, a, mode, ladle_id, occupied_zone_ids`。记录进区及车尾清空时点、核对节点/方向/速度及容量冲突。分布局报两车队的空载/有载运行、冲突及等锁、交接瓶颈、钢包等待、按时到位和连续供钢或风险代理，附配对资格与场地假设。 |
| 资源和事件日志 | 占用主体、进出事件 ID、需求量、授权 ID、版本、发生时刻与可见时刻。保留计划变更、作废预约、故障/恢复公告、状态变更、边界快照。 |
| 检查与诊断 | 每项验收状态、规则、任务/资源/车对、时刻、区间、阈值及裕度、配置版本；附 `outcome, solver_status, reason_code, certificate_scope, witness_mode`。轨迹阈值比较采用记录的数值容差，临界值报告原裕度，精度不足继续细算或标未判定；容差不充当物理安全裕量。 |
| 运行与对照 | 版本、数据/配置哈希、依赖、种子、外生路径、样本来源、训练/验证/留出划分、权限、初态、策略及变化、终止条件、求解耗时。报告计划目标、硬工艺条件、假设与已核范围。共享输出禁止真实生产明细。 |

`outcome` 取 `on_plan|delay_required|physical_infeasible|unresolved`。`solver_status` 独立说明见证/最优/候选不可行/候选耗尽/求解超时/输入阻塞。未到时限的任务用后窗状态，勿提前认作逾期。能安全晚到却无“不可能严格完成”证书时，用 `unresolved` 加授权迟到见证。检查器必须能定位 `input_blocked, hard_time_lower_bound, plan_time_lower_bound, resource_conflict, candidate_network_infeasible, candidate_exhausted, timeout` 等原因。局部资源冲突若仅出现在选定排法中，只否定该排法。

**分母和时间。** 报告目标总炉次、已生成腿、A/B/C 覆盖、按窗到期总任务及各完成/迟到/未交/阻塞数；未知的按时状态单列并说明可评范围。三场景对比锁定同一目标与时域。供钢报告评估的交接数、有工艺证据的分母、缺失与风险代理。等待按[模型 §3](model_spec.md#3-分层目标与指标)逐项复算，同时报未交截至观测时点的等待与截尾。完成的延误用实际完成减适用目标的正部，未交可报截至观察时点的延误下界，不当作终值。报告最大/总延误时给完成及截尾口径。改派以原基线车辆和新车辆均定义的任务为分母，起吊偏移以共同定义的起吊时刻统计，取消和新增分别列出。Q2/Q3 的配对比较记录全部重复及各策略未能执行的重复。

## 4. 验收顺序

| 顺序 | 固定用例 | 必须通过的检查 |
| --- | --- | --- |
| 1 | 输入、资源、窗口、持有 | 四类精炼链与空包，跨午夜，空缺分档，原重量/单位，双炉冲突与半开交接、渡车未就绪、未完成翻渣、载荷超限。检查事件因果顺序、装卸双边占用、唯一持有、同浇次顺序、供钢独立判据及前后窗。 |
| 2 | [C 反例 4.1](C/C_validation_plan.md#41-故障车停在单轨中间) | 净距 8 m，C3 在 50 m 占轨；唯一合格 C2 最多到 42 m，无法在截止前抵 60 m。对照 C3 在 70 m 时，预期 12 s 到位且最小净距 10 m。 |
| 2 | [C 反例 4.2](C/C_validation_plan.md#42-端点安全区间内部净距不足) | 段两端净距 10 m，阈值 9 m，内部 $t=1$ s 处 8 m。必须拒绝并准确定位车对和时点。 |
| 2 | [C 反例 4.3](C/C_validation_plan.md#43-行车带载运动中故障) | $t=2$ s 带载故障，反应制动后 $t=5$ s 停于 16 m，保持持有至合法交付。获准恢复后最早放下 $46/3$ s，较计划晚 $10/3$ s，净距最小 9 m。旧 $t=15$ s 取吊预约应重排；硬截止失败与授权晚到分别验收。 |
| 2 | 渡车独立网络与联合交接 | 单线双向同期占用应拒绝。两条独立轨道可并行，共用交叉或站位锁时应拒绝同时进入。校验未翻渣、未停稳、错误接收车辆及无容量的落车。故障车保留区段和载荷，未清空时禁止他车进入；存在合法旁通且共享咽喉未阻塞时才允许改发。 |
| 3 | [H1/H2/E1/E2 短窗](../01_topic_selection/validation/README.md#b-题复跑) | 先复现原时标和假设的共享回归。授权历史输入另验可信上下界、站点/持有、翻渣前序和相邻占用；逐项替换假设。两个往期串行原型的结果分别回归，完整并行安排以新独立回放为准。 |
| 4 | 并发、竞争与因果 | 合成竞争确认规则选择可分化。至少两个审核过的真实竞争窗测增量，分别评估预调位、非串行候选与短窗 MILP。固定同一样本和信息时点；对相同可见历史、不同未来实际路径，两次策略的当期承诺应相同。 |
| 4 | 渡车布局配对 | 固定生产需求、三台渡车/五台行车初态及策略，对比同一干线上的两种旁通位置与基准。确认轨道数发生变化时车数仍为 3，分开报告运输、冲突、钢包等待和连续供钢。 |
| 5 | 三场景端到端 | 固定参数比较 Q1 基线/改进及有限候选 MILP；Q2 保留配对、留出及截尾；Q3 覆盖就绪延迟、设备不可用、停轨与带载故障，检查前缀/恢复和四种输出判定。所有结果附可追溯的回放及覆盖。 |

当前三个 C 反例可作为精确算术判据。下一阶段需要用正式数据接口喂给独立检查器，验证其能稳定接受正例、拒绝反例。源码、原型与授权回放的已核范围见[收敛报告](convergence_report.md#本次复核)。
