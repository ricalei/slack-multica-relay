"""两个调查同时交接、真实启动回读、单一汇总与旧轮隔离的定向回归。"""
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from duty_queue_test import candidate, conclude, freeze, issue, lane_freeze, record, suspended


spec = importlib.util.spec_from_file_location('duty_dispatch', Path(__file__).parents[1] / 'scripts/duty-dispatch.py')
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)
REVISION = 'a' * 40
SCOPE = dict(round_id='scan', current_issue='scan', current_run='scan-run', agent_id='agent',
             project_id='project', workspace_id='workspace', runtime_id='runtime', revision=REVISION)


def run(issue_id, run_id=None, status='running'):
    return {'id': run_id or issue_id + '-run', 'issue_id': issue_id, 'agent_id': 'agent',
            'workspace_id': 'workspace', 'runtime_id': 'runtime', 'status': status}


def snapshot(rows):
    scan = issue('scan', dailyScan={'coverageComplete': True}, dailyExecution={
        'schemaVersion': 1, 'policy': dispatch.ISSUE_POLICY, 'investigationConcurrency': 2,
        'repairConcurrency': 1, 'schedulerRevision': REVISION, 'runtimeId': 'runtime'})
    scan.update(assignee_id='agent', status='in_progress')
    freeze(scan, rows)
    issues = [scan, *rows]
    for row in issues:
        row['workspace_id'] = 'workspace'
    return {'agent': {'id': 'agent', 'workspace_id': 'workspace', 'runtime_id': 'runtime',
                      'max_concurrent_tasks': 1}, 'issues': issues, 'runs': [run('scan')]}


class FakeNative:
    def __init__(self, data):
        self.data = data
        self.starts = []
        self.limits = []
        self.failure = None
        self.writes = []

    def snapshot(self):
        return copy.deepcopy(self.data)

    def issue(self, key):
        return copy.deepcopy(next(row for row in self.data['issues'] if row['id'] == key))

    def history(self, key):
        return copy.deepcopy([r for r in self.data['runs'] if r['issue_id'] == key])

    def metadata(self, key, name, value):
        self.writes.append((key, name))
        next(row for row in self.data['issues'] if row['id'] == key)['metadata'][name] = value

    def save_skip(self, issue_id, item, *, allow_write, before_write):
        if allow_write:
            before_write()
        row = next(row for row in self.data['issues'] if row['id'] == issue_id)
        from duty_queue_test import comment
        row['_comments'].append(comment(dispatch.queue.RECORD_MARKER, item, at='2026-10-23T10:00:00Z'))
        self.writes.append((issue_id, 'failureSkip'))

    def concurrency(self, limit):
        self.limits.append(limit)
        self.data['agent']['max_concurrent_tasks'] = limit

    def assign_without_start(self, key):
        next(row for row in self.data['issues'] if row['id'] == key)['assignee_id'] = 'agent'

    def status(self, key, value):
        self.writes.append((key, 'status'))
        next(row for row in self.data['issues'] if row['id'] == key)['status'] = value

    def rerun(self, key):
        self.starts.append(key)
        if self.failure != 'before':
            count = self.starts.count(key)
            self.data['runs'].append(run(key, 'new-' + key + ('' if count == 1 else f'-{count}'), 'queued'))
        if self.failure:
            raise dispatch.Error('模拟超时，不能从退出码推断启动失败')


