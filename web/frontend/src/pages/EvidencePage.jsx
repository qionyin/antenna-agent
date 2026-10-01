import React from "react";
import { Gauge, RefreshCw } from "lucide-react";

export function EvidencePage({ evidencePool, evidenceSources, defaultEvidencePool, sourceLabels, onRerunReview, reviewBusy, hasTask, onBack }) {
  const llmReview = evidencePool.llm_review || {};
  const vectorSource = evidencePool.internal_sources?.vector_library || {};
  return (
    <section className="pageStack">
      <div className="pageHeader">
        <div>
          <h2>PaperWise 证据审查</h2>
          <p>中枢派 evidence_retrieval_task_agent 召回候选，再派 evidence_relevance_review_agent 用 ReAct 审查相关性。</p>
        </div>
        <div className="pageActions">
          <button disabled={!hasTask || reviewBusy} onClick={onRerunReview}>
            <RefreshCw size={17} /> {reviewBusy ? "Reviewing..." : "重新用 LLM 审查"}
          </button>
          <button onClick={onBack}><Gauge size={17} /> 返回总览</button>
        </div>
      </div>
      <section className="panel evidencePanel" id="paperwise-evidence">
        <div className="sectionHead">
          <h2>证据池摘要</h2>
          <span>{evidencePool.status || "not_loaded"}</span>
        </div>
        <div className="evidenceSummary">
          <strong>PaperWise 已只读接入：精读报告 + 创新</strong>
          <p>精读报告区展示向量库召回块追溯到的源头论文；创新区展示图谱/LLM 候选和 gate 结论。</p>
          <small>
            审查模式：{evidencePool.review_mode || "not_loaded"} |
            召回：{evidencePool.retrieval_agent || "not_loaded"} |
            审查：{evidencePool.review_agent || "not_loaded"}
          </small>
          <small>
            LLM attempted: {String(Boolean(llmReview.attempted))} |
            success: {String(Boolean(llmReview.success))} |
            model: {llmReview.model || "not_configured"} |
            expanded: {llmReview.expanded_candidate_count || 0}
          </small>
          {llmReview.error && <small className="missingReason">LLM error: {llmReview.error}</small>}
          <small>
            Vector backend: {vectorSource.retrieval_backend || "not_loaded"} |
            chunks: {vectorSource.chunk_count || 0} |
            papers: {vectorSource.paper_count || 0}
          </small>
          {vectorSource.vector_query_error && <small className="missingReason">Vector fallback reason: {vectorSource.vector_query_error}</small>}
          {evidencePool.gate_summary && (
            <small>
              Gate：采用 {evidencePool.gate_summary.adopted || 0} |
              阻塞 {evidencePool.gate_summary.blocked || 0} |
              待补证据 {evidencePool.gate_summary.needs_more_evidence || 0}
            </small>
          )}
          {evidencePool.accelerator_policy && (
            <small>
              规则：精读报告按本地硬约束和可复现排序；创新需 LLM 语义审查；所有来源最终由 gate/reviewer 采用裁决。
            </small>
          )}
        </div>
        <EvidenceReview review={evidencePool.react_review || defaultEvidencePool.react_review} />
        <div className="evidenceGrid evidenceGridTwo">
          <EvidenceSource
            name={sourceLabels.deep_read_papers}
            source={evidenceSources.deep_read_papers || evidenceSources.reports || defaultEvidencePool.sources.deep_read_papers || { status: "missing", items: [] }}
            variant="primary"
          />
          <div className="innovationColumn">
            <EvidenceSource
              name={sourceLabels.graph_library}
              source={evidenceSources.graph_library || defaultEvidencePool.sources.graph_library || { status: "missing", items: [] }}
            />
            {(evidenceSources.llm_semantic_expansion?.items || []).length > 0 && (
              <EvidenceSource
                name={sourceLabels.llm_semantic_expansion}
                source={evidenceSources.llm_semantic_expansion}
              />
            )}
          </div>
        </div>
      </section>
    </section>
  );
}

