"""Where the private state lives: resolution, the marker, adoption and concurrent first use.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import errno
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import BridgeCase, NT, remove_tree, run_in_threads
from claude_codex_bridge import process, state


class StateRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dtc-state-"))
        self.addCleanup(remove_tree, self.tmp)
        self.home = self.tmp / "claude"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(state.STATE_ENV, None)

    def at(self, name):
        return mock.patch.object(state, "PACKAGE_ROOT", self.tmp / name)

    def install_keyed(self, install: str, *, schema=1) -> Path:
        """A state directory the way version 1.0.0 made one: keyed on the install path."""
        with self.at(install):
            root = self.home / "delegate-to-codex-state" / state.installation_id()[:16]
            marker = {"schema_version": schema, "application": "delegate-to-codex",
                      "installation_sha256": state.installation_id()}
        (root / "artifacts").mkdir(parents=True)
        (root / ".delegate-to-codex-state.json").write_text(json.dumps(marker), encoding="utf-8")
        return root

    def test_state_root_does_not_depend_on_the_install_path(self):
        with self.at("skill-1.0"):
            first = state.ensure_state_root()
        with self.at("somewhere-else"):
            second = state.ensure_state_root()
        self.assertEqual(first, second)
        self.assertEqual(first.name, "default")
        self.assertEqual(first, (self.home / "delegate-to-codex-state" / "default").resolve())

    def test_an_install_keyed_state_from_version_1_0_is_kept_across_an_install_move(self):
        old = self.install_keyed("skill-a")
        original = (old / ".delegate-to-codex-state.json").read_text(encoding="utf-8")
        with self.at("skill-a"):
            self.assertEqual(state.ensure_state_root(), old.resolve())
        with self.at("skill-b"):  # the skill moved: it still finds the one existing state directory
            self.assertEqual(state.ensure_state_root(), old.resolve())
        self.install_keyed("skill-c")  # an unrelated second install appears later
        with self.at("skill-d"):
            self.assertEqual(state.ensure_state_root(), old.resolve())  # remembered by the pointer
        self.assertEqual((old / ".delegate-to-codex-state.json").read_text(encoding="utf-8"), original)

    def test_a_recorded_worktree_can_be_resolved_after_its_directory_is_gone(self):
        gone = self.tmp / "state" / "wt" / "0123456789"
        with self.assertRaises(FileNotFoundError):
            state.recorded_worktree(self.tmp, {"worktree": str(gone)})
        self.assertEqual(state.recorded_worktree(self.tmp, {"worktree": str(gone)}, strict=False), gone.resolve())

    def test_several_install_keyed_states_are_not_merged_but_listed(self):
        first, second = self.install_keyed("skill-a"), self.install_keyed("skill-b")
        with self.at("skill-a"):
            self.assertEqual(state.ensure_state_root(), first.resolve())
        with self.at("skill-b"):
            self.assertEqual(state.ensure_state_root(), second.resolve())
        with self.at("skill-new"):
            self.assertEqual(state.state_root().name, "default")
            listed = {Path(item["path"]).name for item in state.discover_state_roots()}
            self.assertTrue({first.name, second.name} <= listed)
            self.assertEqual(state.adopt_state_root(first), first.resolve())
            self.assertEqual(state.ensure_state_root(), first.resolve())

    def test_an_explicit_directory_from_another_installation_is_adopted(self):
        other = self.install_keyed("some-other-install")
        shutil.move(str(other), str(self.tmp / "carried"))
        with mock.patch.dict(os.environ, {state.STATE_ENV: str(self.tmp / "carried")}):
            with self.at("brand-new-path"):
                self.assertEqual(state.ensure_state_root(), (self.tmp / "carried").resolve())

    def test_unmarked_nonempty_and_foreign_directories_are_still_refused(self):
        stray = self.tmp / "stray"
        stray.mkdir()
        (stray / "notes.txt").write_text("mine", encoding="utf-8")
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / ".delegate-to-codex-state.json").write_text(json.dumps({"application": "other"}), encoding="utf-8")
        for path in (stray, foreign):
            with self.subTest(path=path.name), mock.patch.dict(os.environ, {state.STATE_ENV: str(path)}):
                with self.assertRaises(ValueError):
                    state.ensure_state_root()

    def test_marker_appearing_between_the_check_and_the_listing_is_not_an_error(self):
        root = self.tmp / "racing-state"
        real_mkdir = Path.mkdir
        injected = []

        def mkdir_then_lose_the_race(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            if path == root.resolve() and not injected:
                injected.append(True)  # another process initializes the directory right now
                (path / ".delegate-to-codex-state.json").write_text(json.dumps(
                    {"schema_version": 2, "application": "delegate-to-codex", "state_id": "winner"}), encoding="utf-8")
                real_mkdir(path / "artifacts")

        with mock.patch.dict(os.environ, {state.STATE_ENV: str(root)}), \
                mock.patch.object(Path, "mkdir", mkdir_then_lose_the_race):
            self.assertEqual(state.ensure_state_root(), root.resolve())
        self.assertTrue(injected)
        self.assertEqual(state.state_id(root.resolve()), "winner")

    def test_concurrent_first_runs_agree_on_one_marker(self):
        root = self.tmp / "parallel-state"
        results, errors = [], []
        barrier = threading.Barrier(6)

        def initialize():
            try:
                barrier.wait(timeout=10)
                results.append(state.ensure_state_root())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with mock.patch.dict(os.environ, {state.STATE_ENV: str(root)}):
            threads = [threading.Thread(target=initialize) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {root.resolve()})
        self.assertEqual([p.name for p in root.iterdir()], [".delegate-to-codex-state.json"])
        self.assertTrue(state.state_id(root.resolve()))

    @unittest.skipIf(NT, "POSIX permission bits")
    def test_state_directory_is_owner_only_on_posix(self):
        root = self.tmp / "private-state"
        root.mkdir(mode=0o755)
        os.chmod(root, 0o755)
        with mock.patch.dict(os.environ, {state.STATE_ENV: str(root)}):
            state.ensure_state_root()
        self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
        self.assertEqual(state.state_permission_report(root)["status"], "ok")
        os.chmod(root, 0o755)
        self.assertEqual(state.state_permission_report(root)["status"], "warning")

    @unittest.skipUnless(NT, "ACL inspection is Windows-only")
    def test_acl_report_flags_broad_principals(self):
        root = self.tmp / "acl-state"
        root.mkdir()
        granted = subprocess.run([process.system_tool("icacls"), str(root), "/grant", "*S-1-1-0:(OI)(CI)R"],
                                 capture_output=True)
        if granted.returncode != 0:
            self.skipTest("cannot change ACLs here")
        report = state.state_permission_report(root)
        self.assertEqual(report["status"], "warning")
        self.assertTrue(any("Everyone" in issue for issue in report["issues"]), report)
        before = state.state_permission_report(self.tmp)  # never modifies anything
        self.assertIn(before["status"], {"ok", "warning", "unknown"})

    @unittest.skipUnless(NT, "ACL inspection is Windows-only")
    def test_a_failed_icacls_query_is_unknown_not_ok(self):
        root = self.tmp / "acl-failed"
        root.mkdir()
        failed = subprocess.CompletedProcess([], 5, stdout=b"", stderr=b"")
        with mock.patch.object(state.subprocess, "run", return_value=failed):
            report = state.state_permission_report(root)
        self.assertEqual(report["status"], "unknown")
        self.assertTrue(report["issues"])

    def test_the_marker_is_published_whole_when_hard_links_are_unavailable(self):
        root = self.tmp / "no-links-state"
        real_open = Path.open

        def open_unless_exclusive(path, mode="r", *args, **kwargs):
            if "x" in mode:
                raise AssertionError("must not write the final marker in place")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.dict(os.environ, {state.STATE_ENV: str(root)}),                 mock.patch.object(state.os, "link", side_effect=OSError(errno.EPERM, "no links")),                 mock.patch.object(Path, "open", open_unless_exclusive):
            state.ensure_state_root()
        self.assertTrue(state.state_id(root.resolve()))
        self.assertEqual([p.name for p in root.iterdir()], [".delegate-to-codex-state.json"])

    def test_a_stray_empty_default_folder_does_not_hide_the_one_install_keyed_state(self):
        old = self.install_keyed("skill-a")
        (self.home / "delegate-to-codex-state" / "default").mkdir()  # no marker: not a state directory yet
        with self.at("skill-b"):
            self.assertEqual(state.state_root(), old.resolve())
            self.assertEqual(state.ensure_state_root(), old.resolve())

    def test_a_marked_default_is_kept_when_install_keyed_directories_exist_too(self):
        with self.at("skill-a"):
            default = state.ensure_state_root()
        self.install_keyed("skill-old")
        with self.at("skill-b"):
            self.assertEqual(state.state_root(), default)

    def test_the_refusal_inside_a_repository_names_the_way_out(self):
        repo = self.tmp / "dotfiles"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "DELEGATE_TO_CODEX_STATE_DIR"):
            state.require_external_storage(repo / "state")


class StateMarkerAcrossFilesystems(unittest.TestCase):
    def test_marker_published_when_the_parent_link_is_cross_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            real_link = os.link
            calls = []

            def link(src, dst, *args, **kwargs):
                calls.append(Path(src).parent)
                if Path(src).parent == root.parent:
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return real_link(src, dst, *args, **kwargs)

            with mock.patch.dict(os.environ, {state.STATE_ENV: str(root)}), \
                    mock.patch.object(state, "require_external_storage", lambda value: value), \
                    mock.patch.object(os, "link", link):
                state.ensure_state_root()
                state.ensure_state_root()  # a second initialization sees the complete marker
            marker = json.loads((root / ".delegate-to-codex-state.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["application"], "delegate-to-codex")
            self.assertEqual(calls, [root.parent, root])
            self.assertEqual([p.name for p in root.iterdir()], [".delegate-to-codex-state.json"])
            self.assertEqual([p for p in root.parent.iterdir() if p.name.startswith(".delegate-state-")], [])

    def test_marker_published_when_hard_links_are_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            with mock.patch.dict(os.environ, {state.STATE_ENV: str(root)}), \
                    mock.patch.object(state, "require_external_storage", lambda value: value), \
                    mock.patch.object(os, "link", side_effect=OSError(errno.EPERM, "no hard links")):
                state.ensure_state_root()
            self.assertEqual([p.name for p in root.iterdir()], [".delegate-to-codex-state.json"])


class ConcurrentInitialization(BridgeCase):
    seed_auto_review = None  # the state folder must start empty

    def test_concurrent_initial_state_markers_are_published_complete(self):
        barrier = threading.Barrier(2, timeout=60)
        link = os.link
        arrived = threading.local()

        def publish(source, destination):
            self.assertEqual(json.loads(source.read_text(encoding="utf-8"))["application"], "delegate-to-codex")
            if not getattr(arrived, "done", False):  # a retry in the same thread must not wait for its peer again
                arrived.done = True
                barrier.wait()
            link(source, destination)

        with mock.patch.object(state.os, "link", side_effect=publish):
            results = run_in_threads(lambda _: state.ensure_state_root(), range(2), barrier)
        self.assertEqual(results, [self.state, self.state])
        self.assertEqual([p.name for p in self.state.iterdir()], [".delegate-to-codex-state.json"])
        self.assertFalse(list(self.tmp.glob(".delegate-state-*.tmp")))


    def test_two_initializers_without_hard_links_keep_the_first_marker(self):
        barrier = threading.Barrier(2, timeout=60)
        exists = Path.exists
        arrived = threading.local()

        def exists_then_wait(path):
            found = exists(path)
            if path.name == state.MARKER_NAME and not found and not getattr(arrived, "done", False):
                arrived.done = True  # both initializers pass the existence check before either publishes
                barrier.wait()
            return found

        with mock.patch.object(state.os, "link", side_effect=OSError(errno.EPERM, "no links")),                 mock.patch.object(Path, "exists", exists_then_wait):
            results = run_in_threads(lambda _: (state.ensure_state_root(), state.state_id(self.state)), range(2), barrier)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][1], state.state_id(self.state))
        self.assertEqual([p.name for p in self.state.iterdir()], [".delegate-to-codex-state.json"])


if __name__ == "__main__":
    unittest.main()
