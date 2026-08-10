# V2.2.1 更新报告：相对 V2.2.0 的增量说明

日期：2026-07-07

## 一句话结论

V2.2.1 不是新功能大版本，而是 V2.2.0 的证据召回修正版。

V2.2.0 已经有 PaperWise 证据池、精读报告、创新区、前端展示和
本地匹配排序。V2.2.1 主要补上一个关键缺口：

```text
确认并强制区分“真正的 PaperWise Chroma 向量召回”
和“SQLite 本地全文回退检索”。
```

## V2.2.0 已有内容

V2.2.0 的主题是：

```text
PaperWise 只读证据池接入
```

它已经实现了以下能力。

### 1. PaperWise 只读证据池

V2.2.0 已经能在任务创建时读取 PaperWise 输出，并生成：

```text
task_metadata.evidence_pool_summary
```

证据池来源包括：

```text
reports
deep_read_papers
graph_library
```

其中：

```text
reports:
  PaperWise 精读报告候选。

deep_read_papers:
  面向论文复现和报告解释的源头论文候选。

graph_library:
  面向创新判断的图谱关系候选。
```

### 2. 前端 PaperWise 页面

V2.2.0 已经把前端从一个大页面拆出了 PaperWise 证据页。

前端主要保留两个区域：

```text
精读报告
创新
```

其中精读报告区域占主要空间，创新区域作为辅助判断。

### 3. 本地匹配排序

V2.2.0 已经支持按任务目标对论文候选进行本地结构化匹配。

匹配信号包括：

```text
天线类型或结构
优化指标
算法
参数数量
```

参数数量允许误差：

```text
abs(requested_parameter_count - candidate_parameter_count) <= 2
```

匹配分档：

```text
exact:
  请求信号全部命中。

match_80:
  少一个请求信号。

match_60:
  少两个请求信号。

reject:
  低于最低相关要求。
```

结构在 V2.2.0 后期已经不再作为硬 veto。

### 4. LLM 语义审查和 gate 思路

V2.2.0 已经引入：

```text
evidence_retrieval_task_agent
evidence_relevance_review_agent
gate / reviewer
```

设计目标是：

```text
本地匹配负责硬约束和可复现排序。
外接 LLM 负责语义补充、候选扩展、理由解释。
最终由 gate / reviewer 判断能不能采用。
```

### 5. V2.2.0 的主要问题

V2.2.0 的问题不在于“没有 PaperWise 证据池”，而在于：

```text
前端和报告里说的是向量库召回，
但实际运行时经常回落到 SQLite 全文检索。
```

这会造成误导：

```text
用户以为系统正在使用 PaperWise Chroma 向量库，
但实际可能只是查了 embedding_fulltext_search_content。
```

所以 V2.2.1 是对 V2.2.0 的召回真实性修正。

## V2.2.1 相比 V2.2.0 新增了什么

### 1. 新增真实向量召回优先规则

V2.2.1 明确规定：

```text
PaperWise 证据池必须先尝试官方 Chroma 向量召回。
```

调用入口是 PaperWise 官方接口：

```text
research_helper.kb.store.query(query, top_k=..., mode="paper")
```

这比 V2.2.0 更明确。

V2.2.0 的行为更像：

```text
能查到候选即可，不一定区分真实向量和本地全文。
```

V2.2.1 的行为变成：

```text
先试 PaperWise 官方向量库。
失败后才允许本地回退。
回退必须记录原因。
```

### 2. 新增召回后端标记

V2.2.1 新增后端字段：

```text
retrieval_backend
```

可能值：

```text
paperwise_kb_store_query
sqlite_fulltext_fallback
```

这解决了 V2.2.0 的一个盲点：

```text
用户看不到当前候选到底来自 Chroma 向量召回，
还是来自 SQLite 全文回退。
```

### 3. 新增向量回退原因

V2.2.1 新增字段：

```text
vector_query_error
```

当官方向量查询失败时，会记录真实原因，例如：

```text
qwen_embedding_api_key_not_configured
paperwise_chroma_papers_collection_missing
model_not_found
invalid_api_key
```

这比 V2.2.0 更可诊断。

V2.2.0 里即使召回少，也很难判断是：

```text
API 没调通
向量库没建好
PaperWise 没同步
还是匹配规则太严
```

V2.2.1 能把这些情况区分出来。

### 4. 新增 Qwen embedding 与聊天 LLM 的职责分离

V2.2.0 后期接入了前端外接 LLM 配置页。

但一开始有一个问题：

```text
前端配置的聊天 LLM API 被临时拿去做 PaperWise embedding。
```

这会导致两个错误：

```text
聊天模型 API 不一定支持 embedding。
聊天模型 API key 不一定是 DashScope / Qwen key。
```

V2.2.1 修正为：

```text
聊天 LLM:
  继续使用前端设置页的 Base URL / API Key / Chat Model。

PaperWise embedding:
  使用 PaperWise 自己的 QWEN_API_KEY。
  provider 固定为 qwen。
  model 默认 text-embedding-v3。
```

推荐配置位置：

```text
C:\Users\30626\.codex\skills\paperwise-main\.env
```

推荐配置内容：

```env
QWEN_API_KEY=你的DashScope Key
EMBEDDING_PROVIDER=qwen
EMBEDDING_MODEL=text-embedding-v3
```

