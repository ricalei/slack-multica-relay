#!/usr/bin/env python3
"""只读采集 Multica 当前运行，提取统计和 PR／分支工具证据。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import urlsplit
from uuid import UUID


class RunContextError(Exception):
    pass


def write_new(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as file:
        os.chmod(path, 0o600)
        json.dump(value, file, ensure_ascii=False, indent=2)


def query(command, *, text=False):
    try:
        result = subprocess.run(command, capture_output=True,
                                text=True, timeout=30, check=True)
        return result.stdout if text else json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise RunContextError("只读查询失败；请检查 CLI 安装、认证和目标") from None


def scope(env):
    keys = ("MULTICA_SERVER_URL", "MULTICA_WORKSPACE_ID", "MULTICA_TASK_ID")
    if any(not env.get(key) for key in keys):
        raise RunContextError("缺少 Multica Runtime 配置")
    return {key: env[key] for key in keys}


def snapshot(issue, env):
    identity = scope(env)
    cli = ["multica", "--server-url", identity["MULTICA_SERVER_URL"],
           "--workspace-id", identity["MULTICA_WORKSPACE_ID"]]
    runs = query(cli + ["issue", "runs", issue, "--output", "json"])
    matches = [r for r in runs if isinstance(r, dict) and r.get("id") == identity["MULTICA_TASK_ID"]]
    if len(matches) != 1:
        raise RunContextError("当前 run 未唯一命中，禁止改选其他运行")
    run = matches[0]
    if run.get("issue_id") != issue or run.get("workspace_id") != identity["MULTICA_WORKSPACE_ID"]:
        raise RunContextError("运行归属不匹配")
    model = None
    service_tier = None
    agent_id = run.get("agent_id")
    if agent_id:
        try:
            agent = query(cli + ["agent", "get", agent_id, "--output", "json"])
            if isinstance(agent, dict) and agent.get("id") == agent_id and agent.get("workspace_id") == identity["MULTICA_WORKSPACE_ID"]:
                candidate = agent.get("model")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", candidate.strip()):
                    model = candidate.strip()
                    if agent.get("service_tier") in ("priority", "default"):
                        service_tier = agent["service_tier"]
        except RunContextError:
            pass
    try:
        messages = query(cli + ["issue", "run-messages", run["id"], "--issue", issue, "--output", "json"])
    except RunContextError:
        messages = []
    captured = datetime.now(timezone.utc).isoformat()
    # 不保存发起人、邮箱、完整任务正文等与统计无关的 run 字段。
    selected = {key: run[key] for key in ("id", "issue_id", "workspace_id", "agent_id", "started_at", "status") if key in run}
    if model:
        selected.update(model=model, model_source="agent_config")
    if service_tier:
        selected.update(service_tier=service_tier, service_tier_source="agent_config")
    return {"version": 1, "scope": identity, "captured_at": captured, "run": selected, "messages": messages}


def valid_messages(data):
    messages = data.get("messages")
    run = data["run"]
    if not isinstance(messages, list) or len(messages) > 10000:
        return []
    if any(not isinstance(m, dict) or m.get("seq") != index + 1
           or m.get("task_id") != run["id"] or m.get("issue_id") != run["issue_id"]
           or m.get("type") not in ("text", "tool_use", "tool_result", "error")
           for index, m in enumerate(messages)):
        return []
    return messages


def words(message):
    if message.get("tool") != "exec_command":
        return []
    value = message.get("input") or {}
    command = value.get("command", value.get("cmd", ""))
    try:
        parsed = shlex.split(command)
        if len(parsed) == 3 and Path(parsed[0]).name in ("zsh", "sh", "bash") and parsed[1] in ("-c", "-lc"):
            command = parsed[2]
            parsed = shlex.split(command)
        if any(char in command for char in (";", "|", "&", "`", "$", "\n", ">", "<")):
            return []
        return parsed
    except (ValueError, TypeError):
        return []


def skill_paths(message):
    parsed = words(message)
    if len(parsed) > 1 and parsed[1] == "--":
        parsed = [parsed[0], *parsed[2:]]
    paths = parsed[1:]
    if paths and parsed[0] in ("cat", "/bin/cat") and all(
            re.fullmatch(r"(?:/|~/)[^*?\[\]]+/SKILL\.md", path) for path in paths):
        return paths
    return []


def loaded_names(output, count):
    if not isinstance(output, str) or not re.match(r"^---\r?\n", output):
        return []
    # 单文件只读取开头的 frontmatter，正文中的 Skill 示例不参与统计。
    headers = re.findall(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", output, re.S | re.M)
    if count == 1:
        headers = headers[:1]
    loaded = [re.findall(r"^name:\s*['\"]?([A-Za-z0-9_:/.-]+)['\"]?\s*$", header, re.M) for header in headers]
    if len(loaded) == count and all(len(name) == 1 for name in loaded):
        return [name[0] for name in loaded]
    return []


def skill_names(messages):
    names = set()
    calls, results = [], []
    for message in messages:
        if message["type"] == "tool_use":
            calls.append(message)
        elif message["type"] == "tool_result":
            results.append(message)
        else:
            continue
        if not calls or len(results) != len(calls):
            continue
        # API 没有 call_id。等待并行批次收齐，按实际 frontmatter 与请求路径
        # 关联 Skill；不按返回顺序配对，也不让无关搜索清空已确认的名称。
        reads = [(call, skill_paths(call)) for call in calls]
        for result in results:
            matches = []
            for call, paths in reads:
                if not paths or call["seq"] >= result["seq"] or call.get("tool") != result.get("tool"):
                    continue
                loaded = loaded_names(result.get("output"), len(paths))
                if not loaded:
                    continue
                sequential_single = len(calls) == 1 and len(paths) == 1
                path_names = [Path(path).parent.name for path in paths]
                returned_names = [name.rsplit(":", 1)[-1] for name in loaded]
                if sequential_single or returned_names == path_names:
                    matches.append(loaded)
            # 多个候选仅在名称完全一致时可按名称去重，具体 call 归属不作推断。
            if matches and all(match == matches[0] for match in matches):
                names.update(matches[0])
        calls, results = [], []
    return names


def statistics(data):
    stats = {}
    try:
        seconds = round((datetime.fromisoformat(data["captured_at"]) - datetime.fromisoformat(data["run"]["started_at"].replace("Z", "+00:00"))).total_seconds())
        if seconds >= 0:
            stats["duration_seconds"] = seconds
    except (ValueError, KeyError, TypeError):
        pass
    model = data["run"].get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model):
        stats.update(model=model, model_source="agent_config")
        service_tier = data["run"].get("service_tier")
        if service_tier in ("priority", "default"):
            stats.update(service_tier=service_tier, service_tier_source="agent_config")
    messages = valid_messages(data)
    calls = [m for m in messages if m["type"] == "tool_use"]
    if calls:
        stats["tools"] = len(calls)
    names = skill_names(messages)
    if names:
        stats.update(skills=len(names), skill_names=sorted(names))
    return stats


def code_evidence(messages):
    evidence = []
    pending = []
    selected = {}
    for message in messages:
        if message["type"] == "tool_use":
            pending.append(message)
            command = words(message)
            is_pr = command[:2] == ["gh", "pr"]
            git_args = command[3:] if len(command) > 3 and command[1] == "-C" else command[1:]
            is_git = bool(git_args) and command[0] == "git" and git_args[0] in (
                "branch", "status", "remote", "rev-parse")
            if is_pr or is_git:
                row = {"call_seq": message["seq"], "command": shlex.join(command)}
                evidence.append(row)
                selected[message["seq"]] = row
        elif message["type"] == "tool_result":
            candidates = [call for call in pending if call.get("tool") == message.get("tool")]
            # API 没有 call_id；并发或不相邻时保留调用证据，不猜结果归属。
            if len(pending) == 1 and candidates and candidates[0]["seq"] + 1 == message["seq"]:
                row = selected.get(candidates[0]["seq"])
                output = message.get("output")
                if row is not None and isinstance(output, str):
                    row.update(result_seq=message["seq"], output=output[:4000], output_truncated=len(output) > 4000)
            if candidates:
                pending.remove(candidates[0])
    return evidence


def link_context(data, identifier=None, workspace_slug=None, app_url=None, env=None):
    env = env or {}
    if workspace_slug is None:
        workspace_slug = env.get("FINAL_REPLY_WORKSPACE_SLUG")
    if app_url is None:
        app_url = env.get("FINAL_REPLY_APP_URL")
    identity = data["scope"]
    cli = ["multica", "--server-url", identity["MULTICA_SERVER_URL"],
           "--workspace-id", identity["MULTICA_WORKSPACE_ID"]]
    if identifier is None:
        try:
            issue = query(cli + ["issue", "get", data["run"]["issue_id"], "--output", "json"])
            if (isinstance(issue, dict) and issue.get("id") == data["run"]["issue_id"]
                    and issue.get("workspace_id") == identity["MULTICA_WORKSPACE_ID"]):
                identifier = issue.get("identifier")
        except RunContextError:
            pass
    if workspace_slug is None:
        try:
            workspace = query(cli + ["workspace", "get", identity["MULTICA_WORKSPACE_ID"], "--output", "json"])
            if isinstance(workspace, dict) and workspace.get("id") == identity["MULTICA_WORKSPACE_ID"]:
                workspace_slug = workspace.get("slug")
        except RunContextError:
            pass
    if app_url is None:
        try:
            # config show 暂无 JSON 输出；只读取完整配置键，不把 API 地址猜成网页地址。
            config = query(cli + ["config", "show"], text=True)
            values = dict(re.findall(r"^(server_url|app_url):[ \t]*(.+)$", config, re.M))
            if values.get("server_url", "").strip().rstrip("/") == identity["MULTICA_SERVER_URL"].rstrip("/"):
                app_url = values.get("app_url", "").strip()
        except RunContextError:
            pass
    return identifier, workspace_slug, app_url


def issue_link(data, identifier, workspace_slug, app_url):
    # 参数已由调用方提供或通过当前任务配置补齐，此处只组装链接。
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,31}-[1-9][0-9]{0,15}", identifier):
        return {}
    if not isinstance(workspace_slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", workspace_slug):
        return {}
    if len(workspace_slug) > 100 or not isinstance(app_url, str):
        return {}
    try:
        issue_id = str(UUID(data["run"]["issue_id"]))
        origin = urlsplit(app_url)
        if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                or origin.password is not None or origin.path not in ("", "/")
                or origin.query or origin.fragment or origin.port == 0
                or re.search(r"[\s<>|\\]", app_url)):
            return {}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}
    return {"issue_identifier": identifier,
            "issue_url": f"{origin.scheme}://{origin.netloc}/{workspace_slug}/issues/{issue_id}"}


def summarize(data, issue_identifier=None, workspace_slug=None, app_url=None):
    messages = valid_messages(data)
    return {"version": 1, "run_id": data["run"]["id"], "issue_id": data["run"]["issue_id"],
            **issue_link(data, issue_identifier, workspace_slug, app_url),
            "captured_at": data["captured_at"], "last_message_seq": messages[-1]["seq"] if messages else None,
            "statistics": statistics(data), "code_evidence": code_evidence(messages)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--issue-identifier", help="已有任务详情中的编号，例如 GRM-87")
    parser.add_argument("--workspace-slug", help="已确认的当前工作区 slug，例如 grm")
    parser.add_argument("--app-url", help="已确认的 Multica 网页根地址，必须为 HTTPS")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        data = snapshot(args.issue, os.environ)
        link_args = link_context(data, args.issue_identifier, args.workspace_slug, args.app_url, os.environ)
        summary = summarize(data, *link_args)
        write_new(args.output, summary)
    except Exception as error:
        print(json.dumps({"error": str(error) if isinstance(error, RunContextError) else "运行资料整理失败，请检查 CLI 返回和输出路径"}, ensure_ascii=False))
        return 1
    print(json.dumps({"output": args.output, "statistics": summary["statistics"],
                      "evidence_count": len(summary["code_evidence"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
