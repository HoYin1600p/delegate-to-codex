"""The task file contract: schema, field validation, paths and launch checks.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import copy
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import BridgeCase, SKILL
from claude_codex_bridge import bridge, cli, gitops
from claude_codex_bridge.contracts import ContractError, load_task, validate_task


def schema_accepts(schema, value, root=None):
    """Tiny JSON Schema checker for the keywords task.schema.json uses (stdlib only)."""
    root = root if root is not None else schema
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        if not schema_accepts(target, value, root):
            return False
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}

    def is_type(item, name):
        if name == "integer":
            return isinstance(item, int) and not isinstance(item, bool)
        return isinstance(item, kinds[name]) and not (name != "boolean" and isinstance(item, bool))

    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(is_type(value, n) for n in names):
            return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return False
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10 ** 9):
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10 ** 9):
            return False
        if "items" in schema and not all(schema_accepts(schema["items"], item, root) for item in value):
            return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        for key, item in value.items():
            if key in properties and not schema_accepts(properties[key], item, root):
                return False
    for part in schema.get("allOf", []):
        if "if" in part:
            if schema_accepts(part["if"], value, root) and not schema_accepts(part.get("then", {}), value, root):
                return False
        elif not schema_accepts(part, value, root):
            return False
    return True


class SchemaMatchesValidator(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((SKILL / "schemas" / "task.schema.json").read_text(encoding="utf-8"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = {
            "task_id": "contract-case", "repo_root": str(Path(self.tmp.name).resolve()), "base_commit": "0" * 40,
            "mode": "implement", "objective": "Do one thing", "context_paths": ["a.py"],
            "allowed_changed_paths": ["a.py"], "acceptance_criteria": ["It works"],
            "validation_command": ["python", "-V"],
        }

    def accepted_by_validator(self, raw):
        try:
            task = validate_task(copy.deepcopy(raw))
            gitops.validate_task_id(task.task_id)  # the bridge applies this on top of the contract pattern
        except (ContractError, gitops.BridgeError):
            return False
        return True

    def case(self, **changes):
        raw = copy.deepcopy(self.base)
        for key, value in changes.items():
            if value is KeyError:
                raw.pop(key, None)
            else:
                raw[key] = value
        return raw

    def test_schema_and_validator_agree(self):
        cases = {
            "valid implement": self.case(),
            "empty validation_command": self.case(validation_command=[]),
            "empty validation argument": self.case(validation_command=["python", ""]),
            "null validation implement": self.case(validation_command=None),
            "missing validation implement": self.case(validation_command=KeyError),
            "missing validation test": self.case(mode="test", validation_command=KeyError),
            "valid test no allowed paths": self.case(mode="test", allowed_changed_paths=[]),
            "empty model": self.case(model=""),
            "blank model": self.case(model="  "),
            "null model": self.case(model=None),
            "named model": self.case(model="gpt-x"),
            "blank objective": self.case(objective="  "),
            "blank list entry": self.case(locked_decisions=[" "]),
            "empty acceptance": self.case(acceptance_criteria=[]),
            "blank acceptance": self.case(acceptance_criteria=[" "]),
            "missing acceptance": self.case(acceptance_criteria=KeyError),
            "analyze with empty allowed": self.case(mode="analyze", allowed_changed_paths=[],
                                                    validation_command=KeyError),
            "analyze with allowed path": self.case(mode="analyze", validation_command=KeyError),
            "analyze missing allowed": self.case(mode="analyze", allowed_changed_paths=KeyError,
                                                 validation_command=KeyError),
            "review with empty allowed": self.case(mode="review", allowed_changed_paths=[],
                                                   validation_command=KeyError),
            "review with allowed path": self.case(mode="review"),
            "implement with no allowed path": self.case(allowed_changed_paths=[]),
            "mode list": self.case(mode=["implement"]),
            "mode object": self.case(mode={"a": 1}),
            "risk list": self.case(risk=["low"]),
            "risk object": self.case(risk={"a": 1}),
            "bad risk": self.case(risk="extreme"),
            "model with leading dash": self.case(model="-m"),
            "model with space": self.case(model="gpt x"),
            "colon in allowed path": self.case(allowed_changed_paths=["a.py:stream"]),
            "drive path in context": self.case(context_paths=["C:/x/a.py"]),
            ".git component": self.case(allowed_changed_paths=["sub/.git/config"]),
            "dotdot component": self.case(allowed_changed_paths=["../a.py"]),
            "absolute allowed path": self.case(allowed_changed_paths=["/a.py"]),
            "directory boundary": self.case(allowed_changed_paths=["src/**"]),
            "middle wildcard": self.case(allowed_changed_paths=["src/*.py"]),
            "copy_ignored glob": self.case(copy_ignored=["libs/*.jar"]),
            "copy_ignored colon": self.case(copy_ignored=["libs/a.jar:x"]),
            "auto_review true": self.case(auto_review=True),
            "auto_review false": self.case(auto_review=False),
            "auto_review null": self.case(auto_review=None),
            "auto_review string": self.case(auto_review="off"),
            "auto_review number": self.case(auto_review=0),
            "auto_review list": self.case(auto_review=[False]),
            "analyze with copy_ignored": self.case(mode="analyze", allowed_changed_paths=[],
                                                   validation_command=KeyError, copy_ignored=["libs"]),
            "review with copy_ignored": self.case(mode="review", allowed_changed_paths=[],
                                                  validation_command=KeyError, copy_ignored=["libs"]),
            "review with empty copy_ignored": self.case(mode="review", allowed_changed_paths=[],
                                                        validation_command=KeyError, copy_ignored=[]),
            "test with copy_ignored": self.case(mode="test", copy_ignored=["libs/*.jar"]),
            "copy_ignored dotdot": self.case(copy_ignored=["../libs"]),
            "copy_ignored git dir": self.case(copy_ignored=[".git/config"]),
            "copy_ignored absolute": self.case(copy_ignored=["/libs"]),
            "con id": self.case(task_id="con"),
            "con with extension id": self.case(task_id="con.txt"),
            "nul id": self.case(task_id="nul"),
            "aux with suffix id": self.case(task_id="aux.v2"),
            "prn id": self.case(task_id="prn"),
            "com1 id": self.case(task_id="com1"),
            "com9 extension id": self.case(task_id="com9.log"),
            "lpt3 id": self.case(task_id="lpt3"),
            "com0 id is ordinary": self.case(task_id="com0"),
            "console id is ordinary": self.case(task_id="console"),
            "device name after a dot is ordinary": self.case(task_id="my.con"),
            "device prefix with dash is ordinary": self.case(task_id="nul-test"),
            "dotdot id": self.case(task_id="foo..bar"),
            "single dots id": self.case(task_id="foo.bar.baz"),
            "uppercase device id": self.case(task_id="CON"),
            "model with C1 control": self.case(model="gpt\x85x"),
            "model starting with C1 control": self.case(model="\x80gpt"),
            "model with last C1 control": self.case(model="gpt\x9fx"),
            "model with no-break space": self.case(model="gpt\u00a0x"),
            "model with accented letter": self.case(model="gpt-\u00e9"),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.assertEqual(schema_accepts(self.schema, raw), self.accepted_by_validator(raw))

    def test_plan_status_default_and_shared_path_definitions(self):
        self.assertEqual(self.schema["properties"]["plan_status"]["default"], "READY")
        for key in ("context_paths", "allowed_changed_paths"):
            self.assertEqual(self.schema["properties"][key]["items"], {"$ref": "#/$defs/repoPath"})
        self.assertEqual(self.schema["properties"]["copy_ignored"]["items"], {"$ref": "#/$defs/ignoredPath"})
        self.assertTrue(schema_accepts(self.schema, self.case(plan_status="READY")))
        self.assertFalse(schema_accepts(self.schema, self.case(plan_status="DRAFT")))

    def test_expected_verdicts(self):
        self.assertTrue(self.accepted_by_validator(self.case()))
        for changes in ({"acceptance_criteria": []}, {"validation_command": KeyError}, {"validation_command": None},
                        {"mode": "analyze"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.accepted_by_validator(self.case(**changes)))
        read_only = self.case(mode="review", allowed_changed_paths=[], validation_command=KeyError)
        self.assertTrue(self.accepted_by_validator(read_only))

    def test_unusual_mode_and_risk_values_give_contract_errors(self):
        for key, value in (("mode", ["implement"]), ("mode", {"a": 1}), ("risk", ["low"]), ("risk", {"a": 1})):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ContractError):
                    validate_task(self.case(**{key: value}))

    def test_non_string_repo_root_without_base_commit_is_a_contract_error(self):
        path = Path(self.tmp.name) / "task.json"
        for value in (["x"], {"a": 1}, 7):
            path.write_text(json.dumps({"mode": "analyze", "repo_root": value}), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "repo_root"):
                load_task(path)

    def test_template_validates_and_its_command_finds_unpackaged_tests(self):
        template = json.loads((SKILL / "assets" / "task.template.json").read_text(encoding="utf-8"))
        self.assertTrue(schema_accepts(self.schema, template))
        template.update(task_id="template", repo_root=self.base["repo_root"], base_commit="0" * 40,
                        context_paths=template["allowed_changed_paths"])
        task = validate_task(template)
        self.assertIn("-s", task.validation_command)
        tests = Path(self.tmp.name) / "tests"
        tests.mkdir()
        (tests / "test_module.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_it(self):\n        pass\n", encoding="utf-8")
        command = [sys.executable if part == "python" else part for part in task.validation_command]
        result = subprocess.run(command, cwd=self.tmp.name, capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)


class ContractPaths(BridgeCase):
    def test_every_component_is_checked_for_colons(self):
        for bad in ("src/a.txt:stream", "src/C:x", "a/b:c/d", "C:/x"):
            for key in ("allowed_changed_paths", "context_paths"):
                with self.subTest(path=bad, key=key), self.assertRaises(ContractError):
                    load_task(self.task(**{key: [bad]}))

    def test_git_directories_are_not_allowed_paths(self):
        for bad in (".git", ".git/hooks/pre-commit", "src/.git/config", ".GIT/x", "src/.git./x", "git~1/x"):
            for key in ("allowed_changed_paths", "context_paths"):
                with self.subTest(path=bad, key=key), self.assertRaises(ContractError):
                    load_task(self.task(**{key: [bad]}))
        ok = load_task(self.task(allowed_changed_paths=[".github/**", "src/.gitkeep", "docs/git.md"]))
        self.assertEqual(ok.allowed_changed_paths, (".github/**", "src/.gitkeep", "docs/git.md"))

    def test_task_inference_runs_git_through_the_bridges_resolver(self):
        value = json.loads(self.task().read_text(encoding="utf-8"))
        value.pop("base_commit")
        value.pop("repo_root")
        path = self.tmp / "inferred.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        calls = []
        real = subprocess.run

        def spy(argv, **kwargs):
            calls.append(list(argv))
            return real(argv, **kwargs)

        with mock.patch.object(Path, "cwd", return_value=self.repo), \
                mock.patch.object(gitops.subprocess, "run", side_effect=spy):
            task = load_task(path)
        self.assertEqual(task.base_commit, self.base)
        self.assertTrue(calls)
        self.assertTrue(all(Path(call[0]).is_absolute() for call in calls), calls)

    def test_setup_script_uses_the_same_git_resolver(self):
        spec = importlib.util.spec_from_file_location("bridge_setup_script_b", SKILL / "scripts" / "setup.py")
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        self.assertTrue(Path(setup.git_path()).is_absolute())
        commands = []
        real = setup.run

        def spy(args, **kwargs):
            commands.append(list(args))
            return real(args, **kwargs)

        with mock.patch.object(setup, "run", side_effect=spy):
            self.assertEqual(setup.self_test()["status"], "passed")
        self.assertTrue(any("git" in Path(c[0]).stem.lower() for c in commands))
        self.assertTrue(all(Path(c[0]).is_absolute() for c in commands), commands)


class LaunchChecks(BridgeCase):
    def test_check_task_rejects_ids_that_cannot_make_a_branch_or_a_lock(self):
        for bad in ("v1..2", "nul", "con.json"):
            with self.subTest(task_id=bad):
                path = self.task(task_id=bad)
                with self.assertRaises(bridge.BridgeError):
                    bridge.check_task(path)
                with self.assertRaises(bridge.BridgeError):
                    bridge.run(path)
        self.assertFalse(self.log.exists(), "nothing may reach Codex")
        self.assertEqual(self.artifacts("v1..2"), [])

    def test_a_typo_in_the_validation_command_fails_before_a_worker_run(self):
        task = self.task(validation_command=["pythn-typo", "-m", "unittest"])
        with self.assertRaisesRegex(bridge.BridgeError, "validation executable not found"):
            bridge.check_task(task)
        with self.assertRaisesRegex(bridge.BridgeError, "validation executable not found"):
            bridge.run(task)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.artifacts(), [])
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.main(["check-task", "--task", str(task)]), 2)
        self.assertIn("pythn-typo", json.loads(err.getvalue())["error"])

    def test_the_resolved_validation_program_is_reported(self):
        ready = bridge.check_task(self.task(validation_command=[sys.executable, "-m", "unittest"]))
        self.assertEqual(Path(ready["validation_executable"]), Path(sys.executable))
        bare = bridge.check_task(self.task(validation_command=[Path(sys.executable).name, "-m", "unittest"]))
        self.assertTrue(Path(bare["validation_executable"]).is_absolute())

    def test_a_repository_program_must_exist_or_be_creatable(self):
        with self.assertRaisesRegex(bridge.BridgeError, "not in the repository"):
            bridge.check_task(self.task(validation_command=["scripts/missing.sh"]))
        existing = bridge.check_task(self.task(validation_command=["./README.md"]))
        self.assertEqual(existing["status"], "ready")
        creatable = bridge.check_task(self.task(validation_command=["./hello.py"]))
        self.assertEqual(creatable["status"], "ready")

    def test_py_launcher_selectors_keep_their_place(self):
        va = bridge._validation_arguments
        self.assertEqual(va(Path("py.exe"), ["-3.12", "-m", "unittest"]), ["-3.12", "-B", "-m", "unittest"])
        self.assertEqual(va(Path("PY"), ["-3", "-c", "pass"]), ["-3", "-B", "-c", "pass"])
        self.assertEqual(va(Path("py"), ["-V:3.12", "-m", "unittest"]), ["-V:3.12", "-B", "-m", "unittest"])
        self.assertEqual(va(Path("py"), ["-3.12-64", "-u", "t.py"]), ["-3.12-64", "-B", "-u", "t.py"])
        self.assertEqual(va(Path("py"), ["-3.12", "-B", "-m", "unittest"]), ["-3.12", "-B", "-m", "unittest"])
        self.assertEqual(va(Path("py"), ["-m", "unittest"]), ["-B", "-m", "unittest"])
        self.assertEqual(va(Path("python.exe"), ["-m", "unittest"]), ["-B", "-m", "unittest"])

    def test_a_model_that_looks_like_an_option_is_refused_everywhere(self):
        for bad in ("-x", "--model", "-", "a b"):
            with self.subTest(model=bad):
                with self.assertRaises(ContractError):
                    load_task(self.task(model=bad))
        self.assertEqual(load_task(self.task(model="gpt-6.1-sol")).model, "gpt-6.1-sol")
        with self.assertRaises(bridge.BridgeError):
            bridge._settings("-x", None, load_task(self.task()))


if __name__ == "__main__":
    unittest.main()
