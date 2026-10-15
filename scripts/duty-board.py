#!/usr/bin/env python3
"""Build the daily Bug duty board from Multica, GitHub and Slack (read-only)."""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
PROD_WORKFLOW = 'Release to PRODUCTION'
REJECT_EMOJI = 'no_entry_sign'
REJECT_PREFIXES = ('🚫', ':no_entry_sign:')
PR_URL = re.compile(r'https://github\.com/([^/]+/[^/]+)/pull/(\d+)')
# 看板字段集中记在无人指派的台账任务评论里：任务 metadata 有 8KB 上限且已被值守状态占用，
# 在指派给 Agent 的任务下评论又会唤醒它。
BOARD_MARKER = '<!-- duty-board:v1 -->'
BOARD_JSON = re.compile(r'```json\n([\s\S]*?)\n```')
SCAN_SPLIT = ('nonBug', 'productDecision', 'csFollowed', 'suspected')
SCAN_OUTCOMES = ('confirmed', 'duplicate', 'verificationBlocked', 'excludedAfterInvestigation')
FOLLOW_UP_DAYS = 14
# 各来源 notify-followup 记录的跟踪字段，respondedAt 为共用的客户回应时间。
FOLLOW_UP_FIELDS = {'intercom': ('judgedThrough', 'csInformedAt', 'csInformedPartId'),
                    'canny': ('judgedThrough', 'baselineScore', 'respondedCommentId')}
STATE_LABELS = {
    'rejected': '⛔ 不上线', 'released': '🟢 已上线', 'merged': '🟡 已合并待上线',
    'review': '⚪ 待审', 'pr_closed': '⚠️ PR 已关闭未合并', 'fixing': '🔧 修复中',
}


# ---------- 纯逻辑：上线、🚫、漏斗 ----------

def find_release(merge_sha, runs, contains):
    """runs 为合并后成功的生产发布运行；返回最早包含该合并提交的那次。"""
    for run in sorted(runs, key=lambda item: item['created_at']):
        if contains(merge_sha, run['head_sha']):
            return {'tag': run['head_branch'], 'at': run['updated_at'], 'url': run.get('html_url')}
    return None


def ledger_entries(comments):
    """台账任务评论 → {(任务编号, kind): 字段}。每条带标记的评论含一个 JSON 对象或数组，
    每项须有 issue 字段；同一任务的同类记录以最新评论（同评论内靠后项）为准。
    同一 Bug 可有 bug 与 notify（回告）等多类记录，按 kind 分开取最新，互不覆盖。"""
    entries, errors = {}, []
    for comment in sorted(comments, key=lambda item: item['created_at']):
        content = comment.get('content') or ''
        if BOARD_MARKER not in content:
            continue
        for block in BOARD_JSON.findall(content):
            try:
                value = json.loads(block)
            except json.JSONDecodeError:
                errors.append(f"台账评论 {comment.get('id')} 的 JSON 无法解析")
                continue
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict) and isinstance(item.get('issue'), str):
                    entries[(item['issue'], item.get('kind'))] = item
                else:
                    errors.append(f"台账评论 {comment.get('id')} 有缺少 issue 的记录")
    return entries, errors


def compare_contains(compare, number):
    """compare 为 GitHub compare(合并提交...发布标签) 结果。
    部分仓库（如 Boarding_Desktop）发布线 cherry-pick PR 提交，SHA 不同但标题保留 (#PR号)。"""
    if compare['status'] in ('ahead', 'identical'):
        return True
    suffix = f'(#{number})'
    return any(commit['commit']['message'].split('\n', 1)[0].rstrip().endswith(suffix)
               for commit in compare.get('commits') or [])


def rejection(messages, reviewer, emoji=REJECT_EMOJI):
    """审查人在交付 thread 任意消息上点 🚫 即不上线；原因取其最新一条以 🚫 开头的回复。"""
    rejected = any(reviewer in (reaction.get('users') or []) and reaction.get('name', '').split('::')[0] == emoji
                   for message in messages for reaction in message.get('reactions') or [])
    if not rejected:
        return None
    reason = None
    for message in messages:
        author = message.get('author') or {}
        text = (message.get('text_raw') or '').strip()
        if reviewer in (author.get('user_id'), author.get('id')) and text.startswith(REJECT_PREFIXES):
            for prefix in REJECT_PREFIXES:
                text = text.removeprefix(prefix)
            reason = text.strip(' ：:｜|') or reason
    return {'reason': reason}


def notify_state(notify):
    """已上线 Bug 的回告进度：取台账最新 notify 记录的 sent / skipped，没有记录为 pending。"""
    state = (notify or {}).get('state')
    return state if state in ('sent', 'skipped') else 'pending'


def canny_follow_up(notify, record, activity, base):
    """Canny 评论没有官方标识，是否为客户回应由看板 Agent 按语义判断：回告后（及上次判断点 judgedThrough 后）的
    公开新评论交给它，判为客户回应的最早一条记为 respondedAt。首次跟踪时记录票数基准，票数变化只作参考。"""
    new_baseline = record.get('baselineScore') is None
    if new_baseline:
        record = {**record, 'baselineScore': activity['post']['score']}
    since = _parse(record.get('judgedThrough') or notify['at'])
    candidates = [{'commentId': item['id'], 'at': _parse(item['createdAt']).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'author': item.get('authorName', ''), 'isReply': bool(item['parentId']), 'text': item.get('text', '')[:2000]}
                  for item in activity['comments']
                  if not item['internal'] and not item['private'] and _parse(item['createdAt']) > since]
    result = {'state': 'awaiting_customer', **base, 'scoreDelta': activity['post']['score'] - record['baselineScore']}
    if candidates:
        return {**result, 'candidates': candidates, 'record': record}
    return {**result, 'proposal': record} if new_baseline else result


def _iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def _part_time(part):
    return dt.datetime.fromtimestamp(part['created_at'], dt.timezone.utc)


