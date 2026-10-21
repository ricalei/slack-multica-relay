#!/usr/bin/env python3
"""只读生成每日值守的下一步计划；任务写入、交付验证和启动由值守 Agent 执行。"""
import argparse
import datetime as dt
import hashlib
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
PROTOCOL_VERSION = 2
ROUND_POLICY = 'investigate_all_then_repair'
ROUND_STATES = {'collecting', 'registering', 'investigating', 'ready_repair',
                'preparing_repair', 'repairing', 'completed'}
FIXABLE = {'confirmed', 'waiting_repair', 'delivered'}


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
    if (block.get('kind') != 'external_evidence' or not all(text(block.get(k)) for k in required)
            or block.get('evidenceRevision') != item.get('evidenceRevision')):
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


def candidate_set_hash(candidate_ids):
    payload = json.dumps(sorted(candidate_ids), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def verified_record(record):
    return (isinstance(record, dict) and text(record.get('attachmentId'))
            and re.fullmatch(r'[0-9a-f]{64}', record.get('sha256') or '') is not None
            and record.get('readbackHashVerified') is True)


def round_candidates(scan, issues, agent_id):
    """用冻结清单的身份摘要核对完整登记，防止少登记一项后提前进入修复。"""
    round_info = meta(scan, 'dailyRound')
    if (round_info.get('schemaVersion') != 1 or round_info.get('policy') != ROUND_POLICY
            or round_info.get('state') not in ROUND_STATES):
        raise QueueError('本轮调查协议或阶段无效')
    if not verified_record(round_info.get('candidateManifest')):
        raise QueueError('本轮冻结清单缺少真实附件及哈希回读')
    rows = [row for row in issues if meta(row, 'dailyInvestigation').get('roundId') == scan['id']]
    identities = [meta(row, 'dailyInvestigation')['candidateId'] for row in rows]
    if (type(round_info.get('candidateCount')) is not int or round_info['candidateCount'] < 0
            or len(rows) != round_info['candidateCount'] or len(set(identities)) != len(rows)
            or candidate_set_hash(identities) != round_info.get('candidateSetSha256')):
        raise QueueError('本轮候选数量或身份与冻结清单不一致')
    pending = []
    for row in sorted(rows, key=order):
        item = meta(row, 'dailyInvestigation')
        if item.get('sourceScanId') != scan['id'] or row.get('parent_issue_id') != scan['id']:
            raise QueueError('本轮调查任务与真实原扫描关系不一致')
        if row.get('assignee_id') not in (None, agent_id):
            raise QueueError('本轮调查已交由其他执行者，不能接管')
        if row.get('status') in {'cancelled', 'canceled'} or item['state'] in {'paused', 'cancelled'}:
            raise QueueError('本轮包含人工暂停或取消项，须核对决定，不能当作调查完成')
        state = item['state']
        record = item.get('investigationRecord') or {}
        concluded = (verified_record(record) and text(item.get('evidenceRevision'))
                     and record.get('evidenceRevision') == item['evidenceRevision'])
        if state in FIXABLE:
            concluded = concluded and valid_confirmation(item)
        elif state == 'suspended_external':
            concluded = concluded and valid_external_block(item)
        elif state in {'duplicate', 'excluded', 'cs_followed'}:
            concluded = concluded and text(item.get('conclusionRef'))
        else:
            concluded = False
        if not concluded:
            if row.get('status') == 'done':
                raise QueueError('已关闭调查缺少有效结论与证据，不能作为调查完成或自动重开')
            pending.append(row)
    return rows, pending


def round_plan(scan, issues, by_id, agent_id):
    info = meta(scan, 'dailyRound')
    if info.get('schemaVersion') != 1 or info.get('policy') != ROUND_POLICY or info.get('state') not in ROUND_STATES:
        raise QueueError('本轮调查协议或阶段无效')
    if info['state'] == 'collecting':
        return {'action': 'collect_sources', 'issueId': scan['id'], 'reason': '先完成本轮来源读取，成功窗口不重采'}
    if info.get('registrationComplete') is not True:
        return {'action': 'register_round', 'issueId': scan['id'], 'reason': '只登记本轮冻结清单，不导入历史积压'}
    rows, pending = round_candidates(scan, issues, agent_id)
    if pending:
        if info.get('investigationComplete') is True or info['state'] in {'preparing_repair', 'repairing'}:
            raise QueueError('调查完成标记与逐条证据矛盾，禁止提前修复')
        return {'action': 'investigate', 'issueId': pending[0]['id'], 'roundId': scan['id'],
                'remaining': len(pending), 'reason': '先调查完本轮全部候选，已确认项暂不启动修复'}
    if info.get('investigationComplete') is not True or not verified_record(info.get('investigationIndex')):
        return {'action': 'finalize_round', 'issueId': scan['id'], 'reason': '逐条调查已留档，先汇总并回读本轮完整调查索引'}
    confirmed = [row for row in rows if meta(row, 'dailyInvestigation')['state'] in FIXABLE]
    if meta(scan, 'dailyScan').get('coverageComplete') is not True:
        return {'action': 'attention', 'issueId': scan['id'], 'reason': '已保留调查结论；原扫描缺源，只恢复失败来源后修复'}
    if not confirmed:
        return {'action': 'complete_round', 'issueId': scan['id'], 'reason': '本轮全部调查完成，无新增可修项'}
    batch = meta(scan, 'dailyBatch')
    if not batch.get('bugIds'):
        return {'action': 'prepare_round_repair', 'issueId': scan['id'],
                'investigationIds': [row['id'] for row in sorted(confirmed, key=order)],
                'reason': '全部调查已结束，为已确认项登记唯一固定修复队列；部分登记先恢复现场'}
    bugs = round_bugs(scan, confirmed, by_id)
    bug_ids = batch['bugIds']
    if batch.get('state') == 'finished_with_skips':
        return {'action': 'complete_round', 'issueId': scan['id'], 'outcome': 'finished_with_skips',
                'reason': '保留真人跳过与未完成范围，不将跳过项标为完整交付'}
    if (batch.get('state') != 'completed' or batch.get('outcome') != 'engineering_delivery_completed'
            or batch.get('deliveredBugIds') != bug_ids or not all(verified_delivery(batch, bug) for bug in bugs)):
        raise QueueError('本轮修复尚未完整交付，不能结束本轮')
    return {'action': 'complete_round', 'issueId': scan['id'], 'reason': '本轮全部调查与固定修复交付已核验'}


def round_bugs(scan, confirmed, by_id):
    batch = meta(scan, 'dailyBatch')
    confirmed_by_id = {row['id']: row for row in confirmed}
    bug_ids = batch['bugIds']
    if not isinstance(bug_ids, list) or len(set(bug_ids)) != len(bug_ids) or batch.get('total') != len(bug_ids):
        raise QueueError('本轮固定修复清单数量无效')
    bugs = [by_id.get(key) for key in bug_ids]
    for index, bug in enumerate(bugs, 1):
        item = meta(bug, 'dailyBug') if bug else {}
        if (not bug or bug.get('parent_issue_id') != scan['id'] or item.get('batchId') != scan['id']
                or item.get('mode') != 'bug' or item.get('index') != index or item.get('total') != len(bug_ids)):
            raise QueueError('本轮修复任务缺失或真实父子关系、队列位置错误')
        investigation = confirmed_by_id.get(item.get('investigationIssueId'))
        if not investigation or item.get('candidateId') != meta(investigation, 'dailyInvestigation')['candidateId']:
            raise QueueError('修复任务与已确认调查的候选身份不一致')
    related = [meta(bug, 'dailyBug').get('investigationIssueId') for bug in bugs]
    if len(set(related)) != len(related) or set(related) != set(confirmed_by_id):
        raise QueueError('修复队列与本轮确认项不一致')
    return bugs


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
        scan = by_id.get(batch.get('sourceScanId')) if batch.get('mode') == 'repair' else batch_issue
        if scan and meta(scan, 'dailyRound'):
            info = meta(scan, 'dailyRound')
            if info.get('registrationComplete') is not True:
                raise QueueError('调查清单尚未登记完成，禁止启动或恢复修复')
            members, pending = round_candidates(scan, issues, agent_id)
            if (pending or info.get('investigationComplete') is not True
                    or not verified_record(info.get('investigationIndex'))
                    or meta(scan, 'dailyScan').get('coverageComplete') is not True):
                raise QueueError('本轮全部调查及来源覆盖尚未完成，禁止启动或恢复修复')
            round_bugs(scan, [row for row in members if meta(row, 'dailyInvestigation')['state'] in FIXABLE], by_id)
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

    rounds = [issue for issue in issues if meta(issue, 'dailyRound')
              and meta(issue, 'dailyRound').get('state') != 'completed' and issue.get('status') not in STOPPED]
    if len(rounds) > 1:
        raise QueueError('存在多个未完成调查轮次，须核对现场，不能合并或跳过本轮')
    if rounds:
        return round_plan(rounds[0], issues, by_id, agent_id)
    return {'action': 'idle', 'reason': '没有未完成的新流程轮次；历史待调查及附件不自动恢复或导入'}


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
        plan = next_action(issues, runs, project_id=args.project, agent_id=args.agent,
                           current_issue_id=args.current_issue)
        print(json.dumps({'protocolVersion': PROTOCOL_VERSION, **plan}, ensure_ascii=False))
    except (QueueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f'无法生成接续计划：{error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
