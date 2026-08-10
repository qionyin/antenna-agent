# V2 多版本实现路线图

本文把完整 V2 蓝图拆成多个可落地版本。

核心原则：

- 每个版本至少实现一个大功能。
- 每个版本都能单独验收。
- 不把 V2 一次性做成大系统。
- 论文复现和创新性评估是主线，不放到最后当附属功能。
- PaperWise 是论文复现和创新判断的关键证据底座。
- PaperWise 只读。
- `C:\Users\30626\.codex\skills\Antenna Skills` 和 `E:\antenna skills`
  均不直接修改，只做适配层。

## V2.0 已完成：真实 CST 单次闭环

大功能：

```text
真实 CST 单次运行 -> 结果解析 -> 审查 -> 报告 -> 前端展示
```

范围：

- 新增真实模式 `real`。
- 保留 V1 mock 模式。
- CST solver 前必须等待用户批准。
- 产物登记到 task artifact。
- 生成 completed_report / failed_report。
- 前端显示中文任务标题、状态、subagent、日志、artifact、结果和报告。

验收标准：

- 能创建真实 CST 单次任务。
- preflight 失败能生成失败报告。
- 审批后能进入真实 CST adapter。
- 能解析或明确失败 S11 / return loss / bandwidth。
- 前端能看懂任务当前在做什么。

## V2.1 已完成：真实任务输入与预检增强

大功能：

```text
把真实 CST 任务创建前的输入校验、环境预检、前端提示做清楚
```

要解决的问题：

- 当前前端 placeholder 容易误导用户，以为示例路径是真实路径。
- 真实 CST 缺少 `.cst` / `model_json_path` 时，应该在前端提前提示。
- preflight 结果需要更清楚展示：哪个路径缺失、哪个能力不可用、下一步该补什么。

范围：

- 前端创建前校验 `.cst` / `model_json_path` / 参数 JSON。
- 后端返回更结构化的 preflight blockers。
- 前端显示 preflight checklist。
- 报告里写清楚失败发生在 preflight。

验收标准：

- 用户填错路径时，前端直接提示。
- 用户不用打开 JSON，也能看懂失败原因。
- preflight checklist 能显示 CST、E 平台、输出目录、参数是否通过。

实现记录：

- 前端真实模式创建前拦截缺少 `.cst` / `model_json_path` 的请求。
- `D:\path\model.cst` 等示例 placeholder 不再被当成真实输入。
- 后端 preflight 保留旧字段，同时新增结构化 `checks` / `blocker_details` / `structured_blockers`。
- failed_report 明确写出 `preflight failed`、`CST: not_reached`、`Result: not_available` 和补充真实输入建议。

## V2.2 已完成：PaperWise 只读证据接入

大功能：

```text
把 PaperWise 报告、向量库、图谱库作为只读证据来源接入任务。
向量库是精读论文内容库，可用于论文复现和证据支撑。
图谱库是论文/概念/结构/指标关系库，可用于创新判断和关系证据。
```

要解决的问题：

- PaperWise 是论文复现和创新判断的证据底座。
- 系统不能修改 PaperWise。
- 证据要能被报告、审查 agent、论文复现链引用。

范围：

- 新增 PaperWise read-only adapter 能力快照。
- 从 PaperWise 报告、向量库、图谱库读取候选证据。
- 生成 `evidence_pool_summary`。
- 前端显示证据来源、论文标题、证据等级、引用路径。
- 报告标注哪些结论有 PaperWise 支撑。

验收标准：

- 能从 PaperWise 找到与任务目标相关的论文证据。
- 证据只读，不写回 PaperWise。
- 报告中能看到证据引用来源。
- 没有证据时能明确提示“证据不足”，而不是静默跳过。

## V2.3：论文复现任务链

大功能：

```text
从一篇论文或 PaperWise 报告启动论文复现任务
```

要解决的问题：

- 当前系统能跑 CST，但还不能把“我要复现这篇论文”变成明确任务链。
- 论文复现需要先确定论文目标、结构、参数、指标和可复现边界。
- 复现任务必须有论文证据支撑，不能只靠用户一句话。

范围：

- 新增任务类型：`paper_reproduction`。
- 输入可以是：

```text
PaperWise report path
论文标题
论文 DOI / 本地编号
用户手动指定论文材料
```

- 生成 `reproduction_goal.json`：

```text
论文目标
天线类型
目标指标
目标频段
论文原始结果
需要复现的图/表/曲线
复现边界
缺失材料
```

- 中枢根据 PaperWise 证据决定：

```text
证据足够 -> 进入结构提取
证据不足 -> 要求补材料
```

验收标准：

- 用户选择一篇 PaperWise 报告后，能创建论文复现任务。
- 前端显示“复现目标”，而不是只显示任务编号。
- 报告能列出论文原始指标和待复现指标。
- 缺论文结构图、参数表或目标曲线时，系统明确阻塞并说明缺什么。

## V2.4：Antenna Skills 结构提取链接入

大功能：

```text
调用 Antenna Skills 的结构文本、参数、几何草图、几何校验流程
```

要解决的问题：

- V1 里结构提取只是轻量占位。
- 真正专业流程已经在 Antenna Skills 里。
- 本项目应该调它，而不是重复造简化版。

范围：

- 适配 `antenna-research-ideation`。
- 调用或包装以下产物链：

```text
evidence_ledger.json
parameter_resolution.json
geometry_sketch.json
geometry_sketch_validation.json
cst_spec 或建模前置产物
```

- 本项目只做统一接口和产物登记。
- 不修改原 skill。

验收标准：

- 能从论文或任务输入生成 geometry_sketch。
- 能看到参数到结构对象的映射。
- geometry validation 不通过时阻塞 CST。
- 前端能显示“结构证据是否足够”。

