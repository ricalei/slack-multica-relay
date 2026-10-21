#!/usr/bin/env python3
"""只读生成每日值守的下一步计划；任务写入、交付验证和启动由值守 Agent 执行。"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path


TERMINAL = {'delivered', 'duplicate', 'excluded', 'cs_followed', 'cancelled'}
STATES = TERMINAL | {'ready', 'investigating', 'confirmed', 'waiting_repair', 'suspended_external', 'paused'}
FINISHED_BATCHES = {'completed', 'finished_with_skips'}
STOPPED = {'cancelled', 'canceled', 'done'}
IN_FLIGHT = {'queued', 'dispatched', 'running', 'waiting_local_directory'}


class QueueError(ValueError):
    pass


def meta(issue, key):
    value = (issue.get('metadata') or {}).get(key)
    if value is None:
        return {}
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise QueueError(f"{issue['id']} 的 {key} 无法解析") from error
    if not isinstance(value, dict):
        raise QueueError(f"{issue['id']} 的 {key} 不是对象")
    return value


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError) as error:
        raise QueueError('时间必须包含有效时区') from error


def text(value):
    return isinstance(value, str) and bool(value.strip())


def valid_external_block(item):
    block = item.get('blocker') or {}
    if not isinstance(block, dict):
        return False
    attempts = block.get('attempts') or {}
    if not isinstance(attempts, dict):
        return False
    required = ('missingEvidence', 'resumeCondition', 'evidenceRevision')
    if block.get('kind') != 'external_evidence' or not all(text(block.get(k)) for k in required):
        return False
    # 每一项都是实际调查记录的引用；不适用时引用解释原因的段落，不能用布尔值代替证据。
    if not all(text(attempts.get(k)) for k in ('source', 'implementation', 'data', 'verification')):
        return False
    try:
        return timestamp(block['nextCheckAt']) >= timestamp(block['checkedAt']) + dt.timedelta(days=1)
    except (KeyError, QueueError):
        return False


def valid_confirmation(item):
    proof = item.get('verification') or {}
    return (isinstance(proof, dict) and proof.get('fixable') is True
            and proof.get('productDecisionRequired') is False
            and all(text(proof.get(k)) for k in ('expectedBehaviorRef', 'observedDeviationRef', 'dedupeRef')))


def verified_delivery(batch, bug_issue):
    bug = meta(bug_issue, 'dailyBug') if bug_issue else {}
    version = bug.get('deliveryVersion')
    if (not bug_issue or bug.get('completeDelivery') is not True or bug.get('state') != 'delivered'
            or type(version) is not int or version < 1):
        return False
    for receipt in batch.get('completedDeliveries') or []:
        if receipt.get('issueId') != bug_issue['id'] or receipt.get('deliveryVersion') != version:
            continue
        report, delivery = receipt.get('report') or {}, receipt.get('delivery') or {}
        if (report.get('readbackHashVerified') is True and delivery.get('readbackVerified') is True
                and re.fullmatch(r'[0-9a-f]{64}', report.get('sha256') or '')
                and text(report.get('multicaAttachmentId')) and text(delivery.get('permalink'))
                and text(receipt.get('pr')) and re.fullmatch(r'[0-9a-f]{40}', receipt.get('prHead') or '')):
            return True
    return False


def order(issue):
    item = meta(issue, 'dailyInvestigation')
    return (item.get('feedbackDate') or '', issue.get('created_at') or '', issue['id'])


def next_action(issues, runs, *, project_id, agent_id, current_issue_id=None, now=None):
    """只使用已保存的事实决策，不把计划当作已启动，也不改变固定批次的游标。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    by_id = {}
    investigations, batches = [], []
    for issue in issues:
        if issue.get('project_id') != project_id or not text(issue.get('id')) or issue['id'] in by_id:
            raise QueueError('任务不属于目标项目、缺少 ID 或出现重复 ID')
        by_id[issue['id']] = issue
        item, batch = meta(issue, 'dailyInvestigation'), meta(issue, 'dailyBatch')
        if item:
            if item.get('schemaVersion') != 1 or item.get('mode') != 'investigate':
                raise QueueError(f"{issue['id']} 的调查协议不支持")
            if item.get('state') not in STATES or not text(item.get('candidateId')):
                raise QueueError(f"{issue['id']} 的候选身份或状态无效")
            if item.get('state') not in TERMINAL | {'paused'} and issue.get('status') not in STOPPED:
                investigations.append(issue)
        if (batch.get('mode') in ('scan', 'repair') and batch.get('bugIds')) or batch.get('mode') == 'repair':
            batches.append(issue)

    identities = [meta(issue, 'dailyInvestigation')['candidateId'] for issue in investigations]
    if len(identities) != len(set(identities)):
        raise QueueError('同一候选存在多个未结束调查任务，须先归并')
    for run in runs:
        if (run.get('status') in IN_FLIGHT and run.get('issue_id') != current_issue_id
                and run.get('agent_id') == agent_id):
            return {'action': 'wait', 'issueId': run['issue_id'], 'reason': '已有排队或运行中的任务，释放当前名额'}

    active_batches = [issue for issue in batches if meta(issue, 'dailyBatch').get('state') not in FINISHED_BATCHES
                      and issue.get('status') not in STOPPED]
    if len(active_batches) > 1:
        raise QueueError('存在多个未结束固定修复批次，不能另起任务')
    if active_batches:
        batch_issue = active_batches[0]
        batch = meta(batch_issue, 'dailyBatch')
        if batch.get('mode') == 'repair' and not batch.get('bugIds'):
            return {'action': 'reconcile_repair', 'batchId': batch_issue['id'],
                    'issueId': batch.get('investigationIssueId'), 'reason': '恢复已登记批次的子项与队列，不重复建批次'}
        bug_ids, index = batch['bugIds'], batch.get('index')
        if (not isinstance(bug_ids, list) or len(set(bug_ids)) != len(bug_ids)
                or batch.get('total') != len(bug_ids) or type(index) is not int
                or not 1 <= index <= len(bug_ids) or batch.get('currentIssueId') != bug_ids[index - 1]):
            raise QueueError('固定修复队列的 total、index 或 currentIssueId 不一致')
        current = by_id.get(batch['currentIssueId'])
        if not current or current.get('parent_issue_id') != batch_issue['id']:
            raise QueueError('当前修复任务缺失或真实父子关系不符')
        if current.get('assignee_id') not in (None, agent_id):
            raise QueueError('当前修复项已交由其他执行者，不能接管')
        bug = meta(current, 'dailyBug')
        if bug.get('batchId') != batch_issue['id'] or bug.get('index') != index or bug.get('total') != len(bug_ids):
            raise QueueError('当前 Bug 与固定批次位置不符')
        if batch.get('state') in ('blocked', 'paused', 'cancelled') or current.get('status') in STOPPED:
            return {'action': 'attention', 'issueId': current['id'], 'reason': '按原修复流程恢复阻塞，不绕过交付门槛或真人暂停'}
        return {'action': 'resume_repair', 'issueId': current['id'], 'batchId': batch_issue['id']}

    # 导入回执属于原扫描；新扫描即使零候选，也不能遮蔽旧扫描尚未登记的积压。
    scans = [issue for issue in issues if meta(issue, 'dailyScan') and issue.get('status') not in STOPPED]
    for scan_issue in sorted(scans, key=lambda row: (row.get('created_at') or '', row['id'])):
        scan = meta(scan_issue, 'dailyScan')
        pending = max(scan.get('currentPending') or 0, scan.get('cumulativePending') or 0,
                      (scan.get('counts') or {}).get('verification_blocked') or 0)
        imported = meta(scan_issue, 'dailyInvestigationImport')
        attachment = scan.get('pendingCandidatesAttachmentId')
        registered = imported.get('registeredCandidateCount')
        registered_total = sum(bool(meta(row, 'dailyInvestigation')) for row in issues)
        if pending and not (imported.get('schemaVersion') == 1 and imported.get('completed') is True
                            and attachment and imported.get('pendingAttachmentId') == attachment
                            and imported.get('sourceRecordCount') == pending
                            and type(registered) is int and 1 <= registered <= registered_total
                            and re.fullmatch(r'[0-9a-f]{64}', imported.get('sourceSha256') or '')):
            return {'action': 'register_investigations', 'issueId': scan_issue['id'], 'attachmentId': attachment,
                    'reason': '先核对最新待验证附件并登记全部积压，不能把未导入记录报成 idle'}

    runnable, checks, coverage_waiters = [], [], []
    for issue in sorted(investigations, key=order):
        item = meta(issue, 'dailyInvestigation')
        origin = by_id.get(item.get('sourceScanId'))
        if not origin or issue.get('parent_issue_id') != origin['id']:
            raise QueueError('调查任务缺少原扫描或真实父子关系不符')
        owner = issue.get('assignee_id')
        if owner and owner != agent_id:
            raise QueueError(f"{issue['id']} 已交由其他执行者，不能接管")
        related = [batch for batch in batches
                   if meta(batch, 'dailyBatch').get('investigationIssueId') == issue['id']]
        if len(related) > 1:
            raise QueueError('一个调查任务关联多个修复批次')
        if related:
            batch_issue = related[0]
            batch = meta(batch_issue, 'dailyBatch')
            if item.get('repairBatchId') not in (None, batch_issue['id']):
                raise QueueError('调查任务与修复批次的关联不一致')
            if batch.get('state') == 'completed' and batch.get('outcome') == 'engineering_delivery_completed':
                bug_ids = batch['bugIds']
                if (not isinstance(bug_ids, list) or len(bug_ids) != 1
                        or batch.get('deliveredBugIds') != bug_ids):
                    raise QueueError('调查修复批次缺少完整交付记录')
                bug_issue = by_id.get(bug_ids[0])
                bug = meta(bug_issue, 'dailyBug') if bug_issue else {}
                if (not bug_issue or bug_issue.get('parent_issue_id') != batch_issue['id']
                        or bug.get('batchId') != batch_issue['id'] or not verified_delivery(batch, bug_issue)):
                    raise QueueError('修复批次与 Bug 的实际交付版本不一致')
                return {'action': 'complete_investigation', 'issueId': issue['id'], 'batchId': batch_issue['id']}
            # 取消/跳过后的现场只留人工处理，不把它重新包装成另一轮修复。
            continue
        if item.get('repairBatchId') or item.get('state') == 'waiting_repair':
            raise QueueError('调查指向的修复批次缺失，须回读恢复，不能重复创建')
        state = item['state']
        if state == 'suspended_external' and valid_external_block(item):
            block = item['blocker']
            if item.get('evidenceRevision') != block['evidenceRevision']:
                runnable.append((issue, 'investigate', '外部证据版本已变化，重新验证'))
            elif timestamp(block['nextCheckAt']) <= now:
                checks.append((issue, 'check_external_evidence', '到期只检查恢复条件，不重复调查或采集'))
            continue
        coverage = bool(origin and meta(origin, 'dailyScan').get('coverageComplete') is True)
        if state == 'confirmed' and valid_confirmation(item):
            if coverage:
                runnable.append((issue, 'prepare_repair', '已确认可修且原来源覆盖完整，建立独立固定修复批次'))
            else:
                coverage_waiters.append(issue)
        else:
            reason = ('挂起依据不完整，继续补证验证' if state == 'suspended_external'
                      else '继续调查；确认前补齐行为、验证、查重和原来源覆盖证据')
            runnable.append((issue, 'investigate', reason))
    if runnable or checks:
        # 到期检查有界且优先于普通调查，避免持续新增的可执行项饿死外部证据恢复检查。
        priority = {'prepare_repair': 0, 'check_external_evidence': 1, 'investigate': 2}
        issue, action, reason = min(runnable + checks, key=lambda row: (priority[row[1]], order(row[0])))
        return {'action': action, 'issueId': issue['id'], 'reason': reason}
    if coverage_waiters:
        issue = coverage_waiters[0]
        return {'action': 'attention', 'issueId': issue['id'],
                'sourceScanId': meta(issue, 'dailyInvestigation')['sourceScanId'],
                'reason': '已确认项等待原扫描缺失来源恢复；不要重复验证或在调查入口重采'}
    return {'action': 'idle', 'reason': '没有可执行项；真正缺外部证据的项保留恢复条件'}


