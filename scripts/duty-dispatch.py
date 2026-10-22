#!/usr/bin/env python3
"""新轮次调查并发 2；以本机短锁和真实运行回读交接，收尾与修复恢复并发 1。"""
import argparse
import contextlib
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
POLICY = 'parallel_investigation_serial_repair_v1'
PROTOCOL_VERSION = 1


def execution(scan, revision, runtime):
    value = queue.meta(scan, 'dailyExecution')
    if (value.get('schemaVersion') != 1 or value.get('policy') != POLICY
            or value.get('investigationConcurrency') != 2 or value.get('repairConcurrency') != 1
            or value.get('schedulerRevision') != revision or value.get('runtimeId') != runtime
            or re.fullmatch(r'[0-9a-f]{40}', revision) is None):
        raise Error('轮次执行协议、固定版本或 Runtime 不符；不得接管旧串行轮次')
    return value


def plan(snapshot, *, round_id, current_issue, current_run, agent_id, project_id,
         workspace_id, runtime_id, revision):
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
    if any(row['id'] != round_id and queue.meta(row, 'dailyRound')
           and queue.meta(row, 'dailyRound').get('state') != 'completed'
           and row.get('status') not in queue.STOPPED for row in issues):
        raise Error('存在其它未结束轮次，不能切换全局并发')
    active = [run for run in runs if run.get('status') in queue.IN_FLIGHT]
    own = [run for run in active if run.get('id') == current_run]
    if (len(own) != 1 or own[0].get('issue_id') != current_issue
            or own[0].get('agent_id') != agent_id or own[0].get('runtime_id') != runtime_id
            or own[0].get('workspace_id') != workspace_id or own[0].get('status') != 'running'):
        raise Error('调用者没有对应的真实执行中 run')
    info = queue.meta(scan, 'dailyRound')
    if info.get('registrationComplete') is not True:
        if current_issue != round_id or agent['max_concurrent_tasks'] != 1:
            raise Error('清单未完整登记，不能开启并发调查')
        return {'action': 'register_round', 'concurrency': 1}
    rows, pending = queue.round_candidates(scan, issues, agent_id)
    members = {row['id']: row for row in rows}
    for row in rows:
        if row.get('project_id') != project_id or row.get('workspace_id') != workspace_id:
            raise Error('本轮调查跨越项目或工作区')
        item = queue.meta(row, 'dailyInvestigation')
        if (item.get('schemaVersion') != 1 or item.get('mode') != 'investigate'
                or item.get('state') not in queue.STATES):
            raise Error('本轮调查入口协议或状态无效')
    if current_issue != round_id and current_issue not in members:
        raise Error('当前任务不是本轮调查或原扫描')
    for run in active:
        if (run.get('agent_id') != agent_id or run.get('runtime_id') != runtime_id
                or run.get('workspace_id') != workspace_id
                or run.get('issue_id') not in {round_id, *members}):
            raise Error('存在本轮之外的活动任务，不能调整全局并发')
    active_ids = [run['issue_id'] for run in active]
    for issue_id in set(active_ids):
        same_issue = [run for run in active if run['issue_id'] == issue_id]
        # 原扫描可有一个仍在退出的旧运行和一个已持久排队的汇总后继。
        successor = (not pending and issue_id == round_id and len(same_issue) == 2
                     and sum(run['status'] == 'running' for run in same_issue) == 1
                     and sum(run['status'] in {'queued', 'dispatched'} for run in same_issue) == 1)
        if len(same_issue) > 1 and not successor:
            raise Error('同一任务存在多个活动运行，停止派发并保留现场')
    if pending:
        if info.get('investigationComplete') is True or info.get('state') != 'investigating':
            raise Error('尚有未完成调查，不能进入汇总或修复')
        if queue.meta(scan, 'dailyBatch').get('bugIds'):
            raise Error('尚有未完成调查却已登记修复队列')
        if queue.meta(scan, 'dailyScan').get('coverageComplete') is not True:
            raise Error('三源覆盖未完成，保留现场，不能开启新轮并发')
        occupied = {row['id'] for row in pending} & set(active_ids)
        if len(occupied) > 2:
            raise Error('待完成调查已超过两个名额')
        selected = [row['id'] for row in sorted(pending, key=queue.order)
                    if row['id'] not in occupied][:2 - len(occupied)]
        # 已有有效结论的运行只是在退出；其物理名额仍由 Runtime 的上限控制。
        return {'action': 'dispatch', 'concurrency': 2, 'issueIds': selected,
                'total': len(rows), 'completed': len(rows) - len(pending), 'remaining': len(pending)}
    if current_issue == round_id:
        if any(run['issue_id'] == round_id and run['id'] != current_run for run in active):
            return {'action': 'yield_to_finalizer', 'concurrency': 1}
        return {'action': 'finalize_round_here', 'concurrency': 1, 'total': len(rows),
                'completed': len(rows), 'remaining': 0}
    return {'action': 'finalize_round', 'concurrency': 1, 'issueIds': [round_id],
            'total': len(rows), 'completed': len(rows), 'remaining': 0}


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

    def concurrency(self, limit):
        before = self.call(['agent', 'get', self.agent_id])
        if before.get('max_concurrent_tasks') == limit:
            return
        try:
            self.call(['agent', 'update', self.agent_id, '--max-concurrent-tasks', str(limit)])
        except Error:
            pass
        after = self.call(['agent', 'get', self.agent_id])
        if after.get('max_concurrent_tasks') != limit or after.get('runtime_id') != before.get('runtime_id'):
            raise Error('Agent 阶段并发写后回读不一致，不能继续交接')

    def assign_without_start(self, issue_id):
        try:
            self.call(['issue', 'assign', issue_id, '--to-id', self.agent_id, '--no-start'])
        except Error:
            pass
        if self.issue(issue_id).get('assignee_id') != self.agent_id:
            raise Error('调查归属未确认，不能启动')

    def rerun(self, issue_id):
        self.call(['issue', 'rerun', issue_id])