class PlanTests(unittest.TestCase):
    def test_available_slots_are_filled_without_exceeding_two(self):
        data = snapshot([candidate(str(i)) for i in range(4)])
        for occupied in range(3):
            with self.subTest(occupied=occupied):
                data['runs'] = [run('scan'), *[run(str(i)) for i in range(occupied)]]
                result = dispatch.plan(data, **SCOPE)
                self.assertEqual(result['issueIds'], [str(i) for i in range(occupied, 2)])

    def test_draining_conclusions_do_not_leave_the_next_slot_empty(self):
        data = snapshot([candidate('first', 'excluded'), candidate('second', 'confirmed'), candidate('third')])
        data['runs'] += [run('first'), run('second')]
        result = dispatch.plan(data, **SCOPE)
        self.assertEqual((result['completed'], result['remaining'], result['issueIds']), (2, 1, ['third']))

    def test_all_conclusions_return_to_original_scan_before_repair(self):
        for rows in ([], [candidate(state='confirmed')], [suspended()]):
            with self.subTest(rows=len(rows)):
                data = snapshot(rows)
                result = dispatch.plan(data, **SCOPE)
                self.assertEqual(result['action'], 'finalize_round_here')
                self.assertFalse(data['issues'][0]['metadata']['dailyRound']['investigationComplete'])

    def test_missing_evidence_is_still_pending_and_never_becomes_delivery(self):
        row = suspended()
        del row['metadata']['dailyInvestigation']['blocker']['attempts']['verification']
        data = snapshot([row])
        result = dispatch.plan(data, **SCOPE)
        self.assertEqual((result['remaining'], result['issueIds']), (1, ['external']))
        self.assertNotIn('dailyBug', row['metadata'])

    def test_legacy_round_and_changed_version_or_runtime_cannot_be_migrated(self):
        original = snapshot([candidate()])
        for key, value in [('dailyExecution', None), ('schedulerRevision', 'b' * 40), ('runtimeId', 'other')]:
            data = copy.deepcopy(original)
            if key == 'dailyExecution':
                del data['issues'][0]['metadata'][key]
            else:
                data['issues'][0]['metadata']['dailyExecution'][key] = value
            before = copy.deepcopy(data)
            with self.subTest(key=key), self.assertRaises(dispatch.Error):
                dispatch.plan(data, **SCOPE)
            self.assertEqual(data, before)

    def test_wrong_scope_transfer_or_duplicate_run_fails_closed(self):
        data = snapshot([candidate()])
        variants = []
        for key in ('agent_id', 'runtime_id', 'workspace_id', 'issue_id'):
            changed = copy.deepcopy(data)
            changed['runs'][0][key] = 'other'
            variants.append(changed)
        changed = copy.deepcopy(data)
        changed['issues'][1]['parent_issue_id'] = 'other'
        variants.append(changed)
        changed = copy.deepcopy(data)
        changed['issues'][0]['assignee_id'] = 'other'
        variants.append(changed)
        changed = copy.deepcopy(data)
        changed['runs'].append(run('scan', 'duplicate-run'))
        variants.append(changed)
        for index, changed in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(dispatch.Error):
                dispatch.plan(changed, **SCOPE)

    def test_paused_cancelled_or_unregistered_candidates_are_not_skipped_or_reopened(self):
        for state in ('paused', 'cancelled'):
            with self.subTest(state=state), self.assertRaises(dispatch.Error):
                dispatch.plan(snapshot([candidate(state=state)]), **SCOPE)
        data = snapshot([candidate()])
        data['issues'][0]['metadata']['dailyRound']['candidateCount'] = 2
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)

    def test_registration_and_coverage_cannot_be_bypassed(self):
        data = snapshot([candidate()])
        data['issues'][0]['metadata']['dailyRound']['registrationComplete'] = False
        self.assertEqual(dispatch.plan(data, **SCOPE)['action'], 'register_round')
        data['agent']['max_concurrent_tasks'] = 3
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)
        data = snapshot([candidate()])
        data['issues'][0]['metadata']['dailyScan']['coverageComplete'] = False
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)

    def test_early_repair_or_complete_marker_cannot_hide_pending_work(self):
        for key, value in [('dailyBatch', {'bugIds': ['early-bug']}),
                           ('dailyRound', {'investigationComplete': True})]:
            data = snapshot([candidate()])
            data['issues'][0]['metadata'].setdefault(key, {}).update(value)
            with self.subTest(key=key), self.assertRaises(dispatch.Error):
                dispatch.plan(data, **SCOPE)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def advance(self, native, scope=SCOPE):
        with dispatch.locked(self.root / 'dispatch.lock'):
            return dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), scope)

    def test_concurrent_completions_fill_two_slots_once_and_keep_latest_progress(self):
        data = snapshot([candidate('1', 'excluded'), candidate('2', 'confirmed'),
                         candidate('3'), candidate('4'), candidate('5')])
        data['runs'] = [run('1'), run('2')]
        for row in data['issues'][1:3]:
            row['assignee_id'] = 'agent'
        native = FakeNative(data)
        scopes = [{**SCOPE, 'current_issue': key, 'current_run': key + '-run'} for key in ('1', '2')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda scope: self.advance(native, scope), scopes))
        self.assertEqual(native.starts, ['3', '4'])
        self.assertEqual([r['action'] for r in results], ['dispatch', 'dispatch'])
        self.assertEqual(native.issue('scan')['metadata']['dailyDispatch']['completed'], 2)
        # 调度器不写任何调查结论、父轮完成标记或修复批次。
        self.assertEqual([key for _, key in native.writes if key != 'dailyDispatch'],
                         ['dailyInvestigation', 'dailyInvestigation'])
        self.assertFalse(native.issue('scan')['metadata']['dailyRound']['investigationComplete'])

    def test_last_two_workers_enqueue_one_finalizer_without_changing_concurrency(self):
        data = snapshot([candidate('first', 'excluded'), candidate('second', 'confirmed')])
        data['runs'] = [run('first'), run('second')]
        data['agent']['max_concurrent_tasks'] = 2
        for row in data['issues'][1:]:
            row['assignee_id'] = 'agent'
        native = FakeNative(data)
        scopes = [{**SCOPE, 'current_issue': key, 'current_run': key + '-run'} for key in ('first', 'second')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda scope: self.advance(native, scope), scopes))
        self.assertEqual(native.starts, ['scan'])
        self.assertEqual((native.data['agent']['max_concurrent_tasks'], native.limits), (2, []))
        self.assertTrue(all(r['action'] == 'finalize_round' for r in results))
        self.assertEqual(results[0]['receipts'][0]['runId'], results[1]['receipts'][0]['runId'])
        self.assertFalse(native.issue('scan')['metadata']['dailyRound']['investigationComplete'])

    def test_finalizer_does_not_rerun_itself(self):
        native = FakeNative(snapshot([candidate(state='excluded')]))
        result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('finalize_round_here', []))

    def test_last_worker_queues_a_successor_while_original_scan_is_exiting(self):
        data = snapshot([candidate('last', 'confirmed')])
        data['issues'][1]['assignee_id'] = 'agent'
        data['runs'].append(run('last'))
        native = FakeNative(data)
        worker = {**SCOPE, 'current_issue': 'last', 'current_run': 'last-run'}
        result = self.advance(native, worker)
        self.assertEqual((result['action'], native.starts), ('finalize_round', ['scan']))
        self.assertEqual(result['receipts'][0]['runId'], 'new-scan')
        self.assertEqual(native.data['runs'][0]['status'], 'running')
        # 旧 scan 的最后一次退出检查发生在后继排队后，只让出，不做第二次汇总。
        self.assertEqual(self.advance(native)['action'], 'yield_to_finalizer')
        self.advance(native, worker)
        self.assertEqual(native.starts, ['scan'])
        for old in native.data['runs'][:2]:
            old['status'] = 'completed'
        native.data['runs'][-1]['status'] = 'running'
        finalizer = {**SCOPE, 'current_run': 'new-scan'}
        self.assertEqual(self.advance(native, finalizer)['action'], 'finalize_round_here')
        self.assertEqual(native.issue('scan')['metadata']['dailyDispatch']['finalizerRunId'], 'new-scan')
        serial = dict(project_id='project', agent_id='agent', current_issue_id='scan')
        self.assertEqual(dispatch.queue.next_action(data['issues'], data['runs'], **serial)['action'],
                         'finalize_round')
        data['issues'][0]['metadata']['dailyRound'].update(
            state='ready_repair', investigationComplete=True, investigationIndex=record('index'))
        self.assertEqual(dispatch.queue.next_action(data['issues'], data['runs'], **serial)['action'],
                         'prepare_round_repair')

    def test_original_scan_with_draining_worker_queues_one_successor_and_exits(self):
        data = snapshot([candidate('last', 'excluded')])
        data['issues'][1]['assignee_id'] = 'agent'
        data['runs'].append(run('last'))
        native = FakeNative(data)
        result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('finalize_round', ['scan']))
        self.assertNotIn('dailyDispatch', native.issue('scan')['metadata'])
        self.assertEqual(self.advance(native)['action'], 'yield_to_finalizer')
        worker = {**SCOPE, 'current_issue': 'last', 'current_run': 'last-run'}
        self.advance(native, worker)
        self.assertEqual(native.starts, ['scan'])

    def test_expired_or_incomplete_conclusions_are_reinvestigated(self):
        for row in (candidate('bug', 'confirmed'), suspended()):
            row['metadata']['dailyInvestigation']['investigationRecord']['evidenceRevision'] = 'old'
            native = FakeNative(snapshot([row]))
            with self.subTest(issue=row['id']):
                result = self.advance(native)
                self.assertEqual((result['action'], native.starts), ('dispatch', [row['id']]))
                self.assertEqual(native.issue(row['id'])['metadata']['dailyInvestigation']['state'], 'investigating')

    def test_unknown_start_is_recovered_from_real_run_and_not_repeated(self):
        native = FakeNative(snapshot([candidate('first')]))
        native.failure = 'after'
        result = self.advance(native)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(result['receipts'][0]['runId'], 'new-first')
        self.advance(native)
        self.assertEqual(native.starts, ['first'])

    def test_unconfirmed_start_keeps_intent_and_never_repeats_write(self):
        native = FakeNative(snapshot([candidate('first')]))
        native.failure = 'before'
        self.assertEqual(self.advance(native)['action'], 'attention')
        self.assertEqual(self.advance(native)['action'], 'attention')
        self.assertEqual(native.starts, ['first'])
        self.assertNotIn('investigationRecord', native.issue('first')['metadata']['dailyInvestigation'])

    def test_failed_run_is_not_a_success_receipt_or_a_new_automatic_start(self):
        native = FakeNative(snapshot([candidate('first')]))
        self.advance(native)
        native.data['runs'][-1]['status'] = 'failed'
        result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('attention', ['first']))

    def test_legacy_round_never_changes_concurrency_or_starts_a_run(self):
        native = FakeNative(snapshot([candidate()]))
        del native.data['issues'][0]['metadata']['dailyExecution']
        with self.assertRaises(dispatch.Error):
            self.advance(native)
        self.assertEqual((native.starts, native.writes), ([], []))

    def test_metadata_mismatch_stops_launch_and_keeps_other_slot_eligible(self):
        native = FakeNative(snapshot([candidate('first')]))
        original = native.metadata
        def save(key, name, value):
            if key != 'scan':
                raise dispatch.Error('写后回读不一致')
            original(key, name, value)
        with patch.object(native, 'metadata', side_effect=save):
            result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('attention', []))


