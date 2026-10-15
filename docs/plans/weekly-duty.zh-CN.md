# 周值守多问题与半人工决策迭代

## 当前结论

2026-10-14 基于 W40/W41 真实反馈讨论后，周值守重定位为“以决策卡为主、直接实施为补充”，按 2026-10-14-v5 重写全部规则，已用 CLI 发布 Leader、Engineer、Squad（说明与描述）和 Autopilot 正文并逐字回读一致，飞书规则页同步改为第二期（revision 10）；隔离回归 GRM-671 通过。模型、Runtime、并发、Skills、成员角色与每周一北京时间 11:00 调度不变。W41（10.05–10.11）四源已于 2026-10-14 正式发布；GRM-638 仍为 waiting_sources，下一阶段按 v5 接续。

v5 已确认的规则：

1. 只入选 Grooming 团队承接的问题，跨团队不入选、不出卡。
2. 出卡前 Leader 只读核实归属、已有能力、是否违背既有规则；查不清且影响分类的不入选。
3. 决策卡分待确认与待取舍：待确认卡 ⑥ 写全范围、不做、验收和参照实现，owner 回复“同意推荐”即为完整决定；待取舍卡用于诉求冲突的用户群，未决合计不超过 2 张。
4. 直接实施项限于缺陷，或做法已由现有产品决定的情形（照搬已有样式、往已有清单加项、沿用已有状态含义、补齐另一端），正文写明参照实现。

## 完整范围与阶段

1. 本地重写（已完成）：Leader、Engineer、Squad、Autopilot 正文、决策卡模板，新旧对照见 `weekly-duty-v5-mapping.zh-CN.md`，飞书规则页草稿见 `weekly-duty-rules-page.zh-CN.md`。不写线上。
2. 发布与隔离回归（已完成，见下方 v5 回归结果）：CLI 发布四项配置、Squad 描述与飞书规则页并回读；按 GRM-612 方式跑一次 flow_test，确认待决定不阻塞、决定后接续、重复决定不重复派发仍成立。
3. 真实混合批次（2026-10-15 部分验证收尾）：接续 GRM-638 跑 W41，读 350 条、入选 4 项。直接实施 GRM-678（Quick jump 扩到 20 周，PR MoeGolibrary/Boarding_Desktop#7202，未合并）完成真实开发、验收、HTML 与 Slack 交付；待确认卡 GRM-675/676/677 已生成并经周汇总通知，保持待决定。10-15 修正 Slack 版式（与每日 Bug 值守一致的模板、顶层周汇总并 mention），Leader 已原地修正两条消息。
   - 已验证：v5 选题与出卡前核实、Grooming 归属排除、“往已有清单加项”直接实施、真实交付链路、周汇总与交付通知；决定接收与按 ⑥ 派发已在隔离回归 GRM-671 验证。
   - 未验证：真实决定驱动的真实开发（Engineer 按 weeklyDecision 范围实施）、待取舍卡上限统计。owner 表示无法对这三张卡做产品决定，不以模拟决定替代真实批准；留待后续有真实决定的卡验证。
   - 待处理：明确谁能做 Grooming 产品决定（owner 委派），否则决策卡会持续积压；合并对话产品域不一致时的归属判定规则；PR #7202 待 owner 审查。

待决定、不做、暂缓分别保存有效结论；不把信息不足当产品取舍，不把票数当实施批准。正式经验沿用问题任务/候选页引用，不建新后台；单次历史决定不自动成为全局授权。

## 真源与目标

- Leader：`WEEKLY-DUTY-LEADER-PROMPT.md`；Agent ID `eb6bc24d-6398-4b7b-b2ee-24864525456f`。
- Engineer：`WEEKLY-DUTY-ENGINEER-PROMPT.md`；Agent ID `4831cd74-392e-4231-a533-4980f6bf2343`，并发1。
- Squad：`WEEKLY-DUTY-SQUAD-PROMPT.md`；ID `9dae42fe-c2e3-411e-a515-f8c09045aa4f`。instructions 上限 8192 字符；只在 Leader 以小队身份被唤醒（@小队、成员交接 mention、指派给小队）时注入，Autopilot 直接指派 Leader 的运行和 Engineer 都收不到，因此 Leader、Engineer 的 Prompt 须各自完整。
- 飞书规则页：https://mengshikeji.feishu.cn/wiki/UGQIwnZlzixtl3k1numcVwWVnze ；v5 草稿 `docs/plans/weekly-duty-rules-page.zh-CN.md`。
- 周任务：`WEEKLY-DUTY-TASK.md`；Autopilot ID `b8ae9262-504c-4c63-ada0-6f47ae20878d`，原schedule保留，未手动触发真实delivery。
- 展示模板：`WEEKLY-DECISION-TEMPLATE.md`；问题/来源、用户背景、现状依据、待决事项、选项、AI推荐、人工决定与执行边界七项。
- 工作区GRM：`1b6d114a-8407-4ba8-adea-d31209caabe5`；项目 `d0fb1844-dfa8-488f-9c6b-f5810767df51`。

