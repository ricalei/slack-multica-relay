import datetime as dt
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('duty_board', Path(__file__).resolve().parents[1] / 'scripts/duty-board.py')
board_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board_module)

REVIEWER = 'U1'
NOW = dt.datetime(2026, 10, 15, 0, 30, tzinfo=dt.timezone.utc)


def pr(state, deployable=True, release=None):
    return {'url': 'https://github.com/o/r/pull/1', 'repo': 'o/r', 'number': 1, 'state': state,
            'createdAt': '2026-10-13T00:00:00Z', 'mergedAt': '2026-10-14T00:00:00Z' if state == 'merged' else None,
            'deployable': deployable, 'release': release}


def bug(identifier, date, state, prs=None):
    return {'identifier': identifier, 'url': f'https://m/{identifier}', 'summary': identifier, 'feedbackDate': date,
            'source': 'canny', 'sourceUrl': None, 'deliveryPermalink': 'https://s/p1', 'rejection': None,
            'prs': prs if prs is not None else [pr('open')], 'state': state}


class ReleaseTests(unittest.TestCase):
    def test_picks_earliest_successful_run_containing_merge_commit(self):
        runs = [
            {'created_at': '2026-10-14T10:00:00Z', 'head_sha': 'late', 'head_branch': 'release-261014.8', 'updated_at': 'T2'},
            {'created_at': '2026-10-14T08:00:00Z', 'head_sha': 'before', 'head_branch': 'release-261014.6', 'updated_at': 'T0'},
            {'created_at': '2026-10-14T09:00:00Z', 'head_sha': 'first', 'head_branch': 'release-261014.7', 'updated_at': 'T1'},
        ]
        contained = {'first', 'late'}
        release = board_module.find_release('m', runs, lambda base, head: head in contained)
        self.assertEqual(release['tag'], 'release-261014.7')

    def test_contains_by_ancestry_or_cherry_picked_pr_title(self):
        def commit(title):
            return {'commit': {'message': f'{title}\n\nbody'}}
        self.assertTrue(board_module.compare_contains({'status': 'identical', 'commits': []}, 7))
        diverged = {'status': 'diverged', 'commits': [commit('fix: other (#71)'), commit('fix: target (#7)')]}
        self.assertTrue(board_module.compare_contains(diverged, 7))
        self.assertFalse(board_module.compare_contains(diverged, 1))

    def test_no_release_when_no_run_contains_commit(self):
        runs = [{'created_at': 'a', 'head_sha': 'x', 'head_branch': 't', 'updated_at': 'u'}]
        self.assertIsNone(board_module.find_release('m', runs, lambda base, head: False))


class LedgerTests(unittest.TestCase):
    def test_latest_entry_per_issue_and_kind_wins_and_bad_records_are_reported(self):
        def comment(cid, created, body):
            return {'id': cid, 'created_at': created, 'content': f'{board_module.BOARD_MARKER}\n说明\n```json\n{body}\n```'}
        comments = [
            comment('c2', '2026-10-14T02:00:00Z', '{"issue":"GRM-1","kind":"bug","v":2}'),
            comment('c1', '2026-10-14T01:00:00Z',
                    '[{"issue":"GRM-1","kind":"bug","v":1},{"issue":"GRM-2","kind":"scan","v":1}]'),
            {'id': 'c3', 'created_at': '2026-10-14T03:00:00Z', 'content': '普通评论 ```json\n{"issue":"GRM-1","kind":"bug","v":9}\n```'},
            comment('c4', '2026-10-14T04:00:00Z', '{oops'),
            comment('c5', '2026-10-14T05:00:00Z', '{"v":3}'),
            comment('c6', '2026-10-14T06:00:00Z', '{"issue":"GRM-1","kind":"notify","v":1}'),
        ]
        entries, errors = board_module.ledger_entries(comments)
        self.assertEqual({key: value['v'] for key, value in entries.items()},
                         {('GRM-1', 'bug'): 2, ('GRM-2', 'scan'): 1, ('GRM-1', 'notify'): 1})
        self.assertEqual(len(errors), 2)


class RejectionTests(unittest.TestCase):
    def test_only_reviewer_no_entry_sign_counts_anywhere_in_thread(self):
        messages = [
            {'author': {'user_id': 'BOT'}, 'reactions': [{'name': 'no_entry_sign', 'users': ['U2']}]},
            {'author': {'user_id': 'BOT'}, 'reactions': [{'name': 'x', 'users': [REVIEWER]}]},
        ]
        self.assertIsNone(board_module.rejection(messages, REVIEWER))
        messages[1]['reactions'].append({'name': 'no_entry_sign', 'users': [REVIEWER]})
        self.assertEqual(board_module.rejection(messages, REVIEWER), {'reason': None})

    def test_reason_is_latest_reviewer_reply_prefixed_with_emoji(self):
        messages = [
            {'author': {'user_id': 'BOT'}, 'reactions': [{'name': 'no_entry_sign', 'users': [REVIEWER]}]},
            {'author': {'user_id': REVIEWER}, 'text_raw': ':no_entry_sign: 旧原因', 'reactions': []},
            {'author': {'user_id': REVIEWER}, 'text_raw': '普通审查意见', 'reactions': []},
            {'author': {'user_id': 'U2'}, 'text_raw': '🚫 别人的原因', 'reactions': []},
            {'author': {'user_id': REVIEWER}, 'text_raw': '🚫：影响仅限旧版 iPad', 'reactions': []},
        ]
        self.assertEqual(board_module.rejection(messages, REVIEWER), {'reason': '影响仅限旧版 iPad'})


