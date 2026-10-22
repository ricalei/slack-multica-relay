<!-- 交付频道 ID 使用占位符，线上同步前替换为已核对的真实值。 -->

按 Agent 当前 instructions 在每天北京时间 10:00 生成并发布值守阶段快照，统计截至昨日反馈日。

从同一 origin/main SHA 导出 scripts/duty-board.py、scripts/duty-progress.py 与 scripts/duty-queue.py，展示已采集数量、调查留档与结论、完整修复交付进度、真实当前任务和下一步。最终台账尚未登记时也读取已有阶段事实，不把调查中、修复中或读取失败标为未扫描。

向 <交付频道 ID> 发送精简版，在消息 thread 上传完整 HTML，并在 GRM-665 追加完整版。正常同日已发布则复用回执；真人明确补发时按 instructions 的稳定请求 ID 去重，复用已有采集数据。保持 03:00 值守与 10:00 看板，不触发值守或重采 Facebook、Intercom。