def follow_up(source, notify, record, read, now):
    """已回告 Bug 的回应进度，从回告时间 notify.at 起跟踪 FOLLOW_UP_DAYS 天。
    Intercom 须先由看板 Agent 判断 CS 回复确实告知客户已修复上线（记 csInformedAt），之后的客户消息才算回应；
    判断结果与回应时间记在 notify-followup 记录，judgedThrough 之前的 CS 回复不再重复判断。
    record 为最新 notify-followup，notifyAt 与本次回告不同则视为旧回告的记录；read() 读取会话消息，失败返回 None。
    返回 {'state', 'candidates': 待判断的 CS 回复, 'proposal': 待写入台账的记录}。"""
    if source == 'facebook':
        return {'state': 'untracked'}
    if source not in FOLLOW_UP_FIELDS:
        return {'state': 'not_integrated'}
    record = record if record and record.get('notifyAt') == notify['at'] else {
        'issue': notify['issue'], 'kind': 'notify-followup', 'notifyAt': notify['at'],
        **dict.fromkeys(FOLLOW_UP_FIELDS[source]), 'respondedAt': None}
    if record.get('respondedAt'):
        return {'state': 'responded', 'respondedAt': record['respondedAt']}
    deadline = _parse(notify['at']) + dt.timedelta(days=FOLLOW_UP_DAYS)
    base = {'deadline': deadline.isoformat(), 'csInformedAt': record.get('csInformedAt')}
    if now > deadline:
        return {'state': 'expired', **base}
    parts = read()
    if parts is None:
        return {'state': 'unread', **base}
    if source == 'canny':
        return canny_follow_up(notify, record, parts, base)
    if not record.get('csInformedAt'):
        since = _parse(record.get('judgedThrough') or notify['at'])
        candidates = [{'partId': part['id'], 'at': _iso(part['created_at']), 'text': part['text'][:2000]}
                      for part in parts if part['author_type'] == 'admin' and part['part_type'] != 'note'
                      and _part_time(part) > since]
        return {'state': 'awaiting_cs', **base, 'candidates': candidates, 'record': record}
    informed = _parse(record['csInformedAt'])
    replies = [part for part in parts if part['author_type'] in ('user', 'lead')
               and _part_time(part) > informed]
    if not replies:
        return {'state': 'cs_informed', **base}
    responded = _iso(min(part['created_at'] for part in replies))
    return {'state': 'responded', 'respondedAt': responded, 'proposal': {**record, 'respondedAt': responded}}


def bug_state(prs, rejected):
    if rejected:
        return 'rejected'
    if not prs:
        return 'fixing'
    if any(pr['state'] == 'closed' for pr in prs):
        return 'pr_closed'
    if any(pr['state'] == 'open' for pr in prs):
        return 'review'
    deployable = [pr for pr in prs if pr['deployable']]
    # 没有生产发布流程的仓库（如共享库）合并即就绪；Bug 需全部可部署 PR 都已上线。
    if deployable and all(pr.get('release') for pr in deployable):
        return 'released'
    return 'merged'


def scan_warnings(scan):
    warnings = []
    collected = scan.get('collected')
    total = sum(collected.values()) if isinstance(collected, dict) else None
    parts = [scan.get(key) for key in SCAN_SPLIT]
    if total is not None and all(isinstance(value, int) for value in parts) and sum(parts) != total:
        warnings.append(f"{scan['identifier']} 分类合计 {sum(parts)} ≠ 采集 {total}")
    outcomes = scan.get('outcomes')
    if isinstance(outcomes, dict) and isinstance(scan.get('suspected'), int):
        values = [outcomes.get(key) for key in SCAN_OUTCOMES]
        if all(isinstance(value, int) for value in values) and sum(values) != scan['suspected']:
            warnings.append(f"{scan['identifier']} 候选去向合计 {sum(values)} ≠ 疑似候选 {scan['suspected']}")
    return warnings


def _layers(bugs):
    alive = [bug for bug in bugs if bug['state'] != 'rejected']
    return {
        'confirmed': len(bugs),
        'rejected': len(bugs) - len(alive),
        'shouldShip': len(alive),
        'delivered': sum(1 for bug in alive if bug['prs']),
        'merged': sum(1 for bug in alive if bug['state'] in ('merged', 'released')),
        'released': sum(1 for bug in alive if bug['state'] == 'released'),
        'notified': sum(1 for bug in alive if bug.get('notify') == 'sent'),
        'responded': sum(1 for bug in alive if (bug.get('followUp') or {}).get('state') == 'responded'),
    }


def build_board(scans, bugs, start, today):
    """按反馈日分组：start 之前的 Bug 归入试运行；统计到 today 前一天为止。"""
    yesterday = today - dt.timedelta(days=1)
    latest_scan = {}
    for scan in sorted(scans, key=lambda item: item['createdAt']):
        latest_scan[scan['feedbackDate']] = scan
    rows, day = [], start
    while day <= yesterday:
        key = day.isoformat()
        scan = latest_scan.get(key)
        collected = scan.get('collected') if scan else None
        rows.append({
            'label': f'd{(day - start).days + 1} · {day:%m-%d}', 'date': key, 'scanned': scan is not None,
            'collected': sum(collected.values()) if isinstance(collected, dict) else None,
            'collectedBySource': collected, 'suspected': scan.get('suspected') if scan else None,
            'suspectedEstimated': bool(scan and scan.get('suspectedEstimated')),
            'coverageComplete': scan.get('coverageComplete') if scan else None,
            **_layers([bug for bug in bugs if bug['feedbackDate'] == key]),
        })
        day += dt.timedelta(days=1)
    trial_bugs = [bug for bug in bugs if bug['feedbackDate'] < start.isoformat()]
    trial = {'label': '试运行（回溯）', 'collected': None, 'suspected': None, **_layers(trial_bugs)} if trial_bugs else None
    # 扫描晚于看板或当日扫描失败时该反馈日未扫描；累计只合计已扫描的反馈日并注明排除项，
    # 避免一天未扫描就让整列累计显示缺失。已扫描但台账缺字段仍按缺失处理。
    scanned_rows = [row for row in rows if row['scanned']]
    unscanned = [row['date'] for row in rows if not row['scanned']]
    def total_of(key):
        values = [row[key] for row in scanned_rows]
        return sum(values) if values and all(isinstance(value, int) for value in values) else None
    in_window = [bug for bug in bugs if start.isoformat() <= bug['feedbackDate'] <= yesterday.isoformat()]
    excluded = f"，不含未扫描 {'、'.join(date[5:] for date in unscanned)}" if unscanned else ''
    # Bug 各层与「Bug 现状」同口径，包含试运行；采集与疑似只有 start 起的扫描数据。
    with_trial = f'，Bug 含试运行 {len(trial_bugs)} 个' if trial_bugs else ''
    total = {'label': f'累计（d1–d{len(rows)}{excluded}{with_trial}）', 'collected': total_of('collected'),
             'suspected': total_of('suspected'), 'suspectedEstimated': any(row['suspectedEstimated'] for row in rows),
             'unscanned': unscanned, 'scopeNote': excluded + with_trial, **_layers(trial_bugs + in_window)}
    warnings = [warning for scan in latest_scan.values() for warning in scan_warnings(scan)]
    return {'start': start.isoformat(), 'through': yesterday.isoformat(), 'rows': rows, 'trial': trial,
            'total': total, 'bugs': sorted(bugs, key=lambda bug: (bug['feedbackDate'], bug['identifier'])),
            'warnings': warnings}


