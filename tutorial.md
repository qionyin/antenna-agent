# Antenna Agent Lab 八周源码课程

## 项目用途与学习目标

Antenna Agent Lab 是一个面向天线论文证据、动态计划、Skill 调用、真实 CST 单次运行、结果审查和学习记忆的多 Agent 工程。课程目标不是记住类名，而是在八周后能够：

1. 从 API 请求追踪到任务终态。
2. 解释 Scheduler、LangGraph、Blackboard、Redis、Agent 和 Adapter 的职责边界。
3. 修改一个跨后端、Agent、外部能力和前端的功能，并用测试证明没有绕过审批或 Gate。
4. 区分检索命中、可用证据、领域知识、任务经验和已验证创新。

## 源码版本

```text
Git 基线：0a33b73ce67da395165be9043ea97043ec9ff341
分支：master
课程生成日期：2026-09-20
当前工作区：包含尚未提交的 V2.3 学习记忆层
```

学习前执行并保存：

```powershell
git rev-parse HEAD
git status --short --branch
```

当前 `docs/current_architecture.md` 对唯一执行链仍有参考价值，但其中旧函数名或版本描述可能落后于实现。课程以 `web/api.py`、`agent_runtime/scheduler.py` 和 `agent_runtime/langgraph_runtime.py` 的当前代码为准。

## 前置基础

- 已熟悉 Python、类型标注、异常处理和单元测试。
- 已熟悉 RAG、Embedding、BM25 和 LLM API 基本概念。
- 需要补充：FastAPI 路由、LangGraph StateGraph、Redis 持久化、React hooks、审批与幂等设计。
- 不要求实际运行 CST solver；课程默认使用 mock、fixture 和只读检查。

## 覆盖范围

本课程覆盖当前唯一执行链、三子图、Agent IO、Skill 路由与 Gate、MCP Adapter、PaperWise 检索、CST 审批与结果处理、失败修复、V2.3 记忆学习层、React 前端和测试体系。

暂不覆盖：CST 官方 Python API 的全部建模命令、PaperWise 内部实现、外部 Antenna Skills 源码逐行分析、真实大规模优化训练、生产部署与云基础设施。

## 每周节奏

```text
源码阅读：2 小时
调用链跟踪：2 小时
实验/调试：2–4 小时
复盘与理解题：1–2 小时
```

| 周次 | 课程 | 主题 | 周交付 |
|---|---|---|---|
| Week 01 | 01–02 | 项目全貌与普通任务主链 | 启动记录、任务调用链、`week-01.md` |
| Week 02 | 03–04 | LangGraph 状态、子图、重试与修复 | 状态字段地图、失败分流记录 |
| Week 03 | 05–06 | 多 Agent 职责与 IO 裁剪 | Agent 对照表、IO 前后样本 |
| Week 04 | 07–08 | Skill 路由、Gate Token 与 MCP | 路由评分案例、Gate 拒绝案例 |
| Week 05 | 09–10 | PaperWise 混合检索与证据链 | 检索追溯记录、evidence-to-claim 表 |
| Week 06 | 11–12 | CST 审批、结果解析与失败修复 | 模拟审批记录、修复 manifest 分析 |
| Week 07 | 13–14 | Redis 记忆与 V2.3 学习闭环 | 记忆结构图、候选/晋级实验 |
| Week 08 | 15–16 | 前端观察与毕业改造 | 来源详情功能、回归结果、课程复盘 |

---

## 第 1 周：项目全貌与一次请求

### 课程 01：启动与模块地图

**状态：大纲**

**读完能回答：** 项目有哪些运行组件，一次请求为什么要经过 API、Scheduler、LangGraph、Agent、Adapter、Blackboard 和 Redis？

**主链：**

```text
React -> FastAPI -> Scheduler -> LangGraph -> Agent/Adapter
      -> Blackboard/Redis -> API 读取 -> React 刷新
```

**必读路径：**

