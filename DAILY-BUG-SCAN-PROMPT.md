<!-- 交付频道 ID 使用占位符，线上同步前替换为已核对的真实值。 -->

mode=scan
executionPolicy=lane_investigation_serial_repair_v2
schedulerRevision=<调度器提交 SHA>
investigationFailurePolicy=retry_2_then_skip_v1

# 每日三源采集、全轮调查与修复

每天北京时间 03:00，由「GRM 每日 Bug 值守」按当前 instructions 执行固定顺序：三源采集 → 本轮清单完整登记 → 两张调查工位卡并行把本轮候选全部留档 → 汇总结论 → 固定修复队列（全局一次一项） → 逐条验收交付。只保留现有值守与看板两个 Agent；由同一个值守 Agent 完成调查和修复，不派生或转派其它 Agent。看板 Agent 的配置与 10:00 定时保持原样。

## 入口与采集

1. 回读当前 Issue、真实运行、Agent 与项目。Agent 为 `387e0620-8a22-44f5-919c-4a3e6296fcfb`，项目为 `724dce94-a8b9-44e7-8390-9c3cbdac7bc0`，GRM 工作区为 `1b6d114a-8407-4ba8-adea-d31209caabe5`。缺少 metadata 时按 Agent 规则初始化 dailyBatch 和 dailyRound，有现场则恢复，不覆盖。仅新扫描按正文 executionPolicy、schedulerRevision、investigationFailurePolicy 和真实 Runtime 初始化 dailyExecution；已有旧轮不迁移。任何阶段都不修改 Agent 并发；其它轮次仍在调查或修复时本轮照常开始。
2. 按实际运行时间计算 Asia/Shanghai 前一个完整自然日；开始采集前先保存并回读 dailyScan.targetDate、sources 初始状态及 coverageComplete=false，恢复时复用已保存目标日。每来源成功或失败后立即保存真实状态、已知 collected 数量与回执，未知或失败不填成 0。每来源首次只采昨天，后续从该来源最后成功日期的次日补到昨天；成功覆盖的窗口复用，失败只补缺失来源。任务标题的 UTC 日期不代替反馈窗口。
3. 使用已合并稳定的 moe-feedback-collector SKILL.md 与 collect-daily CLI，沿用 DAILY_BUG_COLLECTOR_SCRIPT、DAILY_BUG_OUTPUT_ROOT 和 Canny account-ref 协议。独立采集 Canny、Facebook、Intercom，各业务均覆盖；不从 CS 起采，不扩大为历史扫描。Facebook、Intercom 已成功日期与 run 不重复采集。

采集与初筛完成后立即保存现有 dailyScan 的来源覆盖、数量、初筛分类和 scan-evidence-index.json 回执，供 10:00 看板读取阶段快照；调查和修复仍按原协议分别留档，最终扫描台账待全轮调查结束统一写入。阶段展示不推进队列，也不把当前轮迁移到新调度版本。

## 本轮全部调查完成门槛