# ---------- 渲染 ----------

def _num(value, scanned=True):
    if not scanned:
        return '未扫描'
    return '缺失' if value is None else str(value)


def _suspected(row, scanned=True):
    text = _num(row['suspected'], scanned)
    return f'约 {text}' if row.get('suspectedEstimated') and row['suspected'] is not None else text


def _age(since, now):
    if not since:
        return '–'
    days = (now - dt.datetime.fromisoformat(since.replace('Z', '+00:00'))).days
    return f'{days} 天' if days >= 1 else '不足 1 天'


def _pr_links(bug):
    return '、'.join(f"[{pr['repo'].split('/')[-1]}#{pr['number']}]({pr['url']})" for pr in bug['prs']) or '–'


def notify_label(bug, now):
    """Bug 明细的回告列：只有已上线的 Bug 有回告进度。"""
    if bug['state'] != 'released':
        return '–'
    if bug.get('notify') == 'pending':
        return '待回告'
    if bug.get('notify') == 'skipped':
        return f"不回告：{bug.get('notifyReason') or '原因未填'}"
    follow = bug.get('followUp') or {}
    state = follow.get('state')
    days = max(0, (_parse(follow['deadline']) - now).days) if follow.get('deadline') else None
    left = f'（剩 {days} 天）' if days is not None else ''
    if state == 'responded':
        return f"客户有回应（{_parse(follow['respondedAt']).astimezone(TZ):%m-%d}）"
    if state == 'expired' and bug['source'] == 'canny':
        return '客户无回应（已截止）'
    if state == 'expired':
        return 'CS 已告知，客户无回应（已截止）' if follow.get('csInformedAt') else 'CS 未告知客户（已截止）'
    if state == 'awaiting_customer':
        notes = ([f"票数 {follow['scoreDelta']:+d}"] if follow.get('scoreDelta') else []) + (
            [f'剩 {days} 天'] if days is not None else [])
        return '已回告，待客户回应' + (f"（{'，'.join(notes)}）" if notes else '')
    return {'untracked': '已回告（Facebook 不跟踪回应）', 'not_integrated': '已回告（回应识别未接入）',
            'unread': '已回告（会话读取失败）', 'awaiting_cs': f'已回告，待 CS 告知客户{left}',
            'cs_informed': f'CS 已告知，待客户回应{left}'}.get(state, '已回告')


def attention(board, now):
    items = {'notify': [], 'responded': [], 'merged': [], 'review': [], 'pr_closed': [], 'unrejectable': [], 'fixing': []}
    for bug in board['bugs']:
        state = bug['state']
        follow = bug.get('followUp') or {}
        if state == 'released' and bug.get('notify') == 'pending':
            items['notify'].append((bug, None))
        elif follow.get('state') == 'responded' and now - _parse(follow['respondedAt']) <= dt.timedelta(days=1):
            items['responded'].append((bug, None))
        elif state == 'merged':
            merged = max((pr['mergedAt'] for pr in bug['prs'] if pr.get('mergedAt')), default=None)
            items['merged'].append((bug, _age(merged, now)))
        elif state == 'review':
            created = min((pr['createdAt'] for pr in bug['prs'] if pr['state'] == 'open'), default=None)
            items['review'].append((bug, _age(created, now)))
        elif state in ('pr_closed', 'fixing'):
            items[state].append((bug, None))
        if not bug.get('deliveryPermalink') and state != 'rejected':
            items['unrejectable'].append((bug, None))
    return items


ATTENTION_TITLES = {
    'notify': '已上线待回告', 'responded': '已回告有新回应', 'merged': '已合并待上线', 'review': '待审 PR', 'pr_closed': 'PR 已关闭未合并且未标 🚫',
    'fixing': '已确认尚无 PR', 'unrejectable': '无交付消息，无法用 🚫 标注',
}


def attention_line(key, entries):
    return f"- {ATTENTION_TITLES[key]} {len(entries)}：" + '、'.join(
        f"[{bug['identifier']}]({bug['url']})" + (f'（{note}）' if note else '') for bug, note in entries)


def attention_notes(board, now):
    return [attention_line(key, entries) for key, entries in attention(board, now).items() if entries]


FUNNEL_HEADER = ['反馈日', '采集', '疑似候选', '确认 Bug', '不上线', '应上线', '已交付 PR', '已合并', '已上线', '已回告', '用户回应']


def funnel_cells(board, row):
    if row is board['trial']:
        intake = ['–', '–']
    else:
        scanned = row.get('scanned', True)
        intake = [_num(row['collected'], scanned), _suspected(row, scanned)]
    cells = [row['label'], *intake]
    cells += [str(row[key]) for key in ('confirmed', 'rejected', 'shouldShip', 'delivered', 'merged', 'released')]
    return cells + [str(row['notified']), str(row['responded'])]


def bug_status_text(bug):
    state = STATE_LABELS[bug['state']]
    if bug['state'] == 'released':
        tags = '、'.join(sorted({pr['release']['tag'] for pr in bug['prs'] if pr.get('release')}))
        state += f'（{tags}）'
    if bug['state'] == 'rejected':
        state += f"：{bug['rejection'].get('reason') or '原因未填'}"
    return state


