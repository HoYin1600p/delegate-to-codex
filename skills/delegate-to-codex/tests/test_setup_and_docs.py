"""The setup script (doctor, self-test) and the documentation commands it promises.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SKILL
import claude_codex_bridge
from claude_codex_bridge import bridge


class DocsAreRunnable(unittest.TestCase):
    def read(self, relative):
        return (SKILL / relative).read_text(encoding="utf-8")

    def test_setup_docs_do_not_depend_on_bundled_tests(self):
        self.assertNotIn('"$SKILL/tests"', self.read("references/setup.md"))

    def test_readme_setup_commands_name_python(self):
        text = self.read("README.md")
        for command in ('python -B "$SKILL/scripts/setup.py" self-test', 'python -B "$SKILL/scripts/setup.py" doctor'):
            self.assertIn(command, text)

    def test_platform_guide_names_the_setup_gate(self):
        self.assertIn("scripts/setup.py", self.read("PLATFORMS.md").split("Platform gate", 1)[1][:200])

    def test_docs_and_sources_carry_no_build_history(self):
        forbidden = re.compile(r"\b(?:grok|wave [ab]|legacy|older release|older layouts?|historical names|"
                               r"pre-unbounded|as before)\b", re.IGNORECASE)
        files = [*SKILL.glob("*.md"), *(SKILL / "references").glob("*.md"), *(SKILL / "src").rglob("*.py"),
                 *(SKILL / "scripts").glob("*.py"), *(SKILL / "tests").glob("*.py"), *(SKILL / "schemas").glob("*.json")]
        offenders = {}
        for path in files:
            if path.name == "test_setup_and_docs.py":
                continue
            found = forbidden.findall(path.read_text(encoding="utf-8"))
            if found:
                offenders[path.relative_to(SKILL).as_posix()] = sorted(set(found))
        self.assertEqual(offenders, {})

    def test_skill_front_matter_names_author_version_and_keywords(self):
        header = self.read("SKILL.md").split("---")[1]
        for line in ("author: HoY", "version: 1.1.0", "permissions: [env, file_read, file_write, network, shell]"):
            self.assertIn(line, header)
        keywords = re.search(r"^keywords: \[(.*)\]$", header, re.MULTILINE)
        self.assertIsNotNone(keywords)
        for word in ("windows", "codex", "delegation", "git-worktree"):
            self.assertIn(word, keywords.group(1))

    def test_the_package_version_is_the_one_in_the_skill_front_matter(self):
        header = self.read("SKILL.md").split("---")[1]
        self.assertRegex(header, rf"(?m)^version: {re.escape(claude_codex_bridge.__version__)}$")

    def test_skill_md_has_no_overlong_lines_and_links_the_worked_example(self):
        text = self.read("SKILL.md").split("\n---\n", 1)[1]  # the front matter's description stays on one line
        self.assertEqual([n for n, line in enumerate(text.splitlines(), 1) if len(line) > 200], [])
        self.assertIn("references/example.md", text)
        self.assertTrue((SKILL / "references" / "example.md").is_file())
        for step in ("check-task", "run --task", "show-diff", "accept --task", "--expect-tree"):
            self.assertIn(step, self.read("references/example.md"))

    def test_the_documented_test_command_needs_no_environment_variable(self):
        self.assertIn("python -B -m unittest discover -s tests", self.read("references/setup.md"))
        for name in ("README.md", "SKILL.md", "references/setup.md"):
            self.assertNotIn("PYTHONPATH", self.read(name), name)


class SetupDoctorGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"DELEGATE_TO_CODEX_STATE_DIR": str(Path(self.tmp.name) / "state"),
                                             "CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "claude")})
        patch.start()
        self.addCleanup(patch.stop)
        spec = importlib.util.spec_from_file_location("bridge_setup_script", SKILL / "scripts" / "setup.py")
        self.setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.setup)
        self.record = {"version": "codex-test", "plan_type": "plus",
                       "usage": {"gate": "available", "five_hour": {}, "weekly": {}}}

    def doctor(self, system):
        with mock.patch.object(self.setup.platform, "system", return_value=system), \
                mock.patch.object(bridge, "preflight", return_value=self.record):
            return self.setup.doctor()

    def test_not_ready_when_the_platform_check_fails(self):
        result = self.doctor("Linux")
        self.assertEqual(result["status"], "setup_required")
        by_name = {c["check"]: c["status"] for c in result["checks"]}
        self.assertEqual(by_name["platform"], "setup_required")
        self.assertEqual(by_name["codex_subscription"], "ready")
        with mock.patch.object(self.setup, "doctor", return_value=result), \
                mock.patch.object(sys, "argv", ["setup.py", "doctor"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.setup.main(), 2)

    def test_ready_when_every_check_passes(self):
        if not self.setup.shutil.which("git"):
            self.skipTest("git is not installed")
        self.assertEqual(self.doctor("Windows")["status"], "codex_ready")

    def test_an_unset_auto_review_choice_is_a_setup_item_not_a_failure(self):
        for result in (self.setup.doctor(offline=True), self.doctor("Windows")):
            self.assertIn(result["status"], {"installation_ready", "codex_ready"})
            self.assertEqual(result["settings"]["auto_review"], "unset")
            item, = result["setup_items"]
            self.assertEqual((item["item"], item["status"]), ("auto_review", "unset"))
            self.assertEqual(item["question"], "Do you want Codex auto-review (Approve for me) enabled or disabled?")
            self.assertIn("settings set auto-review on", item["record_with"]["enabled"])
            self.assertNotIn("settings_file", {c["check"] for c in result["checks"]})
        with mock.patch.object(sys, "argv", ["setup.py", "doctor", "--offline"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.setup.main(), 0)

    def test_a_recorded_choice_is_reported_and_leaves_no_setup_item(self):
        from claude_codex_bridge import settings as user_settings
        for word, state in (("on", "on"), ("off", "off")):
            user_settings.set_value("auto-review", word)
            result = self.setup.doctor(offline=True)
            self.assertEqual(result["status"], "installation_ready")
            self.assertEqual(result["settings"]["auto_review"], state)
            self.assertEqual(result["setup_items"], [])

    def test_an_unreadable_settings_file_fails_its_own_check(self):
        from claude_codex_bridge import settings as user_settings
        user_settings.set_value("auto-review", "on")
        user_settings.settings_path().write_text("{broken", encoding="utf-8")
        result = self.setup.doctor(offline=True)
        by_name = {c["check"]: c for c in result["checks"]}
        self.assertEqual(by_name["settings_file"]["status"], "setup_required")
        self.assertIn("unreadable", by_name["settings_file"]["detail"])
        self.assertEqual(result["status"], "setup_required")
        self.assertEqual(result["settings"]["auto_review"], "unreadable")

    def test_doctor_lists_the_state_directories_it_finds(self):
        for result in (self.doctor("Windows"), self.setup.doctor(offline=True)):
            listed = result["state_directories"]
            self.assertEqual([item["path"] for item in listed], [str((Path(self.tmp.name) / "state").resolve())])
            self.assertEqual([item["active"] for item in listed], [True])


    def test_self_test_fixture_ignores_inherited_git_variables(self):
        if not self.setup.shutil.which("git"):
            self.skipTest("git is not installed")
        foreign_index = Path(self.tmp.name) / "foreign-index"
        foreign_dir = Path(self.tmp.name) / "foreign.git"
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(foreign_index), "GIT_DIR": str(foreign_dir)}):
            self.assertNotIn("GIT_INDEX_FILE", self.setup.scrubbed_environment())
            self.assertNotIn("git_dir", {key.lower() for key in self.setup.scrubbed_environment()})
            self.assertEqual(self.setup.self_test()["status"], "passed")
        self.assertFalse(foreign_index.exists())
        self.assertFalse(foreign_dir.exists())

    def test_docs_state_the_limits_the_code_has(self):
        read = lambda name: (SKILL / name).read_text(encoding="utf-8")
        safety, task, workflow = read("references/safety.md"), read("references/task-file.md"), read("references/codex-workflow.md")
        self.assertIn("at most 50", safety)
        self.assertIn("two example paths", safety)
        self.assertIn("manual `continue` and `revise` rounds are unbounded", task)
        self.assertIn("never gains it", workflow)
        self.assertIn("no session id", workflow)
        self.assertIn("returns nothing on macOS", read("PLATFORMS.md"))
        for name in ("README.md", "SKILL.md", "references/setup.md", "references/example.md"):
            self.assertNotIn("9f1c2b7e4a", read(name), name)
        self.assertNotIn("install.py --auto-review", read("README.md") + read("SKILL.md"))
        self.assertIn("not part of this skill folder", read("references/setup.md"))


if __name__ == "__main__":
    unittest.main()
