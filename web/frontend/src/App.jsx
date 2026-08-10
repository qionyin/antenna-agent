import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  Bot,
  Braces,
  CheckCircle2,
  FileText,
  Gauge,
  GitBranch,
  KeyRound,
  ListChecks,
  MessageSquare,
  Network,
  Play,
  RadioTower,
  RefreshCw,
  Send,
  ShieldCheck,
  Workflow,
  XCircle
} from "lucide-react";
import "./styles.css";

const API = "http://127.0.0.1:8000";
const PLACEHOLDER_PATHS = new Set([
  "d:\\path\\model.cst",
  "d:/path/model.cst",
  "d:\\path\\model.json",
  "d:/path/model.json"
]);

const STATE_LABELS = {
  created: "已创建",
  preflight_running: "预检中",
  running: "运行中",
  waiting_approval: "等待审批",
  failed: "失败",
  completed: "完成"
};

const STAGE_LABELS = {
  created: "已创建",
  preflight: "环境预检",
  waiting_approval: "等待真实 CST 审批",
  cst_single_run: "真实 CST 单次运行",
  result_parse: "结果解析",
  report: "报告生成",
  failed: "失败",
  completed: "完成"
};

const ROUTE_DECISION_LABELS = {
  route_selected: "已选择专用能力",
  no_specialized_skill_needed: "无需专用能力",
  ambiguous: "需要审查确认",
  not_planned: "尚未规划"
};

const REVIEW_STATUS_LABELS = {
  pass: "通过",
  passed: "已通过",
  pending: "等待执行",
  running: "执行中",
  blocked: "阻塞",
  block: "阻塞",
  revise: "需要修改",
  reroute: "需要重规划",
  wait_user: "等待用户",
  not_reviewed: "未审查",
  no_issue: "未发现问题",
  advisory: "提示",
  needs_reroute: "需要重规划"
};

const ROUTE_STATUS_LABELS = {
  available: "可用",
  planned: "已规划",
  missing_capability: "能力缺失",
  excluded: "已排除",
  blocked: "阻塞"
};

const ROUTE_STAGE_LABELS = {
  paperwise_evidence: "论文证据检索",
  pdf_extract: "PDF / 图片提取",
  idea_evidence: "创新证据分析",
  experiment_contract: "实验目标规划",
  baseline_ablation: "基线与消融",
  geometry_evidence: "几何建模证据",
  cst_control: "CST 控制",
  e_platform: "E 盘平台能力",
  claim_assessment: "结论支撑审查",
  phase_review: "阶段审查",
  antenna_skill_router: "天线能力总分派"
};

const REFLECTION_ERROR_LABELS = {
  none: "无问题",
  false_positive: "误触发",
  false_negative: "漏触发",
  wrong_primary: "主能力选错",
  missing_dependency: "缺少依赖",
  unsafe_execution: "存在风险执行",
  weak_evidence: "证据偏弱"
};

const SKILL_LABELS = {
  "paperwise": "PaperWise 论文证据",
  "antenna-skills": "天线技能总入口",
  "antenna-research-ideation": "论文复现与几何建模",
  "antenna-research-idea-advisor": "创新与论文图谱分析",
  "antenna-claim-experiment-planner": "实验目标规划",
  "antenna-baseline-ablation-planner": "基线与消融设计",
  "antenna-result-to-claim": "结果支撑结论审查",
  "antenna-research-reviewer": "阶段审查",
  "cst-control": "CST 控制",
  "e-platform-cst": "E 盘 CST 平台",
  "pdf": "PDF 读取"
};

const ROUTE_ACTION_LABELS = {
  retrieve_and_trace_evidence: "检索并追溯论文证据",
  extract_pdf_or_figure_evidence: "提取 PDF 或图片证据",
  review_graph_and_novelty_candidates: "审查图谱和创新候选",
  plan_claim_experiment_contract: "规划可验证实验目标",
  plan_baselines_and_ablations: "规划基线和消融实验",
  prepare_geometry_evidence_and_gate: "准备几何证据并过建模门",
  preflight_or_execute_cst_after_approval: "审批后预检或执行 CST",
  select_e_platform_cst_adapter: "选择 E 盘 CST 适配器",
  assess_results_against_claim: "判断结果能否支撑结论",
  review_selected_skill_outputs_before_next_phase: "审查后决定能否进入下一步",
  route_top_level_antenna_workflow: "分派天线工作流",
  route_specialized_skill: "分派专用能力"
};

const STEP_LABELS = {
  step_001_preflight: "第 1 步：环境预检",
  step_002_approval: "第 2 步：等待用户审批",
  step_003_cst_run: "第 3 步：真实 CST 单次运行",
  step_004_parse: "第 4 步：解析仿真结果",
  step_005_report: "第 5 步：生成报告",
  step_001_evidence: "第 1 步：收集论文证据",
  step_002_goal_contract: "第 2 步：整理目标和约束",
  step_003_geometry: "第 3 步：准备几何建模证据",
  step_004_baseline: "第 4 步：规划对照实验",
  step_005_claim: "第 5 步：审查结论支撑",
  step_007_report: "第 7 步：生成报告",
  step_001_intake: "第 1 步：理解用户目标",
  step_002_review: "第 2 步：审查是否需要重分派",
  step_arbw_claim_review: "轴比带宽结论审查",
  step_claim_evidence_check: "结论论文证据检查",
  step_geometry_readiness: "几何建模就绪检查"
};

const STEP_GOAL_LABELS = {
  "Check CST path, adapter, permissions, and output directory": "检查 CST 路径、适配器、权限和输出目录",
  "Wait for user approval before real CST solver": "真实 CST 求解器启动前等待用户审批",
  "Run one real CST job through the approved adapter": "通过已审批适配器执行一次真实 CST 任务",
  "Parse S11, return loss, and bandwidth from CST outputs": "从 CST 输出中解析 S11、回波损耗和带宽",
  "Review parsed CST result sanity and evidence sufficiency": "审查解析结果是否合理、证据是否足够",
  "Generate completed or failed report with artifacts and review conclusion": "生成包含产物和审查结论的完成或失败报告",
  "Collect traceable PaperWise or artifact evidence for the antenna goal": "为天线目标收集可追溯论文或产物证据",
  "Normalize antenna type, target metrics, algorithm hints, and constraints": "整理天线类型、优化指标、算法线索和约束",
  "Prepare geometry evidence and model-readiness gate": "准备几何证据并检查是否可进入建模",
  "Plan baseline and ablation checks for the claim": "为结论规划基线和消融检查",
  "Assess whether available evidence can support the antenna claim": "判断现有证据能否支撑天线结论",
  "Write an understandable report from the dynamic plan outputs": "根据动态计划输出写出可读报告",
  "Understand the user request without specialized antenna skills": "不调用专用天线能力，先理解用户目标",
  "Review whether the general response needs reroute": "审查通用任务是否需要重新分派"
};

const AGENT_LABELS = {
  preflight_task_agent: "环境预检执行角色",
  preflight_review_agent: "环境预检模块审查",
  approval_task_agent: "审批等待执行角色",
  cst_run_task_agent: "CST 运行执行角色",
  result_parse_task_agent: "结果解析执行角色",
  report_task_agent: "报告生成执行角色",
  evidence_retrieval_task_agent: "论文证据检索执行角色",
  goal_contract_task_agent: "目标整理执行角色",
  geometry_evidence_task_agent: "几何证据执行角色",
  baseline_ablation_task_agent: "基线消融执行角色",
  claim_assessment_task_agent: "结论支撑执行角色",
  global_review_agent: "全局审查",
  general_task_agent: "通用任务执行角色",
  antenna_result_to_claim_task_agent: "结果到结论审查执行角色",
  evidence_check_task_agent: "证据检查执行角色"
};