def render_markdown(board, now):
    header = FUNNEL_HEADER
    lines = [f"## 每日 Bug 值守看板｜截至 {board['through']} 反馈日", '',
             f"生成时间：{now.astimezone(TZ):%Y-%m-%d %H:%M}（Asia/Shanghai）。按反馈日分组，d1 = {board['start']}。", '',
             '### 漏斗', '', '| ' + ' | '.join(header) + ' |', '|' + '---|' * len(header)]
    def row_line(row, bold=False):
        cells = funnel_cells(board, row)
        if bold:
            cells = [f'**{cell}**' for cell in cells]
        return '| ' + ' | '.join(cells) + ' |'
    if board['trial']:
        lines.append(row_line(board['trial']))
    lines += [row_line(row) for row in board['rows']]
    lines.append(row_line(board['total'], bold=True))
    sources = [f"{row['label']}：" + '、'.join(f'{k} {v}' for k, v in row['collectedBySource'].items())
               + ('' if row['coverageComplete'] else '（来源覆盖不完整）')
               for row in board['rows'] if isinstance(row.get('collectedBySource'), dict)]
    lines += ['', '来源拆分：' + ('；'.join(sources) or '–'),
              '', '### Bug 明细', '',
              '| Bug | 反馈日 | 来源 | 问题 | PR | 状态 | 回告 |', '|---|---|---|---|---|---|---|']
    for bug in board['bugs']:
        state = bug_status_text(bug)
        source = f"[{bug['source']}]({bug['sourceUrl']})" if bug.get('sourceUrl') else bug['source']
        lines.append(f"| [{bug['identifier']}]({bug['url']}) | {bug['feedbackDate']} | {source} | "
                     f"{bug['summary']} | {_pr_links(bug)} | {state} | {notify_label(bug, now)} |")
    lines += ['', '### 需要关注', ''] + (attention_notes(board, now) or ['- 无'])
    lines += ['', '### 数据说明', ''] + [f'- {warning}' for warning in board['warnings']] + [
        '- 已上线：PR 合并提交已包含在「Release to PRODUCTION」成功运行的 release 标签中；无该流程的仓库合并即视为就绪。',
        '- 不上线：审查人在 Bug 交付 thread 上点 🚫；原因取其以 🚫 开头的最新回复。',
        f'- 已回告：台账有发送记录；用户回应：Intercom 须 CS 回复确实告知客户已修复上线后，客户再有消息才算。'
        f'Canny 由看板 Agent 按语义判断回告后的新评论是否为客户回应，票数变化只作参考。从回告起跟踪 {FOLLOW_UP_DAYS} 天；Facebook 不跟踪回应。']
    return '\n'.join(lines) + '\n'


# Slack 版按状态分组；已上线、不上线会持续累积，只列前几个编号。
STATUS_ORDER = ('released', 'merged', 'review', 'pr_closed', 'fixing', 'rejected')
STATUS_LIST_LIMIT = {'released': 5, 'rejected': 5}


def status_lines(board, now):
    items = attention(board, now)
    ages = {bug['identifier']: age for key in ('merged', 'review') for bug, age in items[key] if age}
    lines = []
    for state in STATUS_ORDER:
        bugs = [bug for bug in board['bugs'] if bug['state'] == state]
        if not bugs:
            continue
        limit = STATUS_LIST_LIMIT.get(state, len(bugs))
        names = [f"[{bug['identifier']}]({bug['url']})" + (f"（{ages[bug['identifier']]}）" if bug['identifier'] in ages else '')
                 for bug in bugs[-limit:]]
        more = f' 等 {len(bugs)} 个' if len(bugs) > limit else ''
        lines.append(f"- {STATE_LABELS[state]} {len(bugs)}：{'、'.join(names)}{more}")
    return lines


def render_slack(board, now, board_link=None):
    def intake(row):
        suspected = _suspected(row)
        gap = '' if suspected.startswith('约') else ' '
        return f"采集 {_num(row['collected'])} 条 · 疑似{gap}{suspected} 条 · 确认 Bug {row['confirmed']} 个"
    last = board['rows'][-1] if board['rows'] else None
    lines = [f"**📊 每日 Bug 值守看板｜截至 {board['through'][5:]}**", '']
    if last:
        lines += [f"**昨日反馈（{last['date'][5:]}）**：" + (intake(last) if last['scanned'] else '未扫描'), '']
    trial = board['trial']['confirmed'] if board['trial'] else 0
    lines.append(f"**Bug 现状**（共 {len(board['bugs'])} 个" + (f"，含试运行 {trial} 个）" if trial else '）'))
    lines += status_lines(board, now) or ['- 暂无']
    items = attention(board, now)
    for key in ('notify', 'responded'):
        if items[key]:
            lines.append(attention_line(key, items[key]))
    total = board['total']
    lines += ['', f"**累计（{board['start'][5:]} 起{total['scopeNote']}）**：{intake(total)} · 已上线 {total['released']} 个"
              f" · 已回告 {total['notified']} 个 · 客户回应 {total['responded']} 个"]
    unrejectable = items['unrejectable']
    notes = [f'- {warning}' for warning in board['warnings']]
    if unrejectable:
        notes.append(f"- {ATTENTION_TITLES['unrejectable']}：" + '、'.join(bug['identifier'] for bug, _ in unrejectable))
    if notes:
        lines += ['', '**⚠️ 数据提醒**'] + notes
    if board_link:
        lines += ['', f'完整看板：[Multica 常驻任务]({board_link})']
    return '\n'.join(lines) + '\n'


# ---------- 数据读取 ----------

def run_json(command):
    return json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)


def load_issues(project):
    issues, offset = [], 0
    while True:
        page = run_json(['multica', 'issue', 'list', '--project', project, '--limit', '100',
                         '--offset', str(offset), '--output', 'json'])
        issues += page['issues']
        if not page.get('has_more'):
            return issues
        offset += len(page['issues'])


def _meta(issue, key):
    value = (issue.get('metadata') or {}).get(key)
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return None