def query(cli, args):
    try:
        result = subprocess.run(cli + args + ['--output', 'json'], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        # CLI 输出可能包含来源原文或配置，失败时不回显。
        raise QueueError('Multica 只读查询失败，请检查 CLI 身份与目标') from error


def snapshot(cli, project_id, agent_id, current_issue_id):
    project = query(cli, ['project', 'get', project_id])
    agent = query(cli, ['agent', 'get', agent_id])
    if project.get('id') != project_id or agent.get('id') != agent_id or agent.get('max_concurrent_tasks') != 1:
        raise QueueError('项目/Agent 身份不符或 Agent 并发不是 1')
    issues, offset, total = [], 0, None
    while True:
        page = query(cli, ['issue', 'list', '--project', project_id, '--limit', '100', '--offset', str(offset),
                           '--sort', 'created_at', '--direction', 'asc'])
        rows = page.get('issues')
        if (not isinstance(rows, list) or type(page.get('has_more')) is not bool
                or type(page.get('total')) is not int or page.get('offset') != offset):
            raise QueueError('任务分页响应不完整')
        total = page['total'] if total is None else total
        if page['total'] != total:
            raise QueueError('读取期间项目任务数量变化，请重新读取')
        issues.extend(rows)
        if not page['has_more']:
            break
        if not rows:
            raise QueueError('任务分页未推进')
        offset += len(rows)
    if len(issues) != total:
        raise QueueError('任务分页总数不闭合')
    # 所有候选都保留为独立任务；只查询正在交接/执行的现场，启动前还须即时回读所选任务。
    run_ids = set()
    for issue in issues:
        item, batch = meta(issue, 'dailyInvestigation'), meta(issue, 'dailyBatch')
        if issue.get('status') == 'in_progress' or item.get('state') == 'investigating':
            run_ids.add(issue['id'])
        if batch.get('state') not in FINISHED_BATCHES and batch.get('currentIssueId'):
            run_ids.add(batch['currentIssueId'])
    runs = []
    for issue_id in sorted(run_ids - {current_issue_id}):
        data = query(cli, ['issue', 'runs', issue_id, '--active'])
        if not isinstance(data, list):
            raise QueueError('活动运行响应不完整')
        runs.extend(data)
    return issues, runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-url', required=True)
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--current-issue', required=True)
    parser.add_argument('--snapshot', type=Path, help='离线验证：包含 issues 和 runs 的脱敏 JSON，不调用 CLI')
    args = parser.parse_args()
    try:
        if args.snapshot:
            data = json.loads(args.snapshot.read_text())
            issues, runs = data['issues'], data['runs']
        else:
            cli = ['multica', '--server-url', args.server_url, '--workspace-id', args.workspace_id]
            issues, runs = snapshot(cli, args.project, args.agent, args.current_issue)
        print(json.dumps(next_action(issues, runs, project_id=args.project, agent_id=args.agent,
                                     current_issue_id=args.current_issue), ensure_ascii=False))
    except (QueueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f'无法生成接续计划：{error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
