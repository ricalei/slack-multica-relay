"""Specialist workers must return evidence analysis, never inherit delivery authority."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "readonly_reviewer", Path(__file__).parents[1] / "scripts/readonly-reviewer.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class ReadonlyReviewerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.packet = Path(self.directory.name) / "packet.md"
        self.packet.write_text("PR 1, head abc, base def\n完整 diff 与上下文", encoding="utf-8")
        self.env = {
            "HOME": "/original/home", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
            "CODEX_HOME": "/original/codex", "TMPDIR": self.directory.name,
            "SLACK_TOKEN": "private-slack", "GH_TOKEN": "private-gh",
            "MULTICA_TASK_ID": "parent-run", "MULTICA_API_TOKEN": "private-multica",
            "CODEX_THREAD_ID": "parent-thread", "OPENAI_API_KEY": "private-openai",
            "HTTPS_PROXY": "http://private-proxy", "RELAY_IGNORE_TOKEN": "private-ignore",
        }
        self.result = {"status": "complete", "findings": [], "summary": "No findings.", "evidence_gaps": []}
        self.process = Mock(pid=34567, returncode=0)
        self.process.communicate.return_value = (None, None)

    def launch(self, result=None, **kwargs):
        def start(command, **options):
            self.command, self.options = command, options
            self.workdir = Path(options["cwd"])
            self.assertNotEqual(self.workdir, self.packet.parent)
            if result is not False:
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps(self.result if result is None else result), encoding="utf-8")
            return self.process
        with patch.object(worker.subprocess, "Popen", side_effect=start):
            return worker.review(self.packet, "security", "current-model", env=self.env,
                                 codex="/verified/bin/codex", **kwargs)

    def test_locked_tools_context_and_environment(self):
        result = self.launch()
        command = self.command
        self.assertEqual(command[0:3], ["/verified/bin/codex", "-a", "never"])
        for feature in ("multi_agent", "multi_agent_v2", "shell_tool", "code_mode_host", "apps", "plugins", "memories", "request_permissions_tool"):
            self.assertIn(["--disable", feature], [command[i:i+2] for i in range(len(command)-1)])
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check"):
            self.assertIn(flag, command)
        self.assertIn(["--sandbox", "read-only"], [command[i:i+2] for i in range(len(command)-1)])
        self.assertIn('web_search="disabled"', command)
        self.assertIn("project_doc_max_bytes=0", command)
        self.assertIn("skills.include_instructions=false", command)
        self.assertIn("skills.bundled.enabled=false", command)
        self.assertIn("--output-schema", command)
        self.assertNotIn("resume", command)
        self.assertNotIn("fork", command)
        self.assertEqual(command[-1], "-")
        self.assertEqual(self.options["env"], {key: self.env[key] for key in ("HOME", "PATH", "LANG", "CODEX_HOME", "TMPDIR")})
        self.assertTrue(self.options["start_new_session"])
        self.assertIn("完整 diff 与上下文", self.process.communicate.call_args.args[0])
        self.assertEqual(result["role"], "security")
        self.assertEqual(len(result["packet_sha256"]), 64)
        self.assertEqual(result["result"], self.result)
        self.assertFalse(self.workdir.exists())

    def test_invalid_packets_never_launch(self):
        symlink = self.packet.parent / "link.md"
        symlink.symlink_to(self.packet)
        fifo = self.packet.parent / "fifo"
        os.mkfifo(fifo)
        for target, content in ((self.packet, b""), (self.packet, b"\xff"),
                                (self.packet, b"x" * (2 * 1024 * 1024 + 1)),
                                (symlink, None), (fifo, None), (self.packet.parent, None),
                                (self.packet.parent / "absent", None)):
            with self.subTest(target=target, content_size=len(content) if content else 0):
                if content is not None:
                    self.packet.write_bytes(content)
                with patch.object(worker.subprocess, "Popen") as start:
                    with self.assertRaises(worker.ReviewerError):
                        worker.review(target, "intent", "current-model", env=self.env, codex="/verified/codex")
                    start.assert_not_called()

    def test_invalid_options_never_launch(self):
        for role, model, timeout in (("unknown", "model", 20), ("intent", "", 20),
                                     ("intent", "--yolo\n", 20), ("intent", "model", 0),
                                     ("intent", "model", 1801)):
            with self.subTest(role=role, model=model, timeout=timeout), patch.object(worker.subprocess, "Popen") as start:
                with self.assertRaises(worker.ReviewerError):
                    worker.review(self.packet, role, model, timeout=timeout, env=self.env, codex="/verified/codex")
                start.assert_not_called()

    def test_incomplete_is_preserved_not_promoted_to_clean(self):
        value = dict(self.result, status="incomplete", evidence_gaps=["missing caller context"])
        self.assertEqual(self.launch(value)["result"]["status"], "incomplete")

    def test_invalid_or_missing_result_fails_closed(self):
        for value in (False, [], {}, dict(self.result, status="approved"),
                      dict(self.result, findings="No findings"), dict(self.result, evidence_gaps=["missing"]),
                      dict(self.result, findings=[{"priority": "P2"}])):
            with self.subTest(value=value):
                with self.assertRaises(worker.ReviewerError):
                    self.launch(value)

    def test_nonzero_exit_never_returns_even_valid_result(self):
        self.process.returncode = 1
        with self.assertRaises(worker.ReviewerError):
            self.launch()

    def test_timeout_kills_only_worker_group_and_never_retries(self):
        self.process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 10), (None, None)]
        with patch.object(worker.os, "killpg") as kill:
            with self.assertRaises(worker.ReviewerError):
                self.launch(timeout=10)
        kill.assert_called_once_with(self.process.pid, signal.SIGTERM)
        self.assertEqual(self.process.communicate.call_count, 2)

    def test_timeout_escalates_only_same_process_group(self):
        self.process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 10),
                                                subprocess.TimeoutExpired("codex", 5), (None, None)]
        with patch.object(worker.os, "killpg") as kill:
            with self.assertRaises(worker.ReviewerError):
                self.launch(timeout=10)
        self.assertEqual([c.args for c in kill.call_args_list],
                         [(self.process.pid, signal.SIGTERM), (self.process.pid, signal.SIGKILL)])

    def test_interruption_also_stops_worker(self):
        self.process.communicate.side_effect = [KeyboardInterrupt(), (None, None)]
        with patch.object(worker.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                self.launch()
        kill.assert_called_once_with(self.process.pid, signal.SIGTERM)

    def test_sigterm_cancels_helper_and_its_detached_worker(self):
        binary = self.packet.parent / "bin"
        binary.mkdir()
        ready = self.packet.parent / "worker.pid"
        executable = binary / "codex"
        executable.write_text(
            f"#!{sys.executable}\nimport os, pathlib, time\n"
            f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\ntime.sleep(120)\n")
        executable.chmod(0o700)
        command = [sys.executable, str(Path(worker.__file__)), "--packet", str(self.packet),
                   "--role", "intent", "--model", "synthetic-model"]
        helper = subprocess.Popen(command, env={"HOME": self.directory.name, "PATH": str(binary)},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  start_new_session=True)
        worker_pid = None
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and helper.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "synthetic worker did not start")
            worker_pid = int(ready.read_text())
            os.killpg(helper.pid, signal.SIGTERM)
            stdout, _ = helper.communicate(timeout=5)
            self.assertNotEqual(helper.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(worker_pid, 0)
            self.assertEqual(json.loads(stdout)["status"], "blocked")
        finally:
            if helper.poll() is None:
                os.killpg(helper.pid, signal.SIGKILL)
                helper.communicate()
            if worker_pid is not None:
                try:
                    os.killpg(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
