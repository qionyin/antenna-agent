# 版本日志

## V1 - 框架版 / 规划版

日期：2026-07-04

### 当前定位

本版本是 `antenna_agent_lab` 的 V1 框架版。它已经能把用户的天线研究需求组织成一个可追踪的任务流程，但默认不执行真实 CST 仿真，也不启动真实优化任务。

简单说：V1 负责“接收需求、整理证据、生成七阶段任务包、展示状态、保留审计记录”，不负责“真实跑 CST 出结果”。

### 已完成

- 后端服务入口：`web.api:app`。
- 前端控制台：中文界面，可查看任务、能力、记忆、审批、实时事件、报告列表。
- 任务创建：
  - 通用自然语言任务。
  - 基于 PaperWise 报告的论文规划任务。
- 七阶段任务包链：
  - `idea_card`
  - `experiment_contract`
  - `geometry_contract`
  - `run_manifest`
  - `result_packet`
  - `claim_assessment`
  - `next_iteration_plan`
- PaperWise 报告读取：前端可列出并选择报告，后端会读取报告摘要作为任务上下文。
- Antenna Skills 接入：
  - 已接入 `C:\Users\30626\.codex\skills\Antenna Skills` 下的离线 `packet_adapter.py`。
  - 每个任务阶段会生成对应的 `antenna_skill_packet` 附件。
  - 这些附件作为 Antenna Skills 协议层的真实连接证据。
- CST 安全限制：
  - V1 明确不执行真实 CST。
  - `run_manifest` 和 `result_packet` 强制声明 `no_cst_execution=true`。
  - 如果发现 `real_cst_execution=true`、`execution_mode=cst` 等真实运行标志，会被拦截。
- 能力注册：
  - 扫描 Antenna Skills 协议层。
  - 扫描 `E:\antenna skills` 的本地能力清单。
  - 标记 CST、优化、训练等高风险能力。
- 运行状态管理：
  - 任务黑板。
  - 版本回滚。
  - 审批记录。
  - 审计日志。
  - 事件流。
  - 失败归档和 dead-letter 记录。
- 记忆和检索：
  - L1 最近对话。
  - L2 事实/偏好记忆。
  - L3 工作流经验。
  - 简单向量检索和降级存储。

### 不在 V1 范围内

- 不打开 CST。
- 不运行 CST solver。
- 不做真实 GWO/ANN/CNN/LSTM 优化。
- 不把 PaperWise 摘要直接当成几何事实。
- 不自动生成可用于论文结论的仿真证据。
- 不把 `E:\antenna skills` 的执行能力直接放开运行。

### 本轮新增

- 前端从英文界面改为中文界面。
- 左侧导航变成可点击，报告列表点击后有选中反馈。
- Antenna Skills 从“只扫描登记”升级为“真实调用离线 packet adapter”。
- 保持 no-CST 策略不变。
- 新增测试验证：
  - 能生成 Antenna Skills packet 附件。
  - 不出现真实 CST 执行标志。
  - 原有后端、Web API、LangGraph 流程仍通过。

### 验证记录

已通过：

```powershell
python -m py_compile adapters\antenna_skills_adapter.py agent_runtime\scheduler.py
python -m unittest tests.test_runtime_core
python -m unittest tests.test_web_api
python -m unittest tests.test_langgraph_runtime
npm run build
npm run test:smoke
```

其中前端验证命令在 `web\frontend` 目录下运行。

### 下一版建议

V2 可以考虑做“受控执行版”：

- 在审批通过后，允许少量 dry-run 或 mock-to-real 桥接。
- 增加明确的 CST 长任务审批门。
- 把 `E:\antenna skills` 的具体技能从“能力登记”推进到“受控调用”。
- 在每次真实执行前生成 preflight plan。
- 对真实结果 CSV、S11、增益、轴比等做严格解析和证据分级。
# V2.0 - 最小真实 CST 闭环

## 目标

V2.0 新增真实模式 `real`，用于完成：

```text
真实 CST 单次运行 -> 结果解析 -> 审查 -> 报告 -> 前端展示
```

## 本版新增

- 新增 V2.0 六态状态机：`created / preflight_running / waiting_approval / running / failed / completed`。
- 新增 `CstRealRunAdapter`，只做外部 CST 能力包装，不修改原 skill。
- 新增真实 CST 审批门：solver 前必须进入 `waiting_approval`。
- 新增 task/review subagent 节点记录，前端可看到当前执行者和审查者。
- 新增 S11 / return loss / bandwidth 解析与审查。
- 新增 completed_report / failed_report。
- 新增 V2.0 前端控制台。

## 保留

V1 mock / packet 链路保留，不走 V2.0 real CST 路径。

# V2.1 - 真实任务输入与预检增强

## 本版新增

- 前端真实模式在创建前校验真实输入：未勾选 simulate 时，必须提供非 placeholder 的 `.cst project_path` 或 `model_json_path`。
- 后端 preflight 增强为结构化输出：旧的 `checks[].ok/reason` 和 `blockers` 字符串保留，新增 `status`、`severity`、`hint`、`blocker_details`、`structured_blockers`。
- 调度器把 preflight 快照写入 `task_metadata.preflight`，前端可直接展示 checklist。
- failed_report 明确标注 `preflight failed`、`CST: not_reached`、`Result: not_available`，并提示补充真实 `.cst project_path` 或 `model_json_path`。

## 验证记录

```powershell
python -m py_compile adapters\cst_real_run_adapter.py agent_runtime\scheduler.py
python -m unittest tests.test_runtime_core tests.test_web_api
npm run build
npm run test:smoke
```

其中前端命令在 `web\frontend` 目录下运行。

# V2.2 - PaperWise 只读证据池

## 本版本新增

- 新增 `PaperWiseAdapter.evidence_pool_summary()`，生成三类只读证据摘要：
  - `reports`：PaperWise 精读报告候选证据。
  - `vector_library`：PaperWise 向量库，存储精读论文语义块，用于论文复现和证据支撑。
  - `graph_library`：PaperWise 图谱库，存储论文、概念、结构、指标之间的关系，用于创新判断和关系证据。
- V2 real CST 任务创建时写入 `task_metadata.evidence_pool_summary`。
- 任务 artifact 登记 `paperwise_evidence_pool_summary.json`。
- completed / failed report 增加 `PaperWise Evidence Pool` 段。
- 前端增加 `PaperWise 证据池` 面板，显示报告、向量库、图谱库状态和用途。
- 能力快照新增：
  - `paperwise_report_reader`
  - `paperwise_vector_library_reader`
  - `paperwise_graph_library_reader`

# V2.2.1 - PaperWise 向量召回修正版

## 相对 V2.2.0 的定位

V2.2.0 已经完成 PaperWise 只读证据池接入，包括：

- `reports`
- `deep_read_papers`
- `graph_library`
- 前端 PaperWise 页面
- 精读报告 / 创新 两个主展示区
- 本地硬约束匹配和可复现排序

V2.2.1 不新增大系统能力，只修正 V2.2.0 的一个关键问题：

