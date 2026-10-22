"""两个调查同时交接、真实启动回读、单一汇总与旧轮隔离的定向回归。"""
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from duty_queue_test import candidate, freeze, issue, record, suspended


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
        'schemaVersion': 1, 'policy': dispatch.POLICY, 'investigationConcurrency': 2,
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

    def concurrency(self, limit):
        self.limits.append(limit)
        self.data['agent']['max_concurrent_tasks'] = limit

    def assign_without_start(self, key):
        next(row for row in self.data['issues'] if row['id'] == key)['assignee_id'] = 'agent'

    def rerun(self, key):
        self.starts.append(key)
        if self.failure != 'before':
            self.data['runs'].append(run(key, 'new-' + key, 'queued'))
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
                self.assertEqual((result['action'], result['concurrency']), ('finalize_round_here', 1))
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

    def test_wrong_scope_transfer_run_or_foreign_work_blocks_global_concurrency(self):
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
        changed['runs'].append(run('unrelated'))
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
        data['agent']['max_concurrent_tasks'] = 2
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

    def test_last_two_workers_lower_concurrency_and_enqueue_one_finalizer(self):
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
        self.assertEqual(native.data['agent']['max_concurrent_tasks'], 1)
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
        self.assertEqual(result['receipts'][0]['runId'], 'new-scan')
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
        self.assertEqual((native.starts, native.limits, native.writes), ([], [], []))

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


class NativeTests(unittest.TestCase):
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
        with patch.object(native, 'call', side_effect=[{'max_concurrent_tasks': 2}, {},
                                                      {'max_concurrent_tasks': 2}]):
            with self.assertRaises(dispatch.Error):
                native.concurrency(1)


if __name__ == '__main__':
    unittest.main()
