# 模块化架构

## 唯一执行链

模块拆分只调整代码职责，不增加第二条执行链：

```text
FastAPI
-> Scheduler facade
-> DynamicLangGraphRuntime
-> planning / execution / repair graph nodes
-> task / module review / global review
-> Gate + MCP + Adapter
-> Blackboard / Redis / artifact
```

## 后端模块

```text
agent_runtime/
  scheduler.py                 任务生命周期和依赖装配门面
  langgraph_runtime.py         父图、子图构建、调用与 checkpoint
  orchestration/
    planning_service.py        动态计划生成、规范化和校验
    central_messages.py        步骤级中枢沟通与受控重规划
    execution_service.py       动态 action 与修复 action
    evidence_review.py         PaperWise 候选审查和 evidence gate
    learning_lifecycle.py      终态 Episode 沉淀
    state.py / retry.py        图状态和重试谓词
    graph_nodes/
      planning.py
      execution.py
      repair.py

  memory.py                    L1 与旧 L2/L3 兼容门面
  knowledge/
    repository.py              原文、证据、知识、Episode、经验、Wiki 存取
    extraction.py              原文证据编译
    review.py                  知识审查和冲突检测
    clustering.py              候选聚类
    innovation.py              创新四 Gate
    wiki.py                    Wiki 投影
    service.py                 学习用例编排
  learning.py                  旧导入路径兼容 re-export

integrations/paperwise/
  reports.py                   报告读取与结构化索引
  vector_store.py              SQLite/Embedding 混合检索
  graph_reader.py              既有论文图谱只读访问
  query_profile.py             查询标签、相关性和标题映射
adapters/paperwise_adapter.py   PaperWise 组合门面

web/
  api.py                       应用装配和核心任务 API
  routes/learning.py           学习与知识 API router
```

## 前端模块

```text
web/frontend/src/
  App.jsx                      顶层状态、任务动作、导航与 Workflow 画布
  components/Common.jsx        Metric / List
  pages/EvidencePage.jsx       PaperWise 证据页面
  pages/LearningPage.jsx       学习记忆页面
  pages/SupportPages.jsx       报告、日志和设置页面
```

## 依赖规则

- Scheduler 是唯一任务门面和最终状态提交者。
- LangGraph 节点通过 Scheduler 已装配的能力执行，不创建第二套 Scheduler。
- Knowledge Repository 只负责存取；抽取、审查、聚类和晋级由独立服务负责。
- PaperWise 模块保持只读，不更新 Blackboard。
- CST 调用仍只有 `DynamicExecutionServiceMixin -> cst_real.run_single()` 一个位置。
- 旧模块路径仅保留 re-export 或组合门面，不保留重复实现。
