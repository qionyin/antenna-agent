# V2 完整蓝图

多版本拆分路线图见：

```text
D:\pythoncode\antenna_agent_lab\docs\v2_version_roadmap.md
```

本文件保存 V2 总蓝图，暂不在 V2.0 全量实现。

## 总方向

完整 V2 目标是让系统从“能展示流程”升级为“能真实组织天线研究工作”。

核心能力包括：

```text
PaperWise 只读证据
Antenna Skills 结构提取和审查
E:\antenna skills 真实 CST / 优化 / 后处理
中枢 agent 调度
模块 task subagent
模块 review subagent
前端可解释任务视图
```

## 完整 V2 模块

```text
preflight_snapshot
scheduler_plan_v2
evidence_pipeline
geometry_contract
cst_build_or_run
result_parse
result_review
claim_assessment
report
frontend_visibility
```

## 完整 V2 暂缓内容

V2.0 不实现以下内容：

```text
自动修复
完整优化闭环
多任务并发队列
联网查论文
完整创新性评估
复杂状态机
完整 DAG 编译器
多轮证据补查
```

这些内容以后按 V2.1 / V2.2 拆分实现，避免 V2.0 变成大系统。

## V2.0 和完整 V2 的关系

V2.0 是完整 V2 的最小真实闭环：

```text
真实 CST 单次运行 -> 结果解析 -> 审查 -> 报告 -> 前端展示
```

它验证真实运行、审批、安全边界、产物登记和前端可见性。