def launch(native, journal, issue_id, agent_id, runtime_id, *, finalizer=False, accepted_run=None):
    """保存启动前 run 集合；超时后只认实际新 run，未知结果不能重复启动。"""
    history = native.history(issue_id)
    active = [run for run in history if run.get('status') in queue.IN_FLIGHT]
    if any(run.get('agent_id') != agent_id or run.get('runtime_id') != runtime_id for run in active):
        raise Error('目标任务存在其它执行者或 Runtime 的运行')
    future = [run for run in active if run.get('status') in {'queued', 'dispatched'}
              or run.get('id') == accepted_run]
    if len(active) > 1 and not (finalizer and len(active) == 2 and len(future) == 1):
        raise Error('目标任务已有多个活动运行')
    intent = journal.data.get(issue_id)
    if intent:
        found = [run for run in history if run['id'] not in intent['beforeRunIds']]
        if (len(found) == 1 and found[0].get('agent_id') == agent_id
                and found[0].get('runtime_id') == runtime_id):
            journal.data[issue_id] = {**intent, 'runId': found[0]['id']}
            journal.save()
            if found[0].get('status') not in queue.IN_FLIGHT:
                raise Error('既有运行已结束但调查或汇总未完成，保留报告与失败现场')
            return {'issueId': issue_id, 'runId': found[0]['id'], 'status': found[0]['status'], 'reused': True}
        if len(found) > 1:
            raise Error('启动 intent 对应多个运行，须核对后恢复')
        raise Error('已有启动 intent 尚无可确认的新运行，不能重复启动')
    reusable = future if finalizer else active
    if reusable:
        return {'issueId': issue_id, 'runId': reusable[0]['id'], 'status': reusable[0]['status'], 'reused': True}
    row = native.issue(issue_id)
    if row.get('status') in queue.STOPPED or row.get('assignee_id') not in (None, agent_id):
        raise Error('目标已关闭或转交，不能启动')
    if row.get('assignee_id') is None:
        native.assign_without_start(issue_id)
    # 仅汇总可在旧 scan 仍执行时排一个后继。原生 rerun 保留 executing run，
    # 后继受服务端同 issue 串行约束；复用未经接管确认的旧 run 会丢掉退出窗口内的请求。
    intent = {'beforeRunIds': [run['id'] for run in native.history(issue_id)]}
    journal.data[issue_id] = intent
    journal.save()
    try:
        native.rerun(issue_id)
    except Error:
        pass
    history = native.history(issue_id)
    found = [run for run in history if run['id'] not in intent['beforeRunIds']]
    if len(found) != 1 or found[0].get('agent_id') != agent_id or found[0].get('runtime_id') != runtime_id:
        raise Error('启动结果未确认；保留 intent，不自动重发')
    journal.data[issue_id] = {**intent, 'runId': found[0]['id']}
    journal.save()
    if found[0].get('status') not in queue.IN_FLIGHT:
        raise Error('新运行已结束但调查或汇总未完成，须核对真实结论')
    return {'issueId': issue_id, 'runId': found[0]['id'], 'status': found[0]['status'], 'reused': False}