1. `README.md`：输入是启动方式和环境要求；下一步进入架构说明；输出是运行组件清单。
2. `docs/current_architecture.md`：输入是项目组件；下一步定位唯一执行链；输出是架构假设与待核对旧描述。
3. `web/api.py`：阅读模块级依赖装配和 `health()`、`capabilities()`；输出是公开入口与共享对象。
4. `agent_runtime/scheduler.py::Scheduler.__init__()`：输入是配置和基础设施对象；下一步装配 Router、Learning、Adapter 和 LangGraph；输出是中枢运行时。
5. `agent_runtime/langgraph_runtime.py::DynamicLangGraphRuntime.__init__()`：输入是 Scheduler；下一步构建三个子图和父图；输出是可调用图。

**实验：** 启动 Redis、后端和前端；调用 `/health`、`/capabilities`、`/tasks`；把真实返回记录到 `study/week-01.md`。

**选读证据：** `agent_runtime/config.py`、`config.yaml`、`tests/test_web_api.py::test_health_and_capabilities`。

**暂缓阅读：** 各 LangGraph 节点内部逻辑由课程 03–04 负责；Redis 数据结构由课程 13 负责。

### 课程 02：普通任务从创建到终态

**状态：大纲**

**读完能回答：** `POST /tasks` 如何生成动态计划、执行步骤并最终落到 `completed`、`failed` 或 `waiting_approval`？

**主链：**

```text
POST /tasks
-> web.api.create_general_task()
-> Scheduler.create_task_from_request()
-> Scheduler._create_dynamic_task()
-> DynamicLangGraphRuntime.start()
-> Scheduler._apply_dynamic_execution_result()
```

**必读路径：**

1. `web/api.py::create_general_task()`：输入是 JSON payload；输出是任务状态 JSON。
2. `scheduler.py::create_task_from_request()`：输入是用户目标和可选 modeling request；输出交给 `_create_dynamic_task()`。
3. `scheduler.py::_create_dynamic_task()`：创建 Blackboard 记录、L1 对话和 capability snapshot；输出 LangGraph 初始输入。
4. `langgraph_runtime.py::start()`：调用父图；输出 final decision、stage 和 reason。
5. `scheduler.py::_apply_dynamic_execution_result()`：把图结果投影为公开任务状态，并仅在终态触发学习 episode。

**实验：** 创建一个非天线 mock 任务，保存 `task_id`、dynamic plan、nodes、central decisions 和 audit events。

**选读证据：** `tests/test_runtime_core.py::test_scheduler_gate_skips_antenna_adapter_for_non_antenna_task`。

**暂缓阅读：** Planner 生成细节由课程 04；Agent 执行细节由课程 05。

---

## 第 2 周：LangGraph 编排与状态

### 课程 03：父图、子图与状态裁剪

**状态：大纲**

**读完能回答：** 为什么项目有一个大状态，但进入 planning、execution、repair 子图时不会传入全部字段？

**主链：**

```text
DynamicGraphState
-> parent graph
-> input_schema 裁剪
-> planning / execution / repair subgraph
-> 子图结果合回父图状态
```

**必读路径：**

1. `langgraph_runtime.py::DynamicGraphState`：全局运行字段。
2. `io_contracts.py::PlanningGraphInput`、`ExecutionGraphInput`、`RepairGraphInput`：三类子图输入边界。
3. `langgraph_runtime.py::_build_parent_graph()`：子图注册、条件边和终态节点。
4. `langgraph_runtime.py::_parent_entry()` 及三个 `_after_parent_*()`：分支条件。
5. `tests/test_langgraph_runtime.py::test_parent_graph_uses_sliced_subgraph_inputs`：输入裁剪证据。

**实验：** 输出父图和三个子图的节点名；对照 TypedDict 制作 `study/state-field-map.md`。

**选读证据：** `langgraph_runtime.py::_record_runtime_state()`、`_phase_config()`。

**暂缓阅读：** Agent 级 IO 裁剪由课程 06；Checkpoint 后端由课程 12–13。

