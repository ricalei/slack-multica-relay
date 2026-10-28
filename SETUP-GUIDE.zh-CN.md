# 配置与部署

同一源码支持 Vercel、EdgeOne Cloud Functions 和 Cloudflare Workers。比较平台时，每次只让一个部署接收同一个 Slack App 的事件，以免双重执行。

## 1. 专用资源

准备本项目专用的 Upstash Redis 和 QStash；不复用公司业务数据库。Redis 保存映射和写入状态；QStash 负责持久化事件、消费重试与失败队列。

按 [.env.example](.env.example) 配置环境变量。凭据放平台 Secret 配置，不提交到 Git。QStash 的 current/next signing key 用于校验消费请求，不能只校验一个自定义静态请求头。

## 2. Multica

以下 Prompt、回复 Skill 和回复身份配置针对 `Slack Task Router (Codex)`。配置其他智能体或小队时，单独确定其任务入口、instructions、Skills、结果接收位置与操作权限，不因使用 Relay 或 Slack 链接而继承 Router 的意图判断、静默、原 thread 回复及发布/部署限制。

- 在目标 Workspace 创建专用 Project 和 Agent，绑定需要使用的 Runtime。
- 将 [AGENT-PROMPT.md](AGENT-PROMPT.md) 仅同步为 Slack Task Router 的 instructions。
- 个人 Review 按 AGENT-PROMPT 加载本机业务 Skills，使用迁入本目录的隔离 reviewer。保留上游 multica-final-reply 供明确选择该发送方案的 Agent 使用，不与个人直接发送流程同时启用。频道和发送者准入由 Relay 统一校验。
- 个人 Review 使用 `with-skill-env` 和 `moe-slack --as user` 在原 thread 回复；Bug Resolver 使用独立 Prompt 和 `--as bot`，不覆盖共享 User token。可选 adapter 必须替换而非叠加发送步骤，启用前核验对应 actor。
- 回读 Agent 的 Runtime、权限和并发。初期并发2即可；Mac 休眠/断网会影响执行。
- 读取本地 Skills 和 Workspace 指派 Skills 的实际加载结果。数据库 Skill 数量不能单独说明任务可用能力。
- Relay 使用 MULTICA_PROJECT_ID/MULTICA_AGENT_ID 调用普通 Issue API；不再需要 Autopilot。

Agent instructions 写入任务工作目录 AGENTS.md。Multica daemon 为 Codex 准备任务环境；桌面聊天上下文不会自动复制。现有 Codex 适配器会自动批准工具请求，Prompt/Skills 只能构成行为合同；不可绕过的写审批需要执行端或工具端支持。

### Slack Task Router 最终回复与模型来源

个人 Review 的回复规则以 [AGENT-PROMPT.md](AGENT-PROMPT.md) 为准。以下上游采集器仅供选择 `multica-final-reply` 的 Agent 使用，由该 Skill 的 `scripts/run_context.py` 读取当前 run、所属 Agent 配置和运行日志，再按 [Footer 展示](multica-skills/multica-final-reply/references/footer-display.md) 通过 Slack Skill 发送；风格与表情沿用该 Skill 的 references。本地文件变更不会自动替换这些指令或线上绑定。

采集器在隔离运行中使用 `MULTICA_SERVER_URL`、`MULTICA_WORKSPACE_ID` 和 `MULTICA_TASK_ID`。任务链接使用 Agent 环境中的 `FINAL_REPLY_APP_URL`（HTTPS 网页根地址）和 `FINAL_REPLY_WORKSPACE_SLUG`；可用已有详情通过 `--issue-identifier`、`--workspace-slug`、`--app-url` 显式传入。缺少链接信息时省略链接，继续回复。

