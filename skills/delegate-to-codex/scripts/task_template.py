"""task-template: write a starter task.json from arguments plus optional per-project defaults.

Project-specific text (locked decisions, standing acceptance lines, stop conditions, usual validation command)
lives in a defaults JSON file kept with the project, passed with --defaults. The defaults file may hold any
task field; list fields from the file come first and the arguments are appended, scalar arguments override.

    python -B scripts/task_template.py --id my-task --mode implement --objective "..." \
        --allowed src/a.py tests/ --acceptance "observable result" --defaults project-defaults.json \
        --out task.json --validation python -B -m unittest     # --validation takes the rest of the line

The new file is linted afterwards (warnings go to stderr). An existing file is kept unless --force is given.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import task_lint  # noqa: E402

LIST_FIELDS = ("allowed_changed_paths", "context_paths", "forbidden_context", "acceptance_criteria",
               "locked_decisions", "stop_conditions", "copy_ignored")
SCALAR_FIELDS = ("risk", "max_turns", "timeout_seconds", "auto_continue", "validation_timeout_seconds", "model",
                 "repo_root", "base_commit")
PLACEHOLDER = "TODO: describe one observable acceptance criterion"


def merged(defaults: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    task: dict[str, Any] = {k: v for k, v in defaults.items() if k not in LIST_FIELDS}
    for key in LIST_FIELDS:
        items = [*(defaults.get(key) or []), *(given.get(key) or [])]
        unique = list(dict.fromkeys(items))
        if unique:
            task[key] = unique
    for key, value in given.items():
        if key not in LIST_FIELDS and value is not None:
            task[key] = value
    return task


def build(args: argparse.Namespace, defaults: dict[str, Any]) -> dict[str, Any]:
    given: dict[str, Any] = {
        "task_id": args.id, "mode": args.mode, "objective": args.objective,
        "allowed_changed_paths": args.allowed, "context_paths": args.context,
        "forbidden_context": args.forbid, "acceptance_criteria": args.acceptance,
        "locked_decisions": args.locked, "stop_conditions": args.stop, "copy_ignored": args.copy_ignored,
        "risk": args.risk, "max_turns": args.max_turns, "timeout_seconds": args.timeout,
        "auto_continue": args.auto_continue, "repo_root": args.repo_root, "base_commit": args.base_commit,
        "model": args.model,
    }
    validation = json.loads(args.validation_json) if args.validation_json else (args.validation or None)
    if validation:
        given["validation_command"] = validation
    task = merged(defaults, given)
    if args.mode in task_lint.READ_ONLY:
        task["allowed_changed_paths"] = []
    task.setdefault("allowed_changed_paths", [])
    if not task.get("acceptance_criteria"):
        task["acceptance_criteria"] = [PLACEHOLDER]
    order = ["task_id", "repo_root", "base_commit", "mode", "objective", "context_paths", "forbidden_context",
             "allowed_changed_paths", "acceptance_criteria", "locked_decisions", "stop_conditions", "risk"]
    return {**{k: task[k] for k in order if k in task}, **{k: v for k, v in task.items() if k not in order}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-template", description=__doc__.split("\n\n")[0])
    parser.add_argument("--id", required=True, help="task_id (lowercase letters, digits, '.', '_', '-')")
    parser.add_argument("--mode", required=True, choices=sorted(["analyze", "implement", "review", "test"]))
    parser.add_argument("--objective", required=True)
    parser.add_argument("--allowed", nargs="*", default=[], metavar="PATH", help="allowed_changed_paths")
    parser.add_argument("--context", nargs="*", default=[], metavar="PATH", help="context_paths")
    parser.add_argument("--forbid", nargs="*", default=[], metavar="TEXT", help="forbidden_context")
    parser.add_argument("--acceptance", action="append", default=[], help="repeatable acceptance criterion")
    parser.add_argument("--locked", action="append", default=[], help="repeatable locked decision")
    parser.add_argument("--stop", action="append", default=[], help="repeatable stop condition")
    parser.add_argument("--copy-ignored", nargs="*", default=[], metavar="PATH")
    parser.add_argument("--risk", choices=["low", "medium", "high"])
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int, help="timeout_seconds")
    parser.add_argument("--auto-continue", type=int)
    parser.add_argument("--model")
    parser.add_argument("--repo-root")
    parser.add_argument("--base-commit")
    parser.add_argument("--defaults", type=Path, help="JSON file of reusable task fields for the project")
    parser.add_argument("--validation-json", help="validation_command as a JSON array")
    parser.add_argument("--out", type=Path, help="write here (default: print)")
    parser.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    parser.add_argument("--validation", nargs=argparse.REMAINDER,
                        help="validation_command: every argument after this option (put it last)")
    args = parser.parse_args(argv)
    defaults: dict[str, Any] = {}
    if args.defaults:
        try:
            defaults = json.loads(args.defaults.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"task-template: cannot read defaults {args.defaults}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(defaults, dict):
            print("task-template: the defaults file must hold a JSON object", file=sys.stderr)
            return 2
    task = build(args, defaults)
    text = json.dumps(task, indent=2) + "\n"
    if args.out:
        if args.out.exists() and not args.force:
            print(f"task-template: {args.out} exists; use --force to overwrite", file=sys.stderr)
            return 2
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"task-template: wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    findings = task_lint.lint(task, args.out or Path(args.id + ".json"))
    if findings:
        print(task_lint.render(findings), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