class GitHub:
    def __init__(self):
        self.prod_repos, self.runs = {}, {}

    def api(self, path):
        return run_json(['gh', 'api', path])

    def deployable(self, repo):
        if repo not in self.prod_repos:
            workflows = self.api(f'repos/{repo}/actions/workflows?per_page=100')['workflows']
            self.prod_repos[repo] = any(item['name'] == PROD_WORKFLOW for item in workflows)
        return self.prod_repos[repo]

    def contains(self, repo, number):
        def check(base, head):
            return compare_contains(self.api(f'repos/{repo}/compare/{base}...{head}'), number)
        return check

    def release_runs(self, repo, since):
        key = (repo, since)
        if key not in self.runs:
            runs, page = [], 1
            while True:
                data = self.api(f'repos/{repo}/actions/runs?event=push&status=success&per_page=100'
                                f'&page={page}&created=%3E%3D{since}')
                runs += [run for run in data['workflow_runs'] if run['name'] == PROD_WORKFLOW]
                if len(data['workflow_runs']) < 100:
                    break
                page += 1
            self.runs[key] = runs
        return self.runs[key]

    def pr(self, url):
        repo, number = PR_URL.match(url).groups()
        data = self.api(f'repos/{repo}/pulls/{number}')
        state = 'merged' if data.get('merged_at') else data['state']
        pr = {'url': url, 'repo': repo, 'number': int(number), 'state': state, 'createdAt': data['created_at'],
              'mergedAt': data.get('merged_at'), 'deployable': self.deployable(repo), 'release': None}
        if state == 'merged' and pr['deployable']:
            runs = self.release_runs(repo, data['merged_at'])
            pr['release'] = find_release(data['merge_commit_sha'], runs, self.contains(repo, number))
        return pr


def slack_thread(cli, permalink):
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / 'thread.json'
        subprocess.run([sys.executable, cli, 'replies', '--url', permalink, '--output', str(output)],
                       check=True, capture_output=True, text=True)
        return json.loads(output.read_text())['messages']


def intercom_parts(cli, url):
    """经 intercom skill 读取会话消息（客户、客服、机器人消息的类型、时间与正文）；读取失败返回 None。"""
    if not url:
        return None
    try:
        return run_json(['uv', 'run', '-q', '--script', cli, 'get-conversation', url])['parts']
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return None


def canny_activity(cli, url, account_ref):
    """经 moe-feedback-collector 只读读取 Canny 帖子票数与评论（含正文，供看板 Agent 判断）；读取失败返回 None。"""
    if not url:
        return None
    command = ['uv', 'run', '-q', '--script', cli, 'canny-post', '--url', url, '--include-text']
    try:
        return run_json(command + (['--account-ref', account_ref] if account_ref else []))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def follow_up_tasks(bugs):
    """取出看板 Agent 要处理的事项：待语义判断的 Intercom CS 回复或 Canny 新评论、待写入台账的跟踪记录。
    会话原文只留在这里，不进入 Bug 明细。"""
    judge, record = [], []
    for bug in bugs:
        follow = bug.get('followUp') or {}
        candidates, base = follow.pop('candidates', None), follow.pop('record', None)
        if candidates:
            judge.append({'issue': bug['identifier'], 'source': bug['source'], 'summary': bug['summary'],
                          'sourceUrl': bug['sourceUrl'], 'record': base, 'candidates': candidates})
        if 'proposal' in follow:
            record.append(follow.pop('proposal'))
    return {'judge': judge, 'record': record}


def collect(args, now):
    scans, bugs = [], []
    github = GitHub()
    start = dt.date.fromisoformat(args.start)
    ledger, warnings = ledger_entries(run_json(['multica', 'issue', 'comment', 'list', args.ledger_issue,
                                                '--full', '--compact', '--output', 'json']))
    for issue in load_issues(args.project):
        is_bug = (_meta(issue, 'dailyBug') or {}).get('mode') == 'bug'
        is_scan = bool(_meta(issue, 'dailyScan')) and issue['created_at'][:10] >= start.isoformat()
        if not (is_bug or is_scan):
            continue
        board = ledger.get((issue['identifier'], 'bug' if is_bug else 'scan'))
        if board is None:
            warnings.append(f"{issue['identifier']} 在字段台账中没有记录，未计入看板")
            continue
        if not is_bug:
            scans.append({**board, 'identifier': issue['identifier'], 'createdAt': issue['created_at']})
            continue
        bug = {'identifier': issue['identifier'], 'url': args.issue_url_base + issue['identifier'],
               'summary': issue['title'].split('｜')[-1], 'feedbackDate': board['feedbackDate'],
               'source': board.get('source'), 'sourceUrl': board.get('sourceUrl'),
               'deliveryPermalink': (board.get('delivery') or {}).get('permalink'), 'rejection': None, 'notify': None}
        bug['prs'] = [github.pr(url) for url in board.get('prs') or []]
        if bug['deliveryPermalink']:
            bug['rejection'] = rejection(slack_thread(args.slack_cli, bug['deliveryPermalink']), args.reviewer)
        bug['state'] = bug_state(bug['prs'], bug['rejection'] is not None)
        if bug['state'] == 'released':
            notify = ledger.get((issue['identifier'], 'notify'))
            bug['notify'] = notify_state(notify)
            if bug['notify'] == 'skipped':
                bug['notifyReason'] = notify.get('reason')
            if bug['notify'] == 'sent':
                def read():
                    if bug['source'] == 'canny':
                        return canny_activity(args.collector_cli, bug['sourceUrl'], args.canny_account_ref)
                    return intercom_parts(args.intercom_cli, bug['sourceUrl'])
                bug['followUp'] = follow_up(bug['source'], notify, ledger.get((issue['identifier'], 'notify-followup')),
                                            read, now)
                if bug['followUp']['state'] == 'unread':
                    warnings.append(f"{issue['identifier']} {bug['source']} 来源读取失败，回应状态未更新")
        bugs.append(bug)
    return scans, bugs, warnings


# ---------- HTML 版：Slack thread 附件 ----------

# 扫描任务以固定文件名上传证据索引，单独作为附件或放在证据 zip 内。
SCAN_INDEX_NAME = 'scan-evidence-index.json'
SCAN_INDEX_MAX_BYTES = 20 * 1024 * 1024
# 顺序即页面展示顺序：需要处理的在前，非 Bug 在后。
RECORD_STATES = {
    'confirmed': '确认可修', 'verification_blocked': '待验证/验证受阻', 'duplicate': '重复',
    'cs_followed': '已有工单跟进', 'excluded_after_investigation': '调查后排除',
    'product_decision': '产品/设计诉求', 'non_bug': '普通支持',
}
RECORD_DETAILS = (('unresolvedGap', '待确认缺口'), ('resumeCondition', '恢复条件'),
                  ('correctBehaviorBasis', '正确行为依据'), ('observation', '动态观察'),
                  ('verificationMethod', '验证方式'))
TICKET_KEY = re.compile(r'[A-Z][A-Z0-9]{1,9}-\d+')


def _https(value):
    return value if isinstance(value, str) and re.match(r'https?://', value) else ''


