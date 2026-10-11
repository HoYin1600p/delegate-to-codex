"""Pinned worktrees, links and nested repositories, junctions, files hidden from the patch, and the primary checkout.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import BridgeCase, NT, RepoCase, git, make_junction, remove_tree, rewrite_dotgit
from claude_codex_bridge import bridge, gitops, revision
from claude_codex_bridge.contracts import load_task


class PinnedWorktree(RepoCase):
    def test_creation_records_the_git_dir_and_survives_a_restart(self):
        wt = self.worktree()
        pin = gitops.load_pin(wt)
        self.assertEqual(pin.base_commit, self.base)
        self.assertEqual(pin.git_dir.parent, (self.repo / ".git" / "worktrees").resolve())
        self.assertTrue(gitops.pin_path(wt).is_file())
        self.assertFalse(str(gitops.pin_path(wt)).startswith(str(wt) + os.sep))  # outside the worker's tree
        gitops._pins.clear()  # a new process only has the sidecar
        self.assertEqual(gitops.load_pin(wt), pin)
        self.assertEqual(gitops.worktree_integrity_problems(wt), [])

    def test_rewritten_dotgit_pointer_is_detected_and_never_executed(self):
        evil = self.tmp / "evil"
        git(self.tmp, "clone", "-q", str(self.repo), str(evil))
        wt = self.worktree()
        gitdir = wt / "evilgd"
        shutil.move(str(evil / ".git"), str(gitdir))
        with (gitdir / "config").open("a", encoding="utf-8") as handle:
            handle.write(f"\n[core]\n\tfsmonitor = echo ran > '{(self.flags / 'pwned').as_posix()}'\n")
        rewrite_dotgit(wt, f"gitdir: {gitdir.as_posix()}\n")
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        self.assertTrue(gitops.worktree_integrity_problems(wt))
        self.assertTrue(any(".git" in item for item in gitops.worktree_hazards(wt, self.base)))
        with self.assertRaises(gitops.WorktreeTampered):
            gitops.changed_paths(wt, self.base)
        with self.assertRaises(gitops.WorktreeTampered):
            gitops._git(wt, ["rev-parse", "HEAD"])
        self.assertEqual(self.planted(), [])

    def test_deleted_or_replaced_dotgit_is_detected(self):
        wt = self.worktree()
        (wt / ".git").unlink()
        self.assertTrue(any("missing" in item for item in gitops.worktree_integrity_problems(wt)))
        with self.assertRaises(gitops.WorktreeTampered):
            gitops.snapshot_tree(wt)

    def test_commands_run_against_the_pinned_dir_even_when_dotgit_is_untouched_by_us(self):
        wt = self.worktree()
        calls = self.spy_on_git()
        gitops._git(wt, ["rev-parse", "HEAD"])
        argv = [a for a, _ in calls if "rev-parse" in a][0]
        self.assertIn(f"--work-tree={os.path.abspath(wt)}", argv)

    def test_failed_creation_leaves_no_worktree_or_branch(self):
        real = gitops._git_retry
        path = self.tmp / "wts" / "partial"

        def add_then_time_out(cwd, args, **kwargs):
            result = real(cwd, args, **kwargs)
            if args[:2] == ["worktree", "add"]:
                raise gitops.BridgeError("Git command timed out after 900 seconds: worktree add")
            return result

        with mock.patch.object(gitops, "_git_retry", side_effect=add_then_time_out):
            with self.assertRaises(gitops.BridgeError):
                gitops.create_worktree(self.repo, path, "delegate/codex-t-partial", self.base)
        self.assertFalse(path.exists())
        self.assertNotIn("delegate/codex-t-partial", git(self.repo, "branch", "--list"))
        self.assertFalse(gitops.pin_path(path).exists())
        self.assertIsNone(gitops.load_pin(path))


class LinksAndNestedRepos(RepoCase):
    def base_files(self):
        return {**super().base_files(), ".gitignore": b"ignored/\n"}

    @unittest.skipUnless(NT, "junctions are Windows-only")
    def test_links_and_nested_repositories_inside_git_ignored_paths_are_tolerated(self):
        victim = self.make_victim()
        wt = self.worktree()
        (wt / "ignored").mkdir()
        if not make_junction(wt / "ignored" / "link", victim):
            self.skipTest("cannot create a junction here")
        nested = wt / "ignored" / "vendor"
        nested.mkdir()
        git(nested, "init", "-q")
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        self.assertEqual(gitops.scan_worktree(wt, self.base), [])
        self.assertEqual(gitops.worktree_hazards(wt, self.base), [])
        self.assertEqual(gitops.changed_paths(wt, self.base), ["src/keep.py"])
        self.assertNotIn("secret.txt", git(self.repo, "ls-tree", "-r", "--name-only", gitops.snapshot_tree(wt)))
        gitops.remove_worktree(self.repo, wt)  # links are still unlinked, never followed, on removal
        self.assertEqual((victim / "secret.txt").read_text(encoding="utf-8"), "secret\n")

    def make_victim(self) -> Path:
        victim = self.tmp / "victim"
        (victim / "sub").mkdir(parents=True)
        (victim / "secret.txt").write_text("secret\n", encoding="utf-8")
        (victim / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")
        return victim

    @unittest.skipUnless(NT, "junctions are Windows-only")
    def test_junction_is_reported_and_never_followed_by_git(self):
        victim = self.make_victim()
        wt = self.worktree()
        if not make_junction(wt / "src" / "link", victim):
            self.skipTest("cannot create a junction here")
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        self.assertEqual(gitops.scan_worktree(wt, self.base), [{"kind": "junction", "path": "src/link"}])
        self.assertTrue(any("junction: src/link" in item for item in gitops.worktree_hazards(wt, self.base)))
        self.assertEqual(gitops.changed_paths(wt, self.base), ["src/keep.py"])
        tree = gitops.snapshot_tree(wt)
        listed = git(self.repo, "ls-tree", "-r", "--name-only", tree)
        self.assertNotIn("secret.txt", listed)
        self.assertNotIn("link", listed)
        self.assertNotIn(b"secret", gitops.tree_diff(wt, self.base, tree))

    @unittest.skipUnless(NT, "junctions are Windows-only")
    def test_removing_a_worktree_never_deletes_what_a_junction_points_at(self):
        victim = self.make_victim()
        wt = self.worktree()
        if not make_junction(wt / "src" / "link", victim):
            self.skipTest("cannot create a junction here")
        removed = gitops.remove_worktree(self.repo, wt)
        self.assertEqual(removed, ["src/link"])
        self.assertFalse(wt.exists())
        self.assertEqual((victim / "secret.txt").read_text(encoding="utf-8"), "secret\n")
        self.assertEqual((victim / "sub" / "deep.txt").read_text(encoding="utf-8"), "deep\n")
        self.assertFalse(gitops.pin_path(wt).exists())

    @unittest.skipUnless(NT, "extended-length paths are Windows-only")
    def test_a_junction_below_max_path_is_still_found(self):
        import _winapi
        victim = self.make_victim()
        wt = self.worktree()
        current = "\\\\?\\" + str(wt / "src")
        for _ in range(6):  # well past the 260 character limit
            current += "\\" + "a" * 60
            os.mkdir(current)
        try:
            _winapi.CreateJunction(str(victim), current + "\\link")
        except OSError:
            self.skipTest("cannot create a junction here")
        try:
            found = gitops.scan_worktree(wt, self.base)
            self.assertEqual([item["kind"] for item in found], ["junction"])
            self.assertTrue(found[0]["path"].endswith("/link") and len(found[0]["path"]) > 260, found)
        finally:
            os.rmdir(current + "\\link")
            shutil.rmtree("\\\\?\\" + str(wt / "src" / ("a" * 60)), ignore_errors=True)

    def test_a_directory_that_cannot_be_listed_is_a_finding_and_kept_out_of_git_walks(self):
        wt = self.worktree()
        (wt / "locked").mkdir()
        (wt / "locked" / "inside.txt").write_text("x", encoding="utf-8")
        real = os.scandir

        def scandir(path="."):
            if str(path).replace("\\", "/").endswith("/locked"):
                raise PermissionError(13, "denied")
            return real(path)

        with mock.patch.object(gitops.os, "scandir", side_effect=scandir):
            self.assertEqual(gitops.scan_worktree(wt, self.base), [{"kind": "unreadable", "path": "locked"}])
            self.assertTrue(any("unreadable: locked" in item for item in gitops.worktree_hazards(wt, self.base)))
            self.assertEqual(gitops.changed_paths(wt, self.base), [])

    def test_symlink_is_reported_unless_the_base_commit_tracks_it(self):
        wt = self.worktree()
        try:
            os.symlink(str(self.tmp / "elsewhere"), str(wt / "src" / "ln"))
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symlink here")
        self.assertEqual(gitops.scan_worktree(wt, self.base), [{"kind": "symlink", "path": "src/ln"}])
        self.assertEqual(gitops.changed_paths(wt, self.base), [])  # excluded, not followed or snapshotted
        self.assertNotIn("ln", git(self.repo, "ls-tree", "-r", "--name-only", gitops.snapshot_tree(wt)))

    def test_nested_repository_is_reported_and_kept_out_of_the_snapshot(self):
        wt = self.worktree()
        nested = wt / "src" / "vendor"
        nested.mkdir()
        git(nested, "init", "-q")
        (nested / "lib.py").write_text("lib\n", encoding="utf-8")
        git(nested, "add", ".")
        git(nested, "commit", "-qm", "nested")
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        self.assertEqual(gitops.scan_worktree(wt, self.base), [{"kind": "nested_git", "path": "src/vendor"}])
        self.assertEqual(gitops.changed_paths(wt, self.base), ["src/keep.py"])
        tree = gitops.snapshot_tree(wt)
        self.assertNotIn("160000", git(self.repo, "ls-tree", "-r", tree))
        self.assertNotIn(b"Subproject commit", gitops.tree_diff(wt, self.base, tree))

    def test_special_modes_in_a_tree_are_found_and_refused(self):
        blob = git(self.repo, "hash-object", "-w", "--stdin", input=b"target\n").strip()
        index = str(self.tmp / "special.index")
        env = {"GIT_INDEX_FILE": index}
        git(self.repo, "read-tree", self.base, env=env)
        git(self.repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},src/link", env=env)
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.base},src/sub", env=env)
        tree = git(self.repo, "write-tree", env=env).strip()
        self.assertEqual(gitops.special_entries(self.repo, self.base, tree),
                         [{"kind": "symlink", "path": "src/link"}, {"kind": "gitlink", "path": "src/sub"}])
        self.assertEqual(gitops.special_entries(self.repo, self.base, self.base), [])
        wt = self.worktree()
        with mock.patch.object(gitops, "special_entries",
                               return_value=[{"kind": "symlink", "path": "src/link"}]):
            with self.assertRaises(gitops.UnsafeWorktree) as caught:
                gitops.snapshot_tree(wt)
            self.assertEqual(caught.exception.findings, [{"kind": "symlink", "path": "src/link"}])
            self.assertTrue(gitops.snapshot_tree(wt, reject_special=False))

    def test_remove_worktree_is_idempotent_for_a_directory_that_is_already_gone(self):
        wt = self.worktree()
        remove_tree(wt)
        self.assertEqual(gitops.remove_worktree(self.repo, wt), [])
        self.assertNotIn(str(wt.name), git(self.repo, "worktree", "list"))
        self.assertEqual(gitops.remove_worktree(self.repo, wt), [])


class JunctionDetection(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_junction_is_a_link_without_os_path_isjunction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target").mkdir()
            junction = root / "link"
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(root / "target")],
                                  capture_output=True, encoding="utf-8", errors="replace")
            if made.returncode != 0:
                self.skipTest("cannot create a junction here")
            (root / "plain").mkdir()
            with mock.patch.object(os.path, "isjunction", None, create=True):  # simulate Python 3.11
                self.assertTrue(revision._is_link(junction))
                self.assertFalse(revision._is_link(root / "plain"))
                with self.assertRaises(revision.RevisionError):
                    revision.ensure_no_links(root, "link/file.txt")
            # Remove the junction itself, never its target's contents.
            os.rmdir(junction)


class ReparsePointDetection(unittest.TestCase):
    def test_any_reparse_point_is_a_link(self):
        info = mock.Mock(st_file_attributes=0x400, st_reparse_tag=0x80000018)  # a cloud placeholder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dir").mkdir()
            with mock.patch.object(os, "lstat", return_value=info):
                self.assertTrue(revision._is_link(root / "dir"))
                with self.assertRaises(revision.RevisionError):
                    revision.ensure_no_links(root, "dir/file.txt")
            self.assertFalse(revision._is_link(root / "dir"))


class FeedbackFile(unittest.TestCase):
    def load(self, feedback, scope=("src/**", "SRC/**")):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            return revision.load_feedback(feedback, allowed_changed_paths=list(scope), worktree=root)

    def test_a_missing_or_unreadable_feedback_file_is_a_revision_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            for target in (Path(tmp) / "absent.json", Path(tmp)):  # missing, and a directory
                with self.subTest(target=target.name):
                    with self.assertRaisesRegex(revision.RevisionError, "feedback file"):
                        revision.load_feedback(target, allowed_changed_paths=["src/**"], worktree=Path(tmp))

    def test_a_missing_worktree_is_a_revision_error(self):
        finding = {"path": "src/a.py", "issue": "the loop never ends", "expected_behavior": "the loop must end"}
        with self.assertRaises(revision.RevisionError):
            revision.load_feedback({"findings": [finding]}, allowed_changed_paths=["src/**"],
                                   worktree=Path(tempfile.gettempdir()) / "dtc-no-such-worktree")

    @unittest.skipUnless(NT, "path case is only insignificant on Windows")
    def test_path_case_does_not_make_a_finding_new_on_windows(self):
        base = {"issue": "the loop never ends", "expected_behavior": "the loop must end"}
        with self.assertRaisesRegex(revision.RevisionError, "duplicates"):
            self.load({"findings": [{"path": "src/App.py", **base}, {"path": "SRC/app.py", **base}]})
        upper = self.load({"findings": [{"path": "SRC/APP.py", **base}]})
        lower = self.load({"findings": [{"path": "src/app.py", **base}]})
        self.assertEqual(upper["sha256"], lower["sha256"])
        self.assertEqual(upper["findings"][0]["path"], "SRC/APP.py")  # the original spelling is kept


class IgnoredFiles(BridgeCase):
    def setUp(self):
        super().setUp()
        (self.repo / ".gitignore").write_text("*.cfg\n__pycache__/\nbuild/\n", encoding="utf-8")
        self.commit_all("ignore rules")

    def test_an_ignored_file_created_by_the_worker_fails_the_result(self):
        def edit(worktree):
            (worktree / "helper.cfg").write_text("hidden from the patch\n", encoding="utf-8")

        with self.after_segment(edit):
            result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertEqual(result["ignored_files_created"], ["helper.cfg"])
        self.assertTrue(any("git-ignored files" in f and "helper.cfg" in f for f in result["failures"]), result["failures"])
        self.assertEqual(result["validation"]["status"], "not_run")  # never validated against files the patch lacks
        self.assertEqual(result["changed_paths"], ["hello.py"])

    def test_a_worker_written_gitignore_cannot_hide_a_file_from_the_patch(self):
        def edit(worktree):
            (worktree / ".gitignore").write_text("*.cfg\n__pycache__/\nbuild/\npayload.py\n", encoding="utf-8")
            (worktree / "payload.py").write_text("print('only validation sees me')\n", encoding="utf-8")

        with self.after_segment(edit):
            result = bridge.run(self.task(allowed_changed_paths=["hello.py", ".gitignore"]))
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertEqual(result["ignored_files_created"], ["payload.py"])
        self.assertNotIn("diff --git a/payload.py", Path(result["diff_path"]).read_text(encoding="utf-8"))

    def test_build_output_is_not_a_failure(self):
        def edit(worktree):
            (worktree / "__pycache__").mkdir()
            (worktree / "__pycache__" / "hello.cpython-312.pyc").write_bytes(b"\0")
            (worktree / "build").mkdir()
            (worktree / "build" / "out.txt").write_text("x", encoding="utf-8")

        with self.after_segment(edit):
            result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["ignored_build_output_files"], ["build/out.txt"])
        self.assertFalse(any("git-ignored" in w for w in result["warnings"]), result["warnings"])  # expected output
        self.assertEqual(result["ignored_files_created"], [])
        self.assertFalse((Path(result["worktree"]) / "__pycache__" / "hello.cpython-312.pyc").exists())

    def test_copy_ignored_files_are_not_new_but_are_reported_as_exposed(self):
        (self.repo / "settings.cfg").write_text("sdk=local\n", encoding="utf-8")
        result = bridge.run(self.task(copy_ignored=["settings.cfg"]))
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertTrue(any("copy_ignored exposed 1" in w and "settings.cfg" in w for w in result["warnings"]),
                        result["warnings"])
        self.assertTrue(any("exposed" in w for w in bridge.brief(result)["warnings"]))


class PrimaryCheckout(BridgeCase):
    def review_task(self):
        return self.task(mode="review", allowed_changed_paths=[], validation_command=None)

    def test_a_read_only_task_that_changes_the_checkout_fails(self):
        def edit(worktree):
            (worktree / "stray.txt").write_text("a read-only worker wrote this\n", encoding="utf-8")

        with self.after_segment(edit):
            result = bridge.run(self.review_task())
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertTrue(any("read-only task changed the primary checkout" in f and "anyone else" in f and "stray.txt" in f
                            for f in result["failures"]), result["failures"])

    def test_a_hook_installed_in_the_primary_repository_fails_every_mode(self):
        def edit(worktree):
            (self.repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\necho owned\n", encoding="utf-8")

        for task in (self.task(), self.review_task()):
            with self.subTest(mode=load_task(task).mode), self.after_segment(edit):
                result = bridge.run(task)
                self.assertEqual(result["lifecycle_status"], "BLOCKED")
                self.assertTrue(any(bridge.GUARD_FAILURE in f and "hooks/pre-commit" in f
                                    for f in result["failures"]), result["failures"])
            (self.repo / ".git" / "hooks" / "pre-commit").unlink()

    def test_a_changed_git_config_fails_the_result(self):
        def edit(worktree):
            path = self.repo / ".git" / "config"
            path.write_text(path.read_text(encoding="utf-8") + "\n[core]\n\tfsmonitor = echo owned\n", encoding="utf-8")

        with self.after_segment(edit):
            result = bridge.run(self.task())
        self.assertTrue(any(bridge.GUARD_FAILURE in f and "config:core.fsmonitor" in f for f in result["failures"]),
                        result["failures"])

    def test_unchanged_configuration_is_not_reported(self):
        result = bridge.run(self.task())
        self.assertFalse(any(bridge.GUARD_FAILURE in f for f in result["failures"]))


class PrimaryRepositorySettings(BridgeCase):
    """The primary repository's hooks and code-running settings: ordinary edits pass, the rest fails by name."""

    def run_with(self, edit, **overrides):
        with self.after_segment(edit):
            return bridge.run(self.task(**overrides))

    def test_everyday_edits_during_a_run_do_not_fail_it(self):
        def edit(worktree):
            git(self.repo, "config", "branch.feature.remote", "origin")
            git(self.repo, "remote", "add", "origin", "https://example.invalid/r.git")
            git(self.repo, "config", "user.name", "Someone Else")
            (self.repo / ".git" / "info" / "exclude").write_text("*.tmp\n", encoding="utf-8")
            (self.repo / ".git" / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n", encoding="utf-8")

        result = self.run_with(edit)
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertFalse(any(bridge.GUARD_FAILURE in f for f in result["failures"]))

    def test_each_setting_that_runs_code_is_named_in_the_failure(self):
        cases = [(("core.hooksPath", "elsewhere"), "config:core.hookspath"),
                 (("alias.st", "!echo owned"), "config:alias.st"),
                 (("filter.f.clean", "cat"), "config:filter.f.clean"),
                 (("credential.helper", "store"), "config:credential.helper"),
                 (("core.pager", "less"), "config:core.pager"),
                 (("url.https://a.invalid/.insteadOf", "https://b.invalid/"), "config:url.https://a.invalid/.insteadof"),
                 (("diff.x.textconv", "cat"), "config:diff.x.textconv"),
                 (("gpg.program", "tool"), "config:gpg.program")]
        for (key, value), name in cases:
            with self.subTest(key=key):
                result = self.run_with(lambda worktree: git(self.repo, "config", key, value))
                self.assertEqual(result["lifecycle_status"], "BLOCKED")
                self.assertTrue(any(bridge.GUARD_FAILURE in f and name in f and "absent ->" in f
                                    for f in result["failures"]), result["failures"])
                self.assertEqual(result["validation"]["status"], "not_run")
                git(self.repo, "config", "--unset-all", key)
                bridge.cleanup(self.task(), Path(result["artifact_directory"]))

    def test_a_setting_value_is_never_written_to_the_record(self):
        secret = "ghp_notARealTokenJustAMarker"
        git(self.repo, "config", "credential.helper", f"!echo {secret}")
        result = bridge.run(self.task())
        artifact = Path(result["artifact_directory"])
        for path in (artifact / "primary-guard.before.json", artifact / "result.json"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"), path.name)
        self.assertIn("config:credential.helper", json.loads((artifact / "primary-guard.before.json")
                                                             .read_text(encoding="utf-8")))

    def test_a_setting_changed_in_an_included_file_is_seen_but_an_everyday_one_is_not(self):
        included = self.tmp / "extra.cfg"
        included.write_text("[user]\n\tname = First\n", encoding="utf-8")
        git(self.repo, "config", "include.path", included.as_posix())

        def everyday(worktree):
            included.write_text("[user]\n\tname = Second\n[branch \"x\"]\n\tremote = origin\n", encoding="utf-8")

        def code(worktree):
            included.write_text("[user]\n\tname = Second\n[alias]\n\tx = !echo owned\n", encoding="utf-8")

        self.assertEqual(self.run_with(everyday)["lifecycle_status"], "REVIEW_PENDING")
        result = self.run_with(code)
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertTrue(any("config:alias.x" in f for f in result["failures"]), result["failures"])

    def test_a_hook_is_named_by_file(self):
        def edit(worktree):
            (self.repo / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\necho owned\n", encoding="utf-8")

        result = self.run_with(edit)
        self.assertTrue(any(bridge.GUARD_FAILURE in f and "hooks/post-commit (absent ->" in f
                            for f in result["failures"]), result["failures"])

    def blocked_by_a_lead_edit(self):
        task = self.task()
        with self.after_segment(lambda worktree: git(self.repo, "config", "core.pager", "less")):
            result = bridge.run(task)
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        return task, Path(result["artifact_directory"])

    def test_revalidate_rebaselines_only_when_asked_and_records_old_and_new_hashes(self):
        task, artifact = self.blocked_by_a_lead_edit()
        refused = bridge.revalidate(task, artifact)
        self.assertEqual((refused["status"], refused["lifecycle_status"]), ("failed", "BLOCKED"))
        self.assertTrue(any("config:core.pager" in f and "--accept-repo-config-change" in f
                            for f in refused["failures"]), refused["failures"])
        accepted = bridge.revalidate(task, artifact, accept_repo_config_change=True)
        self.assertEqual((accepted["status"], accepted["lifecycle_status"]), ("passed", "REVIEW_PENDING"))
        record = self.record(artifact)
        change = record["repo_config_changes_accepted"][0]["changes"][0]
        self.assertEqual((change["name"], change["before"]), ("config:core.pager", None))
        self.assertRegex(change["after"], r"^[0-9a-f]{64}$")
        self.assertTrue(any("accepted changes to primary repository settings" in w for w in record["warnings"]))
        self.assertIn("config:core.pager", json.loads((artifact / "primary-guard.before.json")
                                                      .read_text(encoding="utf-8")))
        self.assertEqual(bridge.accept(task, artifact)["status"], "accepted")

    def test_accept_refuses_settings_changed_since_the_run_started(self):
        task = self.task()
        result = bridge.run(task)
        artifact = Path(result["artifact_directory"])
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
        git(self.repo, "config", "alias.st", "!echo owned")
        with self.assertRaisesRegex(bridge.BridgeError, "config:alias.st.*--accept-repo-config-change"):
            bridge.accept(task, artifact)
        self.assertFalse((self.repo / "hello.py").exists())
        bridge.revalidate(task, artifact, accept_repo_config_change=True)
        self.assertEqual(bridge.accept(task, artifact)["status"], "accepted")

    def test_validation_that_edits_the_primary_repository_is_caught(self):
        hook = self.repo / ".git" / "hooks" / "post-commit"
        script = (f"from pathlib import Path\nPath({str(hook)!r}).write_text('#!/bin/sh\\necho owned\\n')\n"
                  f"Path({str(self.repo / 'README.md')!r}).write_text('changed by validation')\n")
        result = bridge.run(self.task(validation_command=[sys.executable, "-c", script]))
        self.assertEqual(result["validation"]["status"], "passed")  # the program itself succeeded
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertTrue(any("hooks/post-commit" in f for f in result["failures"]), result["failures"])
        self.assertIs(result["primary_checkout_unchanged"], False)
        self.assertTrue(any("README.md" in w for w in result["warnings"]), result["warnings"])

    def test_validation_that_edits_an_allowed_primary_path_fails_the_result(self):
        script = f"from pathlib import Path\nPath({str(self.repo / 'hello.py')!r}).write_text('print(1)\\n')\n"
        result = bridge.run(self.task(validation_command=[sys.executable, "-c", script]))
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertTrue(any("primary checkout changed inside the task's allowed paths" in f and "hello.py" in f
                            for f in result["failures"]), result["failures"])

    def test_revalidate_catches_a_validation_that_edits_the_primary_repository(self):
        hook = self.repo / ".git" / "hooks" / "pre-push"
        armed = self.tmp / "armed.flag"
        script = ("import os\n"
                  f"if os.path.exists({str(armed)!r}):\n"
                  f"    with open({str(hook)!r}, 'a') as handle:\n"
                  "        handle.write('echo owned' + chr(10))\n")
        task = self.task(validation_command=[sys.executable, "-c", script])
        artifact = Path(bridge.run(task)["artifact_directory"])
        armed.write_text("go", encoding="utf-8")
        outcome = bridge.revalidate(task, artifact)
        self.assertEqual((outcome["status"], outcome["lifecycle_status"]), ("failed", "BLOCKED"))
        self.assertTrue(any("while validation ran" in f and "hooks/pre-push" in f for f in outcome["failures"]),
                        outcome["failures"])
        again = bridge.revalidate(task, artifact, accept_repo_config_change=True)  # accepts what is there now ...
        self.assertEqual(again["lifecycle_status"], "BLOCKED")  # ... but not what validation changes while it runs
        self.assertTrue(any("while validation ran" in f for f in again["failures"]), again["failures"])


class HiddenBytecode(BridgeCase):
    extra_files = {".gitignore": b"__pycache__/\n*.pyc\nbuild/\n"}

    def compiled(self, destination: Path, source: str = "pass\n") -> None:
        """A bytecode file that never checks its source and does what ``source`` says."""
        scratch = self.tmp / "benign.py"
        scratch.write_text(source, encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(scratch), cfile=str(destination),
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)

    def import_command(self, module):
        return [sys.executable, "-B", "-c", f"import {module}"]

    def test_a_planted_cache_file_cannot_make_validation_pass(self):
        def edit(worktree):
            (worktree / "hello.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
            self.compiled(Path(importlib.util.cache_from_source(str(worktree / "hello.py"))))

        with self.after_segment(edit):
            result = bridge.run(self.task(validation_command=self.import_command("hello")))
        self.assertEqual(result["validation"]["status"], "failed")
        self.assertEqual(result["validation"]["removed_bytecode_files"], ["__pycache__/hello.cpython-%d%d.pyc"
                                                                           % sys.version_info[:2]])
        self.assertEqual(result["validation"]["removed_bytecode_without_source"], [])
        self.assertFalse((Path(result["worktree"]) / "__pycache__").exists())

    def test_a_planted_sourceless_module_is_removed_and_named(self):
        def edit(worktree):
            self.compiled(worktree / "ghost.pyc")

        with self.after_segment(edit):
            result = bridge.run(self.task(validation_command=self.import_command("ghost")))
        self.assertEqual(result["validation"]["status"], "failed")  # nothing left to import
        self.assertEqual(result["validation"]["removed_bytecode_without_source"], ["ghost.pyc"])
        self.assertTrue(any("no source file" in w and "ghost.pyc" in w for w in result["warnings"]), result["warnings"])

    def test_bytecode_supplied_with_copy_ignored_stays(self):
        self.compiled(self.repo / "supplied.pyc")
        result = bridge.run(self.task(copy_ignored=["supplied.pyc"], validation_command=self.import_command("supplied")))
        self.assertEqual(result["validation"]["status"], "passed", result["failures"])
        self.assertEqual(result["validation"]["removed_bytecode_files"], [])

    def test_python_validation_reads_bytecode_from_a_fresh_empty_folder_that_is_deleted_afterwards(self):
        report = self.tmp / "prefix.txt"
        script = ("import os, sys\nfrom pathlib import Path\n"
                  f"Path({str(report)!r}).write_text(sys.pycache_prefix + '|' + repr(os.listdir(sys.pycache_prefix)),\n"
                  "                                    encoding='utf-8')\n")
        result = bridge.run(self.task(validation_command=[sys.executable, "-c", script]))
        self.assertEqual(result["validation"]["status"], "passed", result["failures"])
        prefix, listing = report.read_text(encoding="utf-8").split("|")
        self.assertEqual(listing, "[]")
        self.assertFalse(Path(prefix).exists(), "the cache folder must not outlive the validation")
        self.assertNotIn(str(Path(result["worktree"])), prefix)

    def test_other_build_output_is_kept_but_named_in_the_record_and_a_warning(self):
        def edit(worktree):
            (worktree / "build" / "deep").mkdir(parents=True)
            (worktree / "build" / "out.bin").write_bytes(b"\0")
            (worktree / "build" / "deep" / "x.o").write_bytes(b"\0")

        with self.after_segment(edit):
            result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(result["ignored_build_output_files"], ["build/deep/x.o", "build/out.bin"])
        self.assertFalse(any("git-ignored" in w for w in result["warnings"]), result["warnings"])
        self.assertTrue((Path(result["worktree"]) / "build" / "out.bin").exists())  # not deleted: only named

    def test_build_output_outside_the_standard_folders_gets_one_short_warning_with_a_count(self):
        (self.repo / ".gitignore").write_text("*.cfg\n__pycache__/\nbuild/\n*.egg-info/\n", encoding="utf-8")
        self.commit_all("ignore egg-info")

        def edit(worktree):
            for name in ("one", "two", "three"):
                (worktree / "pkg.egg-info").mkdir(exist_ok=True)
                (worktree / "pkg.egg-info" / name).write_text("x", encoding="utf-8")
            (worktree / "build").mkdir()
            (worktree / "build" / "out.bin").write_bytes(b"\0")

        with self.after_segment(edit):
            result = bridge.run(self.task())
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING", result["failures"])
        self.assertEqual(len(result["ignored_build_output_files"]), 4)  # the record keeps everything
        lines = [w for w in result["warnings"] if "git-ignored" in w]
        self.assertEqual(len(lines), 1, result["warnings"])
        self.assertIn("3 file(s)", lines[0])
        self.assertNotIn("build/out.bin", lines[0])


if __name__ == "__main__":
    unittest.main()
