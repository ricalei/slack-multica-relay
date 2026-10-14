# GRM 业务上下文 Skill 改名

将 `gather-moego-context` 迁移为 `moe-business-context`。本目录交付经过 PR 审阅的线上逐字段候选；合入本仓库不会自动调用 Multica 或部署 Relay。

## 对象与内容基线

服务及 GRM 工作区、对象 ID、读取时间、字段前后 SHA-256 与替换次数见 [targets.json](targets.json)，逐字段差异见 [changes.patch](changes.patch)。哈希按原始 UTF-8 字符串计算，不 trim、不规范化换行。`file:` 字段指 Skill 内相对路径，发布前按路径重新解析文件，不能复用列表下标。

| 对象 | 动作 |
| --- | --- |
| GRM 的 gather | 原 ID 更新 name 与正文 frontmatter name |
| GRM 的 `analyze-jira-prd` | 更新 description 与正文中的旧名称及链接 |
| GRM 的 `moe-opc` | 更新正文、两份 references 与已有 eval 中的旧名称 |
| `Slack Task Router (Codex)` | 当前 instructions 无旧名称，保持原样；验证新名称可加载 |
| `MoeGo Bug Resolver` | instructions 只替换旧名称，仓库源文件同步为 `BUG-RESOLVER-PROMPT.md` |

Multica 的 gather 是线上现有单文件版本，个人仓库的版本已有三份 references。本次逐对象以线上原文为基线替换名称，不执行整包刷新或借改名升级内容。所有 Skill ID、绑定集合及其他字段保留。共享 Skill 的新名称会反映到既有绑定；其他智能体的 instructions、Winches Lab 和其他成员 Runtime 不在本次修改与验收范围。

## 切换前

1. 先合入本 PR，使候选可回读；个人 skills 的配套改名 PR 在实际切换窗口合入。个人仓库 pull 会触发 SkillDeck 同步，不能提前移除本机旧入口。
2. 使用原生 `multica` CLI，按根 AGENTS.md 核对服务、GRM、两个目标 Agent 及各自 Runtime。用 `agent tasks <id> --output json` 检查活动运行；选定目标没有在途任务且暂不接入新任务的窗口。无法安排时保持现状，不取消既有运行或操作其他智能体。
3. 回读清单内 Skill 的全部内容和文件，以及两个目标 Agent 的 instructions 与绑定。保存本次字段、文件清单、绑定 ID、Runtime 和时间快照到仓库外；本机目录和链接的备份由配套个人 skills 迁移说明负责。
4. 为每个待改字段检查原始字节哈希。等于 `before_sha256` 时，按 `replacement` 生成候选并验证 `after_sha256`；已等于 after 的字段记录为已完成，不能再次作为本次新增写入。其他情况停止并重新形成差异，通过 PR 审阅后继续，不能覆盖并发更新。
5. `Slack Task Router (Codex)` 的无变更检查使用清单里的 instructions 哈希。其内容或 Runtime 变化时重新核对；新内容若增加旧名引用，应先更新候选 PR。

## 发布顺序与原生命令

先按配套 PR 完成本机安装和链接迁移，确认新 Skill 目录及 references 可读。再依次更新线上 gather、两个上传的调用方 Skill，最后更新 Bug Resolver instructions。每个字段写前再次回读并校验；每次写入后按同一对象 ID 回读确认。三个 Skill 的 config 当前均为空，无需提交 config 参数。

使用 `targets.json` 中已核对的 server、workspace 和对象 ID，每条命令显式指定 `--server-url`、`--workspace-id`。只传本次修改字段；下列是命令形态，文件为从本次快照生成且通过哈希校验的 UTF-8 候选：

```text
multica ... skill update <gather-id> --name moe-business-context --content-file <候选正文> --output json
multica ... skill update <analyze-jira-prd-id> --description <候选描述> --content-file <候选正文> --output json
multica ... skill update <moe-opc-id> --content-file <候选正文> --output json
multica ... skill files upsert <moe-opc-id> --path <清单内相对路径> --content-file <候选文件> --output json
multica ... agent update <bug-resolver-id> --instructions <候选完整正文> --output json
```

description 和 instructions 用参数数组传给原生 CLI，不拼接 shell；具体做法沿根 AGENTS.md。不向 Multica 提交整个对象响应，也不删除、重建 Skill 或整组重设绑定。`changes.patch` 是审阅附件，线上发布由原生命令执行。

## 验收与部分失败

- 逐字段回读等于 after 哈希；未涉及字段和文件与切换前快照一致，三个 Skill 的原 ID 与全部既有绑定关系保留。
- 两个目标智能体各使用新会话做限定为读取 Skill 的加载验收：新名称唯一可发现、能读取对应正文及所需 references。使用实际 Runtime 记录确认来源；工作区 Skill 名称、配置保存或文本回答不能替代加载证据。验收不发送 Slack 消息、不触发真实业务处理。
- 其他智能体的旧 Prompt、历史任务和日志不作为本次全局“旧名称清零”的验收对象。个人仓库的历史记录保留原名称。
- 任一步失败停止后续写入，记录本次已成功字段及结果不明字段。结果不明先回读；回退只覆盖本次确实改变、且现状仍等于本次 after 的字段，恢复其 before 快照并回读。已有 after 字段和他人并发修改不得被回退。
- 本机安装、线上名称及 Prompt 应作为同一批切换恢复。恢复前核对安装来源与自动同步状态，避免 hook 再次覆盖恢复结果；源码回退使用普通 revert PR。两个目标加载验证完成后才恢复任务入口并记录线上迁移完成。
