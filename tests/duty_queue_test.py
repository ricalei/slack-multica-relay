"""本轮全部调查留档后才修复，以及历史停止、交付和重复调度保护。"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('duty_queue', Path(__file__).parents[1] / 'scripts/duty-queue.py')
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


def issue(key, **metadata):
    return {'id': key, 'project_id': 'project', 'status': 'backlog', 'assignee_id': None,
            'created_at': '2026-10-22T00:00:00Z', 'metadata': metadata}


def record(name):
    return {'attachmentId': name, 'sha256': hashlib.sha256(name.encode()).hexdigest(),
            'readbackHashVerified': True}


def candidate(key='investigation', state='ready'):
    data = {'schemaVersion': 1, 'mode': 'investigate', 'candidateId': 'intercom:' + key,
            'sourceScanId': 'scan', 'roundId': 'scan', 'feedbackDate': '2026-10-21', 'state': state,
            'evidenceRevision': 'v1'}
    if state not in {'ready', 'investigating'}:
        data.update(investigationRecord={**record(key), 'evidenceRevision': 'v1'}, conclusionRef=key + '#conclusion')
    if state in {'confirmed', 'waiting_repair', 'delivered'}:
        data['verification'] = {'fixable': True, 'productDecisionRequired': False,
                                'expectedBehaviorRef': key + '#expected', 'observedDeviationRef': key + '#observed',
                                'dedupeRef': key + '#dedupe'}
    return {**issue(key, dailyInvestigation=data), 'parent_issue_id': 'scan'}


def suspended():
    row = candidate('external', 'suspended_external')
    row['metadata']['dailyInvestigation']['blocker'] = {
        'kind': 'external_evidence', 'missingEvidence': '原设备发送回执', 'resumeCondition': '原设备回执可读取',
        'attempts': {key: 'attachment#' + key for key in ('source', 'implementation', 'data', 'verification')},
        'evidenceRevision': 'v1', 'checkedAt': '2026-10-22T00:00:00Z', 'nextCheckAt': '2026-10-23T00:00:00Z'}
    return row


def freeze(scan, rows, complete=False):
    ids = sorted(row['metadata']['dailyInvestigation']['candidateId'] for row in rows)
    scan['metadata']['dailyRound'] = {
        'schemaVersion': 1, 'policy': 'investigate_all_then_repair', 'state': 'investigating',
        'registrationComplete': True, 'candidateCount': len(rows),
        'candidateSetSha256': hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
        'candidateManifest': record('manifest'), 'investigationComplete': complete}
    if complete:
        scan['metadata']['dailyRound'].update(state='ready_repair', investigationIndex=record('index'))


def add_repair(scan, rows, state='running'):
    bugs = []
    for index, row in enumerate(rows, 1):
        key = 'bug-' + row['id']
        bugs.append({**issue(key, dailyBug={'mode': 'bug', 'batchId': scan['id'], 'index': index,
                                          'total': len(rows), 'state': 'running',
                                          'candidateId': row['metadata']['dailyInvestigation']['candidateId'],
                                          'investigationIssueId': row['id']}), 'parent_issue_id': scan['id']})
    scan['metadata']['dailyBatch'] = {'mode': 'scan', 'state': state, 'bugIds': [bug['id'] for bug in bugs],
                                     'index': 1, 'total': len(bugs), 'currentIssueId': bugs[0]['id']}
    return bugs


def deliver(scan, bug):
    bug['metadata']['dailyBug'].update(state='delivered', completeDelivery=True, deliveryVersion=2)
    scan['metadata']['dailyBatch'].update(
        state='completed', outcome='engineering_delivery_completed', currentIssueId=None,
        deliveredBugIds=[bug['id']], completedDeliveries=[{
            'issueId': bug['id'], 'deliveryVersion': 2, 'pr': 'https://github.com/org/repo/pull/1',
            'prHead': 'a' * 40, 'report': {**record('html'), 'multicaAttachmentId': 'html'},
            'delivery': {'permalink': 'https://slack.example/message', 'readbackVerified': True}}])


def block(scan, bug, notified=True):
    bug['metadata']['dailyBug'].update(state='blocked', blocker={
        'kind': 'permission_denied', 'reason': '测试部署需合入测试分支，当前身份无合并权限',
        'resumeCondition': '有权限的人合入测试 PR 后在任务评论继续'})
    scan['metadata']['dailyBatch'].setdefault('blockNotices', []).append(
        {'issueId': bug['id'], 'permalink': 'https://slack.example/blocked', 'readbackVerified': notified})


def receipt(bug):
    bug['metadata']['dailyBug'].update(state='delivered', completeDelivery=True, deliveryVersion=1)
    return {'issueId': bug['id'], 'deliveryVersion': 1, 'pr': 'https://github.com/org/repo/pull/2',
            'prHead': 'b' * 40, 'report': {**record('html-' + bug['id']), 'multicaAttachmentId': 'html'},
            'delivery': {'permalink': 'https://slack.example/' + bug['id'], 'readbackVerified': True}}


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.scan = issue('scan', dailyScan={'targetDate': '2026-10-21', 'coverageComplete': True})

    def plan(self, *rows, runs=None):
        return queue.next_action([self.scan, *rows], runs or [], project_id='project', agent_id='agent',
                                 current_issue_id='current')

    def test_collection_and_registration_are_separate_before_investigation(self):
        freeze(self.scan, [])
        info = self.scan['metadata']['dailyRound']
        info.update(state='collecting', registrationComplete=False)
        self.assertEqual(self.plan()['action'], 'collect_sources')
        info['state'] = 'registering'
        self.assertEqual(self.plan()['action'], 'register_round')

    def test_confirmed_bug_waits_for_remaining_investigations(self):
        ready, confirmed = candidate('remaining'), candidate('confirmed', 'confirmed')
        freeze(self.scan, [ready, confirmed])
        before = copy.deepcopy([self.scan, ready, confirmed])
        result = self.plan(confirmed, ready)
        self.assertEqual((result['action'], result['issueId'], result['remaining']), ('investigate', 'remaining', 1))
        self.assertEqual([self.scan, ready, confirmed], before)

    def test_ordinary_investigations_are_processed_in_stable_feedback_order(self):
        older, newer = candidate('older'), candidate('newer')
        older['metadata']['dailyInvestigation']['feedbackDate'] = '2026-10-20'
        freeze(self.scan, [older, newer])
        self.assertEqual(self.plan(newer, older)['issueId'], 'older')

    def test_incomplete_count_changed_identity_and_added_candidate_fail_closed(self):
        row = candidate()
        freeze(self.scan, [row])
        with self.assertRaises(queue.QueueError):
            self.plan()
        changed = copy.deepcopy(row)
        changed['metadata']['dailyInvestigation']['candidateId'] = 'other'
        with self.assertRaises(queue.QueueError):
            self.plan(changed)
        with self.assertRaises(queue.QueueError):
            self.plan(row, candidate('late'))

    def test_unread_manifest_cannot_be_used_as_a_frozen_round(self):
        row = candidate()
        freeze(self.scan, [row])
        self.scan['metadata']['dailyRound']['candidateManifest']['readbackHashVerified'] = False
        with self.assertRaises(queue.QueueError):
            self.plan(row)

    def test_completed_flag_cannot_hide_an_uninvestigated_item(self):
        row = candidate()
        freeze(self.scan, [row], complete=True)
        with self.assertRaises(queue.QueueError):
            self.plan(row)

    def test_confirmed_items_require_behavior_dedupe_and_a_readback_record(self):
        row = candidate(state='confirmed')
        freeze(self.scan, [row])
        for key in ('expectedBehaviorRef', 'observedDeviationRef', 'dedupeRef'):
            changed = copy.deepcopy(row)
            del changed['metadata']['dailyInvestigation']['verification'][key]
            with self.subTest(key=key):
                self.assertEqual(self.plan(changed)['action'], 'investigate')
        row['metadata']['dailyInvestigation']['investigationRecord']['readbackHashVerified'] = False
        self.assertEqual(self.plan(row)['action'], 'investigate')

    def test_report_for_an_old_source_revision_cannot_close_current_investigation(self):
        row = candidate(state='confirmed')
        freeze(self.scan, [row])
        row['metadata']['dailyInvestigation']['evidenceRevision'] = 'new-source-content'
        self.assertEqual(self.plan(row)['action'], 'investigate')

    def test_non_bug_and_known_ticket_conclusions_require_saved_basis(self):
        for state in ('excluded', 'duplicate', 'cs_followed'):
            row = candidate(state=state)
            freeze(self.scan, [row])
            row['status'] = 'done'
            with self.subTest(state=state):
                self.assertEqual(self.plan(row)['action'], 'finalize_round')
                del row['metadata']['dailyInvestigation']['conclusionRef']
                with self.assertRaises(queue.QueueError):
                    self.plan(row)

    def test_only_investigated_external_evidence_counts_as_a_finished_investigation(self):
        row = suspended()
        freeze(self.scan, [row])
        self.assertEqual(self.plan(row)['action'], 'finalize_round')
        for key in ('source', 'implementation', 'data', 'verification'):
            changed = copy.deepcopy(row)
            del changed['metadata']['dailyInvestigation']['blocker']['attempts'][key]
            with self.subTest(key=key):
                self.assertEqual(self.plan(changed)['action'], 'investigate')
        row['metadata']['dailyInvestigation']['blocker'] = {'kind': 'runtime_failure'}
        self.assertEqual(self.plan(row)['action'], 'investigate')

    def test_new_source_material_invalidates_the_previous_external_block(self):
        row = suspended()
        freeze(self.scan, [row])
        item = row['metadata']['dailyInvestigation']
        item['evidenceRevision'] = 'v2'
        item['investigationRecord']['evidenceRevision'] = 'v2'
        self.assertEqual(self.plan(row)['action'], 'investigate')

    def test_all_conclusions_must_be_summarized_before_any_repair(self):
        confirmed, excluded = candidate('confirmed', 'confirmed'), candidate('excluded', 'excluded')
        freeze(self.scan, [confirmed, excluded])
        self.assertEqual(self.plan(confirmed, excluded)['action'], 'finalize_round')
        freeze(self.scan, [confirmed, excluded], complete=True)
        result = self.plan(confirmed, excluded)
        self.assertEqual((result['action'], result['investigationIds']), ('prepare_round_repair', ['confirmed']))
        self.assertEqual(self.plan(confirmed, excluded), result)

    def test_zero_candidates_and_all_non_fixable_conclusions_end_without_repair(self):
        freeze(self.scan, [], complete=True)
        self.assertEqual(self.plan()['action'], 'complete_round')
        rows = [candidate('excluded', 'excluded'), suspended()]
        freeze(self.scan, rows, complete=True)
        self.assertEqual(self.plan(*rows)['action'], 'complete_round')

    def test_missing_source_coverage_never_becomes_a_successful_zero_or_new_repair(self):
        self.scan['metadata']['dailyScan']['coverageComplete'] = False
        freeze(self.scan, [], complete=True)
        self.assertEqual(self.plan()['action'], 'attention')
        row = candidate(state='confirmed')
        freeze(self.scan, [row], complete=True)
        self.assertEqual(self.plan(row)['action'], 'attention')

    def test_an_early_repair_cannot_bypass_the_round_investigation_gate(self):
        rows = [candidate('confirmed', 'confirmed'), candidate('unread')]
        freeze(self.scan, rows)
        bugs = add_repair(self.scan, rows[:1])
        with self.assertRaises(queue.QueueError):
            self.plan(*rows, *bugs)

    def test_active_repair_requires_exactly_the_confirmed_roster(self):
        rows = [candidate('confirmed', 'confirmed'), candidate('excluded', 'excluded')]
        freeze(self.scan, rows, complete=True)
        bugs = add_repair(self.scan, rows[:1])
        self.assertEqual(self.plan(*rows, *bugs)['action'], 'resume_repair')
        bugs[0]['metadata']['dailyBug']['investigationIssueId'] = 'excluded'
        with self.assertRaises(queue.QueueError):
            self.plan(*rows, *bugs)

    def test_repair_handoff_cannot_substitute_a_different_source_candidate(self):
        row = candidate(state='confirmed')
        freeze(self.scan, [row], complete=True)
        bug = add_repair(self.scan, [row])[0]
        bug['metadata']['dailyBug']['candidateId'] = 'another-feedback'
        with self.assertRaises(queue.QueueError):
            self.plan(row, bug)

    def test_partial_repair_registration_reuses_the_same_scan_batch(self):
        row = candidate(state='confirmed')
        freeze(self.scan, [row], complete=True)
        self.scan['metadata']['dailyBatch'] = {'mode': 'scan', 'state': 'preparing', 'bugIds': [],
                                             'index': 0, 'total': 0, 'currentIssueId': None}
        self.scan['metadata']['dailyRound']['state'] = 'preparing_repair'
        self.assertEqual(self.plan(row)['action'], 'prepare_round_repair')

    def test_real_delivery_versions_and_receipts_are_required_before_round_completion(self):
        row = candidate(state='waiting_repair')
        freeze(self.scan, [row], complete=True)
        bug = add_repair(self.scan, [row])[0]
        deliver(self.scan, bug)
        self.assertEqual(self.plan(row, bug)['action'], 'complete_round')
        receipt = self.scan['metadata']['dailyBatch']['completedDeliveries'][0]
        for key, flag in (('report', 'readbackHashVerified'), ('delivery', 'readbackVerified')):
            receipt[key][flag] = False
            with self.subTest(key=key), self.assertRaises(queue.QueueError):
                self.plan(row, bug)
            receipt[key][flag] = True
        receipt['deliveryVersion'] = 1
        with self.assertRaises(queue.QueueError):
            self.plan(row, bug)

    def test_notified_block_moves_to_next_item_without_counting_as_delivery(self):
        rows = [candidate('first', 'waiting_repair'), candidate('second', 'waiting_repair')]
        freeze(self.scan, rows, complete=True)
        bugs = add_repair(self.scan, rows)
        block(self.scan, bugs[0], notified=False)
        self.assertEqual(self.plan(*rows, *bugs)['action'], 'resume_repair')
        self.scan['metadata']['dailyBatch']['blockNotices'][0]['readbackVerified'] = True
        result = self.plan(*rows, *bugs)
        self.assertEqual((result['action'], result['issueId'], result['nextIssueId']),
                         ('advance_after_block', 'bug-first', 'bug-second'))
        self.assertFalse(queue.verified_delivery(self.scan['metadata']['dailyBatch'], bugs[0]))
        del bugs[0]['metadata']['dailyBug']['blocker']['resumeCondition']
        self.assertEqual(self.plan(*rows, *bugs)['action'], 'resume_repair')

    def test_round_closes_with_blocked_items_only_when_each_item_is_delivered_or_notified(self):
        rows = [candidate('first', 'waiting_repair'), candidate('second', 'waiting_repair')]
        freeze(self.scan, rows, complete=True)
        bugs = add_repair(self.scan, rows)
        block(self.scan, bugs[1])
        batch = self.scan['metadata']['dailyBatch']
        batch.update(state='completed', outcome='finished_with_blocked', currentIssueId=None, index=2,
                     deliveredBugIds=['bug-first'], blockedBugIds=['bug-second'],
                     completedDeliveries=[receipt(bugs[0])])
        result = self.plan(*rows, *bugs)
        self.assertEqual((result['action'], result['outcome'], result['blockedIssueIds']),
                         ('complete_round', 'finished_with_blocked', ['bug-second']))
        for change in ({'outcome': 'engineering_delivery_completed'}, {'blockedBugIds': ['bug-second', 'bug-first']},
                       {'deliveredBugIds': []}):
            changed = copy.deepcopy(self.scan)
            changed['metadata']['dailyBatch'].update(change)
            with self.subTest(change=change), self.assertRaises(queue.QueueError):
                queue.next_action([changed, *rows, *bugs], [], project_id='project', agent_id='agent',
                                  current_issue_id='current')
        batch['blockNotices'][0]['readbackVerified'] = False
        with self.assertRaises(queue.QueueError):
            self.plan(*rows, *bugs)

    def test_block_released_later_finishes_as_full_delivery_in_any_order(self):
        rows = [candidate('first', 'waiting_repair'), candidate('second', 'waiting_repair')]
        freeze(self.scan, rows, complete=True)
        bugs = add_repair(self.scan, rows)
        self.scan['metadata']['dailyBatch'].update(
            state='completed', outcome='engineering_delivery_completed', currentIssueId=None, index=2,
            deliveredBugIds=['bug-second', 'bug-first'], blockedBugIds=[],
            completedDeliveries=[receipt(bugs[1]), receipt(bugs[0])])
        result = self.plan(*rows, *bugs)
        self.assertEqual(result['action'], 'complete_round')
        self.assertNotIn('outcome', result)

    def test_human_skip_is_not_converted_to_verified_delivery(self):
        row = candidate(state='waiting_repair')
        freeze(self.scan, [row], complete=True)
        bug = add_repair(self.scan, [row], 'finished_with_skips')[0]
        result = self.plan(row, bug)
        self.assertEqual((result['action'], result['outcome']), ('complete_round', 'finished_with_skips'))
        self.assertNotIn('completeDelivery', bug['metadata']['dailyBug'])

    def test_completed_batch_with_human_skip_outcome_can_close_round(self):
        row = candidate(state='waiting_repair')
        freeze(self.scan, [row], complete=True)
        bug = add_repair(self.scan, [row], 'completed')[0]
        self.scan['metadata']['dailyBatch'].update(
            outcome='finished_with_skips', currentIssueId=None, deliveredBugIds=[], completedDeliveries=[])
        before = copy.deepcopy([self.scan, row, bug])
        result = self.plan(row, bug)
        self.assertEqual((result['action'], result['outcome']), ('complete_round', 'finished_with_skips'))
        self.assertEqual([self.scan, row, bug], before)
        self.assertEqual(row['metadata']['dailyInvestigation']['state'], 'waiting_repair')
        self.assertNotIn('completeDelivery', bug['metadata']['dailyBug'])

    def test_completed_skip_still_requires_round_evidence_and_repair_identity(self):
        row = candidate(state='waiting_repair')
        freeze(self.scan, [row], complete=True)
        bug = add_repair(self.scan, [row], 'completed')[0]
        self.scan['metadata']['dailyBatch'].update(outcome='finished_with_skips', currentIssueId=None)
        row['metadata']['dailyInvestigation']['investigationRecord']['readbackHashVerified'] = False
        with self.assertRaises(queue.QueueError):
            self.plan(row, bug)
        row['metadata']['dailyInvestigation']['investigationRecord']['readbackHashVerified'] = True
        bug['metadata']['dailyBug']['candidateId'] = 'another-feedback'
        with self.assertRaises(queue.QueueError):
            self.plan(row, bug)

    def test_legacy_attachments_and_candidates_are_not_reimported_or_restarted(self):
        self.scan['metadata']['dailyScan'].update(currentPending=494, pendingCandidatesAttachmentId='legacy')
        row = candidate()
        del row['metadata']['dailyInvestigation']['roundId']
        self.assertEqual(self.plan(row)['action'], 'idle')
        row['status'] = 'cancelled'
        row['metadata']['dailyInvestigation']['state'] = 'cancelled'
        self.assertEqual(self.plan(row)['action'], 'idle')

    def test_finished_new_round_does_not_return_to_a_global_candidate_queue(self):
        freeze(self.scan, [], complete=True)
        self.scan['metadata']['dailyRound']['state'] = 'completed'
        old = candidate('old')
        del old['metadata']['dailyInvestigation']['roundId']
        self.assertEqual(self.plan(old)['action'], 'idle')

    def test_paused_and_cancelled_current_round_members_are_not_reopened(self):
        for state in ('paused', 'cancelled'):
            row = candidate(state=state)
            freeze(self.scan, [row])
            with self.subTest(state=state), self.assertRaises(queue.QueueError):
                self.plan(row)

    def test_active_run_prevents_duplicate_dispatch(self):
        row = candidate()
        freeze(self.scan, [row])
        run = {'issue_id': row['id'], 'agent_id': 'agent', 'status': 'queued'}
        self.assertEqual(self.plan(row, runs=[run])['action'], 'wait')
        run['issue_id'] = 'current'
        self.assertEqual(self.plan(row, runs=[run])['action'], 'investigate')

    def test_wrong_parent_owner_scope_and_protocol_fail_closed(self):
        row = candidate()
        freeze(self.scan, [row])
        for change in ({'parent_issue_id': 'other'}, {'assignee_id': 'other'}, {'project_id': 'other'},
                       {'metadata': {'dailyInvestigation': '{broken'}}):
            with self.subTest(change=change), self.assertRaises(queue.QueueError):
                self.plan({**row, **change})
        self.scan['metadata']['dailyRound']['schemaVersion'] = 99
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
                       '--sort', 'created_at', '--direction', 'asc'], [call.args[1] for call in query.call_args_list])
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