```text
向量召回必须真的优先走 PaperWise Chroma 向量库，
不能把 SQLite 全文检索当成向量召回展示。
```

## 本版本新增和修正

- 优先调用 PaperWise 官方接口：
  `research_helper.kb.store.query(query, top_k=..., mode="paper")`
- 新增真实召回后端标记：
  - `paperwise_kb_store_query`
  - `sqlite_fulltext_fallback`
- 新增 `vector_query_error`，用于显示向量召回失败原因。
- PaperWise embedding 改回 Qwen / DashScope 通道：
  - `EMBEDDING_PROVIDER=qwen`
  - `EMBEDDING_MODEL=text-embedding-v3`
  - `QWEN_API_KEY` 从 PaperWise `.env` 读取。
- 前端聊天 LLM 配置不再被误用于 PaperWise embedding。
- 调用 Chroma 前检查 `papers` collection 是否存在。
- 前端 PaperWise 页面新增：
  - `Vector backend`
  - `chunks`
  - `papers`
  - `Vector fallback reason`

## 验证结果

```text
python -m unittest tests.test_runtime_core tests.test_web_api
npm run build
npm run test:smoke
```

结果：

```text
后端单测：73 passed
前端 smoke：4 passed
```

真实 PaperWise 召回诊断：

```text
vector_backend = paperwise_kb_store_query
vector_error   = None
vector_status  = available
vector_items   = 1
deep_read_items = 10
```

## 详细报告

见：

```text
docs\v2_2_1_update_report.md
```

## 边界

- 只读接入 PaperWise。
- 不调用 `sync-kb`、`rebuild-kb` 等可能写库或触发 embedding 的命令。
- 不修改 `C:\Users\30626\.codex\skills\Antenna Skills`。
- 不修改 `E:\antenna skills`。

# V2.2.2 - Skill 系统渐进披露与执行门

## 本版本目标

V2.2.2 把 skill 调用从“中枢硬编码猜测”改为“注册表路由 + reviewer 审查 + gate 放行”。

核心变化：

```text
新执行层管“该不该调谁”
旧执行层管“被允许后怎么调”
```

旧 V1 packet chain 仍然保留，但不能再无条件调用 Antenna Skills adapter。

## 本版本新增

- 新增 `skill_system/` 项目内 skill 注册层。
- 新增 `agent_runtime/skill_registry.py`。
- 新增 `agent_runtime/skill_disclosure.py`。
- 新增 `agent_runtime/skill_matcher.py`。
- 新增 `agent_runtime/skill_executor_gate.py`。
- 重构 `agent_runtime/skill_router.py`，输出固定 `skill_route_plan`。
- 重构 `agent_runtime/scheduler.py`，adapter 调用必须经过 gate token。
- 路由阶段最多使用 Level 2，不读取原始 `SKILL.md`。
- 只有 gate 放行后，旧 adapter 才能触达原 skill 或 `packet_adapter.py`。
- 增加 adapter 上游依赖检查，避免旧 adapter 吃错 packet。
- 真实模式天线上下文会路由到 `e-platform-cst`。
- 非天线任务不会调用天线 adapter。

## 重要行为变化

以前：

```text
_run_packet_stage()
-> _run_antenna_skill_adapter()
```

现在：

```text
_run_packet_stage()
-> _prepare_packet_payload()
-> skill_executor_gate.check()
-> gate 通过才调用 _run_antenna_skill_adapter(..., gate_token)
```

如果 route 没选中对应 skill：

```text
adapter 不调用
普通 packet 继续生成
记录 skipped_by_skill_gate
```

## 验证记录

```text
python -m py_compile agent_runtime\skill_registry.py agent_runtime\skill_disclosure.py agent_runtime\skill_matcher.py agent_runtime\skill_executor_gate.py agent_runtime\skill_router.py agent_runtime\scheduler.py
python -m unittest tests.test_runtime_core
python -m unittest tests.test_web_api
npm run build
npm run test:smoke
```

结果：

```text
tests.test_runtime_core: 58 passed
tests.test_web_api: 18 passed
frontend build: passed
frontend smoke: 4 passed
```

## 详细报告

见：

```text
docs\v2_2_2_skill_system_refactor.md
```

## 边界

- 不修改原始 Codex skill 目录。
- 不修改 `E:\antenna skills`。
- 不跑真实 CST solver。
- 不做完整论文复现闭环。
- 不做完整创新性评估。

# V2.2.2 数据补充 - Skill 路由准确率与执行安全性提升

## 版本重点

V2.2.2 的核心价值是把 skill 系统从“能不能调用”升级为“该不该调用、为什么调用、能不能阻断误调用”。

相比 V2.2.1，本版的数据提升集中在三件事：

```text
路由结果结构化
adapter 调用受 gate 控制
skill 触发准确率可量化验证
```

## 相对 V2.2.1 的提升

| 项目 | V2.2.1 / 旧逻辑 | V2.2.2 |
|---|---:|---:|
| 固定 skill_route_plan | 无 | 有 |
| 路由阶段读取边界 | 不清晰 | 最多 Level 2 |
| 旧 adapter 是否需要 gate token | 不需要 | 必须需要 |
| 未选中 skill 是否会被执行层误调 | 有风险 | gate 拦截 |
| 用户旧 100 条触发准确率 | 无稳定数据 | 100% |
| 用户旧 100 条 primary 准确率 | 无稳定数据 | 100% |
| 用户旧 100 条 false positive | 无稳定数据 | 0 |
| 用户旧 100 条 false negative | 无稳定数据 | 0 |
| 同类泛化样本触发率 | 48% | 86% |
| 同类泛化样本触发提升 | 基线 | +38 个百分点 |
| 同类泛化样本相对提升 | 基线 | 约 79.2% |
| 独立新 100 条触发准确率 | 未测 | 86% |
| 独立新 100 条 primary 准确率 | 未测 | 87% |

其中 `48% -> 86%` 是最重要的数据提升：

```text
旧 skill 系统同类泛化触发率：48%
V2.2.2 新系统同类泛化触发率：86%
绝对提升：+38 个百分点
相对提升：约 79.2%
```

## 已知 100 条样本结果

使用用户之前提供的 100 条 ground truth：

```text
C:\Users\30626\Documents\Codex\2026-07-09\skill-skill-2\outputs\antenna_skill_trigger_tests.csv
```

真实调用项目内：

```text
agent_runtime.skill_router.SkillRouter
```

结果：

```text
rows: 100
trigger_ok: 100
trigger_rate: 100%
primary_ok: 100
primary_rate: 100%
false_positive: 0
false_negative: 0
```

这说明 V2.2.2 已经把之前明确暴露的失败样本修掉了。

覆盖样本包括：

```text
复现这篇论文
这个结构怎么建模
把图里的尺寸整理出来
检查这个结果能不能支撑 claim
审查一下，不要修复
S11 不行
增益太低
React 性能优化
CNN 准确率太低
CST-style card layout
VSCode 端口占用
```

## 独立新 100 条泛化测试