const DEFAULT_EVIDENCE_POOL = {
  status: "not_loaded",
  read_only: true,
  review_mode: "not_loaded",
  react_review: { accepted: [], rejected: [], uncertain: [], evidence_gaps: [] },
  sources: {
    deep_read_papers: {
      status: "waiting_for_task",
      support_level: "not_loaded",
      roles: ["reproduction", "evidence"],
      count: 0,
      description: "精读报告：从向量库召回语义块后，追溯到源头论文和 PaperWise 精读报告。"
    },
    graph_library: {
      status: "waiting_for_task",
      support_level: "not_loaded",
      roles: ["innovation", "relation"],
      relation_count: 0,
      description: "图谱库：论文、结构、指标和概念关系，用于创新判断与关系证据追踪。"
    },
    llm_semantic_expansion: {
      status: "waiting_for_task",
      support_level: "not_loaded",
      roles: ["semantic_expansion"],
      count: 0,
      description: "LLM 语义扩展：只做候选扩展和理由解释，必须绑定论文证据并通过 gate 后才能采用。"
    }
  }
};

const EVIDENCE_SOURCE_LABELS = {
  deep_read_papers: "精读报告",
  graph_library: "创新",
  llm_semantic_expansion: "LLM 语义扩展"
};

function label(value, table = STATE_LABELS) {
  return table[value] || value || "未设置";
}

function cn(value, table, fallback = "未设置") {
  if (value === null || value === undefined || value === "") return fallback;
  return table[value] || String(value);
}

function cnList(values, table) {
  const list = Array.isArray(values) ? values : [];
  if (!list.length) return "无";
  return list.map((item) => cn(item, table, item)).join("、");
}

function routeText(item) {
  const stage = cn(item.stage_id, ROUTE_STAGE_LABELS, item.stage_id);
  const owner = cn(item.owner_skill, SKILL_LABELS, item.owner_skill);
  const action = cn(item.action, ROUTE_ACTION_LABELS, item.action);
  const status = cn(item.status, ROUTE_STATUS_LABELS, item.status);
  const confidence = Number(item.confidence);
  const scores = item.scores || {};
  const band = routeConfidenceBand(confidence);
  const reason = routeConfidenceReason(scores);
  const score = Number.isFinite(confidence)
    ? `，路由置信度 ${(confidence * 100).toFixed(0)}%（${band}；${reason}；领域 ${prettyPercent(scores.domain_score)}，任务 ${prettyPercent(scores.task_type_score)}，产物 ${prettyPercent(scores.artifact_score)}，上下文 ${prettyPercent(scores.context_score)}，负面扣分 ${prettyPercent(scores.negative_score)}）`
    : "";
  return `${stage}：调用 ${owner}；动作：${action}；状态：${status}${score}`;
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function mergeCentralReplies(replies, latestReply) {
  const items = [...asArray(replies)];
  if (latestReply && !items.some((item) => item.reply_id === latestReply.reply_id && item.created_at === latestReply.created_at)) {
    items.push(latestReply);
  }
  return items;
}

function centralReplyStepId(reply = {}) {
  return reply.target_step_id || reply.step_id || reply.message?.target_step_id || reply.source_message?.target_step_id || null;
}

function centralReplyChanged(value) {
  return value === true || value === "true" || value === "yes";
}

function centralReplyChange(reply = {}) {
  return reply.changed || {};
}

function centralReplyPlanVersion(reply = {}) {
  const changed = centralReplyChange(reply);
  return reply.plan_version || reply.new_plan_version || reply.dynamic_plan_version || changed.new_plan_version || changed.dynamic_plan_version || changed.dynamic_plan_version;
}

function centralReplyPlanChanged(reply = {}) {
  const changed = centralReplyChange(reply);
  return centralReplyChanged(reply.plan_changed) || reply.plan_mutation_allowed === true || Boolean(changed.new_plan_version);
}

function centralReplySkillChanged(reply = {}) {
  const changed = centralReplyChange(reply);
  return centralReplyChanged(reply.skill_changed) || reply.skill_route_mutation_allowed === true || asArray(changed.changed_skills).length > 0;
}

function centralReplyChangedSteps(reply = {}) {
  return asArray(reply.changed_steps).length ? asArray(reply.changed_steps) : asArray(centralReplyChange(reply).changed_steps);
}

function centralReplyChangedSkills(reply = {}) {
  return asArray(reply.changed_skills).length ? asArray(reply.changed_skills) : asArray(centralReplyChange(reply).changed_skills);
}

function centralReplyStatus(reply = {}) {
  const action = reply.action || reply.intent;
  if (action === "request_revision" || action === "revise_current_step") return "本步骤已标记为需要修改";
  if (action === "request_reroute" || action === "reroute_plan") {
    const version = centralReplyPlanVersion(reply);
    return `已生成新计划${version ? ` v${version}` : ""}`;
  }
  if (action === "ask_question" || action === "comment" || action === "reply_only") return "仅回复，未修改计划";
  if (action === "risk_acknowledged") return "风险确认已记录";
  if (centralReplyPlanChanged(reply)) return "计划已更新";
  return "仅回复，未修改计划";
}

function centralReplyListText(values) {
  const list = asArray(values);
  return list.length
    ? list.map((item) => (typeof item === "string" ? item : item.step_id || item.skill_id || item.id || item.name || JSON.stringify(item))).join("、")
    : "无";
}

function skillDisplayName(item) {
  if (!item) return "未设置";
  if (typeof item === "string") return cn(item, SKILL_LABELS, item);
  return cn(item.owner_skill || item.skill_id || item.id || item.name, SKILL_LABELS, item.owner_skill || item.skill_id || item.id || item.name || "未设置");
}

function scoreParts(item = {}) {
  const scores = item.scores || {};
  return [
    ["领域", scores.domain_score],
    ["任务", scores.task_type_score],
    ["产物", scores.artifact_score],
    ["证据", scores.evidence_score],
    ["审查反馈", scores.review_feedback_score],
    ["上下文", scores.context_score],
    ["负面扣分", scores.negative_score]
  ];
}

function skillConfidenceText(item = {}) {
  const confidence = Number(item.confidence ?? item.scores?.final_score);
  if (!Number.isFinite(confidence)) return "置信度：未记录";
  return `置信度 ${(confidence * 100).toFixed(0)}% · ${routeConfidenceBand(confidence)}`;
}

function skillNotCalledReason(item = {}) {
  const confidence = Number(item.confidence ?? item.scores?.final_score);
  if (Number.isFinite(confidence) && confidence >= 0.45 && confidence < 0.70) {
    return "未调用：45%-70% 只作为候选技能，需要后续证据或审查建议升为可调用。";
  }
  return `未调用：${item.reason || cn(item.status, ROUTE_STATUS_LABELS, item.status || "未达到调用门槛")}`;
}

function latestForStep(records, stepId) {
  const matched = asArray(records).filter((item) => item.step_id === stepId || item.output?.step_id === stepId);
  return matched.length ? matched[matched.length - 1] : {};
}

function readableDecision(value) {
  const table = {
    pass_next_step: "通过，进入下一步",
    revise_current_step: "修改当前步骤",
    refresh_skill_route: "刷新技能路由",
    reroute_plan: "重规划",
    block_task: "阻塞任务",
    wait_user: "等待用户",
    complete_task: "完成任务"
  };
  return cn(value, { ...REVIEW_STATUS_LABELS, ...table }, value || "未裁决");
}

function routeConfidenceBand(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "无评分";
  if (number >= 0.85) return "强命中";
  if (number >= 0.70) return "较强命中";
  if (number >= 0.45) return "候选未调用";
  return "未达选择线";
}

function routeConfidenceReason(scores = {}) {
  const domain = Number(scores.domain_score || 0);
  const task = Number(scores.task_type_score || 0);
  const artifact = Number(scores.artifact_score || 0);
  const context = Number(scores.context_score || 0);
  const negative = Number(scores.negative_score || 0);
  const parts = [];
  if (domain > 0) parts.push("命中领域词");
  if (task > 0) parts.push("命中任务动作词");
  if (artifact > 0) parts.push("命中产物/文件名");
  if (context > 0) parts.push("由天线上下文补强");
  if (negative > 0) parts.push("存在负面语境扣分");
  return parts.length ? parts.join("、") : "没有明确命中项";
}

function dynamicStepText(item) {
  const step = cn(item.step_id, STEP_LABELS, item.step_id);
  const goal = cn(item.step_goal, STEP_GOAL_LABELS, item.step_goal);
  const taskAgent = cn(item.task_agent, AGENT_LABELS, item.task_agent);
  const reviewAgent = cn(item.module_review_agent || item.review_agent, AGENT_LABELS, item.module_review_agent || item.review_agent);
  const skills = cnList(asArray(item.callable_skills).map((skill) => skill.owner_skill || skill), SKILL_LABELS);
  const candidateCount = asArray(item.candidate_skills).length;
  const status = cn(item.status, REVIEW_STATUS_LABELS, item.status);
  return `${step}：${goal}；执行角色：${taskAgent}；模块审查：${reviewAgent}；调用技能：${skills}；候选未调用：${candidateCount}；状态：${status}`;
}

function cozeStatus(step, currentStepId) {
  if (!step) return "idle";
  const status = String(step.status || "");
  if (step.step_id === currentStepId) return "active";
  if (["passed", "pass", "done", "completed", "pass_next_step"].includes(status)) return "done";
  if (["blocked", "block", "failed"].includes(status)) return "blocked";
  if (["revise", "reroute", "wait_user", "waiting_approval"].includes(status)) return "waiting";
  return status || "pending";
}

function stepPrimarySkill(step = {}) {
  const callable = asArray(step.callable_skills);
  const candidate = asArray(step.candidate_skills);
  const first = callable[0] || candidate[0];
  return first ? skillDisplayName(first) : "无";
}

function stepAgentCount(step = {}) {
  let count = 0;
  if (step.task_agent) count += 1;
  if (step.module_review_agent || step.review_agent) count += 1;
  if (step.global_review_required) count += 1;
  return count;
}

function workflowDetailText(node = {}) {
  if (node.type === "step") return dynamicStepText(node.step || {});
  if (node.type === "task") return `执行员：${cn(node.agent, AGENT_LABELS, node.agent)}；负责完成当前步骤产物。`;
  if (node.type === "review") return `审查员：${cn(node.agent, AGENT_LABELS, node.agent)}；负责检查产物、证据和下一步可执行性。`;
  if (node.type === "skill") return `技能：${node.skillName || "无"}；当前节点只展示能力判断，不绕过 gate。`;
  return node.description || "";
}

function reflectionTitle(reflection) {
  const status = cn(reflection.status, REVIEW_STATUS_LABELS, reflection.status || "未审查");
  const error = cn(reflection.error_type || "none", REFLECTION_ERROR_LABELS, "无问题");
  const confidence = Number(reflection.confidence);
  return Number.isFinite(confidence) ? `${status} / ${error} / 置信度 ${(confidence * 100).toFixed(0)}%` : `${status} / ${error}`;
}

function reflectionBasisText(reflection) {
  const basis = reflection.confidence_basis || {};
  if (!basis.source) return "置信度来源：旧任务缺少来源记录";
  if (basis.source === "rule_review") {
    return `置信度来源：规则审查；证据等级 ${basis.evidence_level || "未知"}；最低通过要求 ${(Number(basis.minimum_pass_confidence || 0.9) * 100).toFixed(0)}%；决策加权 ${prettyPercent(basis.decision_bonus)}；问题加权 ${prettyPercent(basis.issue_bonus)}。`;
  }
  if (basis.source === "packet_rule_review") {
    return `置信度来源：packet 规则审查；最低通过要求 ${(Number(basis.minimum_pass_confidence || 0.9) * 100).toFixed(0)}%；依据：${basis.reason || "确定性规则检查"}。`;
  }
  return `置信度来源：${basis.source}`;
}

function prettyPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0%";
  return `${(number * 100).toFixed(0)}%`;
}

