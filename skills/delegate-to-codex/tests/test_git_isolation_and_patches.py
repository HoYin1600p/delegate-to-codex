"""Byte-exact patches, Git isolation from user and worker configuration, identifiers, and the Git helpers.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import NT, PYTHON, RepoCase, git, init_repo, working_directory
from claude_codex_bridge import gitops, process, revision


class BytePatches(RepoCase):
    def base_files(self):
        return {"crlf.txt": b"one\r\ntwo\r\nthree\r\n", "cr.txt": b"a\rb\r\nc\r\n", "latin.py": b"# caf\xe9\nx = 1\n"}

    def test_crlf_lone_cr_and_non_utf8_survive_snapshot_diff_and_apply(self):
        wt = self.worktree()
        edits = {"crlf.txt": b"one\r\nTWO\r\nthree\r\n", "cr.txt": b"a\rB\r\nc\r\n", "latin.py": b"# caf\xe9\nx = 2\n",
                 "new.txt": b"na\xefve\r\nline\r\n"}
        for name, data in edits.items():
            (wt / name).write_bytes(data)
        patch = gitops.tree_diff(wt, self.base, gitops.snapshot_tree(wt))
        self.assertIsInstance(patch, bytes)
        for needle in (b"-two\r\n", b"+TWO\r\n", b"-a\rb\r\n", b"+a\rB\r\n", b"caf\xe9", b"+na\xefve\r\n"):
            self.assertIn(needle, patch)
        self.assertNotIn(b"\xef\xbf\xbd", patch)
        clone = self.tmp / "clone"
        git(self.tmp, "clone", "-q", "--config", "core.autocrlf=false", str(self.repo), str(clone))
        gitops.apply_patch(clone, patch)
        for name, data in edits.items():
            self.assertEqual((clone / name).read_bytes(), data, name)

    def test_apply_is_independent_of_apply_whitespace_configuration(self):
        wt = self.worktree()
        (wt / "new.txt").write_bytes(b"trailing   \nspace\t\n")
        patch = gitops.tree_diff(wt, self.base, gitops.snapshot_tree(wt))
        for setting in ("fix", "error", "strip"):
            with self.subTest(setting=setting):
                self.set_config(f"[apply]\n\twhitespace = {setting}\n\tignoreWhitespace = change\n")
                clone = self.tmp / f"clone-{setting}"
                git(self.tmp, "clone", "-q", "--config", "core.autocrlf=false", str(self.repo), str(clone))
                gitops.apply_patch(clone, patch)
                self.assertEqual((clone / "new.txt").read_bytes(), b"trailing   \nspace\t\n")

    def test_check_only_does_not_touch_the_tree_and_reports_conflicts(self):
        wt = self.worktree()
        (wt / "crlf.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
        patch = gitops.tree_diff(wt, self.base, gitops.snapshot_tree(wt))
        (self.repo / "crlf.txt").write_bytes(b"completely different\r\n")
        result = gitops.apply_patch(self.repo, patch, check_only=True, raise_on_error=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.repo / "crlf.txt").read_bytes(), b"completely different\r\n")
        with self.assertRaises(gitops.BridgeError):
            gitops.apply_patch(self.repo, patch)


class Isolation(RepoCase):
    def test_diff_format_ignores_user_prefix_colour_and_external_diff_settings(self):
        self.set_config("[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n\texternal = exit 1\n\tcontext = 9\n"
                        "\tsuppressBlankEmpty = true\n[color]\n\tui = always\n[core]\n\tabbrev = 12\n")
        wt = self.worktree()
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n\nmore = 3\n")
        patch = gitops.tree_diff(wt, self.base, gitops.snapshot_tree(wt))
        self.assertTrue(patch.startswith(b"diff --git a/src/keep.py b/src/keep.py\n"), patch[:80])
        self.assertNotIn(b"\x1b[", patch)
        stat = gitops.tree_diffstat(wt, self.base, gitops.snapshot_tree(wt))
        self.assertEqual([item["path"] for item in stat["files"]], ["src/keep.py"])
        self.assertNotIn("\x1b[", stat["stat"])

    def test_hooks_and_fsmonitor_from_user_config_never_run(self):
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        (hooks / "post-checkout").write_text(f"#!/bin/sh\necho ran > '{(self.flags / 'hook').as_posix()}'\n",
                                             encoding="utf-8", newline="\n")
        self.set_config(f"[core]\n\tfsmonitor = echo ran > '{(self.flags / 'fsmonitor').as_posix()}'\n"
                        f"\thooksPath = {hooks.as_posix()}\n")
        wt = self.worktree()  # `git worktree add` would run post-checkout
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        gitops.changed_paths(wt, self.base)
        gitops.snapshot_tree(wt)
        gitops._worktree_fingerprint(wt, self.base)
        self.assertEqual(self.planted(), [])

    def test_worker_gitattributes_cannot_select_a_filter_driver(self):
        script = self.tmp / "filter.py"
        script.write_text("import sys\nopen(sys.argv[1], 'w').write('ran')\n"
                          "sys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
        self.set_config(f"[filter \"evil\"]\n\tclean = \"{PYTHON.as_posix()}\" \"{script.as_posix()}\" "
                        f"\"{(self.flags / 'filter').as_posix()}\"\n")
        wt = self.worktree()
        (wt / "src" / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        (wt / "src" / "extra.py").write_bytes(b"extra\n")
        self.assertIn("src/extra.py", gitops.changed_paths(wt, self.base))
        gitops.snapshot_tree(wt)
        gitops._worktree_fingerprint(wt, self.base)
        self.assertEqual(self.planted(), [])

    def test_worker_gitattributes_is_refused_when_git_cannot_isolate_attributes(self):
        wt = self.worktree()
        with mock.patch.object(gitops, "attr_source_supported", return_value=False):
            gitops.changed_paths(wt, self.base)  # nothing changed: fine
            (wt / "src" / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
            with self.assertRaises(gitops.UnsafeWorktree):
                gitops.changed_paths(wt, self.base)
            with self.assertRaises(gitops.UnsafeWorktree):
                gitops.snapshot_tree(wt)

    def test_attr_source_is_passed_for_a_pinned_worktree(self):
        if not gitops.attr_source_supported():
            self.skipTest("this Git has no --attr-source")
        wt = self.worktree()
        calls = self.spy_on_git()
        gitops.changed_paths(wt, self.base)
        diff = [argv for argv, _ in calls if "diff" in argv]
        self.assertTrue(diff)
        self.assertIn(f"--attr-source={self.base}", diff[0])
        self.assertIn(f"--git-dir={gitops.load_pin(wt).git_dir}", diff[0])

    def test_inherited_git_environment_is_scrubbed(self):
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q")
        (other / "o.txt").write_text("o\n", encoding="utf-8")
        git(other, "add", ".")
        git(other, "commit", "-qm", "o")
        wt = self.worktree()
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        hostile = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                   "GIT_INDEX_FILE": str(other / ".git" / "index"), "GIT_CONFIG_COUNT": "1",
                   "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "echo x"}
        with mock.patch.dict(os.environ, hostile):
            top = gitops._git(self.repo, ["rev-parse", "--show-toplevel"]).stdout.strip()
            self.assertEqual(Path(top).resolve(), self.repo.resolve())
            self.assertEqual(gitops.changed_paths(wt, self.base), ["src/keep.py"])
            self.assertTrue(gitops.snapshot_tree(wt))

    def test_status_uses_no_optional_locks_and_batches_hashing(self):
        wt = self.worktree()
        for number in range(12):
            (wt / f"f{number}.txt").write_text(f"{number}\n", encoding="utf-8")
        calls = self.spy_on_git()
        gitops._worktree_fingerprint(wt, self.base)
        self.assertEqual(len([argv for argv, _ in calls if "hash-object" in argv]), 1)
        gitops.primary_status(mock.Mock(repo_root=self.repo))
        status = [argv for argv, _ in calls if "status" in argv]
        self.assertTrue(status and all("--no-optional-locks" in argv for argv in status))

    def test_fingerprint_falls_back_to_single_hashes_when_the_batch_fails(self):
        wt = self.worktree()
        for number in range(4):
            (wt / f"f{number}.txt").write_text(f"{number}\n", encoding="utf-8")
        expected = gitops._worktree_fingerprint(wt, self.base)
        real = gitops._git_bytes

        def failing_batch(cwd, args, **kwargs):
            if "--stdin-paths" in args:
                return subprocess.CompletedProcess(args, 1, b"", b"fatal: unreadable")
            return real(cwd, args, **kwargs)

        with mock.patch.object(gitops, "_git_bytes", side_effect=failing_batch):
            self.assertEqual(gitops._worktree_fingerprint(wt, self.base), expected)

    def test_fingerprint_matches_the_per_file_hash_it_replaced(self):
        wt = self.worktree()
        (wt / "src" / "keep.py").write_bytes(b"keep = 2\n")
        (wt / "new file.txt").write_bytes(b"spaces\n")
        (wt / "gone.txt").write_bytes(b"x\n")
        (wt / "gone.txt").unlink()
        (wt / "README.md").unlink()
        digest = hashlib.sha256()
        for path in gitops.changed_paths(wt, self.base):
            digest.update(path.encode("utf-8") + b"\0")
            target = wt / path
            digest.update((git(wt, "hash-object", "--", path).strip() if target.is_file() else "<deleted>").encode())
        self.assertEqual(gitops._worktree_fingerprint(wt, self.base), digest.hexdigest())

    def test_default_timeouts_scale_with_the_command_and_can_be_overridden(self):
        calls = self.spy_on_git()
        gitops._git(self.repo, ["rev-parse", "HEAD"])
        gitops._git(self.repo, ["status", "--porcelain"])
        gitops._git(self.repo, ["rev-parse", "HEAD"], timeout=7)
        by_command = [(("status" if "status" in argv else "rev-parse"), timeout)
                      for argv, timeout in calls if "rev-parse" in argv or "status" in argv]
        self.assertEqual(by_command, [("rev-parse", gitops.GIT_TIMEOUT_SECONDS),
                                      ("status", gitops.GIT_HEAVY_TIMEOUT_SECONDS), ("rev-parse", 7)])
        self.assertGreater(gitops.GIT_HEAVY_TIMEOUT_SECONDS, gitops.GIT_TIMEOUT_SECONDS)

    def test_git_is_resolved_without_the_current_directory(self):
        decoy = self.tmp / "decoy"
        decoy.mkdir()
        name = "git.exe" if NT else "git"
        (decoy / name).write_bytes(b"not git")
        os.chmod(decoy / name, 0o755)
        real_directory = str(Path(shutil.which("git")).parent)
        environment = {"PATH": os.pathsep.join(["", ".", real_directory])}
        with working_directory(decoy), mock.patch.dict(os.environ, environment):
            os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)  # the Windows default
            gitops._git_paths.clear()
            resolved = Path(gitops.git_executable()).resolve()
            found = process.which("git", suffixes=(".exe",) if NT else None)
        self.assertNotEqual(resolved.parent, decoy.resolve())
        self.assertTrue(resolved.is_absolute() and resolved.parent == Path(real_directory).resolve())
        self.assertEqual(Path(found).resolve(), resolved)

    def test_which_never_returns_a_relative_result_or_current_directory_entry(self):
        decoy = self.tmp / "decoy2"
        decoy.mkdir()
        name = "probe-tool.exe" if NT else "probe-tool"
        (decoy / name).write_bytes(b"x")
        os.chmod(decoy / name, 0o755)
        with working_directory(decoy), mock.patch.dict(os.environ, {"PATH": os.pathsep.join([".", str(decoy), ""])}):
            self.assertIsNone(process.which("probe-tool"))
        with mock.patch.dict(os.environ, {"PATH": str(decoy)}):
            self.assertEqual(Path(process.which("probe-tool")), decoy / name)
        self.assertIsNone(process.which("." + os.sep + name))

    @unittest.skipUnless(NT, "System32 tools are Windows-only")
    def test_system_tools_resolve_to_system32(self):
        for tool in ("taskkill", "icacls"):
            resolved = Path(process.system_tool(tool))
            self.assertTrue(resolved.is_absolute() and resolved.parent.name.lower() == "system32", resolved)


class CommittedFilters(RepoCase):
    """A filter that the committed .gitattributes selects must not run, whoever wrote its script."""

    def base_files(self):
        return {**super().base_files(), ".gitattributes": b"*.dat filter=evil\n", "a.dat": b"alpha\n"}

    def configure_filters(self):
        script = self.tmp / "filter.py"
        script.write_text("import sys\nopen(sys.argv[1] + sys.argv[2], 'w').write('ran')\n"
                          "sys.stdout.buffer.write(sys.stdin.buffer.read().upper())\n", encoding="utf-8")
        command = f'"{PYTHON.as_posix()}" "{script.as_posix()}" "{self.flags.as_posix()}/"'
        self.set_config(f'[filter "evil"]\n\tclean = {command} clean\n\tsmudge = {command} smudge\n'
                        "\trequired = true\n")

    def test_configured_filters_never_run_and_patches_round_trip(self):
        self.configure_filters()
        wt = self.worktree()  # checkout would run smudge
        (wt / "a.dat").write_bytes(b"alpha\nbeta\r\n")
        (wt / "b.dat").write_bytes(b"new\n")
        tree = gitops.snapshot_tree(wt)
        gitops._worktree_fingerprint(wt, self.base)
        gitops.changed_paths(wt, self.base)
        self.assertEqual(self.planted(), [])
        patch = gitops.tree_diff(wt, self.base, tree)
        self.assertIn(b"+beta\r\n", patch)
        self.assertNotIn(b"BETA", patch)
        gitops.apply_patch(self.repo, patch)
        self.assertEqual((self.repo / "a.dat").read_bytes(), b"alpha\nbeta\r\n")
        self.assertEqual((self.repo / "b.dat").read_bytes(), b"new\n")

    def test_a_filter_added_after_an_earlier_call_is_still_neutralised(self):
        wt = self.worktree()
        gitops.snapshot_tree(wt)  # an earlier call finds no filter
        self.configure_filters()  # lands in the common .git/config, not the worktree's own config
        (wt / "a.dat").write_bytes(b"alpha\nbeta\n")
        gitops.snapshot_tree(wt)
        self.assertEqual(self.planted(), [])

    def test_a_filter_added_through_include_path_after_an_earlier_call_is_still_neutralised(self):
        wt = self.worktree()
        gitops.snapshot_tree(wt)
        script = self.tmp / "included.py"
        script.write_text("import sys\nopen(sys.argv[1] + sys.argv[2], 'w').write('ran')\n"
                          "sys.stdout.buffer.write(sys.stdin.buffer.read().upper())\n", encoding="utf-8")
        command = f'"{PYTHON.as_posix()}" "{script.as_posix()}" "{self.flags.as_posix()}/"'
        included = self.tmp / "shared-filters.gitconfig"
        included.write_text(f'[filter "evil"]\n\tclean = {command} clean\n\tsmudge = {command} smudge\n'
                            "\trequired = true\n", encoding="utf-8")
        self.set_config(self.config.read_text(encoding="utf-8") + f'[include]\n\tpath = {included.as_posix()}\n')
        (wt / "a.dat").write_bytes(b"alpha\nbeta\n")
        gitops.snapshot_tree(wt)
        self.assertEqual(self.planted(), [])

    def test_git_calls_get_a_scrubbed_environment(self):
        hostile = {"AWS_SECRET_ACCESS_KEY": "s", "MY_API_TOKEN": "t", "GITHUB_TOKEN": "g", "UNLISTED_NAME": "u",
                   "GIT_DIR": "x"}
        with mock.patch.dict(os.environ, hostile):
            environment = gitops._git_environment(None, None)
        for name in hostile:
            self.assertNotIn(name, environment)
        self.assertIn("GIT_CONFIG_GLOBAL", environment)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertTrue(any(key.upper() == "PATH" for key in environment))


class Identifiers(RepoCase):
    def test_reserved_and_unsafe_task_ids_are_rejected(self):
        for bad in ("con", "nul", "aux", "prn", "com1", "com9", "lpt1", "lpt9", "nul.txt", "con.json", "v1..2", "a..b",
                    "UPPER", "", "-x", "x" * 65, None):
            with self.subTest(task_id=bad), self.assertRaises(gitops.BridgeError):
                gitops.validate_task_id(bad)
        for good in ("hello-world", "v1.2", "console", "com10", "lpt0", "x.nul", "a.b.c", "t1", "nullable"):
            with self.subTest(task_id=good):
                self.assertEqual(gitops.validate_task_id(good), good)

    def test_the_task_lock_refuses_a_device_name(self):
        with self.assertRaises(gitops.BridgeError):
            gitops.active_task_record(self.tmp, "nul")

    def test_branch_names_built_from_task_ids_are_checked_by_git(self):
        self.assertTrue(gitops.branch_name_valid("delegate/codex-hello-world-20261006t000000000000z"))
        self.assertFalse(gitops.branch_name_valid("delegate/codex-v1..2-20261006t000000000000z"))

    def test_context_directory_globs_match_on_path_boundaries(self):
        observed = revision.context_path_observed
        self.assertTrue(observed("src/**", "cat src/app.py"))
        self.assertTrue(observed("src/**", "get-childitem src"))
        self.assertTrue(observed("src/**", "type c:\\repo\\src\\app.py".replace("\\", "/")))
        self.assertTrue(observed("src\\**", "cat src/app.py"))
        self.assertFalse(observed("src/**", "ls src-other/x.py"))
        self.assertFalse(observed("src/**", "cat resources/src.txt"))
        self.assertTrue(observed("README.md", "cat readme.md"))
        self.assertFalse(observed("README.md", "cat notes.md"))

    def test_context_paths_are_also_claimed_through_files_read(self):
        claimed = revision.context_path_claimed
        self.assertTrue(claimed("src/app.py", ["src\\app.py"]))
        self.assertTrue(claimed("SRC/App.py", ["./src/app.py"]))
        self.assertTrue(claimed("src/app.py", ["C:/work/repo/src/app.py"]))
        self.assertTrue(claimed("src/**", ["src/deep/app.py"]))
        self.assertFalse(claimed("src/**", ["src-other/app.py"]))
        self.assertFalse(claimed("src/app.py", ["src/app.pyc", "other/app.py"]))
        self.assertFalse(claimed("src/app.py", []))

    def test_doubled_separators_in_a_command_still_match(self):
        self.assertTrue(revision.context_path_observed("src/app.py", "get-content src\\\\app.py"))
        self.assertTrue(revision.context_path_observed("src/**", "ls src//deep"))


class RenameScope(unittest.TestCase):
    def test_changed_paths_reports_both_ends_of_a_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            (repo / "secret").mkdir()
            (repo / "allowed").mkdir()
            (repo / "secret" / "a.txt").write_text("identical content\n" * 20, encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD").strip()
            git(repo, "mv", "secret/a.txt", "allowed/a.txt")
            self.assertEqual(gitops.changed_paths(repo, base), ["allowed/a.txt", "secret/a.txt"])
            # The primary-checkout status must likewise list both sides.
            status = gitops.primary_status(SimpleNamespace(repo_root=repo))
            self.assertNotIn("->", status)
            self.assertEqual(gitops.primary_status_changes("", status), ["allowed/a.txt", "secret/a.txt"])


class BaseCommitCase(unittest.TestCase):
    def test_uppercase_base_commit_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            init_repo(repo)
            (repo / "f.txt").write_text("x\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD").strip()
            gitops._verify_repository(SimpleNamespace(repo_root=repo, base_commit=base.upper()))
            with self.assertRaises(gitops.BridgeError):
                gitops._verify_repository(SimpleNamespace(repo_root=repo, base_commit="0" * 40))


class PrimaryContentFingerprint(unittest.TestCase):
    def test_content_change_of_an_already_dirty_file_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            for name in ("dirty.txt", "other.txt"):
                (repo / name).write_text("base\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            task = SimpleNamespace(repo_root=repo)
            (repo / "dirty.txt").write_text("first edit\n", encoding="utf-8")
            (repo / "other.txt").write_text("other edit\n", encoding="utf-8")
            before = gitops.primary_status(task)
            self.assertEqual(gitops.primary_status_changes(before, gitops.primary_status(task)), [])
            (repo / "dirty.txt").write_text("second edit, same status code\n", encoding="utf-8")
            after = gitops.primary_status(task)
            self.assertNotEqual(before, after)
            self.assertEqual(gitops.primary_status_changes(before, after), ["dirty.txt"])

    def test_untracked_content_and_odd_names_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            (repo / "base.txt").write_text("x\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            task = SimpleNamespace(repo_root=repo)
            name = "with space é.txt"
            (repo / name).write_text("one\n", encoding="utf-8")
            before = gitops.primary_status(task)
            (repo / name).write_text("two\n", encoding="utf-8")
            self.assertEqual(gitops.primary_status_changes(before, gitops.primary_status(task)), [name])


class GitTimeout(unittest.TestCase):
    def test_a_hung_git_command_raises_bridge_error(self):
        expired = subprocess.TimeoutExpired(["git", "status"], gitops.GIT_TIMEOUT_SECONDS)
        with mock.patch.object(gitops.subprocess, "run", side_effect=expired):
            with self.assertRaisesRegex(gitops.BridgeError, r"timed out after \d+ seconds: status"):
                gitops._git(Path.cwd(), ["status"])
            with self.assertRaises(gitops.BridgeError):
                gitops._git(Path.cwd(), ["status"], check=False)


class StatusPaths(unittest.TestCase):
    def test_status_paths_include_renames_and_git_quoted_names(self):
        cases = {
            "R  old.py -> new.py": ["old.py", "new.py"],
            'R  "old -> name.py" -> "new\\tname.py"': ["old -> name.py", "new\tname.py"],
            ' R old space.py -> "new\\\"quote.py"': ["old space.py", 'new"quote.py'],
            '?? "caf\\303\\251.py"': ["café.py"],
            ' M "back\\\\slash.py"': ["back\\slash.py"],
            "?? leading and trailing space ": ["leading and trailing space "],
            "?? plain -> filename.py": ["plain -> filename.py"],
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(gitops.status_line_paths(line), expected)

    def test_status_changes_ignore_existing_dirty_paths_and_include_removed_entries(self):
        before = " M unchanged.py\n M removed.py\n?? updated.py\n"
        after = " M unchanged.py\nA  updated.py\nR  old.py -> new.py\n"
        self.assertEqual(gitops.primary_status_changes(before, after),
                         ["new.py", "old.py", "removed.py", "updated.py"])


class PathsAndStatus(RepoCase):
    def test_a_path_holding_line_break_characters_is_not_cut_in_two(self):
        name = "odd\u2028name\u0085x.txt"
        try:
            (self.repo / name).write_text("one\n", encoding="utf-8")
        except OSError:
            self.skipTest("this file system refuses the name")
        task = SimpleNamespace(repo_root=self.repo)
        before = gitops.primary_status(task)
        (self.repo / name).write_text("two\n", encoding="utf-8")
        after = gitops.primary_status(task)
        self.assertEqual(gitops.primary_status_changes(before, after), [name])
        self.assertTrue(all("\u2028" not in line for line in after.split("\n")), "fingerprint lines stay ASCII")

    def test_the_scope_rule_has_one_definition(self):
        self.assertIs(gitops.path_allowed, revision.path_within_scope)
        self.assertTrue(gitops.path_allowed("src\\a.py", ["src/**"]))
        self.assertFalse(gitops.path_allowed("src-other/a.py", ["src"]))

    def test_tracked_links_are_found_in_batches(self):
        blob = git(self.repo, "hash-object", "-w", "--stdin", input=b"target").strip()
        for number in range(3):
            git(self.repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},link{number}")
        git(self.repo, "commit", "-qm", "links")
        base = git(self.repo, "rev-parse", "HEAD").strip()
        names = [f"link{number}" for number in range(3)] + [f"missing{number}" for number in range(120)]
        calls = self.spy_on_git()
        self.assertEqual(gitops._tracked_links(self.repo, base, names), {"link0", "link1", "link2"})
        self.assertLessEqual(len([argv for argv, _ in calls if "ls-tree" in argv]), 3)


class HooksDirectory(unittest.TestCase):
    def test_stale_empty_hooks_directories_from_killed_processes_are_swept(self):
        old = time.time() - 3 * 24 * 3600
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(tempfile, "tempdir", temp), \
                mock.patch.object(gitops, "_hooks_directory", None):
            stale = Path(temp) / "delegate-no-hooks-stale"
            busy = Path(temp) / "delegate-no-hooks-busy"
            fresh = Path(temp) / "delegate-no-hooks-fresh"
            for folder in (stale, busy, fresh):
                folder.mkdir()
            (busy / "keep.txt").write_text("not empty", encoding="utf-8")
            for folder in (stale, busy):
                os.utime(folder, (old, old))
            created = Path(gitops._empty_hooks_directory())
            self.assertFalse(stale.exists())
            self.assertTrue(busy.exists() and fresh.exists())
            self.assertEqual(list(created.iterdir()), [])
            shutil.rmtree(created, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