为了避免只对旧 CSV 过拟合，V2.2.2 又新建了一套独立 100 条测试集。

这套数据不读取旧 CSV，仍然真实调用当前 `SkillRouter`。

结果：

```text
rows: 100
trigger_ok: 86
trigger_rate: 86%
primary_ok: 87
primary_rate: 87%
false_positive: 6
false_negative: 8
```

结果文件：

```text
work\skill_router_v222_fresh_eval\fresh_100_skill_route_eval.csv
work\skill_router_v222_fresh_eval\fresh_100_skill_route_eval_summary.json
work\skill_router_v222_fresh_eval\fresh_100_skill_route_eval_report.md
```

这组数据说明：

```text
V2.2.2 对已知场景提升明显。
但对全新表达方式仍有泛化缺口。
```

这是好事：以前系统只能靠感觉判断 skill 有没有接好，现在能用数据指出具体错在哪里。

## 新旧样本对比

这张表分开看三个口径：

```text
旧 skill 系统 48%：旧系统在同类泛化样本上的触发基线。
旧 100 条 ground truth：验证已知失败样本是否修复。
独立新 100 条：验证 V2.2.2 对新表达的泛化能力。
```

### 旧系统 vs 新系统触发率

| 对比项 | 旧 skill 系统 | V2.2.2 新系统 |
|---|---:|---:|
| 测试口径 | 同类泛化样本触发 | 同类泛化样本触发 |
| 触发率 | 48% | 86% |
| 按 100 条折算 | 48/100 | 86/100 |
| 绝对提升 | - | +38 个百分点 |
| 相对提升 | - | 约 79.2% |

### V2.2.2 内部回归 vs 泛化

| 对比项 | 旧 100 条 ground truth | 独立新 100 条 |
|---|---:|---:|
| 样本来源 | 用户历史样本 | V2.2.2 后新建样本 |
| 是否复用旧 CSV | 是 | 否 |
| 测试目的 | 验证旧失败样本是否修复 | 验证新表达的泛化能力 |
| 测试方式 | 真实调用 `SkillRouter` | 真实调用 `SkillRouter` |
| 样本数 | 100 | 100 |
| trigger_ok | 100 | 86 |
| trigger_rate | 100% | 86% |
| primary_ok | 100 | 87 |
| primary_rate | 100% | 87% |
| false_positive | 0 | 6 |
| false_negative | 0 | 8 |

对比结论：

```text
旧 100 条全部通过，说明 V2.2.2 已经修复已知失败样本。
新 100 条没有全部通过，说明当前 router 仍有泛化问题。
```

这组对比不能理解成“新样本比旧样本退步”。

更准确的理解是：

```text
旧样本证明：已知问题被修复。
新样本证明：系统现在能暴露未知问题。
```

V2.2.2 的提升不是“所有输入都完美识别”，而是：

```text
从不可解释、不可量化，变成可解释、可测试、可继续迭代。
```

## 主要改进点

### 1. 中枢路由可解释

新增：

```text
skill_system/categories/**/*.json
skill_system/registry/skill_index.json
skill_system/registry/category_index.json
skill_system/registry/routing_aliases.json
```

中枢不再直接读原始 `SKILL.md` 猜能力，而是先读取项目内轻量注册卡。

### 2. adapter 执行可阻断

旧逻辑：

```text
_run_packet_stage()
-> _run_antenna_skill_adapter()
```

新逻辑：

```text
_run_packet_stage()
-> skill_executor_gate.check()
-> gate 通过才允许 _run_antenna_skill_adapter(..., gate_token)
```

没有 token、packet_type 不匹配、route 不存在，都会拒绝执行。

### 3. 旧执行层保留但被管住

V1 七阶段 packet chain 仍然保留：

```text
idea_card
experiment_contract
geometry_contract
run_manifest
result_packet
claim_assessment
next_iteration_plan
```

但它失去了自主调用 skill adapter 的权力。

### 4. 依赖错误不再拖垮任务

旧 adapter 需要 skill_packet 输入。

V2.2.2 增加依赖检查：

```text
experiment_contract 需要 idea_card skill_packet
run_manifest        需要 geometry_contract skill_packet
claim_assessment    需要 result_packet skill_packet
next_iteration_plan 需要 claim_assessment skill_packet
```

依赖不满足时跳过 adapter，普通 packet 继续生成。

## 当前不足

独立新 100 条测试暴露了三类问题：

```text
阶段词漏识别：
result_packet / claim_assessment / ARBW / radiation pattern

非天线语境误触发：
金融 return loss、前端 gain chart、Python patch、CST Results card

primary skill 细分不足：
objective contract、hypothesis、baseline、novelty
```

所以 V2.2.2 不是终点。

它完成的是：

```text
已知失败样本清零
执行层误调用被 gate 管住
路由质量第一次可以量化
```

下一步 V2.2.3 应继续提升独立测试泛化率。

## 验证记录

```text
python -m py_compile agent_runtime\skill_registry.py agent_runtime\skill_disclosure.py agent_runtime\skill_matcher.py agent_runtime\skill_executor_gate.py agent_runtime\skill_router.py agent_runtime\scheduler.py
python -m unittest tests.test_runtime_core
python -m unittest tests.test_web_api
python -m unittest tests.test_langgraph_runtime
npm run build
npm run test:smoke
```

结果：

```text
tests.test_runtime_core: 58 passed
tests.test_web_api: 18 passed
tests.test_langgraph_runtime: 4 passed
frontend build: passed
frontend smoke: 4 passed
```

## 详细报告

见：

```text
docs\v2_2_2_skill_system_refactor.md
```
## V2.2.3 - Dynamic Plan And Review Reflection

本版把默认任务流程从固定七阶段 packet chain 改为中枢动态计划。

### 前后对比

| 项目 | V2.2.2 之前 | V2.2.3 |
|---|---|---|
| 默认流程 | 固定七阶段 packet chain | `dynamic_plan` |
| 七阶段作用 | 默认强制主流程 | reference / legacy / test 对照 |
| 中枢 agent | 主要做路由和调度 | 生成计划、记录版本、执行 reroute |
| 审查 agent | 审 packet / skill 输出 | 审每一步目标、证据、逻辑、依赖、风险 |
| Reflection | 无统一结构 | 加入 global_review_agent |
| 前端可见性 | 看不到动态步骤 | 可看计划版本、步骤、task/review agent、reflection |
| 真实 CST | V2.0 审批门存在 | dynamic_plan 也显式包含 approval gate |

### 数据支撑

历史路由数据：

```text
旧 skill 系统触发率：48%
V2.2.2 route-only 触发率：86%
V2.2.2 route-only primary 命中率：87%
```

V2.2.3 真实 API 边界测试：

```text
测试方式：100 条全部 POST 到 http://127.0.0.1:8000
真实 CST：只创建到 waiting_approval，不 approve，不调用 solver
```

结果：

