#!/usr/bin/env python3
"""Build the daily Bug duty board from Multica, GitHub and Slack (read-only)."""
import argparse
import datetime as dt
import importlib.util
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

spec = importlib.util.spec_from_file_location('duty_progress', Path(__file__).with_name('duty-progress.py'))
progress_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress_module)

TZ = ZoneInfo('Asia/Shanghai')
MULTICA_CLI = ['multica', '--server-url', 'https://multica.devops.moego.dev',
               '--workspace-id', '1b6d114a-8407-4ba8-adea-d31209caabe5']
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
    'rejected': '⛔ 不上线', 'released': '🟢 已上线',
    'review': '⚪ 待审查', 'pr_closed': '⚠️ PR 已关闭未合并', 'fixing': '🔧 修复中', 'blocked': '🚧 受阻',
}


# ---------- 纯逻辑：上线、🚫、漏斗 ----------

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


def bug_state(prs, rejected, blocked=False):
    # 上线按关联 PR 全部 merged 判断；历史驳回标记不覆盖实际合并结果。
    if prs and all(pr['state'] == 'merged' for pr in prs):
        return 'released'
    if blocked:
        return 'blocked'
    if rejected:
        return 'rejected'
    if not prs:
        return 'fixing'
    if any(pr['state'] == 'closed' for pr in prs):
        return 'pr_closed'
    return 'review'


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
    alive = [bug for bug in bugs if bug['state'] not in {'rejected', 'blocked'}]
    return {
        'confirmed': len(bugs),
        'blocked': sum(1 for bug in bugs if bug['state'] == 'blocked'),
        'rejected': sum(1 for bug in bugs if bug['state'] == 'rejected'),
        'shouldShip': len(alive),
        'delivered': sum(1 for bug in bugs if bug['prs'] and bug['state'] != 'blocked'),
        'merged': sum(1 for bug in alive if bug['state'] == 'released'),
        'released': sum(1 for bug in alive if bug['state'] == 'released'),
        'notified': sum(1 for bug in alive if bug.get('notify') == 'sent'),
        'responded': sum(1 for bug in alive if (bug.get('followUp') or {}).get('state') == 'responded'),
    }


