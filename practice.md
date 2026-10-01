# Antenna Agent Lab 配套理解题

当前 `tutorial.md` 中 16 课均为“大纲”状态，因此以下仅为阅读引导题，不附标准答案，也不把尚未展开的细节当作已学知识。回答时必须引用源码路径或测试证据。

## 课程 01：启动与模块地图

1. 后端模块加载时创建了哪些共享对象？分别由谁持有和使用？
2. 从浏览器点击“刷新”到 `/health` 返回，经过哪些公开边界？
3. 找出一处 `docs/current_architecture.md` 可能落后于当前实现的描述，并说明你用什么源码证据判断。

## 课程 02：普通任务从创建到终态

1. `POST /tasks` 的输入在哪里第一次变成 Blackboard 任务？
2. LangGraph 返回后，哪个函数拥有最终状态提交权？
3. 普通非天线任务为什么仍需要计划校验？用一个测试说明。

## 课程 03：父图、子图与状态裁剪

1. 三个子图各自允许接收哪些主要字段？为什么 repair 输入比 planning 多？
2. `input_schema` 与 `DynamicGraphState` 分别起什么作用？
3. 父图在哪些条件下从 repair 返回 planning，而不是 execution？

## 课程 04：规划、执行、修复与重试的边界

1. 哪些异常会进入 RetryPolicy，哪些不会？
2. “节点重试”和“修复后 replay”在状态和副作用上有什么区别？
3. 一个计划 schema 错误发生时，哪些执行动作尚未发生？

## 课程 05：Task、Module Review、Global Review 和中枢裁决

1. Task agent、module reviewer、global reviewer 分别读取什么、返回什么？
2. Module review 通过但 global review reroute 时，中枢应做什么？
3. 为什么 Reviewer 的 decision 不能直接把任务标记 completed？

## 课程 06：Agent IO 合同与最小上下文

1. `FieldRule.path`、`alias`、`required`、`default` 分别何时生效？
2. 举例说明一个存在于全局 State、但不应进入 task agent 的字段。
3. 缺少 required 字段后，失败发生在 Agent 调用前还是调用后？

## 课程 07：渐进披露和步骤级动态路由

1. 为什么路由阶段最多读取 Level 2，而不是直接读 SKILL.md？
2. `callable` 与 `candidate` 的阈值和行为区别是什么？
3. 同一个输入同时出现“React”和“优化”时，负向分数如何避免天线 Skill 误触发？

## 课程 08：Gate Token、MCP 与外部 Skill

1. Gate token 绑定哪些身份？哪种变化会让它失效？
2. Registry、Router、Gate 和 MCP 各回答一个什么问题？
3. MCP 返回结构正确但 packet schema 不合格时，artifact 是否可以登记？

## 课程 09：混合检索和源论文追溯

1. BM25 Top50 和 Embedding Top15 为什么是候选并集，而不是分数相加？
2. 最终阈值作用于哪个分数？
3. 向量 chunk 命中后，系统如何追溯到源论文？在哪些情况下只能返回 `paperwise_kb`？

## 课程 10：从召回候选到可用证据

1. `status=available` 为什么不能等价为“证据支持结论”？
2. report、deep-read paper 和 graph relation 的证据角色有什么区别？
3. 给出一个有引用但引用不支持 claim 的案例，并指出 Reviewer 应如何处理。

## 课程 11：审批前链路和幂等保护

1. 一个真实 CST 审批绑定了哪些值？为什么需要 request hash？
2. 用户重复点击批准时，run count 如何防止重复执行？
3. 计划版本变化后，旧审批为什么应该失效？

## 课程 12：运行、解析、审查与修复

1. 文件存在但曲线为常数时，应归类为哪一层失败？
2. S11-only 结果可以支持哪些结论，不能支持哪些结论？
3. Repair manifest 如何证明“修复真的改变了可执行状态”？

## 课程 13：L1/L2/L3、Redis 与检索

1. L1、旧 L2、旧 L3 的生命周期和用途分别是什么？
2. Redis 不可用时 fallback 保存在哪里？重启后会发生什么？
3. JSON 存储、Stream 和 Vector 为什么不能互相替代？

## 课程 14：V2.3 学习记忆和 Wiki

1. 原文、evidence unit、domain knowledge、episode、experience 有什么区别？
2. 为什么 candidate 即使相似度很高也不能进入动态计划？
3. CST 结果为什么可以进入经验 Wiki，但不能自动写成论文领域知识？
4. 当前 EvidenceCompiler 能证明什么，不能被宣传成什么？

## 课程 15：前端如何观察后端状态

1. 前端多久刷新一次当前任务？哪些数据在同一次 refresh 中分别请求？
2. `metadata.current_stage`、dynamic plan 当前步骤和 task state 可能怎样不同？
3. 前端为什么只能展示候选/晋级状态，不能直接把候选知识晋级？

## 课程 16：毕业改造——来源详情页

1. 来源详情 API 的只读契约至少要返回哪些字段？
2. 如何测试页面不会把 `user_confirmed` 显示成 `paper_fact`？
3. 改造完成后需要跑哪些最小回归，才能证明没有影响 CST 审批和动态执行链？
