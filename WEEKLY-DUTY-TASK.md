mode=delivery。流程版本：2026-10-14-v5。由「GRM AI 自动化值守 Leader」按其 Prompt 自动完成本周链路，普通步骤不等待用户确认。

1. 回读本周批次、同候选历史产物与在途任务，复用已有对象；已交付不重复启动。按 Leader Prompt 初始化或恢复 weeklyFlow，保存流程版本、原周期、阶段、产物与下一动作并回读。旧版本或已停止的任务不自动迁入。
2. 读取筛选规则页 https://mengshikeji.feishu.cn/wiki/UGQIwnZlzixtl3k1numcVwWVnze ，从 https://mengshikeji.feishu.cn/wiki/OjbbwLwfPikMF7kQBPWcEE0Lnjb 下钻 Asia/Shanghai 上一个完整自然周的 Canny、Intercom、Jira、Facebook 全部来源明细，跨月周合并全部片段。复用 Codex grooming 已发布数据，不启动旧 Multica 采集器。尚未发布记 waiting_sources 并如实通知，不拿旧周替代、不误报 no_action。
3. 只入选 Grooming 承接的问题；出卡前只读核实归属、已有能力和是否违背既有规则。每轮最多新入选 5 项，未决的待取舍卡合计不超过 2 张，不足不凑数。在 https://mengshikeji.feishu.cn/wiki/M3bFwTMwhiUsnzkhn5Mc3Q1anWf 记录来源覆盖、核实结论与候选。
4. 待确认卡、待取舍卡写入独立问题任务，只暂停该项；真人在问题任务 @本小队决定后按原父批次接续。ready 项建或关联 Jira 后由同一 Engineer 串行开发、动态验收、修复复验，并生成正式报告与最终 PR。
5. Leader 回读真实产物后，向 https://moegoworkspace.slack.com/archives/C0C14HSSP2T 发送周汇总和每项完整交付（含 winches mention 与完整 HTML 报告），回读后才算交付待审；无候选发送 no_action，真实硬阻塞发送阻塞通知。

飞书显式 --as user；不重采集、不发布原周报。moe-acceptance 是唯一动态验收入口，不接管用户私有 Ego。所有写入先查重、写后回读。禁止 merge、auto-merge、生产发布、生产数据写入、force push、默认分支直推和历史改写。不修改现有每周一 11:00 调度。