def _clip(value, limit):
    return value[:limit] if isinstance(value, str) else ''


def _attempt_text(attempt):
    """结构化尝试只取路径与结果；查询语句可能带商家 ID，不进入页面。"""
    if isinstance(attempt, str):
        return _clip(attempt, 600)
    if not isinstance(attempt, dict):
        return ''
    parts = [attempt.get('path') or attempt.get('method'), attempt.get('result')]
    return _clip('：'.join(str(part) for part in parts if part), 600)


def scan_records(index):
    """证据索引 → 页面逐条记录。只保留排查与判断字段，不带商家 ID、客户分层与查询语句。"""
    records = []
    for record in index.get('records') or []:
        if not isinstance(record, dict):
            continue
        tickets = [key for key in (record.get('csTickets') or []) + (record.get('verifiedJiraKeys') or [])
                   if isinstance(key, str) and TICKET_KEY.fullmatch(key)]
        records.append({
            'state': _clip(record.get('state'), 60), 'source': _clip(record.get('source'), 30),
            'summary': _clip(record.get('summary') or record.get('title') or record.get('evidenceId'), 240),
            'reason': _clip(record.get('reason'), 600), 'sourceUrl': _https(record.get('sourceUrl')),
            'details': [[label, _clip(record[key], 600)] for key, label in RECORD_DETAILS
                        if isinstance(record.get(key), str) and record[key]],
            'attempts': [text for text in map(_attempt_text, (record.get('attempts') or [])[:12]) if text],
            'tickets': list(dict.fromkeys(tickets)),
            'pr': _https(record.get('relatedPR') or record.get('pr')),
        })
    rank = {state: index for index, state in enumerate(RECORD_STATES)}
    return sorted(records, key=lambda record: rank.get(record['state'], len(rank)))


def read_scan_index(path):
    if path.name == SCAN_INDEX_NAME:
        return json.loads(path.read_text())
    with zipfile.ZipFile(path) as archive:
        # 只读取同名索引，不解压其它文件；超限的成员视为不可用。
        members = [member for member in archive.infolist()
                   if Path(member.filename).name == SCAN_INDEX_NAME and member.file_size <= SCAN_INDEX_MAX_BYTES]
        return json.loads(archive.read(members[0])) if members else None


def load_scan_index(identifier):
    """取扫描任务最新上传的证据索引；找不到返回 None。"""
    comments = run_json(['multica', 'issue', 'comment', 'list', identifier, '--output', 'json'])
    comments = comments if isinstance(comments, list) else comments.get('comments', [])
    attachments = [attachment for comment in sorted(comments, key=lambda item: item['created_at'], reverse=True)
                   for attachment in comment.get('attachments') or []]
    with tempfile.TemporaryDirectory() as directory:
        for attachment in attachments:
            name = attachment.get('filename') or ''
            if name != SCAN_INDEX_NAME and not name.endswith('.zip'):
                continue
            subprocess.run(['multica', 'attachment', 'download', attachment['id'], '-o', directory],
                           check=True, capture_output=True, text=True)
            index = read_scan_index(Path(directory) / name)
            if index is not None:
                return index
    return None


def yesterday_scan(board, scans):
    """昨日反馈日最新一次扫描的逐条记录；没有扫描或索引不可用时返回原因供页面说明。"""
    candidates = [scan for scan in scans if scan.get('feedbackDate') == board['through']]
    if not candidates:
        return None, f"{board['through']} 反馈日尚未扫描，暂无逐条记录。"
    scan = max(candidates, key=lambda item: item['createdAt'])
    try:
        index = load_scan_index(scan['identifier'])
    except (subprocess.CalledProcessError, OSError, ValueError, zipfile.BadZipFile):
        index = None
    if not index or index.get('targetDate') not in (None, board['through']):
        return None, f"{scan['identifier']} 没有可用的证据索引（{SCAN_INDEX_NAME}），暂无逐条记录。"
    return {'identifier': scan['identifier'], 'records': scan_records(index)}, None


def html_payload(board, now, scan, scan_note, board_link=None, issue_url_base=''):
    rows = ([board['trial']] if board['trial'] else []) + board['rows'] + [board['total']]
    bugs = [{
        'identifier': bug['identifier'], 'url': _https(bug['url']), 'summary': bug['summary'],
        'feedbackDate': bug['feedbackDate'], 'source': bug.get('source') or '', 'sourceUrl': _https(bug.get('sourceUrl')),
        'prs': [{'label': f"{pr['repo'].split('/')[-1]}#{pr['number']}", 'url': _https(pr['url'])} for pr in bug['prs']],
        'state': bug['state'], 'status': bug_status_text(bug), 'notify': notify_label(bug, now),
    } for bug in board['bugs']]
    attention_groups = [{'title': ATTENTION_TITLES[key], 'items': [
        {'identifier': bug['identifier'], 'url': _https(bug['url']), 'note': note or ''} for bug, note in entries]}
        for key, entries in attention(board, now).items() if entries]
    return {
        'through': board['through'], 'start': board['start'],
        'generatedAt': f'{now.astimezone(TZ):%Y-%m-%d %H:%M}', 'boardLink': _https(board_link),
        'header': FUNNEL_HEADER, 'funnel': [funnel_cells(board, row) for row in rows],
        'latest': board['rows'][-1] if board['rows'] else None, 'total': board['total'],
        'bugs': bugs, 'attention': attention_groups, 'warnings': board['warnings'],
        'scan': scan and {**scan, 'url': _https(issue_url_base + scan['identifier'])},
        'scanNote': scan_note, 'stateNames': RECORD_STATES,
    }


def _h(value):
    return escape(str(value), quote=True)


def _a(text, url):
    url = _https(url)
    return f'<a href="{_h(url)}" target="_blank" rel="noopener">{_h(text)}</a>' if url else _h(text)


def _html_kpis(payload):
    latest, total = payload['latest'], payload['total']
    if not latest:
        return ''
    scanned = latest.get('scanned', True)
    suspected = _suspected(latest, scanned)
    cards = [('昨日采集', _num(latest['collected'], scanned), f"{latest['date'][5:]} 反馈日"),
             ('昨日疑似候选', suspected, '需要调查的反馈'), ('昨日确认 Bug', latest['confirmed'], '进入修复'),
             ('累计确认 Bug', total['confirmed'], f"已上线 {total['released']} · 已回告 {total['notified']}")]
    return '<div class="kpis">' + ''.join(
        f'<div class="kpi"><div class="label">{_h(label)}</div><div class="value">{_h(value)}</div>'
        f'<div class="hint">{_h(hint)}</div></div>' for label, value, hint in cards) + '</div>'


