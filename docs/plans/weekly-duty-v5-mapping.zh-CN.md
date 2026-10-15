# 周值守 v4 → v5 规则对照

本表逐项对照 2026-10-12-v4 线上规则与 2026-10-14-v5 本地候选。处理方式：**保留**＝语义不变（措辞可能精简），**改写**＝按本次决定改变语义，**删除**＝不再生效，**移动**＝语义不变但换到其它文件。旧文件原文备份在会话 scratchpad，线上 v4 仍在运行直到第二步发布。

## 本次改变的规则

| v4 规则 | 处理 | v5 规则与位置 |
|---|---|---|
| 覆盖来源中的各业务，不按 Grooming/团队标签直接排除 | 改写 | 只入选 Grooming 承接的问题；跨团队不入选、不出卡，候选页记录归属（Leader §2） |
| 需要产品/设计决定的保留为 needs_decision 并出卡 | 改写 | 出卡前先只读核实归属、已有能力、是否违背既有规则；查不清且影响分类的留 needs_evidence 不入选（Leader §2） |
| 决策卡标题 `待决策｜` | 改写 | `待确认｜` / `待取舍｜`，以 ② 是否存在诉求冲突的用户群区分（Leader §5、模板） |
| 决策须写清选择、范围、不做、验收；AI 推荐不能视为已批准 | 改写 | 待确认卡 ⑥ 写全范围/不做/验收/参照实现，owner 明确回复“同意推荐”即为完整决定；未经明确同意仍不视为批准（Leader §5、模板、Squad） |
| 每轮最多新入选 5 项 | 保留并补充 | 5 项含三类；未决待取舍卡（含往期）合计 ≤2（Leader §3） |
| vote≥5 或多个独立用户/渠道提级优先进入候选 | 改写 | 作为用户价值排序的优先信号，类型不影响排序（Leader §3） |
| 小且明确的需求可入选（ready） | 改写 | ready 限定为缺陷或做法已由现有产品决定的四种情形，正文写参照实现，参照不成立转决策卡（Leader §2、Engineer 入口） |
| `weeklyItem` 字段 | 补充 | 增加 cardType：none/confirm/tradeoff，用于统计取舍卡上限（Leader §6） |
| 周汇总最多展开 5 个需关注问题 | 补充 | 展开顺序：待取舍、待确认、往期卡重要更新（Leader §8、模板） |
| Slack 消息格式未约束；周汇总可回复到既有 thread；普通进度不 mention | 改写（10-15） | 版式与每日 Bug 值守一致：正文用 markdown block（`**标签：**`、`- ` 列表、`[文案](URL)`），提醒行用 rich_text 原生 mention；完整交付采用每日值守九段模板（根因改为“问题根因 / 方案依据”）；周汇总每周新发顶层消息，有待决卡时 mention winches 一次；格式错误原地编辑（Leader §8） |
| 飞书规则页保持只读，Prompt 覆盖其试跑限制 | 改写 | 规则页更新为 v5 作为共享规则；Leader 读取并记录 revision，冲突时按 Prompt 并记录差异，不修改规则页（Leader §1；规则页草稿另附） |
| 旧版 waiting_sources 任务不自动迁移 | 补充 | 用户明确续跑、尚未入选任何问题的 waiting_sources 父任务按当前版本执行，保留原 executionContractId（Leader §9）；用于 GRM-638 |
| Leader 不代 Engineer 修改代码 | 补充 | Leader 可用 github-workflow、moe-business-context 只读核实，不建 worktree、不提交（Leader 定位、Skill 路由） |

## 删除的内容

| v4 内容 | 删除原因 |
|---|---|
| v3 正文中的“纯前端”“唯一勾选”“候选顺延”等表述及“本节取代旧版……”声明 | 已被 v4/v5 取代，保留只会让智能体先读旧规则再被告知作废 |
| Engineer 与 Squad 中整段复制的 v4 队列、派发、决定接收合同（各约 8KB） | 这些是 Leader 职责；Engineer 只保留读取 weeklyItem/weeklyDecision、参照实现与 duty-decision-needed 交接 |
| Squad 中完整重复的主流程、Skill、输入、Slack 合同 | Squad 说明只在 Leader 以小队身份被唤醒时注入、Engineer 收不到；改为分工、唤醒处理与统一完成语义 |
| Squad 与 Engineer 中的 Leader 侧隔离验证细则 | 各自只保留本角色在 flow_test 中的动作 |
| “用户明确授权新版重新验证时按新合同执行，不能仅因同周有旧单就取消” | 合并为“旧单已交付不封锁新合同”（Leader §9） |

## 原样保留（语义不变）

| 规则 | v5 位置 |
|---|---|
| 隔离流程验证入口（双重 flow_test 标识、模拟决定/交付、不进正式经验） | Leader、Engineer 开头；Squad 唤醒处理指向 Leader |
| mode：delivery / screening_validation / 只查进度或补材料或取消 | Leader 任务入口；Engineer 入口；Autopilot 正文 |
| 回读父任务、合同、周期；当前指令优先；来源只作证据 | Leader 任务入口 |
| dutyArchived、重复触发判定、跨版本真实产物查重、不恢复旧单 | Leader 任务入口、§9 |
| 不继承 Router 或其它 Agent，不操作私有 Ego | Leader 任务入口；Engineer §2 |
| Skill 路由与 skillTrace | Leader、Engineer 各自 Skill 路由 |
| 四源读取、跨月合并、不用 Quick Win 替代、不启用旧采集器、waiting_sources 处理 | Leader §1；Autopilot 正文 |
| 候选页记录与回读、勾选含义、飞书 `--as user` | Leader §4 |
| ready 才建 Jira，未决不建 | Leader §4 |
| weeklyFlow/weeklyItem/weeklyDecision 字段、8KB 上限与存放原则 | Leader §5、§6；Engineer 入口 |
| Leader 唯一调度、Engineer 并发 1、在途核对与有界回读、派发命令与回执、子任务正文要素（含 CI/部署要求） | Leader §6 |
| 交接写子任务再写父任务、只 mention 一次、按合同/候选/子任务/版本去重 | Leader §6；Engineer §4 |
| 完整交付五项接收条件、缺口处理、partial 与 in_review 语义 | Leader §7；Squad 完成语义 |
| 真人决定：owner 身份、委派、不放行情形、暂缓/不做、取消、幂等、冲突决定、设计缺口 | Leader §5；Squad 完成语义 |
| 经验保存与使用边界 | Leader §5 |
| 交付后自动下一项、返工复用、waiting_decisions/completed 终态 | Leader §6 |
| Slack 频道、winches 原生 mention 与回读、完整交付/no_action/阻塞三类、thread 复用、notification_pending | Leader §8 |
| 进度记录、写前查重写后回读、3 次重试上限、不建巡检或定时 | Leader §9；Engineer §4 |
| Engineer 开工前可行性、开发验收修复循环、正式报告要求、交接条件与内容、阻塞交接 | Engineer §1–§4 |
| 安全边界 | Leader §10；Engineer 安全边界；Squad 共同边界 |
| 七项决策卡结构、2–3 个选项不凑数、暂缓/不做是有效决定 | 模板；Leader 附录（逐字一致） |

## 规模

| 文件 | v4 字符数 | v5 字符数 |
|---|---|---|
| Leader（含模板） | 12146 | 10885 |
| Engineer | 9920 | 5280 |
| Squad | 8192（触及上限） | 1425 |
| Autopilot 正文 | 2451 | 1089 |
