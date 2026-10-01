# Antenna Agent Lab

基于 LangGraph 的天线研究多 Agent 调度项目，覆盖论文证据检索、动态计划、步骤级 Skill 路由、审查与反思、失败修复、真实 CST 单次运行、结果解析和报告展示。

## 运行环境

- Python 3.11+
- Node.js 18+
- Redis（持久化记忆与 LangGraph checkpoint）
- CST Studio Suite（仅真实 CST 模式需要）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest tests.test_runtime_core tests.test_web_api tests.test_langgraph_runtime
```

启动后端：

```powershell
python -m uvicorn web.api:app --host 127.0.0.1 --port 8000
```

启动前端：

```powershell
cd web\frontend
npm install
npm run dev
```

## 凭据

项目不保存 API Key。根据 `config.yaml` 使用环境变量配置：

```powershell
$env:OPENAI_API_KEY = "..."
$env:API_KEY = "..."  # Qwen embedding
```

真实 CST、外部 LLM 和数据外发仍受审批与 Gate 约束。本地路径可在 `config.yaml` 中按环境调整。

## 学习与自进化开关

V2.3.1 将学习记忆实现独立到 `agent_learning/`。当前默认配置保留任务经验沉淀，关闭领域知识链和自进化执行链：

```yaml
learning:
  enabled: true
  domain_knowledge_enabled: false
  evolution_enabled: false
```

- `enabled`：控制 Episode、Experience 和学习上下文。
- `domain_knowledge_enabled`：控制原文池、Evidence、Domain Knowledge、聚类、创新 Gate 和 Wiki。
- `evolution_enabled`：控制评测、提案、低风险 alias 修改、独立复测和回滚。

自进化系统不会仅因代码存在而运行，必须在配置中显式设置：

```yaml
learning:
  evolution_enabled: true
```

也可临时使用环境变量：

```powershell
$env:LEARNING_EVOLUTION_ENABLED = "true"
```

高风险 Prompt、阈值、知识晋级和代码修改不会自动执行。外部 LLM 仅生成候选建议，并复用中枢 Agent 的模型配置。