function EvidenceSource({ name, source, variant = "" }) {
  const roles = source.roles || [];
  const items = source.items || source.candidates || source.sample_relations || [];
  const count = source.count ?? source.chunk_count ?? source.relation_count ?? source.node_count ?? 0;
  const hiddenCount = Math.max(0, items.length - 10);
  return (
    <div className={`evidenceSource ${variant} ${source.status || "missing"}`}>
      <div className="evidenceSourceHead">
        <strong>{name}</strong>
        <span>{source.status || "missing"}</span>
      </div>
      <p>{source.description || source.reason || source.label || "insufficient_evidence"}</p>
      <div className="roleLine">{roles.map((role) => <span key={role}>{role}</span>)}</div>
      <small>{source.support_level || source.evidence_grade || "insufficient_evidence"} | {count} items</small>
      {source.review_requirement && <small>审查要求：{source.review_requirement}</small>}
      {(source.accepted_count !== undefined || source.rejected_count !== undefined || source.uncertain_count !== undefined) && (
        <small>接受 {source.accepted_count || 0} | 拒绝 {source.rejected_count || 0} | 不确定 {source.uncertain_count || 0}</small>
      )}
      {items.length < 10 && <small>当前只显示 {items.length} 条：后端只召回到这么多候选，或被本地硬约束过滤。</small>}
      {hiddenCount > 0 && <small>另有 {hiddenCount} 条未显示；本页最多展示前 10 条。</small>}
      {source.path && <small>{source.path}</small>}
      {source.reason && <small className="missingReason">{source.reason}</small>}
      <ul>
        {items.slice(0, 10).map((item, index) => (
          <li key={item.path || index}>
            <b>{item.display_title || item.title || item.target_title || item.source || item.relation || "evidence"}</b>
            {item.original_title && item.original_title !== item.display_title && <small>原题：{item.original_title}</small>}
            <span>{item.description || item.snippet || item.path || item.target_arxiv || ""}</span>
            {item.trace_sources?.length > 0 && <small>来源追溯：{item.trace_sources.map((trace) => trace.type).join(" + ")}</small>}
            {item.paper_path && <small>源头论文：{item.paper_path}</small>}
            {item.react_review && <small>{item.react_review.decision}: {item.react_review.reason}</small>}
            {item.evidence_gate && (
              <small className={item.evidence_gate.adoption_decision === "adopt" ? "gatePass" : "gateWarn"}>
                Gate {item.evidence_gate.adoption_decision}: {item.evidence_gate.reason}
              </small>
            )}
            {item.evidence_gate?.blockers?.length > 0 && <small className="missingReason">阻塞：{item.evidence_gate.blockers.join(", ")}</small>}
            {item.evidence_gate?.warnings?.length > 0 && <small>待确认：{item.evidence_gate.warnings.join(", ")}</small>}
            <em>{item.level || item.evidence_grade || item.status || item.relation || "unknown"}</em>
          </li>
        ))}
      </ul>
    </div>
  );
}

function EvidenceReview({ review }) {
  const accepted = review.accepted || [];
  const rejected = review.rejected || [];
  const uncertain = review.uncertain || [];
  const gaps = review.evidence_gaps || [];
  if (!accepted.length && !rejected.length && !uncertain.length && !gaps.length) return null;
  return (
    <div className="reactReviewGrid">
      <ReviewBucket title="已接受证据" items={accepted} />
      <ReviewBucket title="已拒绝证据" items={rejected} />
      <ReviewBucket title="不确定证据" items={uncertain} />
      <div className="reviewBucket">
        <strong>证据缺口</strong>
        {gaps.length ? gaps.map((gap) => <small key={gap}>{gap}</small>) : <small>暂无</small>}
      </div>
    </div>
  );
}

function ReviewBucket({ title, items }) {
  return (
    <div className="reviewBucket">
      <strong>{title}</strong>
      {items.slice(0, 4).map((item, index) => (
        <small key={item.path || index}>{item.display_title || item.title || item.source || "evidence"} | {item.reason}</small>
      ))}
      {!items.length && <small>暂无</small>}
    </div>
  );
}
