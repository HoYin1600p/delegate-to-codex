"""bridge-report: a compact human summary of a bridge result.

Input is one of: the bridge's stdout JSON saved to a file, an artifact's result.json, an artifact folder, or
``--task <task.json> --latest`` (the newest artifact of that task). Standard library only.

    python -B scripts/bridge_report.py <stdout.json | result.json | artifact-folder> [--task T] [--json]
    python -B scripts/bridge_report.py --task T --latest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPT.parents[1] / "src"))

LOG_LIMIT = 20_000_000
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
JAVAC = re.compile(r"^(?P<path>.+?\.\w+):(?P<line>\d+): error: (?P<msg>.*\S)\s*$")
KOTLIN = re.compile(r"^e: (?:file:///)?(?P<path>.+?\.\w+):(?P<line>\d+)(?::\d+)? (?P<msg>.*\S)\s*$")
FAILED_TESTS = (
    re.compile(r"^\s*(?P<name>\S.*? > \S.*?) FAILED\s*$"),            # Gradle: Class > method() FAILED
    re.compile(r"^(?:FAIL|ERROR): (?P<name>\S+ \(.+\))\s*$"),         # unittest
    re.compile(r"^FAILED (?P<name>\S+::\S.*?)(?: - .*)?\s*$"),        # pytest
)
ACCEPTABLE = {"REVIEW_PENDING", "IMPLEMENTED"}


def clean(value: Any, limit: int = 2000) -> str:
    return CONTROL.sub("", str(value if value is not None else ""))[:limit]


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"bridge-report: cannot read {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"bridge-report: {path} does not hold a JSON object")
    return data


def read_log(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(LOG_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return ""


def latest_artifact(task: Path) -> Path:
    from claude_codex_bridge import bridge
    from claude_codex_bridge.gitops import BridgeError
    try:
        return Path(bridge.resolve_artifact(Path("latest"), task))
    except BridgeError as exc:
        raise SystemExit(f"bridge-report: {exc}")


def locate(source: str | None, task: Path | None, latest: bool) -> tuple[dict[str, Any], Path | None]:
    """Return (record_or_report, artifact folder when known)."""
    if latest:
        if task is None:
            raise SystemExit("bridge-report: --latest needs --task")
        folder = latest_artifact(task)
        return read_json(folder / "result.json"), folder
    if not source:
        raise SystemExit("bridge-report: give a file, an artifact folder, or --task T --latest")
    path = Path(source)
    if path.is_dir():
        return read_json(path / "result.json"), path
    data = read_json(path)
    folder = data.get("artifact_directory") or data.get("artifact")
    return data, (Path(folder) if folder else (path.parent if path.name == "result.json" else None))


def path_prefixes(record: dict[str, Any], artifact: Path | None) -> list[str]:
    values = [record.get("worktree"), record.get("repository")]
    if artifact is not None:
        values.append(str(artifact / "worktree"))
    return [v.replace("\\", "/").rstrip("/") + "/" for v in values if isinstance(v, str) and v]


def shorten(path: str, prefixes: list[str]) -> str:
    text = path.replace("\\", "/")
    for prefix in prefixes:
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix):]
    match = re.search(r"/wt/[0-9a-f]+/(.*)$", text)
    return match.group(1) if match else text


def compiler_errors(text: str, prefixes: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for line in text.splitlines():
        for pattern in (JAVAC, KOTLIN):
            match = pattern.match(line.strip())
            if match:
                seen.setdefault(f"{shorten(match['path'], prefixes)}:{match['line']}: error: {match['msg']}", None)
                break
    return list(seen)


def gradle_problem(text: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "* What went wrong:":
            block: list[str] = []
            for follow in lines[index + 1:]:
                if follow.startswith("* ") or len(block) >= 25:
                    break
                block.append(follow.rstrip())
            while block and not block[-1].strip():
                block.pop()
            return block
    return []


def failed_tests(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for line in text.splitlines():
        for pattern in FAILED_TESTS:
            match = pattern.match(line)
            if match:
                seen.setdefault(match["name"].strip(), None)
                break
    return list(seen)


def normalize(data: dict[str, Any], artifact: Path | None) -> dict[str, Any]:
    """One shape from either a result.json record or the bridge's compact stdout report."""
    record = data
    if "artifact_directory" not in data and artifact is not None and (artifact / "result.json").is_file():
        record = read_json(artifact / "result.json")
    claim = data.get("worker") or data.get("codex_claim") or {}
    checks = []
    for item in claim.get("checks") or []:
        checks.append(clean(f"{item.get('reported_outcome')}: {item.get('description')}") if isinstance(item, dict) else clean(item))
    validation = data.get("validation") or {}
    stat = data.get("diffstat") or record.get("diffstat") or {}
    files = [{"path": clean(f.get("path"), 300), "added": f.get("added"), "removed": f.get("removed")}
             for f in (stat.get("files") or []) if isinstance(f, dict)]
    binding = data.get("review_binding")
    if not binding and data.get("snapshot_tree") and data.get("patch_sha256"):
        binding = {"snapshot_tree": data["snapshot_tree"], "patch_sha256": data["patch_sha256"]}
    warnings = [clean(w) for w in data.get("warnings") or []]
    orphans = bool((((record.get("validation") or {}).get("process")) or {}).get("orphans_killed")) or any(
        (s.get("process") or {}).get("orphans_killed") for s in record.get("segments") or [] if isinstance(s, dict))
    return {
        "task_id": data.get("task_id"),
        "status": data.get("status"),
        "lifecycle_status": data.get("lifecycle_status"),
        "validation_status": validation.get("status"),
        "worker": {"status": claim.get("status"), "summary": clean(claim.get("summary")),
                   "findings": [clean(x) for x in claim.get("findings") or []],
                   "blockers": [clean(x) for x in claim.get("blockers") or []], "checks": checks},
        "failures": [clean(x) for x in data.get("failures") or []],
        "warnings": warnings,
        "orphans_killed": orphans,
        "changed_paths": [clean(p, 300) for p in data.get("changed_paths") or []],
        "files": files,
        "review_binding": binding,
        "artifact": str(artifact) if artifact else None,
        "patch": data.get("patch") or data.get("diff_path"),
        "worktree": record.get("worktree"),
        "repository": record.get("repository"),
        "prefixes": path_prefixes(record, artifact),
        "extension_request": data.get("extension_request"),
    }