def build_board(scans, bugs, start, today):
    """按所属扫描批次的反馈日归组，展示扫描日；范围外 Bug 不进入明细或累计。"""
    yesterday = today - dt.timedelta(days=1)
    bugs = [bug for bug in bugs if start.isoformat() <= bug['feedbackDate'] <= yesterday.isoformat()]
    latest_scan = {}
    for scan in sorted(scans, key=lambda item: item['createdAt']):
        latest_scan[scan['feedbackDate']] = scan
    rows, day = [], start
    while day <= yesterday:
        key = day.isoformat()
        scan = latest_scan.get(key)
        collected = scan.get('collected') if scan else None
        scan_day = scan.get('scanDate') if scan else None
        period = f'{day:%m-%d} 反馈'
        if scan_day:
            period = f'{scan_day[5:]} 扫描（{period}）'
        label = f'd{(day - start).days + 1} · {period}'
        rows.append({
            'label': label, 'date': key, 'scanDate': scan_day, 'scanned': scan is not None,
            'collected': sum(collected.values()) if isinstance(collected, dict) else None,
            'collectedBySource': collected, 'suspected': scan.get('suspected') if scan else None,
            'suspectedEstimated': bool(scan and scan.get('suspectedEstimated')),
            'coverageComplete': scan.get('coverageComplete') if scan else None,
            **_layers([bug for bug in bugs if bug['feedbackDate'] == key]),
            'progress': scan.get('progress') if scan else None,
        })
        # 已确认但尚未登记 Bug 的调查也属于阶段结论；建任务后按调查 ID 去重。
        confirmed = ((scan or {}).get('progress') or {}).get('investigation', {}).get('confirmedIssueIds', [])
        detailed = {bug.get('investigationIssueId') for bug in bugs if bug['feedbackDate'] == key}
        extra = len(set(confirmed) - detailed)
        rows[-1]['confirmedWithoutDetail'] = extra
        rows[-1]['confirmed'] += extra
        rows[-1]['shouldShip'] += extra
        day += dt.timedelta(days=1)
    # 扫描晚于看板或当日扫描失败时该反馈日未扫描；累计只合计已扫描的反馈日并注明排除项，
    # 避免一天未扫描就让整列累计显示缺失。已扫描但台账缺字段仍按缺失处理。
    scanned_rows = [row for row in rows if row['scanned']]
    unscanned = [row['date'] for row in rows if not row['scanned']]
    def total_of(key):
        values = [row[key] for row in scanned_rows]
        return sum(values) if values and all(isinstance(value, int) for value in values) else None
    excluded = f"，不含未扫描 {'、'.join(date[5:] for date in unscanned)}" if unscanned else ''
    total = {'label': f'累计（d1–d{len(rows)}{excluded}）', 'collected': total_of('collected'),
             'suspected': total_of('suspected'), 'suspectedEstimated': any(row['suspectedEstimated'] for row in rows),
             'unscanned': unscanned, 'scopeNote': excluded, **_layers(bugs)}
    extra = sum(row['confirmedWithoutDetail'] for row in rows)
    total.update(confirmed=total['confirmed'] + extra, shouldShip=total['shouldShip'] + extra,
                 confirmedWithoutDetail=extra)
    warnings = [warning for scan in latest_scan.values() for warning in scan_warnings(scan)]
    return {'start': start.isoformat(), 'through': yesterday.isoformat(), 'rows': rows, 'trial': None,
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
    if bug['state'] == 'blocked':
        return '未交付（历史 PR 见任务）' if bug['prs'] else '未交付'
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
    items = {'notify': [], 'responded': [], 'review': [], 'pr_closed': [], 'unrejectable': [], 'fixing': []}
    for bug in board['bugs']:
        state = bug['state']
        follow = bug.get('followUp') or {}
        if state == 'released' and bug.get('notify') == 'pending':
            items['notify'].append((bug, None))
        elif follow.get('state') == 'responded' and now - _parse(follow['respondedAt']) <= dt.timedelta(days=1):
            items['responded'].append((bug, None))
        elif state == 'review':
            created = min((pr['createdAt'] for pr in bug['prs'] if pr['state'] == 'open'), default=None)
            items['review'].append((bug, _age(created, now)))
        elif state in ('pr_closed', 'fixing'):
            items[state].append((bug, None))
        # 只提醒已有 PR 或已交付却缺交付链接的真实台账缺口；排队、修复中尚未发消息属正常。
        if (not bug.get('deliveryPermalink') and state not in {'rejected', 'blocked'}
                and (bug.get('prs') or bug.get('delivered'))):
            items['unrejectable'].append((bug, None))
    return items


ATTENTION_TITLES = {
    'notify': '已上线待回告', 'responded': '已回告有新回应', 'review': '待审 PR', 'pr_closed': 'PR 已关闭未合并且未标 🚫',
    'fixing': '已确认尚无 PR', 'unrejectable': '无交付消息，无法用 🚫 标注',
}


def attention_line(key, entries):
    return f"- {ATTENTION_TITLES[key]} {len(entries)}：" + '、'.join(
        f"[{bug['identifier']}]({bug['url']})" + (f'（{note}）' if note else '') for bug, note in entries)


def attention_notes(board, now):
    return [attention_line(key, entries) for key, entries in attention(board, now).items() if entries]


FUNNEL_HEADER = ['扫描日（反馈日）', '采集', '疑似候选', '确认可修', '受阻', '不上线', '应上线', '已交付 PR', '已合并', '已上线', '已回告', '用户回应']


def funnel_cells(board, row):
    scanned = row.get('scanned', True)
    intake = [_num(row['collected'], scanned), _suspected(row, scanned)]
    cells = [row['label'], *intake]
    cells += [str(row[key]) for key in ('confirmed', 'blocked', 'rejected', 'shouldShip', 'delivered', 'merged', 'released')]
    return cells + [str(row['notified']), str(row['responded'])]


def bug_status_text(bug):
    state = STATE_LABELS[bug['state']]
    if bug['state'] == 'rejected':
        state += f"：{(bug.get('rejection') or {}).get('reason') or '原因未填'}"
    elif bug['state'] == 'blocked':
        state += f"：{bug.get('blockedReason') or '原因未填'}"
    return state


def progress_rows(progress):
    """三种输出共享同一阶段文案；完整交付只计经过调度器回执校验的版本。"""
    collected, inv, fix = (progress[key] for key in ('collection', 'investigation', 'repair'))
    source = '、'.join(f'{key.capitalize()} {value}' for key, value in (collected.get('collected') or {}).items())
    known = collected.get('collected')
    partial = f"已采集 {sum(known.values())} 条（{source}）；" if known is not None else ''
    collection_text = {'completed': f"已完成 {_num(collected.get('total'))} 条（{source}）",
                       'collecting': partial + '采集或覆盖核对进行中',
                       'incomplete': partial + '来源覆盖不完整，保留已采集结果',
                       'attention': '阶段数据待核对'}[collected['state']]
    if inv['state'] in {'investigating', 'completed'}:
        inv_text = (f"已留档 {inv['completed']}/{inv['total']} 个问题 · 确认可修 {inv['confirmed']}"
                    f" · 重复 {inv['duplicate']} · 已有工单 {inv['csFollowed']} · 排除 {inv['excluded']}")
        if inv.get('skippedFailure'):
            inv_text += f" · 执行失败跳过 {inv['skippedFailure']}（未确认）"
    else:
        inv_text = {'registering': '本轮清单登记中', 'legacy': '历史记录未保存逐条调查进度',
                    'attention': '证据或清单待核对，完成数未知'}[inv['state']]
    if fix['state'] in {'repairing', 'completed', 'completed_with_skips', 'completed_with_blocked',
                        'blocked', 'paused', 'cancelled'}:
        fix_text = f"完整交付 {fix['completed']}/{fix['total']}"
        if fix['state'] == 'repairing':
            fix_text += f" · 固定队列第 {fix.get('index')}/{fix['total']} 项"
        elif fix['state'] == 'completed_with_skips':
            fix_text += ' · 已结束，含真人授权跳过项'
        elif fix['state'] == 'completed':
            fix_text += ' · 工程交付已结束'
        elif fix['state'] == 'completed_with_blocked':
            fix_text += ' · 固定队列已结束'
        else:
            fix_text += ' · ' + {'blocked': '修复受阻，详见当前任务', 'paused': '已暂停', 'cancelled': '已取消'}[fix['state']]
        if fix.get('blocked'):
            # 受阻回执是历史工程事实，当前是否已上线由 PR 状态判断，不能等同于不上线数。
            fix_text += f" · 受阻留档 {fix['blocked']} 项"
    else:
        fix_text = {'not_started': '尚未开始，等待全轮调查与汇总', 'preparing': '固定修复清单登记中',
                    'not_needed': '本轮无新增可修项', 'attention': '交付证据或暂停现场待核对'}[fix['state']]
    return [('采集', collection_text), ('调查', inv_text), ('修复', fix_text)]


def progress_lines(progress):
    lines = [f'- **{label}**：{text}' for label, text in progress_rows(progress)]
    active = []
    for item in progress['current']:
        status = '执行中' if item['status'] in {'running', 'waiting_local_directory'} else '已排队'
        summary = item['summary'].replace('\n', ' ').replace('@', '＠')
        for token in ('\\', '[', ']', '*', '_', '<', '>'):
            summary = summary.replace(token, '\\' + token)
        active.append(f"{item['phase']}{status} [{item['identifier']}]({item['url']}) {summary}")
    current = '；'.join(active) or ('暂无在途运行' if progress['activityKnown'] else '运行状态读取失败，待核对')
    return lines + [f'- **当前处理**：{current}', f"- **下一步**：{progress['nextAction']}"]


def render_markdown(board, now):
    header = FUNNEL_HEADER
    lines = [f"## 每日 Bug 值守看板｜截至 {board['through']} 反馈日", '',
             f"生成时间：{now.astimezone(TZ):%Y-%m-%d %H:%M}（Asia/Shanghai）。按所属扫描批次归组，d1 反馈日 = {board['start']}。", '']
    progress = (board['rows'] or [{}])[-1].get('progress')
    if progress:
        lines += ['### 阶段结论与当前进度', ''] + progress_lines(progress) + ['']
    lines += ['### 漏斗', '', '| ' + ' | '.join(header) + ' |', '|' + '---|' * len(header)]
    def row_line(row, bold=False):
        cells = funnel_cells(board, row)
        if bold:
            cells = [f'**{cell}**' for cell in cells]
        return '| ' + ' | '.join(cells) + ' |'
    lines += [row_line(row) for row in board['rows']]
    lines.append(row_line(board['total'], bold=True))
    sources = [f"{row['label']}：" + '、'.join(f'{k} {v}' for k, v in row['collectedBySource'].items())
               + ('' if row['coverageComplete'] else '（来源覆盖不完整）')
               for row in board['rows'] if isinstance(row.get('collectedBySource'), dict)]
    lines += ['', '来源拆分：' + ('；'.join(sources) or '–'),
              '', '### Bug 明细', '',
              '| Bug | 扫描日 | 批次反馈日 | 来源 | 问题 | PR | 状态 | 回告 |', '|---|---|---|---|---|---|---|---|']
    for bug in board['bugs']:
        state = bug_status_text(bug)
        source = f"[{bug['source']}]({bug['sourceUrl']})" if bug.get('sourceUrl') else bug['source']
        lines.append(f"| [{bug['identifier']}]({bug['url']}) | {bug.get('scanDate') or '–'} | {bug['feedbackDate']} | {source} | "
                     f"{bug['summary']} | {_pr_links(bug)} | {state} | {notify_label(bug, now)} |")
    lines += ['', '### 需要关注', ''] + (attention_notes(board, now) or ['- 无'])
    lines += ['', '### 数据说明', ''] + [f'- {warning}' for warning in board['warnings']] + [
        '- 确认可修：按所属扫描批次统计，方案后续被驳回仍计入；范围外批次不回溯计入。',
        '- 已上线：关联 PR 全部为 merged；部分合并、待审或关闭未合并不算已上线。',
        '- 受阻：关联 PR 尚未全部合并且修复受阻；单列保留原因与证据，不计应上线或已交付 PR，不自动补验收或催办。',
        '- 不上线：审查人在 Bug 交付 thread 上点 🚫；受阻项单列，已全部合并仍按已上线统计。',
        f'- 已回告：台账有发送记录；用户回应：Intercom 须 CS 回复确实告知客户已修复上线后，客户再有消息才算。'
        f'Canny 由看板 Agent 按语义判断回告后的新评论是否为客户回应，票数变化只作参考。从回告起跟踪 {FOLLOW_UP_DAYS} 天；Facebook 不跟踪回应。']
    return '\n'.join(lines) + '\n'


# Slack 版按状态分组；已上线、不上线会持续累积，只列前几个编号。
STATUS_ORDER = ('released', 'review', 'pr_closed', 'fixing', 'blocked', 'rejected')
STATUS_LIST_LIMIT = {'released': 5, 'rejected': 5}


def status_lines(board, now):
    items = attention(board, now)
    ages = {bug['identifier']: age for bug, age in items['review'] if age}
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
        return f"采集 {_num(row['collected'])} 条 · 疑似{gap}{suspected} 条 · 确认可修 {row['confirmed']} 个"
    last = board['rows'][-1] if board['rows'] else None
    lines = [f"**📊 每日 Bug 值守看板｜截至 {board['through'][5:]}**", '']
    if last:
        lines += [f"**{last['label']}**：" + (intake(last) if last['scanned'] else '未扫描'), '']
        if last.get('progress'):
            lines += progress_lines(last['progress']) + ['']
    lines.append(f"**Bug 现状**（已进入看板 {len(board['bugs'])} 个）")
    if board['total'].get('confirmedWithoutDetail'):
        lines.append(f"- 已确认待进入修复明细 {board['total']['confirmedWithoutDetail']} 个，进度见本轮阶段快照")
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
    return json.loads(subprocess.run(multica_command(command), check=True, capture_output=True, text=True).stdout)


def multica_command(command):
    return MULTICA_CLI + command[1:] if command and command[0] == 'multica' else command


def load_issues(project):
    issues, offset, total = [], 0, None
    while True:
        page = run_json(['multica', 'issue', 'list', '--project', project, '--limit', '100',
                         '--offset', str(offset), '--sort', 'created_at', '--direction', 'asc', '--output', 'json'])
        rows = page.get('issues')
        if (not isinstance(rows, list) or type(page.get('has_more')) is not bool
                or type(page.get('total')) is not int or page.get('offset') != offset):
            raise ValueError('项目任务分页不完整，不能生成阶段快照')
        total = page['total'] if total is None else total
        if page['total'] != total:
            raise ValueError('读取期间任务数量变化，请重新生成快照')
        issues.extend(rows)
        if not page['has_more']:
            if len(issues) != total or len({row['id'] for row in issues}) != total:
                raise ValueError('项目任务总数或身份不闭合')
            return issues
        if not rows:
            raise ValueError('项目任务分页未推进')
        offset += len(rows)


def _meta(issue, key):
    value = (issue.get('metadata') or {}).get(key)
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return None


class GitHub:
    def api(self, path):
        return run_json(['gh', 'api', path])

    def pr(self, url):
        repo, number = PR_URL.match(url).groups()
        data = self.api(f'repos/{repo}/pulls/{number}')
        return {'url': url, 'repo': repo, 'number': int(number),
                'state': 'merged' if data.get('merged_at') else data['state'],
                'createdAt': data['created_at'], 'mergedAt': data.get('merged_at')}


def scan_date(issue):
    """扫描批次创建时间按北京时间取日期，不使用 Bug 创建或客户原反馈时间。"""
    return dt.datetime.fromisoformat(issue['created_at'].replace('Z', '+00:00')).astimezone(TZ).date().isoformat()


def bug_cohort(issue, issues_by_id, ledger):
    meta = _meta(issue, 'dailyBug') or {}
    batch = issues_by_id.get(meta.get('batchId') or issue.get('parent_issue_id'))
    if not batch:
        return None
    source = batch
    repair = _meta(batch, 'dailyBatch') or {}
    if repair.get('mode') == 'repair':
        source = issues_by_id.get(repair.get('sourceScanId'))
        if not source or batch.get('parent_issue_id') != source['id']:
            return None
    scan = _meta(source, 'dailyScan') or {}
    record = ledger.get((source['identifier'], 'scan')) or {}
    feedback = scan.get('targetDate') or record.get('feedbackDate')
    # 历史非扫描批次可识别为范围外，不猜它属于哪一个反馈日。
    return {'scanDate': scan_date(source), 'feedbackDate': feedback, 'batch': batch['identifier']}


def slack_thread(cli, permalink):
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / 'thread.json'
        subprocess.run([sys.executable, cli, 'replies', '--url', permalink, '--output', str(output)],
                       check=True, capture_output=True, text=True, timeout=30)
        messages = json.loads(output.read_text())['messages']
        if not isinstance(messages, list):
            raise ValueError('Slack thread 消息列表无效')
        return messages


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
    issues = load_issues(args.project)
    # 工位轮次的清单与逐条结论在评论里，读取后才能推导调查进度；读取失败只让该轮进度显示待核对。
    try:
        progress_module.queue.attach_comments(issues, lambda issue_id: run_json(
            ['multica', 'issue', 'comment', 'list', issue_id, '--full', '--compact', '--output', 'json']),
            include_finished=True)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError, progress_module.queue.QueueError):
        warnings.append('调查工位评论读取失败，相关轮次的调查进度待核对')
    runs = []
    if any(_meta(issue, 'dailyRound') for issue in issues):
        try:
            runs = run_json(['multica', 'agent', 'tasks', getattr(args, 'duty_agent',
                            '387e0620-8a22-44f5-919c-4a3e6296fcfb'), '--output', 'json'])
            if not isinstance(runs, list):
                raise ValueError('运行列表无效')
        except (subprocess.CalledProcessError, ValueError, OSError):
            runs = None
    issues_by_id = {issue['id']: issue for issue in issues}
    through = (dt.date.fromisoformat(args.today) if args.today else now.astimezone(TZ).date()) - dt.timedelta(days=1)
    for issue in issues:
        is_bug = (_meta(issue, 'dailyBug') or {}).get('mode') == 'bug'
        is_scan = bool(_meta(issue, 'dailyScan')) and scan_date(issue) >= start.isoformat()
        if not (is_bug or is_scan):
            continue
        cohort = bug_cohort(issue, issues_by_id, ledger) if is_bug else None
        if is_bug:
            if cohort and cohort['scanDate'] < start.isoformat():
                continue
            if not cohort or not cohort['feedbackDate']:
                warnings.append(f"{issue['identifier']} 缺少所属扫描批次或批次反馈日，未计入看板")
                continue
            if not start.isoformat() <= cohort['feedbackDate'] <= through.isoformat():
                continue
        board = ledger.get((issue['identifier'], 'bug' if is_bug else 'scan'))
        if is_scan and not is_bug:
            data = _meta(issue, 'dailyScan') or {}
            feedback = data.get('targetDate') or (board or {}).get('feedbackDate')
            if not feedback or not start.isoformat() <= feedback <= through.isoformat():
                continue
            if board and board.get('feedbackDate') != feedback:
                warnings.append(f"{issue['identifier']} 台账反馈日与原扫描不一致，采用原扫描阶段事实")
                board = None
            counts = data.get('counts') or {}
            record = {**(board or {}), 'identifier': issue['identifier'], 'feedbackDate': feedback,
                      'createdAt': issue['created_at'], 'scanDate': scan_date(issue)}
            # 采集事实先于最终台账存在；逐阶段展示，不把“尚未汇总”误判为“未扫描”。
            if data.get('collected') is not None:
                values = data['collected']
                record['collected'] = values if (isinstance(values, dict) and
                    all(progress_module.count(value) for value in values.values())) else None
            record['coverageComplete'] = data.get('coverageComplete', record.get('coverageComplete'))
            for key in SCAN_SPLIT:
                if progress_module.count(counts.get(key)):
                    record[key] = counts[key]
            if _meta(issue, 'dailyRound'):
                record['progress'] = progress_module.snapshot(issue, issues, runs, now, args.issue_url_base, board)
                record['collected'] = record['progress']['collection'].get('collected')
                warnings.extend(record['progress']['warnings'])
            elif board is None:
                warnings.append(f"{issue['identifier']} 已有采集记录，最终分类或调查进度尚未登记")
            scans.append(record)
            continue
        if board is None:
            warnings.append(f"{issue['identifier']} 在字段台账中没有记录，未计入看板")
            continue
        daily_bug = _meta(issue, 'dailyBug') or {}
        delivery = board.get('delivery')
        # 兼容历史台账直接保存 Slack permalink 的写法。
        if isinstance(delivery, str):
            delivery = {'permalink': delivery}
        bug = {'identifier': issue['identifier'], 'url': args.issue_url_base + issue['identifier'],
               'summary': issue['title'].split('｜')[-1], 'feedbackDate': cohort['feedbackDate'], 'scanDate': cohort['scanDate'],
               'batch': cohort['batch'], 'sourceFeedbackDate': board.get('feedbackDate'),
               'investigationIssueId': daily_bug.get('investigationIssueId'),
               'source': board.get('source'), 'sourceUrl': board.get('sourceUrl'),
               'deliveryPermalink': (delivery or {}).get('permalink'), 'rejection': None, 'notify': None,
               'delivered': daily_bug.get('completeDelivery') is True}
        bug['prs'] = [github.pr(url) for url in board.get('prs') or []]
        blocked = daily_bug.get('state') == 'blocked'
        bug['state'] = bug_state(bug['prs'], False, blocked)
        # 真实受阻单列，历史草稿 PR 不算交付；已合并仍按事实记上线，均不依赖旧 Slack 消息。
        if bug['state'] == 'blocked':
            bug['blockedReason'] = (daily_bug.get('blocker') or {}).get('reason') or '修复受阻，保留现场'
        elif bug['state'] != 'released' and bug['deliveryPermalink']:
            try:
                bug['rejection'] = rejection(slack_thread(args.slack_cli, bug['deliveryPermalink']), args.reviewer)
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
                warnings.append(f"{bug['identifier']} Slack 交付 thread 读取失败，审查标记未核对；保留 PR 状态继续生成")
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
    'confirmed': '确认可修', 'skipped_failure': '执行失败已跳过（未确认）', 'verification_blocked': '待验证/验证受阻', 'duplicate': '重复',
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
    if path.suffix != '.zip':
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
            subprocess.run(multica_command(['multica', 'attachment', 'download', attachment['id'], '-o', directory]),
                           check=True, capture_output=True, text=True)
            index = read_scan_index(Path(directory) / name)
            if index is not None:
                return index
    return None


