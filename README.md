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
