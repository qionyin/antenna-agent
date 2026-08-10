# V2.2.2 更新报告：Skill 系统渐进披露与执行门

## 版本定位

V2.2.2 的核心目标不是新增一个大功能页面，而是修正系统底层调度逻辑：

```text
以前：中枢凭硬编码和旧 packet chain 直接调用 skill adapter。
现在：中枢先生成 skill_route_plan，再由 gate 决定是否允许旧 adapter 执行。
```

这版解决了一个关键问题：

```text
系统不能再“看起来接了 skill”，但实际不知道为什么调用、该不该调用、有没有误调用。
```

原始 skill 目录未修改：

```text
C:\Users\30626\.codex\skills
E:\antenna skills
```

## 数据提升总览

V2.2.2 相比 V2.2.1，最明显的提升是 skill 路由从“不可量化”变成“可测试、可审计、可阻断”。

| 指标 | V2.2.1 / 旧逻辑 | V2.2.2 |
|---|---:|---:|
| 是否有固定 `skill_route_plan` schema | 无 | 有 |
| 路由阶段是否限制读取原始 `SKILL.md` | 无明确边界 | 最多 Level 2 |
| adapter 是否需要 gate token | 不需要 | 必须需要 |
| 未选中 skill 是否可能被旧执行层误调 | 可能 | 被 gate 拦截 |
| 旧 100 条 ground truth 触发准确率 | 无稳定指标 | 100/100 |
| 旧 100 条 ground truth primary 准确率 | 无稳定指标 | 100/100 |
| 旧 100 条 false positive | 无稳定指标 | 0 |
| 旧 100 条 false negative | 无稳定指标 | 0 |
| 同类泛化样本触发率 | 48% | 86% |
| 同类泛化样本触发提升 | 基线 | +38 个百分点 |
| 同类泛化样本相对提升 | 基线 | 约 79.2% |
| 独立新 100 条触发准确率 | 未测 | 86/100 |
| 独立新 100 条 primary 准确率 | 未测 | 87/100 |

说明：

旧 100 条是用户提供的 ground truth，用于验收 V2.2.2 是否修复既有失败样本。

独立新 100 条是 V2.2.2 完成后重新构造的泛化测试集，不复用旧 CSV。

它暴露了下一步还要继续提升的地方，但也证明现在系统已经能被量化评估，而不是凭感觉判断。

## 旧系统与新系统触发率对比

如果只看“全新表达方式是否能触发专业路由”，V2.2.2 相比旧 skill 系统有明显提升。

| 对比项 | 旧 skill 系统 | V2.2.2 新系统 |
|---|---:|---:|
| 测试口径 | 同类泛化样本触发 | 同类泛化样本触发 |
| 触发率 | 48% | 86% |
| 通过数量，按 100 条折算 | 48/100 | 86/100 |
| 绝对提升 | - | +38 个百分点 |
| 相对提升 | - | 约 79.2% |
| 主要原因 | 硬编码和关键词猜测 | 注册表路由、上下文评分、负向匹配、gate 约束 |

这个对比说明：

```text
旧系统每 100 条新表达，大约只能正确触发 48 条。
新系统每 100 条新表达，可以正确触发 86 条。
```

也就是说，V2.2.2 不是只把旧测试集刷高了。

它在新样本上的触发能力从：

```text
48% -> 86%
```

提升为：

```text
+38 个百分点
相对提升约 79.2%
```

但这个 86% 仍然不是终点。

它说明新系统已经明显强于旧系统，同时也留下了 V2.2.3 的优化空间。

## 旧问题

V2.2.1 之前，系统主要有两个问题。

第一个问题：

```text
中枢 agent 知道有 Antenna Skills，但路由决策不稳定。
```

它更像是：

```text
看到一些关键词 -> 猜测可能需要 skill -> 执行层继续往下跑
```

而不是：

```text
生成结构化路线 -> 审查路线 -> gate 放行 -> 执行 adapter
```

第二个问题：

```text
旧 V1 packet chain 有无条件调用 adapter 的倾向。
```

这会导致两个副作用：

```text
非天线任务可能误触发天线 adapter。
旧 adapter 可能在上游 skill_packet 不完整时被提前调用。
```

V2.2.2 的重点就是消除这两个问题。

## 新执行层

新增项目内 skill 注册层：

```text
skill_system/
  categories/
  registry/
  prompts/
```

唯一手写入口：

```text
skill_system/categories/**/*.json
```

自动生成或维护的注册文件：

```text
skill_system/registry/skill_index.json
skill_system/registry/category_index.json
skill_system/registry/routing_aliases.json
```

新增运行时模块：

