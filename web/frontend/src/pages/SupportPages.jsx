import React from "react";
import { Gauge, KeyRound, XCircle } from "lucide-react";
import { List } from "../components/Common";

export function ReportsPage({ reports, artifacts, task, metadata, onBack }) {
  return (
    <section className="pageStack">
      <div className="pageHeader">
        <div>
          <h2>任务报告</h2>
          <p>{metadata.task_title || task?.task_id || "未选择任务"}</p>
        </div>
        <button onClick={onBack}><Gauge size={17} /> 返回总览</button>
      </div>
      <section className="columns">
        <section className="panel">
          <div className="sectionHead">
            <h2>报告入口</h2>
            <span>{reports.length}</span>
          </div>
          <List items={reports} render={(item) => `${item.report_type}: ${item.markdown_path || item.html_path}`} />
        </section>
        <section className="panel">
          <div className="sectionHead">
            <h2>报告相关产物</h2>
            <span>{artifacts.length}</span>
          </div>
          <List items={artifacts.filter((item) => item.artifact_type === "report" || String(item.name || "").includes("report"))} render={(item) => `${item.name || item.artifact_type}: ${item.path}`} />
        </section>
      </section>
    </section>
  );
}

export function LogsPage({ logs, onBack }) {
  return (
    <section className="pageStack">
      <div className="pageHeader">
        <div>
          <h2>实时日志</h2>
          <p>按时间查看 subagent 执行、审查和中枢状态更新。</p>
        </div>
        <button onClick={onBack}><Gauge size={17} /> 返回总览</button>
      </div>
      <section className="panel">
        <div className="sectionHead">
          <h2>日志明细</h2>
          <span>{logs.length}</span>
        </div>
        <List items={logs.slice(-160)} render={(item) => `${item.sequence_id || ""} ${item.node_id || ""} ${item.status || ""} ${item.ui_safe_message || item.message || ""}`} />
      </section>
    </section>
  );
}

export function SettingsPage({
  llmSettings,
  llmBaseUrl,
  setLlmBaseUrl,
  llmModelName,
  setLlmModelName,
  llmEmbeddingModelName,
  setLlmEmbeddingModelName,
  llmApiKey,
  setLlmApiKey,
  saveRuntimeLlmSettings,
  disableRuntimeLlm,
  onBack
}) {
  return (
    <section className="pageStack">
      <div className="pageHeader">
        <div>
          <h2>运行设置</h2>
          <p>前端配置的外接 LLM 优先用于 evidence_relevance_review_agent；未配置或调用失败时回到 local_react_fallback。</p>
        </div>
        <button onClick={onBack}><Gauge size={17} /> 返回总览</button>
      </div>
      <section className="panel">
        <div className="sectionHead">
          <h2>外接 LLM</h2>
          <span>{llmSettings.enabled ? "enabled" : "disabled"}</span>
        </div>
        <div className="formGrid">
          <label>
            <span>Base URL</span>
            <input value={llmBaseUrl} onChange={(event) => setLlmBaseUrl(event.target.value)} placeholder="https://example.com/v1" />
          </label>
          <label>
            <span>Chat Model</span>
            <input value={llmModelName} onChange={(event) => setLlmModelName(event.target.value)} placeholder="gpt-4.1 / qwen-plus / ..." />
          </label>
          <label>
            <span>Embedding Model</span>
            <input value={llmEmbeddingModelName} onChange={(event) => setLlmEmbeddingModelName(event.target.value)} placeholder="text-embedding-v3" />
          </label>
          <label className="wide">
            <span>API Key</span>
            <input type="password" value={llmApiKey} onChange={(event) => setLlmApiKey(event.target.value)} placeholder={llmSettings.api_key_configured ? "已配置，留空表示继续使用当前 key" : "请输入 API Key"} />
          </label>
        </div>
        <div className="toolbar">
          <button className="primary" onClick={saveRuntimeLlmSettings}><KeyRound size={17} /> 保存并启用</button>
          <button onClick={disableRuntimeLlm}><XCircle size={17} /> 禁用</button>
          <span className="settingsHint">API Key 不回显，只保存在当前后端进程内存中。</span>
        </div>
      </section>
    </section>
  );
}