def _index_error(error):
    """读取失败原因摘要：命令失败取退出码与 stderr 末段，其它异常取类型与说明。"""
    if isinstance(error, subprocess.CalledProcessError):
        detail = ' '.join((error.stderr or '').split())[-200:]
        return f"{' '.join(error.cmd[:3])} 退出码 {error.returncode}" + (f"：{detail}" if detail else '')
    return f"{type(error).__name__}：{' '.join(str(error).split())[:200]}"


def yesterday_scan(board, scans, scan_index_path=None):
    """昨日反馈日最新一次扫描的逐条记录；没有扫描或索引不可用时返回原因供页面说明。

    scan_index_path 是值守 Agent 预先下载的截至日索引，只用于截至日视图；其它日期仍由脚本自行下载。"""
    candidates = [scan for scan in scans if scan.get('feedbackDate') == board['through']]
    if not candidates:
        return None, f"{board['through']} 反馈日尚未扫描，暂无逐条记录。"
    scan = max(candidates, key=lambda item: item['createdAt'])
    unavailable = f"{scan['identifier']} 没有可用的证据索引（{SCAN_INDEX_NAME}），暂无逐条记录"
    try:
        index = read_scan_index(Path(scan_index_path)) if scan_index_path else load_scan_index(scan['identifier'])
    except (subprocess.CalledProcessError, OSError, ValueError, zipfile.BadZipFile) as error:
        return None, f"{unavailable}；读取失败：{_index_error(error)}。"
    if not index:
        return None, f"{unavailable}。"
    if index.get('targetDate') not in (None, board['through']):
        return None, f"{unavailable}；索引反馈日为 {index.get('targetDate')}。"
    if index.get('identifier') not in (None, scan['identifier']):
        return None, f"{unavailable}；索引属于 {index.get('identifier')}。"
    return {'identifier': scan['identifier'], 'records': scan_records(index)}, None


