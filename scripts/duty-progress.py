"""只读推导每日值守阶段快照；复用调度器的证据与交付门槛，不推进业务状态。"""
import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('duty_queue', Path(__file__).with_name('duty-queue.py'))
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
SOURCES = ('canny', 'facebook', 'intercom')


def count(value):
    return type(value) is int and value >= 0


def collection(scan, scan_record=None):
    data = queue.meta(scan, 'dailyScan')
    values = data.get('collected')
    # 旧轮只约定最终台账保存采集数；读取时兼容，不为看板迁移旧轮 metadata。
    if values is None:
        values = (scan_record or {}).get('collected')
    if values is not None and (not isinstance(values, dict)
                              or any(not count(value) for value in values.values())):
        raise queue.QueueError('采集数量不是有效非负整数')
    known = {key: values[key] for key in SOURCES if isinstance(values, dict) and count(values.get(key))}
    covered = data.get('coverageComplete', (scan_record or {}).get('coverageComplete'))
    complete_counts = len(known) == len(SOURCES)
    state = 'completed' if covered is True and complete_counts else 'collecting'
    sources = data.get('sources') or {}
    if not isinstance(sources, dict):
        raise queue.QueueError('来源状态格式无效')
    if any(isinstance(value, dict) and value.get('status') in {'failed', 'error'} for value in sources.values()):
        state = 'incomplete'
    return {'state': state, 'collected': known or None,
            'total': sum(known.values()) if complete_counts else None, 'coverageComplete': covered}


def investigation(scan, issues):
    info = queue.meta(scan, 'dailyRound')
    if not info:
        return {'state': 'legacy', 'completed': None, 'total': None, 'confirmed': None}, []
    if info.get('registrationComplete') is not True:
        return {'state': 'registering', 'completed': None, 'total': info.get('candidateCount'), 'confirmed': None}, []
    members, pending = queue.round_candidates(scan, issues, scan.get('assignee_id'))
    for row in members:
        if row.get('project_id') != scan.get('project_id') or row.get('workspace_id') != scan.get('workspace_id'):
            raise queue.QueueError('调查任务不属于原扫描的项目或工作区')
        item = queue.meta(row, 'dailyInvestigation')
        if item.get('schemaVersion') != 1 or item.get('mode') != 'investigate' or item.get('state') not in queue.STATES:
            raise queue.QueueError('调查入口协议或状态无效')
    pending_ids = {row['id'] for row in pending}
    concluded = [row for row in members if row['id'] not in pending_ids]
    states = [queue.meta(row, 'dailyInvestigation')['state'] for row in concluded]
    result = {'state': 'investigating' if pending else 'completed', 'total': len(members),
              'completed': len(concluded), 'confirmed': sum(state in queue.FIXABLE for state in states),
              'confirmedIssueIds': [row['id'] for row in concluded
                                    if queue.meta(row, 'dailyInvestigation')['state'] in queue.FIXABLE],
              'duplicate': states.count('duplicate'), 'excluded': states.count('excluded'),
              'csFollowed': states.count('cs_followed'), 'skippedFailure': states.count('skipped_failure')}
    return result, members


def repair(scan, issues, investigated, members):
    batch, info = queue.meta(scan, 'dailyBatch'), queue.meta(scan, 'dailyRound')
    if not batch.get('bugIds'):
        if batch.get('state') == 'preparing':
            state = 'preparing'
        elif investigated['state'] == 'completed' and investigated.get('confirmed') == 0:
            state = 'not_needed'
        else:
            state = 'not_started'
        return {'state': state, 'completed': 0, 'total': None}, []
    if investigated['state'] != 'completed' or info.get('investigationComplete') is not True:
        raise queue.QueueError('调查尚未全部留档，不能将已有修复登记显示为正常修复')
    if not queue.verified_record(info.get('investigationIndex')) or queue.meta(scan, 'dailyScan').get('coverageComplete') is not True:
        raise queue.QueueError('完整调查索引或来源覆盖尚未核对')
    confirmed = [row for row in members if queue.meta(row, 'dailyInvestigation')['state'] in queue.FIXABLE]
    bugs = queue.round_bugs(scan, confirmed, {row['id']: row for row in issues})
    complete = sum(queue.verified_delivery(batch, row) for row in bugs)
    # 只统计已回读受阻通知的项；队列越过它们继续，受阻不计入完整交付。
    blocked = sum(queue.verified_block(batch, row) for row in bugs)
    state = 'repairing'
    if batch.get('state') in {'blocked', 'paused', 'cancelled'}:
        state = batch['state']
    elif batch.get('state') in queue.FINISHED_BATCHES:
        final = queue.round_plan(scan, issues, {row['id']: row for row in issues}, scan.get('assignee_id'))
        if final['action'] != 'complete_round':
            raise queue.QueueError('修复完成标记缺少对应版本的完整交付回执')
        state = {'finished_with_skips': 'completed_with_skips',
                 'finished_with_blocked': 'completed_with_blocked'}.get(final.get('outcome'), 'completed')
    current = batch.get('currentIssueId')
    if state == 'repairing' and (type(batch.get('index')) is not int
            or not 1 <= batch['index'] <= len(bugs) or current != bugs[batch['index'] - 1]['id']):
        raise queue.QueueError('固定修复游标与当前任务不一致')
    return {'state': state, 'completed': complete, 'blocked': blocked, 'total': len(bugs), 'index': batch.get('index'),
            'currentIssueId': current}, bugs


