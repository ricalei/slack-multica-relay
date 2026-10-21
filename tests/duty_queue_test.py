"""值守调查与修复的接续、挂起、恢复和重复调度保护。"""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('duty_queue', Path(__file__).parents[1] / 'scripts/duty-queue.py')
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
NOW = dt.datetime(2026, 10, 21, 4, tzinfo=dt.timezone.utc)


def issue(key, **metadata):
    return {'id': key, 'project_id': 'project', 'status': 'backlog', 'assignee_id': None,
            'created_at': '2026-10-21T00:00:00Z', 'metadata': metadata}


def candidate(key='investigation', state='ready'):
    return {**issue(key, dailyInvestigation={
        'schemaVersion': 1, 'mode': 'investigate', 'candidateId': 'intercom:' + key,
        'sourceScanId': 'scan', 'feedbackDate': '2026-10-20', 'state': state, 'evidenceRevision': 'v1',
    }), 'parent_issue_id': 'scan'}


def suspended(key='suspended'):
    row = candidate(key, 'suspended_external')
    row['metadata']['dailyInvestigation']['blocker'] = {
        'kind': 'external_evidence', 'missingEvidence': '原设备发送回执', 'resumeCondition': '原设备回执可读取',
        'attempts': {k: 'attachment#' + k for k in ('source', 'implementation', 'data', 'verification')},
        'evidenceRevision': 'v1', 'checkedAt': '2026-10-21T00:00:00Z', 'nextCheckAt': '2026-10-22T00:00:00Z',
    }
    return row


def confirmed():
    row = candidate(state='confirmed')
    row['metadata']['dailyInvestigation']['verification'] = {
        'fixable': True, 'productDecisionRequired': False,
        'expectedBehaviorRef': 'attachment#expected', 'observedDeviationRef': 'attachment#observed',
        'dedupeRef': 'attachment#dedupe',
    }
    return row