async function getJson(path, fallback) {
  try {
    const response = await fetch(`${API}${path}`);
    if (!response.ok) return fallback;
    return await response.json();
  } catch {
    return fallback;
  }
}

function prettyNumber(value) {
  if (value === null || value === undefined || value === "not_available") return "not_available";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toFixed(3);
}

function cleanInputPath(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const normalized = text.replaceAll("/", "\\").replace(/\\+/g, "\\").toLowerCase();
  if (PLACEHOLDER_PATHS.has(normalized)) return "";
  return text;
}

function hasExtension(path, extension) {
  return String(path || "").trim().toLowerCase().endsWith(extension);
}

function checkText(item) {
  const status = item.ok ? "通过" : "未通过";
  const name = item.name || item.type || "preflight";
  const detail = item.reason || item.hint || item.path || "";
  return `${status} | ${name}${detail ? ` | ${detail}` : ""}`;
}

function App() {
  const [health, setHealth] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [task, setTask] = useState(null);
  const [artifacts, setArtifacts] = useState([]);
  const [logs, setLogs] = useState([]);
  const [reports, setReports] = useState([]);
  const [actions, setActions] = useState([]);
  const [mode, setMode] = useState("real");
  const [taskTitle, setTaskTitle] = useState("真实 CST 单次运行 - 贴片天线 - S11 验证");
  const [userInput, setUserInput] = useState("贴片天线 S11 单次真实 CST 验证");
  const [projectPath, setProjectPath] = useState("");
  const [modelJsonPath, setModelJsonPath] = useState("");
  const [parametersText, setParametersText] = useState('{"w": 10}');
  const [simulateCst, setSimulateCst] = useState(false);
  const [error, setError] = useState("");
  const [view, setView] = useState("dashboard");
  const [llmSettings, setLlmSettings] = useState({ enabled: false, base_url: "", model_name: "", embedding_model_name: "text-embedding-v3", api_key_configured: false });
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModelName, setLlmModelName] = useState("");
  const [llmEmbeddingModelName, setLlmEmbeddingModelName] = useState("text-embedding-v3");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [paperwiseReviewBusy, setPaperwiseReviewBusy] = useState(false);
  const [centralIntent, setCentralIntent] = useState("request_revision");
  const [centralMessage, setCentralMessage] = useState("");
  const [centralMessageBusy, setCentralMessageBusy] = useState(false);
  const [stepCentralDrafts, setStepCentralDrafts] = useState({});
  const [stepCentralBusy, setStepCentralBusy] = useState("");
  const [selectedWorkflowNodeId, setSelectedWorkflowNodeId] = useState("");

  const metadata = task?.task_metadata || {};
  const results = metadata.results || {};
  const metrics = results.metrics || {};
  const preflight = metadata.preflight || {};
  const preflightChecks = preflight.checks || [];
  const preflightBlockers = (metadata.reviews || []).flatMap((item) => item.structured_blockers || []);
  const evidencePool = metadata.evidence_pool_summary || DEFAULT_EVIDENCE_POOL;
  const evidenceSources = evidencePool.sources || {};
  const skillRoutePlan = metadata.skill_route_plan || {};
  const skillRouteReview = metadata.skill_route_review || {};
  const skillRoutes = skillRoutePlan.routes || [];
  const skillRouteHistory = asArray(metadata.skill_route_plan_history);
  const dynamicPlan = metadata.dynamic_plan || {};
  const dynamicSteps = dynamicPlan.steps || [];
  const currentStepId = metadata.current_stage;
  const currentStep = dynamicSteps.find((item) => item.step_id === currentStepId) || dynamicSteps.find((item) => !["passed", "pass_next_step"].includes(item.status)) || dynamicSteps[dynamicSteps.length - 1] || {};
  const stepSkillContexts = asArray(metadata.step_skill_contexts);
  const currentStepSkillContext = currentStep.step_skill_context || stepSkillContexts.findLast?.((item) => item.step_id === currentStep.step_id) || stepSkillContexts[stepSkillContexts.length - 1] || {};
  const currentCallableSkills = asArray(currentStep.callable_skills?.length ? currentStep.callable_skills : currentStepSkillContext.callable_skills?.length ? currentStepSkillContext.callable_skills : skillRoutePlan.callable_skills || skillRoutes);
  const currentCandidateSkills = asArray(currentStep.candidate_skills?.length ? currentStep.candidate_skills : currentStepSkillContext.candidate_skills?.length ? currentStepSkillContext.candidate_skills : skillRoutePlan.candidate_skills);
  const currentExcludedSkills = asArray(currentStep.blocked_skills?.length ? currentStep.blocked_skills : currentStepSkillContext.excluded_skills?.length ? currentStepSkillContext.excluded_skills : skillRoutePlan.excluded_skills);
  const moduleReviewRecords = asArray(metadata.module_review_records);
  const globalReviewRecords = asArray(metadata.global_review_records);
  const centralDecisions = asArray(metadata.central_decisions);
  const centralMessages = asArray(metadata.central_agent_messages);
  const centralReplies = mergeCentralReplies(metadata.central_agent_replies, metadata.last_central_agent_reply);
  const reflectionRecords = metadata.reflection_records || [];
  const latestReflection = reflectionRecords.length ? reflectionRecords[reflectionRecords.length - 1] : {};
  const currentApproval = task ? task.approvals?.[`real_cst:${task.task_id}`] : null;
  const isWaitingApproval = task?.state === "waiting_approval";
  const canApprove = isWaitingApproval && Boolean(task?.task_id);
  const canReject = isWaitingApproval && Boolean(task?.task_id);

  const visibleTasks = useMemo(() => tasks.slice(0, 30), [tasks]);

  useEffect(() => {
    refreshAll();
  }, []);

  useEffect(() => {
    if (!task?.task_id) return undefined;
    const timer = window.setInterval(() => refreshTask(task.task_id), 3000);
    return () => window.clearInterval(timer);
  }, [task?.task_id]);

  async function refreshAll() {
    setHealth(await getJson("/health", { status: "offline" }));
    const nextLlmSettings = await getJson("/settings/llm-runtime", llmSettings);
    setLlmSettings(nextLlmSettings);
    setLlmBaseUrl(nextLlmSettings.base_url || "");
    setLlmModelName(nextLlmSettings.model_name || "");
    setLlmEmbeddingModelName(nextLlmSettings.embedding_model_name || "text-embedding-v3");
    setTasks(await getJson("/tasks", []));
    if (task?.task_id) {
      await refreshTask(task.task_id);
    }
  }

  async function refreshTask(taskId) {
    const nextTask = await getJson(`/tasks/${taskId}/state`, null);
    if (!nextTask) return;
    setTask(nextTask);
    setTaskTitle(nextTask.task_metadata?.task_title || nextTask.task_id);
    setArtifacts(await getJson(`/tasks/${taskId}/artifacts`, []));
    setLogs(await getJson(`/tasks/${taskId}/logs`, []));
    setReports(await getJson(`/tasks/${taskId}/reports`, []));
    const actionData = await getJson(`/tasks/${taskId}/available-actions`, { actions: [] });
    setActions(actionData.actions || []);
  }

  async function createTask() {
    setError("");
    if (mode === "mock") {
      const data = await postJson("/tasks", { user_input: userInput });
      await afterCreate(data);
      return;
    }
    const cleanProjectPath = cleanInputPath(projectPath);
    const cleanModelJsonPath = cleanInputPath(modelJsonPath);
    if (!simulateCst && !cleanProjectPath && !cleanModelJsonPath) {
      setError("真实模式需要填写真实的 CST 项目路径（.cst）或模型 JSON 路径；D:\\path\\model.cst 只是示例，不能作为输入。");
      return;
    }
    if (cleanProjectPath && !hasExtension(cleanProjectPath, ".cst")) {
      setError("CST 项目路径必须指向 .cst 文件。");
      return;
    }
    if (cleanModelJsonPath && !hasExtension(cleanModelJsonPath, ".json")) {
      setError("模型 JSON 路径必须指向 .json 文件。");
      return;
    }
    let parameters = {};
    if (parametersText.trim()) {
      try {
        parameters = JSON.parse(parametersText);
      } catch {
        setError("参数 JSON 格式错误");
        return;
      }
    }
    const data = await postJson("/tasks/real-cst-single-run", {
      user_input: userInput,
      task_title: taskTitle,
      project_path: cleanProjectPath || null,
      model_json_path: cleanModelJsonPath || null,
      parameters,
      simulate_cst: simulateCst,
      target_metric: "S11"
    });
    await afterCreate(data);
  }

  async function afterCreate(data) {
    if (data?.error) {
      setError(data.message || data.error);
      return;
    }
    setTask(data);
    await refreshTask(data.task_id);
    setTasks(await getJson("/tasks", []));
  }

  async function postJson(path, body = {}) {
    try {
      const response = await fetch(`${API}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      return await response.json();
    } catch (err) {
      return {
        error: "backend_unreachable",
        message: `无法连接后端 ${API}，请先启动后端服务。`
      };
    }
  }

  async function approveRealCst() {
    if (!task?.task_id) return;
    const data = await postJson(`/tasks/${task.task_id}/approve`);
    if (data?.error) {
      setError(data.message || data.error);
      return;
    }
    await refreshTask(task.task_id);
    setTasks(await getJson("/tasks", []));
  }

  async function rejectRealCst() {
    if (!task?.task_id) return;
    const data = await postJson(`/tasks/${task.task_id}/reject`);
    if (data?.error) {
      setError(data.message || data.error);
      return;
    }
    await refreshTask(task.task_id);
    setTasks(await getJson("/tasks", []));
  }

  async function rerunPaperwiseReview() {
    if (!task?.task_id) return;
    setError("");
    setPaperwiseReviewBusy(true);
    const data = await postJson(`/tasks/${task.task_id}/paperwise/review`);
    setPaperwiseReviewBusy(false);
    if (data?.error) {
      setError(data.message || data.error);
      return;
    }
    setTask(data);
    await refreshTask(task.task_id);
    setTasks(await getJson("/tasks", []));
  }

  async function submitCentralMessage({ intent, targetStepId, message, onSuccess, busyKey = "overview" }) {
    if (!task?.task_id || !String(message || "").trim()) return;
    setError("");
    if (busyKey === "overview") {
      setCentralMessageBusy(true);
    } else {
      setStepCentralBusy(busyKey);
    }
    const data = await postJson(`/tasks/${task.task_id}/central-message`, {
      intent,
      target_step_id: targetStepId || null,
      message: String(message).trim()
    });
    if (busyKey === "overview") {
      setCentralMessageBusy(false);
    } else {
      setStepCentralBusy("");
    }
    if (data?.error) {
      setError(data.message || data.error);
      return;
    }
    onSuccess?.();
    setTask(data);
    await refreshTask(task.task_id);
    setTasks(await getJson("/tasks", []));
  }

  async function sendCentralMessage() {
    await submitCentralMessage({
      intent: centralIntent,
      targetStepId: currentStep.step_id || metadata.current_stage || null,
      message: centralMessage,
      onSuccess: () => setCentralMessage(""),
      busyKey: "overview"
    });
  }

  function updateStepCentralDraft(stepId, patch) {
    setStepCentralDrafts((drafts) => ({
      ...drafts,
      [stepId]: {
        intent: "ask_question",
        message: "",
        open: false,
        ...(drafts[stepId] || {}),
        ...patch
      }
    }));
  }

  async function sendStepCentralMessage(stepId) {
    const draft = stepCentralDrafts[stepId] || {};
    await submitCentralMessage({
      intent: draft.intent || "ask_question",
      targetStepId: stepId,
      message: draft.message || "",
      onSuccess: () => updateStepCentralDraft(stepId, { message: "" }),
      busyKey: stepId
    });
  }

  async function saveRuntimeLlmSettings() {
    setError("");
    const data = await postJson("/settings/llm-runtime", {
      enabled: true,
      base_url: llmBaseUrl,
      model_name: llmModelName,
      embedding_model_name: llmEmbeddingModelName,
      api_key: llmApiKey
    });
    if (data?.error) {
      setError(data.message || data.error);
      return;
    }
    setLlmSettings(data);
    setLlmApiKey("");
  }

  async function disableRuntimeLlm() {
    const data = await postJson("/settings/llm-runtime", {
      enabled: false,
      base_url: llmBaseUrl,
      model_name: llmModelName,
      embedding_model_name: llmEmbeddingModelName,
      api_key: ""
    });
    setLlmSettings(data);
  }

  async function selectTask(item) {
    await refreshTask(item.task_id);
  }

  function openView(nextView) {
    setView(nextView);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return (
    <main className="appShell">
      <aside className="rail">
        <div className="brand"><RadioTower size={19} /> antenna_agent_lab</div>
        <button className={view === "workflow" ? "activeNav" : ""} onClick={() => openView("workflow")}><Workflow size={17} /> 工作流</button>
        <button className={view === "dashboard" ? "activeNav" : ""} onClick={() => openView("dashboard")}><Gauge size={17} /> 总览</button>
        <button className={view === "evidence" ? "activeNav" : ""} onClick={() => openView("evidence")}><ShieldCheck size={17} /> PaperWise</button>
        <button className={view === "reports" ? "activeNav" : ""} onClick={() => openView("reports")}><FileText size={17} /> 报告</button>
        <button className={view === "logs" ? "activeNav" : ""} onClick={() => openView("logs")}><Activity size={17} /> 日志</button>
        <button className={view === "settings" ? "activeNav" : ""} onClick={() => openView("settings")}><KeyRound size={17} /> 设置</button>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <h1>V2.2 真实 CST 单次闭环</h1>
            <p>API {label(health?.status, { ok: "正常", offline: "离线" })} | 状态机 6 态 | 当前 {task ? label(task.state) : "未选择任务"}</p>
            <p className="topbarEvidence">V2.2 已接入 PaperWise：精读报告 / 创新（只读）</p>
          </div>
          <button onClick={refreshAll}><RefreshCw size={17} /> 刷新</button>
        </header>

        {view === "dashboard" && (
        <>
        <section className="dashboardShell">
          <aside className="dashboardSidebar">
            <section className="panel createPanel compactCreate" id="task-create">
              <div className="sectionHead">
                <h2>任务创建</h2>
                <div className="segmented">
                  <button className={mode === "real" ? "selected" : ""} onClick={() => setMode("real")}>真实</button>
                  <button className={mode === "mock" ? "selected" : ""} onClick={() => setMode("mock")}>模拟</button>
                </div>
              </div>

              <div className="formGrid">
                <label className="wide">
                  <span>中文任务标题</span>
                  <input value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)} />
                </label>
                <label className="wide">
                  <span>任务目标</span>
                  <input value={userInput} onChange={(event) => setUserInput(event.target.value)} />
                </label>
                <label className="wide">
                  <span>CST 项目路径</span>
                  <input value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="D:\\path\\model.cst" />
                </label>
                <label className="wide">
                  <span>模型 JSON 路径</span>
                  <input value={modelJsonPath} onChange={(event) => setModelJsonPath(event.target.value)} placeholder="可选" />
                </label>
                <label className="wide">
                  <span>参数 JSON</span>
                  <textarea rows={3} value={parametersText} onChange={(event) => setParametersText(event.target.value)} />
                </label>
                <label className="checkLine">
                  <input type="checkbox" checked={simulateCst} onChange={(event) => setSimulateCst(event.target.checked)} />
                  <span>使用模拟 CST 结果</span>
                </label>
              </div>
              <div className="toolbar">
                <button className="primary" onClick={createTask}><Play size={17} /> 创建任务</button>
              </div>
              {error && <span className="errorText">{error}</span>}
            </section>

            <section className="panel taskList dashboardTaskList">
              <div className="sectionHead">
                <h2>任务列表</h2>
                <span>{visibleTasks.length}</span>
              </div>
              {visibleTasks.map((item) => (
                <button className={task?.task_id === item.task_id ? "taskRow active" : "taskRow"} key={item.task_id} onClick={() => selectTask(item)}>
                  <span>{item.task_metadata?.task_title || item.task_id}</span>
                  <strong>{label(item.state)}</strong>
                </button>
              ))}
            </section>
          </aside>

          <section className="dashboardMain">
            <section className="dashboardHeaderPanel" id="task-state">
              <div>
                <small>当前任务</small>
                <h2>{metadata.task_title || task?.task_id || "未选择任务"}</h2>
              </div>
              <div className="dashboardHeaderActions">
                <span className={`statePill ${task?.state || "created"}`}>{label(task?.state)}</span>
              </div>
            </section>

            {isWaitingApproval && task?.task_id && (
              <section className="approvalBanner" id="approval-gate">
                <div>
                  <strong>真实 CST solver 等待审批</strong>
                  <span>{currentApproval?.reason || "预检已完成。批准后才会启动真实 CST；拒绝会结束任务并生成失败报告。"}</span>
                </div>
                <div className="approvalActions">
                  {canApprove && <button className="approveButton" onClick={approveRealCst}><ShieldCheck size={18} /> 批准真实 CST solver</button>}
                  {canReject && <button className="rejectButton" onClick={rejectRealCst}><XCircle size={18} /> 拒绝真实 CST solver</button>}
                </div>
              </section>
            )}

            <section className="overviewGrid">
              <Metric label="当前阶段" value={label(metadata.current_stage, STAGE_LABELS)} />
              <Metric label="CST 状态" value={metadata.cst_status || "not_reached"} />
              <Metric label="本步骤调用技能" value={currentCallableSkills.length} />
              <Metric label="动态计划版本" value={dynamicPlan.plan_version ? `v${dynamicPlan.plan_version}` : "not_planned"} />
              <Metric label="审查反思" value={cn(latestReflection.status || "not_reviewed", REVIEW_STATUS_LABELS)} />
            </section>

            <section className="dashboardWorkArea">
              <section className="dashboardPrimary">
                {task?.task_id ? (
                  <CozeWorkflowPanel
                    task={task}
                    metadata={metadata}
                    dynamicPlan={dynamicPlan}
                    steps={dynamicSteps}
                    currentStep={currentStep}
                    selectedNodeId={selectedWorkflowNodeId}
                    onSelectNode={setSelectedWorkflowNodeId}
                  />
                ) : (
                  <section className="panel emptyWorkbench">
                    <Bot size={28} />
                    <h2>创建或选择一个任务</h2>
                    <p>任务创建后，这里会显示中枢动态计划、步骤级 task/review agent、skill 调用和输出流向。</p>
                  </section>
                )}

                {dynamicSteps.length > 0 && (
                  <section className="panel dynamicPlanPanel">
                    <div className="sectionHead">
                      <h2>中枢动态计划</h2>
                      <span>当前计划 v{dynamicPlan.plan_version || 1} · {dynamicSteps.length} 步</span>
                    </div>
                    <div className="stepCardList">
                      {dynamicSteps.map((step) => (
                        <DynamicStepCard
                          key={step.step_id}
                          step={step}
                          current={step.step_id === currentStep.step_id}
                          moduleReview={latestForStep(moduleReviewRecords, step.step_id)}
                          globalReview={latestForStep(globalReviewRecords, step.step_id)}
                          centralDecision={latestForStep(centralDecisions, step.step_id)}
                          centralReplies={centralReplies}
                          draft={stepCentralDrafts[step.step_id]}
                          busy={stepCentralBusy === step.step_id}
                          onDraftChange={updateStepCentralDraft}
                          onSend={sendStepCentralMessage}
                        />
                      ))}
                    </div>
                    {latestReflection.status && (
                      <div className="reflectionBox">
                        <strong>{reflectionTitle(latestReflection)}</strong>
                        <span>{latestReflection.why_central_failed || "未发现中枢计划问题"}</span>
                        <span>{latestReflection.what_should_change || "无需调整"}</span>
                        <span>{reflectionBasisText(latestReflection)}</span>
                      </div>
                    )}
                  </section>
                )}
              </section>

              <aside className="dashboardAside">
                {(currentCallableSkills.length > 0 || currentCandidateSkills.length > 0 || currentExcludedSkills.length > 0) && (
                  <SkillJudgementPanel
                    currentStep={currentStep}
                    dynamicPlan={dynamicPlan}
                    skillRouteReview={skillRouteReview}
                    stepSkillContext={currentStepSkillContext}
                    callableSkills={currentCallableSkills}
                    candidateSkills={currentCandidateSkills}
                    excludedSkills={currentExcludedSkills}
                  />
                )}

                {task?.task_id && (
                  <CentralAgentMessagePanel
                    currentStep={currentStep}
                    intent={centralIntent}
                    setIntent={setCentralIntent}
                    message={centralMessage}
                    setMessage={setCentralMessage}
                    busy={centralMessageBusy}
                    onSend={sendCentralMessage}
                    messages={centralMessages}
                    replies={centralReplies}
                  />
                )}

                {skillRouteHistory.length > 0 && (
                  <RouteHistoryPanel history={skillRouteHistory} />
                )}
              </aside>
            </section>

            {(preflightChecks.length > 0 || preflightBlockers.length > 0) && (
              <section className="panel">
                <div className="sectionHead">
                  <h2>Preflight checklist</h2>
                  <span>{preflight.success ? "通过" : "未通过"}</span>
                </div>
                <ul className="checkList">
                  {preflightChecks.map((item, index) => (
                    <li className={item.ok ? "checkItem pass" : "checkItem fail"} key={`${item.name || "check"}-${index}`}>
                      {item.ok ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
                      <span>{checkText(item)}</span>
                    </li>
                  ))}
                  {preflightBlockers.map((item, index) => (
                    <li className="checkItem fail" key={`${item.type || "blocker"}-${index}`}>
                      <XCircle size={16} />
                      <span>{item.reason}{item.hint ? ` | 建议: ${item.hint}` : ""}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="dashboardBottom">
              <section className="panel metricPanel">
                <div className="sectionHead">
                  <h2>结果指标</h2>
                  <span>{metadata.cst_status || "not_reached"}</span>
                </div>
                <div className="metricsBand">
                  <Metric label="S11 最小值" value={prettyNumber(metrics.s11_min_db)} suffix="dB" icon={Gauge} />
                  <Metric label="S11 频点" value={prettyNumber(metrics.s11_min_freq)} suffix="GHz" icon={RadioTower} />
                  <Metric label="Return loss" value={prettyNumber(metrics.return_loss_max_db)} suffix="dB" icon={Activity} />
                  <Metric label="-10 dB 带宽" value={prettyNumber(metrics.bandwidth_10db)} suffix="GHz" icon={ListChecks} />
                </div>
              </section>

              <section className="panel evidenceMiniPanel">
                <div className="sectionHead">
                  <h2>精读报告与创新</h2>
                  <button onClick={() => openView("evidence")}><ShieldCheck size={17} /> 打开</button>
                </div>
                <div className="statusGrid compactStatus">
                  <Metric label="PaperWise 接入" value={evidencePool.status || "not_loaded"} />
                  <Metric label="精读报告" value={evidenceSources.deep_read_papers?.status || evidenceSources.reports?.status || "not_loaded"} />
                  <Metric label="创新" value={evidenceSources.graph_library?.status || "not_loaded"} />
                </div>
              </section>
            </section>

            <section className="dashboardBottom">
              <section className="panel" id="task-artifacts">
                <div className="sectionHead">
                  <h2>Artifact</h2>
                  <span>{artifacts.length}</span>
                </div>
                <List items={artifacts.slice(0, 8)} render={(item) => `${item.name || item.artifact_type}: ${item.path}`} />
              </section>

              <section className="panel quickLinksPanel">
                <div className="sectionHead">
                  <h2>报告与日志</h2>
                  <span>{reports.length + logs.length}</span>
                </div>
                <div className="detailActions">
                  <button onClick={() => openView("reports")}><FileText size={17} /> 打开报告页</button>
                  <button onClick={() => openView("logs")}><Activity size={17} /> 打开日志页</button>
                </div>
                <List items={(metadata.reviews || []).slice(-4)} render={(item) => `${item.agent}: ${item.status} | ${item.conclusion || (item.blockers || []).join("; ")}`} />
              </section>
            </section>
          </section>
        </section>
        </>
        )}

        {view === "workflow" && (
          <section className="pageStack">
            <div className="pageHeader">
              <div>
                <h2>可视自动工作流</h2>
                <p>Coze 风格节点画布：有任务即可显示；如果后端尚未返回动态步骤，会先显示开始、中枢和输出占位节点。</p>
              </div>
              <button onClick={() => openView("dashboard")}><Gauge size={17} /> 返回总览</button>
            </div>
            <CozeWorkflowPanel
              task={task}
              metadata={metadata}
              dynamicPlan={dynamicPlan}
              steps={dynamicSteps}
              currentStep={currentStep}
              selectedNodeId={selectedWorkflowNodeId}
              onSelectNode={setSelectedWorkflowNodeId}
            />
          </section>
        )}

        {view === "evidence" && (
          <EvidencePage
            evidencePool={evidencePool}
            evidenceSources={evidenceSources}
            onRerunReview={rerunPaperwiseReview}
            reviewBusy={paperwiseReviewBusy}
            hasTask={Boolean(task?.task_id)}
            onBack={() => openView("dashboard")}
          />
        )}

        {view === "reports" && (
          <ReportsPage
            reports={reports}
            artifacts={artifacts}
            task={task}
            metadata={metadata}
            onBack={() => openView("dashboard")}
          />
        )}

        {view === "logs" && (
          <LogsPage logs={logs} onBack={() => openView("dashboard")} />
        )}

        {view === "settings" && (
          <SettingsPage
            llmSettings={llmSettings}
            llmBaseUrl={llmBaseUrl}
            setLlmBaseUrl={setLlmBaseUrl}
            llmModelName={llmModelName}
            setLlmModelName={setLlmModelName}
            llmEmbeddingModelName={llmEmbeddingModelName}
            setLlmEmbeddingModelName={setLlmEmbeddingModelName}
            llmApiKey={llmApiKey}
            setLlmApiKey={setLlmApiKey}
            saveRuntimeLlmSettings={saveRuntimeLlmSettings}
            disableRuntimeLlm={disableRuntimeLlm}
            onBack={() => openView("dashboard")}
          />
        )}
      </section>
    </main>
  );
}

const CENTRAL_INTENT_OPTIONS = [
  { value: "comment", label: "补充说明" },
  { value: "request_revision", label: "要求修改当前步骤" },
  { value: "request_reroute", label: "请求重新规划" },
  { value: "approve_risk", label: "确认风险继续" },
  { value: "ask_question", label: "提问" }
];

const CENTRAL_INTENT_LABELS = Object.fromEntries(CENTRAL_INTENT_OPTIONS.map((item) => [item.value, item.label]));

function CozeWorkflowPanel({
  task,
  metadata,
  dynamicPlan,
  steps,
  currentStep,
  selectedNodeId,
  onSelectNode
}) {
  const workflowNodes = useMemo(() => {
    const startNode = {
      id: "start",
      type: "start",
      title: "开始",
      subtitle: metadata.task_title || task?.task_id || "未选择任务",
      status: task?.state || "created",
      icon: Play,
      description: "任务入口：创建目标、模式、输入和初始上下文。"
    };
    const centralNode = {
      id: "central",
      type: "central",
      title: "中枢 Agent",
      subtitle: `计划 v${dynamicPlan.plan_version || 1}`,
      status: "active",
      icon: Bot,
      description: "负责生成动态计划、分派 task/review subagent、收集结果并决定下一步。"
    };
    const outputNode = {
      id: "output",
      type: "output",
      title: "输出",
      subtitle: metadata.cst_status || task?.state || "not_reached",
      status: task?.state || "pending",
      icon: FileText,
      description: "最终报告、artifact、日志和审查结论。"
    };
    const stepNodes = asArray(steps).map((step, index) => ({
      id: `step:${step.step_id}`,
      type: "step",
      title: cn(step.step_id, STEP_LABELS, step.step_id || `步骤 ${index + 1}`),
      subtitle: cn(step.step_goal, STEP_GOAL_LABELS, step.step_goal || "等待中枢定义目标"),
      status: cozeStatus(step, currentStep?.step_id),
      icon: GitBranch,
      step,
      lane: index + 1,
      metrics: {
        agents: stepAgentCount(step),
        skills: asArray(step.callable_skills).length,
        candidates: asArray(step.candidate_skills).length
      }
    }));
    return [startNode, centralNode, ...stepNodes, outputNode];
  }, [dynamicPlan.plan_version, metadata.cst_status, metadata.task_title, steps, currentStep?.step_id, task?.state, task?.task_id]);

  const selectedNode = workflowNodes.find((node) => node.id === selectedNodeId) || workflowNodes.find((node) => node.status === "active") || workflowNodes[1];
  const callableSkills = asArray(selectedNode?.step?.callable_skills);
  const candidateSkills = asArray(selectedNode?.step?.candidate_skills);
  const statusCounts = workflowNodes.reduce((acc, node) => {
    acc[node.status] = (acc[node.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <section className="panel cozeWorkflowPanel">
      <div className="cozeWorkflowHeader">
        <div>
          <h2><Workflow size={18} /> 可视自动工作流</h2>
          <p>Coze 风格画布：展示中枢、动态步骤、执行员、审查员、技能调用和输出流向。</p>
        </div>
        <div className="cozeWorkflowStats">
          <span>步骤 {asArray(steps).length}</span>
          <span>运行 {statusCounts.active || 0}</span>
          <span>完成 {statusCounts.done || 0}</span>
        </div>
      </div>

      <div className="cozeWorkflowBody">
        <div className="cozeCanvas" role="list" aria-label="Coze style visual workflow">
          <div className="cozeRailLine" />
          {workflowNodes.map((node, index) => {
            const Icon = node.icon || Network;
            const isSelected = selectedNode?.id === node.id;
            return (
              <button
                type="button"
                role="listitem"
                key={node.id}
                className={`cozeNode ${node.type} ${node.status} ${isSelected ? "selected" : ""}`}
                onClick={() => onSelectNode(node.id)}
              >
                {index > 0 && <span className="cozeConnector" />}
                <span className="cozeNodeIcon"><Icon size={17} /></span>
                <span className="cozeNodeText">
                  <strong>{node.title}</strong>
                  <small>{node.subtitle}</small>
                </span>
                <span className="cozeNodeStatus">{cn(node.status, REVIEW_STATUS_LABELS, node.status)}</span>
                {node.type === "step" && (
                  <span className="cozeNodeMeta">
                    <em>{node.metrics.agents} agent</em>
                    <em>{node.metrics.skills} skill</em>
                    <em>{node.metrics.candidates} 候选</em>
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <aside className="cozeInspector">
          <div className="cozeInspectorTitle">
            <span>{selectedNode?.type || "node"}</span>
            <strong>{selectedNode?.title || "未选择节点"}</strong>
          </div>
          <p>{workflowDetailText(selectedNode)}</p>

          {selectedNode?.type === "step" && (
            <>
              <div className="cozeInspectorGrid">
                <div>
                  <small>Task subagent</small>
                  <strong>{cn(selectedNode.step.task_agent, AGENT_LABELS, selectedNode.step.task_agent)}</strong>
                </div>
                <div>
                  <small>Review subagent</small>
                  <strong>{cn(selectedNode.step.module_review_agent || selectedNode.step.review_agent, AGENT_LABELS, selectedNode.step.module_review_agent || selectedNode.step.review_agent)}</strong>
                </div>
                <div>
                  <small>主技能</small>
                  <strong>{stepPrimarySkill(selectedNode.step)}</strong>
                </div>
                <div>
                  <small>状态</small>
                  <strong>{cn(selectedNode.step.status, REVIEW_STATUS_LABELS, selectedNode.step.status)}</strong>
                </div>
              </div>
              <div className="cozeSkillPills">
                {callableSkills.map((skill, index) => <span className="callable" key={`callable-${index}`}>{skillDisplayName(skill)}</span>)}
                {candidateSkills.map((skill, index) => <span className="candidate" key={`candidate-${index}`}>{skillDisplayName(skill)} 候选</span>)}
                {!callableSkills.length && !candidateSkills.length && <span>无技能调用</span>}
              </div>
            </>
          )}

          <div className="cozeMiniMap">
            {workflowNodes.map((node) => (
              <button
                type="button"
                key={`mini-${node.id}`}
                className={`cozeMiniDot ${node.status} ${selectedNode?.id === node.id ? "selected" : ""}`}
                title={node.title}
                onClick={() => onSelectNode(node.id)}
              />
            ))}
          </div>
        </aside>
      </div>
    </section>
  );
}

function CentralAgentMessagePanel({
  currentStep,
  intent,
  setIntent,
  message,
  setMessage,
  busy,
  onSend,
  messages,
  replies
}) {
  const recentMessages = asArray(messages).slice(-5).reverse();
  const recentReplies = asArray(replies).slice(-3).reverse();
  const stepName = cn(currentStep.step_id, STEP_LABELS, currentStep.step_id || "当前阶段");
  return (
    <section className="panel centralAgentPanel">
      <div className="sectionHead">
        <div>
          <h2><MessageSquare size={17} /> 与中枢 Agent 沟通</h2>
          <p>把你对“需要修改 / 需要重规划 / 证据不足”的判断发给中枢，作为下一轮裁决依据。</p>
        </div>
        <span>{stepName}</span>
      </div>
      <div className="centralMessageGrid">
        <label>
          <span>沟通意图</span>
          <select value={intent} onChange={(event) => setIntent(event.target.value)}>
            {CENTRAL_INTENT_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
        </label>
        <label className="wide">
          <span>发送给中枢的内容</span>
          <textarea
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="例如：这个步骤证据不足，请重新检索贴片天线 S11 / GWO 相关论文，再决定是否进入下一步。"
          />
        </label>
      </div>
      <div className="centralMessageActions">
        <button className="primary" disabled={busy || !message.trim()} onClick={onSend}>
          <Send size={17} /> {busy ? "发送中..." : "发送给中枢"}
        </button>
        <span>当前只记录并通知中枢，不会绕过审批或直接调用 CST。</span>
      </div>
      <div className="centralMessageHistory">
        <strong>最近沟通记录</strong>
        {!recentMessages.length && <p className="empty">暂无</p>}
        {recentMessages.map((item) => (
          <div className="centralMessageItem" key={item.message_id || `${item.intent}-${item.created_at}`}>
            <span>{cn(item.intent, CENTRAL_INTENT_LABELS, item.intent)} · {item.target_step_id || "未指定步骤"}</span>
            <p>{item.message}</p>
            <small>{item.created_at || "未记录时间"}</small>
          </div>
        ))}
        {recentReplies.length > 0 && <strong>中枢实时回复</strong>}
        {recentReplies.map((reply, index) => (
          <CentralReplyCard key={reply.reply_id || `${reply.action}-${reply.created_at}-${index}`} reply={reply} />
        ))}
      </div>
    </section>
  );
}

function SkillJudgementPanel({
  currentStep,
  dynamicPlan,
  skillRouteReview,
  stepSkillContext,
  callableSkills,
  candidateSkills,
  excludedSkills
}) {
  const stepName = cn(currentStep.step_id, STEP_LABELS, currentStep.step_id || "未选择步骤");
  const policy = stepSkillContext.confidence_policy || {};
  return (
    <section className="panel">
      <div className="sectionHead">
        <h2>当前阶段能力判断</h2>
        <span>计划 v{dynamicPlan.plan_version || stepSkillContext.plan_version || 1} · {cn(skillRouteReview.status || "not_reviewed", REVIEW_STATUS_LABELS)}</span>
      </div>
      <div className="routeContext">
        <strong>当前步骤：{stepName}</strong>
        <span>{cn(currentStep.step_goal, STEP_GOAL_LABELS, currentStep.step_goal || "等待中枢选择下一步")}</span>
        <small>
          阈值：{prettyPercent(policy.callable_min ?? 0.7)} 以上才会调用；
          {prettyPercent(policy.candidate_min ?? 0.45)}-{prettyPercent(policy.candidate_max ?? 0.699)} 只作为候选，明确未调用。
        </small>
        {stepSkillContext.score_formula && <small>置信度组成：{stepSkillContext.score_formula}</small>}
      </div>
      <div className="skillBuckets">
        <SkillBucket title="本步骤调用技能" tone="callable" items={callableSkills} empty="当前步骤没有达到调用门槛的技能" />
        <SkillBucket title="候选技能，未调用" tone="candidate" items={candidateSkills} empty="暂无候选技能" candidate />
        <SkillBucket title="未调用原因" tone="excluded" items={excludedSkills.slice(0, 5)} empty="暂无明确排除项" excluded />
      </div>
    </section>
  );
}

function SkillBucket({ title, tone, items, empty, candidate = false, excluded = false }) {
  return (
    <div className={`skillBucket ${tone}`}>
      <div className="skillBucketHead">
        <strong>{title}</strong>
        <span>{items.length}</span>
      </div>
      {!items.length && <p className="empty">{empty}</p>}
      {items.map((item, index) => (
        <SkillRouteCard
          key={item.route_id || item.owner_skill || item.skill_id || item.id || index}
          item={item}
          candidate={candidate}
          excluded={excluded}
        />
      ))}
    </div>
  );
}

function SkillRouteCard({ item, candidate, excluded }) {
  const action = cn(item.action, ROUTE_ACTION_LABELS, item.action || "未记录动作");
  const status = cn(item.status, ROUTE_STATUS_LABELS, item.status || "未记录状态");
  return (
    <div className={`skillRouteCard ${candidate ? "candidate" : ""} ${excluded ? "excluded" : ""}`}>
      <strong>{skillDisplayName(item)}</strong>
      <span>{skillConfidenceText(item)}</span>
      <small>动作：{action} · 状态：{status}</small>
      {candidate && <small className="notCalled">{skillNotCalledReason(item)}</small>}
      {excluded && <small className="notCalled">{skillNotCalledReason(item)}</small>}
      <div className="scoreChips">
        {scoreParts(item).map(([name, value]) => (
          <em key={name}>{name} {prettyPercent(value)}</em>
        ))}
      </div>
      {asArray(item.matched_terms).length > 0 && <small>命中词：{item.matched_terms.join("、")}</small>}
      {asArray(item.negative_matches).length > 0 && <small className="notCalled">负面命中：{item.negative_matches.join("、")}</small>}
    </div>
  );
}

function CentralReplyCard({ reply }) {
  return (
    <div className="centralReplyCard">
      <div className="centralReplyHead">
        <strong>{centralReplyStatus(reply)}</strong>
        <span>{reply.action || reply.intent || "reply"}</span>
      </div>
      <p>{reply.reply_text || reply.message || "中枢已回复，但没有返回文本。"}</p>
      <div className="centralReplyMeta">
        <span>计划修改：{centralReplyPlanChanged(reply) ? "是" : "否"}</span>
        <span>技能修改：{centralReplySkillChanged(reply) ? "是" : "否"}</span>
        <span>changed_steps：{centralReplyListText(centralReplyChangedSteps(reply))}</span>
        <span>changed_skills：{centralReplyListText(centralReplyChangedSkills(reply))}</span>
      </div>
    </div>
  );
}

function DynamicStepCard({ step, current, moduleReview, globalReview, centralDecision, centralReplies, draft, busy, onDraftChange, onSend }) {
  const callable = asArray(step.callable_skills);
  const candidate = asArray(step.candidate_skills);
  const stepDraft = { intent: "ask_question", message: "", open: false, ...(draft || {}) };
  const stepReplies = asArray(centralReplies).filter((reply) => centralReplyStepId(reply) === step.step_id);
  const openComposer = (intent) => onDraftChange(step.step_id, { intent, open: true });
  return (
    <article className={`dynamicStepCard ${current ? "current" : ""}`}>
      <div className="dynamicStepHead">
        <div>
          <strong>{cn(step.step_id, STEP_LABELS, step.step_id)}</strong>
          <p>{cn(step.step_goal, STEP_GOAL_LABELS, step.step_goal)}</p>
        </div>
        <span>{cn(step.status, REVIEW_STATUS_LABELS, step.status)}</span>
      </div>
      <div className="agentGrid">
        <AgentReviewLine label="执行角色" value={cn(step.task_agent, AGENT_LABELS, step.task_agent)} />
        <AgentReviewLine label="模块审查" value={cn(step.module_review_agent || step.review_agent, AGENT_LABELS, step.module_review_agent || step.review_agent)} decision={moduleReview.decision} />
        <AgentReviewLine
          label="全局审查"
          value={step.global_review_required ? "跨步骤边界启用" : "本步骤不需要"}
          decision={step.global_review_required ? globalReview.decision : undefined}
        />
        <AgentReviewLine label="中枢裁决" value={readableDecision(step.central_decision || centralDecision.decision)} />
      </div>
      <div className="stepSkillLine">
        <span>调用技能：{callable.length ? callable.map(skillDisplayName).join("、") : "无"}</span>
        <span>候选未调用：{candidate.length ? candidate.map(skillDisplayName).join("、") : "无"}</span>
      </div>
      <div className="stepCentralHub">
        <div className="stepCentralActions">
          <button type="button" onClick={() => openComposer("ask_question")}><MessageSquare size={15} /> 向中枢提问</button>
          <button type="button" onClick={() => openComposer("request_revision")}><RefreshCw size={15} /> 要求修改本步骤</button>
          <button type="button" onClick={() => openComposer("request_reroute")}><ListChecks size={15} /> 请求从这里重规划</button>
        </div>
        {stepDraft.open && (
          <div className="stepCentralComposer">
            <label>
              <span>发送给中枢</span>
              <textarea
                value={stepDraft.message}
                onChange={(event) => onDraftChange(step.step_id, { message: event.target.value })}
                placeholder="说明你的问题、修改要求或从此步骤重规划的原因"
              />
            </label>
            <div className="centralMessageActions">
              <span>target_step_id={step.step_id} · intent={stepDraft.intent}</span>
              <button className="primary" type="button" disabled={busy || !stepDraft.message.trim()} onClick={() => onSend(step.step_id)}>
                <Send size={15} /> {busy ? "发送中..." : "发送"}
              </button>
            </div>
          </div>
        )}
        <div className="stepCentralReplies">
          {!stepReplies.length && <p className="empty">暂无本步骤中枢回复</p>}
          {stepReplies.slice(-3).reverse().map((reply, index) => (
            <CentralReplyCard key={reply.reply_id || `${step.step_id}-${reply.action}-${index}`} reply={reply} />
          ))}
        </div>
      </div>
    </article>
  );
}

function AgentReviewLine({ label: name, value, decision }) {
  return (
    <div className="agentReviewLine">
      <small>{name}</small>
      <strong>{value}</strong>
      {decision && <span>{readableDecision(decision)}</span>}
    </div>
  );
}

function RouteHistoryPanel({ history }) {
  return (
    <section className="panel">
      <div className="sectionHead">
        <h2>历史 route 版本</h2>
        <span>{history.length}</span>
      </div>
      <div className="routeHistoryList">
        {history.slice(-6).map((item, index) => (
          <div className="routeHistoryItem" key={`${item.plan_version || index}-${item.reason || "route"}`}>
            <strong>v{item.plan_version || index + 1}</strong>
            <span>{item.reason || "route_update"}</span>
            <small>调用 {asArray(item.selected_owner_skills).length} · 候选 {asArray(item.candidate_owner_skills).length}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function Metric({ label: name, value, suffix, icon: Icon }) {
  return (
    <div className="metric">
      <span>{Icon ? <Icon size={15} /> : null}{name}</span>
      <strong>{value}{suffix && value !== "not_available" ? ` ${suffix}` : ""}</strong>
    </div>
  );
}

function List({ items, render }) {
  if (!items.length) return <p className="empty">暂无</p>;
  return (
    <ul className="denseList">
      {items.map((item, index) => <li key={item.id || item.path || item.markdown_path || index}>{render(item)}</li>)}
    </ul>
  );
}

function EvidencePage({ evidencePool, evidenceSources, onRerunReview, reviewBusy, hasTask, onBack }) {
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
        <EvidenceReview review={evidencePool.react_review || DEFAULT_EVIDENCE_POOL.react_review} />
        <div className="evidenceGrid evidenceGridTwo">
          <EvidenceSource
            name={EVIDENCE_SOURCE_LABELS.deep_read_papers}
            source={evidenceSources.deep_read_papers || evidenceSources.reports || DEFAULT_EVIDENCE_POOL.sources.deep_read_papers || { status: "missing", items: [] }}
            variant="primary"
          />
          <div className="innovationColumn">
            <EvidenceSource
              name={EVIDENCE_SOURCE_LABELS.graph_library}
              source={evidenceSources.graph_library || DEFAULT_EVIDENCE_POOL.sources.graph_library || { status: "missing", items: [] }}
            />
            {(evidenceSources.llm_semantic_expansion?.items || []).length > 0 && (
              <EvidenceSource
                name={EVIDENCE_SOURCE_LABELS.llm_semantic_expansion}
                source={evidenceSources.llm_semantic_expansion}
              />
            )}
          </div>
        </div>
      </section>
    </section>
  );
}

function ReportsPage({ reports, artifacts, task, metadata, onBack }) {
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

function LogsPage({ logs, onBack }) {
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

function SettingsPage({
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

createRoot(document.getElementById("root")).render(<App />);
