#!/usr/bin/env python3
"""调查调度：工位轮次每轮两张工位卡逐条领取候选，旧协议轮次按逐条调查任务补足两路；
以本机短锁和真实运行回读交接，不再切换 Agent 并发；修复队列全局一次一条。"""
import argparse
import contextlib
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


spec = importlib.util.spec_from_file_location('duty_queue', Path(__file__).with_name('duty-queue.py'))
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
Error = queue.QueueError
LANE_POLICY = 'lane_investigation_serial_repair_v2'
ISSUE_POLICY = 'parallel_investigation_serial_repair_v1'
PROTOCOL_VERSION = 2


def execution(scan, revision, runtime):
    value = queue.meta(scan, 'dailyExecution')
    if value.get('policy') == LANE_POLICY:
        valid = value.get('laneCount') == queue.LANE_COUNT and queue.lane_store(scan)
    elif value.get('policy') == ISSUE_POLICY:
        valid = value.get('investigationConcurrency') == 2 and not queue.lane_store(scan)
    else:
        valid = False
    if (not valid or value.get('schemaVersion') != 1 or value.get('repairConcurrency') != 1
            or value.get('schedulerRevision') != revision or value.get('runtimeId') != runtime
            or re.fullmatch(r'[0-9a-f]{40}', revision) is None):
        raise Error('轮次执行协议、固定版本或 Runtime 不符；不得接管 701a730 串行轮次')
    return value


def check_runs(active, issue_ids, agent_id, runtime_id, workspace_id):
    """本轮任务上的运行必须属于本 Agent；同一任务只允许一个执行中加一个已排队的后继。"""
    for run in active:
        if run.get('issue_id') in issue_ids and (run.get('agent_id') != agent_id or run.get('runtime_id') != runtime_id
                                                 or run.get('workspace_id') != workspace_id):
            raise Error('本轮任务存在其它执行者或 Runtime 的运行')
    for issue_id in issue_ids:
        same_issue = [run for run in active if run.get('issue_id') == issue_id]
        successor = (len(same_issue) == 2 and sum(run['status'] == 'running' for run in same_issue) == 1
                     and sum(run['status'] in {'queued', 'dispatched'} for run in same_issue) == 1)
        if len(same_issue) > 1 and not successor:
            raise Error('同一任务存在多个活动运行，停止派发并保留现场')


def finalize_plan(round_id, current_issue, current_run, active, member_ids, total):
    """调查全部留档后由原扫描唯一汇总。最后一个调查运行在退出前已排汇总后继，后继可能在它退出前开跑；
    此时原扫描再排一个自身后继后退出，等调查运行全部退出再汇总，避免两边都退出后无人接手。"""
    if current_issue == round_id:
        if any(run['issue_id'] == round_id and run['id'] != current_run for run in active):
            return {'action': 'yield_to_finalizer'}
        if not any(run['issue_id'] in member_ids for run in active):
            return {'action': 'finalize_round_here', 'total': total, 'completed': total, 'remaining': 0}
    return {'action': 'finalize_round', 'issueIds': [round_id], 'total': total, 'completed': total, 'remaining': 0}


