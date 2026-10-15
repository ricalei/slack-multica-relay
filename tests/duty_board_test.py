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
    def test_latest_sent_or_skipped_record_counts_otherwise_pending(self):
        self.assertEqual(board_module.notify_state(None), 'pending')
        self.assertEqual(board_module.notify_state({'state': 'drafted'}), 'pending')
        self.assertEqual(board_module.notify_state({'state': 'sent'}), 'sent')
        self.assertEqual(board_module.notify_state({'state': 'skipped'}), 'skipped')


class FollowUpTests(unittest.TestCase):
    NOTIFY = {'issue': 'B1', 'kind': 'notify', 'state': 'sent', 'at': '2026-10-14T00:00:00Z'}
    AT = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp()

    def part(self, pid, hours, author='admin', part_type='comment'):
        return {'id': pid, 'part_type': part_type, 'author_type': author, 'created_at': self.AT + hours * 3600,
                'text': f'text {pid}'}

    def run_follow_up(self, parts, record=None, now=NOW, source='intercom'):
        return board_module.follow_up(source, self.NOTIFY, record, lambda: parts, now)

    def test_other_sources_are_not_read(self):
        def fail():
            raise AssertionError('不应读取会话')
        self.assertEqual(board_module.follow_up('facebook', self.NOTIFY, None, fail, NOW)['state'], 'untracked')
        self.assertEqual(board_module.follow_up('canny', self.NOTIFY, None, fail, NOW)['state'], 'not_integrated')

    def test_awaiting_cs_lists_public_admin_replies_after_notify_and_after_judged_point(self):
        parts = [self.part('a0', -1), self.part('n1', 1, part_type='note'), self.part('b1', 2, author='bot'),
                 self.part('u1', 3, author='user'), self.part('a1', 4), self.part('a2', 6)]
        result = self.run_follow_up(parts)
        self.assertEqual(result['state'], 'awaiting_cs')
        self.assertEqual([item['partId'] for item in result['candidates']], ['a1', 'a2'])
        self.assertEqual(result['record']['notifyAt'], self.NOTIFY['at'])
        judged = {**result['record'], 'judgedThrough': '2026-10-14T04:00:00Z'}
        self.assertEqual([item['partId'] for item in self.run_follow_up(parts, judged)['candidates']], ['a2'])
        stale = {**judged, 'notifyAt': '2026-10-01T00:00:00Z'}
        self.assertEqual(len(self.run_follow_up(parts, stale)['candidates']), 2)

    def test_customer_message_after_cs_informed_is_response(self):
        record = {'issue': 'B1', 'kind': 'notify-followup', 'notifyAt': self.NOTIFY['at'],
                  'judgedThrough': '2026-10-14T04:00:00Z', 'csInformedAt': '2026-10-14T04:00:00Z',
                  'csInformedPartId': 'a1', 'respondedAt': None}
        before = [self.part('u0', 3, author='user'), self.part('a1', 4)]
        self.assertEqual(self.run_follow_up(before, record)['state'], 'cs_informed')
        result = self.run_follow_up(before + [self.part('u2', 7, author='lead'), self.part('u1', 5, author='user')], record)
        self.assertEqual((result['state'], result['respondedAt']), ('responded', '2026-10-14T05:00:00Z'))
        self.assertEqual(result['proposal'], {**record, 'respondedAt': '2026-10-14T05:00:00Z'})

    def test_recorded_response_and_deadline_skip_reading(self):
        def fail():
            raise AssertionError('不应读取会话')
        record = {'notifyAt': self.NOTIFY['at'], 'csInformedAt': '2026-10-14T04:00:00Z', 'respondedAt': '2026-10-14T05:00:00Z'}
        self.assertEqual(board_module.follow_up('intercom', self.NOTIFY, record, fail, NOW)['state'], 'responded')
        late = NOW + dt.timedelta(days=14)
        expired = board_module.follow_up('intercom', self.NOTIFY, {**record, 'respondedAt': None}, fail, late)
        self.assertEqual((expired['state'], expired['csInformedAt']), ('expired', '2026-10-14T04:00:00Z'))
        self.assertEqual(self.run_follow_up(None)['state'], 'unread')

    def test_layers_labels_and_new_response_attention(self):
        released = [pr('merged', release={'tag': 't'})]
        def item(identifier, notify, follow=None):
            return {**bug(identifier, '2026-10-13', 'released', released), 'notify': notify, 'followUp': follow}
        bugs = [item('P', 'pending'), item('S', 'skipped'),
                item('W', 'sent', {'state': 'awaiting_cs', 'deadline': '2026-10-27T00:00:00+00:00'}),
                item('R', 'sent', {'state': 'responded', 'respondedAt': '2026-10-14T20:00:00Z'}),
                item('O', 'sent', {'state': 'responded', 'respondedAt': '2026-10-13T20:00:00Z'})]
        board = board_module.build_board([], bugs, dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        self.assertEqual((board['rows'][0]['notified'], board['rows'][0]['responded']), (3, 2))
        self.assertEqual([entry[0]['identifier'] for entry in board_module.attention(board, NOW)['responded']], ['R'])
        labels = {item['identifier']: board_module.notify_label(item, NOW) for item in bugs}
        self.assertEqual(labels['P'], '待回告')
        self.assertEqual(labels['S'], '不回告：原因未填')
        self.assertEqual(labels['W'], '已回告，待 CS 告知客户（剩 11 天）')
        self.assertEqual(labels['R'], '客户有回应（10-15）')
        self.assertIn('已回告 3 个 · 客户回应 2 个', board_module.render_slack(board, NOW))


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

    def test_released_bugs_without_notify_record_need_notify(self):
        released = [pr('merged', release={'tag': 't'})]
        bugs = [{**bug(f'N{index}', '2026-10-13', 'released', released), 'notify': notify}
                for index, notify in enumerate(('pending', 'sent', 'skipped'))]
        board = board_module.build_board([], bugs, dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        self.assertEqual([entry[0]['identifier'] for entry in board_module.attention(board, NOW)['notify']], ['N0'])
        self.assertIn('- 已上线待回告 1：[N0](https://m/N0)', board_module.render_slack(board, NOW))

if __name__ == '__main__':
    unittest.main()
