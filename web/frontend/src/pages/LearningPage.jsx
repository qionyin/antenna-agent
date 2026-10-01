import React from "react";
import { Gauge, RefreshCw } from "lucide-react";
import { List, Metric } from "../components/Common";

const asArray = (value) => Array.isArray(value) ? value : [];

export function LearningPage({ snapshot, taskLearning, knowledgeItems, experienceItems, task, onRefresh, onBack }) {
  const knowledge = snapshot.domain_knowledge || {};
  const experiences = snapshot.experiences || {};
  const knowledgeStatus = knowledge.by_status || {};
  const experienceStatus = experiences.by_status || {};
  const context = taskLearning.validated_learning_context || {};
  const usedKnowledge = asArray(context.knowledge);
  const usedExperiences = asArray(context.experiences);
  return (
    <section className="pageStack learningPage">
      <div className="pageHeader">
        <div>
          <h2>学习记忆与领域知识</h2>
          <p>候选只保存和审查；只有 promoted 知识与经验能够进入动态计划。</p>
        </div>
        <div className="pageActions">
          <button onClick={onRefresh}><RefreshCw size={17} /> 刷新</button>
          <button onClick={onBack}><Gauge size={17} /> 返回总览</button>
        </div>
      </div>
      <section className="learningMetrics">
        <Metric label="原文池" value={snapshot.raw_sources || 0} />
        <Metric label="证据单元" value={snapshot.evidence_units || 0} />
        <Metric label="任务 Episode" value={snapshot.episodes || 0} />
        <Metric label="领域知识" value={knowledge.total || 0} />
        <Metric label="工作经验" value={experiences.total || 0} />
        <Metric label="聚类记录" value={snapshot.cluster_runs || 0} />
        <Metric label="创新评估" value={snapshot.innovations || 0} />
      </section>
      <section className="columns">
        <section className="panel">
          <div className="sectionHead"><h2>领域知识状态</h2><span>{knowledge.total || 0}</span></div>
          <LearningStatusRows values={knowledgeStatus} />
        </section>
        <section className="panel">
          <div className="sectionHead"><h2>经验状态</h2><span>{experiences.total || 0}</span></div>
          <LearningStatusRows values={experienceStatus} />
        </section>
      </section>
      <section className="panel">
        <div className="sectionHead">
          <h2>当前任务使用的已晋级记忆</h2>
          <span>{task?.task_id || "未选择任务"}</span>
        </div>
        <p className="learningPolicy">策略：{context.policy || "promoted_only"}。候选、冲突和被拒绝记录不会进入计划。</p>
        <div className="columns">
          <div>
            <strong>领域知识</strong>
            <List items={usedKnowledge} render={(item) => `${item.subject || "?"} -> ${item.relation || "?"} -> ${item.object || "?"} | ${item.lifecycle_status || "unknown"} | 来源 ${(item.evidence_refs || []).join(", ") || "未记录"}`} />
          </div>
          <div>
            <strong>任务经验</strong>
            <List items={usedExperiences} render={(item) => `${item.lesson || item.action || "未记录经验"} | ${item.lifecycle_status || "unknown"} | 复用 ${item.reuse_count || 0}`} />
          </div>
        </div>
      </section>
      <section className="columns">
        <section className="panel">
          <div className="sectionHead"><h2>知识候选与正式状态</h2><span>{knowledgeItems.length}</span></div>
          <List items={knowledgeItems} render={(item) => `${item.subject || "?"} -> ${item.relation || "?"} -> ${item.object || "?"} | ${item.lifecycle_status || "unknown"} | ${item.evidence_type || "unknown"} | ${(item.evidence_refs || []).join(", ") || "无来源"}`} />
        </section>
        <section className="panel">
          <div className="sectionHead"><h2>经验候选与正式状态</h2><span>{experienceItems.length}</span></div>
          <List items={experienceItems} render={(item) => `${item.lesson || item.action || "未记录经验"} | ${item.lifecycle_status || "unknown"} | 复用 ${item.reuse_count || 0} | ${item.error_attribution || "无错误归因"}`} />
        </section>
      </section>
      <section className="panel">
        <div className="sectionHead"><h2>本任务沉淀</h2><span>{taskLearning.learning_episode_id ? "已记录" : "未到终态"}</span></div>
        <div className="learningTrace">
          <span>Episode：{taskLearning.learning_episode_id || "not_available"}</span>
          <span>经验候选：{taskLearning.experience_candidate_id || "not_available"}</span>
          <span>最终状态：{taskLearning.episode?.final_status || "not_available"}</span>
        </div>
      </section>
    </section>
  );
}

function LearningStatusRows({ values }) {
  const rows = Object.entries(values || {});
  if (!rows.length) return <p className="empty">暂无记录</p>;
  return <div className="learningStatusRows">{rows.map(([status, count]) => <div key={status}><span>{status}</span><strong>{count}</strong></div>)}</div>;
}