def lane_plan(scan, issues, active, *, round_id, current_issue, current_run, agent_id, handoff):
    """每个工位同一时间只持有一条候选；结论回读后释放，按清单顺序领下一条并给工位排一个后继。"""
    lanes = queue.round_lanes(scan, issues, agent_id)
    rows, pending = queue.round_candidates(scan, issues, agent_id)
    lane_ids = {lane['id'] for lane in lanes}
    if current_issue != round_id and current_issue not in lane_ids:
        raise Error('当前任务不是本轮调查工位或原扫描')
    if not pending:
        return finalize_plan(round_id, current_issue, current_run, active, lane_ids, len(rows))
    info = queue.meta(scan, 'dailyRound')
    if info.get('investigationComplete') is True or info.get('state') != 'investigating':
        raise Error('尚有未完成调查，不能进入汇总或修复')
    if queue.meta(scan, 'dailyBatch').get('bugIds'):
        raise Error('尚有未完成调查却已登记修复队列')
    if queue.meta(scan, 'dailyScan').get('coverageComplete') is not True:
        raise Error('三源覆盖未完成，保留现场，不能开启调查')
    order = [row['metadata']['dailyInvestigation']['candidateId'] for row in pending]
    live = {}
    for lane in lanes:
        claim = queue.meta(lane, 'dailyLane').get('currentCandidateId')
        if claim in order:
            live[lane['id']] = claim
    if len(set(live.values())) != len(live):
        raise Error('两个工位领取了同一候选，须核对后恢复')
    if current_issue in live:
        if not handoff:
            return {'action': 'investigate_candidate', 'laneIssueId': current_issue, 'candidateId': live[current_issue],
                    'total': len(rows), 'completed': len(rows) - len(pending), 'remaining': len(pending)}
        raise Error('当前工位所领候选尚无有效结论；先保存结论或失败现场，不领取下一条')
    unclaimed = [key for key in order if key not in live.values()]
    claims, launches, released = [], [], []
    for lane in lanes:
        running = any(run['issue_id'] == lane['id'] for run in active)
        if lane['id'] in live:
            if not running:
                launches.append(lane['id'])
            continue
        # 执行中的其它工位结论回读后会自己领下一条，这里只为调用者和空闲工位领取。
        if running and lane['id'] != current_issue:
            continue
        if unclaimed:
            claims.append({'laneId': lane['id'], 'candidateId': unclaimed.pop(0)})
            launches.append(lane['id'])
        elif queue.meta(lane, 'dailyLane').get('state') != 'done':
            released.append(lane['id'])
    return {'action': 'dispatch', 'claims': claims, 'issueIds': launches, 'released': released,
            'total': len(rows), 'completed': len(rows) - len(pending), 'remaining': len(pending)}


def issue_plan(scan, issues, active, *, round_id, current_issue, current_run, agent_id, project_id, workspace_id):
    """旧协议轮次（逐条调查任务）：补足最多两条在途调查，不再要求其它轮次已结束。"""
    rows, pending = queue.round_candidates(scan, issues, agent_id)
    members = {row['id']: row for row in rows}
    for row in rows:
        if row.get('project_id') != project_id or row.get('workspace_id') != workspace_id:
            raise Error('本轮调查跨越项目或工作区')
    if current_issue != round_id and current_issue not in members:
        raise Error('当前任务不是本轮调查或原扫描')
    if not pending:
        return finalize_plan(round_id, current_issue, current_run, active, set(members), len(rows))
    info = queue.meta(scan, 'dailyRound')
    if info.get('investigationComplete') is True or info.get('state') != 'investigating':
        raise Error('尚有未完成调查，不能进入汇总或修复')
    if queue.meta(scan, 'dailyBatch').get('bugIds'):
        raise Error('尚有未完成调查却已登记修复队列')
    if queue.meta(scan, 'dailyScan').get('coverageComplete') is not True:
        raise Error('三源覆盖未完成，保留现场，不能开启调查')
    occupied = {row['id'] for row in pending} & {run['issue_id'] for run in active}
    if len(occupied) > 2:
        raise Error('待完成调查已超过两个名额')
    selected = [row['id'] for row in sorted(pending, key=queue.order) if row['id'] not in occupied][:2 - len(occupied)]
    return {'action': 'dispatch', 'issueIds': selected, 'total': len(rows),
            'completed': len(rows) - len(pending), 'remaining': len(pending)}


