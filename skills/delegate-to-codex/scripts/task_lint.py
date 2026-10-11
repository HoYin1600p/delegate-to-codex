"""task-lint: check a task file before ``check-task`` or ``run`` and explain how to fix what is wrong.

The bridge's own contract validation runs too; this adds explanations for mistakes that have happened and a few
warnings the contract cannot know about. Exit 0 when there are no errors, 1 on errors (or warnings with --strict).

    python -B scripts/task_lint.py <task.json> [--strict] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

READ_ONLY = {"analyze", "review"}
WILDCARDS = set("*?[]")
POWERSHELL = {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}
DOTTED_OPTION = re.compile(r"""(?<![\w"'`.])-[A-Za-z]\w*\.[\w.]*=""")
GRADLE_NAMES = {"gradle", "gradlew", "gradle.bat", "gradlew.bat"}
PLACEHOLDER = re.compile(r"\b(TODO|FIXME|XXX)\b")


class Finding(dict):
    def __init__(self, level: str, field: str, message: str, fix: str = "") -> None:
        super().__init__(level=level, field=field, message=message, fix=fix)


def is_absolute(value: str) -> bool:
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute() or value.startswith(("/", "\\"))


def lint_paths(raw: dict[str, Any], findings: list[Finding]) -> None:
    for key in ("allowed_changed_paths", "context_paths", "copy_ignored"):
        values = raw.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            findings.append(Finding("error", key, "must be a list of strings", f"write {key} as [\"path\", \"dir/**\"]"))
            continue
        for value in values:
            normalized = value.replace("\\", "/")
            if is_absolute(value) or re.match(r"^[A-Za-z]:", value):
                findings.append(Finding("error", key, f"{value!r} is an absolute path or has a drive letter",
                                        "use a path relative to the repository root, with forward slashes"))
            elif ":" in value:
                findings.append(Finding("error", key, f"{value!r} contains ':' (a drive letter or alternate data stream)",
                                        "remove the colon; paths are repository-relative"))
            if ".." in normalized.split("/"):
                findings.append(Finding("error", key, f"{value!r} leaves the repository with '..'",
                                        "name a path inside the repository"))
            if key != "copy_ignored" and any(c in normalized for c in WILDCARDS):
                tail_ok = normalized.endswith("/**") and not any(c in normalized[:-3] for c in WILDCARDS)
                if not tail_ok:
                    findings.append(Finding(
                        "error", key, f"{value!r} uses a wildcard other than a trailing '/**'",
                        "list exact files, or one directory as 'dir/**'; '*' in the middle of a path is not supported"))


def lint_numbers(raw: dict[str, Any], findings: list[Finding]) -> None:
    def integer(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    ranges = (("max_turns", 1, 12), ("auto_continue", 0, 3), ("timeout_seconds", 5, 7200),
              ("validation_timeout_seconds", 1, 7200))
    for key, low, high in ranges:
        if key in raw and not (integer(raw[key]) and low <= raw[key] <= high):
            findings.append(Finding("error", key, f"{raw[key]!r} is not an integer from {low} through {high}",
                                    f"set {key} between {low} and {high}"))


def lint_mode(raw: dict[str, Any], findings: list[Finding]) -> None:
    mode = raw.get("mode")
    if mode in READ_ONLY:
        if raw.get("allowed_changed_paths"):
            findings.append(Finding("error", "allowed_changed_paths", f"{mode} tasks are read-only",
                                    "set allowed_changed_paths to [] and name the files to read in context_paths"))
        if raw.get("copy_ignored"):
            findings.append(Finding("error", "copy_ignored", f"{mode} tasks cannot copy ignored files",
                                    "remove copy_ignored or use an implement or test task"))
        if not raw.get("context_paths"):
            findings.append(Finding("warning", "context_paths", "a read-only task defaults to no context",
                                    "name the files the worker should read first"))
    elif mode in {"implement", "test"} and not raw.get("validation_command"):
        findings.append(Finding("error", "validation_command", f"{mode} tasks need a validation command",
                                "add an argument array such as [\"python\", \"-B\", \"-m\", \"unittest\"]"))


def program_name(command: list[str]) -> str:
    return re.split(r"[\\/]", command[0])[-1].lower() if command else ""


def lint_validation(raw: dict[str, Any], findings: list[Finding]) -> None:
    command = raw.get("validation_command")
    if command is None:
        return
    if not isinstance(command, list) or not command or any(not isinstance(c, str) or not c for c in command):
        findings.append(Finding("error", "validation_command", "must be an array of non-empty strings, one per argument",
                                "write [\"program\", \"arg1\", \"arg2\"], not one command-line string"))
        return
    if len(command) > 24:
        findings.append(Finding("error", "validation_command", "has more than 24 arguments",
                                "move the long command into a wrapper script and run that"))
    lowered = [c.lower() for c in command]
    names = {re.split(r"[\\/]", c)[-1] for c in lowered}
    if program_name(command) in POWERSHELL:
        for argument in command[1:]:
            if DOTTED_OPTION.search(argument):
                findings.append(Finding(
                    "warning", "validation_command",
                    f"PowerShell splits an unquoted option with a dot, like '-Pa.b=c', at the dot ({argument[:60]!r})",
                    "quote it ('-Pa.b=c' inside single quotes in the -Command text), or use --% or a wrapper script"))
                break
    if names & GRADLE_NAMES or any("gradlew" in c or re.search(r"\bgradle\b", c) for c in lowered):
        if not any("--no-daemon" in c or "-dorg.gradle.daemon=false" in c for c in lowered):
            findings.append(Finding(
                "warning", "validation_command",
                "a Gradle command without --no-daemon can leave a daemon holding the bridge's output pipes",
                "add --no-daemon to the Gradle arguments"))


def lint_placeholders(raw: dict[str, Any], findings: list[Finding]) -> None:
    for key in ("objective", "acceptance_criteria", "locked_decisions", "stop_conditions"):
        value = raw.get(key)
        texts = value if isinstance(value, list) else [value]
        if any(isinstance(t, str) and PLACEHOLDER.search(t) for t in texts):
            findings.append(Finding("warning", key, "still holds a TODO placeholder", f"replace it before running"))


def contract_error(raw: dict[str, Any], path: Path) -> str | None:
    """The bridge's own validation. Missing inferable fields are filled with stand-ins so only real errors show."""
    from claude_codex_bridge.contracts import ContractError, validate_task
    candidate = dict(raw)
    candidate.setdefault("task_id", path.stem)
    if not isinstance(candidate.get("repo_root"), str):
        candidate["repo_root"] = str(Path.cwd())
    else:
        try:
            if not Path(candidate["repo_root"]).is_dir():
                return f"repo_root is not an existing directory: {candidate['repo_root']}"
        except OSError:
            pass
    candidate.setdefault("base_commit", "0" * 40)
    candidate.setdefault("context_paths", candidate.get("allowed_changed_paths"))
    try:
        validate_task(candidate)
    except ContractError as exc:
        return str(exc)
    except (OSError, ValueError) as exc:
        return str(exc)
    return None


def lint(raw: Any, path: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not isinstance(raw, dict):
        return [Finding("error", "(file)", "the task file must hold one JSON object", "")]
    lint_paths(raw, findings)
    lint_numbers(raw, findings)
    lint_mode(raw, findings)
    lint_validation(raw, findings)
    lint_placeholders(raw, findings)
    if not any(f["level"] == "error" for f in findings):
        message = contract_error(raw, path)
        if message:
            findings.append(Finding("error", "(contract)", message, "see references/task-file.md"))
    return findings


def render(findings: list[Finding]) -> str:
    if not findings:
        return "task-lint: ok"
    lines = []
    for item in findings:
        lines.append(f"{item['level'].upper():7} {item['field']}: {item['message']}")
        if item["fix"]:
            lines.append(f"        fix: {item['fix']}")
    errors = sum(f["level"] == "error" for f in findings)
    lines.append(f"task-lint: {errors} error(s), {len(findings) - errors} warning(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-lint", description=__doc__.split("\n\n")[0])
    parser.add_argument("task", type=Path)
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.task.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        print(f"task-lint: cannot read {args.task}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        findings = [Finding("error", "(file)", f"not valid JSON: {exc}", "fix the JSON syntax")]
    else:
        findings = lint(raw, args.task)
    print(json.dumps(findings, indent=2) if args.json else render(findings))
    failed = any(f["level"] == "error" or (args.strict and f["level"] == "warning") for f in findings)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
