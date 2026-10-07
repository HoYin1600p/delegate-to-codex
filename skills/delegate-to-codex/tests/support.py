"""Shared helpers for the test suite. No provider is contacted: Codex is the test double in fake_codex.py.

Run from the skill directory:  python -B -m unittest discover -s tests
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "src"))

from claude_codex_bridge import bridge, gitops  # noqa: E402
from claude_codex_bridge import settings as user_settings  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_codex.py"
PYTHON = Path(sys.executable)
NT = os.name == "nt"
ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
EXHAUSTED = json.dumps({"rateLimits": {"primary": {"usedPercent": 100, "windowDurationMins": 10080}}})


# ------------------------------------------------------------------ helpers

def git_bytes(cwd, *args, input=None, env=None):
    """Run git with the user's configuration replaced and a fixed identity; returns the raw output."""
    environment = dict(os.environ, **ISOLATED, **(env or {}))
    return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
                          cwd=cwd, env=environment, check=True, capture_output=True, input=input).stdout


def git(cwd, *args, **options):
    return git_bytes(cwd, *args, **options).decode("utf-8")


def init_repo(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")


def remove_tree(path) -> None:
    """rmtree that also deletes read-only files (git object files) and never follows a junction."""
    def retry(function, target, _error):
        try:
            os.chmod(target, 0o700)
            function(target)
        except OSError:
            pass

    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


def make_junction(link: Path, target: Path) -> bool:
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return made.returncode == 0


def rewrite_dotgit(worktree: Path, text: str) -> None:
    """Replace the worktree's ``.git`` pointer the way a worker could (git marks it hidden on Windows)."""
    (worktree / ".git").unlink()
    (worktree / ".git").write_text(text, encoding="utf-8")


@contextmanager
def working_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def wait_gone(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not gitops.pid_is_running(pid):
            return True
        time.sleep(0.1)
    return not gitops.pid_is_running(pid)


def wait_for_file(path: Path, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return True
        time.sleep(0.1)
    return False


def kill_pid(pid: int) -> None:
    if gitops.pid_is_running(pid):
        if NT:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 9)


def run_in_threads(function, items, barrier=None):
    """Run ``function(item)`` for every item in its own thread and return the results in order.

    A thread that fails before it reaches the barrier aborts it, so its peers fail at once instead of waiting out
    the timeout, and the real error (not the BrokenBarrierError it caused) is the one raised.
    """
    items = list(items)
    results = [None] * len(items)
    errors = []

    def call(index, item):
        try:
            results[index] = function(item)
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller's thread below
            errors.append(exc)
            if barrier is not None:
                barrier.abort()

    threads = [threading.Thread(target=call, args=pair, daemon=True) for pair in enumerate(items)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    real = [exc for exc in errors if not isinstance(exc, threading.BrokenBarrierError)]
    if real or errors:
        raise (real or errors)[0]
    return results


# ------------------------------------------------------------------ base classes

class ScratchRepo(unittest.TestCase):
    """A scratch repository whose Git configuration is a file the test controls (the user's is never read)."""

    def base_files(self) -> dict[str, bytes]:
        return {"README.md": b"readme\n", "src/keep.py": b"keep = 1\n"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dtc-"))
        self.addCleanup(remove_tree, self.tmp)
        self.config = self.tmp / "gitconfig"
        self.config.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.config), "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        gitops._pins.clear()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "core.autocrlf", "false")
        for name, data in self.base_files().items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()

    def set_config(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")

    def spy_on_git(self):
        """Record every (argv, timeout) of the Git processes the bridge starts."""
        calls = []
        real = subprocess.run

        def spy(argv, **kwargs):
            calls.append((list(argv), kwargs.get("timeout")))
            return real(argv, **kwargs)

        patcher = mock.patch.object(gitops.subprocess, "run", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class RepoCase(ScratchRepo):
    """A scratch repository for tests of the Git layer, with a directory the tests plant flag files in."""

    def setUp(self):
        super().setUp()
        self.flags = self.tmp / "flags"
        self.flags.mkdir()

    def worktree(self, name="wt") -> Path:
        path = self.tmp / "wts" / name
        gitops.create_worktree(self.repo, path, f"delegate/codex-t-{name}", self.base)
        return path

    def planted(self) -> list[str]:
        return sorted(p.name for p in self.flags.iterdir())


class BridgeCase(ScratchRepo):
    """A repository plus a private state folder and the Codex test double: everything ``bridge.run`` needs.

    ``extra_files`` are committed with the repository (file name to bytes).
    """

    extra_files: dict[str, bytes] = {}
    # The user's auto-review choice recorded before each test (None leaves it unset, so a launch refuses).
    seed_auto_review: bool | None = False

    def base_files(self):
        return {
            "README.md": b"Write hello.py that prints Hello, world!\n",
            "test_hello.py": (b"import subprocess, sys, unittest\nclass T(unittest.TestCase):\n"
                              b"    def test_hello(self):\n"
                              b"        out = subprocess.run([sys.executable, 'hello.py'], capture_output=True,\n"
                              b"                             encoding='utf-8').stdout\n"
                              b"        self.assertEqual(out.strip(), 'Hello, world!')\n"),
            **self.extra_files,
        }

    def setUp(self):
        super().setUp()
        # The Codex test double needs no access to the real home directory (which can be sandboxed).
        home = mock.patch.object(Path, "home", return_value=self.tmp)
        home.start()
        self.addCleanup(home.stop)
        self.state = self.tmp / "state"
        self.log = self.tmp / "codex-calls.jsonl"
        self.setenv(
            DELEGATE_TO_CODEX_STATE_DIR=str(self.state),
            CODEX_BRIDGE_COMMAND=json.dumps([sys.executable, str(FAKE)]),
            FAKE_CODEX_LOG=str(self.log),
            FAKE_CODEX_SCENARIO="hello",
            # The worker's environment is scrubbed; the test double reads its scenario and log from these.
            CODEX_BRIDGE_WORKER_ENV="FAKE_CODEX_SCENARIO,FAKE_CODEX_LOG")
        for key in ("FAKE_CODEX_LIMITS", "FAKE_CODEX_ACCOUNT", "OPENAI_API_KEY"):
            os.environ.pop(key, None)
        if self.seed_auto_review is not None:
            self.set_auto_review(self.seed_auto_review)

    def set_auto_review(self, enabled: bool) -> None:
        user_settings.set_value("auto-review", "on" if enabled else "off")

    def setenv(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def task(self, **overrides):
        value = json.loads((SKILL / "assets" / "task.template.json").read_text(encoding="utf-8"))
        value.update(task_id="hello-world", repo_root=str(self.repo), base_commit=self.base,
                     objective="Create hello.py that prints Hello, world!",
                     context_paths=["README.md", "test_hello.py"], allowed_changed_paths=["hello.py"],
                     acceptance_criteria=["python hello.py prints Hello, world!"],
                     validation_command=[sys.executable, "-m", "unittest", "-v"], timeout_seconds=60)
        value.update(overrides)
        path = self.tmp / f"task-{len(list(self.tmp.glob('task-*.json')))}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def feedback(self, issue="Missing main guard"):
        path = self.tmp / "feedback.json"
        path.write_text(json.dumps({"findings": [{"path": "hello.py", "issue": issue,
                                                "expected_behavior": "Add a main guard"}]}), encoding="utf-8")
        return path

    def artifacts(self, task_id="hello-world"):
        return sorted((self.state / "artifacts" / "codex").glob(f"{task_id}-*"))

    def record(self, artifact):
        return json.loads((Path(artifact) / "result.json").read_text(encoding="utf-8"))

    def branches(self):
        return git(self.repo, "branch", "--list", "delegate/*").strip()

    def after_segment(self, edit):
        """Patch the worker segment so ``edit(worktree)`` runs right after the (fake) Codex finished."""
        segment = bridge._segment

        def edited(*args, **kwargs):
            result = segment(*args, **kwargs)
            edit(args[2])
            return result

        return mock.patch.object(bridge, "_segment", side_effect=edited)

    def run_with_edits(self, task, edit):
        with self.after_segment(edit):
            return bridge.run(task)

    def commit_all(self, message="more"):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", message)
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