def validation_details(view: dict[str, Any]) -> dict[str, Any]:
    artifact = Path(view["artifact"]) if view["artifact"] else None
    if artifact is None:
        return {"compiler_errors": [], "gradle_problem": [], "failed_tests": []}
    text = read_log(artifact / "validation.stdout.log") + "\n" + read_log(artifact / "validation.stderr.log")
    return {"compiler_errors": compiler_errors(text, view["prefixes"]), "gradle_problem": gradle_problem(text),
            "failed_tests": failed_tests(text)}


def quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def bridge_command() -> str:
    return f'python -B {quote(str(SCRIPT.with_name("codex_bridge.py")))}'


def accept_command(view: dict[str, Any], task: str) -> str | None:
    binding = view["review_binding"]
    if view["lifecycle_status"] not in ACCEPTABLE or view["validation_status"] != "passed" or not binding or not view["artifact"]:
        return None
    return (f'{bridge_command()} accept --task {quote(task)} --artifact {quote(view["artifact"])} '
            f'--expect-tree {binding["snapshot_tree"]} --expect-patch-sha256 {binding["patch_sha256"]}')


def revise_skeleton(view: dict[str, Any], details: dict[str, Any], task: str) -> str | None:
    if view["validation_status"] != "failed" or not view["artifact"]:
        return None
    by_file: dict[str, list[str]] = {}
    for item in details["compiler_errors"]:
        by_file.setdefault(item.split(":", 1)[0], []).append(item)
    findings = []
    for path, items in list(by_file.items())[:5]:
        findings.append(f'--finding {quote(f"{path}::{len(items)} compile error(s), first: {items[0]}::the file compiles")}')
    for name in details["failed_tests"][:3]:
        findings.append(f'--finding {quote(f"<path>::test {name} fails::the test passes")}')
    if not findings:
        findings.append('--finding "<path>::<defect and scenario>::<expected correction>"')
    lines = [f'{bridge_command()} revise --task {quote(task)} --artifact {quote(view["artifact"])}']
    lines += [f"    {item}" for item in findings]
    return " \\\n".join(lines)


