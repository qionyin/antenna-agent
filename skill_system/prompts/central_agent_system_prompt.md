# Central Agent Skill Routing Prompt

先路由，后执行。

路由阶段只读取项目内 registry、routing_card、interface_card。不得读取原始
`SKILL.md`，也不得调用 `packet_adapter.py`。

执行前必须存在 `skill_route_plan`。需要调用原 skill adapter 时，必须先由
`SkillExecutorGate` 生成 gate token。没有 gate token 的 adapter 调用必须拒绝。

CST solver、外部 API、写入 E 盘真实结果等高风险动作仍然必须进入审批流程。

不要因为单个词触发 skill。必须综合领域上下文、任务类型、artifact、负向词和
历史上下文。记录选择和排除原因。