def snapshot(scan, issues, runs, now, issue_url_base, scan_record=None):
    """阶段结论来自已保存证据；当前处理只认真实 run，不把 index 或 intent 当成正在执行。"""
    result = {'snapshotAt': now.isoformat(), 'scanIssue': scan['identifier'], 'activityKnown': runs is not None,
              'scanUrl': issue_url_base + scan['identifier'], 'current': [], 'warnings': []}
    members, bugs = [], []
    for name in ('collection', 'investigation', 'repair'):
        try:
            if name == 'collection':
                result[name] = collection(scan, scan_record)
            elif name == 'investigation':
                result[name], members = investigation(scan, issues)
            else:
                result[name], bugs = repair(scan, issues, result['investigation'], members)
        except (queue.QueueError, KeyError, TypeError, ValueError):
            # 阶段读取失败不能伪装为 0 或未扫描，也不能阻断其它已有事实的展示。
            result[name] = {'state': 'attention', 'completed': None, 'total': None, 'confirmed': None}
            label = {'collection': '采集', 'investigation': '调查', 'repair': '修复'}[name]
            result['warnings'].append(f"{scan['identifier']} 的{label}阶段数据不完整或证据待核对")
    lanes = []
    if queue.lane_store(scan):
        try:
            lanes = queue.round_lanes(scan, issues, scan.get('assignee_id'))
        except queue.QueueError:
            result['warnings'].append(f"{scan['identifier']} 的调查工位登记待核对")
    by_id = {row['id']: row for row in [scan, *members, *bugs, *lanes]}
    member_ids, bug_ids = {row['id'] for row in members} | {row['id'] for row in lanes}, {row['id'] for row in bugs}
    titles = {queue.meta(row, 'dailyInvestigation').get('candidateId'): row['title'].split('｜')[-1] for row in members}
    if runs is None:
        result['warnings'].append('值守运行状态读取失败，当前处理任务未知')
    else:
        for run in sorted(runs, key=lambda row: row.get('status') != 'running'):
            row = by_id.get(run.get('issue_id'))
            if (not row or run.get('status') not in queue.IN_FLIGHT
                    or run.get('agent_id') != scan.get('assignee_id')):
                continue
            scan_phase = '采集' if result['collection']['state'] != 'completed' else '扫描汇总'
            phase = '调查' if row['id'] in member_ids else '修复' if row['id'] in bug_ids else scan_phase
            summary = row['title'].split('｜')[-1]
            claim = queue.meta(row, 'dailyLane').get('currentCandidateId')
            if claim in titles:
                # 工位卡展示「工位 n/2：正在查的候选」，便于看出两路各自在查什么。
                summary = f'{summary}：{titles[claim]}'
            result['current'].append({'identifier': row['identifier'], 'url': issue_url_base + row['identifier'],
                                      'summary': summary, 'phase': phase, 'status': run['status']})
    if result['investigation']['state'] == 'attention' or result['repair']['state'] == 'attention':
        result['activityKnown'] = False
    inv, fix = result['investigation'], result['repair']
    if result['collection']['state'] != 'completed':
        next_action = '继续完成本轮采集与覆盖核对，成功来源不重采'
    elif inv['state'] == 'registering':
        next_action = '完成本轮清单登记后调查'
    elif inv['state'] == 'investigating':
        next_action = '完成本轮剩余调查并汇总，再串行修复'
    elif inv['state'] == 'attention' or fix['state'] == 'attention':
        next_action = '核对阶段证据或暂停现场，保留已有结果'
    elif fix['state'] == 'blocked':
        next_action = '保留受阻证据，未全部合并项不上线且不自动补验收；该批次仍停在阻塞位置，接续状态待核对'
    elif fix['state'] in {'paused', 'cancelled'}:
        next_action = '保留暂停或取消现场，等待真人决定后续范围'
    elif fix['state'] == 'not_needed':
        next_action = '本轮无新增可修项，保留完整调查结论'
    elif fix['state'] in {'completed', 'completed_with_skips'}:
        next_action = '工程处理已结束，按既有流程审查 PR 与交付报告'
    elif fix['state'] == 'completed_with_blocked':
        next_action = '固定队列已结束；保留受阻证据，未全部合并的受阻项不上线，仅真人明确要求恢复时续作'
    elif fix['state'] == 'repairing':
        next_action = '当前项完整验收交付后，串行继续固定修复队列'
    else:
        next_action = '统一汇总调查结论，登记固定修复队列'
    result['nextAction'] = next_action
    return result