RECORD_GROUPS = (('需要关注', ('verification_blocked', 'confirmed', 'duplicate')),
                 ('已处理', ('cs_followed', 'excluded_after_investigation')),
                 ('非 Bug', ('product_decision', 'non_bug')))
DEFAULT_RECORD_STATE = 'verification_blocked'
TODO_TAGS = {'notify': 'notify', 'responded': 'released', 'review': 'review',
             'pr_closed': 'rejected', 'fixing': 'review', 'unrejectable': 'review'}


def _bug_note(bug, now, ages):
    if bug['state'] == 'released':
        return notify_label(bug, now)
    if bug['state'] == 'rejected':
        return (bug.get('rejection') or {}).get('reason') or '原因未填'
    if bug['state'] == 'blocked':
        return bug.get('blockedReason') or '原因未填'
    return ages.get(bug['identifier'], '')


def html_payload(board, now, scan, scan_note, board_link=None, issue_url_base=''):
    """HTML 版所需数据：与 Markdown/Slack 版同源，只做展示所需的整理。"""
    items = attention(board, now)
    ages = {bug['identifier']: note for bug, note in items['review'] if note}
    bugs = [{
        'identifier': bug['identifier'], 'url': _https(bug['url']), 'summary': bug['summary'],
        'feedbackDate': bug['feedbackDate'], 'scanDate': bug.get('scanDate'),
        'source': bug.get('source') or '', 'sourceUrl': _https(bug.get('sourceUrl')), 'state': bug['state'],
        'prs': [{'label': f"{pr['repo'].split('/')[-1]}#{pr['number']}", 'url': _https(pr['url'])} for pr in bug['prs']],
        'note': _bug_note(bug, now, ages),
    } for bug in board['bugs']]
    todo = [{'key': key, 'title': ATTENTION_TITLES[key], 'identifier': bug['identifier'], 'url': _https(bug['url']),
             'summary': bug['summary'], 'note': note or ''} for key, entries in items.items() for bug, note in entries]
    return {
        'through': board['through'], 'start': board['start'],
        'generatedAt': f'{now.astimezone(TZ):%m-%d %H:%M}', 'boardLink': _https(board_link),
        'rows': board['rows'], 'total': board['total'], 'bugs': bugs, 'todo': todo, 'warnings': board['warnings'],
        'scan': scan and {**scan, 'url': _https(issue_url_base + scan['identifier'])}, 'scanNote': scan_note,
    }