Relay 仍在准备单条事件时，用现有 PAT 读取一次配置并冻结到 `replyContext`，deadline 两秒。这个事件快照与发送前采集器读取的当前 Agent 配置是不同时间点的数据，都不能证明 run 实际使用的模型或计费档位。默认最终回复按采集器结果展示；下文可选 adapter 未收到本轮统计文件时，才使用事件快照显示配置模型及已知 Fast 标记。有有效本轮统计时不叠加旧模型行。

## 3. Slack App

使用专用 App 或明确获准复用的 App 接收需要的 message 事件。私有频道订阅 `message.groups`，并将接收 App 加入指定频道。message 订阅也会送达 App/Bot 消息；Relay 默认拒绝，只有下述发布候选三元组和严格消息格式可以例外进入。Review 路径的 Relay 不添加接单 reaction；Review Agent 核验 Slack 来源、Review/Approve 意图和唯一 PR 后，才使用获准的 owner USER 身份添加 `eyes`。非 Review mention 静默结束，不加 reaction、不回复“没有 PR”；Review 意图明确但 PR 不唯一时可澄清，目标固定前不加 `eyes`。Relay 仅在取消成功后使用同一用户的 `SLACK_USER_TOKEN` 读取和清理自己的 reaction；`SLACK_BOT_TOKEN` 不参与 Review 路径，独立 Bug 路由用它添加接单 eyes 并清理 Bot reaction。Review 验收时核对 reaction 和回复消息的 `user` 都是 `U05GLCUAUTU`。