### 课程 04：规划、执行、修复与重试的边界

**状态：大纲**

**读完能回答：** 临时调用失败、产物不合格、计划错误和中断恢复为什么不能用同一种“重试”？

**主链：**

```text
planning: route -> review -> generate -> normalize -> validate -> persist
execution: select -> route -> task -> module review -> global review -> commit
repair: triage -> repair plan -> repair task -> review -> replay/terminal
```

**必读路径：**

1. `_build_planning_subgraph()`：规划节点和 RetryPolicy。
2. `_build_execution_subgraph()`：逐步执行和 central decision。
3. `_build_repair_subgraph()`：失败分类、修复、审查和 replay。
4. `retry_transient_runtime_error()`：哪些错误可自动重试。
5. `failure_store.py` 与 `repair_policy.py`：业务失败记录和修复决策。

**实验：** 用测试夹具制造 ConnectionError、计划 schema 错误和产物缺失；比较自动重试、修复和重新规划。

**选读证据：** `tests/test_failure_recovery.py`、`tests/test_parent_subgraphs_and_repair.py`。

**暂缓阅读：** 真实 CST 失败类型由课程 12 负责。

---

## 第 3 周：多 Agent 和 IO 层

### 课程 05：Task、Module Review、Global Review 和中枢裁决

**状态：大纲**

**读完能回答：** 四个角色分别拥有哪部分判断权，为什么 Reviewer 不能直接结束任务？

**主链：**

```text
task agent output
-> module review 当前产物
-> optional global review 跨步骤证据链
-> Scheduler central decision
-> commit step
```

**必读路径：**

1. `langgraph_runtime.py::_task_agent()`：执行 handler 或受控 Skill。
2. `subagents.py::run_task()`：任务输出 envelope 与可选外部 LLM。
3. `_module_review()` 与 `run_module_review()`：当前步骤检查。
4. `_global_review()` 与 `run_global_review()`：依赖、证据与 reroute。
5. `_central_decision()`、`_commit_step()`：最终步骤裁决和状态写回。

**实验：** 给同一份“只有 S11、没有 gain”的输出分别写 module review 和 global review 预期。

**选读证据：** `tests/test_runtime_core.py` 中 module/global review 场景。

**暂缓阅读：** Skill 是否允许调用由课程 07–08 负责。

### 课程 06：Agent IO 合同与最小上下文

**状态：大纲**

**读完能回答：** Agent 如何从子图状态中只取自己需要的字段？

**主链：**

```text
subgraph state
-> IO_CONTRACTS
-> path lookup / alias / default / required check
-> minimal agent context
```

**必读路径：**

1. `io_contracts.py::FieldRule`：字段路径、别名、必需性和默认值。
2. `IO_CONTRACTS`：task、module review、global review、repair 的输入表。
3. `IOContractResolver.build_input()`：构造最小 payload。
4. `IOContractResolver._lookup()`：嵌套字段读取。
5. `langgraph_runtime.py` 中所有 `self.io.build_input()` 调用点。

**实验：** 打印 task context 前后差异；验证 `current_failure` 和 `repair_plan` 不会泄露给普通 task agent；缺失 `task_id` 时观察异常。

**选读证据：** `tests/test_langgraph_runtime.py::test_io_contract_builds_minimal_task_agent_payload`。

**暂缓阅读：** 前端展示字段不属于 Agent IO，由课程 15 负责。

---

## 第 4 周：Skill 路由、Gate 与 MCP

### 课程 07：渐进披露和步骤级动态路由

**状态：大纲**

**读完能回答：** 系统如何从注册信息选出 callable、candidate 和 excluded Skill？

**主链：**

```text
用户目标 + 当前步骤 + 证据/审查反馈
-> registry cards
-> disclosure Level 0/1/2
-> score components
-> route plan
-> route review
```

**必读路径：**