## V2.5：CST 建模与导出增强

大功能：

```text
把 CST 建模、solver、结果导出做成更完整的真实单次 CST 阶段
```

要解决的问题：

- V2.0 能跑 solver，但结果导出依赖外部已有文件或 E 平台输出。
- 论文复现需要稳定导出 S11 / gain / efficiency / axial ratio 等结果。
- CST 结果必须可追溯到同一次建模和同一次仿真。

范围：

- 增强 CST adapter 对 `cst-control` 和 `E:\antenna skills` 的包装。
- 明确导出文件要求。
- 支持 S11 必选，gain / efficiency / axial ratio 可选。
- 检查 CST 日志、run manifest、导出文件。
- 结果合理性检查更完整。

验收标准：

- 单次真实 CST 能生成 run manifest。
- S11 导出文件路径可追踪。
- 缺失导出时任务 failed，并写明缺什么。
- CST 成功但结果全 0 / 常数 / NaN 时被审查拦住。

## V2.6：论文复现结果审查

大功能：

```text
把 CST 结果与论文原始结果进行复现对照审查
```

要解决的问题：

- 跑出 CST 曲线不等于复现成功。
- 需要把 CST 结果和论文里的目标频段、S11 最小值、带宽、增益等对比。
- 偏差必须透明写入报告。

范围：

- 新增 `reproduction_review_agent`。
- 输入：

```text
reproduction_goal.json
PaperWise 证据
CST parsed_results.json
artifact manifest
```

- 输出 `reproduction_review.json`：

```text
reproduced
reproduced_with_deviation
reproduction_failed
insufficient_evidence
```

- 默认偏差阈值先使用可配置值，例如 10%。
- 所有偏差写进报告。

验收标准：

- 能判断“是否复现成功”。
- 能列出与论文指标的偏差。
- 偏差超过阈值时不能写成成功。
- 缺论文原始指标时标记 `insufficient_evidence`。

## V2.7：创新性评估与新设计任务链

大功能：

```text
基于 PaperWise 证据池和当前设计结果，判断新设计是否有结构或指标创新
```

要解决的问题：

- 用户最终关心的不只是复现，还包括新天线设计是否有创新。
- 创新判断不能靠 Agent 自说自话，必须引用 PaperWise 论文证据。
- 需要区分“应用创新”“参数变化”“结构创新”“指标提升”。

范围：

- 新增任务类型：`novel_design_assessment`。
- 从 PaperWise 找相似结构、相似频段、相似指标。
- 对比：

```text
结构差异
参数差异
指标差异
工程约束差异
证据充分性
```

- 输出 `innovation_assessment.json`：

```text
not_novel
parameter_variant
structure_variant
potentially_novel
insufficient_evidence
```

- 应用创新默认不作为主要创新点。

验收标准：

- 每条创新判断必须带 PaperWise 证据引用。
- 没有足够证据时标记证据不足。
- 报告中能区分“结构创新”和“只是参数变化”。
- 前端能显示创新性等级和证据链。

## V2.8：审查 Agent 与阻塞规则增强

大功能：

```text
把“证据够不够、谁该补材料、下一步该不该阻塞”交给审查 Agent
```

要解决的问题：

- V2.0 只有最小审查。
- 论文复现和创新判断需要更严格的 block / pass / warning。
- 审查结果要能驱动中枢调度。

范围：

- 引入统一 review schema。
- 每个模块 task_agent 后必须接 review_agent。
- 审查结论分为：

```text
pass
warning
block
```

- block 必须包含：

```text
原因
影响范围
需要谁补材料
是否允许人工强制继续
```

验收标准：

- 前端能看到每个阶段审查结论。
- block 后中枢不继续下游阶段。
- 报告能列出所有 blocker。
- 用户能看懂下一步该补什么。

## V2.9：单轮优化闭环

大功能：

```text
实现一次参数建议 -> CST 运行 -> 结果评价 -> 下一组参数建议
```

要解决的问题：

- V2.0 只跑单次 CST。
- 后续需要开始真实“能改参数、看结果、再建议”的工作闭环。
- 优化建议需要受论文复现目标或创新目标约束。

范围：

- 接入 `E:\antenna skills` 的优化 skill。
- 第一版只做单轮，不做长时间多轮优化。
- 中枢决定是否调用优化建议。
- 结果仍必须经过审查。

验收标准：

- 系统能基于当前结果生成下一组参数建议。
- 参数建议能说明依据。
- 不自动无限循环。
- 报告中能看到“本轮结果”和“下一轮建议”。

## V2.10：任务队列与 checkpoint

大功能：

```text
支持可恢复的长任务队列和 checkpoint
```

要解决的问题：

- CST 真实运行可能很长。
- 中断后需要知道从哪里恢复。
- 多任务不能互相踩 CST license。

范围：

- 增加最小任务队列。
- CST 阶段并发限制为 1。
- checkpoint 记录：

```text
当前阶段
run_dir
artifact refs
审批状态
最后成功节点
```

- 恢复时不重复生成已有报告和 artifact。

验收标准：

- 服务重启后能恢复 waiting_approval / running 前状态。
- 当前 CST 任务未结束时，后续 CST 任务排队。
- checkpoint 损坏时给出明确错误。

## 推荐开发顺序

```text
V2.1 -> V2.2 -> V2.3 -> V2.4 -> V2.5
-> V2.6 -> V2.7 -> V2.8 -> V2.9 -> V2.10
```

原因：

- 先让真实任务输入和失败原因变清楚。
- 再接 PaperWise 证据底座。
- 然后把论文复现变成主线任务。
- 再接 Antenna Skills 结构链和 CST 导出。
- 之后做复现审查和创新性评估。
- 最后再做优化、队列和 checkpoint。