def html_date_payloads(board, scans, now, board_link=None, issue_url_base='', scan_index_path=None):
    """按反馈日内置视图；历史日期限制反馈范围，进度仍采用生成时的最新状态。"""
    payloads = []
    for date in [row['date'] for row in board['rows']] or [board['through']]:
        view = board
        if date != board['through']:
            view = build_board([scan for scan in scans if scan['feedbackDate'] <= date],
                               [bug for bug in board['bugs'] if bug['feedbackDate'] <= date],
                               dt.date.fromisoformat(board['start']),
                               dt.date.fromisoformat(date) + dt.timedelta(days=1))
            # 全局读取告警也必须可见，不因切换日期隐藏数据问题。
            view['warnings'] = board['warnings']
        scan, note = yesterday_scan(view, scans, scan_index_path if date == board['through'] else None)
        payloads.append(html_payload(view, now, scan, note, board_link, issue_url_base))
    return payloads


def _h(value):
    return escape(str(value), quote=True)


def _a(text, url):
    url = _https(url)
    return f'<a href="{_h(url)}" target="_blank" rel="noopener">{_h(text)}</a>' if url else _h(text)


def _tag(kind, text):
    return f'<span class="tag t-{_h(kind)}">{_h(text)}</span>'


def _html_top(payload):
    total = payload['total']
    link = f" · {_a('Multica 常驻看板', payload['boardLink'])}" if payload['boardLink'] else ''
    stats = ''.join(f'<div><span>{label}</span><strong>{value}</strong></div>' for label, value in
                    (('累计确认', total['confirmed']), ('已上线', total['released']), ('已回告', total['notified'])))
    return (f'<header class="top"><div class="wrap"><div><h1>每日 Bug 值守｜{_h(payload["through"][5:])} 反馈日</h1>'
            f'<p>三源扫描 → 疑似调查 → 确认可修 → 上线 · 生成于 {_h(payload["generatedAt"])}{link}</p></div>'
            f'<div class="agg">{stats}</div></div></header>')


def _record_counts(payload):
    counts = {}
    for record in (payload['scan'] or {}).get('records', []):
        counts[record['state']] = counts.get(record['state'], 0) + 1
    return counts


def _html_funnel(payload):
    latest, scan = (payload['rows'] or [None])[-1], payload['scan']
    if not latest or not latest['scanned']:
        body = f'<p class="empty">{_h(payload["through"])} 反馈日尚未扫描，扫描完成后的看板会显示漏斗。</p>'
        return f'<section class="widget"><div class="head"><h2>当日漏斗</h2></div>{body}</section>'
    sources = latest.get('collectedBySource') or {}
    split = ' · '.join(f'{_h(name.capitalize())} {count}' for name, count in
                       sorted(sources.items(), key=lambda item: -item[1]))
    stages = [('s1', '采集', _num(latest['collected']), split), ('s2', '疑似候选', _suspected(latest), '需要调查'),
              ('s3', '确认可修', latest['confirmed'], '进入修复'), ('s4', '已上线', latest['released'], '本批次 PR 已合并')]
    flow = ''.join(f'<div class="stage {cls}"><b>{label}</b><strong>{_h(value)}</strong><small>{note}</small></div>'
                   for cls, label, value, note in stages)
    coverage = '三源采集完整' if latest.get('coverageComplete') else '来源覆盖不完整'
    source = (_h(latest['label']) + ' · ' + (f"扫描任务 {_a(scan['identifier'], scan['url'])} · " if scan else ''))
    counts = _record_counts(payload)
    foot = ''.join(f'<span>{_h(RECORD_STATES.get(state, state))} <b>{counts[state]}</b></span>'
                   for state in RECORD_STATES if state != 'confirmed' and counts.get(state))
    return (f'<section class="widget"><div class="head"><h2>当日漏斗</h2><p>{source}{coverage}</p></div>'
            f'<div class="flow">{flow}</div>' + (f'<div class="foot">{foot}</div>' if foot else '') + '</section>')


def _html_todo(payload):
    rows = ''.join(f'<li>{_tag(TODO_TAGS.get(item["key"], "review"), item["title"])}'
                   f'{_a(item["identifier"] + " " + item["summary"], item["url"])}<span class="meta">{_h(item["note"])}</span></li>'
                   for item in payload['todo'])
    rows += ''.join(f'<li>{_tag("review", "数据说明")}<span>{_h(text)}</span><span></span></li>' for text in payload['warnings'])
    count = len(payload['todo']) + len(payload['warnings'])
    body = f'<ul class="todo">{rows}</ul>' if rows else '<p class="empty">今天没有需要你处理的事项。</p>'
    return f'<section class="widget"><div class="head"><h2>需要你处理</h2><p>{count} 项</p></div>{body}</section>'