def plan(snapshot, *, round_id, current_issue, current_run, agent_id, project_id,
         workspace_id, runtime_id, revision, handoff=False):
    agent, issues, runs = snapshot['agent'], snapshot['issues'], snapshot['runs']
    if (agent.get('id') != agent_id or agent.get('workspace_id') != workspace_id
            or agent.get('runtime_id') != runtime_id or agent.get('max_concurrent_tasks') not in (1, 2)):
        raise Error('Agent 身份、Runtime 或并发现场不符')
    by_id = {row['id']: row for row in issues}
    if len(by_id) != len(issues) or round_id not in by_id or current_issue not in by_id:
        raise Error('项目分页或当前任务不完整')
    scan = by_id[round_id]
    execution(scan, revision, runtime_id)
    for row in (scan, by_id[current_issue]):
        if row.get('project_id') != project_id or row.get('workspace_id') != workspace_id:
            raise Error('当前任务或扫描不属于指定项目与工作区')
    if scan.get('assignee_id') != agent_id or scan.get('status') in queue.STOPPED:
        raise Error('扫描归属不符或已结束，不能重新启动')
    if by_id[current_issue].get('assignee_id') != agent_id:
        raise Error('当前任务归属已改变')
    active = [run for run in runs if run.get('status') in queue.IN_FLIGHT]
    own = [run for run in active if run.get('id') == current_run]
    if (len(own) != 1 or own[0].get('issue_id') != current_issue
            or own[0].get('agent_id') != agent_id or own[0].get('runtime_id') != runtime_id
            or own[0].get('workspace_id') != workspace_id or own[0].get('status') != 'running'):
        raise Error('调用者没有对应的真实执行中 run')
    if queue.meta(scan, 'dailyRound').get('registrationComplete') is not True:
        if current_issue != round_id:
            raise Error('清单未完整登记，不能开启调查')
        return {'action': 'register_round'}
    scope = dict(round_id=round_id, current_issue=current_issue, current_run=current_run, agent_id=agent_id)
    if queue.lane_store(scan):
        lane_ids = {lane['id'] for lane in queue.round_lanes(scan, issues, agent_id)}
        check_runs(active, lane_ids | {round_id}, agent_id, runtime_id, workspace_id)
        return lane_plan(scan, issues, active, handoff=handoff, **scope)
    rows, _ = queue.round_candidates(scan, issues, agent_id)
    check_runs(active, {row['id'] for row in rows} | {round_id}, agent_id, runtime_id, workspace_id)
    return issue_plan(scan, issues, active, project_id=project_id, workspace_id=workspace_id, **scope)


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Error('调度锁等待超时；保留现场后重试同一接续动作')
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


class Journal:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def save(self):
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)


class Native:
    def __init__(self, cli, project_id, agent_id):
        self.cli, self.project_id, self.agent_id = cli, project_id, agent_id

    def call(self, args):
        try:
            result = subprocess.run(self.cli + args + ['--output', 'json'], check=True,
                                    capture_output=True, text=True, timeout=30)
            return json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise Error('Multica 操作未确认；只回读现场，不盲目重复写入') from error

    def issue(self, issue_id):
        return self.call(['issue', 'get', issue_id])

    def history(self, issue_id):
        result = self.call(['issue', 'runs', issue_id])
        if not isinstance(result, list):
            raise Error('运行历史响应不完整')
        return result

    def snapshot(self):
        project = self.call(['project', 'get', self.project_id])
        if project.get('id') != self.project_id:
            raise Error('项目身份不符')
        agent = self.call(['agent', 'get', self.agent_id])
        issues, offset, total = [], 0, None
        while True:
            page = self.call(['issue', 'list', '--project', self.project_id, '--limit', '100',
                              '--offset', str(offset), '--sort', 'created_at', '--direction', 'asc'])
            rows = page.get('issues')
            if (not isinstance(rows, list) or type(page.get('has_more')) is not bool
                    or type(page.get('total')) is not int or page.get('offset') != offset):
                raise Error('项目分页响应不完整')
            total = page['total'] if total is None else total
            if page['total'] != total:
                raise Error('读取期间任务数量变化，请重新读取')
            issues.extend(rows)
            if not page['has_more']:
                break
            if not rows:
                raise Error('任务分页未推进')
            offset += len(rows)
        if len(issues) != total:
            raise Error('任务分页总数不闭合')
        queue.attach_comments(issues, lambda issue_id: self.call(['issue', 'comment', 'list', issue_id,
                                                                    '--full', '--compact']))
        runs = self.call(['agent', 'tasks', self.agent_id])
        if not isinstance(runs, list):
            raise Error('Agent 运行响应不完整')
        return {'agent': agent, 'issues': issues, 'runs': runs}

    def metadata(self, issue_id, key, value):
        try:
            self.call(['issue', 'metadata', 'set', issue_id, '--key', key,
                       '--value', json.dumps(value, ensure_ascii=False, separators=(',', ':'))])
        except Error:
            pass
        if queue.meta(self.issue(issue_id), key) != value:
            raise Error('metadata 写后回读不一致，保留现场')

    def assign_without_start(self, issue_id):
        try:
            self.call(['issue', 'assign', issue_id, '--to-id', self.agent_id, '--no-start'])
        except Error:
            pass
        if self.issue(issue_id).get('assignee_id') != self.agent_id:
            raise Error('调查归属未确认，不能启动')

    def rerun(self, issue_id):
        self.call(['issue', 'rerun', issue_id])

    def status(self, issue_id, value):
        try:
            self.call(['issue', 'status', issue_id, value, '--no-start'])
        except Error:
            pass
        if self.issue(issue_id).get('status') != value:
            raise Error('任务状态写后回读不一致，保留现场')