def build_report(data: dict[str, Any], artifact: Path | None, task: str) -> dict[str, Any]:
    view = normalize(data, artifact)
    details = validation_details(view) if view["validation_status"] == "failed" else {
        "compiler_errors": [], "gradle_problem": [], "failed_tests": []}
    view["validation_details"] = details
    view["accept_command"] = accept_command(view, task)
    view["revise_skeleton"] = revise_skeleton(view, details, task)
    view.pop("prefixes")
    return view


def render(view: dict[str, Any]) -> str:
    out: list[str] = []
    add = out.append

    def section(title: str, items: list[str], bullet: str = "- ") -> None:
        if items:
            add(f"{title}:")
            out.extend(f"  {bullet}{item}" for item in items)

    add(f"task: {view['task_id']}   lifecycle: {view['lifecycle_status']}   status: {view['status']}")
    add(f"validation: {view['validation_status']}")
    worker = view["worker"]
    add(f"worker: {worker['status']}  {worker['summary']}")
    section("worker findings", worker["findings"])
    section("worker blockers", worker["blockers"])
    section("worker checks", worker["checks"])
    section("failures", view["failures"])
    warnings = list(view["warnings"])
    if view["orphans_killed"] and not any("orphan" in w.lower() for w in warnings):
        warnings.append("orphans_killed: a process outlived the leader and was killed (often a leftover build daemon)")
    section("warnings", warnings)
    if view["extension_request"]:
        add(f"extension requested: {clean((view['extension_request'] or {}).get('reason'))}")
    if view["files"]:
        add("files changed:")
        out.extend(f"  {f['path']}  +{f['added']} -{f['removed']}" for f in view["files"])
    elif view["changed_paths"]:
        section("files changed", view["changed_paths"])
    binding = view["review_binding"]
    if binding:
        add(f"review_binding: snapshot_tree={binding['snapshot_tree']}  patch_sha256={binding['patch_sha256']}")
    add(f"artifact: {view['artifact']}")
    if view["patch"]:
        add(f"patch: {view['patch']}")
    details = view["validation_details"]
    if details["compiler_errors"]:
        section(f"compiler errors ({len(details['compiler_errors'])} unique)", details["compiler_errors"])
    if details["gradle_problem"]:
        add("gradle, what went wrong:")
        out.extend(f"  {line}" for line in details["gradle_problem"])
    section("failed tests", details["failed_tests"])
    if view["accept_command"]:
        add("")
        add("accept:")
        add(f"  {view['accept_command']}")
    if view["revise_skeleton"]:
        add("")
        add("revise (edit the findings first):")
        out.extend(f"  {line}" for line in view["revise_skeleton"].splitlines())
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge-report", description=__doc__.split("\n\n")[0])
    parser.add_argument("source", nargs="?", help="bridge stdout JSON file, result.json or artifact folder")
    parser.add_argument("--task", type=Path, help="task file (names the accept command; with --latest, picks the artifact)")
    parser.add_argument("--latest", action="store_true", help="use the newest artifact of --task")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    data, artifact = locate(args.source, args.task, args.latest)
    task = str(args.task) if args.task else "<task.json>"
    view = build_report(data, artifact, task)
    print(json.dumps(view, indent=2) if args.json else render(view))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