```text
trigger_ok: 84 / 100
trigger_rate: 84%

primary_ok: 76 / 100
primary_rate: 76%

reflection_ok: 99 / 100
reflection_rate: 99%

real_no_cst_ok: 100 / 100
failure_count: 30
duration: 161.36s
```

解释：

```text
V2.2.3 的 84% 是真实 HTTP API + 边界样本测试，
不能和 V2.2.2 route-only 的 86% 简单做同集对比。

它证明的是：
1. dynamic_plan 真实链路能跑。
2. reflection 机制基本有效。
3. 真实 CST 审批门没有被绕过。
4. skill primary 分类仍需继续加强。
```

主要暴露问题：

```text
false negative: 10
false positive: 6
primary skill miss: 14
reflection miss: 1
```

核心变化：

```text
旧默认：
idea_card -> experiment_contract -> geometry_contract -> run_manifest
-> result_packet -> claim_assessment -> next_iteration_plan

新默认：
central_agent -> dynamic_plan -> step task_agent -> global_review_agent
```

七阶段流程没有删除，改为：

```text
reference_template
legacy compatibility
test comparison path
```

显式 legacy 入口：

```text
Scheduler.create_legacy_packet_chain_task(...)
```

新增能力：

```text
agent_runtime/dynamic_plan.py
task_metadata.dynamic_plan
task_metadata.dynamic_plan_history
task_metadata.active_dynamic_plan_version
task_metadata.global_review_records
task_metadata.reflection_records
```

审查 Agent 增强：

```text
每一步输出都由 global_review_agent 审查
审查输出增加 reflection
reflection 不单独拆 agent
最多自动 reroute 1 次
真实 CST 审批门不能被 reroute 绕过
```

前端新增展示：

```text
动态计划版本
动态步骤列表
每步 task_agent / review_agent
latest reflection status
reflection error_type
why_central_failed
what_should_change
```

详细报告：

```text
docs\v2_2_3_dynamic_plan_reflection.md
```

## V2.2.4 - 真实多 Agent 与动态 Skill 路由

本版是在 V2.2.3 的 `dynamic_plan + reflection` 基础上继续升级。

V2.2.3 证明了动态计划链路能跑，但仍有两个问题：

```text
1. dynamic step 上的 task/review 更像字段标签，不像真实多 Agent 协作记录。
2. skill_route_plan 容易被理解成任务创建时一次定死，不能随流程变化。
```

V2.2.4 的目标是把这两点落成可检查、可展示、可测试的机制。

### 核心链路对比

```text
V2.2.3:
central_agent
-> dynamic_plan
-> step task_agent
-> global_review_agent

V2.2.4:
central_agent
-> step_skill_context
-> task_subagent
-> module_review_subagent
-> global_review_agent
-> central_decision
```

### 相对 V2.2.3 的变化

| 项目 | V2.2.3 | V2.2.4 |
|---|---|---|
| 多 Agent 形态 | step 里有 `task_agent / review_agent` 字段 | 每步产生 `task / module_review / global_review` 三类 subagent 记录 |
| 审查职责 | global review 容易覆盖模块审查 | module review 审当前产物，global review 审全局证据链和依赖 |
| 中枢职责 | 生成动态计划和记录 reflection | 收集三类 subagent 结果，并写入 `central_decision` |
| Skill 路由 | 初始 `skill_route_plan` 容易一次定死 | 每个 step 前重新生成 `step_skill_context` |
| 45%-70% 置信度 | 容易被误认为已经调用 skill | 明确进入 `candidate_skills`，只展示，不调用 adapter |
| 70% 以上置信度 | 未明确与执行权限强绑定 | 只有 `callable_skills` 才能进入 gate |
| Gate token | 已有 adapter gate | 绑定 `task_id / plan_version / step_id / owner_skill / packet_type / action` |
| 审批安全 | real CST 审批门存在 | LLM 审批、CST 审批、skill gate 继续分离 |
| 前端展示 | 动态计划、reflection | 当前阶段能力判断、候选未调用原因、置信度组成、模块审查、全局审查、中枢裁决 |

### 数据对比

V2.2.3 的真实 API 边界测试基线：

```text
样本数: 100
trigger_ok: 84 / 100
trigger_rate: 84%
primary_ok: 76 / 100
primary_rate: 76%
reflection_ok: 99 / 100
reflection_rate: 99%
real_no_cst_ok: 100 / 100
failure_count: 30
duration: 161.36s
```

V2.2.4 本次没有重新声明 100 条路由准确率提升。
本版提升的是“执行语义”和“安全可验证性”：

| 指标 | V2.2.3 | V2.2.4 |
|---|---:|---:|
| dynamic step 审查层级 | 1 层 global review | 2 层：module review + global review |
| 每步 subagent 记录类型 | 约 1 类 review 记录 | 3 类：task / module_review / global_review |
| 每步中枢裁决记录 | 无独立 `central_decision` | 有 |
| 每步 skill 重算 | 无稳定 `step_skill_context` | 有 |
| skill route 历史 | 初始 route 为主 | `initial / active / history` 三类可见 |
| 45%-70% skill | 可能被误解为可调用 | 固定为 candidate only |
| gate token 过期测试 | 未锁定 | 已锁定 plan_version / step_id |
| V2.2.4 专项后端测试 | 0 | 6 |
| V2.2.4 API 可见性测试 | 0 | 1 |

回归测试数量对比：

```text
上一条完整回归基线:
tests.test_runtime_core: 58 passed
tests.test_web_api: 18 passed
tests.test_langgraph_runtime: 4 passed
frontend smoke: 4 passed

V2.2.4:
tests.test_runtime_core: 66 passed
tests.test_web_api: 19 passed
tests.test_langgraph_runtime: 4 passed
frontend build: passed
frontend smoke: 4 passed
```

测试覆盖增长：

```text
runtime_core: 58 -> 66, 增加 8 个测试
web_api: 18 -> 19, 增加 1 个 API 可见性测试
langgraph_runtime: 4 -> 4, 保持通过
frontend smoke: 4 -> 4, 保持通过并更新 2.2.4 文案断言
```

### 新增验收测试

```text
test_v224_dynamic_routes_have_initial_active_history_and_step_contexts
test_v224_dynamic_steps_record_task_module_global_subagents
test_v224_candidate_48_percent_skill_is_not_callable_and_does_not_open_gate
test_v224_gate_token_expires_on_plan_version_or_step_change
test_v224_module_review_block_cannot_be_passed_by_central_decision
test_v224_task_state_exposes_callable_candidate_and_step_contexts
test_v224_task_state_exposes_callable_candidate_and_step_contexts   # web_api
```

这些测试分别锁定：

```text
1. initial_skill_route_plan / active_skill_route_plan / skill_route_plan_history 存在。
2. 每个 dynamic step 都有 step_skill_context。
3. 每个 step 都有 task / module_review / global_review 三类 subagent 记录。
4. 48% 这类弱命中只能进入 candidate_skills，不能打开 gate。
5. gate token 在 plan_version 或 step_id 改变后必须失效。
6. module review block 时，中枢不能强行 pass。
7. API / 前端能读到 callable_skills、candidate_skills、step_skill_context。
```