1. `skill_system/categories/**/*.json`：唯一手工 Skill 描述源。
2. `skill_registry.py`：加载与索引注册表。
3. `skill_disclosure.py`：路由阶段披露边界。
4. `skill_matcher.py`：领域、任务、artifact、证据、反馈和负向分数。
5. `skill_router.py::build_plan()`、`build_step_context()`、`review_plan()`。

**实验：** 测试天线、React、金融 return loss、CNN 准确率；手算一条 confidence；比较同一 Skill 在两个步骤的分数。

**选读证据：** `tests/evaluation/v2_3_baseline_100/` 中已保留案例与 36 条基线结果。

**暂缓阅读：** Adapter 的真正调用由课程 08 负责。

### 课程 08：Gate Token、MCP 与外部 Skill

**状态：大纲**

**读完能回答：** 路由选中一个 Skill 后，系统如何保证只有当前计划、当前步骤和当前动作能调用它？

**主链：**

```text
route plan
-> SkillExecutorGate.check()
-> version-bound token
-> MCP wrapper/client/server
-> Antenna Skill adapter
-> packet validation / artifact
```

**必读路径：**

1. `skill_executor_gate.py::check()`：token 生成与拒绝理由。
2. `mcp_client.py`：MCP tool discovery 和调用。
3. `adapters/mcp_server.py`：Tool 到本地 Adapter 的绑定。
4. `adapters/mcp_wrappers.py`：项目侧 MCP 封装。
5. `adapters/antenna_skills_adapter.py` 与 `skill_packet_validator.py`：外部 Skill 结果校验。

**实验：** 无 token、错误 owner、错误 step、计划版本变化后使用旧 token；检查审计事件。

**选读证据：** `tests/test_mcp_adapter_bridge.py`。

**暂缓阅读：** CST adapter 的业务细节由课程 11–12 负责。

---

## 第 5 周：PaperWise、检索与证据

### 课程 09：混合检索和源论文追溯

**状态：大纲**

**读完能回答：** 当前查询如何从 BM25/Embedding 候选变成源论文结果，为什么命中片段不等于命中正确论文？

**主链：**

```text
query profile
-> BM25 Top50
-> optional embedding Top15
-> semantic threshold/rerank
-> dedupe papers
-> trace chunk to report.md
```

**必读路径：**

1. `paperwise_adapter.py::_query_profile()`：结构、指标、算法和参数数量。
2. `_project_hybrid_vector_query_items()`：粗召回和精排。
3. `_paperwise_vector_records()`：从 SQLite 读取 chunk 与 metadata。
4. `_paperwise_vector_item_from_record()`：返回源论文引用。
5. `retrieval.py::_retrieve_layer()`：记忆 L2/L3 的并行检索实现。

**实验：** 对同一查询分别关闭/开启 external embedding；复现 `tests/evaluation/v2_3_baseline_100.py --resume` 前先复制结果目录，避免覆盖基线。

**选读证据：** `tests/evaluation/production_bm25_qwen_threshold051_1000.py`、当前 36 条基线结果。

**暂缓阅读：** 证据是否足以支持 claim 由课程 10 负责。

### 课程 10：从召回候选到可用证据

**状态：大纲**

**读完能回答：** `available`、候选相关、证据可追溯和 claim 被支持之间有什么区别？

**主链：**

```text
report/vector/graph candidates
-> source normalization
-> local/LLM relevance review
-> evidence/modeling gate
-> accepted/rejected/uncertain
-> downstream plan evidence refs
```

**必读路径：**

1. `paperwise_adapter.py::evidence_pool_summary()`。
2. `_report_evidence()`、`_deep_read_papers_source()`、`graph_library_summary()`。
3. `scheduler.py` 中 PaperWise review 和 evidence gate 逻辑。
4. `subagents.py::_review_step_output()`：证据缺失如何影响审查。
5. `antenna-result-to-claim` 的外部边界只读说明；不在本课展开其源码。

**实验：** 找一条“正确论文但错误结论”的案例，填写 evidence-to-claim 对照表；验证现有图谱保持只读。

