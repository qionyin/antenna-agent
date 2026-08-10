# 当前唯一执行架构

## 启动入口

```text
python -m uvicorn web.api:app --host 127.0.0.1 --port 8000
cd web/frontend
npm run dev -- --host 127.0.0.1
```

后端唯一入口是 `web.api:app`。

## 唯一执行链

```text
用户目标
-> Scheduler 调用外接 LLM 生成 dynamic_plan 候选
-> Scheduler 硬校验 schema、动作、技能、依赖和审批顺序
-> LangGraph StateGraph 选择当前步骤并推进条件边
-> 当前步骤重新计算 skill route
-> task subagent 执行本地 handler 或 gate 授权的 skill adapter
-> module review subagent 审查当前产物
-> global review agent 审查证据链、依赖和下一步合理性
-> Scheduler 收集审查结果并决定继续、修改、重规划、等待审批、失败或完成
```

普通任务、真实 CST、失败报告、PaperWise 重新审查均经过
`Scheduler._execute_dynamic_plan_steps()`，该入口只调用
`DynamicLangGraphRuntime.invoke()`，不再包含手写步骤循环。

LangGraph 节点：

```text
START
-> select_step
-> route_skill
-> task_agent
-> module_review
-> global_review（按步骤条件启用）
-> central_decision
-> commit_step
-> 下一步骤或 finish
-> END
```

Blackboard + Redis checkpoint 继续负责持久化、恢复和前端数据；LangGraph 只接管
流程编排，不新增第二套状态存储。

真实 CST solver 只有一个调用点：

```text
Scheduler._execute_dynamic_step_handler()
-> action == cst_run
-> CstRealRunAdapter.run_single()
```

## 真实 CST 状态

```text
created
-> preflight_running
-> waiting_approval
-> running
-> completed | failed
```

真实 solver 已通过统一链完成实测。2026-07-11 的任务
`6a39cbcec062e701` 使用 `simulate_cst=false`，由 CST 官方 Python 创建工程、
运行 solver 并导出 1001 个 S11 点。真实 solver 仍只允许在绑定审批通过后启动。

## 已退休内容

- V1 固定七阶段 packet 执行链。
- PacketStore、packet 创建、校验和写入能力。
- 真实 CST 手工 run/parse/review/report 串联。
- 独立失败报告执行链。
- 独立 PaperWise 重审执行链。
- 重复 `_run_v2_agent` 包装器和虚构 executor/challenger 节点。
- L2/L3 记忆、embedding 和 Redis 向量检索保留，并向步骤级 skill 路由提供记忆上下文。

历史 `packet_stage`、`packets` 字段和 packet 查询 API 仅用于只读查看旧任务，
不能创建、恢复或继续执行旧 packet 任务。

## 外部边界

- PaperWise 只读。获批的语义召回通过官方 KB store 查询临时 Chroma 副本；
  未获批或调用失败时使用 SQLite `mode=ro` 全文回退。
- Antenna Skill adapter 返回的每个 packet 必须通过 envelope、packet type 和
  payload 必填字段校验，失败时不得登记为有效 artifact。
- `C:\Users\30626\.codex\skills\Antenna Skills` 不修改。
- `E:\antenna skills` 不修改，只通过 adapter 调用。
- 真实 CST、外部 LLM 和数据外发仍受审批或 gate 控制。
