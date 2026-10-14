# Slack → Multica Relay

在获准频道中 @真人或 User Group，把请求交给 Multica 的专用 Agent，执行环境可以是本地 Codex。适配 Vercel Functions、EdgeOne Cloud Functions 与 Cloudflare Workers，共用同一套处理逻辑。

## 适用范围

Relay 的事件准入、投递、任务映射与恢复是通用能力。本文配套的 [AGENT-PROMPT.md](AGENT-PROMPT.md) 和 [multica-final-reply](multica-skills/multica-final-reply/SKILL.md) 是 `Slack Task Router (Codex)` 的专属配置；其中的意图判断、静默、原 thread 回复及发布/部署限制不自动适用于其他智能体或小队。

新智能体按自己的任务目标配置独立 instructions、Skills、结果接收位置与操作权限。即使输入是 Slack thread 链接，或复用同一 Relay、工作区、Runtime，也不默认采用 Router 的配置。维护时先按 [AGENTS.md](AGENTS.md) 确认当前目标。

## 链路

`Slack → 签名与准入校验 → QStash 持久化 → HTTP 200 → 消费函数 → Multica Issue → Agent/Runtime → Slack 回复`

- 只处理当前消息明确 mention 的事件。Team 必填；频道和发送者支持白名单（可设为 `all`）及黑名单，黑名单优先。Bot、编辑/删除、普通讨论不触发。
- 入站只等待 QStash 接收；队列负责后台投递及3次重试，耗尽后在其失败队列查看/重放。
- 每个 Slack thread 通过普通 Issue API 创建独立任务卡，不经过 Autopilot 同标题60秒去重。
- thread scope 包含 Workspace、Project、稳定的路由标识；默认不同 Agent/Team 不采用彼此的映射。
- 同 thread 后续消息追加评论。QStash 按 thread 限并发，Redis 锁与消息状态处理重投。
- 标题使用消息摘要与稳定的线程短标识；描述和后续评论分为原文引用、来源及 JSON 上下文，附件仅保留定位字段。
- 描述首行的 `relay-thread` 标记和 `relay-payload:v1` 数据区块用于 KV 映射过期后的恢复，请勿删除或修改。读取兼容历史裸 JSON；损坏的数据会停止恢复，不自动重新建卡。
- 写请求结果不明时先查回读；查不到则保留 ambiguous 错误，不盲目再次 POST。需要人工核对/重放，不承诺 exactly-once。
- `comment_persisted` 只表示评论保存，实际执行和原 thread 回复要分别验收。
- Slack Task Router 的 Prompt 真源为 [AGENT-PROMPT.md](AGENT-PROMPT.md)，需要明确同步到该 Agent 的 instructions；其他智能体维护自己的 Prompt。Relay 不调用 Codex 或修改 Multica 源码。

