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
    """台账任务评论 → {任务编号: 字段}。每条带标记的评论含一个 JSON 对象或数组，
    每项须有 issue 字段；同一任务以最新评论（同评论内靠后项）为准。"""
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
                    entries[item['issue']] = item
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
            'coverageComplete': scan.get('coverageComplete') if scan else None,
            **_layers([bug for bug in bugs if bug['feedbackDate'] == key]),
        })
        day += dt.timedelta(days=1)
    trial_bugs = [bug for bug in bugs if bug['feedbackDate'] < start.isoformat()]
    trial = {'label': '试运行（回溯）', 'collected': None, 'suspected': None, **_layers(trial_bugs)} if trial_bugs else None
    def total_of(key):
        values = [row[key] for row in rows]
        return sum(values) if values and all(isinstance(value, int) for value in values) else None
    in_window = [bug for bug in bugs if start.isoformat() <= bug['feedbackDate'] <= yesterday.isoformat()]
    total = {'label': f'累计（d1–d{len(rows)}）', 'collected': total_of('collected'),
             'suspected': total_of('suspected'), **_layers(in_window)}
    warnings = [warning for scan in latest_scan.values() for warning in scan_warnings(scan)]
    return {'start': start.isoformat(), 'through': yesterday.isoformat(), 'rows': rows, 'trial': trial,
            'total': total, 'bugs': sorted(bugs, key=lambda bug: (bug['feedbackDate'], bug['identifier'])),
            'warnings': warnings}


# ---------- 渲染 ----------

def _num(value, scanned=True):
    if not scanned:
        return '未扫描'
    return '缺失' if value is None else str(value)


def _age(since, now):
    if not since:
        return '–'
    days = (now - dt.datetime.fromisoformat(since.replace('Z', '+00:00'))).days
    return f'{days} 天'


def _pr_links(bug):
    return '、'.join(f"[{pr['repo'].split('/')[-1]}#{pr['number']}]({pr['url']})" for pr in bug['prs']) or '–'


def attention(board, now):
    items = {'merged': [], 'review': [], 'pr_closed': [], 'unrejectable': [], 'fixing': []}
    for bug in board['bugs']:
        state = bug['state']
        if state == 'merged':
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
    'merged': '已合并待上线', 'review': '待审 PR', 'pr_closed': 'PR 已关闭未合并且未标 🚫',
    'fixing': '已确认尚无 PR', 'unrejectable': '无交付消息，无法用 🚫 标注',
}


def attention_notes(board, now):
    return [f"- {ATTENTION_TITLES[key]} {len(entries)}：" + '、'.join(
        f"[{bug['identifier']}]({bug['url']})" + (f'（{age}）' if age else '') for bug, age in entries)
        for key, entries in attention(board, now).items() if entries]


def render_markdown(board, now):
    header = ['反馈日', '采集', '疑似候选', '确认 Bug', '不上线', '应上线', '已交付 PR', '已合并', '已上线', '已回告', '用户回应']
    lines = [f"## 每日 Bug 值守看板｜截至 {board['through']} 反馈日", '',
             f"生成时间：{now.astimezone(TZ):%Y-%m-%d %H:%M}（Asia/Shanghai）。按反馈日分组，d1 = {board['start']}。", '',
             '### 漏斗', '', '| ' + ' | '.join(header) + ' |', '|' + '---|' * len(header)]
    def row_line(row, bold=False):
        if row is board['trial']:
            intake = ['–', '–']
        else:
            scanned = row.get('scanned', True)
            intake = [_num(row['collected'], scanned), _num(row['suspected'], scanned)]
        cells = [row['label'], *intake]
        cells += [str(row[key]) for key in ('confirmed', 'rejected', 'shouldShip', 'delivered', 'merged', 'released')]
        cells += ['–', '–']
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
              '「已回告」「用户回应」尚未接入（阶段 3）。', '', '### Bug 明细', '',
              '| Bug | 反馈日 | 来源 | 问题 | PR | 状态 |', '|---|---|---|---|---|---|']
    for bug in board['bugs']:
        state = STATE_LABELS[bug['state']]
        if bug['state'] == 'released':
            tags = '、'.join(sorted({pr['release']['tag'] for pr in bug['prs'] if pr.get('release')}))
            state += f'（{tags}）'
        if bug['state'] == 'rejected':
            state += f"：{bug['rejection'].get('reason') or '原因未填'}"
        source = f"[{bug['source']}]({bug['sourceUrl']})" if bug.get('sourceUrl') else bug['source']
        lines.append(f"| [{bug['identifier']}]({bug['url']}) | {bug['feedbackDate']} | {source} | "
                     f"{bug['summary']} | {_pr_links(bug)} | {state} |")
    lines += ['', '### 需要关注', ''] + (attention_notes(board, now) or ['- 无'])
    lines += ['', '### 数据说明', ''] + [f'- {warning}' for warning in board['warnings']] + [
        '- 已上线：PR 合并提交已包含在「Release to PRODUCTION」成功运行的 release 标签中；无该流程的仓库合并即视为就绪。',
        '- 不上线：审查人在 Bug 交付 thread 上点 🚫；原因取其以 🚫 开头的最新回复。']
    return '\n'.join(lines) + '\n'


