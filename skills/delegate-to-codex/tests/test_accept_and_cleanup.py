"""Accepting, rejecting and cleaning up: review binding, repeated commands, and records without a validation command.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import BridgeCase, git, remove_tree
from claude_codex_bridge import bridge, cli, gitops
from claude_codex_bridge.contracts import ContractError, load_task


class AcceptChecksSuppliedInputs(BridgeCase):
    extra_files = {".gitignore": b"local.properties\n"}

    def test_accept_refuses_when_the_worker_edited_a_copy_ignored_input(self):
        (self.repo / "local.properties").write_text("sdk=real\n", encoding="utf-8")
        task = self.task(copy_ignored=["local.properties"])
        result = bridge.run(task)
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["inputs_changed_by_worker"], [])
        artifact = Path(result["artifact_directory"])
        record = self.record(artifact)
        record["inputs_changed_by_worker"] = ["local.properties"]  # the worker-attributed verdict is what accept trusts
        (artifact / "result.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "copy_ignored input"):
            bridge.accept(task, artifact)
        self.assertFalse((self.repo / "hello.py").exists())

    def test_a_record_without_an_input_verdict_is_refused(self):
        (self.repo / "local.properties").write_text("sdk=real\n", encoding="utf-8")
        task = self.task(copy_ignored=["local.properties"])
        artifact = Path(bridge.run(task)["artifact_directory"])
        record = self.record(artifact)
        del record["inputs_changed_by_worker"]  # written before the verdict was recorded
        (artifact / "result.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "without a verdict"):
            bridge.accept(task, artifact)
        revalidated = bridge.revalidate(task, artifact)
        self.assertTrue(any("without a verdict" in f for f in revalidated["failures"]), revalidated["failures"])
        self.assertFalse((self.repo / "hello.py").exists())

    def test_a_later_change_to_an_input_the_worker_left_alone_only_warns_at_accept(self):
        (self.repo / "local.properties").write_text("sdk=real\n", encoding="utf-8")
        task = self.task(copy_ignored=["local.properties"])
        result = bridge.run(task)
        (Path(result["worktree"]) / "local.properties").write_text("sdk=changed\n", encoding="utf-8")
        outcome = bridge.accept(task, Path(result["artifact_directory"]))
        self.assertEqual(outcome["status"], "accepted")
        self.assertTrue(any("copy_ignored input" in w for w in outcome["warnings"]), outcome["warnings"])


class AcceptCleanup(BridgeCase):
    def flaky_branch_delete(self):
        real = bridge._git_retry

        def flaky(cwd, args, **kwargs):
            if list(args[:2]) == ["branch", "-D"]:
                raise bridge.BridgeError("cannot lock ref")
            return real(cwd, args, **kwargs)

        return mock.patch.object(bridge, "_git_retry", side_effect=flaky)

    def test_a_cleanup_that_fails_after_the_patch_applied_is_reported_and_can_be_finished(self):
        task = self.task()
        result = bridge.run(task)
        artifact, worktree = Path(result["artifact_directory"]), Path(result["worktree"])
        with self.flaky_branch_delete():
            outcome = bridge.accept(task, artifact)
        self.assertEqual(outcome["status"], "accepted")
        self.assertTrue(outcome["cleanup_pending"])
        self.assertIn("cannot lock ref", outcome["cleanup_error"])
        self.assertIn("cleanup", outcome["next_action"])
        applied = (self.repo / "hello.py").read_bytes()
        record = self.record(artifact)
        self.assertTrue(record["applied_at"])
        self.assertNotIn("cleaned_at", record)
        self.assertFalse(worktree.exists())
        self.assertNotEqual(self.branches(), "")
        again = bridge.accept(task, artifact)  # idempotent: never applies twice, finishes the cleanup
        self.assertEqual(again["status"], "accepted")
        self.assertTrue(again["cleaned_at"])
        self.assertEqual(self.branches(), "")
        self.assertEqual((self.repo / "hello.py").read_bytes(), applied)

    def test_cleanup_command_finishes_what_accept_could_not(self):
        task = self.task()
        artifact = Path(bridge.run(task)["artifact_directory"])
        with self.flaky_branch_delete():
            self.assertTrue(bridge.accept(task, artifact)["cleanup_pending"])
        done = bridge.cleanup(task, artifact)
        self.assertEqual(done["status"], "accepted")
        self.assertTrue(done["cleaned_at"])
        self.assertEqual(self.branches(), "")

    def test_cleanup_works_when_the_worktree_directory_was_deleted_by_hand(self):
        task = self.task()
        result = bridge.run(task)
        artifact, worktree = Path(result["artifact_directory"]), Path(result["worktree"])
        remove_tree(worktree)
        done = bridge.cleanup(task, artifact)
        self.assertEqual(done["status"], "rejected")
        self.assertEqual(self.branches(), "")
        self.assertNotIn("prunable", git(self.repo, "worktree", "list", "--porcelain"))

    def test_cleanup_works_when_the_branch_is_already_gone(self):
        task = self.task()
        result = bridge.run(task)
        artifact, worktree = Path(result["artifact_directory"]), Path(result["worktree"])
        git(self.repo, "worktree", "remove", "--force", str(worktree))
        git(self.repo, "branch", "-D", self.record(artifact)["branch"])
        self.assertEqual(bridge.cleanup(task, artifact)["status"], "rejected")

    def test_the_branch_is_recorded_when_the_worktree_is_created(self):
        result = bridge.run(self.task())
        artifact = Path(result["artifact_directory"])
        self.assertEqual(result["branch"], git(Path(result["worktree"]), "branch", "--show-current").strip())
        self.assertEqual(json.loads((artifact / "worktree.json").read_text(encoding="utf-8"))["branch"],
                         result["branch"])
        self.assertTrue(result["pin"]["git_dir"])

    def test_accepting_an_empty_patch_says_so(self):
        task = self.task(validation_command=[sys.executable, "-B", "-c", "pass"])

        def nothing(worktree):
            (worktree / "hello.py").unlink()

        segment = bridge._segment

        def edited(*args, **kwargs):
            seg = segment(*args, **kwargs)
            nothing(args[2])
            seg["claim"]["files_changed"] = []
            return seg

        with mock.patch.object(bridge, "_segment", side_effect=edited):
            result = bridge.run(task)
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertTrue(any("changed nothing" in w for w in result["warnings"]), result["warnings"])
        outcome = bridge.accept(task, Path(result["artifact_directory"]))
        self.assertEqual(outcome["status"], "accepted")
        self.assertTrue(any("patch is empty" in w for w in outcome["warnings"]))
        self.assertFalse((self.repo / "hello.py").exists())


class ReviewBinding(BridgeCase):
    def test_the_report_carries_the_tree_and_patch_hash_and_accept_checks_them(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        digest = hashlib.sha256((artifact / "diff.patch").read_bytes()).hexdigest()
        self.assertEqual(result["patch_sha256"], digest)
        self.assertEqual(result["snapshot_tree"], result["segments"][-1]["tree"])
        self.assertEqual(bridge.brief(result)["review_binding"], {"snapshot_tree": result["snapshot_tree"],
                                                                 "patch_sha256": digest})
        before = (artifact / "result.json").read_bytes()
        with self.assertRaisesRegex(bridge.BridgeError, "snapshot tree"):
            bridge.accept(task, artifact, expect_tree="0" * 12)
        with self.assertRaisesRegex(bridge.BridgeError, "patch"):
            bridge.accept(task, artifact, expect_patch_sha256="f" * 64)
        self.assertEqual((artifact / "result.json").read_bytes(), before)
        self.assertFalse((self.repo / "hello.py").exists())
        outcome = bridge.accept(task, artifact, expect_tree=result["snapshot_tree"][:12],
                                expect_patch_sha256=digest.upper())
        self.assertEqual(outcome["status"], "accepted")

    def test_the_cli_takes_the_expectations(self):
        task = self.task()
        result = bridge.run(task)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main(["accept", "--task", str(task), "--artifact", result["artifact_directory"],
                             "--expect-tree", "1" * 40])
        self.assertEqual(code, 2)
        self.assertIn("snapshot tree", json.loads(err.getvalue())["error"])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = cli.main(["accept", "--task", str(task), "--artifact", result["artifact_directory"],
                             "--expect-tree", result["snapshot_tree"], "--expect-patch-sha256", result["patch_sha256"]])
        self.assertEqual(code, 0, out.getvalue())

    def test_latest_is_refused_when_it_is_not_the_artifact_last_reported(self):
        task = self.task()
        older = bridge.run(task)
        newer = bridge.run(task)
        pointer = self.state / "artifacts" / "codex" / ".reported" / "hello-world.json"
        reported = json.loads(pointer.read_text(encoding="utf-8"))
        newer_name = Path(newer["artifact_directory"]).name
        self.assertEqual(reported["artifact"], newer_name)
        # As if the newest artifact had never been shown to the lead: only the older one was reported.
        reported["artifact"] = Path(older["artifact_directory"]).name
        pointer.write_text(json.dumps(reported), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "last reported"):
            bridge.accept(task, Path("latest"))
        self.assertFalse((self.repo / "hello.py").exists())
        self.assertEqual(bridge.accept(task, Path(newer["artifact_directory"]))["status"], "accepted")

    def test_latest_is_refused_when_the_patch_changed_after_it_was_reported(self):
        task = self.task()
        result = bridge.run(task)
        pointer = self.state / "artifacts" / "codex" / ".reported" / "hello-world.json"
        reported = json.loads(pointer.read_text(encoding="utf-8"))
        reported["patch_sha256"] = "0" * 64
        pointer.write_text(json.dumps(reported), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "changed after it was last reported"):
            bridge.accept(task, Path("latest"))
        self.assertEqual(bridge.accept(task, Path(result["artifact_directory"]))["status"], "accepted")

    def test_latest_is_refused_when_nothing_records_what_was_reported(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        (artifact.parent / ".reported" / "hello-world.json").unlink()
        with self.assertRaisesRegex(bridge.BridgeError, "cannot be confirmed"):
            bridge.accept(task, Path("latest"))
        self.assertEqual(bridge.accept(task, artifact)["status"], "accepted")

    def test_latest_is_refused_when_the_report_record_is_not_an_object(self):
        task = self.task()
        result = bridge.run(task)
        (Path(result["artifact_directory"]).parent / ".reported" / "hello-world.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "cannot be confirmed"):
            bridge.accept(task, Path("latest"))


class Records(BridgeCase):
    def test_result_and_patch_files_are_published_by_atomic_replacement(self):
        destinations = []
        real = os.replace

        def spy(source, destination):
            destinations.append(Path(destination).name)
            return real(source, destination)

        with mock.patch.object(gitops.os, "replace", side_effect=spy):
            bridge.run(self.task())
        for name in ("result.json", "diff.patch", "task.json", "primary-status.before.txt"):
            self.assertIn(name, destinations)

    def test_a_damaged_record_of_another_task_does_not_break_latest(self):
        task = self.task()
        result = bridge.run(task)
        other = self.state / "artifacts" / "codex" / "hello-world-extra-20990101T000000000000Z"
        other.mkdir()
        (other / "result.json").write_text('{"task_id": "hello-world-ex', encoding="utf-8")
        self.assertEqual(bridge.resolve_artifact(Path("latest"), task), Path(result["artifact_directory"]))

    def test_a_damaged_newest_record_of_this_task_is_reported_not_skipped(self):
        task = self.task()
        bridge.run(task)
        newest = self.state / "artifacts" / "codex" / "hello-world-20990101T000000000000Z"
        newest.mkdir()
        (newest / "result.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
            bridge.resolve_artifact(Path("latest"), task)

    def test_a_task_file_that_is_not_an_object_is_a_structured_error(self):
        broken = self.tmp / "broken.json"
        broken.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "JSON object"):
            bridge.resolve_artifact(Path("latest"), broken)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.main(["show-diff", "--artifact", "latest", "--task", str(broken)]), 2)
        self.assertEqual(json.loads(err.getvalue())["status"], "failed")

    def test_the_usage_after_a_run_says_where_it_came_from(self):
        result = bridge.run(self.task())
        after = result["usage_after"]
        self.assertEqual(after["after_run_check"], "launch_gate_snapshot")
        self.assertTrue(after["measured_at"])
        shown = bridge.brief(result)["usage"]
        self.assertEqual(shown["source"], "launch_gate_snapshot")
        self.assertIn("age_seconds", shown)
        self.assertTrue(shown["measured_at"])

    def test_an_unreadable_record_is_a_bridge_error_not_a_traceback(self):
        task = self.task()
        artifact = Path(bridge.run(task)["artifact_directory"])
        (artifact / "result.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
            bridge.cleanup(task, artifact)


class CleanupWithoutSession(BridgeCase):
    def test_failed_launch_without_a_session_can_be_cleaned_up(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        record = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
        record.update(status="failed", lifecycle_status="BLOCKED", session_id=None,
                      failures=["missing structured reply"])
        for segment in record["segments"]:
            segment["session_id"] = None
        (artifact / "result.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(Exception, "resumable"):
            bridge.revise_task(task, artifact, self.tmp / "feedback.json", 3)
        self.assertTrue(Path(result["worktree"]).exists())
        cleaned = bridge.cleanup(task, artifact)
        self.assertEqual(cleaned["status"], "rejected")
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertTrue((artifact / "result.json").exists())

    def test_cleanup_still_refuses_a_foreign_artifact(self):
        task = self.task()
        result = bridge.run(task)
        other = self.task(task_id="other-task")
        with self.assertRaises(Exception):
            bridge.cleanup(other, Path(result["artifact_directory"]))
        self.assertTrue(Path(result["worktree"]).exists())


class ArtifactWithoutValidationCommand(BridgeCase):
    """Artifacts of 1.0.0, whose tasks could omit validation_command, stay cleanable and readable; new tasks cannot."""

    def artifact_without_validation(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        for path in (task, artifact / "task.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("validation_command", None)
            path.write_text(json.dumps(data), encoding="utf-8")
        return task, artifact, result

    def test_the_new_rule_still_applies_to_new_tasks(self):
        task, _, _ = self.artifact_without_validation()
        with self.assertRaisesRegex(ContractError, "validation_command"):
            load_task(task)
        with self.assertRaisesRegex(ContractError, "validation_command"):
            bridge.run(task)
        self.assertEqual(load_task(task, allow_missing_validation=True).validation_command, None)

    def test_artifact_without_validation_can_be_cleaned_up(self):
        task, artifact, result = self.artifact_without_validation()
        self.assertTrue(Path(result["worktree"]).exists())
        cleaned = bridge.cleanup(task, artifact)
        self.assertEqual(cleaned["status"], "rejected")
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertTrue((artifact / "result.json").exists())
        self.assertEqual(bridge.cleanup(task, artifact)["status"], "rejected")

    def test_artifact_without_validation_can_be_inspected(self):
        _, artifact, result = self.artifact_without_validation()
        self.assertIn("hello", bridge.show_diff(artifact))
        record = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(bridge.brief(record)["task_id"], result["task_id"])

    def test_artifact_without_validation_cannot_be_accepted_or_revised(self):
        task, artifact, _ = self.artifact_without_validation()
        with self.assertRaisesRegex(ContractError, "validation_command"):
            bridge.accept(task, artifact)
        with self.assertRaisesRegex(ContractError, "validation_command"):
            bridge.revise_task(task, artifact, self.tmp / "feedback.json", 3)
        self.assertFalse((self.repo / "hello.py").exists())


class ValidationRunsBeforeThePatchIsRecorded(BridgeCase):
    def test_validation_edit_inside_scope_reaches_the_patch(self):
        edit = "open('hello.py', 'a').write('# formatted by validation\\n')"
        result = bridge.run(self.task(validation_command=[sys.executable, "-B", "-c", edit]))
        patch = Path(result["diff_path"]).read_text(encoding="utf-8")
        self.assertIn("# formatted by validation", patch)
        self.assertEqual(result["validation"]["status"], "passed")

    def test_validation_created_out_of_scope_file_is_rejected_and_in_the_patch(self):
        create = "open('generated.txt', 'w').write('x')"
        result = bridge.run(self.task(validation_command=[sys.executable, "-B", "-c", create]))
        self.assertIn("generated.txt", result["unauthorized_changed_paths"])
        self.assertIn("out-of-scope paths changed", result["failures"])
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertIn("generated.txt", Path(result["diff_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
