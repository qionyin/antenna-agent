# V2.3 学习记忆与领域理解

## 目标

V2.3 为 API 型大模型增加可验证的记忆改写闭环：保存原文和任务经历，提取带来源的候选证据，形成候选领域知识和任务经验，经审查、反馈和回放后才允许晋级。

本版本不修改模型参数，也不把一次 LLM 输出直接当作正式知识。

## 已实现链路

```text
PaperWise report / user source / CST result
-> 独立 Redis 原文池
-> EvidenceCompiler
-> 论文候选领域知识或任务经验候选
-> KnowledgeReviewer
-> candidate / validated / promoted / contradicted
-> promoted 论文知识进入检索和 Wiki 投影；既有论文图谱保持唯一
-> promoted 经验进入任务计划提示和 Wiki 实践经验页
```

## 存储边界

```text
mem:source:long_term:*  完整原文
mem:evidence:*          原文证据单元
mem:l2:domain:*         论文来源领域知识
mem:episode:*           一次任务完整经历
mem:l3:experience:*     工作流经验
mem:cluster:*           聚类候选和迭代记录
mem:innovation:*        创新候选及 Gate 结果
mem:wiki:*              由论文知识和 promoted 经验生成的 Wiki 投影
```

硬约束：

- L2 领域知识必须来自 `paper_fact` 或 `source_paper_fact`。
- CST、用户反馈和任务经验只进入 episode/experience，不进入论文知识图谱。
- promoted 经验可以进入 Wiki 的“实践经验”页，并标注为经验记忆，不是普遍领域事实。
- candidate、contradicted 和 rejected 记录不能进入正式检索上下文。
- V2.3 不创建第二张图谱。论文关系仍来自现有 PaperWise 图谱或独立 antenna research graph。
- Wiki 可由 promoted 论文知识和 promoted 经验重建，但不是新的事实源。

## 聚类

支持 `natural`、`semantic` 和 `random` 三种候选聚类。每次保存：

- 随机种子
- 温度和降温率
- 合并/拆分阈值
- 迭代次数
- 每轮簇数量
- 模糊候选对
- 停止原因

聚类只产生 `candidate_cluster` 或 `hypothesis_cluster`，不直接合并正式知识。

## 经验学习

终态任务自动保存 episode 和经验候选。经验包括：

- 触发条件
- 执行动作
- 成功/失败结果
- 错误归因
- lesson
- 不适用条件
- 任务、审查和 CST 证据引用

经验必须经过至少三条回放或明确权威反馈，才能进入 `validated`；再经过显式晋级才能进入 `promoted`。

## 创新 Gate

创新候选采用 baseline-gap-delta 结构，要求：

- 论文证据
- 电磁机制
- 可建模参数
- 基线、消融和失败条件

没有 CST 或实测验证时，最高只能是 `experiment_ready`，不能标记 `validated_innovation`。

## 接口

主要接口：

```text
POST /learning/sources
POST /learning/paperwise/ingest
POST /learning/sources/{source_id}/compile
GET  /learning/knowledge
POST /learning/knowledge/{knowledge_id}/review
POST /learning/knowledge/{knowledge_id}/promote
POST /learning/clusters
GET  /learning/experiences
POST /learning/experiences/{experience_id}/feedback
POST /learning/experiences/{experience_id}/replay
POST /learning/experiences/{experience_id}/promote
POST /learning/innovations/evaluate
POST /learning/wiki/rebuild
GET  /learning/graph
GET  /learning/wiki
GET  /tasks/{task_id}/learning
```

## 验证结果

```text
V2.3 核心学习与边界测试：`tests.test_v23_learning` 12/12 passed；独立学习/进化模块另行计数。
其中学习 API/图谱来源测试：3/3 passed
既有运行回归：116/116 passed
前端生产构建：passed
真实 PaperWise 导入 smoke：90 个证据单元、10 个候选知识，原文哈希回读一致
```

## 当前限制

本版的 `EvidenceCompiler` 是确定性规则基线，不等于完整的大模型语义理解；它用于建立安全的数据结构和验证门。当前仍需要后续版本补充：

- 外部 LLM 结构化抽取适配，但必须保留原文绑定和人工/Reviewer Gate。
- 基于真实历史反馈的经验 lesson 提炼，而不是模板化 lesson。
- 使用固定标注集评估实体、关系、条件和创新判断准确率。
- PaperWise 全量知识的批处理、人工抽样审查和增量重建。

这些限制没有被计入“已完成学习能力”。
