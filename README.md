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

## 每日值守调查闭环

每日流程保留两个 Agent：「GRM 每日 Bug 值守」和独立的「GRM 每日值守看板」。值守 Agent 每天北京时间 03:00 按固定阶段执行：三源采集 → 固定本轮清单 → 全部逐条调查与留档 → 汇总结论 → 固定修复队列 → 逐条完整交付。看板 Agent 的规则、脚本和 10:00 定时保持原样。

值守规则在 [DAILY-BUG-DUTY-PROMPT.md](DAILY-BUG-DUTY-PROMPT.md)，扫描 Autopilot 的任务正文在 [DAILY-BUG-SCAN-PROMPT.md](DAILY-BUG-SCAN-PROMPT.md)。采集按每来源最后成功日期补到昨天，已成功窗口复用。调查只补读指定原帖、会话或 thread，不调用日采集，Facebook、Intercom 不重复采集。

每轮扫描用 `dailyRound` 冻结疑似 Bug 的数量和 candidateId 集合摘要，清单与完整调查过程放真实附件；metadata 只保留状态和回执。每条候选登记为原扫描下的调查子任务，`dailyInvestigation.roundId` 与 sourceScanId 指向当前扫描。确认一项后继续调查下一项，不能提前开始修复。每条确认、排除、重复、已有工单或外部缺证结论都须有可回读的报告；未调查、技术失败和泛化缺口不算完成。

全部候选有依据的结论与完整调查索引回读后，才能设置 investigationComplete；三源覆盖完整后才建立原扫描的固定 dailyBatch。Bug 子任务用 investigationIssueId 对应调查项，全部确认项登记并回读后固定 total、bugIds 和当前位置。同一个值守 Agent 沿用现有修复、CI、动态验收、最终 HTML、PR 与交付回读门槛逐项执行，完整交付后推进固定队列，最后结束本轮。

真正外部缺证必须保留原来源、实现、数据与验证路径的实际尝试、缺失材料和恢复条件，作为已调查结论保存在任务、完整报告与分类台账；值守 Agent 的 Slack 汇总不单列这些内容或数量，也不另发通知。不能把尚未调查项留在候选附件后报告扫描完成。旧扫描、旧 pending-candidates 和历史待调查已停止，仅用于追溯、查重，不自动导入新轮次。停止不删除证据，不改写已有修复与交付结果。

`scripts/duty-queue.py` 通过原生 Multica CLI 完整分页读取现场，生成 protocolVersion=2 的只读计划。计划器核对固定身份集合、每条调查依据与报告回执、全轮索引、来源覆盖、确认项与修复清单，以及最终交付版本；一项确认不能越过其余未结束调查。脚本不写任务或启动运行，真实动作仍由 Agent 按“重读 → intent → 原生启动 → runId 回读 → 结束当前运行”执行。未知写入结果先查现状，不重复创建或派发。

```bash
python3 scripts/duty-queue.py \
  --server-url https://multica.devops.moego.dev --workspace-id <GRM 工作区 UUID> \
  --project <值守项目 UUID> --agent <值守 Agent UUID> --current-issue <当前 Issue UUID>
```

隔离验证可传 `--snapshot <脱敏 JSON>`，输入为 `{"issues": [...], "runs": [...]}`，不访问服务。定向检查为 `python3 -m unittest tests/duty_queue_test.py`。

线上同步先创建流程 PR，再按 AGENTS.md 核对线上 instructions 和 Autopilot description，仅更新对应字段及扫描的 03:00 Asia/Shanghai 定时。Prompt 中的 `<调度器提交 SHA>` 绑定本 PR 已推送并回读的完整提交 SHA，运行时精确 fetch/show 此版本，避免 PR 尚未合入时误用旧 origin/main 调度器；后续改动同步时重新绑定版本。保留值守并发 1、Runtime、模型与环境；看板配置不变。配置回读不等同于真实每日周期完成，不为验证配置擅自触发新采集。

## 每日值守看板

`scripts/duty-board.py` 只读汇总「GRM 每日 Bug 值守」的扫描批次漏斗：读取值守项目任务、字段台账任务 GRM-664 的 `<!-- duty-board:v1 -->` 评论、GitHub PR 的合并状态、Slack 交付 thread 上审查人的 🚫，以及下节的回告记录与回应跟踪，输出 `board.json`、`board.md`、`slack.md`，以及可直接打开、支持切换反馈日期的 `board.html`。「GRM 每日值守看板」Agent（Prompt 真源 [DUTY-BOARD-PROMPT.md](DUTY-BOARD-PROMPT.md)）由 Autopilot 每天 10:00 运行 `origin/main` 版本，发送到指定交付频道、在该消息 thread 上传 `board.html`，并追加到 GRM-665。本地预览：

```bash
python3 scripts/duty-board.py --project <值守项目 ID> --ledger-issue GRM-664 --start 2026-10-12 --reviewer <审查人 Slack 用户 ID> --canny-account-ref <Canny 内部测试账号 aid> --output-dir <输出目录>
```