## 本地验证

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm lint
```

配置与部署见 [搭建手册](SETUP-GUIDE.zh-CN.md)，契约边界见 [审查记录](REVIEW.md)。

## 状态与日志

日志记录关联标识、耗时和有限错误码。正文保存在队列和 Multica；Redis 保存线程/消息状态90天，并保存单条事件的冻结正文24小时。内容级调试日志尚未启用，凭据不进入日志。

| 接口结果                | 含义                                                                   |
| ----------------------- | ---------------------------------------------------------------------- |
| 入站 accepted / HTTP200 | QStash 已接收，不代表 Agent 完成                                       |
| 入站 ignored / HTTP200  | 不满足触发范围                                                         |
| 入站503                 | 收件未确认，交给 Slack 重试                                            |
| 消费 created            | Issue 已创建或从回读恢复                                               |
| 消费 comment_persisted  | 后续评论已保存                                                         |
| 消费 duplicate          | 已处理的消息                                                           |
| 消费 rejected / HTTP200 | 无效输入或确定性失败，retryable=false，不继续队列重试                      |
| 消费503                 | 保留队列重试/DLQ责任，原因包括 timeout、thread*lock_busy、ambiguous*\* |

`GET /api/health` 仅证明函数可响应。消费有45秒整体预算，部署函数上限60秒；入站发布请求超时2秒。平台冷启动、网络延迟与配额仍须实测。

## 迁移到 Multica Team

新任务设置 `MULTICA_ASSIGNEE_TYPE=squad`、`MULTICA_ASSIGNEE_ID=<Team UUID>`。Team Leader 负责分流，成员不会自动全部执行。未设置新参数时继续兼容 `MULTICA_AGENT_ID`。

已有部署迁移必须将 `MULTICA_THREAD_SCOPE_ID` 固定为原 Agent ID，长期保留；消费者 URL、Redis、Workspace 和 Project 不变。这样锁、消息状态、描述标记和标题标识保持兼容。过渡期可设置 `MULTICA_LEGACY_AGENT_ID=<原 Agent ID>`，只允许采用该旧归属，不能填其他 Agent。

先发布兼容代码，再配置 Team。按原 scope marker 核验历史任务，以 `multica issue assign <id> --to-id <Team UUID> --no-start` 迁移空闲任务；指派可能中断正在运行的任务，因此活动任务应完成后再迁移。不得改写原描述 marker 或清空 Redis。切换后等待旧请求结束并补查遗漏任务。过渡期旧任务仍可由原 Agent 处理；完成归属核验后可以移除 LEGACY 参数。旧版本回滚时也必须同时恢复任务归属，不能只回滚代码。

Team 指令只注入 Leader。子任务仍需主动结果交接和回复去重；`in_review` 保留人工验收，不能仅依赖 run completed 自动回复 Slack。Token、签名密钥保留 Secret；上述类型和 ID 使用可查看的 Config。

Team 的取消仍只绑定当前 Issue 接口返回且已校验工作区的活动 run 集合；不扩展到其他 Issue 或子任务，重试不会改为取消之后新增的运行。单 Agent 与过渡期旧 Agent 仍校验具体执行者。

Team 模式不将旧 Agent 的配置快照当作实际执行模型：relay 的 `slack_reply_context` 标记为 unavailable、agentId 为 null；最终回复 Skill 应从实际运行记录获取模型与统计。

## 在 thread 内取消任务

在原任务 thread 回复 `@目标 cancel` 或 `@目标 取消`。只有发送者在 `SLACK_TARGET_USER_IDS` 中才生效，频道和发送者黑白名单继续生效。`SLACK_CANCEL_KEYWORDS` 可配置逗号分隔的关键词，非空配置替换默认值；去掉目标 mention 和首尾空白后完整匹配，英文忽略大小写。

取消指令复用现有消息订阅，经 QStash 持久化处理，不会建卡、追加任务评论或添加启动 reaction。消费者保存目标运行 ID，取消排队或执行中的运行并回读状态，然后清理已记录触发消息上当前 reaction token 身份的全部 reaction（包括启动标记）；其他人的 reaction 保留。已完成、已失败且没有活动运行时保留原标记。

取消期间的任务请求会被忽略。取消完成后重新发送 mention 可继续原任务卡；已处理的旧事件不会重新启动任务。API 或清理失败保留进度，由 QStash 重试；重试耗尽需检查 DLQ 并重放原消息。所有 reaction 操作优先使用 `SLACK_BOT_TOKEN`，未配置时使用 `SLACK_USER_TOKEN`；选中的 token 需要 `reactions:read`、`reactions:write` 且能访问目标频道，调用失败不会切换身份。切换身份后，旧身份添加的表情会保留。无需订阅 `reaction_added`。

## 可靠交付与可选发送适配器

Relay 将触发消息转为任务；上下文读取由 Agent 的 Prompt 和 Skills 决定。每个事件的正文、附件安全元数据与配置快照冻结 24 小时供重试复用，按稳定 marker 搜索恢复任务；附件内容及 private URL 不进入队列。已有取消权限、固定 run 集合与 reaction 清理保持原有语义。

`scripts/slack-reply.py` 是可选的确定性发送入口，使用私有 JSON、来源 Issue/comment 校验和 at-most-once delivery ledger。现有最终回复 Skill 默认仍用原 Slack 发送流程；采用 adapter 时只保留一个发送入口，避免两种方式同时发。配置及升级步骤见 [搭建手册](SETUP-GUIDE.zh-CN.md)。

Cloudflare 使用 `pnpm dev:cf`、`pnpm build:cf` 和 `pnpm deploy:cf`，共享同一 consumer 与 QStash/Redis；构建推荐 Node.js 24。

## 每日值守看板

`scripts/duty-board.py` 只读汇总「GRM 每日 Bug 值守」的反馈日漏斗：读取值守项目任务、字段台账任务 GRM-664 的 `<!-- duty-board:v1 -->` 评论、GitHub PR 与「Release to PRODUCTION」运行（兼容 cherry-pick 发布线），以及 Slack 交付 thread 上审查人的 🚫 和回告草稿上的 📤（已回告）、🔕（不回告），输出 `board.json`、`board.md`、`slack.md`。「GRM 每日值守看板」Agent（Prompt 真源 [DUTY-BOARD-PROMPT.md](DUTY-BOARD-PROMPT.md)）由 Autopilot 每天 08:30 运行 `origin/main` 版本，发送到指定交付频道并追加到 GRM-665。本地预览：

```bash
python3 scripts/duty-board.py --project <值守项目 ID> --ledger-issue GRM-664 --start 2026-10-12 --reviewer <审查人 Slack 用户 ID> --output-dir <输出目录>
```