def _html_attention(payload):
    items = ['<li>' + _h(f"{group['title']} {len(group['items'])}：") + '、'.join(
        _a(item['identifier'], item['url']) + (_h(f"（{item['note']}）") if item['note'] else '')
        for item in group['items']) + '</li>' for group in payload['attention']]
    items += [f'<li>{_h(text)}</li>' for text in payload['warnings']]
    return f'<section><h2>需要关注</h2><ul class="plain">{"".join(items)}</ul></section>' if items else ''


def _html_table(header, rows, last_bold=False):
    head = ''.join(f'<th>{_h(text)}</th>' for text in header)
    body = ''.join(f'<tr class="{"total" if last_bold and index == len(rows) - 1 else ""}">{row}</tr>'
                   for index, row in enumerate(rows))
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _html_bugs(payload):
    rows = [''.join([
        f"<td>{_a(bug['identifier'], bug['url'])}</td>", f"<td>{_h(bug['feedbackDate'])}</td>",
        f"<td>{_a(bug['source'] or '–', bug['sourceUrl'])}</td>", f"<td class=\"wrap\">{_h(bug['summary'])}</td>",
        '<td class="wrap">' + ('、'.join(_a(pr['label'], pr['url']) for pr in bug['prs']) or '–') + '</td>',
        f"<td class=\"wrap\"><span class=\"badge s-{_h(bug['state'])}\">{_h(bug['status'])}</span></td>",
        f"<td class=\"wrap\">{_h(bug['notify'])}</td>"]) for bug in payload['bugs']]
    table = _html_table(['Bug', '反馈日', '来源', '问题', 'PR', '状态', '回告'], rows) if rows else '<p class="empty">暂无 Bug</p>'
    return f'<section><h2>Bug 明细</h2>{table}</section>'


def _html_record(record, names):
    tickets = ''.join(f'<span class="badge">{_h(ticket)}</span>' for ticket in record['tickets'])
    links = (_a('原反馈', record['sourceUrl']) if record['sourceUrl'] else '') + (_a('关联 PR', record['pr']) if record['pr'] else '')
    meta = (f'<div class="meta"><span class="badge">{_h(names.get(record["state"], record["state"]))}</span>'
            f'<span>{_h(record["source"])}</span>{links}{tickets}</div>')
    head = meta + f'<div class="title">{_h(record["summary"])}</div>' + (
        f'<div class="reason">{_h(record["reason"])}</div>' if record['reason'] else '')
    body = ''.join(f'<div><b>{_h(label)}</b>{_h(text)}</div>' for label, text in record['details'])
    if record['attempts']:
        body += '<div><b>已做尝试</b><ul class="plain">' + ''.join(
            f'<li>{_h(text)}</li>' for text in record['attempts']) + '</ul></div>'
    search = ' '.join([record['summary'], record['reason'], *record['tickets']]).lower()
    attrs = f'class="record" data-state="{_h(record["state"])}" data-source="{_h(record["source"])}" data-search="{_h(search)}"'
    if body:
        return f'<details {attrs}><summary>{head}</summary><div class="detail">{body}</div></details>'
    return f'<div {attrs}>{head}</div>'


def _html_scan(payload):
    title = f"<h2>昨日逐条记录（{_h(payload['through'][5:])} 反馈日）</h2>"
    scan = payload['scan']
    if not scan:
        return f'<section>{title}<p class="empty">{_h(payload["scanNote"] or "暂无逐条记录。")}</p></section>'
    records, names = scan['records'], payload['stateNames']
    counts = {}
    for record in records:
        counts[record['state']] = counts.get(record['state'], 0) + 1
    order = list(names) + [state for state in counts if state not in names]
    bars = ''.join(
        f'<div class="bar" data-key="{_h(state)}"><span>{_h(names.get(state, state))}</span>'
        f'<div class="track"><div class="fill" style="width:{counts[state] / len(records) * 100:.1f}%"></div></div>'
        f'<span>{counts[state]}</span></div>' for state in order if counts.get(state))
    sources = ''.join(f'<option value="{_h(source)}">{_h(source)}</option>'
                      for source in sorted({record['source'] for record in records}))
    # 筛选、搜索与分页在脚本可用时才出现；预览禁用脚本时直接展示全部记录。
    toolbar = ('<div class="toolbar js-only"><input type="search" id="q" placeholder="搜索摘要、原因或工单号">'
               f'<select id="src"><option value="">全部来源</option>{sources}</select></div>')
    intro = (f'<div class="sub">共 {len(records)} 条，来自 {_a(scan["identifier"], scan["url"])}。'
             '待验证不等于没有 Bug；点击记录展开判断依据。</div>')
    list_html = ''.join(_html_record(record, names) for record in records)
    return (f'<section id="scan">{title}{intro}<div class="bars">{bars}</div>{toolbar}'
            f'<div id="records">{list_html}</div><p class="empty" id="none" hidden>没有匹配的记录。</p>'
            '<div class="pager js-only" id="pager"></div></section>')