def _html_bugs(payload):
    lanes = []
    for state in STATUS_ORDER:
        bugs = [bug for bug in payload['bugs'] if bug['state'] == state]
        if not bugs:
            continue
        rows = []
        for bug in bugs:
            origin = ' · '.join(filter(None, [bug['source'], f"{bug['scanDate'][5:]} 扫描" if bug['scanDate'] else '', f"{bug['feedbackDate'][5:]} 反馈"]))
            prs = ' · '.join([_a(pr['label'], pr['url']) for pr in bug['prs']]) or '–'
            if state == 'blocked':
                prs = '未交付（历史 PR 见任务）' if bug['prs'] else '未交付'
            tag_kind = {'released': 'released', 'rejected': 'rejected'}.get(state)
            note = _tag(tag_kind, bug['note']) if tag_kind and bug['note'] else _h(bug['note'])
            rows.append(f'<div class="bug">{_a(bug["identifier"], bug["url"])}<div>{_h(bug["summary"])}'
                        f'<span class="src">{_h(origin)}</span></div><div class="prs">{prs}</div><div class="note">{note}</div></div>')
        lanes.append(f'<h3>{_h(STATE_LABELS[state].split(" ", 1)[-1])} {len(bugs)}</h3>' + ''.join(rows))
    sub = f"范围内共 {len(payload['bugs'])} 个"
    body = f'<div class="lane">{"".join(lanes)}</div>' if lanes else '<p class="empty">暂无确认的 Bug。</p>'
    return f'<section class="widget full"><div class="head"><h2>Bug 进度</h2><p>{sub}</p></div>{body}</section>'


def _html_trend(payload):
    rows, total = payload['rows'], payload['total']
    peak = max([row['collected'] for row in rows if isinstance(row.get('collected'), int)] or [1]) or 1
    def bar(row):
        if not isinstance(row.get('collected'), int):
            return ''
        suspected = row['suspected'] if isinstance(row.get('suspected'), int) else 0
        return (f'<div class="bar"><i style="width:{row["collected"] / peak * 100:.0f}%"></i>'
                f'<i class="sus" style="width:{suspected / peak * 100:.0f}%"></i></div>')
    def line(row, label, chart=True):
        scanned = row.get('scanned', True)
        cells = [label, _num(row['collected'], scanned), bar(row) if chart else '', _suspected(row, scanned),
                 row['confirmed'], row['blocked'], row['rejected'], row['released'], row['notified']]
        return ''.join(f'<td>{cell if index == 2 else _h(cell)}</td>' for index, cell in enumerate(cells))
    body = ''.join(f'<tr>{line(row, row["label"])}</tr>' for row in rows)
    body += f'<tr class="total">{line(total, total["label"], chart=False)}</tr>'
    head = ''.join(f'<th>{text}</th>' for text in ('扫描日（反馈日）', '采集', '采集 / 疑似', '疑似', '确认可修', '受阻', '不上线', '已上线', '已回告'))
    return (f'<section class="widget full"><div class="head"><h2>每日趋势</h2><p>按所属扫描批次；范围外 Bug 不计入</p></div>'
            f'<div class="scroll"><table class="days"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></section>')


def _html_record(record):
    side = ' · '.join(filter(None, [_h(record['source']), _a('原反馈', record['sourceUrl']) if record['sourceUrl'] else '',
                                    _a('关联 PR', record['pr']) if record['pr'] else '']))
    tickets = ''.join(_tag('cat', ticket) for ticket in record['tickets'])
    head = (f'<div><div class="t">{_h(record["summary"])}</div>'
            + (f'<div class="r">{_h(record["reason"])}</div>' if record['reason'] else '')
            + f'</div><div class="side">{side}{tickets}</div>')
    detail = ''.join(f'<div><em>{_h(label)}</em>{_h(text)}</div>' for label, text in record['details'])
    if record['attempts']:
        detail += '<div><em>已做尝试</em>' + '；'.join(_h(text) for text in record['attempts']) + '</div>'
    search = ' '.join([record['summary'], record['reason'], *record['tickets']]).lower()
    attrs = (f'class="item" data-state="{_h(record["state"])}" data-source="{_h(record["source"])}" '
             f'data-search="{_h(search)}"')
    if detail:
        return f'<details {attrs}><summary>{head}</summary><div class="detail">{detail}</div></details>'
    return f'<div {attrs}><div class="row">{head}</div></div>'


def _html_records(payload):
    scan = payload['scan']
    title = '<div class="head"><h2>当日逐条记录</h2>'
    if not scan:
        return f'<section class="widget full">{title}</div><p class="empty">{_h(payload["scanNote"] or "暂无逐条记录。")}</p></section>'
    records, counts = scan['records'], _record_counts(payload)
    grouped = {state for _, states in RECORD_GROUPS for state in states}
    groups = list(RECORD_GROUPS) + [('其他', tuple(state for state in counts if state not in grouped))]
    nav = ''
    for index, (name, states) in enumerate(groups):
        # 「需要关注」组即使为 0 也展示，便于确认当天没有新增可修或重复。
        present = [state for state in states if index == 0 or state in counts]
        if not present:
            continue
        nav += f'<div class="group">{_h(name)}</div>' + ''.join(
            f'<button type="button" data-key="{_h(state)}">{_h(RECORD_STATES.get(state, state))}'
            f'<span>{counts.get(state, 0)}</span></button>' for state in present)
    nav += f'<button type="button" data-key="">全部<span>{len(records)}</span></button>'
    sources = ''.join(f'<option value="{_h(source)}">{_h(source)}</option>'
                      for source in sorted({record['source'] for record in records}))
    tools = ('<div class="tools js-only"><input type="search" id="q" placeholder="搜索摘要、原因或工单号" aria-label="搜索记录">'
             f'<select id="src" aria-label="来源"><option value="">全部来源</option>{sources}</select></div>')
    items = ''.join(_html_record(record) for record in records)
    return (f'<section class="widget full" id="records">{title}<p>{len(records)} 条 · 来自 {_a(scan["identifier"], scan["url"])}'
            f' · 待验证不等于没有 Bug</p></div><div class="rec"><nav class="cats" aria-label="分类">{nav}</nav><div>'
            f'{tools}<div id="list">{items}</div><p class="empty" id="none" hidden>没有匹配的记录。</p>'
            f'<div class="pager js-only" id="pager"></div></div></div></section>')


def _html_view(payload):
    """单个反馈日的完整视图；所有动态文本仍经静态渲染转义。"""
    return (_html_top(payload) + '<main><div class="wrap"><div class="grid">' + _html_funnel(payload)
            + _html_todo(payload) + '</div>' + _html_progress(payload) + _html_bugs(payload)
            + _html_trend(payload) + _html_records(payload)
            + '<footer>已上线：关联 PR 全部 merged；确认可修按所属扫描批次统计，后续方案驳回仍计入。'
            '受阻项单列，不计应上线或已交付 PR；保留证据，不自动补验收。不上线：审查人在交付 thread 上点 🚫。</footer></div></main>')


