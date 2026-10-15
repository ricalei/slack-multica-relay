"""验证运行资料整理的归属、统计和代码证据。"""
import importlib.util
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("run_context", Path(__file__).parents[1] / "multica-skills/multica-final-reply/scripts/run_context.py")
final = importlib.util.module_from_spec(spec)
spec.loader.exec_module(final)


class FinalReplyTests(unittest.TestCase):
    def setUp(self):
        self.env = {"MULTICA_TASK_ID": "run", "MULTICA_WORKSPACE_ID": "ws", "MULTICA_SERVER_URL": "https://multica.example"}
        self.run = {"id": "run", "issue_id": "issue", "workspace_id": "ws", "started_at": "2026-09-09T00:00:00Z"}
        self.data = {"scope": self.env, "run": self.run, "captured_at": "2026-09-09T00:01:02+00:00", "messages": []}

    def message(self, kind, **fields):
        self.data["messages"].append({"seq": len(self.data["messages"]) + 1, "type": kind, "task_id": "run", "issue_id": "issue", **fields})

    def test_query_runs_without_a_command_wrapper(self):
        with patch.dict(final.os.environ, {"PATH": ""}):
            self.assertEqual(final.query([sys.executable, "-c", 'print(\'{"status": "ok"}\')']),
                             {"status": "ok"})
            self.assertEqual(final.query([sys.executable, "-c", 'print("server_url: https://example.test")'], text=True),
                             "server_url: https://example.test\n")

    def test_query_reports_cli_failures_without_exposing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = [
                [str(Path(directory) / "missing-cli")],
                [sys.executable, "-c", 'raise SystemExit("private CLI error")'],
                [sys.executable, "-c", 'print("private invalid JSON")'],
            ]
            for command in commands:
                with self.subTest(command=command), self.assertRaises(final.RunContextError) as error:
                    final.query(command)
                self.assertNotIn("private", str(error.exception))

    def test_snapshot_selects_current_not_latest_and_omits_private_metadata(self):
        self.run["attribution"] = {"email": "private@example"}
        with patch.object(final, "query", side_effect=[[{"id": "other"}, self.run], []]) as query:
            data = final.snapshot("issue", self.env)
        self.assertEqual(data["run"]["id"], "run")
        self.assertNotIn("attribution", data["run"])
        self.assertIn("run", query.call_args.args[0])

    def test_scope_mismatch_does_not_fetch_messages(self):
        self.run["workspace_id"] = "other"
        with patch.object(final, "query", return_value=[self.run]) as query:
            with self.assertRaises(final.RunContextError):
                final.snapshot("issue", self.env)
            self.assertEqual(query.call_count, 1)

    def test_model_comes_from_current_run_agent_config(self):
        self.run.update(agent_id="agent-current", model="old-run-model")
        agent = {"id": "agent-current", "workspace_id": "ws", "model": " gpt-6-astra ", "instructions": "private"}
        with patch.object(final, "query", side_effect=[[self.run], agent, []]) as query:
            data = final.snapshot("issue", self.env)
        self.assertEqual(data["run"]["model"], "gpt-6-astra")
        self.assertEqual(data["run"]["model_source"], "agent_config")
        self.assertNotIn("instructions", data["run"])
        self.assertIn("agent-current", query.call_args_list[1].args[0])
        self.assertEqual(final.statistics(data)["model"], "gpt-6-astra")

    def test_service_tier_comes_from_same_agent_query_as_model(self):
        self.run.update(agent_id="agent-current", service_tier="stale-run-tier")
        for tier in ("priority", "default"):
            for messages in ([], final.RunContextError("日志不可用")):
                agent = {"id": "agent-current", "workspace_id": "ws", "model": "gpt-6-astra", "service_tier": tier}
                with self.subTest(tier=tier, messages=messages), patch.object(final, "query", side_effect=[[self.run], agent, messages]) as query:
                    data = final.snapshot("issue", self.env)
                stats = final.summarize(data)["statistics"]
                self.assertEqual(stats["service_tier"], tier)
                self.assertEqual(stats["service_tier_source"], "agent_config")
                self.assertEqual(stats["model"], "gpt-6-astra")
                self.assertEqual(query.call_count, 3)

    def test_missing_or_unknown_service_tier_keeps_model_without_fallback(self):
        self.run.update(agent_id="agent-current", service_tier="priority")
        for fields in ({}, *({"service_tier": value} for value in (None, "", "fast", " priority ", [], {}, True))):
            agent = {"id": "agent-current", "workspace_id": "ws", "model": "gpt-6-astra", **fields}
            with self.subTest(fields=fields), patch.object(final, "query", side_effect=[[self.run], agent, []]):
                data = final.snapshot("issue", self.env)
            stats = final.statistics(data)
            self.assertEqual(stats["model"], "gpt-6-astra")
            self.assertNotIn("service_tier", stats)
            self.assertNotIn("service_tier_source", stats)

    def test_service_tier_requires_valid_current_agent_and_model(self):
        self.run.update(agent_id="agent-current", service_tier="priority")
        agent = {"id": "agent-current", "workspace_id": "ws", "model": "gpt-6-astra", "service_tier": "priority"}
        invalid = [final.RunContextError("读取失败"), None,
                   {**agent, "id": "other"}, {**agent, "workspace_id": "other"},
                   {**agent, "model": None}, {**agent, "model": "<!here>"}]
        for candidate in invalid:
            with self.subTest(agent=candidate), patch.object(final, "query", side_effect=[[self.run], candidate, []]):
                data = final.snapshot("issue", self.env)
            self.assertNotIn("service_tier", data["run"])
            self.assertNotIn("service_tier", final.statistics(data))
            self.assertIn("duration_seconds", final.statistics(data))
        self.run.pop("agent_id")
        with patch.object(final, "query", side_effect=[[self.run], []]):
            self.assertNotIn("service_tier", final.statistics(final.snapshot("issue", self.env)))

    def test_statistics_omits_unknown_tier_and_tier_without_model(self):
        for fields in ({"model": "gpt-6-astra", "service_tier": "fast"},
                       {"service_tier": "priority"}, {"model": "<!here>", "service_tier": "priority"}):
            with self.subTest(fields=fields):
                stats = final.statistics({**self.data, "run": {**self.run, **fields}})
                self.assertNotIn("service_tier", stats)
                self.assertNotIn("service_tier_source", stats)

    def test_unavailable_or_invalid_agent_config_hides_only_model(self):
        self.run.update(agent_id="agent-current", model="do-not-fallback")
        invalid = [final.RunContextError("读取失败"), None, [],
                   {"id": "other", "workspace_id": "ws", "model": "gpt-6-astra"},
                   {"id": "agent-current", "workspace_id": "other", "model": "gpt-6-astra"},
                   {"id": "agent-current", "workspace_id": "ws", "model": ""},
                   {"id": "agent-current", "workspace_id": "ws", "model": "<!here>"}]
        for agent in invalid:
            with self.subTest(agent=agent), patch.object(final, "query", side_effect=[[self.run], agent, []]) as query:
                data = final.snapshot("issue", self.env)
                self.assertNotIn("model", data["run"])
                self.assertEqual(query.call_count, 3)
                self.assertIn("duration_seconds", final.statistics(data))

    def test_missing_current_run_never_uses_previous(self):
        with patch.object(final, "query", return_value=[{"id": "other"}]):
            with self.assertRaises(final.RunContextError):
                final.snapshot("issue", self.env)

    def test_log_failure_still_has_duration_without_invented_counts(self):
        with patch.object(final, "query", side_effect=[[self.run], final.RunContextError("失败")]):
            data = final.snapshot("issue", self.env)
        self.assertEqual(data["messages"], [])
        self.assertEqual(final.statistics(self.data), {"duration_seconds": 62})

    def test_issue_link_reuses_current_issue_without_extra_queries(self):
        issue_id = "00000000-0000-4000-8000-000000000001"
        self.run.update(issue_id=issue_id, agent_id="agent-current")
        agent = {"id": "agent-current", "workspace_id": "ws", "model": "gpt-6-astra"}
        with patch.object(final, "query", side_effect=[[self.run], agent, []]) as query:
            data = final.snapshot(issue_id, self.env)
            summary = final.summarize(data, "GRM-87", "grm", "https://multica.example/")
            self.assertEqual(query.call_count, 3)
        self.assertEqual(summary["issue_identifier"], "GRM-87")
        self.assertEqual(summary["issue_url"], f"https://multica.example/grm/issues/{issue_id}")
        self.assertEqual(summary["run_id"], "run")
        self.assertEqual(summary["statistics"]["model"], "gpt-6-astra")

    def test_issue_link_requires_all_display_context(self):
        self.run["issue_id"] = "00000000-0000-4000-8000-000000000001"
        for args in ((None, "grm", "https://multica.example"),
                     ("GRM-87", None, "https://multica.example"),
                     ("GRM-87", "grm", None), (None, None, None)):
            with self.subTest(args=args):
                summary = final.summarize(self.data, *args)
                self.assertNotIn("issue_url", summary)
                self.assertNotIn("issue_identifier", summary)
                self.assertEqual(summary["statistics"]["duration_seconds"], 62)

    def test_invalid_issue_context_hides_only_link(self):
        self.run["issue_id"] = "00000000-0000-4000-8000-000000000001"
        cases = [(value, "grm", "https://multica.example") for value in
                 ("#GRM-87", "GRM-0", "GRM-87|<!here>", "GRM-87\n", "", 87)]
        cases += [("GRM-87", value, "https://multica.example") for value in
                  ("../other", "grm/issues", "grm?x=1", "", "x" * 101)]
        cases += [("GRM-87", "grm", value) for value in
                  ("javascript:alert(1)", "http://multica.example", "https://name:password@multica.example",
                   "https://multica.example/api", "https://multica.example/?token=secret",
                   "https://multica.example/#section", "https://multica.example|other",
                   "https://multica.example:invalid", "https://multica.example:0",
                   "https://multica.example\n", "https://[invalid", "https://", "")]
        for args in cases:
            with self.subTest(args=args):
                summary = final.summarize(self.data, *args)
                self.assertNotIn("issue_url", summary)
                self.assertNotIn("issue_identifier", summary)
                self.assertEqual(summary["statistics"]["duration_seconds"], 62)

    def test_issue_link_uses_explicit_web_origin_and_workspace(self):
        self.run["issue_id"] = "00000000-0000-4000-8000-000000000001"
        self.env["MULTICA_SERVER_URL"] = "https://api.example"
        summary = final.summarize(self.data, "LAB-5", "winches-lab", "https://web.example:8443")
        self.assertEqual(summary["issue_url"],
                         "https://web.example:8443/winches-lab/issues/00000000-0000-4000-8000-000000000001")
        self.assertEqual(summary["issue_identifier"], "LAB-5")

    def test_invalid_issue_id_never_becomes_link_path(self):
        for issue_id in ("run", "../other", "", None):
            with self.subTest(issue_id=issue_id):
                self.run["issue_id"] = issue_id
                summary = final.summarize(self.data, "GRM-87", "grm", "https://multica.example")
                self.assertNotIn("issue_url", summary)

    def test_issue_link_survives_unavailable_run_messages(self):
        issue_id = "00000000-0000-4000-8000-000000000001"
        self.run["issue_id"] = issue_id
        with patch.object(final, "query", side_effect=[[self.run], final.RunContextError("日志不可用")]):
            data = final.snapshot(issue_id, self.env)
        summary = final.summarize(data, "GRM-87", "grm", "https://multica.example")
        self.assertIn("issue_url", summary)
        self.assertNotIn("tools", summary["statistics"])

    def test_cli_persists_issue_link_with_statistics(self):
        issue_id = "00000000-0000-4000-8000-000000000001"
        self.run["issue_id"] = issue_id
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "context.json")
            argv = ["run_context.py", "--issue", issue_id, "--issue-identifier", "GRM-87",
                    "--workspace-slug", "grm", "--app-url", "https://multica.example", "--output", output]
            with patch("sys.argv", argv), patch.dict(final.os.environ, self.env), \
                    patch.object(final, "query", side_effect=[[self.run], []]) as query, patch("builtins.print"):
                self.assertEqual(final.main(), 0)
                self.assertEqual(query.call_count, 2)
            saved = final.json.loads(Path(output).read_text())
            self.assertEqual(saved["issue_identifier"], "GRM-87")
            self.assertTrue(saved["issue_url"].endswith("/" + issue_id))
            self.assertIn("duration_seconds", saved["statistics"])

    def test_missing_link_parameters_are_resolved_in_current_scope(self):
        issue = {"id": "issue", "workspace_id": "ws", "identifier": "GRM-99"}
        workspace = {"id": "ws", "slug": "grm"}
        config = "server_url: https://multica.example\napp_url: https://web.example\n"
        with patch.object(final, "query", side_effect=[issue, workspace, config]) as query:
            self.assertEqual(final.link_context(self.data), ("GRM-99", "grm", "https://web.example"))
        self.assertEqual(query.call_count, 3)
        for call in query.call_args_list:
            self.assertEqual(call.args[0][:5], ["multica", "--server-url", "https://multica.example", "--workspace-id", "ws"])

    def test_explicit_link_parameters_skip_discovery(self):
        with patch.object(final, "query") as query:
            self.assertEqual(final.link_context(self.data, "LAB-5", "lab", "https://web.example"),
                             ("LAB-5", "lab", "https://web.example"))
            query.assert_not_called()

    def test_foreign_or_missing_link_context_is_not_used(self):
        cases = [
            [{"id": "other", "workspace_id": "ws", "identifier": "GRM-99"},
             {"id": "other", "slug": "grm"}, "server_url: https://other.example\napp_url: https://web.example\n"],
            [{"id": "issue", "workspace_id": "other", "identifier": "GRM-99"}, None, ""],
            [final.RunContextError("读取失败")] * 3,
        ]
        for responses in cases:
            with self.subTest(responses=responses), patch.object(final, "query", side_effect=responses):
                args = final.link_context(self.data)
                self.assertFalse(args[0])
                self.assertFalse(args[1])
                self.assertFalse(args[2])
                summary = final.summarize(self.data, *args)
                self.assertNotIn("issue_url", summary)
                self.assertEqual(summary["statistics"]["duration_seconds"], 62)

    def test_cli_grm99_missing_slug_and_app_url_regression(self):
        issue_id = "00000000-0000-4000-8000-000000000001"
        self.run["issue_id"] = issue_id
        workspace = {"id": "ws", "slug": "grm"}
        config = "server_url: https://multica.example\napp_url: https://web.example\n"
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "context.json")
            argv = ["run_context.py", "--issue", issue_id, "--issue-identifier", "GRM-99", "--output", output]
            with patch("sys.argv", argv), patch.dict(final.os.environ, self.env, clear=True), \
                    patch.object(final, "query", side_effect=[[self.run], [], workspace, config]) as query, patch("builtins.print"):
                self.assertEqual(final.main(), 0)
                self.assertEqual(query.call_count, 4)
            saved = final.json.loads(Path(output).read_text())
            self.assertEqual(saved["issue_identifier"], "GRM-99")
            self.assertEqual(saved["issue_url"], f"https://web.example/grm/issues/{issue_id}")
            self.assertIn("duration_seconds", saved["statistics"])

    def test_runtime_link_environment_does_not_read_personal_config(self):
        env = {"FINAL_REPLY_APP_URL": "https://web.example", "FINAL_REPLY_WORKSPACE_SLUG": "grm"}
        with patch.object(final, "query") as query:
            self.assertEqual(final.link_context(self.data, "GRM-100", env=env),
                             ("GRM-100", "grm", "https://web.example"))
            query.assert_not_called()

    def test_explicit_link_arguments_override_runtime_environment(self):
        env = {"FINAL_REPLY_APP_URL": "https://other.example", "FINAL_REPLY_WORKSPACE_SLUG": "other"}
        with patch.object(final, "query") as query:
            self.assertEqual(final.link_context(self.data, "LAB-5", "lab", "https://web.example", env),
                             ("LAB-5", "lab", "https://web.example"))
            query.assert_not_called()

    def test_cli_grm100_isolated_config_uses_runtime_environment(self):
        issue_id = "00000000-0000-4000-8000-000000000001"
        self.run["issue_id"] = issue_id
        issue = {"id": issue_id, "workspace_id": "ws", "identifier": "GRM-100"}
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "context.json")
            env = {**self.env, "MULTICA_TASK_CONFIG_ROOT": directory,
                   "FINAL_REPLY_APP_URL": "https://web.example", "FINAL_REPLY_WORKSPACE_SLUG": "grm"}
            argv = ["run_context.py", "--issue", issue_id, "--output", output]
            with patch("sys.argv", argv), patch.dict(final.os.environ, env, clear=True), \
                    patch.object(final, "query", side_effect=[[self.run], [], issue]) as query, patch("builtins.print"):
                self.assertEqual(final.main(), 0)
                self.assertEqual(query.call_count, 3)
                self.assertFalse(any("config" in call.args[0] for call in query.call_args_list))
            saved = final.json.loads(Path(output).read_text())
            self.assertEqual(saved["issue_identifier"], "GRM-100")
            self.assertEqual(saved["issue_url"], f"https://web.example/grm/issues/{issue_id}")

    def test_skill_names_deduplicate_and_tokens_never_render(self):
        self.run.update(model="gpt-6-astra", usage=[{"input_tokens": 999}])
        for _ in range(2):
            self.message("tool_use", tool="exec_command", input={"command": "/bin/zsh -lc 'cat /skills/slack/SKILL.md'"})
            self.message("tool_result", tool="exec_command", output="---\nname: slack\ndescription: test\n---\n正文")
        stats = final.statistics(self.data)
        self.assertEqual(stats["tools"], 2)
        self.assertEqual(stats["skills"], 1)
        self.assertEqual(stats["model"], "gpt-6-astra")
        self.assertNotIn("token", stats)

    def test_missing_or_foreign_log_sequence_hides_counts(self):
        self.message("tool_use", tool="exec_command")
        self.data["messages"][0]["task_id"] = "other"
        self.assertNotIn("tools", final.statistics(self.data))
        self.data["messages"][0].update(task_id="run", seq=2)
        self.assertNotIn("tools", final.statistics(self.data))

    def test_concurrent_duplicate_reads_count_one_name(self):
        for _ in range(2):
            self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/slack/SKILL.md"})
        for _ in range(2):
            self.message("tool_result", tool="exec_command", output="---\nname: slack\n---\n")
        self.assertEqual(final.statistics(self.data)["skill_names"], ["slack"])

    def test_parallel_skill_results_can_arrive_in_reverse_order(self):
        for name in ("github-workflow", "multica-final-reply"):
            self.message("tool_use", tool="exec_command", input={"cmd": f"cat /skills/{name}/SKILL.md"})
        for name in ("multica-final-reply", "github-workflow"):
            self.message("tool_result", tool="exec_command", output=f"---\nname: {name}\n---\n正文")
        self.assertEqual(final.statistics(self.data)["skills"], 2)

    def test_parallel_reference_read_does_not_hide_skill(self):
        for command in ("cat /skills/slack/references/format.md", "cat /skills/final/SKILL.md"):
            self.message("tool_use", tool="exec_command", input={"cmd": command})
        self.message("tool_result", tool="exec_command", output="# 格式参考\n---\nname: example\n---\n")
        self.message("tool_result", tool="exec_command", output="---\nname: final\n---\n正文")
        self.assertEqual(final.statistics(self.data)["skill_names"], ["final"])

    def test_failed_unresolved_and_search_calls_preserve_confirmed_names(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/slack/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="---\nname: slack\n---\n")
        for command, output in (
            ("cat /skills/missing/SKILL.md", "cat: /skills/missing/SKILL.md: No such file or directory"),
            ("rg --files -g SKILL.md /skills", "/skills/missing/SKILL.md"),
            ("cat /work/source.md", "---\nname: ordinary-doc\n---\n"),
            ("cat /skills/truncated/SKILL.md", "---\nname: truncated\n"),
        ):
            self.message("tool_use", tool="exec_command", input={"cmd": command})
            self.message("tool_result", tool="exec_command", output=output)
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/pending/SKILL.md"})
        self.assertEqual(final.statistics(self.data)["skill_names"], ["slack"])

    def test_parallel_failed_read_does_not_hide_successful_read(self):
        for name in ("missing", "slack"):
            self.message("tool_use", tool="exec_command", input={"cmd": f"cat /skills/{name}/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="---\nname: slack\n---\n")
        self.message("tool_result", tool="exec_command", output="cat: /skills/missing/SKILL.md: Permission denied")
        self.assertEqual(final.statistics(self.data)["skill_names"], ["slack"])

    def test_skill_body_examples_are_not_additional_skills(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/slack/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="---\nname: slack\n---\n示例\n```yaml\n---\nname: fake\n---\n```")
        self.assertEqual(final.statistics(self.data)["skill_names"], ["slack"])

    def test_parallel_unmatched_name_and_unfinished_batch_are_omitted(self):
        for name in ("a", "b"):
            self.message("tool_use", tool="exec_command", input={"cmd": f"cat /skills/{name}/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="---\nname: a\n---\n")
        self.assertNotIn("skills", final.statistics(self.data))
        self.message("tool_result", tool="exec_command", output="---\nname: unrelated\n---\n")
        self.assertEqual(final.statistics(self.data)["skill_names"], ["a"])

    def test_batch_skill_reads_require_all_headers(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/a/SKILL.md /skills/b/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="---\nname: a\n---\n正文\n---\nname: b\n---\n正文")
        self.assertEqual(final.statistics(self.data)["skills"], 2)
        self.data["messages"][-1]["output"] = "---\nname: a\n---\n部分输出"
        self.assertNotIn("skills", final.statistics(self.data))

    def test_batch_partial_output_cannot_count_a_body_example(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/a/SKILL.md /skills/b/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="---\nname: a\n---\n示例\n---\nname: example\n---\n")
        self.assertNotIn("skills", final.statistics(self.data))

    def test_extracts_pr_and_branch_evidence_without_querying_github(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo --json url,headRefName"})
        self.message("tool_result", tool="exec_command", output='{"url":"https://github.com/owner/repo/pull/12","headRefName":"feature/a"}')
        self.message("tool_use", tool="exec_command", input={"cmd": "git -C /work/repo branch --show-current"})
        self.message("tool_result", tool="exec_command", output="feature/a\n")
        with patch.object(final, "query") as query:
            result = final.summarize(self.data)
            query.assert_not_called()
        self.assertEqual(result["code_evidence"][0]["call_seq"], 1)
        self.assertEqual(result["code_evidence"][0]["result_seq"], 2)
        self.assertIn("headRefName", result["code_evidence"][0]["output"])
        self.assertEqual(result["code_evidence"][1]["output"], "feature/a\n")

    def test_document_examples_are_not_code_evidence(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/github/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="示例 https://github.com/owner/repo/pull/12")
        self.assertEqual(final.summarize(self.data)["code_evidence"], [])

    def test_concurrent_results_are_not_assigned_to_wrong_pr(self):
        for number in (12, 15):
            self.message("tool_use", tool="exec_command", input={"cmd": f"gh pr view {number} --repo owner/repo"})
        for number in (15, 12):
            self.message("tool_result", tool="exec_command", output=f"PR {number}")
        rows = final.summarize(self.data)["code_evidence"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("output" not in row for row in rows))

    def test_long_output_is_explicitly_marked_incomplete(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo"})
        self.message("tool_result", tool="exec_command", output="x" * 5000)
        row = final.summarize(self.data)["code_evidence"][0]
        self.assertTrue(row["output_truncated"])
        self.assertEqual(len(row["output"]), 4000)

    def test_foreign_messages_do_not_leak_into_summary(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo"})
        self.data["messages"][0]["issue_id"] = "other"
        summary = final.summarize(self.data)
        self.assertEqual(summary["code_evidence"], [])
        self.assertIsNone(summary["last_message_seq"])


if __name__ == "__main__":
    unittest.main()
