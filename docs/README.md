# docs

| 文件 | 说明 | 是否入库 |
|---|---|---|
| `UI.md` | UI 成本的逐控件分析（Slate 通道 + UMG 结构导出） | 是 |
| `ADVISOR.md` | 优化建议层的方法论（证据锚定 / 杠杆目录 / 账本校准） | 是 |
| `COST.md` | 一次分析的 token 成本实测与复测方法 | 是 |
| `ci/perf-gate.yml` | CI 闸门 workflow 模板（复制到 `.github/workflows/` 生效） | 是 |
| `PLAN.md` / `HANDOFF.md` / `CHANGELOG.md` | 内部规格、会话交接、变更日志 | **否**（.gitignore，仅本地保留） |

口径规则（R1–R15）、执行协议（P1–P21）、里程碑与验收记录属于内部工程文档，不随公开仓库分发；
代码里凡是引用它们的地方，都已改成中性的表述（例如"编码策略 E1–E8"）。
