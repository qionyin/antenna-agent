# API 地图

| 方法 | 路径 | 调用对象 | 主要状态变化 | 风险/Gate | 实验结果 |
|---|---|---|---|---|---|
| GET | `/health` | 待填写 | 无 | 无 | |
| POST | `/tasks` | 待填写 | 创建并执行动态任务 | LLM/Skill Gate | |
| POST | `/tasks/real-cst-single-run` | 待填写 | 创建真实任务并预检 | CST approval | |
| POST | `/tasks/{id}/approve` | 待填写 | checkpoint resume | request/plan/step 绑定 | |
| POST | `/tasks/{id}/reject` | 待填写 | 终止并报告 | 人工决定 | |
| POST | `/tasks/{id}/central-message` | 待填写 | reply/revise/reroute | intent 约束 | |
| POST | `/tasks/{id}/paperwise/review` | 待填写 | 只读证据重审 | 外部 LLM Gate | |
| GET | `/learning` | 待填写 | 无 | 无 | |
| POST | `/learning/paperwise/ingest` | 待填写 | 原文池/候选证据 | PaperWise 只读 | |
| POST | `/learning/wiki/rebuild` | 待填写 | Wiki 投影 | promoted-only | |
| GET | `/learning/graph` | 待填写 | 无 | PaperWise 图谱只读 | |