def repair(state='running'):
    batch = issue('batch', dailyBatch={
        'mode': 'repair', 'state': state, 'investigationIssueId': 'investigation', 'sourceScanId': 'scan',
        'bugIds': ['bug'], 'index': 1, 'total': 1, 'currentIssueId': 'bug',
    })
    bug = {**issue('bug', dailyBug={'mode': 'bug', 'batchId': 'batch', 'index': 1, 'total': 1,
                                  'state': 'running'}), 'parent_issue_id': 'batch'}
    return batch, bug


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.scan = issue('scan', dailyScan={'targetDate': '2026-10-20', 'coverageComplete': True})

    def plan(self, *issues, runs=None, **kwargs):
        return queue.next_action([self.scan, *issues], runs or [], project_id='project', agent_id='agent',
                                 current_issue_id='current', now=NOW, **kwargs)

    def test_ready_items_follow_feedback_date_and_preserve_snapshot(self):
        older, newer = candidate('older'), candidate('newer')
        older['metadata']['dailyInvestigation']['feedbackDate'] = '2026-10-19'
        before = copy.deepcopy([older, newer])
        result = self.plan(newer, older)
        self.assertEqual((result['action'], result['issueId']), ('investigate', 'older'))
        self.assertEqual([older, newer], before)

    def test_legacy_pending_attachment_cannot_disappear_as_an_idle_queue(self):
        scan = self.scan['metadata']['dailyScan']
        scan.update(currentPending=54, cumulativePending=494, pendingCandidatesAttachmentId='attachment')
        result = self.plan()
        self.assertEqual((result['action'], result['attachmentId']), ('register_investigations', 'attachment'))
        self.scan['metadata']['dailyInvestigationImport'] = {
            'schemaVersion': 1, 'completed': True, 'pendingAttachmentId': 'attachment', 'sourceSha256': 'a' * 64,
            'sourceRecordCount': 494, 'registeredCandidateCount': 1,
        }
        self.assertEqual(self.plan()['action'], 'register_investigations')
        self.assertEqual(self.plan(candidate())['action'], 'investigate')
        scan['pendingCandidatesAttachmentId'] = 'new-attachment'
        self.assertEqual(self.plan(candidate())['action'], 'register_investigations')

    def test_pause_and_cancel_are_not_interpreted_as_a_crashed_run(self):
        row = candidate(state='paused')
        self.assertEqual(self.plan(row)['action'], 'idle')
        batch, bug = repair('paused')
        self.assertEqual(self.plan(batch, bug)['action'], 'attention')

    def test_confirmation_requires_behavior_dedupe_and_complete_source_coverage(self):
        row = confirmed()
        self.assertEqual(self.plan(candidate('older'), row)['action'], 'prepare_repair')
        proof = row['metadata']['dailyInvestigation']['verification']
        for key in ('expectedBehaviorRef', 'observedDeviationRef', 'dedupeRef'):
            with self.subTest(key=key):
                changed = copy.deepcopy(row)
                del changed['metadata']['dailyInvestigation']['verification'][key]
                self.assertEqual(self.plan(changed)['action'], 'investigate')
        proof['productDecisionRequired'] = True
        self.assertEqual(self.plan(row)['action'], 'investigate')
        proof['productDecisionRequired'] = False
        self.scan['metadata']['dailyScan']['coverageComplete'] = False
        self.assertEqual(self.plan(row)['action'], 'attention')

    def test_unfinished_fixed_repair_queue_precedes_new_confirmed_or_ready_items(self):
        batch, bug = repair()
        result = self.plan(candidate('new'), confirmed(), batch, bug)
        self.assertEqual((result['action'], result['issueId']), ('resume_repair', 'bug'))
        bug['assignee_id'] = 'other-agent'
        with self.assertRaises(queue.QueueError):
            self.plan(batch, bug)
        bug['assignee_id'] = None
        batch['metadata']['dailyBatch']['currentIssueId'] = 'wrong'
        with self.assertRaises(queue.QueueError):
            self.plan(batch, bug)

    def test_partial_batch_registration_is_reconciled_instead_of_created_again(self):
        batch, _ = repair()
        batch['metadata']['dailyBatch'].update(bugIds=[], index=0, total=0, currentIssueId=None)
        self.assertEqual(self.plan(confirmed(), batch)['action'], 'reconcile_repair')

    def test_last_verified_delivery_closes_investigation_then_returns_to_next_item(self):
        row = candidate(state='waiting_repair')
        row['metadata']['dailyInvestigation']['repairBatchId'] = 'batch'
        batch, bug = repair('completed')
        batch['metadata']['dailyBatch'].update(
            outcome='engineering_delivery_completed', currentIssueId=None, deliveredBugIds=['bug'],
            completedDeliveries=[{'issueId': 'bug', 'deliveryVersion': 2, 'pr': 'https://github.com/org/repo/pull/1',
                                  'prHead': 'a' * 40,
                                  'report': {'sha256': 'b' * 64, 'readbackHashVerified': True,
                                             'multicaAttachmentId': 'report'},
                                  'delivery': {'permalink': 'https://slack.example/message',
                                               'readbackVerified': True}}])
        bug['metadata']['dailyBug'].update(state='delivered', completeDelivery=True, deliveryVersion=2)
        receipt = batch['metadata']['dailyBatch']['completedDeliveries'][0]
        for key, flag in (('report', 'readbackHashVerified'), ('delivery', 'readbackVerified')):
            receipt[key][flag] = False
            with self.subTest(key=key), self.assertRaises(queue.QueueError):
                self.plan(row, batch, bug, candidate('next'))
            receipt[key][flag] = True
        result = self.plan(row, batch, bug, candidate('next'))
        self.assertEqual((result['action'], result['issueId']), ('complete_investigation', 'investigation'))
        row['metadata']['dailyInvestigation']['state'] = 'delivered'
        result = self.plan(row, batch, bug, candidate('next'))
        self.assertEqual((result['action'], result['issueId']), ('investigate', 'next'))
        # 旧交付重跑只产生相同的下一步计划；计划自身不会再次派发。
        self.assertEqual(self.plan(row, batch, bug, candidate('next')), result)

    def test_completed_batch_without_matching_delivery_cannot_advance(self):
        row = candidate(state='waiting_repair')
        batch, bug = repair('completed')
        batch['metadata']['dailyBatch'].update(outcome='engineering_delivery_completed', deliveredBugIds=['bug'])
        with self.assertRaises(queue.QueueError):
            self.plan(row, batch, bug)

    def test_active_run_prevents_duplicate_dispatch_but_current_run_can_hand_off(self):
        run = {'issue_id': 'next', 'agent_id': 'agent', 'status': 'queued'}
        self.assertEqual(self.plan(candidate('next'), runs=[run])['action'], 'wait')
        run['issue_id'] = 'current'
        self.assertEqual(self.plan(candidate('next'), runs=[run])['action'], 'investigate')

    def test_only_proven_external_block_is_suspended_and_other_items_continue(self):
        blocked = suspended()
        self.assertEqual(self.plan(blocked)['action'], 'idle')
        self.assertEqual(self.plan(blocked, candidate('next'))['issueId'], 'next')
        for key in ('source', 'implementation', 'data', 'verification'):
            with self.subTest(key=key):
                changed = copy.deepcopy(blocked)
                del changed['metadata']['dailyInvestigation']['blocker']['attempts'][key]
                self.assertEqual(self.plan(changed)['action'], 'investigate')
        for invalid in ('legacy missing evidence', {'kind': 'runtime_failure'}, {'kind': 'external_evidence'}):
            blocked['metadata']['dailyInvestigation']['blocker'] = invalid
            self.assertEqual(self.plan(blocked)['action'], 'investigate')

    def test_evidence_change_resumes_but_elapsed_time_only_checks_condition(self):
        row = suspended()
        item = row['metadata']['dailyInvestigation']
        item['evidenceRevision'] = 'v2'
        self.assertEqual(self.plan(row)['action'], 'investigate')
        item['evidenceRevision'] = 'v1'
        item['blocker'].update(checkedAt='2026-10-19T00:00:00Z', nextCheckAt='2026-10-20T00:00:00Z')
        self.assertEqual(self.plan(row)['action'], 'check_external_evidence')
        self.assertEqual(self.plan(row, candidate('ready'))['action'], 'check_external_evidence')
        self.assertEqual(self.plan(row, confirmed())['action'], 'prepare_repair')
        item['blocker'].update(checkedAt=NOW.isoformat(), nextCheckAt=(NOW + dt.timedelta(days=1)).isoformat())
        self.assertEqual(self.plan(row)['action'], 'idle')

    def test_cancelled_items_and_skipped_repairs_are_not_reopened(self):
        row = candidate()
        row['status'] = 'cancelled'
        self.assertEqual(self.plan(row)['action'], 'idle')
        row['status'] = 'backlog'
        batch, bug = repair('finished_with_skips')
        self.assertEqual(self.plan(row, batch, bug, candidate('next'))['issueId'], 'next')

    def test_duplicate_identities_foreign_scope_and_bad_metadata_fail_closed(self):
        row = candidate()
        duplicate = copy.deepcopy(row)
        duplicate['id'] = 'duplicate'
        with self.assertRaises(queue.QueueError):
            self.plan(row, duplicate)
        for change in ({'project_id': 'other'}, {'metadata': {'dailyInvestigation': '{broken'}}):
            with self.subTest(change=change), self.assertRaises(queue.QueueError):
                self.plan({**row, **change})
        row['assignee_id'] = 'other-agent'
        with self.assertRaises(queue.QueueError):
            self.plan(row)