def _html_progress(payload):
    progress = (payload['rows'] or [{}])[-1].get('progress')
    if not progress:
        return ''
    stages = ''.join(f'<div><h3>{_h(label)}</h3><p>{_h(text)}</p></div>'
                     for label, text in progress_rows(progress))
    current = '；'.join(
        _h(item['phase'] + ('执行中' if item['status'] in {'running', 'waiting_local_directory'} else '已排队'))
        + ' ' + _a(item['identifier'], item['url']) + ' ' + _h(item['summary']) for item in progress['current'])
    current = current or ('暂无在途运行' if progress['activityKnown'] else '运行状态读取失败，待核对')
    return ('<section class="widget full"><div class="head"><h2>阶段结论与当前进度</h2>'
            f'<p>截至 {_h(payload["generatedAt"])}</p></div><div class="phase-cards">{stages}</div>'
            f'<p><b>当前处理：</b>{current}</p><p><b>下一步：</b>{_h(progress["nextAction"])}</p></section>')


def render_html(payload, date_payloads=None):
    """默认日期静态可读；日期切换、分类、搜索与分页均在本地完成。"""
    title = f"每日 Bug 值守｜{payload['through'][5:]} 反馈日"
    views = {item['through']: item for item in date_payloads or []}
    views[payload['through']] = payload
    options = ''.join(f'<option value="{_h(date)}"' + (' selected' if date == payload['through'] else '')
                      + f'>{_h((views[date]['rows'] or [{'label': date}])[-1]['label'])}</option>' for date in sorted(views, reverse=True))
    picker = ('<div class="wrap date-tools js-only"><label for="feedback-date">反馈日期</label>'
              f'<select id="feedback-date">{options}</select>'
              '<span>累计截至所选反馈日；Bug 进度为生成时的最新状态。</span></div>')
    templates = ''.join(f'<template id="date-{_h(date)}">{_html_view(item)}</template>'
                        for date, item in views.items())
    body = picker + f'<div id="board-view">{_html_view(payload)}</div>' + templates
    return (HTML_TEMPLATE.replace('__TITLE__', _h(title)).replace('__DEFAULT__', DEFAULT_RECORD_STATE)
            .replace('__BODY__', body))


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--ink:#243235;--muted:#637477;--faint:#8a9a9c;--line:#d8e1df;--line-soft:#e8eeee;--page:#f4f7f6;--white:#fff;
--teal:#146d70;--teal-soft:#e6f1f0;--orange:#c9774f;--orange-soft:#f8ede6;--green:#367b5e;--green-soft:#e9f3ee;
--red:#a8513e;--red-soft:#f6e9e5;--amber:#9a6a1f;--amber-soft:#f6efe1}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--teal);text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:3px}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
.wrap{width:min(1280px,calc(100% - 40px));margin:auto}
.top{background:var(--white);border-bottom:1px solid var(--line)}
.top .wrap{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;padding:24px 0 20px}
.top h1{font-size:24px;line-height:1.2;margin:0;font-weight:600;letter-spacing:-.01em}
.top p{margin:6px 0 0;color:var(--muted);font-size:13px}
.agg{display:flex;gap:28px}.agg div{text-align:right}.agg span{display:block;color:var(--muted);font-size:12px}
.agg strong{font-size:22px;font-weight:600}
main{padding:22px 0 56px}
.grid{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(0,1fr);gap:18px;align-items:start}
.widget{background:var(--white);border:1px solid var(--line);border-radius:6px;padding:20px 22px 18px;min-width:0}
.full{margin-top:18px}
.phase-cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.phase-cards>div{flex:1 1 220px;padding:12px 14px;background:var(--page);border-radius:4px}
.phase-cards h3{font-size:13px;margin:0 0 6px}.phase-cards p{margin:0;color:var(--muted)}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.head h2{font-size:17px;margin:0;font-weight:600}.head p{margin:0;color:var(--muted);font-size:12px}
.empty{color:var(--muted);margin:14px 0 0}
.flow{display:grid;grid-template-columns:38fr 26fr 18fr 18fr;align-items:center;height:184px;margin:18px 0 14px}
.stage{color:#fff;height:100%;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;padding:0 12px}
.s1{background:var(--teal);clip-path:polygon(0 0,calc(100% - 12px) 0,100% 50%,calc(100% - 12px) 100%,0 100%)}
.s2{height:76%;background:#3f8a86;clip-path:polygon(0 0,calc(100% - 11px) 0,100% 50%,calc(100% - 11px) 100%,0 100%,11px 50%)}
.s3{height:58%;background:var(--orange);clip-path:polygon(0 0,calc(100% - 10px) 0,100% 50%,calc(100% - 10px) 100%,0 100%,10px 50%)}
.s4{height:46%;background:var(--green);clip-path:polygon(0 0,100% 0,100% 100%,0 100%,9px 50%)}
.stage b{font-size:12px;font-weight:600}
.stage strong{font-size:42px;line-height:1.05;font-weight:650;margin-top:4px}
.s3 strong,.s4 strong{font-size:34px}
.stage small{font-size:11px;opacity:.88;margin-top:6px;line-height:1.35}
.foot{display:flex;flex-wrap:wrap;gap:6px 16px;border-top:1px solid var(--line-soft);padding-top:12px;color:var(--muted);font-size:12px}
.foot b{color:var(--ink);font-weight:600}
.todo{list-style:none;margin:14px 0 0;padding:0}
.todo li{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line-soft)}
.todo li:first-child{border-top:0}.todo .meta{color:var(--muted);font-size:12px}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:3px;white-space:nowrap}
.t-notify{background:var(--orange-soft);color:#8a4c2e}.t-review{background:var(--amber-soft);color:var(--amber)}
.t-released{background:var(--green-soft);color:var(--green)}.t-rejected{background:var(--red-soft);color:var(--red)}
.t-cat{background:var(--teal-soft);color:var(--teal);margin-left:6px}
.lane{margin-top:8px}.lane h3{font-size:12px;color:var(--muted);font-weight:600;margin:14px 0 4px}
.bug{display:grid;grid-template-columns:76px minmax(0,1fr) minmax(0,1.1fr) 170px;gap:14px;align-items:baseline;padding:9px 0;border-top:1px solid var(--line-soft);font-size:13px}
.bug .src{color:var(--faint);font-size:12px;margin-left:6px}.bug .prs{color:var(--muted);font-size:12px}
.bug .note{color:var(--muted);font-size:12px;text-align:right}.bug .note .tag{white-space:normal;text-align:left}
.scroll{overflow-x:auto}
.days{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
.days th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.days td{padding:9px 8px;border-bottom:1px solid var(--line-soft);vertical-align:middle;white-space:nowrap}
.days td:nth-child(3){width:34%}
.days .bar{height:8px;border-radius:2px;background:var(--line-soft);position:relative;min-width:160px}
.days .bar i{position:absolute;inset:0 auto 0 0;background:#9cc3c1;border-radius:2px}.days .bar i.sus{background:var(--orange)}
.days tr.total td{font-weight:600;border-bottom:0}
.rec{display:grid;grid-template-columns:200px minmax(0,1fr);gap:22px;margin-top:14px}
.cats{position:sticky;top:12px;align-self:start;display:flex;flex-direction:column;gap:2px}
.cats button{all:unset;display:flex;justify-content:space-between;padding:7px 10px;border-radius:4px;font-size:13px;color:var(--ink)}
.cats button span{color:var(--muted)}.cats .group{font-size:11px;color:var(--faint);margin:10px 10px 2px}
.js .cats button{cursor:pointer}.js .cats button:hover{background:var(--page)}
.cats button.on{background:var(--teal-soft);color:var(--teal);font-weight:600}
.js-only{display:none}.js .js-only{display:flex}
.date-tools{gap:10px;align-items:center;flex-wrap:wrap;padding:14px 0;color:var(--muted)}
.date-tools select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:4px;background:var(--white);color:var(--ink)}
.tools{gap:8px;margin-bottom:6px}
.tools input{flex:1;font:inherit;color:inherit;border:1px solid var(--line);border-radius:4px;padding:7px 10px;background:var(--white)}
.tools select{font:inherit;color:inherit;border:1px solid var(--line);border-radius:4px;padding:7px 8px;background:var(--white)}
.item{border-top:1px solid var(--line-soft);padding:11px 0}
.item summary,.item .row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px}
.item summary{list-style:none;cursor:pointer}.item summary::-webkit-details-marker{display:none}
.item .t{font-weight:500}.item .r{color:var(--muted);font-size:13px;margin-top:2px;max-width:72ch}
.item .side{text-align:right;font-size:12px;color:var(--faint);white-space:nowrap}
.item .detail{margin-top:8px;padding:8px 12px;background:var(--page);border-radius:4px;font-size:13px;max-width:80ch}
.item .detail div{margin:3px 0}.item .detail em{font-style:normal;color:var(--muted);margin-right:8px}
.pager{justify-content:space-between;align-items:center;color:var(--muted);font-size:12px;margin-top:12px}
.pager button{font:inherit;color:inherit;border:1px solid var(--line);background:var(--white);border-radius:4px;padding:5px 10px;margin-left:6px;cursor:pointer}
.pager button:disabled{opacity:.4;cursor:default}
footer{margin-top:20px;color:var(--muted);font-size:12px}
@media(max-width:900px){.grid,.rec{grid-template-columns:1fr}.cats{position:static;flex-direction:row;flex-wrap:wrap}
.cats .group{width:100%}.bug{grid-template-columns:70px minmax(0,1fr)}.bug .prs,.bug .note{grid-column:2;text-align:left}
.top .wrap{flex-direction:column;align-items:flex-start}.agg{gap:16px}.flow{height:150px}.stage strong{font-size:30px}}
</style>
</head>
<body>
__BODY__
<script>
(function () {
  document.documentElement.classList.add('js');
  var view = document.getElementById('board-view'), date = document.getElementById('feedback-date');
  date.addEventListener('change', function () {
    var template = document.getElementById('date-' + date.value);
    if (!template) return;
    view.replaceChildren(template.content.cloneNode(true));
    document.title = '每日 Bug 值守｜' + date.value.slice(5) + ' 反馈日';
    initRecords();
  });
  function initRecords() {
    var list = view.querySelector('#list');
    if (!list) return;
    var items = Array.prototype.slice.call(list.children);
    var buttons = Array.prototype.slice.call(view.querySelectorAll('.cats button'));
    var query = view.querySelector('#q'), source = view.querySelector('#src');
    var pager = view.querySelector('#pager'), none = view.querySelector('#none');
    var state = {category: '__DEFAULT__', page: 0}, PAGE = 30;
    if (!buttons.some(function (button) { return button.dataset.key === state.category; })) state.category = '';
    function draw() {
      var q = query.value.trim().toLowerCase(), s = source.value;
      var hits = items.filter(function (node) {
        return (!state.category || node.dataset.state === state.category) && (!s || node.dataset.source === s)
          && (!q || node.dataset.search.indexOf(q) >= 0);
      });
      var pages = Math.max(1, Math.ceil(hits.length / PAGE));
      state.page = Math.min(state.page, pages - 1);
      items.forEach(function (node) { node.hidden = true; });
      hits.slice(state.page * PAGE, (state.page + 1) * PAGE).forEach(function (node) { node.hidden = false; });
      none.hidden = hits.length > 0;
      buttons.forEach(function (button) { button.classList.toggle('on', button.dataset.key === state.category); });
      var label = document.createElement('span');
      label.textContent = hits.length + ' 条 · 第 ' + (state.page + 1) + ' / ' + pages + ' 页';
      var nav = document.createElement('span');
      [['上一页', -1, state.page === 0], ['下一页', 1, state.page >= pages - 1]].forEach(function (item) {
        var button = document.createElement('button');
        button.type = 'button'; button.textContent = item[0]; button.disabled = item[2];
        button.addEventListener('click', function () { state.page += item[1]; draw(); });
        nav.appendChild(button);
      });
      pager.replaceChildren(label, nav);
    }
    buttons.forEach(function (button) {
      button.addEventListener('click', function () { state.category = button.dataset.key; state.page = 0; draw(); });
    });
    query.addEventListener('input', function () { state.page = 0; draw(); });
    source.addEventListener('change', function () { state.page = 0; draw(); });
    draw();
  }
  initRecords();
})();
</script>
</body>
</html>
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='每日 Bug 值守项目 ID')
    parser.add_argument('--server-url', default='https://multica.devops.moego.dev')
    parser.add_argument('--workspace-id', default='1b6d114a-8407-4ba8-adea-d31209caabe5')
    parser.add_argument('--duty-agent', default='387e0620-8a22-44f5-919c-4a3e6296fcfb', help='值守 Agent 的真实运行来源')
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
    parser.add_argument('--scan-index', help='截至日扫描任务的证据索引（scan-evidence-index.json 或含它的证据 zip）；'
                                             '不传时脚本自行从扫描任务附件下载')
    args = parser.parse_args(argv)
    MULTICA_CLI[:] = ['multica', '--server-url', args.server_url, '--workspace-id', args.workspace_id]
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
    date_payloads = html_date_payloads(board, scans, now, args.board_link, args.issue_url_base, args.scan_index)
    (output / 'board.html').write_text(render_html(date_payloads[-1], date_payloads))
    print(json.dumps({'output': str(output), 'warnings': board['warnings'], 'scanNote': date_payloads[-1]['scanNote'],
                      'followUpTasks': {key: len(value) for key, value in tasks.items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