HTML 默认展示昨天的反馈日，可在顶部切换 d1 起的任意反馈日（例如 `2026-10-13`）。选择项与趋势行同时标出扫描日和反馈日（如 10-13 扫描、10-12 反馈）。切换后同步更新该扫描批次的漏斗、截至所选反馈日的累计与 Bug 范围、每日趋势和该日逐条记录；历史扫描证据会内置在同一份 HTML 中，离线打开也可切换。Bug 的修复、上线与回告进度采用生成时的最新状态，不表示历史时点快照。未扫描或证据索引不可用的日期会显示具体原因（下载失败时含命令退出码与 stderr 摘要，默认日期的原因同时输出在 stdout 的 `scanNote`）。默认日期的索引可用 `--scan-index <scan-evidence-index.json 或证据 zip>` 传入已下载的文件，看板 Agent 即按此方式先自行下载再交给脚本；其它日期仍由脚本从扫描任务附件下载。Markdown、Slack 与 JSON 仍只输出默认日期。

确认可修按 Bug 的 `dailyBug.batchId`（或父任务）所属扫描批次归组，调查后新建的修复批次通过 `dailyBatch.sourceScanId` 和真实父任务回溯原扫描，使用原扫描反馈日，保留原反馈日作为追溯字段；不使用客户原帖时间或 Bug 创建时间归组。范围外批次不计入明细、累计或回告跟踪，缺少批次关联时告警并排除，不再回溯补入“试运行”。确认可修的数量包含后续被驳回的方案；已交付 PR 也保留这些方案的 PR。关联 PR 全部 merged 才计入“已上线”，部分 merged、open、closed 未合并均不算；已全部 merged 时历史 🚫 不覆盖合并结果。

## 上线回告

Bug 上线后告知反馈方。AI 只起草，每次对外发送都由审查人把关；值守 Agent 起草与发送的执行规则见 [DAILY-BUG-DUTY-PROMPT.md](DAILY-BUG-DUTY-PROMPT.md) 第 4 节，看板 Agent 的回应判断见 [DUTY-BOARD-PROMPT.md](DUTY-BOARD-PROMPT.md)。

**流程与分工。** 看板把已上线、未标 🚫 且台账无回告记录的 Bug 列为「已上线待回告」。审查人在该 Bug 的 Multica 任务评论以下首行指令，唤醒值守 Agent；这些评论不算返工，不动修复队列。

| 首行评论 | 值守 Agent 的动作 |
|---|---|
| `回告` | 核对已上线后起草英文回告，以 Bot 回复到该 Bug 的 Slack 交付 thread，不 @ 任何人；第二行起可写起草要求 |
| `确认发布` | 仅 Intercom：以审查人 Slack 身份把已审核草稿原文私信给 CS，英文验收报告附在私信 thread；第二行可附私信链接 |
| `已回告` | 审查人已自行发送（Canny、Facebook 原帖留言），记台账 |
| `不回告` | 其后写原因，记台账 |

| 来源 | 回告对象与形式 | 发送人 |
|---|---|---|
| Intercom | 处理该会话的 CS（intercom skill `get-teammates` 的主要客服，按邮箱经 slack skill `resolve` 找到 Slack 用户）；草稿为给 CS 的英文私信，附英文版验收报告 | `确认发布` 后由 Agent 以审查人身份私信 |
| Canny | 原帖下的英文留言 | 审查人自行发送 |
| Facebook | 原帖下的英文留言；只能读 Slack 汇总消息，原帖由审查人自行找到 | 审查人自行发送 |

`确认发布`、`已回告` 完成后，值守 Agent 以 Bot 在该 Bug 交付 thread 根消息加 📤，`不回告` 加 🔕，便于在频道中直接看出回告状态；状态仍以台账为准，看板不读这两个表情。

私信会话用审查人提供的链接，或用审查人 token 搜索与该 CS 的已有私信；审查人 token 不能新开私信，搜不到时请审查人在 Slack 打开私信后附链接。

**台账记录。** GRM-664 按「任务编号 + kind」各取最新一条：

| kind | 写入方 | 写入时机与字段 |
|---|---|---|
| `notify` | 值守 Agent | `确认发布`、`已回告`、`不回告` 完成后：`state`（`sent` / `skipped`）、`at`（发送或标记时间，回应跟踪起点）、`via`、`message`、`cs`、`reason`。起草不写台账 |
| `notify-followup` | 看板 Agent | 回应判断后：`notifyAt`（对应的回告时间）、`judgedThrough`（已判断到的内容时间）、`respondedAt`；Intercom 另有 `csInformedAt`、`csInformedPartId`，Canny 另有 `baselineScore`、`respondedCommentId` |

**回应识别。** 看板从 `notify.at` 起跟踪 14 天，识别到客户回应或到期后不再读取来源：

- Intercom：看板 Agent 判断回告后 CS 的新公开回复是否明确告诉客户问题已修复上线；判定告知后，客户再有消息才算回应。
- Canny：经 moe-feedback-collector `canny-post --include-text` 以内部测试账号只读读取帖子；回告后的新公开评论由看板 Agent 按语义判断是否为客户回应（我方留言、员工跟帖不算）。首次跟踪记录票数基准，票数变化只作参考。
- Facebook：不跟踪回应。

**看板展示。** 漏斗「已回告」「用户回应」两列；Bug 明细「回告」列显示待回告、不回告、待 CS 告知客户、待客户回应（Canny 附票数变化）、客户有回应、已截止；「需要关注」与 Slack 版列出「已上线待回告」和近 1 天的「已回告有新回应」。