def lane_snapshot(keys, other_round=False):
    scan = issue('scan', dailyScan={'coverageComplete': True}, dailyExecution={
        'schemaVersion': 1, 'policy': dispatch.LANE_POLICY, 'laneCount': 2,
        'repairConcurrency': 1, 'schedulerRevision': REVISION, 'runtimeId': 'runtime'})
    scan.update(assignee_id='agent', status='in_progress')
    lanes = lane_freeze(scan, keys)
    issues = [scan, *lanes]
    if other_round:
        # 前一轮仍在修复：不再挡住本轮调查。
        old = issue('old', dailyRound={'state': 'repairing'}, dailyBatch={'state': 'running', 'bugIds': ['x']})
        issues.append(old)
    for row in issues:
        row['workspace_id'] = 'workspace'
        row['assignee_id'] = 'agent'
    return {'agent': {'id': 'agent', 'workspace_id': 'workspace', 'runtime_id': 'runtime',
                      'max_concurrent_tasks': 2}, 'issues': issues, 'runs': [run('scan')]}


class LaneDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def advance(self, native, scope=SCOPE):
        with dispatch.locked(self.root / 'dispatch.lock'):
            return dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), scope)

    def lane(self, native, number):
        return next(row for row in native.data['issues'] if row['id'] == f'lane-{number}')

    def finish(self, native, number, key, state='excluded'):
        """模拟工位运行：执行中的工位写好结论评论后，由同一运行交接。"""
        conclude(self.lane(native, number), key, state)
        current = next(item for item in native.data['runs'] if item['issue_id'] == f'lane-{number}'
                       and item['status'] in {'queued', 'running'})
        current['status'] = 'running'
        return {**SCOPE, 'current_issue': f'lane-{number}', 'current_run': current['id']}

    def test_scan_claims_one_candidate_per_lane_even_when_another_round_is_repairing(self):
        native = FakeNative(lane_snapshot(['a', 'b', 'c'], other_round=True))
        result = self.advance(native)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts, ['lane-1', 'lane-2'])
        self.assertEqual([self.lane(native, n)['metadata']['dailyLane']['currentCandidateId'] for n in (1, 2)],
                         ['intercom:a', 'intercom:b'])
        self.assertEqual(native.limits, [])

    def test_lane_reads_its_claim_and_cannot_hand_off_before_a_conclusion(self):
        native = FakeNative(lane_snapshot(['a', 'b', 'c']))
        self.advance(native)
        lane_run = next(item for item in native.data['runs'] if item['issue_id'] == 'lane-1')
        lane_run['status'] = 'running'
        scope = {**SCOPE, 'current_issue': 'lane-1', 'current_run': lane_run['id']}
        result = dispatch.plan(native.snapshot(), **scope)
        self.assertEqual((result['action'], result['candidateId']), ('investigate_candidate', 'intercom:a'))
        with self.assertRaises(dispatch.Error):
            self.advance(native, scope)

    def test_concluded_lane_claims_next_and_queues_only_its_own_successor(self):
        native = FakeNative(lane_snapshot(['a', 'b', 'c']))
        self.advance(native)
        native.data['runs'][-1]['status'] = 'running'
        result = self.advance(native, self.finish(native, 1, 'a'))
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts, ['lane-1', 'lane-2', 'lane-1'])
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')
        self.assertEqual(self.lane(native, 2)['metadata']['dailyLane']['currentCandidateId'], 'intercom:b')
        self.assertEqual(result['receipts'][0]['status'], 'queued')

    def test_last_conclusions_hand_back_to_one_finalizer(self):
        native = FakeNative(lane_snapshot(['a', 'b']))
        self.advance(native)
        native.data['runs'][-1]['status'] = 'running'
        first = self.advance(native, self.finish(native, 1, 'a'))
        self.assertEqual((first['action'], first['released']), ('dispatch', ['lane-1']))
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['state'], 'done')
        # 仍有未完成候选时原扫描只核对派发，不重复领取。
        idle = self.advance(native)
        self.assertEqual((idle['action'], idle['claims'], idle['issueIds']), ('dispatch', [], []))
        scope = self.finish(native, 2, 'b', 'confirmed')
        last = self.advance(native, scope)
        self.assertEqual((last['action'], native.starts[-1]), ('finalize_round', 'scan'))
        # 汇总后继在工位退出前开跑：再排一个自身后继，不在两边都退出后无人汇总。
        native.data['runs'][0]['status'] = 'completed'
        successor = next(item for item in native.data['runs'] if item['id'] == last['receipts'][0]['runId'])
        successor['status'] = 'running'
        again = self.advance(native, {**SCOPE, 'current_run': successor['id']})
        self.assertEqual((again['action'], native.starts.count('scan')), ('finalize_round', 2))
        for item in native.data['runs']:
            if item['issue_id'] != 'scan' or item['id'] == successor['id']:
                item['status'] = 'completed'
        final = next(item for item in native.data['runs'] if item['id'] == again['receipts'][0]['runId'])
        final['status'] = 'running'
        done = self.advance(native, {**SCOPE, 'current_run': final['id']})
        self.assertEqual(done['action'], 'finalize_round_here')
        self.assertEqual([(self.lane(native, n)['status'], self.lane(native, n)['metadata']['dailyLane']['state'])
                          for n in (1, 2)], [('done', 'done'), ('done', 'done')])

    def test_duplicate_claims_fail_closed(self):
        data = lane_snapshot(['a', 'b'])
        for number in (1, 2):
            data['issues'][number]['metadata']['dailyLane']['currentCandidateId'] = 'intercom:a'
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)


