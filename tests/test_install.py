"""Fresh-profile installation tests: the installer copies only the skill, and the installed copy runs offline."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "Do you want Codex auto-review (Approve for me) enabled or disabled?"


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "new-user"
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("CLAUDE", "ANTHROPIC", "CODEX", "PYTHONPATH", "DELEGATE_TO_CODEX"))}
        env.update(USERPROFILE=str(self.home), HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.home / ".claude"),
                   CODEX_HOME=str(self.home / ".codex"), PYTHONDONTWRITEBYTECODE="1")
        env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), str(Path(shutil.which("git")).parent),
                                      str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")])
        self.env = env

    def tearDown(self):
        self.tmp.cleanup()

    def run_py(self, *args, expected=0):
        # An empty pipe as stdin: not a terminal, so the installer never prompts (the NUL device counts as one on Windows).
        result = subprocess.run([sys.executable, "-B", *map(str, args)], env=self.env, cwd=self.root,
                                capture_output=True, text=True, timeout=120, input="")
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_installs_into_claude_skills_by_default(self):
        out = json.loads(self.run_py(ROOT / "install.py").stdout)
        self.assertEqual(out["status"], "installed")
        skill = self.home / ".claude" / "skills" / "delegate-to-codex"
        self.assertEqual(Path(out["skill"]), skill)
        for rel in ("SKILL.md", "README.md", "LICENSE.txt", "PLATFORMS.md", "scripts/codex_bridge.py",
                    "scripts/setup.py", "schemas/task.schema.json", "schemas/reply.schema.json",
                    "assets/task.template.json", "references/setup.md", "references/results.md",
                    "references/safety.md", "references/task-file.md", "references/codex-workflow.md", "references/example.md",
                    "references/routing-policy.md", "src/claude_codex_bridge/bridge.py",
                    "src/claude_codex_bridge/settings.py"):
            self.assertTrue((skill / rel).is_file(), rel)
        installed = {p.relative_to(skill).as_posix() for p in skill.rglob("*") if p.is_file()}
        self.assertFalse([p for p in installed if p.startswith("tests/") or "__pycache__" in p], installed)
        self.assertEqual(out["files"], len(installed))

    def test_installed_copy_is_the_skill_without_tests(self):
        out = json.loads(self.run_py(ROOT / "install.py", "--destination", self.root / "skill").stdout)
        source = ROOT / "skills" / "delegate-to-codex"
        expected = {p.relative_to(source).as_posix() for p in source.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.relative_to(source).parts[0] != "tests"}
        installed = {p.relative_to(self.root / "skill").as_posix() for p in (self.root / "skill").rglob("*")
                     if p.is_file()}
        self.assertEqual(installed, expected)
        self.assertEqual(out["files"], len(expected))
        self.assertFalse([p for p in installed if any(word in p.lower() for word in ("grok", "handoff", "checkpoint"))])

    def test_refuses_to_overwrite(self):
        target = self.root / "existing"
        target.mkdir()
        out = json.loads(self.run_py(ROOT / "install.py", "--destination", target, expected=2).stdout)
        self.assertEqual(out["status"], "setup_required")
        self.assertEqual(list(target.iterdir()), [])

    def test_installed_copy_self_test_runs_offline(self):
        self.run_py(ROOT / "install.py", "--destination", self.root / "skill")
        result = self.run_py(self.root / "skill" / "scripts" / "setup.py", "self-test")
        self.assertIn("pass", result.stdout.lower())


class Terminal(io.StringIO):
    """Standard input that is (or is not) a terminal, with scripted answers."""

    def __init__(self, text="", tty=True):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class AutoReviewChoice(unittest.TestCase):
    """The installer asks the auto-review question once, or takes the answer from --auto-review."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        home = self.root / "new-user"
        home.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("CLAUDE", "ANTHROPIC", "CODEX", "PYTHONPATH", "DELEGATE_TO_CODEX"))}
        env.update(USERPROFILE=str(home), HOME=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"),
                   CODEX_HOME=str(home / ".codex"), PYTHONDONTWRITEBYTECODE="1")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings_file = home / ".claude" / "delegate-to-codex-state" / "default" / "settings.json"
        spec = importlib.util.spec_from_file_location("installer_under_test", ROOT / "install.py")
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)

    def install(self, *args, stdin=None):
        """Run the installer in this process; returns (exit code, the JSON it printed, what it said on stderr)."""
        out, err = io.StringIO(), io.StringIO()
        destination = self.root / f"skill-{len(list(self.root.glob('skill-*')))}"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.installer.main(["--destination", str(destination), *args], stdin=stdin)
        self.destination = destination
        return code, json.loads(out.getvalue()), err.getvalue()

    def stored(self):
        return json.loads(self.settings_file.read_text(encoding="utf-8"))

    def bridge_state(self):
        done = subprocess.run([sys.executable, "-B", str(self.destination / "scripts" / "codex_bridge.py"),
                               "settings", "show"], capture_output=True, text=True, timeout=120, input="")
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["state"]["auto_review"]

    def test_yes_on_a_terminal_enables_it_through_the_installed_bridge(self):
        code, out, err = self.install(stdin=Terminal("y\n"))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "installed")
        self.assertEqual(out["auto_review"], {"status": "recorded", "choice": "on", "source": "prompt"})
        self.assertIn(QUESTION, err)
        self.assertEqual(self.stored(), {"schema_version": 1, "auto_review": True})
        self.assertEqual(self.bridge_state(), "on")

    def test_no_on_a_terminal_disables_it(self):
        for answer in ("n", "No", " NO "):
            with self.subTest(answer=answer):
                code, out, _ = self.install(stdin=Terminal(answer + "\n"))
                self.assertEqual(code, 0)
                self.assertEqual(out["auto_review"]["choice"], "off")
                self.assertEqual(self.stored()["auto_review"], False)
                self.assertEqual(self.bridge_state(), "off")

    def test_an_invalid_answer_asks_again_until_it_gets_a_valid_one(self):
        code, out, err = self.install(stdin=Terminal("maybe\n\n1\nYes\n"))
        self.assertEqual(code, 0)
        self.assertEqual(out["auto_review"]["choice"], "on")
        self.assertEqual(err.count("Please answer y (enabled) or n (disabled)."), 3)
        self.assertEqual(err.count(QUESTION), 4)

    def test_input_that_ends_without_an_answer_leaves_it_unset(self):
        for text in ("", "perhaps\n"):
            with self.subTest(text=text):
                code, out, _ = self.install(stdin=Terminal(text))
                self.assertEqual(code, 0)
                self.assertEqual(out["auto_review"]["status"], "unset")
                self.assertFalse(self.settings_file.exists())
                self.assertEqual(self.bridge_state(), "unset")

    def test_without_a_terminal_and_without_the_flag_nothing_is_asked_or_recorded(self):
        code, out, err = self.install(stdin=Terminal("y\n", tty=False))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "installed")
        self.assertEqual(out["auto_review"]["status"], "unset")
        self.assertIn("first run will ask", out["auto_review"]["note"])
        self.assertEqual(err, "")
        self.assertFalse(self.settings_file.exists())

    def test_the_flag_records_the_choice_without_asking_even_on_a_terminal(self):
        for word, state in (("on", True), ("true", True), ("enabled", True), ("off", False), ("false", False),
                            ("disabled", False), ("OFF", False)):
            with self.subTest(word=word):
                terminal = Terminal("n\n" if state else "y\n")
                code, out, err = self.install("--auto-review", word, stdin=terminal)
                self.assertEqual(code, 0)
                self.assertEqual(out["auto_review"], {"status": "recorded", "choice": "on" if state else "off",
                                                      "source": "flag"})
                self.assertEqual(err, "")
                self.assertEqual(terminal.read(), "n\n" if state else "y\n")  # nothing was read
                self.assertEqual(self.stored()["auto_review"], state)

    def test_the_flag_works_from_the_command_line_with_no_terminal(self):
        env = dict(os.environ)
        done = subprocess.run([sys.executable, "-B", str(ROOT / "install.py"), "--destination", str(self.root / "cli"),
                               "--auto-review", "on"], env=env, capture_output=True, text=True, timeout=120, input="")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)["auto_review"]["status"], "recorded")
        self.assertEqual(self.stored()["auto_review"], True)

    def test_an_unknown_flag_value_is_refused_before_anything_is_installed(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
            self.installer.main(["--destination", str(self.root / "never"), "--auto-review", "maybe"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("use on, off, true, false, enabled or disabled", err.getvalue())
        self.assertFalse((self.root / "never").exists())

    def test_a_failed_recording_is_reported_and_the_install_stays(self):
        failure = subprocess.CompletedProcess([], 2, "", '{"status":"failed","error":"state is not writable"}')
        with mock.patch.object(self.installer.subprocess, "run", return_value=failure):
            code, out, _ = self.install("--auto-review", "on")
        self.assertEqual(code, 2)
        self.assertEqual(out["status"], "installed")
        self.assertEqual(out["auto_review"]["status"], "failed")
        self.assertIn("state is not writable", out["auto_review"]["error"])
        self.assertTrue((self.destination / "SKILL.md").is_file())

    def test_a_refused_install_asks_nothing(self):
        existing = self.root / "existing"
        existing.mkdir()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.installer.main(["--destination", str(existing)], stdin=Terminal("y\n"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "setup_required")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