def render_html(payload):
    """静态生成全部内容，不依赖脚本即可阅读；脚本只增加筛选、搜索与分页。"""
    title = f"每日 Bug 值守看板｜截至 {payload['through'][5:]}"
    board_link = f" · {_a('Multica 常驻看板', payload['boardLink'])}" if payload['boardLink'] else ''
    funnel_rows = [''.join(f'<td>{_h(cell)}</td>' for cell in cells) for cells in payload['funnel']]
    body = ''.join([
        f'<header><h1>{_h(title)}</h1><div class="sub">生成时间 {_h(payload["generatedAt"])}（Asia/Shanghai）'
        f' · d1 = {_h(payload["start"])}{board_link}</div></header>',
        _html_kpis(payload), _html_attention(payload),
        f'<section><h2>漏斗</h2>{_html_table(payload["header"], funnel_rows, last_bold=True)}</section>',
        _html_bugs(payload), _html_scan(payload)])
    return HTML_TEMPLATE.replace('__TITLE__', _h(title)).replace('__BODY__', body)


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--soft:#eef2ff;
--green:#16a34a;--amber:#d97706;--red:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--text:#e5e7eb;--muted:#9aa1ad;--line:#2b303a;
--accent:#60a5fa;--soft:#1e2a44}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--muted);font-size:13px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi .label{color:var(--muted);font-size:12px}.kpi .value{font-size:26px;font-weight:600;margin-top:2px}
.kpi .hint{color:var(--muted);font-size:12px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
td.wrap{white-space:normal;min-width:180px}
th{color:var(--muted);font-weight:500}
tr.total td{font-weight:600}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--soft)}
.s-released{color:var(--green)}.s-merged,.s-review{color:var(--amber)}.s-rejected,.s-pr_closed{color:var(--red)}
.bars{display:flex;flex-direction:column;gap:6px;margin:12px 0}
.bar{display:grid;grid-template-columns:130px 1fr 56px;gap:10px;align-items:center;border-radius:6px;padding:2px 4px}
.js .bar{cursor:pointer}.js .bar:hover,.bar.on{background:var(--soft)}
.track{height:10px;background:var(--line);border-radius:999px;overflow:hidden}
.fill{height:100%;background:var(--accent)}
.js-only{display:none}.js .js-only{display:flex}
.toolbar{flex-wrap:wrap;gap:8px;margin-bottom:10px}
input,select,button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
input{flex:1;min-width:200px}button{cursor:pointer}button:disabled{opacity:.4;cursor:default}
.record{border-top:1px solid var(--line);padding:10px 2px}
.record summary{cursor:pointer;list-style:none}.record summary::-webkit-details-marker{display:none}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:var(--muted)}
.title{font-weight:500;margin:2px 0}
.reason{color:var(--muted);font-size:13px}
.detail{margin:8px 0 0 0;padding-left:12px;border-left:2px solid var(--line);font-size:13px}
.detail div{margin:4px 0}.detail b{color:var(--muted);font-weight:500;margin-right:6px}
.pager{gap:8px;align-items:center;justify-content:flex-end;margin-top:10px;color:var(--muted)}
ul.plain{margin:0;padding-left:18px}
.empty{color:var(--muted)}
</style>
</head>
<body>
<main>__BODY__</main>
<script>
(function () {
  var section = document.getElementById('scan');
  if (!section || !document.getElementById('records')) return;
  document.documentElement.classList.add('js');
  var records = Array.prototype.slice.call(document.querySelectorAll('#records .record'));
  var bars = Array.prototype.slice.call(section.querySelectorAll('.bar'));
  var query = document.getElementById('q'), source = document.getElementById('src');
  var pager = document.getElementById('pager'), none = document.getElementById('none');
  var state = {category: '', page: 0}, PAGE = 30;
  function draw() {
    var q = query.value.trim().toLowerCase(), s = source.value;
    var hits = records.filter(function (node) {
      return (!state.category || node.dataset.state === state.category) && (!s || node.dataset.source === s)
        && (!q || node.dataset.search.indexOf(q) >= 0);
    });
    var pages = Math.max(1, Math.ceil(hits.length / PAGE));
    state.page = Math.min(state.page, pages - 1);
    records.forEach(function (node) { node.hidden = true; });
    hits.slice(state.page * PAGE, (state.page + 1) * PAGE).forEach(function (node) { node.hidden = false; });
    none.hidden = hits.length > 0;
    bars.forEach(function (bar) { bar.classList.toggle('on', bar.dataset.key === state.category); });
    pager.textContent = hits.length + ' 条 · 第 ' + (state.page + 1) + '/' + pages + ' 页';
    [['上一页', -1, state.page === 0], ['下一页', 1, state.page >= pages - 1]].forEach(function (item) {
      var button = document.createElement('button');
      button.textContent = item[0]; button.disabled = item[2];
      button.addEventListener('click', function () { state.page += item[1]; draw(); });
      pager.appendChild(button);
    });
  }
  bars.forEach(function (bar) {
    bar.addEventListener('click', function () {
      state.category = state.category === bar.dataset.key ? '' : bar.dataset.key; state.page = 0; draw();
    });
  });
  query.addEventListener('input', function () { state.page = 0; draw(); });
  source.addEventListener('change', function () { state.page = 0; draw(); });
  draw();
})();
</script>
</body>
</html>
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='每日 Bug 值守项目 ID')
    parser.add_argument('--ledger-issue', required=True, help='看板字段台账任务 ID 或编号')
    parser.add_argument('--start', required=True, help='d1 对应的反馈日（YYYY-MM-DD）')
    parser.add_argument('--today', help='Asia/Shanghai 当天日期，默认取当前时间')
    parser.add_argument('--reviewer', required=True, help='有权用 🚫 标注不上线的 Slack 用户 ID')
    parser.add_argument('--slack-cli', default=os.path.expanduser('~/.agents/skills/slack/scripts/slack.py'))
    parser.add_argument('--intercom-cli', default=os.path.expanduser('~/.agents/skills/intercom/scripts/intercom.py'))
    parser.add_argument('--collector-cli', default=os.path.expanduser(
        '~/.agents/skills/moe-feedback-collector/scripts/moe_feedback_collector.py'))
    parser.add_argument('--canny-account-ref', default='', help='Canny 需要续登时的内部测试账号 aid:<id>')
    parser.add_argument('--issue-url-base', default='https://multica.devops.moego.dev/grm/issues/')
    parser.add_argument('--board-link', help='常驻看板任务链接，附在 Slack 版末尾')
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    today = dt.date.fromisoformat(args.today) if args.today else now.astimezone(TZ).date()
    scans, bugs, warnings = collect(args, now)
    tasks = follow_up_tasks(bugs)
    board = build_board(scans, bugs, dt.date.fromisoformat(args.start), today)
    board['warnings'] = warnings + board['warnings']
    board['followUpTasks'] = tasks
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'board.json').write_text(json.dumps(board, ensure_ascii=False, indent=2))
    (output / 'board.md').write_text(render_markdown(board, now))
    (output / 'slack.md').write_text(render_slack(board, now, args.board_link))
    scan, scan_note = yesterday_scan(board, scans)
    (output / 'board.html').write_text(
        render_html(html_payload(board, now, scan, scan_note, args.board_link, args.issue_url_base)))
    print(json.dumps({'output': str(output), 'warnings': board['warnings'],
                      'followUpTasks': {key: len(value) for key, value in tasks.items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
