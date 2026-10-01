# 独立学习与自进化模块

V2.3 的学习记忆实现现在归属仓库根目录的 `agent_learning/` 包。

## 主边界

```text
Scheduler / LangGraph
        -> agent_learning.LearningService
        -> MemoryManager / Redis
        -> source / evidence / candidate / experience / wiki
```

`agent_runtime.learning` 和 `agent_runtime.knowledge` 只保留兼容导出，不再承载实现。

## 自进化边界

```text
评测结果
  -> ExtractionQualityEvaluator（结构审计/金标准实体关系评测）
  -> EvolutionService.diagnose()
  -> EvolutionService.propose()
  -> 低风险 alias 白名单校验 / 高风险人工审查
  -> ControlledEvolutionExecutor.apply()
  -> EvolutionService.verify()
  -> 指标提高则保留，否则回滚
```

自进化模块允许自动修改独立学习模块的 alias 覆盖配置，但只接受金标准案例或中枢 LLM 提出的、能在原文中逐字定位的已知实体别名。Prompt、阈值、任务计划、知识晋级和生产代码仍只生成工单，不自动修改。

`self-evolving-kb-master` 的 Markdown、AnythingLLM、Ollama 和观测脚本不作为 V2.3 的第二运行时；后续通过 `integrations/` 适配器接入原文、评测或导出。

外部评测 JSON 可通过 `SelfEvolvingEvaluationAdapter` 转成统一评测结构，再交给 `EvolutionService` 诊断；外部项目本身不直接写入 Redis 或任务状态。

`compile_source()` 每次都会产生抽取结构审计并保存评测记录。带人工金标准的抽取案例可调用：

```text
POST /learning/evolution/evaluate-extraction
POST /learning/evolution/run-extraction
```

诊断与提案保存在 `mem:evolution:run:*`、`mem:evolution:proposal:*`，修改前后验证保存在 `mem:evolution:verification:*`。

`run-extraction` 的闭环为：

```text
训练集评测 -> 诊断 -> 候选 alias -> 策略校验 -> 应用 -> 独立复测集 -> 保留/回滚
```

自动应用必须提供 `verification_cases`，且 case ID 不能与发现问题的训练集重复；缺少独立复测集时只保留 proposal，不修改配置。

当 `use_external_llm=true` 时，学习模块复用 Scheduler 的同一个 `LLMClient`、运行时地址和模型；LLM 只生成候选，不拥有修改权。

## 开关

默认开启：

```yaml
learning:
  enabled: true
  evolution_enabled: false
  domain_knowledge_enabled: false
```

临时关闭：

```powershell
$env:LEARNING_MODULE_ENABLED = "false"
```

单独开启自进化链路：

```powershell
$env:LEARNING_EVOLUTION_ENABLED = "true"
```

当前项目配置中 `learning.enabled=true`、`learning.evolution_enabled=false`：保留任务 Episode/Experience，但不运行评测、提案、自动 alias 修改或复测。

领域知识链单独开关：

```powershell
$env:LEARNING_DOMAIN_KNOWLEDGE_ENABLED = "true"
```

当前 `domain_knowledge_enabled=false`，原文池、Evidence、Domain Knowledge、聚类、创新 Gate、Wiki 和论文图谱学习接口关闭；Episode、Experience、配置 L2、成功工作流 L3 保留。

关闭时：

- 不自动写入 episode / experience。
- 不向计划提供 promoted learning context。
- Scheduler、LangGraph、PaperWise、Skill Gate、CST 审批和执行不受影响。

开启/关闭对照只改变学习层，不改变任务执行主链。
