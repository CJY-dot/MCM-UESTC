# Stage 02 → 03 实现契约

适用于 Q1–Q3，数学定义见[统一模型](model_spec.md)，范围和证据见[Stage 02.5 范围修订](model_scope_revision.md)。规范输入和状态用 JSON，明细表可导出 CSV。本轮只修订逻辑结构和验收口径，后续按[Stage 03 对齐](../03_implementation/IMPLEMENTATION_ALIGNMENT.md)实现。

## 1. 输入、字段与准入

输入包版本保留 `mcm-uestc.dispatch/1`。本轮在正式实现前修订首版草案。顶层字段为 `instance_id, scenario, mode, clock, plan_version, scope, evidence, device_catalog, topology, resources, events, tasks, initial_state`。`topology` 限于 EF。`scenario` 取 `Q1|Q2|Q3`，`mode` 取 `strict_plan|authorized_recovery`。Q1/Q2 使用同一严格验证语义，诊断性迟到回放分开统计。`scope` 声明目标炉次/任务、窗界、到期规则、期初期末约束、工艺模式及恢复授权。`evidence_mode` 区分 `shared_regression|historical_replay|synthetic_competition|simulated_fault`，并另记几何、释放和参数各自来源。现有四任务相对算例沿用原历史格式，接收方按已有配置作固定边界。

| 输入 | 字段与约束 |
| --- | --- |
| `clock` | 窗口原点、秒单位及 `horizon_s`。授权数据可储存日期原点、时区、原始值和 Excel 日期系统。共享算例只用相对时标。跨午夜保留日期差，不能把时分秒直接相减。 |
| `device_catalog` | 设备键、跨区、原始编号、类型、题面出处、图示位置、数据证据及覆盖说明、入模决定和理由。EF 车需区分同号不同吨位。分开登记已观察运行与窗口可用/占轨状态，后者从快照核实。 |
| `topology` | 仅 EF。以图 1 记录工位横向侧、纵向顺序及依据。正式运行所需纵坐标、轨界、允许路段和操作、停靠/待避点、限速及安全距离各有独立证据，缺失可用 `null` 并限制验收。物理单位用 `m`，`normalized` 仅用于明确的旧回归或审定的条件实例。标明净距口径，避免重复扣车宽。 |
| `resources` | 资源 ID、类型、位置、物理容量、服务可用、已占容量与占用主体。渡车等外部资源只登记可核实的接点、接收许可、容量和边界时点；无法关联单车时可保留有据的接口级服务记录。 |
| `events` | `event_id, event_type, heat_id, ladle_id, task_id, resource_id, time_s, visible_at_s, sequence, source_status`。计划、观测、预测与实际实现事件以类型区分。前序边存结束事件及必要滞后 `min_lag_s`，有证据时加 `max_lag_s`。仿真中尚未发生的派生事件可有公式，无实现时点。 |
| `evidence` | 值级 `source_status` 取 `observed`、`derived`、`assumed` 或 `pending`，配 `source_ref, unit, bounds, impact`。题设给定值也标明题面出处，经推导值列原事件与规则。每项约束另记 `applicability`，取 `active`、`not_applicable` 或 `unknown`。未知用 `null`，不代表零、无限容量或无限时窗。文件哈希、相对路径、版本供审计；原表行列及实际值只落授权本机。 |
| `scope` | 以计划需求台账为基数，保留路线未解析、身份冲突、截尾等未生成任务的炉次和缺口。注明统计截止与报表单位。计划版本变更携带公告与生效时刻。 |

### 任务记录

