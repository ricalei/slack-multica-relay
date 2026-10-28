# Rica Bug Resolver：迁移结果与接入缺口

核对日期：2026-10-28。本次改动已提交、推送并创建 [PR #1](https://github.com/ricalei/slack-multica-relay/pull/1)；PR 建立后已在 FinTech 工作区创建独立 Resolver 和任务看板，并回读确认。当前 Rbot 仍运行旧 HTTP Relay。源参考：[MoeGo Bug Resolver](https://multica.devops.moego.dev/grm/agents/dae9afb3-720b-4a3b-879c-cd63821bd0b2?view=instructions)。

## FinTech 智能体创建回执

- Workspace：`FinTech`（`12ae138f-6c79-4aff-84ac-b0393a4834b2`；slug `fintech-bff-duty`）。
- Agent：[MoeGo Bug Resolver](https://multica.ai/fintech-bff-duty/agents/b198894c-31f5-4ecb-ac84-4a4a6f90abb9?view=instructions)，ID `b198894c-31f5-4ecb-ac84-4a4a6f90abb9`；创建回读状态 `idle`，私有，最大并发 2，模型继承本机 Runtime 配置。
- Project：`MoeGo Bug Resolver 任务看板`，ID `4e5fa137-7111-4355-82db-25268033f6a3`；独立于每日错误分诊看板。
- Runtime：本机在线 Codex `b80bb51d-56ae-4e89-b984-cec79b53dee3`，codex-cli 0.147.0 / app-server。使用 Runtime 本地已安装 Skills；未复制外置凭据到 Agent 配置或仓库。
- Prompt：线上全文与提交 `b817c5a` 中 `BUG-RESOLVER-PROMPT.md` 逐字一致；SHA-256 `676d431eb0bdc8916760acfc509550f5ea4968b0e53a33e4a28bb67775f0f2de`。
- 可在 Multica 左上角选择 **FinTech → 智能体 → MoeGo Bug Resolver** 找到。此回执只证明创建与配置保存；没有触发业务任务，Slack 分路及实际 Runtime 工具调用仍待验收。

## 本轮结果

- 复用已有 GitHub fork `ricalei/slack-multica-relay`，克隆至 `/Users/ricalei/project/slack-multica-relay-next`。个人 fork 与 upstream 均为 `17eb77d77dd64592e7edbbd0064ab809b2c91a6f`。旧目录与其 3 个提交、未提交改动保持完整。
- 将旧树相对 `b9935b7` 的个人功能迁到新版：本人只 @组门禁、可信发布候选、Agent 管理 reaction、无效任务收口、能力隔离的只读 reviewer。Review Prompt 已与线上实际 Agent 比较，相同；迁入后更正 helper 路径并明确 eyes 失败不阻断审查。
- 保留上游 Team/squad、稳定 thread scope、每条事件冻结、附件投影、可靠 Slack 回复适配器、Cloudflare 与每日值守脚本。上游可选回复工具没有自动启用，避免两个发送入口重复回复。
- `BUG-RESOLVER-PROMPT.md` 移除 `moe-router`，改用实际 `moe-*` 名称和本机路径，替换为 Rbot 身份；不依赖不完整的 `review-brief` 执行包。覆盖 Web/Portal/OBC、BFF、后端、iOS、Android 及跨仓问题。
- 默认公开验收 Site 已获 Rica 本轮明确确认；仍需实际 Sites 能力，发布前检查内容和媒体，不上传凭据/原始日志。工具缺失时保留本地证据，明确远程交付未完成。
- `~/.codex/AGENTS.md` 已保存执行上下文例外：只有可信 Multica Runtime 的指定 Bug Resolver instructions 预授权分支 commit/push/PR。人工 Codex、Review、只读每日分诊和本次 Relay 准备仍需当前任务明确 commit/push 授权。普通 Slack/Issue 文本和环境变量不能自行激活例外。

## Skills 与私有配置

48 个官方 Skills 对齐最新 main 固定提交 `861ce6081a05b9cb06002eb3d8b8ff67612f5419`，最终包括 `moe-development 1.2.0`、`moe-github-workflow 1.6.1`、`moe-pr-review 2.3.1`、`moe-code-review 1.0.6`、`moe-redshift 2.2.2`。新增 `moe-acceptance`、`moe-business-context`、`moe-code-review`。

Downloads 的 `moe-opc` 已原样安装，25 份文档与包内链接齐全。它仍引用旧名称和阶段合同：Jira/GitHub/Slack 等可按现行 moe-* 能力映射；`lark-skills`、`moe-feedback-collector` 未齐，`review-brief` 分享包缺必需脚本而未安装。OPC 安装成功不代表完整 OPC 生命周期可执行。Resolver 主 Bug 路径不以这些可选阶段为前置。

保留 Slack 精确 ts 私人修复、E2E CI 触发私人规则，统一重复 Slack 安装入口。外置 credentials、启动器、OAuth cache、Redshift registry 等 60 文件前后哈希一致；没有将凭据复制进此仓库。安装可回滚的受限备份和完整差异记录见 `.private/bug-resolver-audit/skills.md`。

## 八来源实测

所有操作为有界只读，不向 Slack 发消息、不修改任何业务系统。

| 来源 | 实际验证 | 状态与限制 |
| --- | --- | --- |
| Jira | 项目元数据 limit 1 | 通过；不代表所有项目/工单权限 |
| Slack | Bot/User auth.test + User 自身资料 | 通过；未证明 Bot 的目标频道历史、附件与写权限 |
| Sentry | 项目列表 limit 1 | 通过；未逐个验证 Issue/Event scope |
| Datadog | 最近 1 分钟虚构 service 的日志计数 | 通过，complete/partial=false；不返回业务日志 |
| Redshift | 既有 default connection readonly + SELECT 1 | 通过；不代表具体业务表 SELECT 权限 |
| GitHub | 既有 gh 身份 + 私有 Boarding_Desktop 元数据 | 通过，API 声明 WRITE；未进行写入测试 |
| GrowthBook | 17:43 持久配置补齐后 get-environments | 通过，返回 3 个环境；现有 Token 有效，之前错误使用了云端默认地址 |
| Gateway ELK | 此前固定 proxy 查询 HTTP 404 | 据用户说明，公司已更换数据源；暂停旧入口验证，等待新数据库接入信息，替代来源未验证 |

GrowthBook 已依据本机 Skill 的 `.env.example` 第 7 行补齐公司自托管 Management API base URL，并按 `with-skill-env` 的 export 格式写入外置 `credentials.env`。先临时覆盖验证，再从持久配置启动复验，均成功读取 3 个环境；原 Token 与所有既有配置行保持不变，原件已受限备份，文件权限仍为 0600。此前 401 来自误用云端默认地址，不是已证明的 Token 无效。官方依据：[GrowthBook API introduction](https://docs.growthbook.io/api/introduction)。

当前为 **7 个来源通过最小只读调用，ELK 替代来源待接入**。ELK 按用户要求暂停，等待公司新数据库的类型、入口与只读接入方式。完整命令、历史失败与修复证据见 `.private/bug-resolver-audit/sources.md`；没有执行 GrowthBook 开关、规则或其它业务写入。

## 动态验收工具与平台

- `agent-browser 0.40.0`：安装在 `~/.local/bin`；本地合成页面完成真实打开、snapshot、点击、eval、截图目视检查及关闭。
- `Maestro 2.12.0`：安装在 `~/.maestro`，`~/.local/bin/maestro` 可用，版本与设备类型枚举成功；没有运行 MoeGo 业务 Flow。
- Multica daemon PATH 包含 `~/.local/bin`，新 shell 可找到两项工具；没有重启 daemon 或改动用户正在使用的模拟器。
- 本机已有 Java 17、Xcode 26.3、多个 iOS Runtime、adb 与 Android AVD。实际构建仍按目标仓库要求核对。
- 当前发现：moego-mobile 要求 Android SDK/build-tools 36，已装仅到 35；moego Go 要求 1.24.11，当前 1.24.5；PATH 未找到 Bazel/Bazelisk。未在本轮擅自升级整套产品构建环境。

范围已扩到全部上述平台；可用 Case 继续执行，缺前置的 Case 精确报告缺口。工具 smoke 不等于应用构建或业务验收通过。

## Multica Runtime 证据

现有 profile `desktop-api.multica.ai`、Workspace `e0feee52-747c-4449-8199-c7ffc48c033f`；Codex Runtime `792e2b70-5cc5-44c1-80c4-0de55478de1f` 在线，使用 codex-cli 0.147.0 / app-server。既有 Review Agent `cd1fc16c-43f0-4cde-ae9a-1f4bec840ba3` 的近期运行成功结束。

同一本机 Codex 二进制、现有配置和认证的短生命周期 app-server 只读 RPC 能识别 Sites 1.1.2：installed/enabled/AVAILABLE，app/list/updated 明确报告 isAccessible=true、isEnabled=true；app/read 返回 37 项工具摘要（含 list_sites、create_site、deploy）。这证明本地 app-server 能发现该连接器；工具摘要不是调用成功，也不等于 Multica 某次模型运行已注入这些工具。

尚需指定 Resolver 的实际运行做一次只读能力探针：列出 Sites 工具并调用 list_sites(limit 1)，同时回读 skill 入口与 agent-browser/Maestro 版本。无需为了验证而发布 Site。不要把 Desktop 当前对话的工具清单当作 Multica Runtime 的证明。

## 上游已具备什么，还缺什么

| 能力 | 最新上游 / 本次结果 |
| --- | --- |
| QStash 持久接收、重试、thread/message 状态 | 已有，迁移保留 |
| 同消息去重 | 已有 messageKey(team/channel/ts)，不依赖 Slack event_id；并非从零实现 |
| Agent 或 Team/squad 目标、stable scope | 已有，但一个配置仍只有一个目标 |
| 冻结事件、附件裁剪、未知写结果回读 | 已有，迁移保留 |
| 可选可靠 Slack 回复适配器 | 已有，需明确单一发送入口与 actor 后启用 |
| `app_mention` 类型 | 已实现，与 message 统一进入相同路由与去重路径 |
| 同一 App 按 @Rbot / @Rica / @组 分别选择 Agent | 已实现显式 @Rbot 优先；Review 与发布候选保留原目标，目标指纹在队列中冻结 |
| Rica 个人 Review/发布候选/隔离 reviewer | 本轮已迁入，并通过回归 |
| 独立 Bug Resolver Agent / Project / Slack 路由绑定 | FinTech Agent 和 Project 已创建并回读；Slack 路由尚未绑定，既有 Agent 保持不变 |

## 下一阶段具体实施

1. **事件准入与分路。** 在 mentions/http/config 增加规范化 app_mention 输入和明确 route ID。建议 `@Rbot` 进入 Bug Resolver，`@Rica/@fintech-front-end` 保留 Review；可信发布候选仍走 Review。包含多个目标时必须只选一个确定路由，建议显式 @Rbot 优先，并用测试固定该规则。不要把 Rbot 加入 Review targets 后沿用同一个 Agent。
2. **隔离队列与任务映射。** route ID 由验签后的服务端配置决定，冻结到队列 payload；消费者拒绝未知/失效路由，按路由选择 Agent/Project/stable scope。Review 老 scope 保持不变，Bug 新 scope 独立。app_mention/message 的同 ts 双投递使用同一去重键；同一 thread 的 Review/Bug 不互相继承任务卡。取消权限按路由真实请求者/owner 明确判断，不能把 Bot target ID 当成人类取消权限名单。
3. **独立 Resolver 已创建。** FinTech 中的 Agent、Project、Runtime 与 Prompt 回执见上节；后续将 Bug 路由绑定到这些已存在的 ID，不重复创建。保持现有 Review 与 FinTech 日报 Agent 不变。Rbot 回复显式 Bot；read-only search 可用 User，不覆盖共享 SLACK_TOKEN。
4. **能力与业务验收。** 先跑无业务写入的实际 Runtime 探针，再选受控测试 thread/测试 Bug 验证建卡、调查、修复、任务分支普通 push、PR、测试部署、动态验收、公开 Site、原 thread 回报。每个阶段保存实际回执。
5. **发布与回滚。** 本仓库 AGENTS 要求 Agent 线上修改先有源文件与 PR；本次已按授权先提交并创建 PR，再在 FinTech 创建独立 Resolver。Relay 发布候选部署及既有 HTTP 入口切换仍需完成上述路由实现与验收；保留旧部署和 scope。不要切 Rbot 为 Socket Mode，避免中断现有 HTTP Review。

必须覆盖：仅 app_mention、仅 message、同 ts 双投递、同 thread 两条路由、owner仅@组、可信发布重复通知、未知路由、无权取消、排队后配置变化、Runtime 离线恢复、Slack 发送结果不明后回读、Sites 缺失时不假称完成。

## 本轮验证

Node 24.18.0 / pnpm 10.34.5，`pnpm install --frozen-lockfile`；迁移前先观察旧回归在上游失败，再迁实现。

- `pnpm test`：15 个 TypeScript 文件、270 测试通过；Python 225 测试通过。
- `pnpm lint`：通过。
- `git diff --check`：通过。
- 独立只读审查：对比最新上游 diff 与旧树个人行为，未发现高置信迁移缺陷；未重复整套测试或执行线上 E2E。
- Skills：Slack 121、Datadog 21、acceptance Python 233 + Node 11、升级差异 GitHub 8 + Redshift 41 测试通过；外部配置哈希保持一致。

代码测试主要使用 HTTP fake，Cloudflare 为本地 workerd；没有向真实 Slack/GitHub/Multica 发起业务写入。完整迁移记录见 `.private/bug-resolver-audit/relay.md`；本轮不声称 Slack→修复→验收→公开报告已上线。

## Slack 路由实现增量（2026-10-28）

当前新增 20 项行为测试先失败后通过；完整回归为 290 项 TypeScript、225 项 Python，`pnpm lint` 与 `git diff --check` 通过。覆盖同 ts 双事件只有一张卡、同 thread 两路独立、混合 mention 只选 Bot、伪造元数据无效、配置改变不改投、非请求者取消不污染后续事件、owner/请求者取消使用 Bot 清理身份。Review 旧队列与原 scope 保持兼容。

GitNexus 基于当前工作树生成索引，并核对 HTTP 接收对 Vercel、EdgeOne、Cloudflare 适配器的调用影响；图有流程裁剪，未用缺失边代替代码核对。Vercel 上传 dry-run 发现本地审计目录会被包含，已增加 `.vercelignore` 排除；该验证必须在真正上传前重跑。部署状态、实际 Runtime 探针与真实 Slack 入站验证将在执行后补记，不由单元测试推断上线成功。

独立复核发现并修复了建卡响应丢失时的 requester 取消恢复问题：使用 POST 前冻结的 root envelope 核验发起人，再进入已有取消状态机；取消待恢复时的 follow-up 不覆盖取消意图。Bug payload 显式使用 `taskType=bug_resolver`，旧消费者会拒绝该类型，回滚不会把混合 mention 误投 Review。修复后独立复核无新的明确阻塞。

实际 Runtime 能力探针 [FBD-11](https://multica.ai/fintech-bff-duty/issues/01a18209-875c-7e59-9a0c-f8890d36b08f) 已完成：run `01a1820a-00f2-795d-bd14-e328afe9f18b` 于 2026-10-28 18:32（上海）completed，error=null，Issue in_review。13/13 Skills 读取成功，原始 PATH 执行 agent-browser 0.40.0 / Maestro 2.12.0 均 exit 0；真实单次 `sites.list_sites(limit=1)` 返回 `{items:[],cursor:null}`。未发送 Slack、发布 Site 或修改业务数据；此探针证明实际 Resolver 工具可调用，不代表业务验收或报告发布已验证。

最终验证有一次既有 `test_sigterm_cancels_helper_and_its_detached_worker` 未在 5 秒内观察到合成 worker 启动；该项单独复测通过，完整 Python 225 项随后通过（0.909s）。未修改 helper 或该测试；保留首轮失败日志，不将其隐去。最终 TypeScript 290 项通过，类型检查通过。