Bug 路径在 Issue / follow-up 持久化后，由 consumer 在触发 @ 消息（非固定 thread 根消息）上添加 `eyes`；使用 `SLACK_BOT_TOKEN`，先用 `auth.test` 核对工作区与 `SLACK_BUG_BOT_USER_ID`。Bot 需要 `reactions:write`。重复事件、取消或忽略事件不加；接单反馈在既有 thread lock 内完成，随后取消仍能清理该反馈。reaction 是有界 best effort，失败只记录 `relay_bug_receipt / eyes_failed`，不重试已完成的任务交付、不阻断 Agent；消息落库后进程中断可能缺少该图标，Agent 仍继续处理。官方接口：[reactions.add](https://docs.slack.dev/reference/methods/reactions.add/)。

配置 Request URL 为 `https://<当前部署>/api/slack/events`，对应 Signing Secret 填入部署环境。新增 scopes 后重新安装。只修改已授权用于 Relay 的 App。

`SLACK_TEAM_ID` 必填；`SLACK_TARGET_USER_IDS` 和 `SLACK_TARGET_SUBTEAM_IDS` 至少填一项。Relay 还必须配置单个 `RELAY_OWNER_SLACK_USER_ID`（当前为 `U05GLCUAUTU`），缺失或格式无效时拒绝启动。owner 只 @目标组、没有直接 @自己时，在入队前静默过滤，消费者也在访问 Redis/Multica 前复核。owner 直接 @自己时正常进入路由，即使同条消息也 @组；其他同事 @组及可信发布候选不受此门槛影响。该过滤不取消已启动任务，也不代替 Agent 对有效 Review 意图的判断。

`SLACK_ALLOWED_CHANNEL_IDS` 必须显式填写频道 ID 或 `all`，缺失/空值拒绝启动。`SLACK_BLOCKED_CHANNEL_IDS`、`SLACK_ALLOWED_SENDER_IDS` 和 `SLACK_BLOCKED_SENDER_IDS` 可选，黑名单优先于白名单。后续问答仍需再次 mention。

### 发布候选直接批准

发布候选功能使用一组可选但不可拆分的配置：`SLACK_RELEASE_APPROVAL_CHANNEL_ID`、`SLACK_RELEASE_APPROVAL_APP_ID`、`SLACK_RELEASE_APPROVAL_BOT_ID`。三项全部留空表示关闭；只配置部分会使 Relay 拒绝启动。当前批准范围为频道 `C08HV4GTJ2D`、App `A080YA0KTJ9`、Bot `B081PVALKC0`，生产环境应把三项配置为 Secret 后重新部署。

Relay 只接受该来源的顶层消息，且正文必须同时包含 `RELEASE APPROVAL REQUIRED`、`WAITING FOR APPROVAL`、唯一 `vX.Y.Z` 版本和唯一 `版本 PR` GitHub 链接；业务 PR、Changelog、Jira 和 Action link 不会成为目标。同一版本 PR 的多条通知汇聚到同一 Multica Issue，Agent 仍会检查当前 head 是否已有 Rica 的有效 APPROVED，避免重复写入。

Agent 对此任务只读取 Slack 来源和 GitHub PR 元数据，不读取 diff、不启动 reviewer。核验后添加 `eyes`，直接 Approve 版本 PR，写后回读成功再添加 `a`；成功或失败均按 AGENT-PROMPT 在原 thread 简短说明实际结果。它不会评论业务代码、合并、启用 auto-merge、进入 merge queue、发布、创建 tag 或部署。部署验收不重放历史通知、不拿现有 PR 做写入测试；下一条真实发布候选是首次外部写入验收。

## 4. Vercel

导入仓库，Framework 选 Other，安装使用 `pnpm install --frozen-lockfile`。入口位于 api/；vercel.json 设置消费函数60秒。配置环境变量，RELAY_CONSUMER_URL 必须是该部署的准确公网消费 URL。

无效任务收口还需生成一枚独立随机 token，以 Secret 形式配置到 Vercel 的 `RELAY_IGNORE_TOKEN`，不得复用 `MULTICA_API_TOKEN`。同时在 Slack Task Router Agent 的 Secret 环境变量中配置相同的 `RELAY_IGNORE_TOKEN`，并配置 `RELAY_IGNORE_URL=https://<当前部署>/api/multica/ignore`。该接口只接受 Agent 已完成语义判断后的无效任务，校验 Issue、当前 run、项目和 Agent 归属后幂等标记 `done`；有效 Review/Approve 不调用。

该个人收口接口当前只支持单 Agent；squad 模式拒绝，避免关闭共享任务。Vercel 路由已迁入，其他部署入口需按实际代码单独核对。

不要把生产密钥配置到不可信分支的 Preview。若部署保护拦住 Slack/QStash，优先使用已配置的正式域名/生产部署；不要静默关闭项目全局保护。

## 5. EdgeOne

导入同一仓库，使用 Cloud Functions（Node.js），入口位于 cloud-functions/；不使用 Edge Functions 的受限运行环境。edgeone.json 设置消费函数60秒、海外新加坡区域和 public 静态目录。控制台需选择不含中国大陆的试验区域，以匹配海外依赖。

按当前平台支持选择 Node.js 版本；TypeScript 源码与依赖会由平台构建。配置与 Vercel 相同的变量，但使用本部署的 RELAY_CONSUMER_URL。

## 6. Cloudflare Workers

Worker 入口是 `src/cloudflare.ts`，配置在 `wrangler.jsonc`，使用同一套 QStash、Redis 和 `/api/` 路由。开发与构建默认 Node.js 24（最低 22.12），Worker 实际运行在 workerd；无需新增 Cloudflare Queues、KV 或 D1。

1. 安装依赖后运行 `pnpm exec wrangler login`，核对账号，并修改 `wrangler.jsonc` 的 Worker 名称。
2. 根据 [.env.example](.env.example) 配置 Relay 变量。普通 ID/URL 在 Wrangler `vars` 中维护，token/PAT/signing key 放 Secret Store，例如 `pnpm exec wrangler secret put SLACK_SIGNING_SECRET` 交互输入；不在 argv 或 Git 中放值。
3. `RELAY_CONSUMER_URL` 使用实际公开的 HTTPS `/api/queue/consume` 地址，与 QStash 验签 URL 完全一致；不得被交互登录或验证码拦截。
4. 执行 `pnpm test`、`pnpm lint`、`pnpm build:cf`。最后一项只构建，不发布。配置完成后执行 `pnpm deploy:cf`，再按下节逐阶段验收。域名变更需同步 consumer URL 和 Slack Events URL。

本地 `pnpm dev:cf` 可使用已忽略的 `.dev.vars`，但仍可能调用所配置的外部系统，应使用独立测试资源。Python reply adapter 与 ledger 在 Agent Runtime 中运行，不部署到 Worker。

## 7. 验收与比较

分别测健康请求、签名事件入队时间、队列到 Issue 的时间、Codex执行时间、最终Slack答复。浏览器访问快不代表Slack回调或入队快。

必须覆盖两个独立thread并发、同thread续问、重复投递、请求超时、创建响应丢失、失败保留、非允许频道/发送者拒绝、Runtime离线恢复。检查QStash失败队列，不能只看函数日志中的HTTP200。

队列中的消息包含Slack正文和附件元数据；Multica也保留内容。按实际需求设置访问权限与平台保留策略。Redis线程与消息状态保留90天，事件冻结正文保留24小时；超过保留窗口不保证重复判定；删除/更改Issue来源标识会影响恢复。

服务端创建/评论不提供完整的幂等接口。ambiguous\_\* 表示写入结果无法确认，需核对Multica；不要清空状态后直接重放。

## 验证记录要求

为每个平台分别保存 immutable commit、环境与地域、Slack 入站耗时、队列消费耗时、最终回复耗时和样本数量。完整回复包含 Agent Runtime 执行，不能只用该指标评定托管平台。

上线前至少回读：Slack Request URL 已验证、`RELAY_CONSUMER_URL` 指向同一部署、真实中文事件验签成功、owner 身份 reaction/回复正确、同 thread 追问复用 Issue、重复事件没有额外任务、临时 503 进入重试且 QStash DLQ 状态可见。Runtime 离线恢复必须单独实测，不能由普通队列重试或 HTTP 200 推断。

EdgeOne Cloud Functions 会把 `Request.body` 暴露为解析值，入口通过 `arrayBuffer()` 保留签名字节；Vercel 入口优先读取原始 Node stream。两边都不能用 `JSON.stringify(parsedBody)` 重建验签原文。

## 取消功能配置与验收

可选环境变量 `SLACK_CANCEL_KEYWORDS=cancel,取消`：未配置或空列表使用默认值；例如设置为 `stop,停止` 后只识别这两个词。Review 取消权限复用 `SLACK_TARGET_USER_IDS`，用户组本身不授予权限。Bug 取消仅允许原请求者或 `RELAY_OWNER_SLACK_USER_ID`，消费者在改动取消状态前回读原 Issue 核验。

继续使用现有 message 订阅。Review 取消清理使用 Rica 的 SLACK_USER_TOKEN；Bug 取消清理使用 Rbot 的 SLACK_BOT_TOKEN。两端身份须分别一致，其他身份的 reaction 保留。无需 reaction 事件订阅。

在专用测试 thread 中启动任务，再由配置的用户回复 `@目标 取消`。检查运行状态变为 cancelled、原触发消息上所选 token 身份的 reaction 被清除、其他身份的 reaction 保留；已完成任务不应被改写或清理。取消结束后发送一条新的任务 mention，检查继续原卡。非授权用户发送取消指令应被忽略。

消费失败仍使用现有 QStash 重试与 DLQ：`cancellation_pending` 表示建卡/运行/终态仍待确认；`reaction_cleanup_failed` 表示任务可能已取消但清理未完成。补齐权限或修复上游后，重放原消息会从保存的阶段继续。`cancellation_new_run` 或 `cancellation_run_missing` 需要先人工核对 Multica，不应通过删除 KV 状态强行重建任务。

### Reaction token 配置迁移

旧部署的 `SLACK_REACTION_READ_TOKEN`、`SLACK_REACTION_TOKEN`、`SLACK_REACTION_NAME` 不再读取。上线前配置 Rica User 的 `SLACK_USER_TOKEN`；Bot token 保留供独立 Bug Resolver 回复使用。新部署成功后才清理旧变量，保留回滚能力。

## 单条事件与可靠交付

Relay 将当前 mention 转交给 Agent，上下文由 Agent 按现有 Prompt 和 Skills 读取。每条事件只冻结其正文、附件安全元数据和模型配置快照，24 小时内相同事件重试复用；新的 mention 单独准备输入。已保存的 Issue/comment 通过稳定 marker 搜索恢复，不扫描整个 Project，旧 marker/裸 JSON 仍可恢复。

附件在入队前最多保留五个，字段为 `id`、`name`、`mime`、可选 `size` 和 `contentStatus: not_loaded`；private URL、缩略图、shares 和文件内容不进入 QStash/Redis 事件副本。字段长度有上限，超出数量时标记 `filesTruncated`。完整事件 envelope 上限 48 KiB，任务展示上限 64 KiB，引用展示最多 4 KiB。正文保存在 QStash、24 小时冻结副本及 Multica，应分别限制访问。

消费者遇到无效事件、损坏线程状态、scope 不匹配、映射歧义、确定性体积超限或 comment 查找超限时，返回 `action: rejected`、`retryable: false`，停止 QStash 重试。暂时性和结果不明的失败返回 503；耗尽后由 DLQ 保留处理责任。`created`、`comment_persisted`、Agent 完成和 Slack 送达分别验收。

## 可选确定性回复 adapter

个人 Review 默认按 AGENT-PROMPT 的 Slack 发送流程。需要代码渲染和持久发送去重时，将最终发送步骤替换成 `scripts/slack-reply.py`；只用一个入口，不再同时调用 Slack Skill 的 send。

在 Agent Runtime 创建 owner-only 的私有 JSON，填写从 CLI 核对的真实 ID 和 server 地址，例如：

```json
{
  "displayName": "🤖 自动化助手",
  "agentId": "<Agent UUID>",
  "workspaceId": "<Workspace UUID>",
  "projectId": "<Project UUID>",
  "teamId": "<Slack Team ID>",
  "serverUrl": "https://multica.example.com",
  "icons": {"time": "⏱️", "model": "🤖", "tools": "🔧", "skills": "🪄", "github": "🔗", "multica": "↗️"}
}
```

`icons` 可省略或用当前 workspace 已确认存在的 shortcode 覆盖；`displayName` 是固定 attribution。该 JSON 不包含 token，放在仓库外或 `.private/` 中。adapter 使用 Runtime 已认证的 Multica CLI 和 `SLACK_USER_TOKEN`；后者需要 `chat:write` 及读取原 thread 做发送后核验的 history scope。

```bash
python3 scripts/slack-reply.py \
  --config /path/to/private/reply.json \
  --issue-id '<当前 Issue UUID>' \
  --text-file /path/to/private/body.txt \
  --run-context-file /path/to/private/context.json
```

`context.json` 沿用本仓库 `run_context.py` 的采集结果。回复 follow-up 时使用本次触发的 `--comment-id`；有已核验成果时加 `--github-context-file`。路由从指定来源 Issue/comment 回读，校验 Workspace、Project、Agent 和 Slack Team，不从正文猜测目标。先用 `--dry-run` 检查 payload；它不会发送 Slack 消息。

GitHub context 使用 `version: 1`、`pullRequests` 数组及可选 `branches` 数组，合计最多五项。分支项包含 `repository`（owner/repo）和 `branch`；PR 项还包含 `number` 及对应的完整 GitHub `url`。只有实际业务证据支持的成果才写入，不能从截断或未配对日志猜结果。adapter 渲染统计及任务入口、各 PR/分支行和固定 attribution，fallback 包含同样信息。风格和正文表情仍由原最终回复 Skill 决定，adapter 不加载个人风格文件。

adapter 在私有 JSON 旁的 `.slack-reply-state/` 保存 owner-only ledger，每个来源 Issue/comment 对应固定 delivery marker。POST 前持久化 `attempting`；收到 message timestamp 后记为 `accepted`，原 thread 回读成功才进入 `sent`。`sent` 重跑复用已确认结果；结果不明时查找原 marker，未找到则返回 `slack_delivery_unknown`，不自动再发。需独立核对原 thread 才能清理未知状态；同源并发返回 `reply_delivery_busy`。

发送错误按 [Slack 错误契约](https://docs.slack.dev/reference/methods/chat.postMessage/#errors) 分类：只有已知明确的鉴权、权限、参数或目标拒绝才清理发送 intent。`internal_error`、`fatal_error`、其他服务端/未知错误、非 429 HTTP 错误和损坏响应保留 `attempting`，下一次先回读，不能盲目再次 POST。

HTTP 429 或 JSON `rate_limited`/`ratelimited` 表示该次发送被限流，ledger 记为 `rate_limited` 并保存 `Retry-After` 对应的 `retryAt`。CLI 返回 `slack_rate_limited`、`retryable: true` 和 `retry_after_seconds`；调用方等待后重新执行同一发送命令，等待期内 adapter 不调用 Slack，到期后可再次发送，无需手动清 ledger。缺失或无效的 Retry-After 采用 60 秒冷却；CLI 不自行睡眠或循环。回读时遇到限流仅要求延后回读，原 `accepted`/`attempting` 状态保持不变。

复制脚本、修改文档或合入 PR 都不会自动启用 adapter。采用后需同步最终回复指令并回读绑定，再以新任务验证一次发送和后续重试。adapter 成功不代表业务任务已经完成。

## Rica Bug Resolver 接入状态

已实现 `message` / `app_mention` 统一入口及 `@Rbot` 独立分路。配置 `.env.example` 的四个 Bug 路由字段与 `SLACK_BOT_TOKEN` 后，显式 @Rbot 优先进入 Bug Resolver；其他 Review mentions 与发布候选仍进入原目标。不要把 Bot User ID 加入 Review targets。当前 Rbot 继续 HTTP Events，不切 Socket Mode。

Bug 消息使用独立 `taskType=bug_resolver`，旧消费者会拒绝而不会重解释为 Review。新队列消息固定 `routeId` 与目标指纹（服务、工作区、项目、执行者与 scope）；消费时目标变化则丢弃，不能自动改投。旧的无 route 字段队列消息仍按 Review 处理。同消息的 `message` / `app_mention` 使用同一个 team/channel/ts 去重键；Bug 与 Review 的队列串行键及任务 scope 独立。禁用 Bug 路由时，清空全部四个 Bug 字段；待消费的旧 Bug 消息会被忽略，不会落入 Review。

当前 FinTech 配置：`SLACK_BUG_BOT_USER_ID=U0AM1GJ8GMQ`、`MULTICA_BUG_WORKSPACE_ID=12ae138f-6c79-4aff-84ac-b0393a4834b2`、`MULTICA_BUG_PROJECT_ID=4e5fa137-7111-4355-82db-25268033f6a3`、`MULTICA_BUG_AGENT_ID=b198894c-31f5-4ecb-ac84-4a4a6f90abb9`。Bot token 从外置配置安全输入，不能写入 Git。旧部署若没有 `SLACK_ALLOWED_CHANNEL_IDS`，迁移时显式设为 `all` 以保留旧行为；不要覆盖或清空既有敏感变量。

Vercel 上传前执行 `deploy --dry --json`，核对 `.vercelignore` 已排除 `.private/`、凭据、GitNexus 索引与本地运行状态。生产候选使用现有项目和 `--prod --skip-domain`，健康与配置检查完成后再 promote，保持原 Slack Request URL 和 QStash consumer URL。回滚使用切换前已回读的生产 deployment ID，不删除 Redis 旧 scope。实际部署与验收状态见 [准备记录](docs/bug-resolver-readiness.md)。
