"""Process trees: a worker and everything it started die with the bridge, a timeout or a kill.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import NT, PYTHON, SKILL, kill_pid, remove_tree, wait_for_file, wait_gone
from claude_codex_bridge import codexcli, gitops, process


HARNESS = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from claude_codex_bridge import process\n"
    "child = 'import os, time; open(' + repr(sys.argv[2]) + ', \"w\").write(str(os.getpid())); time.sleep(300)'\n"
    "process.run_process(Path(sys.executable), ['-c', child], cwd=sys.argv[3], timeout_seconds=300)\n"
)


APP_SERVER_WITH_CHILD = (
    "import json, os, subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import os, time; open(os.environ[\"CHILD_PID_FILE\"], \"w\").write(str(os.getpid())); "
    "time.sleep(300)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    if 'id' in message:\n"
    "        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)\n"
)


ORPHANING_PARENT = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "if len(sys.argv) > 1:\n"
    "    open(sys.argv[1], 'w').write(str(child.pid))\n"
)


class ProcessTrees(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dtc-proc-"))
        self.addCleanup(remove_tree, self.tmp)
        self.pids = []
        self.addCleanup(lambda: [kill_pid(pid) for pid in self.pids])

    @unittest.skipUnless(NT, "the Job Object is Windows-only")
    def test_worker_tree_dies_when_the_bridge_process_is_killed(self):
        pid_file = self.tmp / "child.pid"
        bridge_like = subprocess.Popen([sys.executable, "-c", HARNESS, str(SKILL / "src"), str(pid_file),
                                        str(self.tmp)])
        self.addCleanup(lambda: bridge_like.poll() is None and bridge_like.kill())
        self.assertTrue(wait_for_file(pid_file), "the worker never started")
        child = int(pid_file.read_text(encoding="utf-8"))
        self.pids.append(child)
        self.assertTrue(gitops.pid_is_running(child))
        bridge_like.kill()  # TerminateProcess on the bridge only, not its tree
        bridge_like.wait(timeout=15)
        self.assertTrue(wait_gone(child), "the worker outlived its killed bridge")

    @unittest.skipUnless(NT, "the Job Object is Windows-only")
    def test_a_started_process_is_in_a_job_before_it_runs(self):
        child, job = process.start_process([sys.executable, "-c", "import time; time.sleep(60)"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pids.append(child.pid)
        try:
            self.assertIsNotNone(job.handle)
            self.assertIsNone(child.poll())
        finally:
            process.kill_tree(child, job)
            job.close()
        child.wait(timeout=15)

    def test_closing_the_app_server_kills_the_whole_tree(self):
        stub = self.tmp / "stub.py"
        stub.write_text(APP_SERVER_WITH_CHILD, encoding="utf-8")
        pid_file = self.tmp / "app-child.pid"
        with mock.patch.dict(os.environ, {"CHILD_PID_FILE": str(pid_file)}):
            command = codexcli.CodexCommand(PYTHON, (str(stub),))
            with codexcli.AppServer(command, timeout=20):
                self.assertTrue(wait_for_file(pid_file), "the app-server's child never started")
                child = int(pid_file.read_text(encoding="utf-8"))
                self.pids.append(child)
        self.assertTrue(wait_gone(child), "a descendant of the app-server survived close()")

    def test_taskkill_fallback_uses_an_absolute_path(self):
        if not NT:
            self.skipTest("taskkill is Windows-only")
        seen = []
        with mock.patch.object(process.subprocess, "run", side_effect=lambda argv, **kw: seen.append(argv)):
            fake = mock.Mock(pid=1)
            fake.poll.return_value = None
            process._kill_tree(fake, None)
        self.assertTrue(seen and Path(seen[0][0]).is_absolute())


class ProcessTreeKill(unittest.TestCase):
    def setUp(self):
        self.child_pid = None
        self.addCleanup(self._reap)

    def _reap(self):
        if self.child_pid and gitops.pid_is_running(self.child_pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.child_pid), "/F"], capture_output=True)
            else:
                os.kill(self.child_pid, 9)

    def test_timeout_kills_descendant_that_outlives_the_leader(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            result = process.run_process(PYTHON, ["-c", ORPHANING_PARENT], cwd=tmp, timeout_seconds=2)
            elapsed = time.monotonic() - started
        self.child_pid = int(result.stdout.split()[0])
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 20)
        self.assertTrue(wait_gone(self.child_pid), "descendant holding the pipes survived the timeout")

    def test_cancellation_kills_descendant_that_outlives_the_leader(self):
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            result = process.run_process(PYTHON, ["-c", ORPHANING_PARENT], cwd=tmp, timeout_seconds=60,
                                         cancel_event=cancel)
            elapsed = time.monotonic() - started
        self.child_pid = int(result.stdout.split()[0])
        self.assertTrue(result.cancelled)
        self.assertTrue(result.interrupted)
        self.assertLess(elapsed, 20)
        self.assertTrue(wait_gone(self.child_pid))

    def test_final_output_wait_is_bounded_even_when_the_kill_is_ineffective(self):
        # The surviving child keeps its cwd busy, so run it from the shared temp dir rather than a private one.
        with mock.patch.object(process, "_kill_tree"), mock.patch.object(process, "OUTPUT_GRACE_SECONDS", 1.0):
            started = time.monotonic()
            pid_file = Path(tempfile.gettempdir()) / f"delegate-orphan-{os.getpid()}.pid"
            self.addCleanup(pid_file.unlink, missing_ok=True)
            result = process.run_process(PYTHON, ["-c", ORPHANING_PARENT, str(pid_file)],
                                         cwd=tempfile.gettempdir(), timeout_seconds=1)
            elapsed = time.monotonic() - started
        self.child_pid = int(pid_file.read_text(encoding="utf-8"))
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 15)

    def test_output_wait_does_not_block_on_a_pipe_held_by_an_escaped_child(self):
        # The child survives the kill and keeps the inherited pipes open; the reader threads stay blocked in
        # read(), so the run must give up after the grace period without closing a stream under them.
        pid_file = Path(tempfile.gettempdir()) / f"delegate-escaped-{os.getpid()}.pid"
        self.addCleanup(pid_file.unlink, missing_ok=True)
        with mock.patch.object(process, "_kill_tree"), mock.patch.object(process, "OUTPUT_GRACE_SECONDS", 1.0):
            started = time.monotonic()
            result = process.run_process(PYTHON, ["-c", ORPHANING_PARENT, str(pid_file)],
                                         cwd=tempfile.gettempdir(), timeout_seconds=1)
            elapsed = time.monotonic() - started
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        self.addCleanup(kill_pid, child_pid)
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 1 + 1.0 + 5 + 3)  # timeout + grace + leader wait + slack
        self.assertEqual(result.stdout.split()[0], str(child_pid))  # what was read so far is kept

    def test_normal_run_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = process.run_process(PYTHON, ["-c", "print('ok')"], cwd=tmp, timeout_seconds=30)
        self.assertEqual((result.exit_code, result.stdout.strip(), result.timed_out, result.cancelled),
                         (0, "ok", False, False))

    def test_live_leader_and_its_child_die_on_timeout(self):
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(120)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = process.run_process(PYTHON, ["-c", script], cwd=tmp, timeout_seconds=2)
        self.child_pid = int(result.stdout.split()[0])
        self.assertTrue(result.timed_out)
        self.assertTrue(wait_gone(self.child_pid))


class BoundedOutput(unittest.TestCase):
    def test_a_noisy_child_is_read_to_the_end_but_only_its_head_and_tail_are_kept(self):
        script = "import sys\nprint('FIRST')\nfor n in range(2000):\n    print('line %d ' % n + 'z' * 40)\nprint('LAST')\n"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(process, "OUTPUT_HEAD_CHARS", 300), \
                mock.patch.object(process, "OUTPUT_TAIL_CHARS", 500):
            result = process.run_process(PYTHON, ["-c", script], cwd=tmp, timeout_seconds=60)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.output_truncated)
        self.assertTrue(result.stdout.startswith("FIRST\nline 0 "))
        self.assertTrue(result.stdout.rstrip().endswith("LAST"))
        self.assertIn("characters omitted by the bridge", result.stdout)
        self.assertLess(len(result.stdout), 1000)

    def test_every_stdout_line_reaches_the_callback_even_when_the_stored_middle_is_dropped(self):
        script = ("import sys\nprint('FIRST')\nfor n in range(2000):\n    print('line %d ' % n + 'z' * 40)\n"
                  "sys.stdout.write('LAST-NO-NEWLINE')\n")
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(process, "OUTPUT_HEAD_CHARS", 300), \
                mock.patch.object(process, "OUTPUT_TAIL_CHARS", 500):
            result = process.run_process(PYTHON, ["-c", script], cwd=tmp, timeout_seconds=60,
                                         on_stdout_line=seen.append)
        self.assertTrue(result.output_truncated)
        self.assertNotIn("line 1000 ", result.stdout)
        self.assertEqual(seen[0], "FIRST")
        self.assertEqual(seen[1:-1], [f"line {n} " + "z" * 40 for n in range(2000)])
        self.assertEqual(seen[-1], "LAST-NO-NEWLINE")
        self.assertEqual(result.lines_skipped, 0)

    def test_a_line_over_the_limit_and_a_failing_callback_are_counted_not_fatal(self):
        script = "print('a')\nprint('b' * 5000)\nprint('c')\n"
        seen: list[str] = []

        def callback(line: str) -> None:
            if line == "c":
                raise RuntimeError("consumer bug")
            seen.append(line)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(process, "MAX_LINE_CHARS", 1000):
            result = process.run_process(PYTHON, ["-c", script], cwd=tmp, timeout_seconds=60, on_stdout_line=callback)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(seen, ["a"])
        self.assertEqual(result.lines_skipped, 2)
        self.assertIn("c\n", result.stdout)

    def test_a_trailing_partial_line_is_counted_when_the_pipe_is_closed_under_the_reader(self):
        class BrokenPipe:
            def __init__(self):
                self.chunks = [b"whole\npart"]

            def read(self, size):
                if self.chunks:
                    return self.chunks.pop(0)
                raise OSError("pipe closed")

            def close(self):
                pass

        seen: list[str] = []
        capture = process._BoundedCapture(BrokenPipe(), seen.append)
        capture.read_all()
        self.assertEqual(seen, ["whole"])
        self.assertEqual(capture.lines_skipped, 1)

    def test_a_line_arriving_after_the_run_returned_is_not_delivered_and_is_counted(self):
        class Slow:
            def __init__(self):
                self.release = threading.Event()
                self.step = 0

            def read(self, size):
                self.step += 1
                if self.step == 1:
                    return b"early\n"
                if self.step == 2:
                    self.release.wait(10)
                    return b"late\n"
                return b""

            def close(self):
                pass

        stream = Slow()
        seen: list[str] = []
        capture = process._BoundedCapture(stream, seen.append)
        reader = threading.Thread(target=capture.read_all, daemon=True)
        reader.start()
        for _ in range(500):
            if seen:
                break
            time.sleep(0.01)
        capture.close()
        self.assertTrue(reader.is_alive())  # what run_process reports as stdout_abandoned
        stream.release.set()
        reader.join(5)
        self.assertEqual(seen, ["early"])
        self.assertEqual(capture.lines_skipped, 1)

    def test_close_does_not_wait_forever_for_a_stuck_callback(self):
        started, release = threading.Event(), threading.Event()

        def stuck(line):
            started.set()
            release.wait(10)

        class One:
            def __init__(self):
                self.sent = False

            def read(self, size):
                if not self.sent:
                    self.sent = True
                    return b"line\n"
                return b""

            def close(self):
                pass

        capture = process._BoundedCapture(One(), stuck)
        reader = threading.Thread(target=capture.read_all, daemon=True)
        reader.start()
        self.assertTrue(started.wait(5))
        began = time.monotonic()
        self.assertFalse(capture.close(timeout=0.2))
        self.assertLess(time.monotonic() - began, 2)
        release.set()
        reader.join(5)

    def test_a_clean_run_is_not_marked_abandoned(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = process.run_process(PYTHON, ["-c", "print('x')"], cwd=tmp, timeout_seconds=30,
                                         on_stdout_line=lambda line: None)
        self.assertFalse(result.stdout_abandoned)

    def test_output_below_the_limit_is_complete_and_not_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = process.run_process(PYTHON, ["-c", "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\r\\n')"], cwd=tmp,
                                         timeout_seconds=30)
        self.assertEqual((result.stdout, result.output_truncated), ("a\nb\n", False))

    def test_a_long_prompt_reaches_the_child_and_the_child_output_comes_back(self):
        prompt = "x" * 300_000
        with tempfile.TemporaryDirectory() as tmp:
            result = process.run_process(PYTHON, ["-c", "import sys; print(len(sys.stdin.read()))"], cwd=tmp,
                                         timeout_seconds=60, stdin_text=prompt)
        self.assertEqual(result.stdout.strip(), "300000")

    @unittest.skipUnless(NT, "the suspended start is Windows-only")
    def test_a_child_that_cannot_be_resumed_is_killed_and_its_pipes_are_closed(self):
        started = []
        real = subprocess.Popen

        def spy(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]

        with mock.patch.object(process, "_resume_process", return_value=False), \
                mock.patch.object(process.subprocess, "Popen", side_effect=spy), self.assertRaises(OSError):
            process.start_process([str(PYTHON), "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, stdin=subprocess.PIPE)
        child = started[0]
        self.assertIsNotNone(child.poll(), "the child must be gone, not left suspended")
        self.assertTrue(child.stdout.closed and child.stderr.closed and child.stdin.closed)


if __name__ == "__main__":
    unittest.main()