class NotifyStateTests(unittest.TestCase):
    NOTIFY = {'issue': 'B1', 'kind': 'notify', 'draft': {'ts': '200.1'}}

    def draft(self, *reactions):
        return {'ts': '200.1', 'author': {'user_id': 'BOT'}, 'reactions': list(reactions)}

    def test_no_draft_record_is_pending_and_unmarked_draft_is_drafted(self):
        self.assertEqual(board_module.notify_state([], None, REVIEWER), 'pending')
        others = self.draft({'name': 'outbox_tray', 'users': ['U2']})
        self.assertEqual(board_module.notify_state([others], self.NOTIFY, REVIEWER), 'drafted')

    def test_reviewer_reaction_on_draft_only(self):
        root = {'ts': '100.1', 'author': {'user_id': 'BOT'}, 'reactions': [{'name': 'outbox_tray', 'users': [REVIEWER]}]}
        self.assertEqual(board_module.notify_state([root, self.draft()], self.NOTIFY, REVIEWER), 'drafted')
        sent = self.draft({'name': 'outbox_tray', 'users': [REVIEWER]})
        self.assertEqual(board_module.notify_state([root, sent], self.NOTIFY, REVIEWER), 'sent')
        both = self.draft({'name': 'outbox_tray', 'users': [REVIEWER]}, {'name': 'no_bell', 'users': [REVIEWER]})
        self.assertEqual(board_module.notify_state([both], self.NOTIFY, REVIEWER), 'skipped')

    def test_reviewer_reply_after_draft_counts(self):
        before = {'ts': '150.1', 'author': {'user_id': REVIEWER}, 'text_raw': '已回告'}
        self.assertEqual(board_module.notify_state([before, self.draft()], self.NOTIFY, REVIEWER), 'drafted')
        other = {'ts': '300.1', 'author': {'user_id': 'U2'}, 'text_raw': '📤 已发'}
        reply = {'ts': '301.1', 'author': {'user_id': REVIEWER}, 'text_raw': '🔕 客户已流失'}
        self.assertEqual(board_module.notify_state([self.draft(), other], self.NOTIFY, REVIEWER), 'drafted')
        self.assertEqual(board_module.notify_state([self.draft(), other, reply], self.NOTIFY, REVIEWER), 'skipped')


class BugStateTests(unittest.TestCase):
    def test_rejection_overrides_pr_progress(self):
        self.assertEqual(board_module.bug_state([pr('merged', release={'tag': 't'})], True), 'rejected')

    def test_released_requires_every_deployable_pr_released_and_ignores_libraries(self):
        library = pr('merged', deployable=False)
        self.assertEqual(board_module.bug_state([library, pr('merged', release={'tag': 't'})], False), 'released')
        self.assertEqual(board_module.bug_state([library, pr('merged', release={'tag': 't'}), pr('merged')], False), 'merged')
        self.assertEqual(board_module.bug_state([library], False), 'merged')

    def test_open_closed_and_missing_prs(self):
        self.assertEqual(board_module.bug_state([pr('merged'), pr('open')], False), 'review')
        self.assertEqual(board_module.bug_state([pr('open'), pr('closed')], False), 'pr_closed')
        self.assertEqual(board_module.bug_state([], False), 'fixing')


