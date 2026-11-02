# Rica Bug Resolver 接入准备实施计划

**Goal:** 在最新 Relay 上保留个人 PR Review 行为，准备跨平台 Bug Resolver，并用实际只读请求核对依赖。
**Architecture:** 保留 HTTP Events → QStash → Multica Issue 链路；Review 与 Bug Resolver 使用独立 Prompt、Agent、Project 和 thread scope。
**Tech Stack:** TypeScript / Vitest / Python / Multica Codex Runtime。
**Spec:** 用户 2026-10-28 的九条批注；本目录 BUG-RESOLVER-PROMPT.md；docs/bug-resolver-readiness.md。

## 约束与验收

- 最新 upstream/fork 基线 17eb77d；旧目录及未提交改动保持原样，不复制凭据。
- 准备阶段未 commit/push；用户现已明确授权本次 commit、push、创建 PR，并在 FinTech 工作区创建独立 Resolver。保持现有 Slack 事件入口与 Socket Mode 配置不变；Relay 部署仍待路由实现与验收。
- Skills 升级保留私有补丁、外置 credentials、OAuth cache、Redshift registry。
- 六类平台纳入能力判断：Web、BFF、后端、iOS、Android、跨仓链路；工具 smoke 不冒充业务验收。
- 所有八来源均有实际有界只读探针；失败写明缺口，不用本地安装状态代替连接证明。

## Task 1 — 个人 Relay 行为迁移

文件：src/{config,http,mentions,multica-api,reaction,task-presentation,thread-router,release-approval,ignore}.ts；api/multica/ignore.ts；scripts/readonly-reviewer.py；对应 tests；vercel.json。
- [x] 以 b9935b7 对比两侧；先迁移旧回归并观察新版缺失行为的失败。
- [x] 恢复作者门禁、可信发布候选、Agent 所有的 reaction、无效任务收口和隔离 reviewer；保留新版 squad / stable scope / 冻结 payload / 可靠回复适配器。
- [x] 跑 TypeScript、Python 回归及 TypeScript 检查，记录真实结果。

## Task 2 — Prompt 与权限分离

文件：AGENT-PROMPT.md、BUG-RESOLVER-PROMPT.md、.env.example、README.md、SETUP-GUIDE.zh-CN.md。
- [x] 保留现有 Rica Review prompt 全部个人规则并更正新目录脚本路径。
- [x] 删除 Resolver 的 moe-router，改用已安装技能名；使用 Rbot 身份与本机路径；列出未齐依赖和 Sites 边界。
- [x] Multica 指定 Resolver 的可信 instructions 可预授权当前 Bug 的分支 commit/push/PR；人工会话继续逐任务明确授权。
- [x] 静态核对不存在 Winches 本机路径/旧 Bot ID/已删除 skill 引用，git diff --check。

## Task 3 — 运行环境与上线缺口

- [x] 安装/升级 Skills 并保留私有配置；8 来源只读验证；agent-browser 实际点击 smoke、Maestro版本和PATH检查。
- [x] 核对实际 Multica Runtime/工具注入；分开记录 Skills 文件存在、进程可用、远端服务可用。
- [x] 明确 app_mention、双路由、重复事件与同thread scope隔离的剩余实现及验收样例。
- [x] 汇总交付清单与阻塞，不将未部署代码称为 Slack 闭环上线。

## 回归重点

本人只 @组不得触发Review；他人 @组及本人直接 @自己按原规则接单。可信发布候选精确校验来源。双投递不得重复建卡。冻结重试不能改写原意。观测表情失败不能阻断业务。

执行结论：本阶段的迁移与就绪核对完成。实际 Multica Sites 工具调用、Bug 分路实现与线上端到端验证未执行，明确留在 docs/bug-resolver-readiness.md 下一阶段；没有把它们勾为已交付。

## 已授权的创建交付

- [x] 在 `feat/rica-bug-resolver` 提交、普通 push，并创建个人 fork 的 PR #1。
- [x] 在 FinTech 工作区创建独立 `MoeGo Bug Resolver` 与任务看板，绑定在线 Codex Runtime，并发 2，逐字回读 Prompt。
- [x] 在 readiness 文档记录实际对象 ID 与入口；既有 Agent 和 Slack HTTP 入口不变。
- [ ] 实现并验收 Slack Bug 分路与真实 Runtime 工具调用；本次创建不代表这两项已完成。
