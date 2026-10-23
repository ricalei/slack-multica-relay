import datetime as dt
import importlib.util
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
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


class GitHubTests(unittest.TestCase):
    def test_merged_pr_needs_no_actions_or_release_queries(self):
        github = board_module.GitHub()
        with patch.object(github, 'api', return_value={
            'state': 'closed', 'merged_at': '2026-10-14T00:00:00Z',
            'created_at': '2026-10-13T00:00:00Z',
        }) as api:
            result = github.pr('https://github.com/o/r/pull/7')
        self.assertEqual(result['state'], 'merged')
        api.assert_called_once_with('repos/o/r/pulls/7')



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
        self.assertEqual(board_module.follow_up('jira', self.NOTIFY, None, fail, NOW)['state'], 'not_integrated')

    def canny(self, score, *comments):
        return {'post': {'score': score}, 'comments': [
            {'id': cid, 'createdAt': created, 'authorId': 'a', 'authorName': name, 'parentId': None,
             'internal': internal, 'private': False, 'text': f'text {cid}'}
            for cid, created, name, internal in comments]}

    def test_canny_new_public_comments_go_to_semantic_judgment_with_baseline(self):
        activity = self.canny(5, ('c0', '2026-10-13T23:00:00Z', 'Winches', False),
                              ('c1', '2026-10-14T03:00:00.5Z', 'Cust', False),
                              ('c2', '2026-10-14T04:00:00Z', 'Staff', True))
        result = self.run_follow_up(activity, source='canny')
        self.assertEqual((result['state'], result['scoreDelta']), ('awaiting_customer', 0))
        self.assertEqual(result['candidates'], [{'commentId': 'c1', 'at': '2026-10-14T03:00:00Z', 'author': 'Cust',
                                                 'isReply': False, 'text': 'text c1'}])
        self.assertEqual({key: result['record'][key] for key in ('baselineScore', 'judgedThrough', 'respondedAt')},
                         {'baselineScore': 5, 'judgedThrough': None, 'respondedAt': None})

    def test_canny_baseline_is_proposed_when_nothing_to_judge_and_judged_point_skips_old(self):
        quiet = self.canny(7, ('c0', '2026-10-13T23:00:00Z', 'Winches', False))
        result = self.run_follow_up(quiet, source='canny')
        self.assertEqual(result['proposal']['baselineScore'], 7)
        record = {**result['proposal'], 'judgedThrough': '2026-10-14T03:00:00Z'}
        later = self.canny(9, ('c1', '2026-10-14T03:00:00Z', 'Cust', False), ('c3', '2026-10-14T05:00:00Z', 'Cust', False))
        result = self.run_follow_up(later, record, source='canny')
        self.assertEqual(([item['commentId'] for item in result['candidates']], result['scoreDelta']), (['c3'], 2))
        self.assertNotIn('proposal', result)

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
        canny = {**item('C', 'sent', {'state': 'awaiting_customer', 'scoreDelta': 2,
                                      'deadline': '2026-10-27T00:00:00+00:00'}), 'source': 'canny'}
        self.assertEqual(board_module.notify_label(canny, NOW), '已回告，待客户回应（票数 +2，剩 11 天）')
        canny['followUp'] = {'state': 'expired'}
        self.assertEqual(board_module.notify_label(canny, NOW), '客户无回应（已截止）')
        self.assertIn('已回告 3 个 · 客户回应 2 个', board_module.render_slack(board, NOW))