def launch(native, journal, issue_id, agent_id, runtime_id, *, successor=False, key=None):
    """保存启动前 run 集合；超时后只认实际新 run，未知结果不能重复启动。
    工位每领一条候选用独立 journal 键，后继排在执行中的同一工位之后。"""
    key = key or issue_id
    history = native.history(issue_id)
    active = [run for run in history if run.get('status') in queue.IN_FLIGHT]
    if any(run.get('agent_id') != agent_id or run.get('runtime_id') != runtime_id for run in active):
        raise Error('目标任务存在其它执行者或 Runtime 的运行')
    future = [run for run in active if run.get('status') in {'queued', 'dispatched'}]
    if len(active) > 1 and not (successor and len(active) == 2 and len(future) == 1):
        raise Error('目标任务已有多个活动运行')
    intent = journal.data.get(key)
    if intent:
        found = [run for run in history if run['id'] not in intent['beforeRunIds']]
        if (len(found) == 1 and found[0].get('agent_id') == agent_id
                and found[0].get('runtime_id') == runtime_id):
            journal.data[key] = {**intent, 'runId': found[0]['id']}
            journal.save()
            if found[0].get('status') not in queue.IN_FLIGHT:
                raise Error('既有运行已结束但调查或汇总未完成，保留报告与失败现场')
            return {'issueId': issue_id, 'runId': found[0]['id'], 'status': found[0]['status'], 'reused': True}
        if len(found) > 1:
            raise Error('启动 intent 对应多个运行，须核对后恢复')
        raise Error('已有启动 intent 尚无可确认的新运行，不能重复启动')
    reusable = future if successor else active
    if reusable:
        return {'issueId': issue_id, 'runId': reusable[0]['id'], 'status': reusable[0]['status'], 'reused': True}
    row = native.issue(issue_id)
    if row.get('status') in queue.STOPPED or row.get('assignee_id') not in (None, agent_id):
        raise Error('目标已关闭或转交，不能启动')
    if row.get('assignee_id') is None:
        native.assign_without_start(issue_id)
    # 原生 rerun 保留执行中的 run，后继受服务端同 issue 串行约束。
    intent = {'beforeRunIds': [run['id'] for run in native.history(issue_id)]}
    journal.data[key] = intent
    journal.save()
    try:
        native.rerun(issue_id)
    except Error:
        pass
    history = native.history(issue_id)
    found = [run for run in history if run['id'] not in intent['beforeRunIds']]
    if len(found) != 1 or found[0].get('agent_id') != agent_id or found[0].get('runtime_id') != runtime_id:
        raise Error('启动结果未确认；保留 intent，不自动重发')
    journal.data[key] = {**intent, 'runId': found[0]['id']}
    journal.save()
    if found[0].get('status') not in queue.IN_FLIGHT:
        raise Error('新运行已结束但调查或汇总未完成，须核对真实结论')
    return {'issueId': issue_id, 'runId': found[0]['id'], 'status': found[0]['status'], 'reused': False}


