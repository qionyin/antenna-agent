# V2.2.4 - 真实多 Agent 与动态 Skill 路由

## 版本目标

V2.2.4 解决 V2.2.3 暴露出的两个核心问题：

```text
1. dynamic_plan 已经存在，但执行时仍容易被理解成 task/review 标签。
2. skill_route_plan 在任务创建时生成后，容易被误解为一次定死。
```

本版把流程明确为：

```text
中枢 agent
-> 每个 step 前重新评估 skill
-> task subagent 执行
-> module review subagent 审当前产物
-> global review agent 审全局证据链和依赖
-> 中枢 agent 收集结果并裁决下一步
```

## 相对 V2.2.3 的变化

| 项目 | V2.2.3 | V2.2.4 |
|---|---|---|
| 多 agent 形态 | dynamic step 上有 task/review 字段 | 每步记录 task、module_review、global_review 三类 subagent 结果 |
| 审查职责 | global_review_agent 容易承担所有审查 | module review 审当前步骤，global review 审全局一致性 |
| skill 选择 | 初始 route 容易被当成固定结论 | 每个 step 前生成 step_skill_context |
| skill 置信度 | 45% 以上可能看起来像被调用 | 70% 以上 callable，45%-70% candidate only |
| gate token | adapter gate 已存在 | 测试锁定 task_id / plan_version / step_id / packet_type 绑定 |
| 前端/API 可见性 | 能看到动态计划和 reflection | 需要看到当前调用技能、候选技能、未调用原因和置信度组成 |

## 数据对比

V2.2.3 的真实 API 边界测试基线为：

```text
样本数: 100
trigger_ok: 84 / 100
trigger_rate: 84%
primary_ok: 76 / 100
primary_rate: 76%
reflection_ok: 99 / 100
reflection_rate: 99%
real_no_cst_ok: 100 / 100
failure_count: 30
duration: 161.36s
```

V2.2.4 没有把目标放在重新刷 100 条路由准确率上。
它解决的是 V2.2.3 已经暴露出的结构问题：

```text
多 Agent 只是字段标签
global review 可能覆盖 module review
skill_route_plan 容易一次定死
45%-70% 置信度容易被误解成已调用
gate token 没有被测试锁定 step 级过期语义
```

因此本版的数据重点是工程闭环指标。

| 指标 | V2.2.3 | V2.2.4 |
|---|---:|---:|
| dynamic step 审查层级 | 1 层 global review | 2 层 module + global |
| 每步 subagent 记录类型 | 约 1 类 review 记录 | 3 类 task / module_review / global_review |
| 中枢裁决记录 | 无独立 `central_decision` | 有 |
| 每步 skill 重算 | 无稳定字段 | `step_skill_context` |
| skill route 历史 | 初始 route 为主 | initial / active / history |
| 45%-70% skill | 可能误解成可调用 | candidate only |
| gate token stale 测试 | 未锁定 | 已锁定 plan_version / step_id |
| V2.2.4 专项后端测试 | 0 | 6 |
| V2.2.4 API 可见性测试 | 0 | 1 |

回归测试数量对比：

```text
上一条完整回归基线:
tests.test_runtime_core: 58 passed
tests.test_web_api: 18 passed
tests.test_langgraph_runtime: 4 passed
frontend smoke: 4 passed

V2.2.4:
tests.test_runtime_core: 66 passed
tests.test_web_api: 19 passed
tests.test_langgraph_runtime: 4 passed
frontend build: passed
frontend smoke: 4 passed
```

增长量：

```text
runtime_core: 58 -> 66, +8
web_api: 18 -> 19, +1
langgraph_runtime: 0, 保持通过
frontend smoke: 0, 保持通过并更新 2.2.4 文案断言
```

## 动态 Skill 路由

任务创建时仍会生成：

```text
initial_skill_route_plan
```

但它只代表任务入口的初始判断。

执行过程中，中枢会在每个动态步骤前重新生成：

```text
step_skill_context
```

并更新：

```text
active_skill_route_plan
skill_route_plan_history
```

因此同一个 skill 可以在不同步骤中发生变化：

```text
step_1: candidate_skills
step_3: callable_skills
step_5: excluded_skills
```

这不是矛盾，而是因为证据、artifact、review feedback 和当前 step goal 已经变化。

## 置信度门槛

V2.2.4 明确采用三档：

```text
score >= 0.70        -> callable_skills，可调用
0.45 <= score < 0.70 -> candidate_skills，只展示，不调用
score < 0.45         -> excluded_skills
```

48% 这类结果不再被解释为“调用技能适配器”，而是：

```text
候选技能，当前阶段证据不足，未调用。
```

置信度组成来自：

```text
base_domain_score
task_type_score
artifact_score
evidence_score
review_feedback_score
context_score
negative_score
```

## 两级审查边界

module review agent 只审当前步骤：

```text
输出是否存在
artifact 是否完整
字段是否合格
结果是否可解析
当前输出是否满足 step_goal
```

global review agent 审跨步骤问题：

```text
证据链是否连贯
上游输入是否支撑下游结论
是否缺 skill
是否误用 skill
是否需要 refresh_skill_route / reroute
是否存在弱证据强结论
```

中枢 agent 只做裁决：

```text
pass_next_step
revise_current_step
refresh_skill_route
reroute_plan
block_task
wait_user
complete_task
```

如果 module review 返回 block，中枢不能直接 pass。

## Gate Token 绑定

skill adapter 调用必须通过 gate token。

gate token 绑定：

```text
task_id
plan_version
step_id
owner_skill
packet_type
action
```

如果 plan_version 或 step_id 变化，旧 token 必须失效。

这保证了：

```text
上一个阶段允许的 skill，不能自动沿用到下一个阶段。
```

## 测试覆盖

新增测试覆盖：

```text
test_v224_dynamic_routes_have_initial_active_history_and_step_contexts
test_v224_dynamic_steps_record_task_module_global_subagents
test_v224_candidate_48_percent_skill_is_not_callable_and_does_not_open_gate
test_v224_gate_token_expires_on_plan_version_or_step_change
test_v224_module_review_block_cannot_be_passed_by_central_decision
test_v224_task_state_exposes_callable_candidate_and_step_contexts
```

这些测试锁定：

```text
initial / active / history 路由存在
每个 step 都有 step_skill_context
45%-70% skill 只能 candidate，不调用 adapter
gate token 会因 plan_version / step_id 变化失效
每个 dynamic step 有 task / module_review / global_review 三类记录
module block 不能被中枢强行 pass
API 可以读到 callable_skills / candidate_skills
```

## 边界

V2.2.4 本次测试文档子任务没有修改：

```text
agent_runtime 后端实现
web/frontend 前端实现
C:\Users\30626\.codex\skills
E:\antenna skills
```

真实 CST solver 仍不在本版测试中调用。
