"""Task locks, stale-lock recovery, lock ownership, atomic writes and retried Git commands.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import errno
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import BridgeCase, run_in_threads
from claude_codex_bridge import bridge, gitops
from claude_codex_bridge.contracts import load_task


class ConcurrencyHelpers(unittest.TestCase):
    def test_lock_error_retries_then_succeeds(self):
        messages = ["index.lock: File exists", "'.git/refs/heads/x.lock': File exists",
                    "Unable to create repository lock", "cannot lock ref 'refs/heads/x'"]
        success = subprocess.CompletedProcess(["git"], 0, "done", "")
        for message in messages:
            with self.subTest(message=message):
                with mock.patch.object(gitops, "_git", side_effect=[gitops.BridgeError(message),
                        gitops.BridgeError(message), success]) as command, mock.patch.object(gitops.time, "sleep") as sleep:
                    self.assertIs(gitops._git_retry(Path("."), ["worktree", "add", "x"]), success)
                    self.assertEqual(command.call_count, 3)
                    self.assertEqual(sleep.call_args_list, [mock.call(0.5), mock.call(1.0)])

    def test_git_retry_stops_on_other_errors_and_after_five_attempts(self):
        for message, attempts in (("fatal: invalid reference", 1), ("cannot lock ref", 5)):
            with self.subTest(message=message):
                with mock.patch.object(gitops, "_git", side_effect=gitops.BridgeError(message)) as command, \
                        mock.patch.object(gitops.time, "sleep") as sleep:
                    with self.assertRaises(gitops.BridgeError):
                        gitops._git_retry(Path("."), ["worktree", "add", "x"])
                    self.assertEqual(command.call_count, attempts)
                    self.assertEqual(sleep.call_count, attempts - 1)

    def test_atomic_cache_writers_use_distinct_same_directory_temporary_files(self):
        with tempfile.TemporaryDirectory(prefix="dtc-cache-") as directory:
            cache = Path(directory) / "usage.json"
            seed = {"gate": "available", "_cached_at": time.time(), "writer": "seed"}
            cache.write_text(json.dumps(seed), encoding="utf-8")
            sources = []
            seen_lock = threading.Lock()
            before_publishing = []
            # Every writer has written its own temporary file before any of them publishes. The barrier's action
            # only records the cache: the comparison happens below, so a failed assertion cannot break the barrier.
            barrier = threading.Barrier(4, action=lambda: before_publishing.append(cache.read_text(encoding="utf-8")), timeout=60)
            replace = os.replace

            def publish(source, destination):
                self.assertEqual(source.parent, cache.parent)
                self.assertEqual(destination, cache)
                self.assertIn(json.loads(source.read_text(encoding="utf-8"))["writer"], range(4))
                with seen_lock:
                    first_attempt = source not in sources
                    if first_attempt:
                        sources.append(source)
                if first_attempt:
                    barrier.wait()
                replace(source, destination)

            with mock.patch.object(gitops.os, "replace", side_effect=publish):
                run_in_threads(lambda i: gitops.atomic_write_json(cache, {**seed, "writer": i}), range(4), barrier)
            self.assertEqual([json.loads(text) for text in before_publishing], [seed])
            self.assertEqual(len(set(sources)), 4)
            self.assertIn(json.loads(cache.read_text(encoding="utf-8"))["writer"], range(4))
            self.assertEqual(list(cache.parent.iterdir()), [cache])

    def test_failed_atomic_cache_write_preserves_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(prefix="dtc-cache-") as directory:
            cache = Path(directory) / "usage.json"
            cache.write_text('{"previous": true}', encoding="utf-8")
            with mock.patch.object(gitops.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    gitops.atomic_write_json(cache, {"new": True})
            self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), {"previous": True})
            self.assertEqual(list(cache.parent.iterdir()), [cache])


class TaskLockRecovery(unittest.TestCase):
    def _lock(self, root: Path, text: str, age: float) -> Path:
        (root / ".active").mkdir(parents=True)
        lock = root / ".active" / "task-one.json"
        lock.write_text(text, encoding="utf-8")
        old = time.time() - age
        os.utime(lock, (old, old))
        return lock

    def test_abandoned_truncated_claim_is_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = self._lock(root, '{"task_id": "task-o', age=600)
            result = gitops.clear_stale_task_lock(root, "task-one")
            self.assertEqual(result["status"], "STALE_LOCK_ARCHIVED")
            self.assertFalse(lock.exists())
            self.assertTrue(Path(result["archived_lock"]).is_file())

    def test_fresh_unparsable_claim_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = self._lock(root, "", age=0)
            with self.assertRaises(gitops.BridgeError):
                gitops.clear_stale_task_lock(root, "task-one")
            self.assertTrue(lock.exists())

    def test_claim_is_written_in_one_call_and_removed_on_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = SimpleNamespace(task_id="task-one", timeout_seconds=5)
            task_file = root / "task.json"
            task_file.write_text("{}", encoding="utf-8")
            with gitops.task_lock(root, task, task_file, "run") as lock:
                self.assertEqual(json.loads(lock.read_text(encoding="utf-8"))["task_id"], "task-one")
            self.assertFalse(lock.exists())
            real_open = Path.open

            def failing_open(self, mode="r", *args, **kwargs):
                handle = real_open(self, mode, *args, **kwargs)
                if mode == "x":
                    handle.write = mock.Mock(side_effect=OSError(errno.ENOSPC, "disk full"))
                return handle

            with mock.patch.object(Path, "open", failing_open), self.assertRaises(OSError):
                with gitops.task_lock(root, task, task_file, "run"):
                    pass
            self.assertFalse((root / ".active" / "task-one.json").exists())


class TaskLockOwnership(BridgeCase):
    def test_a_lock_taken_over_after_clear_stale_lock_is_not_deleted(self):
        task_file = self.task()
        task = load_task(task_file)
        root = self.tmp / "locks"
        with gitops.task_lock(root, task, task_file, "run") as lock:
            lock.write_text(json.dumps({"task_id": "hello-world", "pid": 12345, "operation": "run"}), encoding="utf-8")
        self.assertTrue(lock.exists(), "another process's claim must survive this process's exit")
        self.assertEqual(json.loads(lock.read_text(encoding="utf-8"))["pid"], 12345)
        lock.unlink()
        with gitops.task_lock(root, task, task_file, "run") as lock:
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())


class WritesStayUnderTheLock(BridgeCase):
    def result_writes(self):
        writes = []
        real = bridge.write_json

        def spy(path, value, *args, **kwargs):
            if Path(path).name == "result.json":
                writes.append(bridge.active("hello-world")["status"])
            return real(path, value, *args, **kwargs)

        return writes, mock.patch.object(bridge, "write_json", side_effect=spy)

    def test_run_writes_usage_and_result_while_locked(self):
        writes, patch = self.result_writes()
        with patch:
            bridge.run(self.task())
        self.assertTrue(writes)
        self.assertEqual(set(writes), {"RUNNING"})
        self.assertEqual(bridge.active("hello-world")["status"], "NOT_RUNNING")

    def test_usage_lookup_runs_while_locked(self):
        seen = []

        def usage_after(command, before):
            seen.append(bridge.active("hello-world")["status"])
            return {"gate": "available"}

        with mock.patch.object(bridge, "_usage_after", side_effect=usage_after):
            bridge.run(self.task())
        self.assertEqual(seen, ["RUNNING"])

    def test_continue_writes_result_while_locked(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        task = self.task()
        first = bridge.run(task)
        writes, patch = self.result_writes()
        with patch:
            bridge.continue_task(task, Path(first["artifact_directory"]), 4)
        self.assertTrue(writes)
        self.assertEqual(set(writes), {"RUNNING"})

    def test_continue_rereads_the_record_under_the_lock(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "extension"
        task = self.task()
        first = bridge.run(task)
        artifact = Path(first["artifact_directory"])
        real = bridge.task_lock

        def lock_after_other_command_finished(*args, **kwargs):
            # Another command updated the record between the first read and taking the lock.
            record = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
            record.update(lifecycle_status="BLOCKED")
            (artifact / "result.json").write_text(json.dumps(record), encoding="utf-8")
            return real(*args, **kwargs)

        with mock.patch.object(bridge, "task_lock", side_effect=lock_after_other_command_finished):
            with self.assertRaisesRegex(Exception, "not awaiting an extension"):
                bridge.continue_task(task, artifact, 4)


class OwnerIdentity(unittest.TestCase):
    def hold(self, root: Path):
        task = SimpleNamespace(task_id="task-one", timeout_seconds=5)
        task_file = root / "task.json"
        task_file.write_text("{}", encoding="utf-8")
        return gitops.task_lock(root, task, task_file, "run")

    def claim(self, root: Path, **changes) -> Path:
        """Leave a lock file behind that was written by a process with these recorded properties."""
        with self.hold(root) as lock:
            record = json.loads(lock.read_text(encoding="utf-8"))
        record.update(changes)
        lock.write_text(json.dumps(record), encoding="utf-8")
        return lock

    def test_a_lock_records_when_its_owner_process_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.hold(Path(tmp)) as lock:
                record = json.loads(lock.read_text(encoding="utf-8"))
                self.assertEqual(record["process_started"], gitops.process_start_token(os.getpid()))
                self.assertIsNotNone(record["process_started"])
                self.assertIs(gitops.active_task_record(Path(tmp), "task-one")["process_running"], True)

    def test_a_recycled_process_id_is_not_taken_for_the_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.claim(root, process_started="0")  # this pid now belongs to a different process than the owner
            record = gitops.active_task_record(root, "task-one")
            self.assertIs(record["process_running"], False)
            archived = gitops.clear_stale_task_lock(root, "task-one")
            self.assertEqual(archived["status"], "STALE_LOCK_ARCHIVED")

    def test_a_lock_without_a_start_time_still_trusts_the_process_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.claim(root, process_started=None)
            self.assertIs(gitops.active_task_record(root, "task-one")["process_running"], True)
            with self.assertRaises(gitops.BridgeError):
                gitops.clear_stale_task_lock(root, "task-one")

    def test_an_unreadable_claim_file_does_not_crash_the_archive_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".active").mkdir()
            (root / ".active" / "task-one.json").write_text("", encoding="utf-8")
            with mock.patch.object(Path, "stat", side_effect=PermissionError("locked by a scanner")):
                self.assertIsNone(gitops._archive_incomplete_lock(root, "task-one"))


if __name__ == "__main__":
    unittest.main()