1. 初筛、归并和查重后固定本轮疑似 Bug 清单，上传并核对真实附件，保存 dailyRound 的候选数量、身份集合摘要和候选清单回执。只登记本轮反馈，不导入旧 pending-candidates、旧 needs_evidence 或历史调查队列；用户已停止的历史任务不恢复。
2. 不为候选逐条建任务：在原扫描追加清单评论（candidateId、原反馈日、摘要），回读后核对身份集合摘要；再在原扫描下建两张调查工位卡（工位 1/2、2/2），回读后写入 dailyRound 的 investigationStore 与 laneIssueIds，才设置 registrationComplete。完整登记和三源覆盖回读后，只通过固定版本 duty-dispatch.py advance 给两个工位各领取一条候选并启动；工位每条结论回读后再次 advance 领取下一条并排后继，不手工启动、不依赖人工再次调用；扫描与调查入口不修改业务实现。
3. 每条调查主动补读原讨论、核对正确行为、定位实现，按需查询真实商家和窗口的数据或日志，在授权测试环境验证。保存步骤、实际观察、版本和证据，上传报告并下载核对哈希，把含 investigationRecord 的结论记录以评论追加到工位卡并回读。确认、排除、重复、已有工单与真正外部缺证均须有依据。可恢复故障先恢复或替代补证；仍失败保存 executionFailure={kind,error}、过程与 nextAction，回读后立即 advance。调度器保留首次运行及最多 2 次重试，耗尽则留档为 skipped_failure 并释放工位接下一条，不阻塞其它调查。
4. 确认一个 Bug 时只记录 confirmed，继续调查本轮其它项。全部候选均有依据结论或有界重试后已留档跳过后，由最后退出的工位经统一调度器唯一交接回原扫描。仅原扫描汇总并回读完整调查索引、最终分类与扫描消息，保存 investigationComplete 和 ready_repair，才允许建立固定修复队列。ready、investigating 和未留档技术失败都不能算本轮处理结束；有界重试耗尽且已留档的 skipped_failure 可结束本轮调度，但不计确认、排除或验证通过，不进修复队列；不能把未调查项留在候选内容区后发送“扫描完成”。
5. 真正外部缺证须具备 Agent 规定的四条取证路径尝试、缺失材料、恢复条件、证据版本和检查时间，完整保存后可作为本轮已调查结论，不阻塞其它调查与本轮后续修复；不得用其掩盖执行失败。历史外部挂起项也不自动带入新轮次。

## 修复与交付

1. 新轮调查使用 dailyExecution 固定版本的 duty-dispatch.py（protocolVersion=2），采集、汇总与修复使用同版本 duty-queue.py（protocolVersion=2，必须带 --round-id）。旧轮继续各自固定版本，不迁移或重采。只读计划不等于真实启动。按 Agent 的 collect_sources、register_round、investigate_lanes、finalize_round、prepare_round_repair、wait_repair_slot、resume_repair、advance_after_block、complete_round 等动作执行；任何矛盾先核对，不旁路全轮调查门槛。
2. 全轮调查完成、三源覆盖完整且无其它真实修复队列在途后，在原扫描登记唯一固定修复批次，仅纳入 confirmed 项；其它轮次的修复队列未结束时返回 wait_repair_slot，保留现场结束运行，等其结束后由 release-repair 唤起。沿用值守 Agent 当前修复能力，每个 Bug 正文保留目标分支 CI、真实部署版本、动态 Case、最终完整 HTML、PR 和交付回读要求；不参考其它 Agent 的专属规则。
3. 只将首项指派自身，回读真实 queued/dispatched/running 后结束当前运行释放唯一名额。每项完整交付后，或真实受阻并按 Agent 模板在交付频道发出、回读受阻结果后，自动接下一修复；尚未全部合并的受阻项单列“受阻”，不计应上线或已交付 PR；保留现场与证据，确认受阻后不再新建 PR，不自动补验收、催办解除或回流队列，已有 PR 仅作为历史证据保留，仅真人明确要求恢复时续作。已回读的历史 Slack 消息被删除后不补发、不要求重新点 🚫。最后一项的消息写明本批次结果，不另发批次汇总；结束本轮后执行一次 release-repair 唤起等待修复的轮次；不返回历史调查队列，不等待逐项人工确认。PR 合并和生产发布仍需明确授权。
4. 三源缺失、调查未完成、报告缺口或执行失败必须保留现场和具体恢复动作，不能伪造完成或无 Bug。零项合格也须有完整调查与分类证据。保存来源覆盖、scan-evidence-index.json、完整调查索引、真实交接和交付回执。
5. 全轮调查完成后，按 Agent 模板向指定频道 <交付频道 ID> 发送一次扫描结果并回读；明确调查完成数、确认可修数及下一步。外部缺证的过程、数量、材料缺口和恢复条件保留在任务、完整报告与分类台账，不在值守 Agent 的 Slack 汇总中单列，也不另发通知。执行失败跳过数量在阶段结论中明确标出，过程与恢复条件留在任务和完整索引；含跳过项不能声称全部验证通过。尚在调查时只报告实际阶段，不发完成通知；重跑复用回执，避免重复发送。