### 关键修复点

```text
1. 修复“审查 block 后仍可能 complete”的问题：
   _execute_dynamic_plan_steps 返回 final_decision；
   调用方根据 block / wait_user / refresh_skill_route / reroute_plan 更新任务状态。

2. 修复 module review 与 global review 混淆：
   module review payload 不再写死 global_review_agent。

3. 修复 skill_route_plan 被每步 route 覆盖的问题：
   初始路由保留在 skill_route_plan / initial_skill_route_plan；
   当前步骤路由写入 active_skill_route_plan 和 step_skill_contexts。

4. 修复真实 CST 路由丢失 cst-control：
   real 模式下 CST control 明确升为 callable route。

5. 修复旧七阶段兼容链 adapter 数量下降：
   PaperWise planning 链路继续保留 experiment / claim 等必要 callable route。

6. 修复缺 PaperWise 时被 runtime LLM 审批状态掩盖：
   非 settings 级 LLM 场景下，证据源缺失优先 block。
```

### 最终验证结果

```text
python -m unittest tests.test_runtime_core
结果: 66 passed

python -m unittest tests.test_web_api
结果: 19 passed

python -m unittest tests.test_langgraph_runtime
结果: 4 passed

npm run build -- --outDir temp-v224-build
结果: passed

npm run test:smoke
结果: 4 passed
```

说明：

```text
1. 本版验证没有调用真实 CST solver。
2. 本版没有修改 C:\Users\30626\.codex\skills。
3. 本版没有修改 E:\antenna skills。
4. V2.2.4 的数据提升重点是工程闭环可验证性，不是重新训练或重测 100 条路由准确率。
```

### 详细报告

```text
docs\v2_2_4_multi_agent_dynamic_skill_routing.md
```
# V2.2.5 - 唯一动态运行主链与步骤级实时沟通

V2.2.5 按最新版本优先原则，正式结束 V1 固定七阶段作为可执行流程。

现行唯一主链：

```text
dynamic_plan
-> step skill route
-> task subagent
-> module review subagent
-> global review + reflection
-> central decision
-> checkpoint / wait_user / next step
```

本版保留并合并此前有效能力：

- V2.0 的真实 CST preflight、人工审批、单次运行、解析、审查和报告。
- V2.2/2.2.1 的 PaperWise 报告、向量库和图谱只读证据。
- V2.2.2 的渐进披露 Skill registry、route review 和 gate token。
- V2.2.3 的动态计划、reflection 和受控 reroute。
- V2.2.4 的每步动态 Skill 判断与 task/module/global 多 Agent 记录。
- V2.2.5 的步骤级中枢实时回复、局部修订、受控重规划和风险确认。

真实 CST 审批现在绑定：

```text
task_id + request_hash + plan_id + plan_version + step_id
```

重复审批不会产生第二次 solver 运行；计划或请求变化后旧审批失效。

已退役：

- V1 固定七阶段 packet chain 创建和执行入口。
- LangGraph 固定七阶段运行图及其依赖。
- 早期 `agent/main_agent_skeleton.py` 原型和独立配置。
- 只用于旧 packet 执行的 agent 实现和测试。

兼容边界：历史 packet、artifact、报告和 blackboard 记录仍可只读查看，
但不能再创建、恢复或继续执行旧七阶段任务。`work/`、`workspace/` 和审计日志
作为历史证据保留，不在本次裁剪中删除。

## V2.2.5 架构收敛与真实执行补丁

本补丁完成 V2.0 至 V2.2.5 的最终合并。版本冲突按最新版本优先处理，
项目只保留 `dynamic_plan` 现行运行时。

### 与裁剪前对比

```text
裁剪前：V1 packet chain、LangGraph 固定流程、dynamic_plan 并存。
裁剪后：dynamic_plan 是唯一可执行主链；旧 packet 仅只读兼容。

裁剪前：普通动态 task subagent 只返回 status=prepared。
裁剪后：执行 PaperWise 只读检索、本地 handler 或 gate 授权 skill adapter。

裁剪前：计划审查结果只保存，不阻止步骤执行。
裁剪后：dynamic_plan review 非 pass 时任务立即 failed。

裁剪前：CST 开始前 run_count=1，中断恢复后无法再次执行。
裁剪后：attempt_count 在启动时增加，run_count 仅在终态写 1；中断后重置审批。

裁剪前：任务状态混有 complete、blocked、cancelled、abandoned。
裁剪后：只使用 created、preflight_running、running、waiting_approval、failed、completed。

裁剪前：向量 chunk 可能错误套用全库第一篇论文的元数据。
裁剪后：按 embedding row id 绑定 paper_id、title、source 和原论文 report.md。
```

### 真实验证数据

```text
runtime_core: 67 -> 72 passed
web_api: 21 passed
frontend build: passed
Playwright smoke: 4 passed

普通动态天线任务：
  动态步骤: 6
  task/module/global subagent 记录: 18
  实际 Antenna Skill adapter: 4
  历史 packet 字段新增数量: 0
  本机 HTTP 完成时间: 6.32 秒

simulate_cst=true 的真实 HTTP 闭环：
  最终状态: completed
  module review: 6
  global review: 7
  report: 1
  重复审批后的 run_count: 1
  解析 bandwidth_10db: 0.48

PaperWise 只读验证：
  SQLite backend: sqlite_read_only
  检索结果: 10 条
  数据库 SHA256 检索前后完全一致
  数据库 mtime 检索前后完全一致
```

普通动态任务实测调用：

```text
antenna-claim-experiment-planner
antenna-research-ideation
antenna-result-to-claim
antenna-research-reviewer
```

补齐的动态 action：

```text
baseline_ablation
antenna_result_to_claim
evidence_check
```

### 多 Agent 上下文压缩 A/B 评测

本节记录 V2.2.5 的上下文压缩实验。它是使用真实外接模型完成的评测，尚未作为生产默认策略接入运行时。

测试条件：

```text
样本：100 条真实运行日志
难度：简单 34 / 中等 33 / 复杂 33
模型：gpt-5.5
外接 API 调用：800
成功：800
失败：0
Redis 记忆：L2 事实 3 条 / L3 工作流 1 条
```

压缩策略：

```text
task subagent    -> 当前步骤 + Top2 相关事实
module reviewer  -> 当前产物 + 当前步骤
global reviewer  -> 当前产物 + 最新审查 + Top1 相关事实
central agent    -> 当前计划 + 最近 6 条对话 + Top2 事实 + Top1 工作流
```

仅统计实际发送给 Agent 的 `context` JSON 时：

| Agent | 基线上下文 Token | 压缩后 Token | 节省 Token | 上下文压缩率 |
|---|---:|---:|---:|---:|
| task subagent | 133,049 | 40,712 | 92,337 | 69.40% |
| module reviewer | 133,049 | 13,448 | 119,601 | 89.89% |
| global reviewer | 133,049 | 34,587 | 98,462 | 74.00% |
| central agent | 133,049 | 80,078 | 52,971 | 39.81% |
| **整体** | **532,196** | **168,825** | **363,371** | **68.28%** |

