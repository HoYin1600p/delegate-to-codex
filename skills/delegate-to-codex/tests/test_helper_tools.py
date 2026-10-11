"""The helper scripts: bridge_report, task_lint, task_template and land.

They run on synthetic result files and a temporary Git repository; no provider is contacted.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from support import SKILL, git, init_repo, remove_tree

sys.path.insert(0, str(SKILL / "scripts"))
import bridge_report  # noqa: E402
import land  # noqa: E402
import task_lint  # noqa: E402
import task_template  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Fixture",
           "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Fixture",
           "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
TREE = "a" * 40
SHA = "b" * 64


def capture(function, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = function(*args)
    return code, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(remove_tree, self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path


class ReportTests(TempCase):
    def artifact(self, lifecycle="BLOCKED", validation="failed", binding=True):
        folder = self.root / "artifact"
        worktree = self.root / "state" / "wt" / "abc123"
        record = {
            "task_id": "demo", "status": "failed" if lifecycle == "BLOCKED" else "complete",
            "lifecycle_status": lifecycle, "artifact_directory": str(folder), "worktree": str(worktree),
            "repository": str(self.root / "repo"), "changed_paths": ["src/A.java"],
            "failures": ["independent validation failed"] if validation == "failed" else [],
            "warnings": [], "codex_claim": {"status": "complete", "summary": "did it", "findings": ["f1"],
                                           "blockers": [], "checks": [{"reported_outcome": "not_run", "description": "build"}]},
            "validation": {"status": validation, "process": {"orphans_killed": True}},
            "diffstat": {"stat": "", "files": [{"path": "src/A.java", "added": 5, "removed": 2}]},
        }
        if binding:
            record.update(snapshot_tree=TREE, patch_sha256=SHA)
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(record), encoding="utf-8")
        prefix = str(worktree) + os.sep
        (folder / "validation.stdout.log").write_text(
            f"{prefix}src{os.sep}A.java:10: error: cannot find symbol\n"
            f"{prefix}src{os.sep}A.java:10: error: cannot find symbol\n"
            f"{prefix}src{os.sep}B.java:3: error: ';' expected\n"
            "FooTest > bar() FAILED\n    java.lang.AssertionError\nFooTest > bar() FAILED\n", encoding="utf-8")
        (folder / "validation.stderr.log").write_text(
            "FAILURE: Build failed with an exception.\n\n* What went wrong:\nExecution failed for task ':compileJava'.\n"
            "> Compilation failed; see the compiler error output for details.\n\n* Try:\n> Run with --stacktrace\n",
            encoding="utf-8")
        return folder

    def test_failed_validation_lists_unique_shortened_errors_and_a_revise_skeleton(self):
        folder = self.artifact()
        code, out, _ = capture(bridge_report.main, [str(folder), "--task", "T.json"])
        self.assertEqual(code, 0)
        errors = json.loads(capture(bridge_report.main, [str(folder), "--json"])[1])["validation_details"]["compiler_errors"]
        self.assertEqual(errors, ["src/A.java:10: error: cannot find symbol", "src/B.java:3: error: ';' expected"])
        self.assertIn("src/B.java:3: error: ';' expected", out)
        self.assertNotIn("abc123", out)
        self.assertIn("Execution failed for task ':compileJava'.", out)
        self.assertNotIn("Run with --stacktrace", out)
        listed = json.loads(capture(bridge_report.main, [str(folder), "--json"])[1])["validation_details"]
        self.assertEqual(listed["failed_tests"], ["FooTest > bar()"])
        self.assertIn("orphans_killed", out)
        self.assertIn("src/A.java  +5 -2", out)
        self.assertIn(" revise --task \"T.json\"", out)
        self.assertIn("--finding", out)
        self.assertNotIn("accept --task", out)

    def test_review_pending_prints_the_bound_accept_command(self):
        folder = self.artifact("REVIEW_PENDING", "passed")
        code, out, _ = capture(bridge_report.main, [str(folder / "result.json"), "--task", "T.json"])
        self.assertIn(f"--expect-tree {TREE} --expect-patch-sha256 {SHA}", out)
        self.assertIn(f"review_binding: snapshot_tree={TREE}", out)
        self.assertNotIn("revise", out)

    def test_stdout_report_json_and_machine_output(self):
        folder = self.artifact("REVIEW_PENDING", "passed")
        brief = {"status": "complete", "lifecycle_status": "REVIEW_PENDING", "task_id": "demo",
                 "artifact": str(folder), "failures": [], "warnings": ["w1"], "changed_paths": ["src/A.java"],
                 "worker": {"status": "complete", "summary": "s", "findings": [], "blockers": [], "checks": []},
                 "validation": {"status": "passed"},
                 "diffstat": {"files": [{"path": "src/A.java", "added": 1, "removed": 0}]},
                 "review_binding": {"snapshot_tree": TREE, "patch_sha256": SHA}}
        stdout_file = self.write("run.json", json.dumps(brief))
        code, out, _ = capture(bridge_report.main, [str(stdout_file), "--json"])
        data = json.loads(out)
        self.assertEqual(data["review_binding"]["snapshot_tree"], TREE)
        self.assertTrue(data["accept_command"])
        self.assertTrue(data["orphans_killed"])

    def test_unreadable_source_exits_with_a_message(self):
        with self.assertRaises(SystemExit) as caught:
            capture(bridge_report.main, [str(self.root / "missing.json")])
        self.assertIn("cannot read", str(caught.exception))


class LintTests(TempCase):
    def base(self, **changes):
        task = {"task_id": "demo", "repo_root": str(self.root), "base_commit": "c" * 40, "mode": "implement",
                "objective": "do it", "allowed_changed_paths": ["src/**", "a.txt"], "acceptance_criteria": ["works"],
                "validation_command": ["python", "-B", "-m", "unittest"]}
        task.update(changes)
        return task

    def fields(self, task):
        return {(f["level"], f["field"]) for f in task_lint.lint(task, self.root / "demo.json")}

    def test_a_good_task_has_no_findings(self):
        self.assertEqual(task_lint.lint(self.base(), self.root / "demo.json"), [])

    def test_prose_may_mention_absolute_paths_outside_the_repo(self):
        task = self.base(objective="Read C:\\Other\\notes.txt and /etc/hosts for background.")
        self.assertEqual(task_lint.lint(task, self.root / "demo.json"), [])

    def test_path_mistakes_are_errors(self):
        self.assertIn(("error", "allowed_changed_paths"), self.fields(self.base(allowed_changed_paths=["src/*/a.py"])))
        self.assertIn(("error", "allowed_changed_paths"), self.fields(self.base(allowed_changed_paths=["src/**/x"])))
        self.assertIn(("error", "context_paths"), self.fields(self.base(context_paths=["C:/x/y.py"])))
        self.assertIn(("error", "context_paths"), self.fields(self.base(context_paths=["a:b"])))
        self.assertIn(("error", "context_paths"), self.fields(self.base(context_paths=["/abs/file"])))

    def test_ranges_and_read_only_rules(self):
        self.assertIn(("error", "max_turns"), self.fields(self.base(max_turns=13)))
        self.assertIn(("error", "auto_continue"), self.fields(self.base(auto_continue=4)))
        review = self.base(mode="review", validation_command=None)
        self.assertIn(("error", "allowed_changed_paths"), self.fields(review))
        review["allowed_changed_paths"] = []
        review["context_paths"] = ["a.txt"]
        self.assertEqual(task_lint.lint(review, self.root / "demo.json"), [])

    def test_validation_command_must_be_an_array(self):
        self.assertIn(("error", "validation_command"), self.fields(self.base(validation_command="python -m unittest")))
        self.assertIn(("error", "validation_command"), self.fields(self.base(validation_command=[])))

    def test_powershell_dotted_option_and_gradle_daemon_warnings(self):
        shell = self.base(validation_command=["pwsh", "-NoProfile", "-Command", "./gradlew.bat build -Pa.b=c --no-daemon"])
        self.assertEqual(self.fields(shell), {("warning", "validation_command")})
        quoted = self.base(validation_command=["pwsh", "-Command", "./gradlew.bat build '-Pa.b=c' --no-daemon"])
        self.assertEqual(task_lint.lint(quoted, self.root / "demo.json"), [])
        gradle = self.base(validation_command=["cmd", "/c", ".\\gradlew.bat", "build"])
        found = task_lint.lint(gradle, self.root / "demo.json")
        self.assertEqual([(f["level"], "--no-daemon" in f["fix"]) for f in found], [("warning", True)])

    def test_contract_errors_surface_and_exit_codes(self):
        self.assertIn(("error", "(contract)"), self.fields(self.base(plan_status="DRAFT")))
        path = self.write("t.json", json.dumps(self.base(max_turns=99)))
        code, out, _ = capture(task_lint.main, [str(path)])
        self.assertEqual(code, 1)
        self.assertIn("fix:", out)
        good = self.write("g.json", json.dumps(self.base()))
        self.assertEqual(capture(task_lint.main, [str(good)])[0], 0)
        warn = self.write("w.json", json.dumps(self.base(validation_command=["gradlew.bat", "build"])))
        self.assertEqual(capture(task_lint.main, [str(warn)])[0], 0)
        self.assertEqual(capture(task_lint.main, [str(warn), "--strict"])[0], 1)


class TemplateTests(TempCase):
    def test_defaults_merge_with_arguments(self):
        defaults = self.write("defaults.json", json.dumps({
            "locked_decisions": ["keep the API"], "acceptance_criteria": ["suite passes"], "risk": "low"}))
        out = self.root / "task.json"
        code, _, err = capture(task_template.main, [
            "--id", "demo", "--mode", "implement", "--objective", "Do it", "--allowed", "src/**", "a.txt",
            "--acceptance", "new thing works", "--defaults", str(defaults), "--repo-root", str(self.root),
            "--base-commit", "d" * 40, "--out", str(out), "--validation", "python", "-B", "-m", "unittest"])
        self.assertEqual(code, 0, err)
        task = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(task["acceptance_criteria"], ["suite passes", "new thing works"])
        self.assertEqual(task["locked_decisions"], ["keep the API"])
        self.assertEqual(task["validation_command"], ["python", "-B", "-m", "unittest"])
        self.assertEqual(task["risk"], "low")
        self.assertEqual(task_lint.lint(task, out), [])

    def test_refuses_to_overwrite_and_marks_missing_criteria(self):
        out = self.write("task.json", "{}")
        args = ["--id", "demo", "--mode", "review", "--objective", "Look", "--context", "a.txt", "--out", str(out)]
        self.assertEqual(capture(task_template.main, args)[0], 2)
        code, _, err = capture(task_template.main, [*args, "--force"])
        self.assertEqual(code, 0)
        task = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(task["allowed_changed_paths"], [])
        self.assertIn("TODO", task["acceptance_criteria"][0])
        self.assertIn("TODO", err)


class LandTests(TempCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, GIT_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        init_repo(self.repo)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8", newline="\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "base")
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8", newline="\n")
        self.patch = git(self.repo, "diff")
        git(self.repo, "checkout", "-q", "--", "a.txt")
        (self.repo / "other.txt").write_text("keep out\n", encoding="utf-8")
        self.artifact = self.root / "artifact"
        self.artifact.mkdir()
        (self.artifact / "diff.patch").write_text(self.patch, encoding="utf-8", newline="\n")
        self.record = {"task_id": "demo", "lifecycle_status": "BLOCKED", "changed_paths": ["a.txt"],
                       "repository": str(self.repo)}
        self.task = self.write("task.json", json.dumps({"task_id": "demo", "repo_root": str(self.repo)}))
        self.save()

    def save(self):
        (self.artifact / "result.json").write_text(json.dumps(self.record), encoding="utf-8")

    def land(self, *extra, bridge=None):
        bridge = bridge or mock.Mock(return_value=(0, {"status": "rejected"}, b""))
        with mock.patch.object(land, "bridge", bridge):
            code, out, _ = capture(land.main, ["--task", str(self.task), "--artifact", str(self.artifact), "--json", *extra])
        return code, json.loads(out), bridge

    def test_apply_build_compare_commit_and_cleanup(self):
        script = self.write("build.py", "from pathlib import Path\nPath('out.jar').write_bytes(Path('made.jar').read_bytes())\n")
        with zipfile.ZipFile(self.repo / "made.jar", "w") as jar:
            jar.writestr("a/B.class", "x")
            jar.writestr("plugin.json", "{}")
        self.write("repo/baseline.txt", "a/B.class\n")
        (self.repo / "expected.bin").write_bytes((self.repo / "made.jar").read_bytes())
        code, summary, bridge = self.land(
            "--apply", "--commit", "land demo", "--compare-listing", "out.jar=baseline.txt",
            "--allow-added", "plugin.json", "--compare-file", "out.jar=expected.bin",
            "--build-json", json.dumps([sys.executable, "-B", str(script)]))
        self.assertEqual((code, summary["status"]), (0, "landed"), summary)
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s").strip(), "land demo")
        self.assertEqual(git(self.repo, "show", "--name-only", "--format=").split(), ["a.txt"])
        self.assertIn("other.txt", git(self.repo, "status", "--porcelain"))
        self.assertEqual(summary["steps"]["compare_listing"][0]["added_allowed"], ["plugin.json"])
        self.assertEqual(bridge.call_args.args[0], "cleanup")

    def test_a_failing_build_stops_before_commit_and_cleanup(self):
        script = self.write("fail.py", "import sys\nsys.exit(3)\n")
        before = git(self.repo, "rev-parse", "HEAD")
        code, summary, bridge = self.land("--apply", "--commit", "x", "--build", sys.executable, "-B", str(script))
        self.assertEqual((code, summary["failed_step"]), (1, "build"))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        bridge.assert_not_called()

    def test_unexpected_jar_entries_fail_the_comparison(self):
        with zipfile.ZipFile(self.repo / "made.jar", "w") as jar:
            jar.writestr("a/B.class", "x")
            jar.writestr("surprise.txt", "x")
        self.write("repo/baseline.txt", "a/B.class\ngone.txt\n")
        code, summary, _ = self.land("--apply", "--compare-listing", "made.jar=baseline.txt")
        self.assertEqual(summary["failed_step"], "compare")
        self.assertIn("+ surprise.txt", summary["error"])
        self.assertIn("- gone.txt", summary["error"])

    def test_a_patch_that_does_not_apply_changes_nothing(self):
        (self.repo / "a.txt").write_text("changed elsewhere\n", encoding="utf-8", newline="\n")
        code, summary, _ = self.land("--apply")
        self.assertEqual(summary["failed_step"], "integrate")
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "changed elsewhere\n")

    def test_accept_path_uses_the_bound_tree_and_hash(self):
        self.record.update(lifecycle_status="REVIEW_PENDING", snapshot_tree=TREE, patch_sha256=SHA)
        self.save()
        bridge = mock.Mock(return_value=(0, {"status": "accepted"}, b""))
        code, summary, _ = self.land("--expect-tree", "f" * 40, bridge=bridge)
        self.assertEqual(summary["status"], "landed", summary)
        call = bridge.call_args.args
        self.assertEqual(call[0], "accept")
        self.assertEqual(call[call.index("--expect-tree") + 1], "f" * 40)
        self.assertEqual(call[call.index("--expect-patch-sha256") + 1], SHA)

    def test_accept_refuses_a_blocked_result_and_reports_a_bridge_failure(self):
        code, summary, _ = self.land()
        self.assertEqual(summary["failed_step"], "integrate")
        self.assertIn("--apply", summary["error"])
        self.record.update(lifecycle_status="REVIEW_PENDING", snapshot_tree=TREE, patch_sha256=SHA)
        self.save()
        failing = mock.Mock(return_value=(1, {"status": "failed", "error": "conflict"}, b""))
        code, summary, _ = self.land(bridge=failing)
        self.assertEqual((code, summary["failed_step"]), (1, "integrate"))
        self.assertIn("conflict", summary["error"])


if __name__ == "__main__":
    unittest.main()