| 字段 | 类型与使用 |
| --- | --- |
| `task_id, heat_id, ladle_id, campaign_id, campaign_order` | 唯一稳定键及炉序。浇次含连铸机键。物理钢包跨炉复用用相同 ID；允许临时 ID 时标明条件与受影响任务。 |
| `route, scope, load_state, origin, destination` | 计划路径、执行范围（`ef_crane` 或 `external_service`）、载荷状态（`loaded` 或 `empty_ladle`）、规范站点键。仅 `ef_crane` 形成派车决策，`external_service` 使用外部证据。空车调位属于行车轨迹。`service_variants` 记录 EF 站位、许可和交接事件，外部资源 ID 按证据选填。 |
| `mass_t` | 对象 `lo, hi, source_status`。以毛重上界校容量，点值用相同上下界。未知、量纲或来源冲突按准入处理。 |
| `predecessors, release_interval` | 前序事件键、必要时差和外部约束。释放含 `lo_s, hi_s, rule, source_status`。规则随仿真前序更新，区间给情景依据。`physical_release_s` 只储存该实例已实例化的物理释放，`historical_pickup_proxy_s` 单独存。代理实例用带类型的 `applied_release_s`；同一公式和输出明确本次采用哪种释放。 |
| `pickup_window, delivery_window` | 键 `hard_earliest_s, hard_latest_s, plan_earliest_s, plan_latest_s` 各带适用状态、来源和约束事件引用。`pickup_window` 绑定 `pickup_start`，`delivery_window` 绑定实际交付事件（一般为 `drop_complete`，连铸座包为 `seat_complete`）。动态资源可用另由占用事件约束。计划提前量、硬准备历时和真实供钢截止分别在事件表关联。 |
| `capacity_candidates, eligible_cranes` | 前者仅吨位筛选，后者与数据准入的 EF 派车集合、起讫站、完整许可及载荷类别取交，按 `variant_id` 保存。待核用 `null`，已确认无合法行车用 `[]`。派车与回放按所选变体查其名单并传递交接端点。随故障和预约变化的可达性从快照判断。 |
| `resource_requirements` | 资源/替代组、需求量、开始及释放事件、授权和可能的接驳选择。处理位、物理站位、渡车等不同占用各自入表。 |
| `source_status, admission, blocker_codes, required` | `admission` 取 `A`、`B` 或 `C`，记录值级证据、规则版本与场景假设。是否在目标范围内、是否到期与是否准入是独立标记。C 记录依赖阻塞及缺失来源。 |

**与 B 预处理对接。** 读其 `events.csv, tasks.csv, calibration.csv, audit.json`。保留 `source_*`、计划与实测、缺失标志、身份匹配和原候选腿键。从 `process_predecessor_s` 建待核的工艺前序，从 `release_proxy_s`/`observed_lift_s` 取得历史对照，从 `observed_arrival_s` 找该腿对应的到卸点/放下代理，从 `plan_outer_deadline_s` 找计划外界。`gross_weight_t` 与 `weight_status` 按原毛重口径映射。`cranes_by_capacity` 仅映射 `capacity_candidates`。不自动填 B 留空的 `physical_release_s/window_open_s/window_close_s/eligible_cranes`。原同炉次链不自动生成跨炉强制运输边。同浇次计划由规范炉序、转台和供钢规则另行连接。判定 A 级需要的证据缺失时，有完整授权情景才置 B，否则置 C 并保留需求，不悄悄删行。

**行级身份处理。** 在授权本机记录 `workbook_sha256, sheet, row_1based, column, field, raw_value, normalized_value, correction_reason, authority_ref, status`，区分 `identity_conflict`、`ambiguous`、数据方勘误与项目批准（`project_approved`）。用熔炼号收集关联行，核对记号、真实包号与临时罐号、设备及跨日时间，禁止用全局字符串替换。在确认前 `normalized_value=null`，原观测照存，使用该身份的候选腿及不能独立确立持有者的后继都标 C 并保留依赖。批准后逐条核对工作簿版本、原值和唯一坐标，在入模映射应用，不改原表。对目标炉次重做行匹配、规范事件、候选腿、校准和前序，核对相邻持有链；其他炉次做无变化检查。若用现有预处理全表重跑，变化范围仍限定在批准记录及受影响链。LF3、RH1、CC3 的三个独立批准映射及复跑差值见本机 `problem/data/processed/stage02_b_ladle_identity_approved/audit.json`，现有 B 预处理脚本和原有派生目录保持原样。倒渣位标签恢复后仍需按站点拓扑核对可达性。

### 外部交接输入

沿用 `events`、`resources` 和 `external_service`。在相关记录上以 `handoff_id, ef_station_id, readiness_event_id, receipt_or_release_event_id, holder_id, source_status` 关联边界，接收窗、容量、外部资源 ID 和许可有证据时提供。缺少具体外部车号可使用证据支持的接口级接收方，仍须有可追溯的持有转移。缺少必要接点或就绪信息时按准入规则处理。历史草案的 `transfer_layout`、`transfer_vehicles`、`eligible_transfer_vehicles` 及渡车轨迹退出当前接口。已有原型和回归格式沿用其自身版本。

## 2. 状态、接口与预约

`initial_state` 和每次重调度快照用同一结构，含 `now_s, state_version, plan_version, cranes, holders, active_operations, occupancy, campaign_state, task_states, locked_reservations, visible_notices`。EF 行车状态含 `crane_id, track_id, x, v, gear, mode, carried_task_id, held_ladle_id, motion_or_operation_phase, phase_started_s, remaining_service_s`，另记窗口 `dispatch_allowed`（可派、不可派或未知）、`track_presence`（在轨、离轨或未知）及证据。已知占轨但不可派的车仍纳入轨迹或静止包络。停机车保留制动进度与安全占轨后续。边界占用、持有者和公告沿用共同结构。浇次状态保持炉序、正在供钢炉次与有证据的缓冲/剩余工时。Q1 固定 EF 参数及边界条件，Q2 加入扰动与动力学，Q3 更新故障及边界可用性。

