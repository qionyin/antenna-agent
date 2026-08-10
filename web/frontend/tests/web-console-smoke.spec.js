import { expect, test } from "@playwright/test";

const API_ROOT = "http://127.0.0.1:8000";
const mockState = { realCreatePosts: 0 };

async function mockApi(page) {
  let createdTask = null;
  let llmSettings = { enabled: false, base_url: "", model_name: "", api_key_configured: false };
  mockState.realCreatePosts = 0;
  await page.route(`${API_ROOT}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

    if (url.pathname === "/health") {
      return json({ status: "ok", redis: { backend: "mock" } });
    }
    if (url.pathname === "/tasks" && method === "GET") {
      return json(createdTask ? [createdTask] : []);
    }
    if (url.pathname === "/settings/llm-runtime" && method === "GET") {
      return json(llmSettings);
    }
    if (url.pathname === "/settings/llm-runtime" && method === "POST") {
      const body = JSON.parse(request.postData() || "{}");
      llmSettings = {
        enabled: Boolean(body.enabled),
        base_url: body.base_url || "",
        model_name: body.model_name || "",
        api_key_configured: Boolean(body.api_key || llmSettings.api_key_configured)
      };
      return json(llmSettings);
    }
    if (url.pathname === "/tasks/real-cst-single-run" && method === "POST") {
      mockState.realCreatePosts += 1;
      createdTask = {
        task_id: "task-v2",
        state: "waiting_approval",
        approvals: {
          "real_cst:task-v2": { approval_id: "real_cst:task-v2", status: "waiting_approval" }
        },
        task_metadata: {
          mode: "real",
          task_title: "真实 CST 单次运行 - 贴片天线 - S11 验证",
          current_stage: "waiting_approval",
          current_task_subagent: "preflight_task_agent",
          current_review_subagent: "preflight_review_agent",
          cst_status: "waiting_user_approval",
          evidence_pool_summary: {
            status: "available",
            read_only: true,
            support_level: "candidate_evidence",
            retrieval_agent: "evidence_retrieval_task_agent",
            review_agent: "evidence_relevance_review_agent",
            review_mode: "local_react_fallback",
            gate_summary: { total: 3, adopted: 1, blocked: 1, needs_more_evidence: 1 },
            accelerator_policy: {
              final_decider: "evidence_modeling_gate_agent and reviewer, not local matching or LLM alone."
            },
            react_review: {
              accepted: [{ display_title: "Patch Evidence", source: "reports", reason: "同时命中任务的天线类型、指标和必要算法。" }],
              rejected: [{ display_title: "Medical Noise", source: "reports", reason: "明显属于非目标天线任务或医学/成像噪声。" }],
              uncertain: [{ display_title: "Generic Antenna", source: "graph_library", reason: "只命中部分任务要素，需要人工或 LLM 进一步判断。" }],
              evidence_gaps: []
            },
            sources: {
              deep_read_papers: {
                status: "available",
                support_level: "candidate_evidence",
                roles: ["reproduction", "evidence"],
                count: 1,
                accepted_count: 1,
                rejected_count: 0,
                uncertain_count: 0,
                items: [{
                  title: "Patch Evidence",
                  path: "D:/paper/report.md",
                  level: "deep_reading_report",
                  status: "available",
                  trace_sources: [{ type: "vector_chunk", path: "embedding_fulltext_search_content:1" }],
                  react_review: { decision: "accept", reason: "同时命中任务的天线类型、指标和必要算法。" },
                  evidence_gate: { adoption_decision: "adopt", reason: "reports candidate can be adopted: paper-backed evidence and modeling-safety gates are satisfied.", blockers: [], warnings: [] }
                }]
              },
              graph_library: {
                status: "available",
                support_level: "candidate_relation_evidence",
                roles: ["innovation", "relation"],
                relation_count: 1,
                description: "PaperWise relation graph candidate pool for innovation, reviewed by LLM plus gate.",
                review_requirement: "llm_semantic_review_required_before_adoption",
                items: [{ title: "Patch-S11 relation", path: "D:/paper/graph/graph.json", description: "relation evidence for innovation candidate review", level: "relation_graph", status: "available" }]
              }
            }
          },
          preflight: {
            success: true,
            checks: [
              { name: "project_path", path: "D:/real/model.cst", ok: true, status: "pass", reason: "" },
              { name: "parameters", path: "", ok: true, status: "pass", reason: "", count: 1 }
            ]
          },
          results: {},
          dynamic_plan: {
            plan_version: 1,
            steps: [
              {
                step_id: "step_001_preflight",
                step_goal: "Check CST path, adapter, permissions, and output directory",
                task_agent: "preflight_task_agent",
                module_review_agent: "preflight_review_agent",
                status: "pending",
                callable_skills: ["cst-control"],
                candidate_skills: ["paperwise"]
              },
              {
                step_id: "step_002_approval",
                step_goal: "Wait for user approval before real CST solver",
                task_agent: "approval_task_agent",
                module_review_agent: "preflight_review_agent",
                status: "waiting_approval",
                callable_skills: [],
                candidate_skills: ["antenna-research-reviewer"]
              }
            ]
          },
          reviews: [{ agent: "preflight_review_agent", status: "pass", conclusion: "允许进入真实 CST 审批" }]
        }
      };
      return json(createdTask);
    }
    if (url.pathname === "/tasks/task-v2/state") {
      return json(createdTask);
    }
    if (url.pathname === "/tasks/task-v2/available-actions") {
      return json({ task_id: "task-v2", state: createdTask.state, actions: ["approve_real_cst", "reject", "view_logs"] });
    }
    if (url.pathname === "/tasks/task-v2/paperwise/review" && method === "POST") {
      createdTask = {
        ...createdTask,
        task_metadata: {
          ...createdTask.task_metadata,
          evidence_pool_summary: {
            ...createdTask.task_metadata.evidence_pool_summary,
            review_mode: "runtime_llm_react",
            llm_review: {
              attempted: true,
              success: true,
              error: null,
              model: "test-model"
            }
          }
        }
      };
      return json(createdTask);
    }
    if (url.pathname === "/tasks/task-v2/central-message" && method === "POST") {
      const body = JSON.parse(request.postData() || "{}");
      const intent = body.intent || "comment";
      const targetStepId = body.target_step_id || "waiting_approval";
      const nextPlanVersion = intent === "request_reroute"
        ? (createdTask.task_metadata.dynamic_plan?.plan_version || 1) + 1
        : createdTask.task_metadata.dynamic_plan?.plan_version || 1;
      const message = {
        schema_version: "1.0",
        message_id: `msg-${(createdTask.task_metadata.central_agent_messages || []).length + 1}`,
        task_id: "task-v2",
        sender: "user",
        target: "central_agent",
        intent,
        target_step_id: targetStepId,
        message: body.message || "",
        created_at: "2026-07-09T12:00:00+08:00",
        status: "received"
      };
      const reply = {
        schema_version: "1.0",
        reply_id: `reply-${(createdTask.task_metadata.central_agent_replies || []).length + 1}`,
        message_id: message.message_id,
        task_id: "task-v2",
        target_step_id: targetStepId,
        action: intent,
        reply_text: intent === "request_reroute"
          ? "中枢已从该步骤重新规划，并保留后续审批约束。"
          : intent === "request_revision"
            ? "中枢已记录本步骤修改请求，等待执行角色重做。"
            : "中枢仅回复：当前步骤可以先补充证据说明。",
        plan_changed: intent === "request_reroute",
        skill_changed: intent === "request_reroute",
        changed_steps: intent === "request_reroute" || intent === "request_revision" ? [targetStepId] : [],
        changed_skills: intent === "request_reroute" ? ["paperwise"] : [],
        plan_version: nextPlanVersion,
        created_at: "2026-07-09T12:00:01+08:00"
      };
      createdTask = {
        ...createdTask,
        task_metadata: {
          ...createdTask.task_metadata,
          dynamic_plan: {
            ...createdTask.task_metadata.dynamic_plan,
            plan_version: nextPlanVersion
          },
          central_agent_messages: [
            ...(createdTask.task_metadata.central_agent_messages || []),
            message
          ],
          last_user_central_message: message,
          central_agent_replies: [
            ...(createdTask.task_metadata.central_agent_replies || []),
            reply
          ],
          last_central_agent_reply: reply,
          central_decisions: [
            ...(createdTask.task_metadata.central_decisions || []),
            { step_id: message.target_step_id, decision: "user_message_received", intent: message.intent, message_id: message.message_id }
          ]
        }
      };
      return json(createdTask);
    }
    if (url.pathname === "/tasks/task-v2/artifacts") {
      return json([{ id: "a1", name: "preflight_result", path: "D:/mock/preflight_result.json" }]);
    }
    if (url.pathname === "/tasks/task-v2/logs") {
      return json([{ sequence_id: "00000001", node_id: "preflight_task_agent", status: "done", ui_safe_message: "preflight_task_agent done" }]);
    }
    if (url.pathname === "/tasks/task-v2/reports") {
      return json([]);
    }
    if (url.pathname === "/tasks/task-v2/approve" && method === "POST") {
      createdTask = {
        ...createdTask,
        state: "completed",
        task_metadata: {
          ...createdTask.task_metadata,
          current_stage: "completed",
          cst_status: "completed",
          current_task_subagent: "report_task_agent",
          current_review_subagent: "report_review_agent",
          results: {
            metrics: {
              s11_min_db: -23,
              s11_min_freq: 3.2,
              return_loss_max_db: 23,
              bandwidth_10db: 0.42
            }
          }
        }
      };
      return json(createdTask);
    }
    if (url.pathname === "/tasks" && method === "POST") {
      return json({ task_id: "mock-task", state: "completed", task_metadata: { mode: "mock", task_title: "模拟任务" } });
    }
    return json({ error: "unexpected_mock_route", path: url.pathname }, 404);
  });
}

async function expectNoHorizontalOverflow(page) {
  const overflow = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test("V2.2 dashboard creates a real CST task and shows approval controls", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "V2.2 真实 CST 单次闭环" })).toBeVisible();
  await expect(page.getByText("V2.2 已接入 PaperWise：精读报告 / 创新（只读）")).toBeVisible();
  await expect(page.getByLabel("中文任务标题")).toBeVisible();
  await expect(page.getByRole("button", { name: /创建任务/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "PaperWise" })).toBeVisible();
  await page.getByRole("button", { name: "设置" }).click();
  await expect(page.getByRole("heading", { name: "运行设置" })).toBeVisible();
  await page.getByLabel("Base URL").fill("https://example.test/v1");
  await page.getByLabel("Chat Model").fill("test-model");
  await page.getByLabel("Embedding Model").fill("text-embedding-v3");
  await page.getByLabel("API Key").fill("secret-key");
  await page.getByRole("button", { name: /保存并启用/ }).click();
  await expect(page.getByText("enabled")).toBeVisible();
  await page.getByRole("button", { name: /返回总览/ }).click();
  await expectNoHorizontalOverflow(page);

  await page.getByLabel("CST 项目路径").fill("D:\\real\\model.cst");
  await page.getByRole("button", { name: /创建任务/ }).click();
  await expect(page.getByText("等待审批").first()).toBeVisible();
  await expect(page.getByText("环境预检执行角色").first()).toBeVisible();
  await expect(page.getByText("Preflight checklist")).toBeVisible();
  await expect(page.getByRole("button", { name: /批准真实 CST solver/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /拒绝真实 CST solver/ })).toBeVisible();
  await expect(page.locator(".centralAgentPanel")).toBeVisible();
  await page.locator(".centralAgentPanel textarea").fill("请重新规划当前步骤，先补贴片天线 S11 证据。");
  await page.locator(".centralAgentPanel button").click();
  await expect(page.locator(".dynamicStepCard")).toHaveCount(2);
  const firstStepCard = page.locator(".dynamicStepCard").first();
  await firstStepCard.locator(".stepCentralActions button").nth(0).click();
  await firstStepCard.locator(".stepCentralComposer textarea").fill("step question");
  await firstStepCard.locator(".stepCentralComposer button.primary").click();
  await expect(firstStepCard.getByText("仅回复，未修改计划")).toBeVisible();
  await expect(firstStepCard.getByText(/changed_steps/).first()).toBeVisible();
  await expect(firstStepCard.getByText(/changed_skills/).first()).toBeVisible();
  await firstStepCard.locator(".stepCentralActions button").nth(2).click();
  await firstStepCard.locator(".stepCentralComposer textarea").fill("reroute from this step with PaperWise evidence");
  await firstStepCard.locator(".stepCentralComposer button.primary").click();
  await expect(firstStepCard.getByText("已生成新计划 v2")).toBeVisible();
  await expect(firstStepCard.getByText(/paperwise/)).toBeVisible();
  await expect(page.getByText("请重新规划当前步骤")).toBeVisible();

  await page.getByRole("button", { name: "PaperWise" }).click();
  await expect(page.getByRole("heading", { name: "PaperWise 证据审查" })).toBeVisible();
  await expect(page.getByText("证据池摘要")).toBeVisible();
  await expect(page.getByText("PaperWise 已只读接入：精读报告 + 创新")).toBeVisible();
  await expect(page.getByText(/审查模式：local_react_fallback/)).toBeVisible();
  await expect(page.getByRole("button", { name: /重新用 LLM 审查/ })).toBeVisible();
  await page.getByRole("button", { name: /重新用 LLM 审查/ }).click();
  await expect(page.getByText(/审查模式：runtime_llm_react/)).toBeVisible();
  await expect(page.getByText(/LLM attempted: true/)).toBeVisible();
  await expect(page.getByText("已接受证据")).toBeVisible();
  await expect(page.getByText("已拒绝证据")).toBeVisible();
  await expect(page.getByText("不确定证据")).toBeVisible();
  await expect(page.getByText("精读报告").first()).toBeVisible();
  await expect(page.getByText("创新").first()).toBeVisible();
  await expect(page.getByText("reproduction", { exact: true })).toBeVisible();
  await expect(page.getByText("innovation", { exact: true })).toBeVisible();
  await expect(page.getByText(/来源追溯：vector_chunk/)).toBeVisible();
  await expect(page.getByText("relation evidence for innovation candidate review")).toBeVisible();
  await expect(page.getByText(/Gate adopt:/)).toBeVisible();

  await page.getByRole("button", { name: /报告/ }).first().click();
  await expect(page.getByRole("heading", { name: "任务报告" })).toBeVisible();
  await expect(page.getByText("任务报告")).toBeVisible();

  await page.getByRole("button", { name: /返回总览/ }).click();
  await page.getByRole("button", { name: /批准真实 CST solver/ }).click();
  await expect(page.getByText("-23.000 dB")).toBeVisible();
  await expect(page.getByText("0.420 GHz")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("real CST create blocks missing, placeholder, and bad extensions before POST", async ({ page }) => {
  await page.goto("/");

  const createButton = page.getByRole("button", { name: /创建任务|鍒涘缓浠诲姟/ });
  const inputs = page.locator("input");
  const projectInput = inputs.nth(2);
  const modelJsonInput = inputs.nth(3);
  const errorText = page.locator(".errorText");

  await createButton.click();
  await expect(errorText).toBeVisible();
  expect(mockState.realCreatePosts).toBe(0);

  await projectInput.fill("D:\\\\path\\\\model.cst");
  await createButton.click();
  await expect(errorText).toBeVisible();
  expect(mockState.realCreatePosts).toBe(0);

  await projectInput.fill("D:\\\\real\\\\model.txt");
  await createButton.click();
  await expect(errorText).toBeVisible();
  expect(mockState.realCreatePosts).toBe(0);

  await projectInput.fill("");
  await modelJsonInput.fill("D:\\\\real\\\\model.txt");
  await createButton.click();
  await expect(errorText).toBeVisible();
  expect(mockState.realCreatePosts).toBe(0);
});