```text
agent_runtime/skill_registry.py
agent_runtime/skill_disclosure.py
agent_runtime/skill_matcher.py
agent_runtime/skill_executor_gate.py
```

重构模块：

```text
agent_runtime/skill_router.py
agent_runtime/scheduler.py
```

## 渐进披露边界

V2.2.2 明确规定路由阶段最多使用 Level 2：

```text
Level 0: skill_index
Level 1: routing_card
Level 2: interface_card
Level 3: source_skill
```

硬规则：

```text
SkillRouter / SkillMatcher 不允许读取原始 SKILL.md。
只有 SkillExecutorGate 放行后，旧 adapter 才能触达原 skill 或 packet_adapter.py。
```

这使路由阶段更轻、更快，也更容易测试。

## skill_route_plan

V2.2.2 输出固定 schema：

```text
skill_route_plan
```

前端、reviewer、gate、测试都只读取这个结构，不再解析自由文本。

它记录：

```text
选中了哪些 skill
排除了哪些 skill
为什么选中
为什么排除
匹配词是什么
负向匹配是什么
adapter_binding 是什么
风险等级是什么
```

这让“中枢为什么调用某个 skill”第一次变成可解释数据。

## 执行门

旧逻辑：

```text
_run_packet_stage()
-> _run_antenna_skill_adapter()
```

新逻辑：

```text
_run_packet_stage()
-> _prepare_packet_payload()
-> skill_executor_gate.check()
-> gate 通过：_run_antenna_skill_adapter(..., gate_token)
-> gate 不通过：跳过 adapter，继续生成普通 packet
```

`_run_antenna_skill_adapter()` 现在必须校验：

```text
gate_token 存在
task_id 匹配
packet_type 匹配
owner_skill 在 skill_route_plan.routes 中
token_id 匹配当前 route
```

没有 gate token 就拒绝执行。

## 依赖检查

原 Antenna Skills 的 adapter 接收的是 skill_packet 协议，不是本项目普通 packet。

V2.2.2 增加了上游依赖检查：

```text
experiment_contract 需要 idea_card skill_packet
run_manifest        需要 geometry_contract skill_packet
claim_assessment    需要 result_packet skill_packet
next_iteration_plan 需要 claim_assessment skill_packet
```

依赖缺失时：

```text
不调用旧 adapter
记录 skill_adapter_skipped
普通 packet 继续生成
```

这避免了旧 adapter 被错误输入拖垮整个任务。

## 真实模式路由

真实模式 `real` 且有天线上下文时，会路由到：

```text
cst-control
e-platform-cst
antenna-research-reviewer
```

其中：

```text
cst-control     -> CST 控制、preflight、solver gate、结果导出检查
e-platform-cst  -> 本地 E:\antenna skills 平台执行路线
reviewer        -> 阶段审查和风险拦截
```

但 gate token 不能绕过真实 CST 审批。

真实 solver 仍必须经过：

```text
waiting_approval
```

## 测试数据

### 既有 100 条 ground truth

使用用户提供的 100 条 CSV：

```text
C:\Users\30626\Documents\Codex\2026-07-09\skill-skill-2\outputs\antenna_skill_trigger_tests.csv
```

测试结果：

```text
rows: 100
trigger_ok: 100
trigger_rate: 100%
primary_ok: 100
primary_rate: 100%
false_positive: 0
false_negative: 0
```

这说明 V2.2.2 已经覆盖用户之前发现的典型失败样本。

例如：

```text
复现这篇论文
这个结构怎么建模
把图里的尺寸整理出来
检查这个结果能不能支撑 claim
审查一下，不要修复
S11 不行，帮我找原因
增益太低，怎么改
优化这个 React 组件的性能
CNN 准确率太低
Create a CST-style card layout
检查 VSCode 端口占用
```

### 独立新 100 条泛化测试

V2.2.2 完成后又重新构造了一套新 100 条测试集。

这套数据没有读取旧 CSV，避免只对旧样本过拟合。

真实调用项目里的：

```text
agent_runtime.skill_router.SkillRouter
```

测试结果：

```text
rows: 100
trigger_ok: 86
trigger_rate: 86%
primary_ok: 87
primary_rate: 87%
false_positive: 6
false_negative: 8
```

结果文件：

```text
work\skill_router_v222_fresh_eval\fresh_100_skill_route_eval.csv
work\skill_router_v222_fresh_eval\fresh_100_skill_route_eval_summary.json
work\skill_router_v222_fresh_eval\fresh_100_skill_route_eval_report.md
```

这组数据的意义是：