事件回放区分 `pickup_start, pickup_complete, delivery_arrival, drop_start, drop_complete, seat_complete, prepare_complete, supply_start, supply_end`。落包即座包时可共用一个原子事件并记录两种语义。取载完成及放下完成各转移一次持有权。中断发生在取放中间时保存动作进度，按设备规则恢复或者安全处置。以物理顺序或原子交接处理同刻动作，半开区间允许真实的无重叠衔接。对策略不可见的实际故障或时间保留在仿真侧。

| 逻辑接口 | 合约 |
| --- | --- |
| `build_instance(plan, observations, config, boundary) -> instance, coverage` | 校验单位、ID、事件语义、必需字段、图无环和初态。冻结 A/B/C 及未建腿的需求台账。 |
| `make_candidate(task, variant, crane, state, template, reservations) -> candidate_or_rejection` | 输出带完整空驶、待避、装卸、持有与资源边界的候选。同一时域内未改动车辆继承已有轨迹或合法驻车。拒绝给出可定位的原因，不作全局不可行判断。 |
| `policy(visible_state, plan_view, options) -> proposals` | 同一生成器下复现 FCFS 与余量规则，稳定并列规则。只读已公告的事件、许可、信息版本和预测，包括计划已知需求的预调位。策略无法访问未来实际时点或采样器。 |
| `simulate(instance, policy, scenario_stream) -> trace` | 仿真侧根据前序、资源、已承诺动作与外生样本推进真实世界，到对应公告时点公开事件。随机键按情景及事件/工步/设备身份稳定索引，不按策略内调用次数索引。 |
| `repair(visible_state, plan_suffix, limits) -> proposals, diagnostic` | 局部修复再滚动短窗。只允许替换未执行且可解锁动作。返回需替换的预约 ID、理由、快照版本、信息时点与候选安全后续。 |
| `validate(instance, trace, previous_prefix) -> findings, metrics` | 与派车和优化独立。检查 EF 范围与连贯占轨、全时段净距，以及接收就绪、交接占用、持有、工艺和容量。以输入及交付轨迹作为检查依据。提交前核对快照，回放通过才发布可执行动作。 |

## 3. 输出与统计

输出包版本 `mcm-uestc.dispatch-result/1`。全部记录使用相同快照、任务 ID 和时标。输出不确定量或情景值时保留类型，不将观测、假设与证据范围混在一个准入字段。

| 产物 | 最少字段及口径 |
| --- | --- |
| 逐任务结果 | 任务键、准入/范围、承运车/外部资源、前序和取放事件、物理/代理释放及类型、硬/计划界、准时/超窗/未交/推迟/阻塞、已观察等待和截尾、来源。列出到期任务及前后窗统计。 |
| EF 行车轨迹 | `crane_id, t0_s, t1_s, x0, x1, v0, v1, a, gear, mode, task_id, ladle_id, threshold_context`。按车覆盖共同验证时域，含已知静止占轨车。Q1 可从匀速段重算速度，Q2/Q3 验 $C^1$。输出注明占轨覆盖及未核边界。 |
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
| 2 | EF 交接及占轨边界 | 拒绝未就绪或未完成翻渣的落车。持有权在实际交接后转移，核验有据的接收容量及释放。检查已知占轨的未派遣或故障车，并记录关键未知状态是否阻止正式验收。 |
| 3 | [H1/H2/E1/E2 短窗](../01_topic_selection/validation/README.md#b-题复跑) | 保留并按原配置对照两项既有原型。旧五车、假设坐标及固定渡车接收边界只用于对应回归。当前范围下的授权输入另验设备准入、可信上下界、站点/持有及相邻占用。 |
| 4 | 并发、竞争与因果 | 合成竞争确认规则选择可分化。至少两个审核过的真实竞争窗测增量，分别评估预调位、非串行候选与短窗 MILP。固定同一样本和信息时点；对相同可见历史、不同未来实际路径，两次策略的当期承诺应相同。 |
| 5 | 三场景端到端 | 固定参数比较 Q1 基线/改进及有限候选 MILP；Q2 保留配对、留出及截尾；Q3 覆盖就绪延迟、设备不可用、停轨与带载故障，检查前缀/恢复和四种输出判定。所有结果附可追溯的回放及覆盖。 |

三问的开发及复用顺序参见 [Stage 03 对齐](../03_implementation/IMPLEMENTATION_ALIGNMENT.md)。现有 C 反例及旧回归有各自的边界和判据。源码、原型与授权回放的已核范围见[收敛报告](convergence_report.md#本次复核)。