**选读证据：** `tests/test_runtime_core.py` 中 PaperWise relevance 测试。

**暂缓阅读：** 创新四 Gate 在课程 14 讨论。

---

## 第 6 周：真实 CST 与恢复

### 课程 11：审批前链路和幂等保护

**状态：大纲**

**读完能回答：** 为什么真实 solver 必须绑定 request、plan、step 和审批记录？

**主链：**

```text
POST /tasks/real-cst-single-run
-> create task
-> preflight
-> interrupt waiting approval
-> approve/reject
-> checkpoint resume
```

**必读路径：**

1. `web/api.py::create_real_cst_single_run()`、`approve_task()`、`reject_task()`。
2. `scheduler.py::create_real_cst_single_run_task()`。
3. `scheduler.py::approve_real_cst_task()`：request hash、plan、step 和 run count。
4. `langgraph_runtime.py::_commit_step()`：`interrupt()` 与 resume。
5. `approval.py`、`checkpoint.py`：审批和 checkpoint 边界。

**实验：** 使用 fixture/mock 走审批、拒绝、旧审批和重复批准；禁止启动真实 solver。

**选读证据：** `tests/test_web_api.py` 的审批场景。

**暂缓阅读：** CST 输出和结果合理性由课程 12 负责。

### 课程 12：运行、解析、审查与修复

**状态：大纲**

**读完能回答：** CST 工程创建成功、solver 成功、结果文件存在和结果足以支撑结论为什么是四件事？

**主链：**

```text
approved cst_run
-> MCP CST adapter
-> run directory/artifacts
-> S11 parse
-> sanity review
-> report or repair subgraph
```

**必读路径：**

1. `adapters/cst_real_run_adapter.py::run_single()`。
2. `_parse_result_directory()`、`_compute_s11_metrics()`、`_bands_below_threshold()`。
3. `scheduler.py::_execute_dynamic_step_handler()` 的 CST action。
4. `failure_store.py`：失败身份、修复轮次和重放状态。
5. `repair_policy.py` 与 repair subgraph：修复动作、manifest 和 replay。

**实验：** 使用隔离 fixture 测试空文件、NaN、全 0、常数曲线和目标频段缺失；检查 before/after hash。

**选读证据：** `tests/test_failure_recovery.py`、`tests/test_parent_subgraphs_and_repair.py`。

**暂缓阅读：** 天线物理结果解释属于外部专业 Skill，不在本课臆造结论。

---

## 第 7 周：记忆与学习系统

### 课程 13：L1/L2/L3、Redis 与检索

**状态：大纲**

**读完能回答：** 对话记忆、长期事实、工作流经验、Redis JSON、Stream 和 Vector 分别解决什么问题？

**主链：**

```text
MemoryManager
-> RedisStore JSON/vector
-> RetrievalEngine
-> task retrieval_context
```

**必读路径：**

1. `memory.py::add_l1_turn()`、`store_l2()`、`maybe_store_l3_workflow()`。
2. `redis_store.py::set_json()`、`xadd()`、`upsert_vector()`、`vector_search()`。
3. `retrieval.py::build_context()`、`_retrieve_layer()`。
4. `scheduler.py::_retrieval_context()`。
5. `tests/test_runtime_core.py` 中持久化、阈值和 inactive 过滤测试。

**实验：** 各写入一条 L1/L2/L3；验证 Redis 重启持久化和不可用时 fallback；记录当前 Redis 是否真实可用。

**选读证据：** `work/memory_flow_check.py`、`work/memory_recall_one_each.py`。

**暂缓阅读：** V2.3 的 evidence、episode 和 experience 由课程 14 负责。

### 课程 14：V2.3 学习记忆和 Wiki

**状态：大纲**

**读完能回答：** API 型大模型如何通过原文、证据、知识候选、Episode、经验反馈和显式晋级形成可控“学习”？

**主链：**

