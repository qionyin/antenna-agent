# V2.0 最小真实闭环计划

V2.0 只做一件事：把真实 CST 单次运行接进现有系统，并能在前端看懂。

## 目标

```text
真实 CST 单次运行 -> 结果解析 -> 审查 -> 报告 -> 前端展示
```

保留 V1 mock / packet 链路。V2.0 real 模式不走 V1 的 mock packet stages。

## 状态机

V2.0 真实链只使用 6 个状态：

```text
created
preflight_running
waiting_approval
running
failed
completed
```

合法转移：

```text
created -> preflight_running
preflight_running -> waiting_approval
preflight_running -> failed
waiting_approval -> running
waiting_approval -> failed
running -> completed
running -> failed
```

## Agent 结构

中枢 agent 只负责任务调度、状态更新、审计记录和下一步判断。

每个模块都有一个执行 subagent 和一个审查 subagent：

```text
preflight.task_agent          -> preflight_review_agent
cst_single_run.task_agent     -> cst_run_review_agent
result_parse.task_agent       -> result_parse_review_agent
report.task_agent             -> report_review_agent
```

这些 subagent 以结构化节点写入 blackboard，前端直接读取。

## 真实 CST 边界

真实执行通过本项目适配层调用外部能力：

```text
adapters/cst_real_run_adapter.py
```

它只包装调用，不修改：

```text
C:\Users\30626\.codex\skills\Antenna Skills
E:\antenna skills
```

真实产物默认写入：

```text
E:\antenna skills-resault\runs
```

如果没有 `project_path` / `model_json_path` 或真实参数，preflight 会失败，不伪造真实结果。

## 前端必须显示

```text
中文任务标题
模拟 / 真实模式
6 态状态
当前阶段
当前 task subagent
当前 review subagent
CST 状态
实时日志
artifact 列表
S11 / return loss / bandwidth
审查结论
报告入口
批准按钮
```

## API

```text
POST /tasks/real-cst-single-run
POST /tasks/{task_id}/approve
GET  /tasks/{task_id}/available-actions
GET  /tasks/{task_id}/artifacts
GET  /tasks/{task_id}/logs
GET  /tasks/{task_id}/reports
```

旧 V1 API 保留。