class BugStateTests(unittest.TestCase):
    def test_rejection_preserves_confirmation_but_does_not_override_all_merged(self):
        self.assertEqual(board_module.bug_state([pr('open')], True), 'rejected')
        self.assertEqual(board_module.bug_state([pr('merged')], True), 'released')

    def test_released_requires_every_pr_merged_without_release_workflows(self):
        self.assertEqual(board_module.bug_state([pr('merged'), pr('merged')], False), 'released')
        self.assertEqual(board_module.bug_state([pr('merged', deployable=False)], False), 'released')
        self.assertEqual(board_module.bug_state([pr('merged'), pr('open')], False), 'review')
        self.assertEqual(board_module.bug_state([pr('merged'), pr('closed')], False), 'pr_closed')

    def test_open_closed_and_missing_prs(self):
        self.assertEqual(board_module.bug_state([pr('merged'), pr('open')], False), 'review')
        self.assertEqual(board_module.bug_state([pr('open'), pr('closed')], False), 'pr_closed')
        self.assertEqual(board_module.bug_state([], False), 'fixing')

    def test_blocked_repair_has_its_own_state_unless_all_prs_are_merged(self):
        self.assertEqual(board_module.bug_state([pr('open')], False, blocked=True), 'blocked')
        self.assertEqual(board_module.bug_state([], False, blocked=True), 'blocked')
        self.assertEqual(board_module.bug_state([pr('merged'), pr('open')], False, blocked=True), 'blocked')
        self.assertEqual(board_module.bug_state([pr('merged')], False, blocked=True), 'released')
        self.assertEqual(board_module.bug_state([pr('open')], True, blocked=True), 'blocked')

    def test_pending_review_and_blocked_are_separate_in_counts_and_all_views(self):
        bugs = [bug('GRM-633', '2026-10-13', 'review'), bug('GRM-634', '2026-10-13', 'review'),
                {**bug('GRM-1375', '2026-10-13', 'blocked'), 'blockedReason': '部署权限不足'},
                {**bug('GRM-1377', '2026-10-13', 'blocked', []), 'blockedReason': '缺少验收身份'}]
        scans = [{'identifier': 'S1', 'feedbackDate': '2026-10-13', 'createdAt': NOW.isoformat()}]
        board = board_module.build_board(scans, bugs, dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        self.assertEqual(tuple(board['total'][k] for k in ['confirmed', 'blocked', 'rejected', 'shouldShip', 'delivered']),
                         (4, 2, 0, 2, 2))
        cells = dict(zip(board_module.FUNNEL_HEADER, board_module.funnel_cells(board, board['total'])))
        self.assertEqual((cells['受阻'], cells['不上线'], cells['已交付 PR']), ('2', '0', '2'))
        slack = board_module.render_slack(board, NOW)
        self.assertIn('⚪ 待审查 2：', slack)
        self.assertIn('🚧 受阻 2：', slack)
        self.assertNotIn('⛔ 不上线', slack)
        markdown = board_module.render_markdown(board, NOW)
        self.assertIn('部署权限不足', markdown)
        self.assertIn('未交付（历史 PR 见任务）', markdown)
        payload = board_module.html_payload(board, NOW, None, '无索引')
        self.assertEqual({row['identifier'] for row in payload['todo']}, {'GRM-633', 'GRM-634'})
        html = board_module.render_html(payload)
        self.assertIn('<th>受阻</th>', html)
        self.assertIn('<h3>受阻 2</h3>', html)
        self.assertIn('<h3>待审查 2</h3>', html)
        self.assertIn('缺少验收身份', html)


class SlackReadResilienceTests(unittest.TestCase):
    def collect(self, first_state, prs, read_thread, string_delivery=False):
        scan = {'id': 'scan', 'identifier': 'S1', 'title': 'scan', 'created_at': '2026-10-14T01:00:00Z',
                'metadata': {'dailyScan': {'targetDate': '2026-10-13'}}}
        issues = [scan]
        entries = [{'issue': 'S1', 'kind': 'scan', 'feedbackDate': '2026-10-13',
                    'collected': {'facebook': 2}, 'suspected': 2, 'coverageComplete': True}]
        for identifier, state, urls in [('B1', first_state, list(prs)), ('B2', 'delivered', ['next'])]:
            issues.append({'id': identifier, 'identifier': identifier, 'title': identifier,
                           'created_at': '2026-10-14T02:00:00Z', 'parent_issue_id': 'scan',
                           'metadata': {'dailyBug': {'mode': 'bug', 'batchId': 'scan', 'state': state,
                                                    'blocker': {'reason': '测试环境权限不足'}}}})
            entries.append({'issue': identifier, 'kind': 'bug', 'source': 'facebook',
                            'prs': urls, 'delivery': identifier if string_delivery else {'permalink': identifier}})
        comments = [{'created_at': '2026-10-14', 'content': board_module.BOARD_MARKER
                     + '\n```json\n' + json.dumps(entries) + '\n```'}]
        args = SimpleNamespace(start='2026-10-13', today='2026-10-15', project='p', ledger_issue='L',
                               issue_url_base='https://m/', reviewer=REVIEWER, slack_cli='slack')
        with patch.object(board_module, 'load_issues', return_value=issues), \
             patch.object(board_module, 'run_json', return_value=comments), \
             patch.object(board_module.GitHub, 'pr', side_effect=lambda url: prs.get(url) or pr('open')), \
             patch.object(board_module, 'slack_thread', side_effect=read_thread) as threads:
            scans, bugs, warnings = board_module.collect(args, NOW)
        result = board_module.build_board(scans, bugs, dt.date(2026, 10, 13), dt.date(2026, 10, 15))
        result['warnings'].extend(warnings)
        return result, threads

    def test_string_delivery_permalink_in_legacy_ledger_is_read_without_dropping_bugs(self):
        board, threads = self.collect('delivered', {'one': pr('open')}, lambda cli, url: [], string_delivery=True)
        self.assertEqual([item['state'] for item in board['bugs']], ['review', 'review'])
        self.assertEqual([item['deliveryPermalink'] for item in board['bugs']], ['B1', 'B2'])
        self.assertEqual(board['warnings'], [])
        threads.assert_any_call('slack', 'B1')
        threads.assert_any_call('slack', 'B2')

    def test_blocked_and_merged_bugs_do_not_depend_on_deleted_slack_threads(self):
        for prs in ({}, {'one': pr('open')}, {'one': pr('merged'), 'two': pr('open')},
                    {'one': pr('merged'), 'two': pr('merged')}):
            with self.subTest(prs=prs):
                def thread(cli, url):
                    self.assertEqual(url, 'B2', '受阻和全部合并项不应读取已删除的旧消息')
                    return []
                board, threads = self.collect('blocked', prs, thread)
                first = board['bugs'][0]
                merged = bool(prs) and all(item['state'] == 'merged' for item in prs.values())
                self.assertEqual(first['state'], 'released' if merged else 'blocked')
                self.assertEqual(len(first['prs']), len(prs))
                self.assertEqual(board['warnings'], [])
                threads.assert_called_once_with('slack', 'B2')
                if merged:
                    continue
                self.assertEqual((board['total']['confirmed'], board['total']['blocked'], board['total']['rejected'],
                                  board['total']['shouldShip'], board['total']['delivered']), (2, 1, 0, 1, 1))
                self.assertEqual(first['blockedReason'], '测试环境权限不足')
                self.assertIsNone(first['rejection'])
                self.assertEqual([item['identifier'] for item, _ in board_module.attention(board, NOW)['review']], ['B2'])
                self.assertIn('🚧 受阻 1：[B1]', board_module.render_slack(board, NOW))
                self.assertIn('测试环境权限不足', board_module.render_markdown(board, NOW))
                payload = board_module.html_payload(board, NOW, None, '无索引')
                self.assertIn('测试环境权限不足', board_module.render_html(payload))
                self.assertNotIn('B1', [item['identifier'] for item in payload['todo']])

    def test_missing_thread_timeout_or_invalid_response_keeps_other_bugs_and_reports_unknown_review(self):
        failures = [subprocess.CalledProcessError(1, ['slack'], stderr='thread_not_found'),
                    subprocess.TimeoutExpired(['slack'], 30), OSError('unavailable'),
                    json.JSONDecodeError('invalid', '', 0), KeyError('messages'), ValueError('invalid messages')]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                def thread(cli, url):
                    if url == 'B1':
                        raise error
                    return []
                board, threads = self.collect('delivered', {'one': pr('open')}, thread)
                self.assertEqual([item['state'] for item in board['bugs']], ['review', 'review'])
                self.assertIsNone(board['bugs'][0]['rejection'])
                self.assertEqual((board['total']['confirmed'], board['total']['rejected']), (2, 0))
                self.assertEqual(threads.call_count, 2)
                self.assertEqual(len(board['warnings']), 1)
                for render in (board_module.render_markdown, board_module.render_slack):
                    self.assertIn('B1 Slack 交付 thread 读取失败', render(board, NOW))
                payload = board_module.html_payload(board, NOW, None, '无索引')
                self.assertIn('B1 Slack 交付 thread 读取失败，审查标记未核对', board_module.render_html(payload))


class BoardTests(unittest.TestCase):
    def test_groups_by_batch_feedback_date_and_excludes_outside_window(self):
        scans = [
            {'identifier': 'S1', 'feedbackDate': '2026-10-12', 'createdAt': '2026-10-13T01:00:00Z', 'scanDate': '2026-10-13',
             'collected': {'canny': 2, 'intercom': 8}, 'suspected': 4, 'coverageComplete': True},
            {'identifier': 'S2', 'feedbackDate': '2026-10-13', 'createdAt': '2026-10-14T01:00:00Z', 'scanDate': '2026-10-14',
             'collected': {'canny': 5}, 'suspected': None, 'coverageComplete': True},
        ]
        bugs = [
            bug('B0', '2026-10-02', 'review'),
            bug('B1', '2026-10-13', 'released', [pr('merged', release={'tag': 't'})]),
            bug('B2', '2026-10-13', 'rejected', [pr('open')]),
            bug('B3', '2026-10-13', 'released', [pr('merged')]),
            bug('B4', '2026-10-14', 'review'),
        ]
        board = board_module.build_board(scans, bugs, dt.date(2026, 10, 12), dt.date(2026, 10, 14))
        self.assertEqual([row['date'] for row in board['rows']], ['2026-10-12', '2026-10-13'])
        d2 = board['rows'][1]
        self.assertEqual((d2['confirmed'], d2['rejected'], d2['shouldShip'], d2['delivered'], d2['merged'], d2['released']),
                         (3, 1, 2, 3, 2, 2))
        self.assertIsNone(board['trial'])
        self.assertEqual([item['identifier'] for item in board['bugs']], ['B1', 'B2', 'B3'])
        self.assertEqual(board['rows'][0]['scanDate'], '2026-10-13')
        self.assertIn('10-13 扫描（10-12 反馈）', board['rows'][0]['label'])
        total = board['total']
        self.assertEqual((total['collected'], total['suspected'], total['confirmed']), (15, None, 3))
        self.assertNotIn('试运行', total['label'])
        self.assertIn('**累计（10-12 起）**', board_module.render_slack(board, NOW))

    def test_latest_scan_wins_and_missing_day_is_unscanned(self):
        scans = [
            {'identifier': 'S1', 'feedbackDate': '2026-10-12', 'createdAt': '2026-10-13T01:00:00Z', 'collected': {'canny': 1}},
            {'identifier': 'S1b', 'feedbackDate': '2026-10-12', 'createdAt': '2026-10-13T05:00:00Z', 'collected': {'canny': 3}},
        ]
        board = board_module.build_board(scans, [], dt.date(2026, 10, 12), dt.date(2026, 10, 14))
        self.assertEqual(board['rows'][0]['collected'], 3)
        self.assertFalse(board['rows'][1]['scanned'])
        self.assertEqual(board['total']['collected'], 3)
        self.assertEqual(board['total']['label'], '累计（d1–d2，不含未扫描 10-13）')
        slack = board_module.render_slack(board, NOW)
        self.assertIn('**累计（10-12 起，不含未扫描 10-13）**：采集 3 条', slack)

    def test_scanned_day_missing_ledger_field_keeps_total_missing(self):
        scans = [{'identifier': 'S1', 'feedbackDate': '2026-10-12', 'createdAt': 'a', 'collected': {'canny': 2}},
                 {'identifier': 'S2', 'feedbackDate': '2026-10-13', 'createdAt': 'b'}]
        board = board_module.build_board(scans, [], dt.date(2026, 10, 12), dt.date(2026, 10, 14))
        self.assertIsNone(board['total']['collected'])
        self.assertEqual(board['total']['unscanned'], [])

    def test_scan_sums_must_match(self):
        scan = {'identifier': 'S1', 'collected': {'canny': 10}, 'nonBug': 5, 'productDecision': 1, 'csFollowed': 1,
                'suspected': 2, 'outcomes': {'confirmed': 1, 'duplicate': 0, 'verificationBlocked': 0,
                                             'excludedAfterInvestigation': 0}}
        self.assertEqual(len(board_module.scan_warnings(scan)), 2)
        scan.update(suspected=3, outcomes={**scan['outcomes'], 'verificationBlocked': 2})
        self.assertEqual(board_module.scan_warnings(scan), [])

    def test_status_lines_list_each_bug_once_and_truncate_accumulating_states(self):
        released = [bug(f'R{index}', '2026-10-13', 'released', [pr('merged', release={'tag': 't'})])
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
        board = board_module.build_board([], [{**bug('B1', '2026-10-13', 'released', [pr('merged')]), 'notify': 'pending'}, no_message],
                                         dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        items = board_module.attention(board, NOW)
        self.assertEqual([entry[0]['identifier'] for entry in items['notify']], ['B1'])
        self.assertEqual(items['review'][0][1], '2 天')
        self.assertEqual([entry[0]['identifier'] for entry in items['unrejectable']], ['B2'])

    def test_missing_delivery_link_is_reported_only_after_pr_or_delivery(self):
        queued = {**bug('Q1', '2026-10-13', 'fixing', []), 'deliveryPermalink': None}
        delivered = {**bug('D1', '2026-10-13', 'fixing', []), 'deliveryPermalink': None, 'delivered': True}
        with_pr = {**bug('P1', '2026-10-13', 'review'), 'deliveryPermalink': None}
        board = board_module.build_board([], [queued, delivered, with_pr], dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        self.assertEqual([entry[0]['identifier'] for entry in board_module.attention(board, NOW)['unrejectable']],
                         ['D1', 'P1'])

    def test_released_bugs_without_notify_record_need_notify(self):
        released = [pr('merged', release={'tag': 't'})]
        bugs = [{**bug(f'N{index}', '2026-10-13', 'released', released), 'notify': notify}
                for index, notify in enumerate(('pending', 'sent', 'skipped'))]
        board = board_module.build_board([], bugs, dt.date(2026, 10, 13), dt.date(2026, 10, 14))
        self.assertEqual([entry[0]['identifier'] for entry in board_module.attention(board, NOW)['notify']], ['N0'])
        self.assertIn('- 已上线待回告 1：[N0](https://m/N0)', board_module.render_slack(board, NOW))


class CohortTests(unittest.TestCase):
    def test_real_batch_attribution_excludes_older_batch_and_retains_rejected_confirmations(self):
        scans = [
            {'id': 'batch12', 'identifier': 'GRM-621', 'title': 'scan', 'created_at': '2026-10-13T01:00:00Z',
             'metadata': {'dailyScan': {'targetDate': '2026-10-12'}}},
            {'id': 'batch13', 'identifier': 'GRM-635', 'title': 'scan', 'created_at': '2026-10-14T01:00:00Z',
             'metadata': {'dailyScan': {'targetDate': '2026-10-13'}}},
            {'id': 'old', 'identifier': 'GRM-602', 'title': 'old', 'created_at': '2026-10-11T03:00:00Z', 'metadata': {}},
        ]
        issues, entries = list(scans), []
        for scan in scans[:2]:
            entries.append({'issue': scan['identifier'], 'kind': 'scan',
                            'feedbackDate': scan['metadata']['dailyScan']['targetDate'],
                            'collected': {'canny': 10}, 'suspected': 4})
        for key, parent, source_date in [('GRM-632', 'batch12', '2026-10-05'),
                                         ('GRM-633', 'batch12', '2026-10-03'),
                                         ('GRM-634', 'batch12', '2026-10-02'),
                                         ('GRM-636', 'batch13', '2026-10-13'),
                                         ('GRM-603', 'old', '2026-09-28')]:
            issues.append({'id': key, 'identifier': key, 'title': f'每日 Bug 值守｜{key}',
                           'created_at': '2026-10-15T01:00:00Z', 'parent_issue_id': parent,
                           'metadata': {'dailyBug': json.dumps({'mode': 'bug', 'batchId': parent})}})
            entries.append({'issue': key, 'kind': 'bug', 'feedbackDate': source_date,
                            'source': 'canny', 'prs': [key], 'delivery': {'permalink': key}})
        comments = [{'created_at': '2026-10-15', 'content': board_module.BOARD_MARKER
                     + '\n```json\n' + json.dumps(entries) + '\n```'}]
        args = SimpleNamespace(start='2026-10-12', today='2026-10-15', project='p', ledger_issue='L',
                               issue_url_base='https://m/', reviewer=REVIEWER, slack_cli='slack')
        def read_pr(key):
            self.assertNotEqual(key, 'GRM-603', 'out-of-window PRs must not be queried')
            return pr('open' if key in ('GRM-633', 'GRM-634') else 'merged')
        def thread(key):
            if key in ('GRM-633', 'GRM-634'):
                return [{'reactions': [{'name': 'no_entry_sign', 'users': [REVIEWER]}]}]
            return []
        with patch.object(board_module, 'load_issues', return_value=issues), \
             patch.object(board_module, 'run_json', return_value=comments), \
             patch.object(board_module.GitHub, 'pr', side_effect=read_pr) as prs, \
             patch.object(board_module, 'slack_thread', side_effect=lambda cli, url: thread(url)):
            collected, bugs, warnings = board_module.collect(args, NOW)
        self.assertEqual(warnings, [])
        self.assertEqual(prs.call_count, 4)
        board = board_module.build_board(collected, bugs, dt.date(2026, 10, 12), dt.date(2026, 10, 15))
        first, second = board['rows'][:2]
        self.assertEqual((first['scanDate'], first['confirmed'], first['released']), ('2026-10-13', 3, 1))
        self.assertEqual((second['scanDate'], second['confirmed'], second['released']), ('2026-10-14', 1, 1))
        self.assertEqual((board['total']['confirmed'], board['total']['released']), (4, 2))
        self.assertEqual(board['total']['delivered'], 4)
        for render in (board_module.render_markdown, board_module.render_slack):
            text = render(board, NOW)
            self.assertNotIn('GRM-603', text)
            self.assertNotIn('Release to PRODUCTION', text)
        self.assertEqual(bugs[0]['sourceFeedbackDate'], '2026-10-05')
        self.assertEqual(bugs[0]['feedbackDate'], '2026-10-12')

    def test_batch_date_uses_shanghai_timezone_and_missing_link_is_not_guessed(self):
        batch = {'identifier': 'S1', 'created_at': '2026-10-13T17:00:00Z',
                 'metadata': {'dailyScan': {'targetDate': '2026-10-13'}}}
        issue = {'parent_issue_id': 's', 'metadata': {'dailyBug': {'mode': 'bug'}}}
        self.assertEqual(board_module.bug_cohort(issue, {'s': batch}, {})['scanDate'], '2026-10-14')
        self.assertIsNone(board_module.bug_cohort(issue, {}, {}))

    def test_investigation_repair_keeps_original_scan_cohort_without_adding_collection(self):
        scan = {'id': 'scan', 'identifier': 'S1', 'created_at': '2026-10-21T00:00:00Z',
                'metadata': {'dailyScan': {'targetDate': '2026-10-20'}}}
        batch = {'id': 'repair', 'identifier': 'R1', 'created_at': '2026-10-23T00:00:00Z',
                 'parent_issue_id': 'scan',
                 'metadata': {'dailyBatch': {'mode': 'repair', 'sourceScanId': 'scan'}}}
        bug = {'parent_issue_id': 'repair', 'metadata': {'dailyBug': {'mode': 'bug', 'batchId': 'repair'}}}
        cohort = board_module.bug_cohort(bug, {'scan': scan, 'repair': batch}, {})
        self.assertEqual(cohort, {'scanDate': '2026-10-21', 'feedbackDate': '2026-10-20', 'batch': 'R1'})
        self.assertNotIn('dailyScan', batch['metadata'])
        self.assertIsNone(board_module.bug_cohort(bug, {'repair': batch}, {}))
        batch['parent_issue_id'] = 'different-scan'
        self.assertIsNone(board_module.bug_cohort(bug, {'scan': scan, 'repair': batch}, {}))



class HtmlTests(unittest.TestCase):
    def test_scan_records_keep_judgement_fields_and_list_actionable_first(self):
        index = {'records': [
            {'state': 'non_bug', 'source': 'intercom', 'summary': 'S1', 'reason': 'R1', 'sourceUrl': 'javascript:alert(1)',
             'moegoCompanyIds': ['118708'], 'customerSegments': {'tier': 'T1'}},
            {'state': 'verification_blocked', 'source': 'canny', 'title': 'T2', 'sourceUrl': 'https://c/p/2',
             'unresolvedGap': '缺原请求', 'csTickets': ['CS-1', 'not a key'], 'verifiedJiraKeys': ['MER-2', 'CS-1'],
             'attempts': [{'path': 'redshift', 'query': 'company_id = 118708', 'result': '无记录'}, '读完原会话', 3]},
        ]}
        first, second = board_module.scan_records(index)
        self.assertEqual((first['state'], first['summary']), ('verification_blocked', 'T2'))
        self.assertEqual(first['tickets'], ['CS-1', 'MER-2'])
        self.assertEqual(first['attempts'], ['redshift：无记录', '读完原会话'])
        self.assertEqual(first['details'], [['待确认缺口', '缺原请求']])
        self.assertEqual(second['sourceUrl'], '')
        self.assertNotIn('118708', str([first, second]))

    def test_scan_index_is_read_from_evidence_zip(self):
        import json, tempfile, zipfile
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'GRM-1-evidence.zip'
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr('other.json', '{}')
                handle.writestr('nested/scan-evidence-index.json', json.dumps({'targetDate': '2026-10-14', 'records': []}))
            self.assertEqual(board_module.read_scan_index(archive)['targetDate'], '2026-10-14')
            empty = Path(directory) / 'empty.zip'
            with zipfile.ZipFile(empty, 'w') as handle:
                handle.writestr('other.json', '{}')
            self.assertIsNone(board_module.read_scan_index(empty))

    def test_html_is_static_and_escapes_record_text(self):
        board = board_module.build_board([], [], dt.date(2026, 10, 13), dt.date(2026, 10, 15))
        scan = {'identifier': 'S1', 'records': board_module.scan_records(
            {'records': [{'state': 'non_bug', 'summary': '</script><img src=x onerror=alert(1)>'}]})}
        html = board_module.render_html(board_module.html_payload(board, NOW, scan, None, 'https://m/board', 'https://m/'))
        self.assertEqual(html.count('</script>'), 1)
        self.assertIn('&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;', html)
        self.assertNotIn('<img', html)

    def test_date_views_use_each_days_latest_scan_and_limit_feedback_scope(self):
        scans = [
            {'identifier': 'S-OLD', 'feedbackDate': '2026-10-13', 'createdAt': 'a',
             'collected': {'canny': 2}, 'suspected': 1},
            {'identifier': 'S-NEW', 'feedbackDate': '2026-10-13', 'createdAt': 'b',
             'collected': {'canny': 8}, 'suspected': 3},
            {'identifier': 'S-14', 'feedbackDate': '2026-10-14', 'createdAt': 'c',
             'collected': {'canny': 4}, 'suspected': 2},
        ]
        bugs = [bug('TRIAL', '2026-10-11', 'review'), bug('EARLY', '2026-10-13', 'review'),
                bug('LATE', '2026-10-14', 'fixing')]
        board = board_module.build_board(scans, bugs, dt.date(2026, 10, 12), dt.date(2026, 10, 15))
        board['warnings'].append('全局读取告警')
        def index(identifier):
            return {'targetDate': '2026-10-13' if identifier == 'S-NEW' else '2026-10-14',
                    'records': [{'state': 'non_bug', 'summary': identifier}]}
        with patch.object(board_module, 'load_scan_index', side_effect=index) as load:
            payloads = board_module.html_date_payloads(board, scans, NOW)
        self.assertEqual([item['through'] for item in payloads], ['2026-10-12', '2026-10-13', '2026-10-14'])
        self.assertEqual([call.args[0] for call in load.call_args_list], ['S-NEW', 'S-14'])
        early, latest = payloads[1:]
        self.assertEqual(early['scan']['records'][0]['summary'], 'S-NEW')
        self.assertEqual(early['rows'][-1]['collected'], 8)
        self.assertEqual(early['total']['confirmed'], 1)
        self.assertEqual([item['identifier'] for item in early['bugs']], ['EARLY'])
        self.assertEqual({item['identifier'] for item in early['todo']}, {'EARLY'})
        self.assertIn('全局读取告警', early['warnings'])
        self.assertEqual(latest['total'], board['total'])
        self.assertIsNone(payloads[0]['scan'])
        self.assertIn('尚未扫描', payloads[0]['scanNote'])

    def test_missing_historical_index_does_not_drop_other_date_views(self):
        scans = [{'identifier': 'S-13', 'feedbackDate': '2026-10-13', 'createdAt': 'a'},
                 {'identifier': 'S-14', 'feedbackDate': '2026-10-14', 'createdAt': 'b'}]
        board = board_module.build_board(scans, [], dt.date(2026, 10, 13), dt.date(2026, 10, 15))
        with patch.object(board_module, 'load_scan_index', side_effect=[OSError('unavailable'),
                          {'targetDate': '2026-10-14', 'records': []}]):
            early, latest = board_module.html_date_payloads(board, scans, NOW)
        self.assertIsNone(early['scan'])
        self.assertIn('S-13 没有可用的证据索引', early['scanNote'])
        self.assertEqual(latest['scan']['identifier'], 'S-14')

    def test_provided_scan_index_only_serves_latest_date_and_must_match_scan(self):
        import json, tempfile
        scans = [{'identifier': 'S-13', 'feedbackDate': '2026-10-13', 'createdAt': 'a'},
                 {'identifier': 'S-14', 'feedbackDate': '2026-10-14', 'createdAt': 'b'}]
        board = board_module.build_board(scans, [], dt.date(2026, 10, 13), dt.date(2026, 10, 15))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan-evidence-index.json'
            path.write_text(json.dumps({'identifier': 'S-14', 'targetDate': '2026-10-14',
                                        'records': [{'state': 'non_bug', 'summary': 'provided'}]}))
            with patch.object(board_module, 'load_scan_index',
                              return_value={'targetDate': '2026-10-13', 'records': []}) as load:
                early, latest = board_module.html_date_payloads(board, scans, NOW, scan_index_path=str(path))
            self.assertEqual([call.args[0] for call in load.call_args_list], ['S-13'])
            self.assertEqual(early['scan']['identifier'], 'S-13')
            self.assertEqual(latest['scan']['records'][0]['summary'], 'provided')
            path.write_text(json.dumps({'identifier': 'S-OTHER', 'targetDate': '2026-10-14', 'records': []}))
            scan, note = board_module.yesterday_scan(latest, scans, str(path))
        self.assertIsNone(scan)
        self.assertIn('索引属于 S-OTHER', note)

    def test_download_failure_reason_is_kept_in_note(self):
        import subprocess
        scans = [{'identifier': 'S-14', 'feedbackDate': '2026-10-14', 'createdAt': 'a'}]
        board = board_module.build_board(scans, [], dt.date(2026, 10, 13), dt.date(2026, 10, 15))
        error = subprocess.CalledProcessError(1, ['multica', 'attachment', 'download', 'A1', '-o', '/tmp/x'],
                                              stderr='Error: get attachment:\n 403 forbidden\n')
        with patch.object(board_module, 'load_scan_index', side_effect=error):
            scan, note = board_module.yesterday_scan(board, scans)
        self.assertIsNone(scan)
        self.assertIn('multica attachment download 退出码 1：Error: get attachment: 403 forbidden', note)

    def test_html_embeds_dates_and_preserves_static_default(self):
        board = board_module.build_board([], [], dt.date(2026, 10, 12), dt.date(2026, 10, 15))
        payloads = board_module.html_date_payloads(board, [], NOW)
        html = board_module.render_html(payloads[-1], payloads)
        self.assertIn('<option value="2026-10-14" selected>', html)
        self.assertIn('<option value="2026-10-13">', html)
        self.assertIn('<template id="date-2026-10-13">', html)
        self.assertIn('<div id="board-view"><header', html)
        self.assertIn('Bug 进度为生成时的最新状态', html)
        self.assertNotIn('fetch(', html)

    def test_empty_date_range_still_renders_default(self):
        board = board_module.build_board([], [], dt.date(2026, 10, 15), dt.date(2026, 10, 15))
        payloads = board_module.html_date_payloads(board, [], NOW)
        self.assertEqual([item['through'] for item in payloads], ['2026-10-14'])
        self.assertIn('2026-10-14', board_module.render_html(payloads[0], payloads))

    def test_yesterday_without_scan_explains_missing_records(self):
        board = board_module.build_board([], [], dt.date(2026, 10, 13), dt.date(2026, 10, 15))
        scan, note = board_module.yesterday_scan(board, [])
        self.assertIsNone(scan)
        self.assertIn('2026-10-14 反馈日尚未扫描', note)

if __name__ == '__main__':
    unittest.main()
