# 架构地图

## 当前基线

```text
commit: 0a33b73ce67da395165be9043ea97043ec9ff341
working tree: 包含未提交 V2.3 学习层
```

## 主链

```text
React
-> FastAPI web.api
-> Scheduler
-> DynamicLangGraphRuntime
-> task/module/global agents
-> Gate + MCP + Adapters
-> Blackboard / Redis / Artifacts
-> API / React
```

## 模块职责

| 模块 | 我的解释 | 关键证据 | 待确认 |
|---|---|---|---|
| FastAPI | 待填写 | `web/api.py` | |
| Scheduler | 待填写 | `agent_runtime/scheduler.py` | |
| LangGraph | 待填写 | `agent_runtime/langgraph_runtime.py` | |
| Blackboard | 待填写 | `agent_runtime/blackboard.py` | |
| Redis | 待填写 | `agent_runtime/redis_store.py` | |
| Skill/MCP | 待填写 | `agent_runtime/skill_router.py`、`agent_runtime/mcp_client.py` | |
| PaperWise | 待填写 | `adapters/paperwise_adapter.py` | |
| CST | 待填写 | `adapters/cst_real_run_adapter.py` | |
| Learning | 待填写 | `agent_runtime/learning.py` | |