class BoardTests(unittest.TestCase):
    def test_groups_by_feedback_date_with_trial_row_and_excludes_today(self):
        scans = [
            {'identifier': 'S1', 'feedbackDate': '2026-10-12', 'createdAt': '2026-10-13T01:00:00Z',
             'collected': {'canny': 2, 'intercom': 8}, 'suspected': 4, 'coverageComplete': True},
            {'identifier': 'S2', 'feedbackDate': '2026-10-13', 'createdAt': '2026-10-14T01:00:00Z',
             'collected': {'canny': 5}, 'suspected': None, 'coverageComplete': True},
        ]
        bugs = [
            bug('B0', '2026-10-02', 'review'),
            bug('B1', '2026-10-13', 'released', [pr('merged', release={'tag': 't'})]),
            bug('B2', '2026-10-13', 'rejected', [pr('open')]),
            bug('B3', '2026-10-13', 'merged', [pr('merged')]),
            bug('B4', '2026-10-14', 'review'),
        ]
        board = board_module.build_board(scans, bugs, dt.date(2026, 10, 12), dt.date(2026, 10, 14))
        self.assertEqual([row['date'] for row in board['rows']], ['2026-10-12', '2026-10-13'])
        d2 = board['rows'][1]
        self.assertEqual((d2['confirmed'], d2['rejected'], d2['shouldShip'], d2['delivered'], d2['merged'], d2['released']),
                         (3, 1, 2, 2, 2, 1))
        self.assertEqual(board['trial']['confirmed'], 1)
        total = board['total']
        self.assertEqual((total['collected'], total['suspected'], total['confirmed']), (15, None, 3))

    def test_latest_scan_wins_and_missing_day_is_unscanned(self):
        scans = [
            {'identifier': 'S1', 'feedbackDate': '2026-10-12', 'createdAt': '2026-10-13T01:00:00Z', 'collected': {'canny': 1}},
            {'identifier': 'S1b', 'feedbackDate': '2026-10-12', 'createdAt': '2026-10-13T05:00:00Z', 'collected': {'canny': 3}},
        ]
        board = board_module.build_board(scans, [], dt.date(2026, 10, 12), dt.date(2026, 10, 14))
        self.assertEqual(board['rows'][0]['collected'], 3)
        self.assertFalse(board['rows'][1]['scanned'])
        self.assertIsNone(board['total']['collected'])

    def test_scan_sums_must_match(self):
        scan = {'identifier': 'S1', 'collected': {'canny': 10}, 'nonBug': 5, 'productDecision': 1, 'csFollowed': 1,
                'suspected': 2, 'outcomes': {'confirmed': 1, 'duplicate': 0, 'verificationBlocked': 0,
                                             'excludedAfterInvestigation': 0}}
        self.assertEqual(len(board_module.scan_warnings(scan)), 2)
        scan.update(suspected=3, outcomes={**scan['outcomes'], 'verificationBlocked': 2})
        self.assertEqual(board_module.scan_warnings(scan), [])

    def test_status_lines_list_each_bug_once_and_truncate_accumulating_states(self):
        released = [bug(f'R{index}', f'2026-10-0{index}', 'released', [pr('merged', release={'tag': 't'})])
                    for index in range(1, 8)]
        board = board_module.build_board([], released + [bug('W1', '2026-10-13', 'review')],
                                         dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        lines = board_module.status_lines(board, NOW)
        self.assertEqual(len(lines), 2)
        self.assertIn('R7', lines[0])
        self.assertNotIn('R2', lines[0])
        self.assertIn('等 7 个', lines[0])
        self.assertIn('W1', lines[1])
        self.assertIn('（2 天）', lines[1])

    def test_estimated_suspected_marks_its_row_and_total(self):
        scans = [{'identifier': 'S1', 'feedbackDate': '2026-10-12', 'createdAt': 'a', 'collected': {'canny': 9},
                  'suspected': 4, 'suspectedEstimated': True},
                 {'identifier': 'S2', 'feedbackDate': '2026-10-13', 'createdAt': 'b', 'collected': {'canny': 9},
                  'suspected': 5}]
        board = board_module.build_board(scans, [], dt.date(2026, 10, 12), dt.date(2026, 10, 14))
        self.assertEqual([board_module._suspected(row) for row in board['rows']], ['约 4', '5'])
        self.assertEqual(board_module._suspected(board['total']), '约 9')

    def test_attention_lists_pending_states_and_unrejectable_bugs(self):
        no_message = {**bug('B2', '2026-10-13', 'review'), 'deliveryPermalink': None}
        board = board_module.build_board([], [bug('B1', '2026-10-13', 'merged', [pr('merged')]), no_message],
                                         dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        items = board_module.attention(board, NOW)
        self.assertEqual([entry[0]['identifier'] for entry in items['merged']], ['B1'])
        self.assertEqual(items['merged'][0][1], '1 天')
        self.assertEqual([entry[0]['identifier'] for entry in items['unrejectable']], ['B2'])

    def test_released_bugs_pending_or_drafted_need_notify_and_stay_out_of_status_ages(self):
        released = [pr('merged', release={'tag': 't'})]
        bugs = [{**bug(f'N{index}', '2026-10-13', 'released', released), 'notify': notify}
                for index, notify in enumerate(('pending', 'drafted', 'sent', 'skipped'))]
        board = board_module.build_board([], bugs, dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        self.assertEqual([(entry[0]['identifier'], entry[1]) for entry in board_module.attention(board, NOW)['notify']],
                         [('N0', None), ('N1', '草稿待发')])
        slack = board_module.render_slack(board, NOW)
        self.assertIn('- 已上线待回告 2：[N0](https://m/N0)、[N1](https://m/N1)（草稿待发）', slack)
        self.assertNotIn('草稿待发', board_module.status_lines(board, NOW)[0])


if __name__ == '__main__':
    unittest.main()