class LaneFailureTests(LaneDispatchTests):
    def start(self, keys=('a', 'b', 'c')):
        data = lane_snapshot(list(keys))
        data['issues'][0]['metadata']['dailyExecution']['investigationFailurePolicy'] = dispatch.queue.FAILURE_POLICY
        native = FakeNative(data)
        self.advance(native)
        return native

    def failed_scope(self, native):
        latest = native.history('lane-1')[-1]
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'running' if value['id'] == latest['id'] else 'completed'
        lane = self.lane(native, 1)
        from duty_queue_test import comment
        item = candidate('a', 'investigating')['metadata']['dailyInvestigation']
        item.update(executionFailure={'kind': 'source_access_failed', 'error': 'channel_not_found'},
                    nextAction='恢复线程访问或取得安全对象 ID 后续查')
        lane['_comments'].append(comment(dispatch.queue.RECORD_MARKER, item, at='2026-10-23T09:00:00Z'))
        return {**SCOPE, 'current_issue': 'lane-1', 'current_run': latest['id']}

    def test_failure_retries_twice_then_skips_and_claims_next_without_blocking_other_lane(self):
        native = self.start()
        for attempt in range(2):
            scope = self.failed_scope(native)
            result = self.advance(native, scope)
            self.assertEqual((result['action'], native.starts.count('lane-1')), ('dispatch', attempt + 2))
            self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:a')
            # 同一次失败交接重复调用，复用同一个排队重试。
            self.advance(native, scope)
            self.assertEqual(native.starts.count('lane-1'), attempt + 2)
        scope = self.failed_scope(native)
        result = self.advance(native, scope)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts.count('lane-1'), 4)  # 三次 a，接着 c。
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')
        self.assertEqual(self.lane(native, 2)['metadata']['dailyLane']['currentCandidateId'], 'intercom:b')
        self.assertEqual(native.starts.count('lane-2'), 1)
        rows, pending = dispatch.queue.round_candidates(native.data['issues'][0], native.data['issues'], 'agent')
        skipped = rows[0]['metadata']['dailyInvestigation']
        self.assertEqual(skipped['state'], 'skipped_failure')
        self.assertEqual(len(skipped['failureSkip']['runIds']), 3)
        self.assertEqual(skipped['failureSkip']['reason'], 'channel_not_found')
        self.assertNotIn('verification', skipped)
        self.assertEqual(len(pending), 2)
        self.assertEqual(native.writes.count(('lane-1', 'failureSkip')), 1)

    def test_other_lane_recovers_proven_finished_run_and_exhausted_crash_is_skipped(self):
        native = self.start()
        for attempt in range(3):
            for value in native.data['runs']:
                if value['issue_id'] == 'lane-1':
                    value['status'] = 'failed'
            self.advance(native)
        rows, _ = dispatch.queue.round_candidates(native.data['issues'][0], native.data['issues'], 'agent')
        item = rows[0]['metadata']['dailyInvestigation']
        self.assertEqual(item['state'], 'skipped_failure')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')
        self.assertNotIn('investigationRecord', item)  # 崩溃无报告仍保留三次运行，不假造报告。

    def test_unknown_start_never_retries_or_skips(self):
        native = self.start()
        native.failure = 'before'
        scope = self.failed_scope(native)
        self.assertEqual(self.advance(native, scope)['action'], 'attention')
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'completed'
        self.assertEqual(self.advance(native)['action'], 'attention')
        self.assertEqual(native.starts.count('lane-1'), 2)
        self.assertNotIn(('lane-1', 'failureSkip'), native.writes)

    def test_retry_timeout_with_real_run_is_reused(self):
        native = self.start()
        native.failure = 'after'
        scope = self.failed_scope(native)
        self.assertEqual(self.advance(native, scope)['action'], 'dispatch')
        self.advance(native, scope)
        self.assertEqual(native.starts.count('lane-1'), 2)

    def test_unrecorded_live_failure_cannot_release_or_retry(self):
        native = self.start()
        native.data['runs'][1]['status'] = 'running'
        scope = {**SCOPE, 'current_issue': 'lane-1', 'current_run': native.data['runs'][1]['id']}
        result = self.advance(native, scope)
        self.assertEqual(result['action'], 'attention')
        self.assertEqual(native.starts.count('lane-1'), 1)

    def test_recover_requires_idle_target_and_preserves_collection_receipts(self):
        native = self.start()
        recovery = {**SCOPE, 'current_issue': 'lane-1', 'current_run': None}
        with self.assertRaises(dispatch.Error):
            dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)
        native.data['runs'][1]['status'] = 'completed'
        before = copy.deepcopy(native.data['issues'][0]['metadata']['dailyScan'])
        result = dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts.count('lane-1'), 2)
        self.assertEqual(native.data['issues'][0]['metadata']['dailyScan'], before)
        with self.assertRaises(dispatch.Error):
            dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)

    def test_manual_cancel_is_preserved_instead_of_retried(self):
        native = self.start()
        native.data['runs'][1]['status'] = 'cancelled'
        result = self.advance(native)
        self.assertEqual(result['action'], 'attention')
        self.assertEqual(native.starts.count('lane-1'), 1)

    def test_last_failed_candidate_returns_to_finalizer_after_skip(self):
        native = self.start(('a',))
        for attempt in range(3):
            scope = self.failed_scope(native)
            result = self.advance(native, scope)
        self.assertEqual(result['action'], 'finalize_round')
        self.assertEqual(native.starts.count('lane-1'), 3)
        self.assertEqual(native.starts[-1], 'scan')

    def test_skip_preread_failure_can_still_send_once_after_read_recovers(self):
        native = self.start()
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        actual = native.save_skip
        with patch.object(native, 'save_skip', side_effect=dispatch.Error('首次评论只读失败')):
            self.assertEqual(self.advance(native, scope)['action'], 'attention')
        with patch.object(native, 'save_skip', wraps=actual) as save:
            self.assertEqual(self.advance(native, scope)['action'], 'dispatch')
        self.assertTrue(save.call_args.kwargs['allow_write'])
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')

    def test_operator_recovery_after_exhaustion_queues_only_agent_settlement(self):
        native = self.start()
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'completed'
        recovery = {**scope, 'current_run': None}
        result = dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)
        self.assertEqual(native.starts.count('lane-1'), 4)  # 第四次仅留档，不调查。
        self.assertNotIn(('lane-1', 'failureSkip'), native.writes)
        current = native.data['runs'][-1]
        current['status'] = 'running'
        scope = {**scope, 'current_run': current['id']}
        self.assertEqual(dispatch.plan(native.snapshot(), **scope)['action'], 'settle_failure')
        self.assertEqual(self.advance(native, scope)['action'], 'dispatch')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')

    def test_other_lane_unknown_intent_cannot_block_released_lane_dispatch(self):
        native = self.start(('a', 'b', 'c', 'd'))
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'completed'
        other_scope = self.finish(native, 2, 'b')
        # lane2 的 c 启动未知，但 lane1 的 a 耗尽后仍应领取 d。
        original = native.rerun
        def fail_other(key):
            if key == 'lane-2':
                native.starts.append(key)
                raise dispatch.Error('启动结果未知')
            original(key)
        with patch.object(native, 'rerun', side_effect=fail_other):
            result = self.advance(native, other_scope)
        self.assertEqual(result['action'], 'attention')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:d')
        self.assertTrue(any(r['issueId'] == 'lane-1' for r in result['receipts']))
        self.assertTrue(any(r['issueId'] == 'lane-2' for r in result['errors']))

    def test_skip_comment_unknown_result_is_not_sent_again(self):
        native = self.start()
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        calls = []
        def fail(issue_id, item, *, allow_write, before_write):
            calls.append(allow_write)
            if allow_write:
                before_write()
            raise dispatch.Error('评论结果不明')
        with patch.object(native, 'save_skip', side_effect=fail):
            self.assertEqual(self.advance(native, scope)['action'], 'attention')
            self.assertEqual(self.advance(native, scope)['action'], 'attention')
        self.assertEqual(calls, [True, False])
        self.assertEqual(native.starts.count('lane-1'), 3)