class SnapshotTests(unittest.TestCase):
    def test_all_pages_are_loaded_and_only_native_read_commands_are_used(self):
        first = candidate('first')
        first.update(status='in_progress')
        with patch.object(queue, 'query', side_effect=[
            {'id': 'project'}, {'id': 'agent', 'max_concurrent_tasks': 1},
            {'issues': [first], 'has_more': True, 'total': 2, 'offset': 0},
            {'issues': [candidate('second')], 'has_more': False, 'total': 2, 'offset': 1}, [],
        ]) as query:
            issues, runs = queue.snapshot(['multica'], 'project', 'agent', 'current')
        self.assertEqual([row['id'] for row in issues], ['first', 'second'])
        self.assertEqual(runs, [])
        self.assertIn(['issue', 'list', '--project', 'project', '--limit', '100', '--offset', '1',
                       '--sort', 'created_at', '--direction', 'asc'],
                      [call.args[1] for call in query.call_args_list])
        self.assertEqual(query.call_args.args[1], ['issue', 'runs', 'first', '--active'])

    def test_incomplete_pagination_and_wrong_concurrency_cannot_be_reported_as_idle(self):
        for responses in ([{'id': 'project'}, {'id': 'agent', 'max_concurrent_tasks': 2}],
                          [{'id': 'project'}, {'id': 'agent', 'max_concurrent_tasks': 1},
                           {'issues': [], 'has_more': True, 'total': 1, 'offset': 0}]):
            with self.subTest(responses=responses), patch.object(queue, 'query', side_effect=responses):
                with self.assertRaises(queue.QueueError):
                    queue.snapshot(['multica'], 'project', 'agent', 'current')

    def test_json_string_metadata_has_same_meaning_as_object(self):
        row = candidate()
        row['metadata']['dailyInvestigation'] = json.dumps(row['metadata']['dailyInvestigation'])
        self.assertEqual(queue.meta(row, 'dailyInvestigation')['candidateId'], 'intercom:investigation')


if __name__ == '__main__':
    unittest.main()