def advance(native, journal, scope):
    snapshot = native.snapshot()
    result = plan(snapshot, **scope)
    if result['action'] == 'register_round':
        return result
    native.concurrency(result['concurrency'])
    if result['action'] == 'finalize_round_here':
        native.metadata(scope['round_id'], 'dailyDispatch', {
            'schemaVersion': 1, 'phase': 'finalizing', 'finalizerRunId': scope['current_run']})
    receipts, errors = [], []
    for issue_id in result.get('issueIds', []):
        try:
            row = native.issue(issue_id)
            if result['action'] == 'dispatch':
                item = queue.meta(row, 'dailyInvestigation')
                scan = next(r for r in snapshot['issues'] if r['id'] == scope['round_id'])
                refreshed = [row if r['id'] == issue_id else r for r in snapshot['issues']]
                _, still_pending = queue.round_candidates(scan, refreshed, scope['agent_id'])
                if issue_id not in {r['id'] for r in still_pending}:
                    continue
                if issue_id not in journal.data:
                    native.metadata(issue_id, 'dailyInvestigation', {**item, 'state': 'investigating'})
            finalizer = result['action'] == 'finalize_round'
            accepted = queue.meta(row, 'dailyDispatch').get('finalizerRunId') if finalizer else None
            receipts.append(launch(native, journal, issue_id, scope['agent_id'], scope['runtime_id'],
                                   finalizer=finalizer, accepted_run=accepted))
        except Error as error:
            errors.append({'issueId': issue_id, 'reason': str(error)})
    # 调查运行不会写这个共享字段；每次按完整事实重算，避免并发计数丢失。
    if result['action'] == 'dispatch':
        native.metadata(scope['round_id'], 'dailyDispatch', {
            'schemaVersion': 1, 'phase': 'investigating', 'total': result['total'],
            'completed': result['completed'], 'remaining': result['remaining']})
    if errors:
        result['action'] = 'attention'
    return {**result, 'receipts': receipts, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'advance'))
    for name in ('server-url', 'workspace-id', 'project-id', 'agent-id', 'runtime-id',
                 'round-id', 'current-issue', 'current-run', 'revision'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    scope = {key: getattr(args, key) for key in ('workspace_id', 'project_id', 'agent_id', 'runtime_id',
                                                'round_id', 'current_issue', 'current_run', 'revision')}
    try:
        # 同一 Runtime 上全部 worktree 共用；调用方不能指定另一个锁路径绕过互斥。
        for value in (args.workspace_id, args.project_id, args.agent_id, args.round_id):
            if re.fullmatch(r'[a-zA-Z0-9-]+', value) is None:
                raise Error('作用域 ID 格式无效')
        root = Path.home() / '.config/moego/daily-bug-duty/dispatch' / args.workspace_id / args.agent_id
        native = Native(['multica', '--server-url', args.server_url, '--workspace-id', args.workspace_id],
                        args.project_id, args.agent_id)
        with locked(root / (args.project_id + '.lock')):
            if args.command == 'plan':
                result = plan(native.snapshot(), **scope)
            else:
                result = advance(native, Journal(root / (args.round_id + '.json')), scope)
        print(json.dumps({'protocolVersion': PROTOCOL_VERSION, **result}, ensure_ascii=False))
        return 1 if result['action'] == 'attention' else 0
    except (Error, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'调查调度未完成：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
