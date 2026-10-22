"""采集、调查、修复阶段快照的证据门槛，以及无最终台账时的看板展示。"""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from duty_queue_test import candidate, freeze, issue, add_repair, deliver, suspended


spec = importlib.util.spec_from_file_location('board_stages', Path(__file__).parents[1] / 'scripts/duty-board.py')
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)
progress = board.progress_module
NOW = dt.datetime(2026, 10, 22, 2, tzinfo=dt.timezone.utc)
BASE = 'https://multica.devops.moego.dev/grm/issues/'


def fixture(rows, complete=False):
    scan = issue('scan', dailyScan={'targetDate': '2026-10-21', 'coverageComplete': True,
                                  'collected': {'canny': 2, 'facebook': 1, 'intercom': 4},
                                  'counts': {'nonBug': 4, 'productDecision': 0, 'csFollowed': 1, 'suspected': 2}})
    scan.update(identifier='GRM-1', assignee_id='agent', title='每日扫描', status='in_progress')
    freeze(scan, rows, complete)
    for index, row in enumerate(rows, 2):
        row.update(identifier=f'GRM-{index}', title='反馈调查｜问题摘要')
    return scan, [scan, *rows]


def snapshot(scan, issues, runs=None):
    return progress.snapshot(scan, issues, runs if runs is not None else [], NOW, BASE)


class ProgressTests(unittest.TestCase):
    def test_partial_investigation_has_collection_and_real_current_work_without_final_ledger(self):
        scan, issues = fixture([candidate('first', 'confirmed'), candidate('second')])
        run = {'issue_id': 'second', 'agent_id': 'agent', 'status': 'running'}
        result = snapshot(scan, issues, [run])
        self.assertEqual(result['collection']['total'], 7)
        self.assertEqual((result['investigation']['completed'], result['investigation']['total'],
                          result['investigation']['confirmed']), (1, 2, 1))
        self.assertEqual(result['repair']['state'], 'not_started')
        self.assertEqual(result['current'][0]['identifier'], 'GRM-3')
        self.assertIn('剩余调查', result['nextAction'])

    def test_saved_report_and_matching_source_revision_are_required_for_completion(self):
        row = candidate('bug', 'confirmed')
        scan, issues = fixture([row])
        for change in ({'evidenceRevision': 'v2'}, {'verification': {'fixable': True}}):
            modified = copy.deepcopy(issues)
            modified[1]['metadata']['dailyInvestigation'].update(change)
            with self.subTest(change=change):
                result = snapshot(scan, modified)
                self.assertEqual(result['investigation']['completed'], 0)
                self.assertEqual(result['investigation']['confirmedIssueIds'], [])

    def test_external_evidence_is_a_recorded_conclusion_but_not_a_new_repair(self):
        row = suspended()
        scan, issues = fixture([row])
        result = snapshot(scan, issues)
        self.assertEqual((result['investigation']['completed'], result['repair']['state']), (1, 'not_needed'))
        row['metadata']['dailyInvestigation']['blocker']['attempts'].pop('data')
        self.assertEqual(snapshot(scan, issues)['investigation']['completed'], 0)

    def test_full_delivery_is_not_inferred_from_pr_or_completed_flag_alone(self):
        row = candidate('bug', 'waiting_repair')
        scan, issues = fixture([row], complete=True)
        bugs = add_repair(scan, [row])
        bugs[0].update(identifier='GRM-4', title='修复｜问题摘要')
        issues += bugs
        result = snapshot(scan, issues)
        self.assertEqual((result['repair']['completed'], result['repair']['total']), (0, 1))
        deliver(scan, bugs[0])
        self.assertEqual(snapshot(scan, issues)['repair']['state'], 'completed')
        scan['metadata']['dailyBatch']['completedDeliveries'][0]['report']['readbackHashVerified'] = False
        result = snapshot(scan, issues)
        self.assertEqual(result['repair']['state'], 'attention')
        self.assertIsNone(result['repair']['completed'])

    def test_human_skip_and_partial_delivery_are_not_all_delivered(self):
        row = candidate('bug', 'waiting_repair')
        scan, issues = fixture([row], complete=True)
        bugs = add_repair(scan, [row], state='completed')
        scan['metadata']['dailyBatch']['outcome'] = 'finished_with_skips'
        result = snapshot(scan, issues + bugs)
        self.assertEqual((result['repair']['state'], result['repair']['completed']), ('completed_with_skips', 0))

    def test_blocked_or_paused_repair_keeps_verified_count_and_real_next_action(self):
        row = candidate('bug', 'waiting_repair')
        scan, issues = fixture([row], complete=True)
        bugs = add_repair(scan, [row], state='blocked')
        bugs[0]['metadata']['dailyBug']['blocker'] = {'kind': 'authorization_required', 'phase': 'test_deployment'}
        result = snapshot(scan, issues + bugs)
        self.assertEqual((result['repair']['state'], result['repair']['completed']), ('blocked', 0))
        self.assertIn('等待当前任务所需授权', result['nextAction'])
        self.assertIn('完整交付 0/1', str(board.progress_rows(result)))
        scan['metadata']['dailyBatch']['state'] = 'paused'
        self.assertIn('等待真人决定', snapshot(scan, issues + bugs)['nextAction'])

    def test_registration_mismatch_or_wrong_scope_cannot_be_successful_zero(self):
        for mutation in ('missing', 'scope', 'paused'):
            scan, issues = fixture([candidate('bug')])
            if mutation == 'missing':
                issues.pop()
            elif mutation == 'scope':
                issues[1]['project_id'] = 'another-project'
            else:
                issues[1]['metadata']['dailyInvestigation']['state'] = 'paused'
            with self.subTest(mutation=mutation):
                result = snapshot(scan, issues)
                self.assertEqual(result['collection']['total'], 7)
                self.assertEqual(result['investigation']['state'], 'attention')
                self.assertIsNone(result['investigation']['completed'])
                self.assertTrue(result['warnings'])

    def test_runtime_read_failure_does_not_invent_idle_or_current_task_from_intent(self):
        scan, issues = fixture([candidate('bug')])
        scan['metadata']['dailyRound']['currentInvestigationIssueId'] = 'bug'
        result = progress.snapshot(scan, issues, None, NOW, BASE)
        self.assertFalse(result['activityKnown'])
        self.assertEqual(result['current'], [])
        self.assertIn('运行状态读取失败', '\n'.join(board.progress_lines(result)))

    def test_collection_in_progress_and_failed_source_are_visible_as_stages(self):
        scan, issues = fixture([candidate('bug')])
        scan['metadata']['dailyRound']['registrationComplete'] = False
        scan['metadata']['dailyScan'].update(coverageComplete=False, collected={'canny': 2})
        result = snapshot(scan, issues)
        self.assertEqual((result['collection']['state'], result['collection']['total']), ('collecting', None))
        self.assertEqual(result['investigation']['state'], 'registering')
        scan['metadata']['dailyScan']['sources'] = {'intercom': {'status': 'failed'}}
        self.assertEqual(snapshot(scan, issues)['collection']['state'], 'incomplete')

    def test_successful_zero_is_distinct_from_invalid_counts(self):
        scan, issues = fixture([])
        scan['metadata']['dailyScan']['collected'] = dict.fromkeys(progress.SOURCES, 0)
        result = snapshot(scan, issues)
        self.assertEqual((result['collection']['state'], result['collection']['total']), ('completed', 0))
        for value in (-1, True, '7'):
            scan['metadata']['dailyScan']['collected']['canny'] = value
            with self.subTest(value=value):
                self.assertEqual(snapshot(scan, issues)['collection']['state'], 'attention')

    def test_two_active_investigations_are_shown_but_foreign_runs_are_not(self):
        scan, issues = fixture([candidate('first'), candidate('second')])
        runs = [{'issue_id': key, 'agent_id': 'agent', 'status': 'running'} for key in ('first', 'second', 'other')]
        runs.append({'issue_id': 'first', 'agent_id': 'foreign', 'status': 'running'})
        self.assertEqual(len(snapshot(scan, issues, runs)['current']), 2)

    def test_snapshot_is_read_only(self):
        scan, issues = fixture([candidate('bug')])
        before = copy.deepcopy(issues)
        snapshot(scan, issues)
        self.assertEqual(issues, before)


class BoardStagesTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(start='2026-10-12', today='2026-10-22', project='project', ledger_issue='L',
                               issue_url_base=BASE, reviewer='U1', slack_cli='slack', duty_agent='agent')

    def test_missing_scan_ledger_still_contributes_collection_progress_and_index(self):
        scan, issues = fixture([candidate('first', 'confirmed'), candidate('second')])
        with patch.object(board, 'load_issues', return_value=issues), \
             patch.object(board, 'run_json', return_value=[]):
            scans, bugs, warnings = board.collect(self.args(), NOW)
        self.assertEqual(warnings, [])
        result = board.build_board(scans, bugs, dt.date(2026, 10, 12), dt.date(2026, 10, 22))
        self.assertTrue(result['rows'][-1]['scanned'])
        self.assertEqual((result['rows'][-1]['collected'], result['rows'][-1]['confirmed']), (7, 1))
        self.assertEqual(result['total']['confirmedWithoutDetail'], 1)
        for render in (board.render_markdown, board.render_slack):
            text = render(result, NOW)
            self.assertIn('已完成 7 条', text)
            self.assertIn('已留档 1/2 个问题', text)
            self.assertIn('尚未开始', text)
        with patch.object(board, 'load_scan_index', return_value={'records': []}) as load:
            loaded, note = board.yesterday_scan(result, scans)
        self.assertEqual((loaded['identifier'], note), ('GRM-1', None))
        load.assert_called_once_with('GRM-1')

    def test_partial_sources_keep_known_counts_in_all_channels(self):
        scan, issues = fixture([candidate('bug')])
        scan['metadata']['dailyScan'].update(coverageComplete=False, collected={'canny': 2},
                                            sources={'intercom': {'status': 'failed'}})
        with patch.object(board, 'load_issues', return_value=issues), \
             patch.object(board, 'run_json', return_value=[]):
            scans, bugs, _ = board.collect(self.args(), NOW)
        result = board.build_board(scans, bugs, dt.date(2026, 10, 21), dt.date(2026, 10, 22))
        self.assertEqual(result['rows'][-1]['collectedBySource'], {'canny': 2})
        self.assertEqual(result['rows'][-1]['collected'], 2)
        html = board.render_html(board.html_payload(result, NOW, None, None))
        for text in (board.render_markdown(result, NOW), board.render_slack(result, NOW), html):
            self.assertIn('已采集 2 条', text)
            self.assertIn('来源覆盖不完整', text)

    def test_legacy_round_uses_final_ledger_when_metadata_has_no_collection_counts(self):
        scan, issues = fixture([candidate('bug')])
        del scan['metadata']['dailyScan']['collected']
        entry = {'issue': 'GRM-1', 'kind': 'scan', 'feedbackDate': '2026-10-21',
                 'coverageComplete': True, 'collected': {'canny': 2, 'facebook': 1, 'intercom': 4}}
        comments = [{'created_at': '2026-10-22T01:00:00Z',
                     'content': board.BOARD_MARKER + '\n```json\n' + json.dumps(entry) + '\n```'}]
        with patch.object(board, 'load_issues', return_value=issues), \
             patch.object(board, 'run_json', side_effect=[comments, []]):
            scans, bugs, _ = board.collect(self.args(), NOW)
        result = board.build_board(scans, bugs, dt.date(2026, 10, 21), dt.date(2026, 10, 22))
        self.assertEqual(result['rows'][-1]['collected'], 7)
        self.assertEqual(result['rows'][-1]['progress']['collection']['state'], 'completed')
        self.assertIn('剩余调查', result['rows'][-1]['progress']['nextAction'])

    def test_collection_start_is_visible_before_any_source_finishes(self):
        scan, issues = fixture([])
        scan['metadata']['dailyRound']['registrationComplete'] = False
        scan['metadata']['dailyScan'] = {'targetDate': '2026-10-21', 'coverageComplete': False,
                                        'sources': {'canny': {'status': 'queued'}}}
        run = {'issue_id': 'scan', 'agent_id': 'agent', 'status': 'running'}
        with patch.object(board, 'load_issues', return_value=issues), \
             patch.object(board, 'run_json', side_effect=[[], [run]]):
            scans, bugs, _ = board.collect(self.args(), NOW)
        result = board.build_board(scans, bugs, dt.date(2026, 10, 21), dt.date(2026, 10, 22))
        self.assertTrue(result['rows'][-1]['scanned'])
        stage = result['rows'][-1]['progress']
        self.assertEqual(stage['collection']['state'], 'collecting')
        self.assertEqual(stage['current'][0]['phase'], '采集')
        self.assertIn('采集执行中', board.render_slack(result, NOW))

    def test_confirmations_do_not_double_count_after_bug_registration(self):
        row = candidate('bug', 'confirmed')
        scan, issues = fixture([row])
        stage = snapshot(scan, issues)
        scans = [{'identifier': 'GRM-1', 'feedbackDate': '2026-10-21', 'createdAt': 'a', 'progress': stage}]
        bugs = [{'identifier': 'GRM-3', 'feedbackDate': '2026-10-21', 'state': 'fixing', 'prs': [],
                 'investigationIssueId': 'bug'}]
        result = board.build_board(scans, bugs, dt.date(2026, 10, 21), dt.date(2026, 10, 22))
        self.assertEqual((result['total']['confirmed'], result['total']['confirmedWithoutDetail']), (1, 0))

    def test_html_uses_same_stage_values_and_escapes_current_summary(self):
        scan, issues = fixture([candidate('bug')])
        issues[1]['title'] = '调查｜</script><img src=x onerror=alert(1)>'
        stage = snapshot(scan, issues, [{'issue_id': 'bug', 'agent_id': 'agent', 'status': 'running'}])
        result = board.build_board([{'feedbackDate': '2026-10-21', 'createdAt': 'a', 'progress': stage}], [],
                                   dt.date(2026, 10, 21), dt.date(2026, 10, 22))
        payload = board.html_payload(result, NOW, None, None)
        html = board.render_html(payload)
        self.assertIn('已留档 0/1 个问题', html)
        self.assertIn('&lt;/script&gt;&lt;img', html)
        self.assertNotIn('<img', html)

    def test_native_pagination_is_bounded_and_rejects_incomplete_snapshot(self):
        with patch.object(board, 'run_json', side_effect=[
            {'issues': [{'id': 'a'}], 'offset': 0, 'total': 2, 'has_more': True},
            {'issues': [{'id': 'b'}], 'offset': 1, 'total': 2, 'has_more': False}]):
            self.assertEqual([x['id'] for x in board.load_issues('project')], ['a', 'b'])
        with patch.object(board, 'run_json', return_value={'issues': [], 'offset': 0, 'total': 1, 'has_more': True}):
            with self.assertRaises(ValueError):
                board.load_issues('project')


if __name__ == '__main__':
    unittest.main()
