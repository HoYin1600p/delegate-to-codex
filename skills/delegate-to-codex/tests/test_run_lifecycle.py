"""A run from launch to result: the worker segment, extensions, revisions, interruptions and recovery.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import BridgeCase, EXHAUSTED, NT, SKILL, git, make_junction, rewrite_dotgit, run_in_threads
import fake_codex
from claude_codex_bridge import bridge, cli, codexcli, gitops, process, state
from claude_codex_bridge.contracts import ContractError, load_task, validate_task


class Runs(BridgeCase):
    def test_validation_failure_with_unrelated_primary_change_remains_revisable(self):
        task = self.task(validation_command=[sys.executable, "-c", "import sys; sys.exit(1)"])
        segment = bridge._segment

        def change_primary(*args, **kwargs):
            (self.repo / "unrelated.txt").write_text("lead edit\n", encoding="utf-8")
            return segment(*args, **kwargs)

        with mock.patch.object(bridge, "_segment", side_effect=change_primary):
            first = bridge.run(task)
        self.assertEqual(first["failures"], ["independent validation failed"])
        self.assertFalse(first["primary_checkout_unchanged"])
        feedback = self.tmp / "feedback.json"
        feedback.write_text(json.dumps({"findings": [{"path": "hello.py", "issue": "No main guard",
                                                     "expected_behavior": "Add a main guard"}]}))
        revised = bridge.revise_task(task, Path(first["artifact_directory"]), feedback, 3)
        self.assertEqual(revised["revisions"][0]["target_state"], "validation_failed")
        self.assertEqual(revised["failures"], ["independent validation failed"])

    def test_parallel_tasks_share_repo_with_outside_primary_warning(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "parallel"
        tasks = [self.task(task_id=f"parallel-{i}", allowed_changed_paths=[f"hello-{i}.py"],
                           validation_command=[sys.executable, "-B", "-c", "pass"]) for i in range(2)]
        # Both runs create a worktree and start Codex's account check before they meet, which can take a while on a
        # loaded machine, hence the generous timeout; a failure in either run aborts the barrier at once.
        barrier = threading.Barrier(2, action=lambda: (self.repo / "lead notes.txt").write_text("lead edit\n", encoding="utf-8"),
                                    timeout=180)
        segment = bridge._segment

        def together(*args, **kwargs):
            barrier.wait()
            return segment(*args, **kwargs)

        with mock.patch.object(bridge, "_segment", side_effect=together):
            results = run_in_threads(bridge.run, tasks, barrier)
        for i, result in enumerate(results):
            self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
            self.assertEqual(result["changed_paths"], [f"hello-{i}.py"])
            self.assertFalse(result["primary_checkout_unchanged"])
            self.assertIn("primary checkout changed outside the task's paths during delegation "
                          "(another worker, the lead or the user): lead notes.txt", result["warnings"])
        self.assertEqual(len({r["worktree"] for r in results}), 2)
        branches = {git(Path(r["worktree"]), "branch", "--show-current").strip() for r in results}
        self.assertEqual(len(branches), 2)

    def test_primary_change_inside_allowed_paths_blocks(self):
        segment = bridge._segment

        def change_primary(*args, **kwargs):
            (self.repo / "hello.py").write_text("lead edit\n", encoding="utf-8")
            return segment(*args, **kwargs)

        task = self.task()
        with mock.patch.object(bridge, "_segment", side_effect=change_primary):
            result = bridge.run(task)
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertIn("primary checkout changed inside the task's allowed paths: hello.py", result["failures"])

    def test_continue_and_revise_allow_unrelated_primary_changes(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        task = self.task()
        first = bridge.run(task)
        (self.repo / "unrelated.txt").write_text("lead edit\n", encoding="utf-8")
        continued = bridge.continue_task(task, Path(first["artifact_directory"]), 4)
        self.assertEqual(continued["lifecycle_status"], "REVIEW_PENDING", continued["failures"])
        feedback = self.tmp / "feedback.json"
        feedback.write_text(json.dumps({"findings": [{"path": "hello.py", "issue": "No main guard",
                                                     "expected_behavior": "Add a main guard"}]}))
        revised = bridge.revise_task(task, Path(first["artifact_directory"]), feedback, 3)
        self.assertEqual(revised["lifecycle_status"], "REVIEW_PENDING", revised["failures"])
        self.assertTrue(any("unrelated.txt" in w for w in revised["warnings"]))
        self.assertEqual(bridge.revalidate(task, Path(first["artifact_directory"]))["status"], "passed")

    def test_hello_world_end_to_end(self):
        result = bridge.run(self.task(), model="gpt-6.1-sol", effort="medium")
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["changed_paths"], ["hello.py"])
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertTrue(result["primary_checkout_unchanged"])
        self.assertFalse((self.repo / "hello.py").exists())
        self.assertEqual(result["observed_metrics"]["tokens"]["output_tokens"], 80)
        exec_call = next(c for c in self.calls() if c["argv"][0] == "exec")
        argv = exec_call["argv"]
        self.assertIn("--ignore-user-config", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertIn("--output-schema", argv)
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        self.assertNotIn("minimum", json.dumps(schema))
        self.assertEqual(result["requested_model"], "gpt-6.1-sol")
        self.assertIsNone(json.loads((Path(result["artifact_directory"]) / "task.json").read_text(encoding="utf-8")).get("model"))
        app_server_calls = [c for c in self.calls() if c["argv"][:1] == ["app-server"]]
        self.assertEqual(len(app_server_calls), 1, "post-run usage reuses the preflight cache away from exhaustion")

    def test_out_of_scope_change_blocks(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "outofscope"
        result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertIn("extra.txt", result["unauthorized_changed_paths"])

    def test_forbidden_tool_blocks(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "websearch"
        result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertTrue(any("forbidden" in f for f in result["failures"]))

    def test_policy_rejection_is_named(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "policy"
        result = bridge.run(self.task())
        self.assertEqual(result["error_kind"], "sandbox_policy_rejected")
        self.assertTrue(any("blocked by policy" in f for f in result["failures"]))

    def test_blocked_claim_blocks(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "blocked"
        result = bridge.run(self.task())
        self.assertIn("Codex reported blocked", result["failures"])

    def test_validation_failure_then_revision(self):
        result = bridge.run(self.task(validation_command=[sys.executable, "-c", "import sys; sys.exit(1)"]))
        self.assertEqual(result["failures"], ["independent validation failed"])
        feedback = self.tmp / "feedback.json"
        feedback.write_text(json.dumps({"findings": [{"path": "hello.py", "issue": "No main guard for import use",
                                                      "expected_behavior": "Wrap the print in main() behind a main guard"}]}))
        revised = bridge.revise_task(self.tmp / "task-0.json", Path(result["artifact_directory"]), feedback, 3)
        self.assertEqual(revised["revisions"][0]["target_state"], "validation_failed")
        self.assertIn("def main", (Path(revised["worktree"]) / "hello.py").read_text(encoding="utf-8"))
        resume_call = [c for c in self.calls() if c["argv"][:2] == ["exec", "resume"]][-1]
        self.assertIn(result["session_id"], resume_call["argv"])

    def test_extension_and_continue_same_session(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        task = self.task()
        first = bridge.run(task)
        self.assertEqual(first["lifecycle_status"], "EXTENSION_REQUESTED")
        with self.assertRaises(Exception):
            bridge.continue_task(task, Path(first["artifact_directory"]), 9)
        second = bridge.continue_task(task, Path(first["artifact_directory"]), 4)
        self.assertEqual(second["lifecycle_status"], "REVIEW_PENDING", second["failures"])
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(len(second["segments"]), 2)

    def test_time_cap_requests_read_only_checkpoint(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "slow"
        result = bridge.run(self.task(timeout_seconds=5))
        self.assertTrue(result["segments"][0]["time_cap_reached"])
        self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED", result["failures"])
        checkpoint = [c for c in self.calls() if c["argv"][:2] == ["exec", "resume"]][-1]
        self.assertIn('sandbox_mode="read-only"', checkpoint["argv"])

    def test_unstructured_reply_triggers_checkpoint(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "badjson"
        result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertIsNotNone(result["segments"][0]["checkpoint"])

    def test_read_only_review_task_uses_read_only_sandbox(self):
        result = bridge.run(self.task(mode="review", allowed_changed_paths=[], validation_command=None))
        argv = next(c for c in self.calls() if c["argv"][0] == "exec")["argv"]
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertEqual(result["worktree"], str(self.repo))
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])

    def test_check_task_and_launcher(self):
        self.assertEqual(bridge.check_task(self.task())["status"], "ready")
        out = subprocess.run([sys.executable, "-B", str(SKILL / "scripts" / "codex_bridge.py"), "check-task",
                              "--task", str(self.tmp / "task-0.json")], capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_state_inside_a_repository_is_refused(self):
        os.environ["DELEGATE_TO_CODEX_STATE_DIR"] = str(self.repo / "state")
        with self.assertRaises(ValueError):
            bridge.run(self.task())


class Robustness(BridgeCase):
    def test_short_worktree_path_and_recorded_location(self):
        result = bridge.run(self.task())
        artifact = Path(result["artifact_directory"])
        worktree = Path(result["worktree"])
        self.assertEqual(worktree.parent, self.state / "wt")
        self.assertRegex(worktree.name, r"^[0-9a-f]{10}$")
        self.assertEqual(json.loads((artifact / "worktree.json").read_text(encoding="utf-8"))["worktree"], str(worktree))
        self.assertEqual(next(c for c in self.calls() if c["argv"][0] == "exec")["cwd"], str(worktree))
        # Verification/cleanup must bind a short directory to this exact artifact.
        self.assertFalse(state.artifact_owns_worktree(artifact, worktree.parent / "0000000000"))

    def test_a_worktree_inside_the_artifact_folder_continues_and_cleans_up_whether_or_not_it_is_recorded(self):
        for recorded in (True, False):
            with self.subTest(recorded=recorded):
                os.environ["FAKE_CODEX_SCENARIO"] = "extension"
                task = self.task()
                first = bridge.run(task)
                artifact = Path(first["artifact_directory"])
                unrecorded = artifact / "worktree"
                git(self.repo, "worktree", "move", first["worktree"], str(unrecorded))
                if recorded:
                    first["worktree"] = str(unrecorded)
                else:
                    first.pop("worktree")
                (artifact / "result.json").write_text(json.dumps(first), encoding="utf-8")
                result = bridge.continue_task(task, artifact, 4)
                self.assertEqual(result["worktree"], str(unrecorded))
                self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
                bridge.cleanup(task, artifact)
                self.assertFalse(unrecorded.exists())

    def ignored_inputs(self):
        (self.repo / ".git" / "info" / "exclude").write_text("local.properties\nlibs/\n", encoding="utf-8")
        (self.repo / "local.properties").write_bytes(b"sdk=local\n")
        (self.repo / "libs" / "empty").mkdir(parents=True)
        (self.repo / "libs" / "one.jar").write_bytes(b"local jar")

    def test_copy_ignored_file_folder_and_glob_before_launch(self):
        self.ignored_inputs()
        segment = bridge._segment
        def inspect(*args, **kwargs):
            worktree = args[2]
            self.assertEqual((worktree / "local.properties").read_bytes(), b"sdk=local\n")
            self.assertEqual((worktree / "libs" / "one.jar").read_bytes(), b"local jar")
            self.assertTrue((worktree / "libs" / "empty").is_dir())
            return segment(*args, **kwargs)
        with mock.patch.object(bridge, "_segment", side_effect=inspect):
            result = bridge.run(self.task(copy_ignored=["local.*", "libs", "libs/*.jar"]))
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["changed_paths"], ["hello.py"])
        entries = result["copy_ignored"]
        self.assertEqual(sum(e["size_bytes"] for e in entries), len(b"sdk=local\nlocal jar"))
        self.assertEqual({e["path"] for e in entries},
                         {"libs", "libs/empty", "libs/one.jar", "local.properties"})
        artifact = Path(result["artifact_directory"])
        continued = bridge.revise_task(self.tmp / "task-0.json", artifact, {"findings": [{
            "path": "hello.py", "issue": "Missing main guard", "expected_behavior": "Add a main guard"}]})
        self.assertEqual(continued["copy_ignored"], entries)

    def test_copy_ignored_refuses_tracked_nonignored_outside_and_missing(self):
        (self.tmp / "outside.txt").write_text("outside", encoding="utf-8")
        (self.repo / "unignored.txt").write_text("unignored", encoding="utf-8")
        for selected, message in ((["README.md"], "tracked"), (["../outside.txt"], "outside"),
                                  ([str(self.tmp / "outside.txt")], "outside|absolute"),
                                  (["unignored.txt"], "not git-ignored"), (["missing.*"], "matched nothing"),
                                  ([".git/**"], "unsafe")):
            with self.subTest(selected=selected), self.assertRaisesRegex(Exception, message):
                bridge.run(self.task(copy_ignored=selected))
        self.assertFalse(self.log.exists(), "unsafe inputs must be refused before launching the provider")

    def test_copy_ignored_size_limit_counts_unique_files(self):
        self.ignored_inputs()
        task = load_task(self.task(copy_ignored=["local.properties", "local.*"]))
        with mock.patch.object(bridge, "MAX_COPY_IGNORED_BYTES", len(b"sdk=local\n")):
            self.assertEqual(len(bridge._ignored_inventory(task)), 1)
        with mock.patch.object(bridge, "MAX_COPY_IGNORED_BYTES", len(b"sdk=local\n") - 1):
            with self.assertRaisesRegex(Exception, "200 MB"):
                bridge._ignored_inventory(task)

    def test_auto_continue_resumes_with_progress_and_records_grant(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        result = bridge.run(self.task(auto_continue=1))
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["auto_continuations"][0]["granted_turns"], 4)
        self.assertEqual(result["auto_continuations"][0]["outcome_lifecycle_status"], "REVIEW_PENDING")
        self.assertIn("auto_continuations", bridge.brief(result))
        self.assertEqual({s["session_id"] for s in result["segments"]}, {result["session_id"]})

    def test_auto_continue_stops_without_progress(self):
        for scenario, expected_segments in (("extension_empty", 1), ("extension_stalled", 2)):
            with self.subTest(scenario=scenario):
                os.environ["FAKE_CODEX_SCENARIO"] = scenario
                result = bridge.run(self.task(auto_continue=3))
                self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED", result["failures"])
                self.assertEqual(len(result["segments"]), expected_segments)
                self.assertEqual(len(result.get("auto_continuations", [])), expected_segments - 1)

    def test_auto_continue_limits_and_max_extensions(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension_progress"
        for automatic, cap, expected in ((0, None, 0), (3, 0, 0), (3, 1, 1), (2, 5, 2), (3, None, 3)):
            with self.subTest(automatic=automatic, cap=cap):
                result = bridge.run(self.task(auto_continue=automatic, max_extensions=cap))
                self.assertEqual(len(result.get("auto_continuations", [])), expected)
                self.assertEqual(len(result["segments"]), expected + 1)
                self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED", result["failures"])

    def test_auto_continue_validates_integer_range(self):
        for invalid in (-1, 4, True, "1", 1.0):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(Exception, "auto_continue"):
                load_task(self.task(auto_continue=invalid))
        self.assertEqual(load_task(self.task()).auto_continue, 0)
        self.assertEqual(load_task(self.task()).copy_ignored, ())

    def test_windowsapps_programs_are_not_sandbox_runnable(self):
        for folder in ("Program Files/WindowsApps", "PROGRAM FILES/windowsapps"):
            executable = self.tmp / folder / "PowerShell" / "pwsh.exe"
            self.assertFalse(bridge.sandbox_can_run(executable, home=self.tmp / "home",
                                                  acl_text=lambda directory: "CodexSandboxUsers:(RX)"))
        self.assertTrue(bridge.sandbox_can_run(self.tmp / "Program Files" / "PowerShell" / "pwsh.exe",
                                             home=self.tmp / "home", acl_text=lambda directory: ""))
        # Only worker eligibility changes: independent validation still starts this program.
        executable = self.tmp / "Program Files" / "WindowsApps" / "pwsh.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"")
        task = load_task(self.task(validation_command=[str(executable), "-Command", "exit 0"]))
        with mock.patch.object(bridge, "run_process", return_value=bridge.ProcessResult(0, "", "", 0, False, False, False)) as run:
            validation, error = bridge._validation(task, self.repo, self.tmp)
        self.assertIsNone(error)
        self.assertEqual(validation["status"], "passed")
        self.assertEqual(run.call_args.args[0], executable.resolve())

    def test_python_validation_gets_single_dont_write_bytecode_flag(self):
        for name in ("python.exe", "python3", "python3.12", "py", "pythonw.exe", "PYTHON.EXE"):
            self.assertEqual(bridge._validation_arguments(Path(name), ["-m", "unittest"]), ["-B", "-m", "unittest"])
        self.assertEqual(bridge._validation_arguments(Path("python"), ["-B", "-m", "unittest"]), ["-B", "-m", "unittest"])
        self.assertEqual(bridge._validation_arguments(Path("python"), ["-u", "-B", "t.py"]), ["-u", "-B", "t.py"])
        # A -B after the script or module belongs to the program, so it does not count.
        self.assertEqual(bridge._validation_arguments(Path("python"), ["t.py", "-B"]), ["-B", "t.py", "-B"])
        for name in ("pwsh.exe", "node", "pytest", "pythonista"):
            self.assertEqual(bridge._validation_arguments(Path(name), ["x"]), ["x"])


class Efficiency(BridgeCase):
    def test_minimal_task_uses_safe_defaults(self):
        from claude_codex_bridge.contracts import ContractError, load_task, validate_task
        minimal = {"task_id": "minimal", "repo_root": str(self.repo), "base_commit": self.base, "mode": "implement",
                   "objective": "Create hello.py", "context_paths": ["README.md"],
                   "allowed_changed_paths": ["hello.py"], "acceptance_criteria": ["It prints Hello, world!"],
                   "validation_command": [sys.executable, "-B", "-m", "unittest", "-v"]}
        path = self.tmp / "minimal.json"
        path.write_text(json.dumps(minimal), encoding="utf-8")
        task = load_task(path)
        self.assertEqual((task.max_turns, task.timeout_seconds, task.validation_timeout_seconds), (6, 900, 600))
        self.assertEqual(task.forbidden_context, ())
        self.assertFalse(task.allow_subagents)
        self.assertTrue(task.require_subscription_auth)
        for key, unsafe in (("allow_subagents", True), ("require_subscription_auth", False)):
            with self.assertRaises(ContractError):
                validate_task({**minimal, key: unsafe})
        without_validation = {k: v for k, v in minimal.items() if k != "validation_command"}
        for mode in ("implement", "test"):
            with self.assertRaises(ContractError):
                validate_task({**without_validation, "mode": mode})

    def test_worker_check_rule_follows_the_setting(self):
        task = bridge.load_task(self.task())
        os.environ["CODEX_BRIDGE_WORKER_CHECKS"] = "skip"
        try:
            text = bridge._assignment(task)
            self.assertIn("Do not run tests, the validation_command", text)
            self.assertIn("do not run git status", text)
            os.environ["CODEX_BRIDGE_WORKER_CHECKS"] = "run"
            text = bridge._assignment(task)
            self.assertIn("Run validation_command (or a narrower focused check)", text)
            self.assertIn("Do not run git status or git diff", text)
            os.environ["CODEX_BRIDGE_WORKER_CHECKS"] = "sometimes"
            with self.assertRaises(Exception):
                bridge._assignment(task)
        finally:
            os.environ.pop("CODEX_BRIDGE_WORKER_CHECKS", None)
        self.assertEqual(bridge.worker_checks_mode(), "skip" if os.name == "nt" else "run")

    def test_sandbox_can_run_checks_profile_and_acl(self):
        home = self.tmp / "home"
        inside = home / "AppData" / "Python" / "python.exe"
        outside = self.tmp / "Program Files" / "Python" / "python.exe"
        granted = lambda directory: "BUILTIN\\Users:(RX)\nHOST\\CodexSandboxUsers:(OI)(CI)(RX)"
        denied = lambda directory: "BUILTIN\\Administrators:(F)\nHOST\\owner:(F)"
        self.assertTrue(bridge.sandbox_can_run(outside, home=home, acl_text=denied))
        self.assertTrue(bridge.sandbox_can_run(inside, home=home, acl_text=granted))
        self.assertFalse(bridge.sandbox_can_run(inside, home=home, acl_text=denied))
        self.assertFalse(bridge.sandbox_can_run(inside, home=home, acl_text=lambda directory: ""))

    def test_auto_worker_checks_follow_the_validation_executable(self):
        os.environ.pop("CODEX_BRIDGE_WORKER_CHECKS", None)
        with_command = bridge.load_task(self.task())
        # A writing task must carry a validation command, so the "no command" case is a read-only task.
        without_command = bridge.load_task(self.task(mode="review", allowed_changed_paths=[],
                                                     validation_command=None))
        with mock.patch.object(bridge.os, "name", "nt"):
            with mock.patch.object(bridge, "sandbox_can_run", return_value=True):
                self.assertEqual(bridge.worker_checks_mode(with_command), "run")
                self.assertEqual(bridge.worker_checks_mode(without_command), "skip")
            with mock.patch.object(bridge, "sandbox_can_run", return_value=False):
                self.assertEqual(bridge.worker_checks_mode(with_command), "skip")

    def test_revision_prompt_sends_only_the_findings(self):
        result = bridge.run(self.task(validation_command=[sys.executable, "-c", "import sys; sys.exit(1)"]))
        feedback = self.tmp / "feedback.json"
        feedback.write_text(json.dumps({"findings": [{"path": "hello.py", "issue": "No main guard for import use",
                                                      "expected_behavior": "Wrap the print in main() behind a main guard"}]}))
        revised = bridge.revise_task(self.tmp / "task-0.json", Path(result["artifact_directory"]), feedback, 3)
        prompt = (Path(revised["artifact_directory"]) / "revision-1.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("No main guard for import use", prompt)
        self.assertNotIn("Contract:", prompt)
        self.assertNotIn("Rules:", prompt)
        self.assertIn("only the JSON object", prompt)

    def test_brief_report_carries_diff_validation_and_usage(self):
        result = bridge.run(self.task())
        report = bridge.brief(result)
        self.assertEqual(report["lifecycle_status"], "REVIEW_PENDING")
        self.assertIn("hello.py", report["diffstat"]["stat"])
        self.assertEqual(report["diffstat"]["files"], [{"path": "hello.py", "added": 1, "removed": 0}])
        self.assertNotIn("diff", report)
        applied = self.tmp / "applied"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(applied)], check=True)
        subprocess.run(["git", "apply", report["patch"]], cwd=applied, check=True)
        self.assertTrue((applied / "hello.py").is_file())
        self.assertEqual(report["validation"]["status"], "passed")
        self.assertNotIn("output_tail", report["validation"])
        self.assertEqual(report["usage"]["five_hour"], "not on this plan")
        self.assertEqual(report["usage"]["weekly"], 81.0)
        self.assertNotIn("before", report["usage"])
        self.assertIn("summary", report["worker"])
        self.assertNotIn("preflight", report)
        self.assertLess(len(json.dumps(report)), len(json.dumps(result)))

    def test_codex_app_versioned_folder_is_found(self):
        from claude_codex_bridge import codexcli
        saved = {key: os.environ.get(key) for key in ("CODEX_BRIDGE_COMMAND", "LOCALAPPDATA", "PATH")}
        local = self.tmp / "localappdata"
        older = local / "OpenAI" / "Codex" / "bin" / "aaaa" / "codex.exe"
        newer = local / "OpenAI" / "Codex" / "bin" / "bbbb" / "codex.exe"
        for index, exe in enumerate((older, newer)):
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            os.utime(exe, (1_700_000_000 + index * 100, 1_700_000_000 + index * 100))
        (local / "OpenAI" / "Codex" / "bin" / "empty").mkdir()
        empty_path = self.tmp / "empty-path"
        empty_path.mkdir()
        try:
            os.environ.pop("CODEX_BRIDGE_COMMAND", None)
            os.environ["LOCALAPPDATA"] = str(local)
            os.environ["PATH"] = str(empty_path)
            self.assertEqual(codexcli.find_codex().executable, newer.resolve())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class LeadCycle(BridgeCase):
    def command(self, *args):
        from claude_codex_bridge import cli
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = cli.main(list(args))
        return code, output.getvalue()

    def test_cli_compact_and_pretty(self):
        task = self.task()
        code, compact = self.command("check-task", "--task", str(task))
        self.assertEqual(code, 0)
        self.assertEqual(len(compact.splitlines()), 1)
        self.assertNotIn(': ', compact)
        for args in (("--pretty", "check-task", "--task", str(task)),
                     ("check-task", "--task", str(task), "--pretty")):
            code, pretty = self.command(*args)
            self.assertEqual(code, 0)
            self.assertGreater(len(pretty.splitlines()), 1)
            self.assertEqual(json.loads(compact), json.loads(pretty))

    def test_tail_character_line_caps_and_findings(self):
        artifact = self.tmp / "report"
        artifact.mkdir()
        (artifact / "validation.stdout.log").write_text(("s" * 5000 + "\n") * 30, encoding="utf-8")
        (artifact / "validation.stderr.log").write_text(("e" * 5000 + "\n") * 30)
        record = {"lifecycle_status": "BLOCKED", "artifact_directory": str(artifact),
                  "validation": {"status": "failed"}, "codex_claim": {"findings": ["Review this behavior"]}}
        brief = bridge.brief(record)
        tail = brief["validation"]["output_tail"]
        self.assertLessEqual(len(tail), 20)
        self.assertLessEqual(len("\n".join(tail)), 2000)
        self.assertTrue(all(len(line) <= 300 for line in tail))
        self.assertEqual(brief["worker"]["findings"], ["Review this behavior"])
        record["validation"]["status"] = "passed"
        self.assertNotIn("output_tail", bridge.brief(record)["validation"])

    def test_show_diff_files_and_raw_cli(self):
        segment = bridge._segment
        def edit_two(*args, **kwargs):
            result = segment(*args, **kwargs)
            (args[2] / "README.md").write_text("Updated documentation\n", encoding="utf-8")
            return result
        with mock.patch.object(bridge, "_segment", side_effect=edit_two):
            result = bridge.run(self.task(allowed_changed_paths=["hello.py", "README.md"]))
        artifact = Path(result["artifact_directory"])
        selected = bridge.show_diff(artifact, ["hello.py"])
        self.assertIn("+print('Hello, world!')", selected)
        self.assertNotIn("README.md", selected)
        self.assertIn("Updated documentation", bridge.show_diff(artifact, ["README.md"]))
        code, text = self.command("show-diff", "--artifact", str(artifact), "--files", "hello.py")
        self.assertEqual(code, 0)
        self.assertEqual(text, selected)
        self.assertTrue(text.startswith("diff --git"))

    def test_inline_revision_latest_and_interdiff(self):
        task = self.task()
        first = bridge.run(task)
        artifact = Path(first["artifact_directory"])
        code, text = self.command("revise", "--task", str(task), "--artifact", "latest",
                                  "--finding", "hello.py::Missing main guard::Add a main guard")
        self.assertEqual(code, 0, text)
        revised = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(revised["revisions"][0]["granted_turns"], 4)
        self.assertEqual(revised["lifecycle_status"], "REVIEW_PENDING", revised["failures"])
        brief = json.loads(text)
        self.assertEqual(brief["diffstat_base"], "previous_segment")
        self.assertEqual(brief["diffstat"]["files"][0]["removed"], 1)
        self.assertNotEqual(revised["segments"][0]["tree"], revised["segments"][1]["tree"])
        code, diff = self.command("show-diff", "--task", str(task), "--artifact", "latest",
                                  "--since-last", "--files", "hello.py")
        self.assertEqual(code, 0)
        self.assertIn("--- a/hello.py", diff)
        self.assertIn("-print('Hello, world!')", diff)
        self.assertNotIn("/dev/null", diff)
        self.assertEqual(diff, bridge.show_diff(artifact, since_last=True))
        self.assertIn("/dev/null", bridge.show_diff(artifact))

    def test_continue_interdiff(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        task = self.task()
        first = bridge.run(task)
        artifact = Path(first["artifact_directory"])
        result = bridge.continue_task(task, artifact, 4)
        self.assertEqual(result["diffstat_base"], "previous_segment")
        self.assertEqual(result["diffstat"]["files"], [{"path": "hello.py", "added": 1, "removed": 1}])
        self.assertIn("-print('Hello')", bridge.show_diff(artifact, since_last=True))

    def test_latest_uses_newest_artifact_of_task(self):
        task = self.task()
        older = bridge.run(task)
        newer = bridge.run(task)
        self.assertNotEqual(older["artifact_directory"], newer["artifact_directory"])
        self.assertEqual(bridge.resolve_artifact(Path("latest"), task), Path(newer["artifact_directory"]))

    def test_inferred_contract_stays_resolved_across_revision(self):
        task = self.tmp / "inferred-task.json"
        task.write_text(json.dumps({"mode": "implement", "objective": "Create hello.py",
                                   "allowed_changed_paths": ["hello.py"],
                                   "acceptance_criteria": ["Print Hello, world!"],
                                   "validation_command": [sys.executable, "-B", "-m", "unittest", "-v"]}))
        with mock.patch.object(Path, "cwd", return_value=self.repo):
            resolved = load_task(task)
            self.assertEqual(resolved.task_id, "inferred-task")
            self.assertEqual(resolved.repo_root, self.repo)
            self.assertEqual(resolved.base_commit, self.base)
            self.assertEqual(resolved.context_paths, ("hello.py",))
            result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        saved = json.loads((artifact / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, resolved.raw)
        # Advance the primary checkout: omission must continue to mean the stored base/root,
        # even when a revision is launched from a different current directory.
        (self.repo / "README.md").write_text("Next primary commit\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid", "commit", "-qm", "next")
        revised = bridge.revise_task(task, Path("latest"), {"findings": [{"path": "hello.py",
                    "issue": "Missing main guard", "expected_behavior": "Add a main guard"}]})
        self.assertEqual(revised["starting_commit"], self.base)
        self.assertEqual(revised["lifecycle_status"], "REVIEW_PENDING", revised["failures"])
        # Equivalent explicit defaults compare semantically, rather than raw JSON bytes.
        task.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        self.assertEqual(bridge._load_prior(task, artifact)[0].raw, saved)

    def test_inferred_base_warns_for_tracked_edits_and_explicit_values_win(self):
        task = self.task()
        value = json.loads(task.read_text(encoding="utf-8"))
        value.pop("base_commit")
        task.write_text(json.dumps(value), encoding="utf-8")
        (self.repo / "README.md").write_text("Uncommitted input\n", encoding="utf-8")
        with self.assertWarnsRegex(UserWarning, "uncommitted tracked"):
            inferred = load_task(task)
        self.assertEqual(inferred.base_commit, self.base)
        value["base_commit"] = self.base
        task.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(load_task(task).task_id, "hello-world")
        self.assertEqual(load_task(task).context_paths, ("README.md", "test_hello.py"))

    def test_accept_applies_and_cleans_without_committing(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        branch = git(Path(result["worktree"]), "branch", "--show-current").strip()
        code, text = self.command("accept", "--task", str(task), "--artifact", "latest")
        self.assertEqual(code, 0, text)
        accepted = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(accepted["status"], "accepted")
        self.assertTrue(accepted["applied_at"])
        self.assertTrue(accepted["cleaned_at"])
        self.assertTrue((self.repo / "hello.py").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), self.base)
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertNotIn(branch, git(self.repo, "branch", "--list"))
        self.assertTrue((artifact / "diff.patch").exists())
        self.assertTrue(bridge.show_diff(artifact, ["hello.py"]))

    def test_accept_already_applied_records_and_cleans(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        branch = git(Path(result["worktree"]), "branch", "--show-current").strip()
        git(self.repo, "apply", str(artifact / "diff.patch"))
        files = {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
        index = (self.repo / ".git/index").read_bytes()
        code, text = self.command("accept", "--task", str(task), "--artifact", str(artifact), "--already-applied")
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)["status"], "accepted")
        accepted = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(accepted["lifecycle_status"], "ACCEPTED")
        self.assertIs(accepted["applied_manually"], True)
        self.assertTrue(accepted["applied_at"])
        self.assertTrue(accepted["cleaned_at"])
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertNotIn(branch, git(self.repo, "branch", "--list"))
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()})
        self.assertEqual(index, (self.repo / ".git/index").read_bytes())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), self.base)

    def test_accept_already_applied_missing_change_preserves_everything(self):
        for content in (None, b"Partially integrated file\n"):
            with self.subTest(content=content):
                task = self.task()
                result = bridge.run(task)
                artifact = Path(result["artifact_directory"])
                branch = git(Path(result["worktree"]), "branch", "--show-current").strip()
                prior = (artifact / "result.json").read_bytes()
                if content is not None:
                    (self.repo / "hello.py").write_bytes(content)
                files = {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
                index = (self.repo / ".git/index").read_bytes()
                refused = bridge.accept(task, artifact, already_applied=True)
                self.assertEqual(refused["status"], "failed")
                self.assertEqual(refused["conflicting_files"], ["hello.py"])
                self.assertEqual(prior, (artifact / "result.json").read_bytes())
                self.assertEqual(files, {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()})
                self.assertEqual(index, (self.repo / ".git/index").read_bytes())
                self.assertTrue(Path(result["worktree"]).exists())
                self.assertIn(branch, git(self.repo, "branch", "--list"))

    def test_accept_integration_flags_are_mutually_exclusive(self):
        from claude_codex_bridge import cli
        with mock.patch("sys.stderr", new_callable=io.StringIO) as output:
            code = cli.main(["accept", "--task", "unused", "--artifact", "unused",
                             "--already-applied", "--3way"])
        self.assertEqual(code, 2)
        reply = json.loads(output.getvalue())
        self.assertEqual(reply["status"], "failed")
        self.assertTrue(reply["error"].startswith("invalid arguments: "))
        self.assertIn("not allowed with argument", reply["error"])
        with self.assertRaisesRegex(Exception, "mutually exclusive"):
            bridge.accept(Path("unused"), Path("unused"), three_way=True, already_applied=True)

    def test_accept_conflict_changes_nothing(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        prior = (artifact / "result.json").read_bytes()
        (self.repo / "hello.py").write_bytes(b"Primary conflicting file\n")
        files = {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
        index = (self.repo / ".git/index").read_bytes()
        refused = bridge.accept(task, artifact)
        self.assertEqual(refused["status"], "failed")
        self.assertEqual(refused["conflicting_files"], ["hello.py"])
        self.assertEqual(prior, (artifact / "result.json").read_bytes())
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()})
        self.assertEqual(index, (self.repo / ".git/index").read_bytes())
        self.assertTrue(Path(result["worktree"]).exists())

    def test_accept_requires_validation_and_review_scope(self):
        for changes in ({"validation": {"status": "not_run"}}, {"lifecycle_status": "BLOCKED"},
                        {"unauthorized_changed_paths": ["extra.txt"]}):
            with self.subTest(changes=changes):
                task = self.task()
                result = bridge.run(task)
                artifact = Path(result["artifact_directory"])
                (artifact / "result.json").write_text(json.dumps({**result, **changes}), encoding="utf-8")
                with self.assertRaisesRegex(Exception, "review readiness"):
                    bridge.accept(task, artifact)
                self.assertFalse((self.repo / "hello.py").exists())

    def test_cleanup_retains_artifact_and_primary(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        branch = git(Path(result["worktree"]), "branch", "--show-current").strip()
        code, text = self.command("cleanup", "--task", str(task), "--artifact", str(artifact))
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)["status"], "rejected")
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertNotIn(branch, git(self.repo, "branch", "--list"))
        self.assertFalse((self.repo / "hello.py").exists())
        self.assertTrue((artifact / "result.json").exists())
        self.assertEqual(bridge.cleanup(task, artifact)["status"], "rejected")

    def test_crlf_patch_normalizes_and_accepts(self):
        git(self.repo, "config", "core.autocrlf", "true")
        (self.repo / "hello.py").write_bytes(b"print('old greeting')\r\n# second line\r\n")
        git(self.repo, "add", "hello.py")
        git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid", "commit", "-qm", "crlf base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        task = self.task()
        result = bridge.run(task)
        self.assertEqual(result["validation"]["status"], "passed")
        artifact = Path(result["artifact_directory"])
        patch = artifact / "diff.patch"
        self.assertNotIn(b"\r", patch.read_bytes())
        self.assertNotIn(b"\r", (Path(result["worktree"]) / "hello.py").read_bytes())
        clone = self.tmp / "fresh"
        git(self.tmp, "clone", "-q", "--config", "core.autocrlf=true", str(self.repo), str(clone))
        self.assertIn(b"\r\n", (clone / "hello.py").read_bytes())
        git(clone, "apply", "--check", str(patch))
        self.assertEqual(bridge.accept(task, artifact)["status"], "accepted")
        self.assertIn("Hello, world!", (self.repo / "hello.py").read_text(encoding="utf-8"))

    def test_accept_three_way(self):
        task = self.task()
        result = bridge.run(task)
        code, text = self.command("accept", "--task", str(task), "--artifact", result["artifact_directory"], "--3way")
        self.assertEqual(code, 0, text)
        self.assertTrue((self.repo / "hello.py").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), self.base)


class Recovery(BridgeCase):
    def feedback(self):
        path = self.tmp / "feedback.json"
        path.write_text(json.dumps({"findings": [{"path": "hello.py", "issue": "Missing main guard",
                                                "expected_behavior": "Add a main guard"}]}))
        return path

    def test_timeout_then_cli_revalidate_updates_result_and_allows_revision(self):
        from claude_codex_bridge import cli
        task = self.task(validation_command=[sys.executable, "-B", "-c", "import time; time.sleep(1.2)"],
                         validation_timeout_seconds=1)
        first = bridge.run(task)
        self.assertEqual(first["lifecycle_status"], "BLOCKED")
        self.assertTrue(first["validation"]["process"]["timed_out"])
        artifact = Path(first["artifact_directory"])
        calls_before = len(self.calls())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = cli.main(["revalidate", "--task", str(task), "--artifact", str(artifact), "--timeout", "5"])
        self.assertEqual(code, 0, output.getvalue())
        saved = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["lifecycle_status"], "REVIEW_PENDING")
        self.assertEqual(saved["validation"]["status"], "passed")
        self.assertEqual(saved["validation"]["timeout_seconds"], 5)
        self.assertEqual(saved["failures"], [])
        self.assertEqual(len(self.calls()), calls_before, "revalidation must not launch Codex")
        revised = bridge.revise_task(task, artifact, self.feedback(), 3)
        self.assertEqual(revised["revisions"][0]["target_state"], "complete")

    def test_environment_blocker_classification_requires_only_environment_reasons(self):
        for text in ("Cannot run tests in the sandbox", "Validation executable unavailable: access denied",
                     "Python cannot start in the sandbox", "Gradle cannot download dependencies with network disabled"):
            with self.subTest(text=text):
                self.assertTrue(bridge.environment_only_blockers({"blockers": [text]}))
        for blockers in ([], ["stop condition"], ["Cannot run tests in the sandbox", "Need an owner decision"],
                         ["Cannot run tests in the sandbox and the owner must choose the interface"],
                         ["Cannot run tests in the sandbox; unclear expected behavior"],
                         ["Cannot build in the sandbox; implementation is unfinished"]):
            with self.subTest(blockers=blockers):
                self.assertFalse(bridge.environment_only_blockers({"blockers": blockers}))

    def test_revision_accepts_timeout_failure(self):
        task = self.task(validation_command=[sys.executable, "-B", "-c", "import time; time.sleep(1.2)"],
                         validation_timeout_seconds=1)
        first = bridge.run(task)
        revised = bridge.revise_task(task, Path(first["artifact_directory"]), self.feedback(), 3)
        self.assertEqual(revised["revisions"][0]["target_state"], "validation_failed")
        self.assertIn("def main", (Path(revised["worktree"]) / "hello.py").read_text(encoding="utf-8"))

    def test_environment_only_block_with_changes_validates_and_is_revisable(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "environment_blocked"
        # Bridge-side evidence is required to downgrade a "blocked" claim; "skip" means the bridge itself found
        # that interpreters cannot start in the worker sandbox (independent of this machine's ACLs).
        patcher = mock.patch.dict(os.environ, {"CODEX_BRIDGE_WORKER_CHECKS": "skip"})
        patcher.start()
        self.addCleanup(patcher.stop)
        task = self.task()
        first = bridge.run(task)
        self.assertEqual(first["codex_claim"]["status"], "blocked")
        self.assertEqual(first["validation"]["status"], "passed")
        self.assertEqual(first["lifecycle_status"], "REVIEW_PENDING", first["failures"])
        self.assertTrue(any("environment-only" in w for w in first["warnings"]))
        os.environ["FAKE_CODEX_SCENARIO"] = "hello"
        revised = bridge.revise_task(task, Path(first["artifact_directory"]), self.feedback(), 3)
        self.assertEqual(revised["lifecycle_status"], "REVIEW_PENDING", revised["failures"])

    def test_revalidation_recovers_an_environment_only_block(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "environment_blocked"
        patcher = mock.patch.dict(os.environ, {"CODEX_BRIDGE_WORKER_CHECKS": "skip"})
        patcher.start()
        self.addCleanup(patcher.stop)
        task = self.task()
        first = bridge.run(task)
        artifact = Path(first["artifact_directory"])
        first.update(status="failed", lifecycle_status="BLOCKED", failures=["Codex reported blocked"],
                     validation={"status": "not_run"}, warnings=[])
        (artifact / "result.json").write_text(json.dumps(first), encoding="utf-8")
        self.assertEqual(bridge.revalidate(task, artifact, 5)["lifecycle_status"], "REVIEW_PENDING")
        saved = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["failures"], [])
        self.assertTrue(any("environment-only" in w for w in saved["warnings"]))

    def test_non_environment_and_mixed_blockers_stay_blocked_after_revalidation(self):
        for scenario in ("decision_blocked", "mixed_blocked"):
            with self.subTest(scenario=scenario):
                os.environ["FAKE_CODEX_SCENARIO"] = scenario
                task = self.task(task_id=scenario.replace("_", "-"))
                first = bridge.run(task)
                self.assertEqual(first["lifecycle_status"], "BLOCKED")
                self.assertEqual(first["validation"]["status"], "not_run")
                artifact = Path(first["artifact_directory"])
                self.assertEqual(bridge.revalidate(task, artifact, 5)["lifecycle_status"], "BLOCKED")
                with self.assertRaisesRegex(ValueError, "not a revisable"):
                    bridge.revise_task(task, artifact, self.feedback(), 3)

    def test_single_policy_rejection_with_changes_warns_and_allows_revision(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "policy_changed"
        task = self.task(validation_command=[sys.executable, "-B", "-c", "raise SystemExit(1)"])
        first = bridge.run(task)
        self.assertEqual(first["failures"], ["independent validation failed"])
        self.assertTrue(any("blocked by policy" in w and "warning only" in w for w in first["warnings"]))
        os.environ["FAKE_CODEX_SCENARIO"] = "hello"
        revised = bridge.revise_task(task, Path(first["artifact_directory"]), self.feedback(), 3)
        self.assertEqual(revised["revisions"][0]["target_state"], "validation_failed")

    def test_single_policy_rejection_with_passing_validation_is_review_pending(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "policy_changed"
        first = bridge.run(self.task())
        self.assertEqual(first["lifecycle_status"], "REVIEW_PENDING", first["failures"])
        self.assertTrue(any("warning only" in w for w in first["warnings"]))

    def test_revalidation_cannot_clear_scope_or_primary_checkout_failures(self):
        for scenario in ("outofscope", "hello"):
            with self.subTest(scenario=scenario):
                os.environ["FAKE_CODEX_SCENARIO"] = scenario
                task = self.task(task_id="guard-" + scenario)
                first = bridge.run(task)
                if scenario == "hello":
                    (self.repo / "hello.py").write_text("lead edit\n", encoding="utf-8")
                artifact = Path(first["artifact_directory"])
                result = bridge.revalidate(task, artifact, 5)
                self.assertEqual(result["lifecycle_status"], "BLOCKED")
                self.assertTrue(json.loads((artifact / "result.json").read_text(encoding="utf-8"))["failures"])

    def test_revalidation_timeout_limits(self):
        task = self.task()
        first = bridge.run(task)
        for timeout in (0, 1801, True):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(Exception, "1 through 1800"):
                bridge.revalidate(task, Path(first["artifact_directory"]), timeout)


class BridgeEndToEnd(BridgeCase):
    extra_files = {
        "crlf.txt": b"one\r\ntwo\r\nthree\r\n", "cr.txt": b"a\rb\r\nc\r\n",
        "latin.py": b"# caf\xe9\nx = 1\n", "src/keep.py": b"keep = 1\n",
        ".gitignore": b"local/\n",
    }

    def test_accept_preserves_committed_crlf_lone_cr_and_non_utf8_bytes(self):
        edits = {"crlf.txt": b"one\r\nTWO\r\nthree\r\n", "cr.txt": b"a\rB\r\nc\r\n", "latin.py": b"# caf\xe9\nx = 2\n"}

        def edit(worktree):
            for name, data in edits.items():
                (worktree / name).write_bytes(data)

        task = self.task(allowed_changed_paths=["hello.py", *edits])
        result = self.run_with_edits(task, edit)
        self.assertEqual(result["status"], "complete", result["failures"])
        artifact = Path(result["artifact_directory"])
        patch = (artifact / "diff.patch").read_bytes()
        self.assertIn(b"-two\r\n", patch)
        self.assertIn(b"caf\xe9", patch)
        self.assertEqual(bridge.accept(task, artifact)["status"], "accepted")
        for name, data in edits.items():
            self.assertEqual((self.repo / name).read_bytes(), data, name)

    def test_user_git_config_cannot_move_or_alter_the_accepted_change(self):
        self.set_config("[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n[color]\n\tui = always\n"
                        "[apply]\n\twhitespace = fix\n")

        def edit(worktree):
            (worktree / "src" / "new.txt").write_bytes(b"hi  \n")

        task = self.task(allowed_changed_paths=["hello.py", "src/**"])
        result = self.run_with_edits(task, edit)
        self.assertEqual(result["status"], "complete", result["failures"])
        self.assertEqual(bridge.accept(task, Path(result["artifact_directory"]))["status"], "accepted")
        self.assertEqual((self.repo / "src" / "new.txt").read_bytes(), b"hi  \n")
        self.assertFalse((self.repo / "new.txt").exists())

    def test_show_diff_returns_text_even_for_a_non_utf8_patch(self):
        def edit(worktree):
            (worktree / "latin.py").write_bytes(b"# caf\xe9\nx = 2\n")

        result = self.run_with_edits(self.task(allowed_changed_paths=["hello.py", "latin.py"]), edit)
        text = bridge.show_diff(Path(result["artifact_directory"]))
        self.assertIn("latin.py", text)

    @unittest.skipUnless(NT, "junctions are Windows-only")
    def test_junction_in_the_worktree_blocks_the_result_and_cleanup_spares_its_target(self):
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "secret.txt").write_text("secret\n", encoding="utf-8")

        def edit(worktree):
            if not make_junction(worktree / "src" / "link", victim):
                raise unittest.SkipTest("cannot create a junction here")

        task = self.task(allowed_changed_paths=["hello.py", "src/**"])
        result = self.run_with_edits(task, edit)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("junction: src/link" in item for item in result["failures"]), result["failures"])
        self.assertFalse(any(path.startswith("src/link") for path in result["changed_paths"]))
        self.assertNotIn(b"secret", (Path(result["artifact_directory"]) / "diff.patch").read_bytes())
        bridge.cleanup(task, Path(result["artifact_directory"]))
        self.assertEqual((victim / "secret.txt").read_text(encoding="utf-8"), "secret\n")
        self.assertFalse(Path(result["worktree"]).exists())

    def test_nested_repository_blocks_the_result_and_adds_no_gitlink(self):
        def edit(worktree):
            nested = worktree / "src" / "vendor"
            nested.mkdir()
            git(nested, "init", "-q")
            (nested / "lib.py").write_text("lib\n", encoding="utf-8")
            git(nested, "add", ".")
            git(nested, "commit", "-qm", "nested")

        result = self.run_with_edits(self.task(allowed_changed_paths=["hello.py", "src/**"]), edit)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("nested_git" in item for item in result["failures"]), result["failures"])
        self.assertNotIn(b"Subproject commit", (Path(result["artifact_directory"]) / "diff.patch").read_bytes())

    def test_modified_dotgit_pointer_fails_the_segment_instead_of_running_it(self):
        def edit(worktree):
            rewrite_dotgit(worktree, "gitdir: " + (worktree / "nowhere").as_posix() + "\n")

        # The tampering is recorded as a failure (so the artifact can be reported and cleaned up) rather
        # than aborting the run without a record; git is still never run against the rewritten pointer.
        result = self.run_with_edits(self.task(), edit)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("worktree identity changed" in item for item in result["failures"]), result["failures"])

    def test_cleanup_still_works_after_the_worktree_pointer_was_tampered_with(self):
        task = self.task()
        result = bridge.run(task)
        artifact, worktree = Path(result["artifact_directory"]), Path(result["worktree"])
        rewrite_dotgit(worktree, "gitdir: " + (worktree / "nowhere").as_posix() + "\n")
        cleaned = bridge.cleanup(task, artifact)
        self.assertEqual(cleaned["status"], "rejected")
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "delegate/*").strip(), "")

    def test_accept_cleans_up_the_recorded_branch_even_if_the_worker_switched_branches(self):
        task = self.task()
        result = bridge.run(task)
        worktree = Path(result["worktree"])
        git(worktree, "checkout", "-q", "-b", "worker-made-branch")  # would have made cleanup refuse before
        self.assertEqual(bridge.accept(task, Path(result["artifact_directory"]))["status"], "accepted")
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "delegate/*").strip(), "")

    def test_ignored_inventory_asks_git_in_two_batched_calls(self):
        local = self.repo / "local"
        local.mkdir()
        for number in range(20):
            (local / f"f{number}.cfg").write_text(str(number), encoding="utf-8")
        task = bridge.load_task(self.task(copy_ignored=["local"]))
        calls = self.spy_on_git()
        inventory = bridge._ignored_inventory(task)
        self.assertEqual(len([item for item in inventory if not item.get("directory")]), 20)
        self.assertLessEqual(len([argv for argv, _ in calls if "ls-files" in argv or "check-ignore" in argv]), 2)

    def test_ignored_inventory_still_refuses_tracked_and_unignored_paths(self):
        with self.assertRaisesRegex(bridge.BridgeError, "tracked"):
            bridge._ignored_inventory(bridge.load_task(self.task(copy_ignored=["src/keep.py"])))
        (self.repo / "loose.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "not git-ignored"):
            bridge._ignored_inventory(bridge.load_task(self.task(copy_ignored=["loose.txt"])))

    def test_context_directory_globs_count_as_observed(self):
        task = bridge.load_task(self.task(context_paths=["src/**", "README.md"]))
        parsed = {"commands": [{"command": "Get-Content src/keep.py"}, {"command": "type README.md"}]}
        self.assertEqual(bridge._context_evidence(task, parsed)["status"], "verified")


class ReviseRetry(BridgeCase):
    def test_a_capacity_pause_before_revise_leaves_nothing_behind_so_the_retry_works(self):
        task = self.task()
        first = bridge.run(task)
        artifact = Path(first["artifact_directory"])
        os.environ["FAKE_CODEX_LIMITS"] = EXHAUSTED
        with self.assertRaises(bridge.CapacityPaused):
            bridge.revise_task(task, artifact, self.feedback(), 3)
        self.assertEqual(list(artifact.glob("revision-1.*")), [])
        os.environ.pop("FAKE_CODEX_LIMITS")
        revised = bridge.revise_task(task, artifact, self.feedback(), 3)
        self.assertEqual(revised["lifecycle_status"], "REVIEW_PENDING", revised["failures"])
        self.assertEqual(revised["revisions"][0]["index"], 1)

    def test_a_sign_in_failure_before_revise_can_be_retried(self):
        task = self.task()
        artifact = Path(bridge.run(task)["artifact_directory"])
        os.environ["FAKE_CODEX_ACCOUNT"] = "{}"
        with self.assertRaises(codexcli.CodexCliError):
            bridge.revise_task(task, artifact, self.feedback(), 3)
        os.environ.pop("FAKE_CODEX_ACCOUNT")
        self.assertEqual(bridge.revise_task(task, artifact, self.feedback(), 3)["lifecycle_status"], "REVIEW_PENDING")

    def test_files_left_by_an_attempt_that_never_recorded_a_revision_are_replaced(self):
        task = self.task()
        artifact = Path(bridge.run(task)["artifact_directory"])
        (artifact / "revision-1.prior-result.json").write_bytes(b"stale")
        (artifact / "revision-1.feedback.json").write_bytes(b"stale")
        revised = bridge.revise_task(task, artifact, self.feedback(), 3)
        self.assertEqual(revised["lifecycle_status"], "REVIEW_PENDING", revised["failures"])
        kept = json.loads((artifact / "revision-1.prior-result.json").read_text(encoding="utf-8"))
        self.assertEqual(kept["lifecycle_status"], "REVIEW_PENDING")


class StrandedRuns(BridgeCase):
    def assert_cleanable(self, task, artifact):
        artifact = bridge.resolve_artifact(Path(artifact), task)
        record = self.record(artifact)
        worktree = Path(record["worktree"])
        self.assertTrue(record["branch"].startswith("delegate/codex-hello-world-"))
        cleaned = bridge.cleanup(task, artifact)
        self.assertEqual(cleaned["status"], "rejected")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.branches(), "")

    def test_an_exception_after_the_worktree_exists_leaves_a_record_that_cleanup_removes(self):
        task = self.task()
        with mock.patch.object(bridge, "_segment", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                bridge.run(task)
        (artifact,) = self.artifacts()
        record = self.record(artifact)
        self.assertEqual((record["status"], record["lifecycle_status"]), ("failed", "BLOCKED"))
        self.assertIn("boom", record["failures"][-1])
        self.assertTrue(Path(record["worktree"]).exists())
        self.assertIn(record["branch"], self.branches())
        self.assert_cleanable(task, Path("latest"))

    def test_a_failure_while_the_assignment_is_built_is_recorded_too(self):
        task = self.task()
        self.setenv(CODEX_BRIDGE_WORKER_CHECKS="bogus")  # raises inside _assignment, after the worktree exists
        with self.assertRaises(bridge.BridgeError):
            bridge.run(task)
        self.assertEqual(len(self.artifacts()), 1)
        os.environ.pop("CODEX_BRIDGE_WORKER_CHECKS")
        self.assert_cleanable(task, Path("latest"))

    def test_a_failed_worktree_creation_still_leaves_a_record(self):
        task = self.task()
        with mock.patch.object(bridge, "create_worktree", side_effect=bridge.BridgeError("git broke")):
            with self.assertRaisesRegex(bridge.BridgeError, "git broke"):
                bridge.run(task)
        (artifact,) = self.artifacts()
        self.assertEqual(self.record(artifact)["lifecycle_status"], "BLOCKED")
        self.assertEqual(bridge.cleanup(task, artifact)["status"], "rejected")  # nothing exists, nothing breaks

    def test_the_artifact_path_travels_with_a_crash_error(self):
        task = self.task()
        with mock.patch.object(bridge, "_segment", side_effect=RuntimeError("boom")):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                code = cli.main(["run", "--task", str(task)])
        self.assertEqual(code, 2)
        message = json.loads(err.getvalue())["error"]
        self.assertIn("boom", message)
        self.assertIn(str(self.artifacts()[0]), message)

    def test_a_killed_bridge_leaves_a_running_record_and_cleanup_works_after_clearing_the_lock(self):
        self.setenv(FAKE_CODEX_SCENARIO="slow")
        task = self.task(timeout_seconds=60)
        script = ("import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
                  "from claude_codex_bridge import bridge\nbridge.run(Path(sys.argv[2]))\n")
        process = subprocess.Popen([sys.executable, "-B", "-c", script, str(SKILL / "src"), str(task)],
                                   env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and not (
                    self.log.exists() and any(c["argv"][:1] == ["exec"] for c in self.calls())):
                time.sleep(0.2)
            self.assertTrue(any(c["argv"][:1] == ["exec"] for c in self.calls()), "the worker never started")
        finally:
            process.kill()
            process.wait()
        deadline = time.monotonic() + 15
        while gitops.pid_is_running(process.pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        (artifact,) = self.artifacts()
        self.assertEqual(self.record(artifact)["lifecycle_status"], "RUNNING")
        with self.assertRaisesRegex(bridge.BridgeError, "clear-stale-lock"):
            bridge.cleanup(task, artifact)
        self.assertEqual(bridge.clear_stale_lock("hello-world")["status"], "STALE_LOCK_ARCHIVED")
        time.sleep(1)  # the job object ends the worker; give Windows a moment to release its directory
        self.assert_cleanable(task, artifact)


class InterruptedSegments(BridgeCase):
    def die_after_editing(self, content):
        def die(*args, **kwargs):
            (args[2] / "hello.py").write_text(content, encoding="utf-8")
            raise RuntimeError("killed mid-segment")

        return mock.patch.object(bridge, "_segment", side_effect=die)

    def test_a_continue_that_died_mid_segment_can_simply_be_continued_again(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        task = self.task()
        first = bridge.run(task)
        artifact = Path(first["artifact_directory"])
        with self.die_after_editing("print('Hello, wor')\n"):
            with self.assertRaisesRegex(RuntimeError, "killed"):
                bridge.continue_task(task, artifact, 4)
        record = self.record(artifact)
        self.assertEqual(record["lifecycle_status"], "EXTENSION_REQUESTED")
        self.assertIn("killed mid-segment", record["segment_in_progress"]["error"])
        self.assertIn("interrupted_segment", bridge.brief(record))
        resumed = bridge.continue_task(task, artifact, 4)
        self.assertEqual(resumed["lifecycle_status"], "REVIEW_PENDING", resumed["failures"])
        self.assertNotIn("segment_in_progress", resumed)

    def test_a_revise_that_died_is_refused_by_accept_and_can_be_adopted_by_revalidate(self):
        task = self.task()
        artifact = Path(bridge.run(task)["artifact_directory"])
        with self.die_after_editing("print('Hello, world!')\n# half-applied review fix\n"):
            with self.assertRaises(RuntimeError):
                bridge.revise_task(task, artifact, self.feedback(), 3)
        with self.assertRaisesRegex(bridge.BridgeError, "interrupted"):
            bridge.accept(task, artifact)
        self.assertFalse((self.repo / "hello.py").exists())
        outcome = bridge.revalidate(task, artifact, 30)
        self.assertEqual(outcome["status"], "passed")
        adopted = self.record(artifact)
        self.assertNotIn("segment_in_progress", adopted)
        self.assertTrue(any("interrupted segment" in w for w in adopted["warnings"]))
        self.assertEqual(bridge.accept(task, artifact)["status"], "accepted")
        self.assertIn("half-applied", (self.repo / "hello.py").read_text(encoding="utf-8"))

    def test_revalidate_keeps_a_pending_extension_continuable(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        task = self.task()
        artifact = Path(bridge.run(task)["artifact_directory"])
        outcome = bridge.revalidate(task, artifact, 30)
        self.assertEqual(outcome["status"], "failed")  # the partial hello.py does not pass the tests yet
        record = self.record(artifact)
        self.assertEqual((record["status"], record["lifecycle_status"]), ("extension_requested", "EXTENSION_REQUESTED"))
        self.assertEqual(record["failures"], [])
        self.assertEqual(record["validation"]["status"], "failed")
        done = bridge.continue_task(task, artifact, 4)
        self.assertEqual(done["lifecycle_status"], "REVIEW_PENDING", done["failures"])

    def test_revalidate_refuses_an_artifact_without_a_worktree(self):
        task = self.task()
        artifact = Path(bridge.run(task)["artifact_directory"])
        bridge.cleanup(task, artifact)
        with self.assertRaisesRegex(bridge.BridgeError, "preserved worktree"):
            bridge.revalidate(task, artifact)


class AutomaticContinuation(BridgeCase):
    def test_a_failed_precheck_between_segments_keeps_the_first_result(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        real = bridge.preflight
        calls = []

        def second_check_fails():
            calls.append(1)
            if len(calls) > 1:
                raise codexcli.CodexCliError("Codex is not signed in")
            return real()

        with mock.patch.object(bridge, "preflight", side_effect=second_check_fails):
            result = bridge.run(self.task(auto_continue=1))
        self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED", result["failures"])
        self.assertTrue(any("automatic continuation stopped" in w and "not signed in" in w for w in result["warnings"]),
                        result["warnings"])
        self.assertEqual(len(result["segments"]), 1)


class BlockedClaims(BridgeCase):
    def test_wording_alone_cannot_downgrade_a_blocked_claim(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "environment_blocked"
        self.setenv(CODEX_BRIDGE_WORKER_CHECKS="run")
        result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertIn("Codex reported blocked", result["failures"])
        self.assertEqual(result["validation"]["status"], "not_run")
        self.assertEqual(result["environment_evidence"], [])

    def test_a_policy_rejection_the_bridge_saw_is_evidence(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "environment_blocked"
        self.setenv(CODEX_BRIDGE_WORKER_CHECKS="skip")
        result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertTrue(result["environment_evidence"])

    def test_the_decision_needs_both_wording_and_evidence(self):
        claim = {"blockers": ["Cannot run tests in the sandbox"]}
        self.assertFalse(bridge.blocker_may_be_downgraded(claim, []))
        self.assertFalse(bridge.blocker_may_be_downgraded(claim, None))
        self.assertTrue(bridge.blocker_may_be_downgraded(claim, ["Codex rejected 1 worker command(s)"]))
        self.assertFalse(bridge.blocker_may_be_downgraded({"blockers": ["Need an owner decision"]}, ["x"]))


class ReadOnlyTasks(BridgeCase):
    """Read-only tasks read the lead's own checkout, which the lead may keep editing."""

    def review_task(self):
        return self.task(mode="review", allowed_changed_paths=[], validation_command=None)

    def test_a_read_only_extension_can_be_continued_after_the_lead_edited_the_checkout(self):
        task = self.review_task()
        os.environ["FAKE_CODEX_SCENARIO"] = "extension_empty"
        first = bridge.run(task)
        self.assertEqual(first["lifecycle_status"], "EXTENSION_REQUESTED", first["failures"])
        (self.repo / "README.md").write_text("edited by the lead between the two segments\n", encoding="utf-8")
        os.environ["FAKE_CODEX_SCENARIO"] = "hello"
        second = bridge.continue_task(task, Path(first["artifact_directory"]), 2)
        self.assertEqual(second["lifecycle_status"], "REVIEW_PENDING", second["failures"])

    def test_revalidate_says_not_run_when_there_is_no_validation_command(self):
        task = self.review_task()
        result = bridge.run(task)
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        outcome = bridge.revalidate(task, Path(result["artifact_directory"]))
        self.assertEqual(outcome["status"], "not_run")
        self.assertEqual(outcome["validation"]["status"], "not_run")