```text
V2.2.2 已经明显提升了已知样本的命中能力和执行安全性。
但对全新表达方式的泛化能力还没有完全达标。
```

## 新旧样本对比

这里要分清三个概念：

```text
旧 skill 系统 48%：旧系统在同类泛化样本上的触发基线。
旧 100 条：验证系统是否修复了已经发现的问题。
新 100 条：验证系统是否能处理没见过的新表达。
```

这三个口径不能混在一起看。

| 对比项 | 旧 100 条 ground truth | 独立新 100 条 |
|---|---:|---:|
| 样本来源 | 用户提供的历史失败/边界样本 | V2.2.2 后重新构造 |
| 是否复用旧 CSV | 是 | 否 |
| 主要用途 | 回归测试，防止旧问题复发 | 泛化测试，检查新说法能不能识别 |
| 测试方式 | 真实调用 `SkillRouter` | 真实调用 `SkillRouter` |
| 样本数 | 100 | 100 |
| trigger_ok | 100 | 86 |
| trigger_rate | 100% | 86% |
| primary_ok | 100 | 87 |
| primary_rate | 100% | 87% |
| false_positive | 0 | 6 |
| false_negative | 0 | 8 |
| 结论 | 已知问题已清零 | 泛化能力仍需增强 |

从数据上看，V2.2.2 对旧样本的提升非常明显：

```text
旧 100 条：
触发准确率从“没有稳定指标”提升到 100%。
primary skill 准确率从“没有稳定指标”提升到 100%。
误触发和漏触发都降到 0。
```

但新样本说明它还不是“泛化完成版”：

```text
新 100 条：
仍有 8 条该触发但没触发。
仍有 6 条不该触发但误触发。
仍有 13 条 primary skill 判断不够准。
```

所以 V2.2.2 的准确定位是：

```text
已知问题修复版 + 可量化路由框架版。
不是最终泛化完成版。
```

它的真实进步在于：

```text
以前只能说“感觉 skill 没被主动调用”。
现在可以精确说“哪一类输入漏触发，哪一类输入误触发，哪个 primary skill 错分”。
```

## 数据结论

V2.2.2 的数据提升主要体现在三点。

第一，已知失败样本被修复。

```text
旧 100 条 ground truth：
trigger_rate  = 100%
primary_rate  = 100%
false_positive = 0
false_negative = 0
```

第二，adapter 调用变成受控行为。

```text
以前：旧执行层可以无条件进入 adapter。
现在：没有 route 和 gate token，adapter 不会执行。
```

这让 `unselected_adapter_called = 0` 成为可验证目标。

第三，系统开始能暴露泛化风险。

独立新 100 条只达到：

```text
trigger_rate = 86%
primary_rate = 87%
```

这不是最终满意结果，但它比之前“看不出来哪里错”强很多。

现在可以精确知道错误集中在哪里：

```text
result_packet / claim_assessment / ARBW 等阶段词需要增强。
GWO / return loss / gain / patch / CST Results 在非天线语境下需要更强负向判断。
objective contract / hypothesis / baseline 的 primary skill 需要更细分。
```

## 验证记录

代码与测试：

```text
python -m py_compile agent_runtime\skill_registry.py agent_runtime\skill_disclosure.py agent_runtime\skill_matcher.py agent_runtime\skill_executor_gate.py agent_runtime\skill_router.py agent_runtime\scheduler.py
python -m unittest tests.test_runtime_core
python -m unittest tests.test_web_api
python -m unittest tests.test_langgraph_runtime
npm run build
npm run test:smoke
```

通过结果：

```text
tests.test_runtime_core: 58 passed
tests.test_web_api: 18 passed
tests.test_langgraph_runtime: 4 passed
frontend build: passed
frontend smoke: 4 passed
```

## 版本边界

V2.2.2 不做：

```text
真实 CST solver 自动运行
完整论文复现闭环
完整创新性评估
多任务并发队列
修改原始 skill
```

V2.2.2 做到的是：

```text
skill 路由可解释
adapter 调用可阻断
旧执行层受 gate 管控
已知 100 条样本完全通过
独立 100 条泛化问题被量化暴露
```

## 下一步

建议 V2.2.3 做路由泛化修复。

目标：

```text
旧 100 条保持 100%
新 100 条 trigger_rate 提升到 >= 95%
新 100 条 primary_rate 提升到 >= 92%
false_positive <= 3%
false_negative <= 3%
```

重点修：

```text
阶段词识别：result_packet / claim_assessment / ARBW / radiation pattern
负向语境：金融 return loss、前端 gain chart、Python patch、CST Results card
primary 细分：objective contract、hypothesis、baseline、novelty
```
