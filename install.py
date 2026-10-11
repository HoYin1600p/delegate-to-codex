"""Install the delegate-to-codex skill's distributable files into Claude Code. Never copies account state."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "skills" / "delegate-to-codex"
SCRIPTS = {"codex_bridge.py", "setup.py", "bridge_report.py", "task_lint.py", "task_template.py", "land.py"}
SCHEMAS = {"task.schema.json", "reply.schema.json"}
# Top-level files travel with the skill: the licence notice and the readme that SKILL.md and the licence refer to.
ROOT_FILES = {"SKILL.md", "PLATFORMS.md", "README.md", "LICENSE.txt"}


QUESTION = "Do you want Codex auto-review (Approve for me) enabled or disabled?"
EXPLANATION = ("Auto-review sends a worker's requests to go beyond its sandbox to a reviewer model instead of refusing "
               "them. It does not widen the sandbox; the bridge's own checks and your review still apply. Details: "
               "references/safety.md. You can change this later with the bridge's settings command.")
# What a person may type at the prompt, and what the bridge's settings command takes for each.
ANSWERS = {"y": "on", "yes": "on", "enabled": "on", "n": "off", "no": "off", "disabled": "off"}
FLAG_VALUES = {"on": "on", "true": "on", "enabled": "on", "off": "off", "false": "off", "disabled": "off"}


def default_destination() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config) if config else Path.home() / ".claude"
    return base / "skills" / "delegate-to-codex"


def package_files():
    for path in sorted(SOURCE.rglob("*")):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("The skill package must not contain symbolic links or junctions")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SOURCE)
        parts = rel.parts
        allowed = (
            rel.as_posix() in {*ROOT_FILES, "assets/task.template.json"}
            or (len(parts) == 2 and parts[0] == "references" and path.suffix == ".md")
            or (len(parts) == 2 and parts[0] == "scripts" and path.name in SCRIPTS)
            or (len(parts) == 2 and parts[0] == "schemas" and path.name in SCHEMAS)
            or (len(parts) == 3 and parts[:2] == ("src", "claude_codex_bridge") and path.suffix == ".py")
        )
        if allowed:
            yield path, rel


def install(destination: Path) -> dict:
    if sys.version_info < (3, 11):
        raise ValueError("Install Python 3.11 or later, then rerun this installer")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists; keep it, or choose another --destination to review a new copy")
    required = [*(SOURCE / p for p in ROOT_FILES), *(SOURCE / "scripts" / p for p in SCRIPTS),
                *(SOURCE / "schemas" / p for p in SCHEMAS), SOURCE / "src/claude_codex_bridge/bridge.py"]
    if not all(p.is_file() for p in required):
        raise ValueError("Incomplete download: clone or download the whole repository before installing")
    files = list(package_files())
    destination.mkdir(parents=True)
    for source, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return {"status": "installed", "skill": str(destination), "files": len(files),
            "next": "Restart Claude Code, then ask it to use the delegate-to-codex skill and run its first-use setup."}


def flag_value(text: str) -> str:
    folded = text.strip().lower()
    if folded not in FLAG_VALUES:
        raise argparse.ArgumentTypeError("use on, off, true, false, enabled or disabled")
    return FLAG_VALUES[folded]


def ask_auto_review(stdin) -> str | None:
    """Ask the question on a terminal until the answer is y or n; None when the input ends without one.

    The text goes to stderr so that stdout stays one JSON document.
    """
    print(f"\n{EXPLANATION}", file=sys.stderr)
    while True:
        sys.stderr.write(f"{QUESTION} [y = enabled, n = disabled]: ")
        sys.stderr.flush()
        line = stdin.readline()
        if not line:
            print("", file=sys.stderr)
            return None
        answer = ANSWERS.get(line.strip().lower())
        if answer:
            return answer
        print("Please answer y (enabled) or n (disabled).", file=sys.stderr)


def record_auto_review(destination: Path, choice: str) -> dict:
    """Record the choice through the installed bridge's own settings command (the one place that writes the file)."""
    launcher = destination / "scripts" / "codex_bridge.py"
    command = [sys.executable, "-B", str(launcher), "settings", "set", "auto-review", choice]
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "choice": choice, "error": str(exc)}
    if done.returncode:
        return {"status": "failed", "choice": choice, "error": (done.stderr or done.stdout).strip()[:500]}
    return {"status": "recorded", "choice": choice}


def choose_auto_review(flag: str | None, stdin) -> tuple[str | None, str]:
    """The choice to record and where it came from: the flag, a prompt on a terminal, or nothing (left unset)."""
    if flag is not None:
        return flag, "flag"
    interactive = False
    try:
        interactive = bool(stdin and stdin.isatty())
    except (AttributeError, ValueError):
        pass
    if not interactive:
        return None, "not interactive"
    answer = ask_auto_review(stdin)
    return (answer, "prompt") if answer else (None, "no answer")


def main(argv: list[str] | None = None, *, stdin=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=default_destination(),
                        help="skill folder to create (default: ~/.claude/skills/delegate-to-codex)")
    parser.add_argument("--auto-review", type=flag_value, metavar="on|off",
                        help="record the auto-review choice without asking (on, off, true, false, enabled, disabled)")
    args = parser.parse_args(argv)
    try:
        record = install(args.destination)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "setup_required", "message": str(exc)}))
        return 2
    choice, source = choose_auto_review(args.auto_review, sys.stdin if stdin is None else stdin)
    if choice is None:
        record["auto_review"] = {"status": "unset", "reason": source,
                                 "note": "The first run will ask the question and record the answer."}
    else:
        record["auto_review"] = {**record_auto_review(args.destination.expanduser().absolute(), choice),
                                 "source": source}
    print(json.dumps(record, indent=2))
    return 2 if record["auto_review"]["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