def advance(native, journal, scope, now=None):
    snapshot = native.snapshot()
    result = plan(snapshot, handoff=True, **scope)
    if result['action'] in ('register_round', 'yield_to_finalizer'):
        return result
    round_id = scope['round_id']
    if result['action'] == 'finalize_round_here':
        native.metadata(round_id, 'dailyDispatch', {
            'schemaVersion': 1, 'phase': 'finalizing', 'finalizerRunId': scope['current_run']})
        scan = next(row for row in snapshot['issues'] if row['id'] == round_id)
        if queue.lane_store(scan):
            # 全部工位已退出，此时收尾工位卡不会打断任何运行：标记 done 并把任务置为完成。
            for lane in queue.round_lanes(scan, snapshot['issues'], scope['agent_id']):
                value = {**queue.meta(lane, 'dailyLane'), 'state': 'done', 'currentCandidateId': None}
                if queue.meta(lane, 'dailyLane') != value:
                    native.metadata(lane['id'], 'dailyLane', value)
                if lane.get('status') != 'done':
                    native.status(lane['id'], 'done')
    by_id = {row['id']: row for row in snapshot['issues']}
    receipts, errors, claimed = [], [], {}
    stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    for claim in result.get('claims', []):
        try:
            lane = queue.meta(native.issue(claim['laneId']), 'dailyLane')
            native.metadata(claim['laneId'], 'dailyLane', {**lane, 'state': 'investigating',
                                                           'currentCandidateId': claim['candidateId'],
                                                           'claimedAt': stamp})
            claimed[claim['laneId']] = claim['candidateId']
        except Error as error:
            errors.append({'issueId': claim['laneId'], 'reason': str(error)})
    for lane_id in result.get('released', []):
        try:
            lane = queue.meta(native.issue(lane_id), 'dailyLane')
            native.metadata(lane_id, 'dailyLane', {**lane, 'state': 'done', 'currentCandidateId': None})
        except Error as error:
            errors.append({'issueId': lane_id, 'reason': str(error)})
    for issue_id in result.get('issueIds', []):
        if any(error['issueId'] == issue_id for error in errors):
            continue
        try:
            row = by_id[issue_id]
            if result['action'] == 'dispatch' and queue.meta(row, 'dailyLane'):
                candidate = claimed.get(issue_id) or queue.meta(row, 'dailyLane').get('currentCandidateId')
                receipts.append(launch(native, journal, issue_id, scope['agent_id'], scope['runtime_id'],
                                       successor=issue_id == scope['current_issue'], key=f'{issue_id}:{candidate}'))
                continue
            if result['action'] == 'dispatch':
                # 旧协议逐条调查任务：启动前按最新回读确认仍未完成。
                fresh = native.issue(issue_id)
                refreshed = [fresh if r['id'] == issue_id else r for r in snapshot['issues']]
                _, still_pending = queue.round_candidates(by_id[round_id], refreshed, scope['agent_id'])
                if issue_id not in {r['id'] for r in still_pending}:
                    continue
                if issue_id not in journal.data:
                    item = queue.meta(fresh, 'dailyInvestigation')
                    native.metadata(issue_id, 'dailyInvestigation', {**item, 'state': 'investigating'})
            finalize = result['action'] == 'finalize_round'
            # 每个请求汇总的调查运行各用一条 intent；已有排队后继时直接复用，不重复排队。
            receipts.append(launch(native, journal, issue_id, scope['agent_id'], scope['runtime_id'], successor=finalize,
                                   key=f"{issue_id}:finalize:{scope['current_run']}" if finalize else None))
        except Error as error:
            errors.append({'issueId': issue_id, 'reason': str(error)})
    # 调查运行不写这个共享字段；每次按完整事实重算，避免并发计数丢失。
    if result['action'] == 'dispatch':
        native.metadata(round_id, 'dailyDispatch', {
            'schemaVersion': 1, 'phase': 'investigating', 'total': result['total'],
            'completed': result['completed'], 'remaining': result['remaining']})
    if errors:
        result['action'] = 'attention'
    return {**result, 'receipts': receipts, 'errors': errors}