包含系统提示、输出格式和模型调用开销的完整指标：

| Agent | 准确率变化 | Prompt Token 节省 | 平均延迟变化 |
|---|---:|---:|---:|
| task subagent | 100% -> 100% | 14.22% | 8057.9 -> 7855.9 ms |
| module reviewer | 100% -> 100% | 18.63% | 8200.5 -> 5479.4 ms |
| global reviewer | 100% -> 100% | 13.82% | 8289.1 -> 7143.2 ms |
| central agent | 100% -> 99% | 8.48% | 8078.2 -> 7630.9 ms |
| **整体** | **100% -> 99.75%** | **13.78%** | **8156.4 -> 7027.4 ms** |

完整总 Token 从 `2,613,033` 降至 `2,245,907`，节省 `367,126`，降幅 `14.05%`；平均响应时间降低 `1129.1 ms`，降幅 `13.84%`。

唯一偏差发生在中枢：当前步骤已经 `passed` 时，压缩上下文曾错误返回 `complete_task`，而标准答案是 `pass_next_step`。随后明确规则：

```text
当前步骤 passed -> pass_next_step
当前步骤裁决禁止直接返回 complete_task
只有 Scheduler 确认所有必需步骤终态通过后，才能把整个任务设为 completed
```

对失败样本 `context-047` 重新调用真实模型后返回 `pass_next_step`，与标准答案一致。该修正约束的是“步骤通过”和“任务完成”的边界，不改变模块审查或全局审查职责。

评测产物：

```text
docs/evaluations/context_compression_agents_100_20260722/summary.json
docs/evaluations/context_compression_agents_100_20260722/actual_context_token_metrics.json
docs/evaluations/context_compression_agents_100_20260722/central_context047_retest.json
docs/evaluations/context_compression_agents_100_20260722/report.md
```

结论：前三类生产 LLM 角色的上下文压缩有效，准确率未下降；中枢压缩收益较低且曾出现一次完成边界错误，因此生产接入时必须保留 Scheduler 的确定性最终状态控制，不能让压缩后的单步 LLM 裁决直接提交 `completed`。

### 边界

- 未启动真实 CST solver；真实 HTTP 链使用 `simulate_cst=true`。
- 未修改 `C:\Users\30626\.codex\skills\Antenna Skills`。
- 未修改 `E:\antenna skills`。
- PaperWise 原 SQLite 曾被旧 Chroma 查询路径修改；本补丁以当前文件为新基线，
  新查询路径已验证不会继续修改数据库。

## V2.2.5 能力恢复与真实全链验证补丁

本补丁恢复裁剪过度的两项能力，但不增加第二条执行链：

```text
PaperWise：SQLite 全文回退 -> 官方 KB store + 临时 Chroma 副本语义召回
Skill packet：只检查 live-CST 标记 -> envelope、类型、必填 payload 严格校验
```

PaperWise 官方库保持只读。实测查询 5 条结果全部来自
`paperwise_chroma_vector`；查询前后 `chroma.sqlite3` 的 SHA256、mtime 和大小一致。
外部 embedding 未获批或失败时，继续使用 SQLite `mode=ro` 回退。

真实动态 Skill 链实测：

```text
任务: d19535ff5ae9f96c
动态步骤: 5/5 passed
真实 skill packet: 3
packet schema 校验: 3/3 passed
subagent/module/global/central 记录: 15/5/6/5
```

真实 CST 全链实测：

```text
任务: 6a39cbcec062e701
simulate_cst: false
审批: waiting_approval -> approved -> running
CST 官方 Python runner: status=ok
S11 导出: 1001 点
S11 最小值: -9.7695356 dB @ 1.027 GHz
-10 dB 带宽: 0.0 GHz（模型未达到阈值，不伪造通过）
module/global/central 记录: 8/9/8
报告: 1
最终状态: completed
```

完整回归：后端 `80/80`，前端 build 通过，Playwright `4/4`。
# 唯一执行链收口与冗余清理

- 真实 CST、失败报告、PaperWise 重审统一进入 `_execute_dynamic_plan_steps()`。
- 真实 solver 调用点收敛为 1 个：`CstRealRunAdapter.run_single()`。
- 删除 V1 packet 写入、`PacketStore`、重复 Agent 包装器和虚构 executor/challenger 节点。
- 保留并重新接入 L2/L3 记忆、embedding 和 Redis 向量检索；步骤级 skill 路由可读取记忆召回上下文。
- 保留历史 packet 只读兼容、checkpoint、回滚、维护和审计能力。
- 详细判断见 `docs/redundancy_audit.md`。
- 验证：后端 `77/77`，前端 build 通过，Playwright `4/4`。
- 独立 HTTP `simulate_cst=true`：`waiting_approval -> completed`，带宽解析 `0.48`。
- PaperWise SQLite/graph 在重审前后 SHA256 和 mtime 均不变。

## V2.2.5 检索排序升级：BM25 粗排 + Qwen 语义精排

更新时间：2026-07-22。

本补丁只调整 L2/L3 共享记忆检索子系统，不修改中枢调度、动态计划、
多 Agent、PaperWise、Skill adapter 或 CST 执行链。

### 生产检索链

```text
原查询
-> BM25 固定召回 Top50
-> Qwen Embedding 固定召回 Top15
-> 合并去重，粗排候选最多 65 条
-> 只使用原始查询的 Qwen cosine 语义分数精排
-> 原始查询语义阈值 0.51
-> 最终 Top5
```

最终排序不再使用：

```text
关键词 overlap 加权
SequenceMatcher fuzzy 加权
memory confidence 排名加权或同分决胜
BM25 分数与 embedding 分数混合
RAG-Fusion 查询扩展分数
```

BM25 分数只负责粗排候选；最终 `retrieval_score` 等于原始查询的
Qwen embedding cosine。返回结果新增 `retrieval_pipeline`、
`coarse_sources` 和 `bm25_score`，用于解释候选来源，但 `bm25_score`
不参与最终名次。

### 为什么未采用 RAG-Fusion

同一 1000 条测试中，GPT-5.5 生成两条相关问题并执行 RRF 后，
只要最终仍严格使用原始查询 Qwen 分数，Precision、Recall、F1 和 Hit@K
与不加 RAG-Fusion 完全相同；本地排序耗时从 4.108 ms 增加至 9.538 ms，
还额外产生外接 LLM 和 embedding 请求。因此本补丁不接入 RAG-Fusion。

### 更改前后数据

固定测试集：1000 条，固定种子 `20260721`；正向 800 条，负向 200 条。

| 指标 | 更改前 Hybrid | 更改后 BM25+Qwen+0.51 | 变化 |
|---|---:|---:|---:|
| Precision@K | 36.12% | 52.77% | +16.65 个百分点 |
| Recall@K | 56.31% | 69.60% | +13.29 个百分点 |
| F1@K | 44.01% | 60.03% | +16.02 个百分点 |
| Hit@K | 88.88% | 98.25% | +9.37 个百分点 |
| 负例拒绝率 | 11.00% | 71.00% | +60.00 个百分点 |
| Top1 命中率 | 77.88% | 97.12% | +19.24 个百分点 |