class RetriedLabels(BridgeCase):
    def test_a_retried_label_never_reads_the_previous_attempts_last_message(self):
        task = load_task(self.task())
        artifact = self.tmp / "artifact"
        artifact.mkdir()
        stale = (artifact / "initial.last-message.txt")
        stale.write_text(json.dumps(fake_codex.claim()), encoding="utf-8")
        silent = bridge.ProcessResult(0, "", "", 0, False, False, False)
        with mock.patch.object(bridge, "run_process", return_value=silent):
            _, _, final = bridge._run_codex(codexcli.find_codex(), task, self.tmp, artifact, "initial", "prompt",
                                            sandbox="workspace-write", effort=None, resume=None, timeout=5)
        self.assertEqual(final, "")
        self.assertFalse(stale.exists())


class CapturedOutputIsBounded(BridgeCase):
    def test_a_truncated_worker_transcript_is_reported(self):
        with mock.patch.object(process, "OUTPUT_HEAD_CHARS", 120), mock.patch.object(process, "OUTPUT_TAIL_CHARS", 700):
            os.environ["FAKE_CODEX_SCENARIO"] = "hello"
            result = bridge.run(self.task(), effort=None)
        events = (Path(result["artifact_directory"]) / "initial.events.jsonl").read_text(encoding="utf-8")
        self.assertIn("characters omitted by the bridge", events)
        self.assertTrue(result["segments"][0]["process"]["output_truncated"])
        self.assertTrue(any("exceeded the capture limit" in w and "saved transcript" in w for w in result["warnings"]),
                        result["warnings"])
        # The events were read as they arrived, so the dropped middle (the command record) is not lost.
        segment = result["segments"][0]
        self.assertTrue(segment["events_complete"])
        self.assertEqual(segment["commands_run"], 1)
        self.assertNotIn("cat README.md", events)
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])  # the claim is in the tail

    def test_event_totals_are_marked_incomplete_only_after_truncation(self):
        line = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}})
        self.assertTrue(bridge.parse_events(line)["events_complete"])
        self.assertFalse(bridge.parse_events(line, truncated=True)["events_complete"])

    def test_event_summary_reads_every_line_and_bounds_what_it_keeps(self):
        summary = bridge.EventSummary()
        summary.feed(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        summary.feed("not json")
        for _ in range(3):
            summary.feed(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}))
        summary.feed(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                                                                    "command": "x" * 10_000, "exit_code": 1}}))
        parsed = summary.result()
        self.assertEqual((parsed["thread_id"], parsed["usage"], parsed["event_count"]), ("t1", {"input_tokens": 15}, 5))
        self.assertEqual(len(parsed["commands"][0]["command"]), bridge.MAX_EVENT_TEXT_CHARS)
        self.assertTrue(parsed["events_complete"])
        with mock.patch.object(bridge, "MAX_EVENT_COMMANDS", 1):
            summary.feed(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "y"}}))
        self.assertEqual(len(summary.commands), 1)
        self.assertFalse(summary.result()["events_complete"])
        self.assertFalse(bridge.EventSummary().result(lines_skipped=1)["events_complete"])

    def test_message_and_error_text_kept_by_the_summary_is_bounded(self):
        summary = bridge.EventSummary()
        summary.feed(json.dumps({"type": "error", "message": "e" * 50_000}))
        summary.feed(json.dumps({"type": "turn.failed", "error": {"message": "f" * 50_000}}))
        summary.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "m" * 500_000}}))
        parsed = summary.result()
        self.assertEqual(len(parsed["errors"][0]), bridge.MAX_EVENT_TEXT_CHARS)
        self.assertEqual(len(parsed["turn_failed"]), bridge.MAX_EVENT_TEXT_CHARS)
        self.assertEqual(len(parsed["messages"][0]), bridge.MAX_EVENT_MESSAGE_CHARS)

    def test_an_unreadable_event_line_blocks_the_result_but_a_full_record_list_only_warns(self):
        self.assertTrue(bridge.EventSummary().result(lines_skipped=1)["events_unreadable"])
        self.assertTrue(bridge.EventSummary().result(truncated=True)["events_unreadable"])
        full = bridge.EventSummary()
        with mock.patch.object(bridge, "MAX_EVENT_COMMANDS", 0):
            full.feed(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "y"}}))
        self.assertEqual((full.result()["events_complete"], full.result()["events_unreadable"]), (False, False))
        os.environ["FAKE_CODEX_SCENARIO"] = "hello"
        with mock.patch.object(bridge, "MAX_EVENT_COMMANDS", 0):
            overflow = bridge.run(self.task(), effort=None)
        self.assertEqual(overflow["lifecycle_status"], "REVIEW_PENDING", overflow["failures"])
        self.assertFalse(overflow["segments"][0]["events_complete"])
        self.assertTrue(any("not recorded" in w for w in overflow["warnings"]), overflow["warnings"])
        with mock.patch.object(process, "MAX_LINE_CHARS", 20):
            skipped = bridge.run(self.task(), effort=None)
        self.assertEqual(skipped["status"], "failed")
        self.assertTrue(skipped["segments"][0]["events_unreadable"])
        self.assertTrue(any("could not be read" in f for f in skipped["failures"]), skipped["failures"])
        self.assertEqual(skipped["lifecycle_status"], "BLOCKED")

    def test_a_stream_abandoned_at_return_makes_the_events_unreadable(self):
        task = load_task(self.task())
        artifact = self.tmp / "artifact"
        artifact.mkdir()
        abandoned = bridge.ProcessResult(0, "", "", 0, False, False, False, stdout_abandoned=True)
        with mock.patch.object(bridge, "run_process", return_value=abandoned):
            _, parsed, _ = bridge._run_codex(codexcli.find_codex(), task, self.tmp, artifact, "initial", "prompt",
                                             sandbox="workspace-write", effort=None, resume=None, timeout=5)
        self.assertEqual((parsed["events_complete"], parsed["events_unreadable"]), (False, True))

    def test_a_noisy_validation_keeps_its_head_and_tail_and_says_so(self):
        script = "print('HEAD-MARK'); print('x' * 5000); print('TAIL-MARK'); raise SystemExit(1)"
        with mock.patch.object(process, "OUTPUT_HEAD_CHARS", 100), mock.patch.object(process, "OUTPUT_TAIL_CHARS", 100):
            result = bridge.run(self.task(validation_command=[sys.executable, "-c", script]))
        log = (Path(result["artifact_directory"]) / "validation.stdout.log").read_text(encoding="utf-8")
        self.assertTrue(log.startswith("HEAD-MARK"))
        self.assertTrue(log.rstrip().endswith("TAIL-MARK"))
        self.assertLess(len(log), 400)
        self.assertTrue(result["validation"]["process"]["output_truncated"])
        self.assertTrue(any("validation output exceeded" in w for w in result["warnings"]), result["warnings"])