def render_slack(board, now, board_link=None):
    total = board['total']
    def chain(row):
        intake = '' if row is board['trial'] else f"采集 {_num(row['collected'])} → 疑似候选 {_num(row['suspected'])} → "
        return (f"{intake}确认 {row['confirmed']}（不上线 {row['rejected']}）→ 已交付 {row['delivered']}"
                f" → 已合并 {row['merged']} → 已上线 {row['released']}")
    last = board['rows'][-1] if board['rows'] else None
    lines = [f"**📊 每日 Bug 值守看板｜截至 {board['through']} 反馈日（d1–d{len(board['rows'])}）**", '',
             f'- 累计：{chain(total)}']
    if last:
        lines.append(f"- 昨日（{last['label']}）：" + (chain(last) if last['scanned'] else '未扫描'))
    if board['trial']:
        lines.append(f"- 试运行（回溯）：{chain(board['trial'])}")
    notes = attention_notes(board, now) + [f'- 数据异常：{warning}' for warning in board['warnings']]
    lines += ['', '**⚠️ 需要关注**', ''] + (notes or ['- 无'])
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


def collect(args):
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
        board = ledger.get(issue['identifier'])
        if board is None:
            warnings.append(f"{issue['identifier']} 在字段台账中没有记录，未计入看板")
            continue
        if board.get('kind') == 'scan':
            scans.append({**board, 'identifier': issue['identifier'], 'createdAt': issue['created_at']})
            continue
        if board.get('kind') != 'bug':
            continue
        bug = {'identifier': issue['identifier'], 'url': args.issue_url_base + issue['identifier'],
               'summary': issue['title'].split('｜')[-1], 'feedbackDate': board['feedbackDate'],
               'source': board.get('source'), 'sourceUrl': board.get('sourceUrl'),
               'deliveryPermalink': (board.get('delivery') or {}).get('permalink'), 'rejection': None}
        bug['prs'] = [github.pr(url) for url in board.get('prs') or []]
        if bug['deliveryPermalink']:
            bug['rejection'] = rejection(slack_thread(args.slack_cli, bug['deliveryPermalink']), args.reviewer)
        bug['state'] = bug_state(bug['prs'], bug['rejection'] is not None)
        bugs.append(bug)
    return scans, bugs, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='每日 Bug 值守项目 ID')
    parser.add_argument('--ledger-issue', required=True, help='看板字段台账任务 ID 或编号')
    parser.add_argument('--start', required=True, help='d1 对应的反馈日（YYYY-MM-DD）')
    parser.add_argument('--today', help='Asia/Shanghai 当天日期，默认取当前时间')
    parser.add_argument('--reviewer', default='U06GM8PAFEX', help='有权用 🚫 标注不上线的 Slack 用户 ID')
    parser.add_argument('--slack-cli', default=os.path.expanduser('~/.agents/skills/slack/scripts/slack.py'))
    parser.add_argument('--issue-url-base', default='https://multica.devops.moego.dev/grm/issues/')
    parser.add_argument('--board-link', help='常驻看板任务链接，附在 Slack 版末尾')
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    today = dt.date.fromisoformat(args.today) if args.today else now.astimezone(TZ).date()
    scans, bugs, warnings = collect(args)
    board = build_board(scans, bugs, dt.date.fromisoformat(args.start), today)
    board['warnings'] = warnings + board['warnings']
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'board.json').write_text(json.dumps(board, ensure_ascii=False, indent=2))
    (output / 'board.md').write_text(render_markdown(board, now))
    (output / 'slack.md').write_text(render_slack(board, now, args.board_link))
    print(json.dumps({'output': str(output), 'warnings': board['warnings']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
