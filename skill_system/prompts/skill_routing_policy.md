# Skill Routing Policy

V2.2.2 使用四级渐进披露：

- Level 0：只读 skill 索引。
- Level 1：只读路由词、负向词、领域词。
- Level 2：只读输入、输出、风险、adapter binding。
- Level 3：读取原 `SKILL.md` 或调用 adapter，只能由 executor gate 放行。

旧 V1 packet chain 保留为执行层，但不再拥有自主调用 skill adapter 的权力。