## v5 隔离回归结果

父任务 [GRM-671](https://multica.devops.moego.dev/grm/issues/GRM-671)，executionContractId=weekly-v5-flow-test-20261014；A 为待确认卡 [GRM-672](https://multica.devops.moego.dev/grm/issues/GRM-672)，B/C 为 ready [GRM-673](https://multica.devops.moego.dev/grm/issues/GRM-673)、[GRM-674](https://multica.devops.moego.dev/grm/issues/GRM-674)。全部为 flow_test、simulated，不进入正式经验。

- A 由 Leader 补成七项卡，标题前缀“待确认｜”，cardType=confirm，⑥ 含范围、不做、验收与参照实现；未决定时无工程指派。
- B、C 串行模拟交付后父批次停在 waiting_decisions（覆盖了 v4 未专项测试的“仅余待决定终态”）。
- owner 在 A @小队“暂不决定”：不放行，decisionVersion=0。
- owner 回复“同意推荐（隔离模拟决定）”：Leader 按 ⑥ 逐字段保存决定（rationale=采纳 AI 推荐，decisionVersion=1），C 结束后派发 A 一次，A 模拟交付后父批次 completed。
- 重复确认同一决定：decisionVersion 仍为 1，A 无新增工程运行。
- 工程运行各 1 次且无重叠（北京时间）：B 22:01:55–22:03:53，C 22:05:23–22:06:56，A 22:14:44–22:17:55。父任务 metadata 4.6KB，未接近 8KB。

未覆盖：待取舍卡上限统计、出卡前核实与 Grooming 归属判断、真实开发/验收/HTML/Slack 交付，留待 GRM-638 真实批次验证。

## v4 隔离验证结果（GRM-612）

父任务[GRM-612](https://multica.devops.moego.dev/grm/issues/GRM-612)，executionContractId为weekly-v4-flow-test-20261012。A为待决定[GRM-613](https://multica.devops.moego.dev/grm/issues/GRM-613)，B/C为明确项[GRM-614](https://multica.devops.moego.dev/grm/issues/GRM-614)、[GRM-615](https://multica.devops.moego.dev/grm/issues/GRM-615)。全部正文与metadata双重mode=flow_test，模拟决定/交付标simulated，未进入正式经验；四个任务已结束，所有运行均completed。

- A未决定时，B先执行；B结束后C自动接续。主会话未替Leader派发工程子项。
- owner在A任务@小队“暂不决定”：Leader真实运行处理后，A无工程指派、decisionVersion=0，未保存实施决定。
- owner随后在A任务@小队明确隔离模拟决定，包含范围、例外、交互和验收：Leader保存模拟decisionVersion=1、真实作者和评论ID、原卡版本，并在C结束后由原父批次派发A。
- 明确决定在C运行期间返回，验证了决定接收不打断当前工程；重复引用同一已应用决定的评论由Leader处理，决定仍为版本1，A仍只有一个工程run。
- 最终三项模拟交接均核验，父metadata weeklyFlow为completed/flow_test_completed、activeChildId=null；派发回执3份，工程实际顺序B→C→A，无运行重叠。

北京时间2026-10-12的实际工程运行：

| 项目 | 开始 | 结束 | 工程run数 |
|---|---|---|---|
| GRM-614（B） | 16:27:58 | 16:30:22 | 1 |
| GRM-615（C） | 16:31:22 | 16:36:24 | 1 |
| GRM-613（A） | 16:42:22 | 16:45:05 | 1 |

独立核验的完整ID、原评论ID与断言保存在`weekly-duty-flow-verification.json`，服务器父任务testVerification保留紧凑通过摘要。run completed不是独立成功依据，本轮核验了真实父子/指派、metadata、原评论、决定版本、交接回执及运行区间。

## 已发现的平台边界与验证限制

本轮父任务原metadata已约7933字节，保存详细独立核验报告触及Multica每任务metadata总量8KB上限。未删除有效队列或交接记录：保存紧凑摘要，详细验证记录放本地源文件；最终规则要求metadata只保留紧凑状态、ID、版本和去重，详细卡/技术交接/时间/诊断置于任务正文、评论或附件，避免多问题队列被审计记录增长阻断。

本轮只证明采样的队列和owner原生mention决定接续；没有读客户反馈、开发、业务API、Jira/飞书/Slack写入、真实PR/验收报告或部署。仅余待决定终态、重复工程交接及非owner委派入口未专项实测；真实工程、通知和决策经验应用需下一阶段验证。配置回读也不代表下一个真实定时周期已通过。