class ValidationProgramIsThePatchs(BridgeCase):
    extra_files = {"tools/check.cmd": b"@exit /b 0\n"}

    def test_a_relative_program_is_never_taken_from_the_primary_checkout(self):
        task = load_task(self.task(validation_command=["tools/check.cmd"], allowed_changed_paths=["hello.py", "tools/**"]))
        worktree = self.tmp / "no-fallback"
        gitops.create_worktree(self.repo, worktree, "delegate/codex-no-fallback", self.base)
        self.assertEqual(bridge._resolve_validation_executable(task, worktree), (worktree / "tools/check.cmd").resolve())
        (worktree / "tools" / "check.cmd").unlink()  # the patch deleted the wrapper; the primary copy still exists
        self.assertEqual(bridge._resolve_validation_executable(task, None), (self.repo / "tools/check.cmd").resolve())
        self.assertIsNone(bridge._resolve_validation_executable(task, worktree))
        record, error = bridge._validation(task, worktree, self.tmp)
        self.assertEqual((record["status"], error), ("failed", "validation executable not found"))


class IgnoredInputsAreChecked(BridgeCase):
    extra_files = {".gitignore": b"local.properties\nnotes.log\n"}

    def setUp(self):
        super().setUp()
        (self.repo / "local.properties").write_text("sdk=real\n", encoding="utf-8")

    def test_a_worker_edit_to_a_copy_ignored_input_fails_the_result(self):
        result = self.run_with_edits(
            self.task(copy_ignored=["local.properties"]),
            lambda worktree: (worktree / "local.properties").write_text("sdk=rigged\n", encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(f.startswith(bridge.INPUT_FAILURE) and "local.properties" in f for f in result["failures"]),
                        result["failures"])
        self.assertEqual(result["validation"]["status"], "not_run")

    def test_a_worker_deleting_a_copy_ignored_input_fails_the_result(self):
        result = self.run_with_edits(self.task(copy_ignored=["local.properties"]),
                                     lambda worktree: (worktree / "local.properties").unlink())
        self.assertTrue(any(f.startswith(bridge.INPUT_FAILURE) for f in result["failures"]), result["failures"])

    def test_a_validation_that_rewrites_an_input_is_not_blamed_on_the_worker(self):
        script = "open('local.properties', 'w').write('sdk=regenerated')"
        task = self.task(copy_ignored=["local.properties"], validation_command=[sys.executable, "-c", script])
        result = bridge.run(task)
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["inputs_changed_by_worker"], [])
        artifact = Path(result["artifact_directory"])
        revalidated = bridge.revalidate(task, artifact, 30)
        self.assertEqual(revalidated["lifecycle_status"], "REVIEW_PENDING", revalidated)
        outcome = bridge.accept(task, artifact)
        self.assertEqual(outcome["status"], "accepted")
        self.assertTrue(any("copy_ignored input" in w for w in outcome["warnings"]), outcome["warnings"])

    def test_a_revision_after_validation_rewrote_an_input_is_not_blamed_on_the_worker(self):
        script = "open('local.properties', 'w').write('sdk=regenerated'); raise SystemExit(1)"
        task = self.task(copy_ignored=["local.properties"], validation_command=[sys.executable, "-c", script])
        first = bridge.run(task)
        self.assertEqual(first["failures"], ["independent validation failed"])
        self.assertTrue(first["copy_ignored"][0]["validated_sha256"])
        feedback = self.tmp / "feedback.json"
        feedback.write_text(json.dumps({"findings": [{"path": "hello.py", "issue": "Tests fail",
                                                     "expected_behavior": "Make the tests pass"}]}))
        revised = bridge.revise_task(task, Path(first["artifact_directory"]), feedback, 3)
        self.assertFalse(any(f.startswith(bridge.INPUT_FAILURE) for f in revised["failures"]), revised["failures"])
        self.assertEqual(revised["inputs_changed_by_worker"], [])

    def test_revalidate_catches_an_input_edited_by_an_interrupted_segment(self):
        task = self.task(copy_ignored=["local.properties"])
        result = bridge.run(task)
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        artifact = Path(result["artifact_directory"])
        (Path(result["worktree"]) / "local.properties").write_text("sdk=rigged", encoding="utf-8")
        revalidated = bridge.revalidate(task, artifact, 30)
        self.assertTrue(any(f.startswith(bridge.INPUT_FAILURE) for f in revalidated["failures"]), revalidated)
        self.assertEqual(self.record(artifact)["inputs_changed_by_worker"], ["local.properties"])
        with self.assertRaises(bridge.BridgeError):  # no longer review-ready, so accept refuses
            bridge.accept(task, artifact)

    def test_untouched_inputs_still_pass_and_record_their_hash(self):
        result = bridge.run(self.task(copy_ignored=["local.properties"]))
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["copy_ignored"][0]["sha256"], hashlib.sha256((self.repo / "local.properties").read_bytes()).hexdigest())

    def test_edits_to_other_existing_ignored_files_are_found_but_build_output_is_not(self):
        worktree = self.tmp / "stamps"
        gitops.create_worktree(self.repo, worktree, "delegate/codex-stamps", self.base)
        (worktree / "notes.log").write_text("one", encoding="utf-8")
        (worktree / "build").mkdir()
        (worktree / "build" / "out.txt").write_text("one", encoding="utf-8")
        names = ["notes.log", "build/out.txt"]
        before = bridge._ignored_stamps(worktree, names)
        self.assertEqual(bridge._edited_ignored_files(worktree, before), [])
        (worktree / "notes.log").write_text("longer text", encoding="utf-8")
        (worktree / "build" / "out.txt").write_text("longer text", encoding="utf-8")
        self.assertEqual(bridge._edited_ignored_files(worktree, before), ["notes.log"])
        self.assertEqual(bridge._edited_ignored_files(worktree, before, skip=["notes.log"]), [])
        (worktree / "notes.log").unlink()
        self.assertEqual(bridge._edited_ignored_files(worktree, before), ["notes.log"])


class CopyIgnoredBatching(BridgeCase):
    extra_files = {".gitignore": b"local/\n"}

    def test_copying_ignored_files_asks_git_in_one_call(self):
        local = self.repo / "local"
        local.mkdir()
        for number in range(25):
            (local / f"f{number}.cfg").write_text(str(number), encoding="utf-8")
        task = load_task(self.task(copy_ignored=["local"]))
        inventory = bridge._ignored_inventory(task)
        worktree = self.tmp / "copy-target"
        gitops.create_worktree(self.repo, worktree, "delegate/codex-batch-test", self.base)
        calls = self.spy_on_git()
        bridge._copy_ignored(task, worktree, inventory)
        self.assertEqual(len([argv for argv, _ in calls if "ls-files" in argv]), 1)
        self.assertEqual(len(list((worktree / "local").glob("*.cfg"))), 25)


if __name__ == "__main__":
    unittest.main()