### 5. 新增 Chroma collection 预检查

V2.2.1 在调用 PaperWise 官方 `store.query()` 前，会检查：

```text
.kb/chroma.sqlite3 中是否存在 papers collection
```

如果不存在，不会硬调 Chroma，而是记录：

```text
paperwise_chroma_papers_collection_missing
```

这样可以避免把测试夹具、残缺库、空库误判成可用向量库。

### 6. 前端新增向量召回可见状态

V2.2.1 在 PaperWise 页面新增显示：

```text
Vector backend
chunks
papers
Vector fallback reason
```

V2.2.0 前端只展示“精读报告 / 创新”，但看不出召回底层。

V2.2.1 前端可以直接看到：

```text
当前是否真的走 paperwise_kb_store_query。
如果回退，为什么回退。
当前向量库有多少 chunk 和 paper。
```

### 7. 前端设置页名称更清楚

V2.2.1 把设置页中的模型输入拆成：

```text
Chat Model
Embedding Model
```

这不是为了让前端聊天 API 控制 PaperWise embedding，而是为了避免再把
聊天模型和 embedding 模型混成一个概念。

当前实际边界是：

```text
Chat Model:
  用于外接 LLM 语义审查。

Embedding Model:
  默认记录 text-embedding-v3。
  PaperWise embedding 实际走 Qwen 通道。
```

## V2.2.1 没有新增什么

V2.2.1 没有新增大功能。

它没有做：

```text
没有重建 PaperWise 向量库
没有自动同步论文进 PaperWise
没有修改 PaperWise skill
没有修改 Antenna Skills
没有修改 E:\antenna skills
没有新增论文复现主流程
没有新增结构提取链
没有新增 CST 优化闭环
没有新增创新性完整评估
```

所以它不是 V2.3，也不是论文复现版本。

它只是把 V2.2.0 的 PaperWise 证据底座修正得更真实、更可诊断。

## 代码层面的增量

### 后端增量

主要文件：

```text
adapters/paperwise_adapter.py
```

V2.2.1 新增或修正：

```text
_paperwise_kb_query_items()
  优先调用 PaperWise 官方 store.query()。
  embedding provider 改回 qwen。
  embedding key 只读取 PaperWise .env 的 QWEN_API_KEY。
  不再复用前端聊天 LLM API key。

_paperwise_chroma_collection_exists()
  调用前检查 Chroma papers collection 是否存在。

_vector_library_summary()
  写入 retrieval_backend。
  写入 vector_query_error。
  失败后才回退 SQLite。
```

相关配置文件：

```text
agent_runtime/runtime_llm_config.py
agent_runtime/config.py
web/api.py
config.yaml
```

这些文件保留了前端 LLM 配置和 embedding model 字段。

### 前端增量

主要文件：

```text
web/frontend/src/App.jsx
```

V2.2.1 新增：

```text
Chat Model 输入
Embedding Model 输入
Vector backend 显示
Vector fallback reason 显示
chunks / papers 统计显示
```

### 测试增量

主要文件：

```text
tests/test_web_api.py
web/frontend/tests/web-console-smoke.spec.js
```

V2.2.1 新增或修正：

```text
runtime LLM 配置不回显 API key
embedding model 配置字段保存
前端 smoke 测试适配 Chat Model / Embedding Model
```

## 验证结果

已验证命令：

```text
python -m py_compile adapters\paperwise_adapter.py
python -m py_compile adapters\paperwise_adapter.py agent_runtime\runtime_llm_config.py agent_runtime\config.py web\api.py
python -m unittest tests.test_runtime_core tests.test_web_api
npm run build
npm run test:smoke
```

结果：

```text
后端单测：73 passed
前端构建：passed
前端 smoke：4 passed
```

真实 PaperWise 召回诊断结果：

```text
vector_backend = paperwise_kb_store_query
vector_error   = None
vector_status  = available
vector_items   = 1
deep_read_items = 10
```

这说明：

```text
Qwen embedding 已经调通。
PaperWise 官方 Chroma 向量召回已经被实际调用。
```

## 当前仍然存在的现象

虽然向量召回链路已经打通，但真实向量召回只返回：

```text
vector_items = 1
```

而精读报告最终展示：

```text
deep_read_items = 10
```

这是因为：

```text
1 条来自 PaperWise Chroma 向量召回。
其余由本地精读报告硬约束和可复现排序补齐。
```

如果后续还觉得召回少，优先排查：

```text
PaperWise 向量库里可检索 chunk 是否太少。
是否所有精读报告都同步进了向量库。
查询词是否和已索引 chunk 语义差距过大。
PaperWise 索引是否需要重建或补充。
```

## 版本边界

V2.2.0 解决的是：

```text
PaperWise 证据池能接入、能显示、能参与任务。
```

V2.2.1 解决的是：

```text
PaperWise 证据池的“向量召回”必须真实、可见、可诊断。
```

下一步如果继续开发，不应把 V2.2.1 当成论文复现版本。

后续更合理的版本方向是：

```text
V2.3:
  论文复现任务主线。

V2.4:
  Antenna Skills 结构提取链接入。

V2.5:
  CST 建模与导出增强。
```
