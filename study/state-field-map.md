# 状态字段地图

## 父图与子图

| 字段 | 谁写 | 谁读 | 生命周期 | 是否持久化 | 备注 |
|---|---|---|---|---|---|
| `task_id` | 待填写 | 待填写 | 全任务 | 是 | |
| `plan` | 待填写 | 待填写 | 计划版本 | 是 | |
| `current_step` | 待填写 | 待填写 | 当前步骤 | checkpoint | |
| `task_result` | 待填写 | 待填写 | 当前步骤 | checkpoint | |
| `module_result` | 待填写 | 待填写 | 当前步骤 | checkpoint | |
| `global_result` | 待填写 | 待填写 | 当前步骤 | checkpoint | |
| `current_failure` | 待填写 | 待填写 | 修复周期 | checkpoint | |
| `validated_learning_context` | 待填写 | 待填写 | 任务创建快照 | Blackboard | |

## Agent IO 裁剪

| Contract | 必填输入 | 默认字段 | 禁止泄露字段 | 我的验证 |
|---|---|---|---|---|
| `agent.task` | 待填写 | 待填写 | 待填写 | |
| `agent.module_review` | 待填写 | 待填写 | 待填写 | |
| `agent.global_review` | 待填写 | 待填写 | 待填写 | |
| `agent.repair_task` | 待填写 | 待填写 | 待填写 | |