更改后 L2/L3 分层：

| 层级 | Precision@K | Recall@K | F1@K | Hit@K | 负例拒绝率 |
|---|---:|---:|---:|---:|---:|
| L2 | 59.76% | 62.58% | 61.14% | 98.00% | 75.00% |
| L3 | 45.79% | 76.62% | 57.32% | 98.50% | 67.00% |

生产 `RetrievalEngine` 使用真实 Redis 重载文本和缺失向量重建后，指标与独立
原型接近但不完全相同；以上表格以真实生产接入复测为准。生产路径平均排序耗时为 41.07 ms；该口径包含 Redis 记录读取、
BM25 索引重建、向量候选扫描和审计字段构造，但不包含首次外部 embedding
生成耗时。原型的 4.108 ms 仅为已缓存向量上的算法计算时间，两者不能直接
作为同一延迟口径比较。

### 已知边界

- 明确否定类 50 条仍全部失败，说明语义阈值不能替代否定意图 Gate。
- 失效记忆负例拒绝率为 84%，仍有 8 条被其他活动记忆误接收。
- `0.51` 绑定当前 `text-embedding-v4`、1024 维配置；更换模型后必须重新校准。
- 当前 Redis 无 RediSearch 时会走已有内存向量回退；重启后应保证向量重新加载。

### 验证

```text
tests.test_runtime_core: 68/68 passed
tests.test_runtime_core + tests.test_web_api: 89/89 passed
生产 RetrievalEngine + Qwen 缓存 + Redis DB15: 1000/1000 完成
生产接入指标: Precision 52.77%, Recall 69.60%, F1 60.03%
Redis DB15: 评测结束后清空
```

## V2.2.6 - Scheduler 主导的 LLM 动态计划

更新时间：2026-07-22

本版按最新架构要求收敛计划链，删除旧的规则模板计划器 `DynamicPlanBuilder`。当前唯一可执行链为：

```text
Scheduler
-> 调用 OpenAI-compatible LLM 生成 dynamic_plan 候选
-> Scheduler 硬校验候选计划
-> 调用 task / module review / global review subagent
-> Scheduler 根据审查结果更新最终任务状态
```

### 前后对比

| 项目 | 修改前 | V2.2.6 |
|---|---|---|
| 计划来源 | `DynamicPlanBuilder` 硬编码模板 | 当前配置的外接 LLM |
| 计划校验 | 旧计划器同时生成并审查 | Scheduler 独立确定性校验 |
| LLM 不可用 | 可能落回规则模板 | 明确失败 `external_llm_failed`，不静默回退 |
| 最终状态 | 子 Agent 侧提供裁决建议 | Scheduler 唯一提交 `completed/failed/waiting_approval` |
| 真实 CST 保护 | 既有审批和 gate | 保留，并由 Scheduler 校验候选顺序与 approval gate |
| 旧计划器 | 保留在生产路径 | `agent_runtime/dynamic_plan.py` 已删除 |

### 硬校验范围

- 候选必须是严格 JSON `dynamic_plan`，步骤不能为空。
- 动作必须来自 Scheduler 已实现的 handler 白名单。
- 所需 skill 必须存在于项目 registry。
- 真实模式必须按 `preflight -> approval -> cst_run -> result_parse -> result_review -> report` 排序。
- `cst_run` 必须声明 approval gate；LLM 不能声明完成，也不能绕过 skill gate。
- 计划生成失败或校验失败时，不执行任何步骤。

### 测试数据

| 验证项 | 结果 |
|---|---:|
| 运行时单元测试 | `70/70` 通过 |
| Web API 测试 | `21/21` 通过 |
| 前端生产构建 | 通过 |
| Playwright smoke | `4/4` 通过 |
| 生产真实 LLM 连接 | 通过，模型 `gpt-5.5` |
| 真实 LLM 候选计划 | `source=external_llm`，Scheduler validation `pass` |
| 真实 LLM 生成的步骤 | `general -> general_review -> report` |
| CST 调用 | 本次集成测试未调用 CST，避免无审批启动 solver |

真实连接测试使用了当前配置的 `https://api-cn.smallice.xyz/v1`，未使用测试夹具；测试输入为不涉及用户私有数据的通用任务。

评测结果：

```text
docs/evaluations/production_bm25_qwen_threshold051_1000_20260722/summary.json
```

## V2.2.6 - 动态执行链迁移回 LangGraph

更新时间：2026-07-24

本次只迁移编排层，不修改 PaperWise、CST adapter、Skill gate、任务 handler、
模块审查、全局审查和最终状态规则。原 `_execute_dynamic_plan_steps()` 手写循环
已删除，所有动态任务统一进入 `DynamicLangGraphRuntime`。

```text
LangGraph StateGraph
-> select_step
-> route_skill
-> task_agent
-> module_review
-> conditional global_review
-> central_decision
-> commit_step
-> finish
```

| 项目 | 迁移前 | 迁移后 |
|---|---|---|
| 编排方式 | Scheduler 内部 `for` 循环 | LangGraph 节点和条件边 |
| 动态状态 | Blackboard 手工推进 | `DynamicGraphState` |
| 持久化 | Blackboard + Redis | Blackboard + Redis，保持不变 |
| 业务共享状态 | Blackboard | Blackboard，保持不变 |
| 审批和 CST 规则 | 原规则 | 原规则，保持不变 |
| 可执行主链 | 1 条 | 1 条 |

验证结果：

```text
tests.test_runtime_core + tests.test_web_api: 94/94 passed
tests.test_langgraph_runtime: 3/3 passed
frontend Playwright smoke: 4/4 passed
frontend production build: passed
```

本次迁移测试沿用现有 CST 模拟审批夹具，没有再次启动真实 CST solver；历史真实
CST 调用点和审批规则未修改。

## V2.3 - 学习记忆与领域理解基础闭环

更新时间：2026-09-14

本版本针对 API 型大模型“能召回但不能沉淀经验、能生成 idea 但缺少领域约束”的问题，增加了受控记忆改写层。它不修改模型参数，也不把一次 LLM 输出直接当作正式知识。

| 项目 | 修改前 | V2.3 |
|---|---|---|
| 原文保存 | L2 事实中只有摘要和短摘录 | 独立 Redis 原文池，保存完整文本、哈希和来源定位 |
| 领域知识 | 任意调用方可写入 L2 fact | 论文原文编译为带证据的 domain knowledge candidate |
| 任务经验 | 只有完成工作流快照 | 终态任务保存 episode，并生成带错误归因的 experience candidate |
| 知识状态 | active/inactive 为主 | candidate、validated、promoted、contradicted 等生命周期 |
| 聚类 | 无受控学习聚类 | natural/semantic/random 候选聚类，固定 seed、温度和迭代边界 |
| 冲突 | 新记录可能覆盖旧结论 | 保留相反关系并标记 conditional conflict |
| 图谱来源 | PaperWise/研究图谱已存在，但 V2.3 曾误加 Redis 投影 | 已修复：不再创建第二张图谱，只读使用既有论文图谱 |
| Wiki 来源 | 主要展示论文知识 | 论文事实 + 明确标记的 promoted 实践经验 |
| 创新判断 | 主要依赖自由文本判断 | baseline-gap-delta + 论文/电磁/建模/实验四 Gate |
| 任务上下文 | 普通 L2/L3 召回 | 动态计划只读取 promoted learning context |