class ReleaseRepairTests(unittest.TestCase):
    def test_waiting_round_is_started_only_after_the_other_batch_finishes(self):
        data = lane_snapshot(['a'])
        scan, lanes = data['issues'][0], data['issues'][1:3]
        lane_freeze(scan, ['a'], complete=True)
        conclude(lanes[0], 'a', 'confirmed')
        old = issue('old', dailyRound={'state': 'repairing'},
                    dailyBatch={'mode': 'scan', 'state': 'running', 'bugIds': ['x'], 'total': 1, 'index': 1,
                                'currentIssueId': 'x'})
        data['issues'].append(old)
        native = FakeNative(data)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        journal = dispatch.Journal(Path(temp.name) / 'release.json')
        kwargs = dict(agent_id='agent', runtime_id='runtime', project_id='project')
        self.assertEqual(dispatch.release_repair(native, journal, **kwargs)['action'], 'wait')
        old['metadata']['dailyBatch']['state'] = 'completed'
        result = dispatch.release_repair(native, journal, **kwargs)
        self.assertEqual((result['action'], native.starts), ('released', ['scan']))


class NativeTests(unittest.TestCase):
    def test_skip_comment_preserves_content_and_requires_agent_authored_readback(self):
        from duty_queue_test import comment
        native = dispatch.Native(['multica'], 'project', 'agent')
        item = {'roundId': 'scan', 'candidateId': 'intercom:a', 'state': 'skipped_failure'}
        saved = comment(dispatch.queue.RECORD_MARKER, item)
        with patch.object(native, 'call', side_effect=[[], {}, [saved]]) as call:
            native.save_skip('lane-1', item, allow_write=True, before_write=lambda: None)
        args, content = call.call_args_list[1].args
        self.assertIn('--content-stdin', args)
        self.assertIn('```json\n', content)
        member_comment = comment(dispatch.queue.RECORD_MARKER, item, author='member')
        with patch.object(native, 'call', return_value=[member_comment]) as call:
            with self.assertRaises(dispatch.Error):
                native.save_skip('lane-1', item, allow_write=False, before_write=lambda: None)
        self.assertTrue(all('add' not in entry.args[0] for entry in call.call_args_list))

    def test_all_project_pages_are_required_before_agent_run_snapshot(self):
        native = dispatch.Native(['multica'], 'project', 'agent')
        with patch.object(native, 'call', side_effect=[
            {'id': 'project'}, {'id': 'agent'},
            {'issues': [{'id': 'one'}], 'offset': 0, 'total': 2, 'has_more': True},
            {'issues': [{'id': 'two'}], 'offset': 1, 'total': 2, 'has_more': False}, []]) as call:
            result = native.snapshot()
        self.assertEqual([row['id'] for row in result['issues']], ['one', 'two'])
        self.assertEqual(call.call_args.args[0], ['agent', 'tasks', 'agent'])

    def test_partial_pagination_and_unconfirmed_concurrency_fail_closed(self):
        native = dispatch.Native(['multica'], 'project', 'agent')
        with patch.object(native, 'call', side_effect=[{'id': 'project'}, {'id': 'agent'},
                          {'issues': [], 'offset': 0, 'total': 1, 'has_more': False}]):
            with self.assertRaises(dispatch.Error):
                native.snapshot()


if __name__ == '__main__':
    unittest.main()