```text
PaperWise report -> raw source -> evidence units
-> domain knowledge candidate -> review -> promoted

terminal task -> episode -> experience candidate
-> feedback/replay -> promoted
-> validated_learning_context / Wiki
```

**必读路径：**

1. `learning.py::EvidenceCompiler`、`KnowledgeReviewer`、`KnowledgeClusterer`。
2. `learning.py::LearningService`：编译、审查、晋级、episode、回放和 Wiki。
3. `memory.py` 的 source/evidence/domain/episode/experience 存取接口。
4. `scheduler.py::_record_terminal_learning()` 与 `validated_learning_context`。
5. `web/api.py` 的 `/learning/*` 接口和 `tests/test_v23_learning.py`。

**实验：** 导入一份 PaperWise 报告；验证 candidate 不进入计划；制造冲突；比较三种聚类；确认 Wiki 可展示 promoted 经验但不会创建第二张图谱。

**选读证据：** `docs/v2_3_learning_memory_domain_understanding.md`、`docs/v2_3_implementation_audit.md`。

**暂缓阅读：** 当前规则抽取不能被称为完整 LLM 领域理解；外部 LLM 抽取属于后续设计。

---

## 第 8 周：前端、质量保障与独立改造

### 课程 15：前端如何观察后端状态

**状态：大纲**

**读完能回答：** 前端怎样把任务状态、动态计划、Agent、Skill、审批、证据、报告和学习记忆展示出来？

**主链：**

```text
React useEffect / polling
-> task APIs
-> metadata normalization
-> dashboard/workflow/evidence/learning pages
-> user action POST
```

**必读路径：**

1. `web/frontend/src/App.jsx::App()`：全局状态、轮询和视图切换。
2. `refreshAll()`、`refreshTask()`、`postJson()`：后端通信。
3. `CozeWorkflowPanel`、`DynamicStepCard`、`EvidencePage`、`LearningPage`。
4. `styles.css`：布局和响应式规则。
5. `web/frontend/tests/web-console-smoke.spec.js`：前端 smoke 行为。

**实验：** 检查等待审批按钮、动态步骤、中枢回复、学习页面；桌面和移动视口截图；确认没有文本重叠。

**选读证据：** `playwright.config.js`、`package.json`。

**暂缓阅读：** 视觉品牌细节不影响后端理解，不作为本课程考核重点。

### 课程 16：毕业改造——来源详情页

**状态：大纲**

**读完能回答：** 如何安全完成一次跨 API、记忆、证据和 React 的功能改造？

**目标链：**

```text
promoted knowledge / experience
-> evidence refs
-> source details API
-> frontend detail view
-> no direct promotion
```

**实施顺序：**

1. 画调用链和数据边界。
2. 定义只读来源详情 API。
3. 先写后端 API 测试。
4. 实现前端详情页和状态标签。
5. 验证 candidate 不进入计划、经验不冒充论文事实。
6. 跑 Python 回归、前端构建和 smoke。
7. 写变更说明与未完成项。

**验收：** 不增加第二执行链；不改变 CST 审批；前端不能直接晋级；所有详情可追溯；后端和前端测试通过。

**选读证据：** 课程 01–15 的源码与笔记。

**暂缓阅读：** 面试包装、线上部署和模型微调不属于毕业任务。

---

## 阶段验证命令

```powershell
python -m py_compile <本周涉及文件>
python -m unittest tests.test_runtime_core
python -m unittest tests.test_langgraph_runtime
python -m unittest tests.test_parent_subgraphs_and_repair
python -m unittest tests.test_v23_learning
python -m unittest tests.test_web_api
cd web\frontend
npm run build
```

真实 CST 不作为日常课程测试；只使用 mock、fixture 或明确审批后的受控执行。

## 完成标准

完成 16 课、8 份周记和毕业改造后，应能独立解释请求主链、三子图、Agent IO、Skill Gate、PaperWise 证据、CST 审批与修复、记忆学习层和前端联动，并能用测试证明一次修改没有越权或污染正式知识。