真实验证：

```text
V2.3 核心学习与边界测试 `tests.test_v23_learning` 12/12 passed；原 14/14 为旧组合口径，已拆分独立学习/进化模块测试。
其中学习 API/图谱来源测试 3/3 passed
既有运行回归 116/116 passed
前端生产构建 passed
真实 PaperWise report 导入 smoke：90 个证据单元、10 个候选知识，原文哈希一致
```

重要限制：本版证据编译器仍是确定性规则基线，不宣称已经实现完整的大模型领域理解；外部 LLM 抽取、真实历史反馈训练和大规模人工标注评估仍待后续版本。详见 `docs/v2_3_learning_memory_domain_understanding.md`。

## 模块化重构 - 单链职责拆分

更新时间：2026-09-30

本次只调整代码组织，不新增第二套执行链，不改变公开 API、CST 审批、Skill Gate、MCP 协议和 PaperWise 只读边界。

| 原堆积文件 | 重构前 | 重构后门面 | 迁出职责 |
|---|---:|---:|---|
| `agent_runtime/scheduler.py` | 2796 行 | 617 行 | 计划、中枢沟通、动态 action、证据审查、终态学习 |
| `agent_runtime/langgraph_runtime.py` | 1728 行 | 471 行 | planning/execution/repair 节点、State、Retry |
| `adapters/paperwise_adapter.py` | 1619 行 | 84 行 | 报告、向量库、图谱、查询画像 |
| `agent_runtime/memory.py` | 457 行 | 151 行 | V2.3 Repository 存取 |
| `agent_runtime/learning.py` | 783 行 | 27 行兼容入口 | extraction/review/clustering/innovation/wiki/service |
| `web/frontend/src/App.jsx` | 1967 行 | 约 1600 行 | Evidence、Learning、Reports、Logs、Settings 页面和公共组件 |

模块说明见 `docs/modular_architecture.md`。

验证结果：

```text
Python 全量回归：174/174 passed
MCP 回归：5/5 passed（含 Uvicorn 活动事件循环场景）
前端生产构建：passed
Playwright smoke：4/4 passed
真实 HTTP 普通任务：completed，生成 dynamic plan 和 Episode
模拟 CST HTTP：waiting_approval -> completed，生成 S11/带宽、报告、Episode 和经验候选
真实 CST solver：未调用
外部 LLM：完整流程 smoke 中明确禁用
```

# V2.3.1 - 独立学习记忆与受控自进化

更新时间：2026-10-01

本版本将 V2.3 学习记忆从运行时编排中抽成独立 `agent_learning/` 模块，并融合评测驱动的受控自进化能力。自进化不是知识沉淀的别名：知识沉淀负责保存数据，自进化负责根据评测修改系统行为并复测。

## 前后对比

| 项目 | V2.3 | V2.3.1 |
|---|---|---|
| 代码归属 | 学习实现位于 `agent_runtime` | 实现独立到 `agent_learning/`，旧路径仅兼容导出 |
| 论文知识 | 原文编译为 candidate | 增加实体边界、别名消歧、逐句关系绑定和抽取质量评测 |
| 任务经验 | Episode/Experience candidate | 保留，并同步把成功无 blocker 的动态计划写入旧 L3 workflow |
| 旧 L2 | 与新学习层并行存在 | 支持从 `config.yaml` 确定性注入项目事实，不修改 `store_l2` |
| 自进化 | 无行为修改闭环 | 训练集评测 → alias 候选 → 策略校验 → 修改 → 独立复测 → 保留/回滚 |
| 外部 LLM | 未接学习层 | 可选复用中枢同一 `LLMClient`、URL 和模型，只生成候选建议 |
| 风险边界 | candidate 需人工晋级 | Prompt、阈值、知识晋级、代码修改仍禁止自动执行 |

## 自进化范围

当前自动执行白名单只有：

```text
add_entity_aliases
```

候选 alias 必须满足：目标实体已注册、alias 在原测试文本中逐字存在、不是过短缩写或通用词，并且必须在独立 `verification_cases` 上提高指标；否则不应用或自动回滚。

外部 LLM 不拥有修改权。它只能分析失败样本并提出候选 alias，候选仍需经过本地策略和独立复测。

## 记忆系统调整

当前默认状态：

```yaml
learning:
  enabled: true
  domain_knowledge_enabled: false
  evolution_enabled: false
```

默认保留：

```text
mem:episode:*
mem:l3:experience:*
mem:l2:{namespace}:*
mem:l3:workflow:*
```

默认关闭：

```text
原文池 / Evidence / Domain Knowledge
候选聚类 / 创新 Gate / Wiki
自进化评测、修改与复测
```

`domain_knowledge_enabled=false` 时，相关 API 返回 `403 domain_knowledge_disabled`；`recall_promoted()` 的 knowledge 恒为空，但 promoted Experience 继续召回。配置中的 `l2_project_facts` 在 Scheduler 初始化时写入旧 L2；完成且无 blocker 的任务在 Episode 之外同步写入旧 L3 workflow。

## 开启方式

自进化系统必须显式开启：

```yaml
learning:
  evolution_enabled: true
```

环境变量覆盖：

```powershell
$env:LEARNING_EVOLUTION_ENABLED = "true"
```

如需恢复论文领域知识链，还需单独启用：

```powershell
$env:LEARNING_DOMAIN_KNOWLEDGE_ENABLED = "true"
```

## 真实验证

```text
Python 全量回归：197/197 passed
前端生产构建：passed
Playwright smoke：4/4 passed
Redis：真实启动并持久化 Episode、Experience、配置 L2、成功工作流 L3
Qwen Embedding：真实调用 text-embedding-v4，1024 维，passed
真实 CST：waiting_approval -> approved -> completed，非 mock
CST S11：1001 点，最低 -9.7695 dB @ 1.027 GHz
中枢外部 LLM：真实请求到达服务端，但返回 403 GROUP_DELETED
```

真实 CST 任务为 `80811b847dcaf9b7`，审批 `run_count=1`，所有执行节点完成，生成解析结果和报告。S11 未达到 `-10 dB`，因此 10 dB 带宽为 0；这表示执行链成功但模型性能未达标。

本次不能宣称外部中枢 LLM 测试通过。失败原因是当前 API Key 所属服务分组已被供应商删除，不是代码或网络连接失败。

# V2.3.2 - Query Expansion

新增 Query 拓展能力，增强 RAG 召回率。