def release_repair(native, journal, *, agent_id, runtime_id, project_id):
    """一条修复队列结束后，唤起最早一条已完成调查、等待修复名额的轮次去登记修复。"""
    snapshot = native.snapshot()
    issues = snapshot['issues']
    waiting = sorted((row for row in issues if queue.meta(row, 'dailyExecution').get('policy') in (LANE_POLICY, ISSUE_POLICY)
                      and queue.meta(row, 'dailyRound').get('state') not in (None, 'completed')
                      and row.get('status') not in queue.STOPPED),
                     key=lambda row: row.get('created_at') or '')
    errors = []
    for scan in waiting:
        try:
            step = queue.next_action(issues, snapshot['runs'], project_id=project_id, agent_id=agent_id,
                                     current_issue_id=None, round_id=scan['id'])
        except Error as error:
            errors.append({'issueId': scan['id'], 'reason': str(error)})
            continue
        if step['action'] == 'wait_repair_slot':
            return {'action': 'wait', 'issueId': scan['id'], 'reason': step['reason'], 'errors': errors}
        if step['action'] == 'prepare_round_repair':
            receipt = launch(native, journal, scan['id'], agent_id, runtime_id, successor=True, key=f"{scan['id']}:repair")
            return {'action': 'released', 'issueId': scan['id'], 'receipts': [receipt], 'errors': errors}
    if errors:
        return {'action': 'attention', 'errors': errors}
    return {'action': 'idle', 'reason': '没有等待修复名额的轮次'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'advance', 'release-repair'))
    for name in ('server-url', 'workspace-id', 'project-id', 'agent-id', 'runtime-id'):
        parser.add_argument('--' + name, required=True)
    for name in ('round-id', 'current-issue', 'current-run', 'revision'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    scope = {key: getattr(args, key) for key in ('workspace_id', 'project_id', 'agent_id', 'runtime_id',
                                                'round_id', 'current_issue', 'current_run', 'revision')}
    try:
        # 同一 Runtime 上全部 worktree 共用；调用方不能指定另一个锁路径绕过互斥。
        ids = [args.workspace_id, args.project_id, args.agent_id] + ([args.round_id] if args.round_id else [])
        for value in ids:
            if re.fullmatch(r'[a-zA-Z0-9-]+', value) is None:
                raise Error('作用域 ID 格式无效')
        if args.command != 'release-repair' and not all(scope[key] for key in scope):
            raise Error('plan/advance 必须提供轮次、当前任务、当前运行与固定版本')
        root = Path.home() / '.config/moego/daily-bug-duty/dispatch' / args.workspace_id / args.agent_id
        native = Native(['multica', '--server-url', args.server_url, '--workspace-id', args.workspace_id],
                        args.project_id, args.agent_id)
        with locked(root / (args.project_id + '.lock')):
            if args.command == 'plan':
                result = plan(native.snapshot(), **scope)
            elif args.command == 'advance':
                result = advance(native, Journal(root / (args.round_id + '.json')), scope)
            else:
                result = release_repair(native, Journal(root / 'release-repair.json'), agent_id=args.agent_id,
                                        runtime_id=args.runtime_id, project_id=args.project_id)
        print(json.dumps({'protocolVersion': PROTOCOL_VERSION, **result}, ensure_ascii=False))
        return 1 if result['action'] == 'attention' else 0
    except (Error, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'调查调度未完成：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
