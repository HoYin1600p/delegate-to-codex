"""land: integrate a reviewed bridge result into the repository, build, compare, and optionally commit.

Steps, stopping at the first failure (nothing is ever pushed):

  1. integrate: the bridge's ``accept`` for a REVIEW_PENDING/IMPLEMENTED result, bound to the tree and patch hash;
     or, with --apply, ``git apply`` of the artifact's diff.patch (for a blocked or partial result)
  2. --build <command ...>      run a build in the repository (argument array; this option must come last)
  3. --compare-listing GLOB=BASELINE   sorted zip entry list of the one file matching GLOB against a baseline listing
  4. --compare-file PRODUCED=BASELINE  byte comparison
  5. --commit "message"         git add of only the changed paths the artifact reports, then commit those paths
  6. clean up the bridge worktree (accept does it itself; --apply uses the bridge's ``cleanup``)

    python -B scripts/land.py --task T --artifact A|latest [--apply] [--3way] [--expect-tree X --expect-patch-sha256 Y]
        [--compare-listing "build/libs/*.jar=baseline.txt" --allow-added META-INF/new.txt]
        [--compare-file out.bin=baseline.bin] [--commit "message"] [--json] [--build gradlew.bat build --no-daemon]
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
sys.dont_write_bytecode = True
BRIDGE = SCRIPT.with_name("codex_bridge.py")
ACCEPTABLE = {"REVIEW_PENDING", "IMPLEMENTED"}


class Stop(Exception):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step, self.message = step, message


def run(command: list[str], cwd: Path | None = None, input: bytes | None = None,
        timeout: int | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, cwd=cwd, input=input, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise Stop("run", f"cannot start {command[0]!r}: {exc}")
    except subprocess.TimeoutExpired:
        raise Stop("run", f"{command[0]!r} exceeded {timeout} seconds")


def text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def tail(data: bytes, lines: int = 25) -> str:
    return "\n".join(text(data).splitlines()[-lines:])


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Stop("load", f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        raise Stop("load", f"{path} does not hold a JSON object")
    return value


def bridge(*args: str) -> tuple[int, dict[str, Any] | None, bytes]:
    done = run([sys.executable, "-B", str(BRIDGE), *args])
    try:
        body = json.loads(text(done.stdout))
    except ValueError:
        body = None
    return done.returncode, body, done.stderr


def resolve_artifact(task: Path, artifact: str) -> Path:
    if artifact != "latest":
        return Path(artifact)
    sys.path.insert(0, str(SCRIPT.parents[1] / "src"))
    from claude_codex_bridge import bridge as bridge_module
    from claude_codex_bridge.gitops import BridgeError
    try:
        return Path(bridge_module.resolve_artifact(Path("latest"), task))
    except BridgeError as exc:
        raise Stop("load", str(exc))


def repo_root(args: argparse.Namespace, record: dict[str, Any]) -> Path:
    if args.repo:
        return args.repo
    task = read_json(args.task)
    value = task.get("repo_root") or record.get("repository")
    if not value:
        raise Stop("load", "the repository is unknown; pass --repo")
    return Path(value)


def integrate_accept(args: argparse.Namespace, artifact: Path, record: dict[str, Any]) -> dict[str, Any]:
    if record.get("lifecycle_status") not in ACCEPTABLE:
        raise Stop("integrate", f"lifecycle_status is {record.get('lifecycle_status')}, not REVIEW_PENDING or IMPLEMENTED; "
                                "use --apply to take the patch of a blocked or partial result")
    tree = args.expect_tree or record.get("snapshot_tree")
    digest = args.expect_patch_sha256 or record.get("patch_sha256")
    if not tree or not digest:
        raise Stop("integrate", "the record has no snapshot_tree/patch_sha256; pass --expect-tree and --expect-patch-sha256")
    command = ["accept", "--task", str(args.task), "--artifact", str(artifact), "--expect-tree", tree,
               "--expect-patch-sha256", digest]
    if args.three_way:
        command.append("--3way")
    code, body, stderr = bridge(*command)
    if code != 0 or not body or body.get("status") != "accepted":
        detail = json.dumps(body) if body else text(stderr)
        raise Stop("integrate", f"bridge accept failed (exit {code}): {detail[:1500]}")
    note = {"method": "accept", "bound_to": "arguments" if args.expect_tree else "the artifact's own record"}
    if body.get("cleanup_pending"):
        code, body, stderr = bridge(*command)
        if code != 0 or not body or body.get("cleanup_pending"):
            raise Stop("cleanup", "the patch is applied but the bridge worktree is not removed; "
                                  "run accept or cleanup again")
    return note


def integrate_apply(args: argparse.Namespace, artifact: Path, repo: Path) -> dict[str, Any]:
    patch_file = artifact / "diff.patch"
    try:
        patch = patch_file.read_bytes()
    except OSError as exc:
        raise Stop("integrate", f"cannot read {patch_file}: {exc}")
    if not patch.strip():
        raise Stop("integrate", "diff.patch is empty; nothing to apply")
    flags = ["--3way"] if args.three_way else []
    checked = run(["git", "apply", "--check", *flags, "-"], cwd=repo, input=patch)
    if checked.returncode:
        raise Stop("integrate", "git apply --check failed, nothing was changed: " + tail(checked.stderr))
    applied = run(["git", "apply", *flags, "-"], cwd=repo, input=patch)
    if applied.returncode:
        raise Stop("integrate", "git apply failed: " + tail(applied.stderr))
    return {"method": "apply", "patch": str(patch_file)}


def build_step(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    command = json.loads(args.build_json) if args.build_json else args.build
    if not command:
        return {}
    done = run(command, cwd=repo, timeout=args.build_timeout)
    if done.returncode:
        raise Stop("build", f"build exited {done.returncode}\n" + tail(done.stdout + b"\n" + done.stderr))
    return {"command": command, "exit_code": 0}


def zip_entries(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return sorted(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise Stop("compare", f"{path} is not a readable zip/jar: {exc}")


def compare_listing(spec: str, allowed: set[str], repo: Path) -> dict[str, Any]:
    pattern, _, baseline_name = spec.partition("=")
    if not pattern or not baseline_name:
        raise Stop("compare", f"--compare-listing needs GLOB=BASELINE, got {spec!r}")
    matches = sorted(glob.glob(str(repo / pattern)))
    if len(matches) != 1:
        raise Stop("compare", f"{pattern!r} matched {len(matches)} files in {repo}; it must match exactly one")
    baseline_path = Path(baseline_name)
    if not baseline_path.is_absolute():
        baseline_path = repo / baseline_path
    try:
        baseline = sorted({line.strip() for line in baseline_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()})
    except OSError as exc:
        raise Stop("compare", f"cannot read baseline {baseline_path}: {exc}")
    produced = zip_entries(Path(matches[0]))
    added = sorted(set(produced) - set(baseline))
    removed = sorted(set(baseline) - set(produced))
    unexpected = [entry for entry in added if entry not in allowed]
    if unexpected or removed:
        parts = [f"{matches[0]} differs from {baseline_path}"]
        parts += [f"  + {entry}" for entry in unexpected[:50]] + [f"  - {entry}" for entry in removed[:50]]
        raise Stop("compare", "\n".join(parts))
    return {"file": matches[0], "entries": len(produced), "added_allowed": added}


def compare_file(spec: str, repo: Path) -> dict[str, Any]:
    produced_name, _, baseline_name = spec.partition("=")
    if not produced_name or not baseline_name:
        raise Stop("compare", f"--compare-file needs PRODUCED=BASELINE, got {spec!r}")
    produced, baseline = (Path(n) if Path(n).is_absolute() else repo / n for n in (produced_name, baseline_name))
    try:
        same = produced.read_bytes() == baseline.read_bytes()
    except OSError as exc:
        raise Stop("compare", str(exc))
    if not same:
        raise Stop("compare", f"{produced} differs from {baseline}")
    return {"file": str(produced), "baseline": str(baseline)}


def commit_step(message: str, paths: list[str], repo: Path) -> dict[str, Any]:
    if not paths:
        raise Stop("commit", "the artifact reports no changed paths to commit")
    added = run(["git", "add", "--", *paths], cwd=repo)
    if added.returncode:
        raise Stop("commit", "git add failed: " + tail(added.stderr))
    done = run(["git", "commit", "-m", message, "--", *paths], cwd=repo)
    if done.returncode:
        raise Stop("commit", "git commit failed: " + tail(done.stdout + b"\n" + done.stderr))
    sha = text(run(["git", "rev-parse", "HEAD"], cwd=repo).stdout).strip()
    return {"commit": sha, "paths": paths}


def cleanup_step(args: argparse.Namespace, artifact: Path) -> dict[str, Any]:
    code, body, stderr = bridge("cleanup", "--task", str(args.task), "--artifact", str(artifact))
    if code != 0:
        raise Stop("cleanup", f"bridge cleanup failed (exit {code}): {(json.dumps(body) if body else text(stderr))[:1000]}")
    return {"status": (body or {}).get("status")}


def land(args: argparse.Namespace) -> dict[str, Any]:
    summary: dict[str, Any] = {"steps": {}, "status": "stopped"}
    steps = summary["steps"]
    try:
        artifact = resolve_artifact(args.task, args.artifact)
        record = read_json(artifact / "result.json")
        repo = repo_root(args, record)
        summary.update(task_id=record.get("task_id"), artifact=str(artifact), repo=str(repo))
        if args.apply and record.get("lifecycle_status") in {"RUNNING", "ACCEPTED", "REJECTED"}:
            raise Stop("integrate", f"lifecycle_status is {record.get('lifecycle_status')}; its patch is not for applying")
        steps["integrate"] = integrate_apply(args, artifact, repo) if args.apply else integrate_accept(args, artifact, record)
        build = build_step(args, repo)
        if build:
            steps["build"] = build
        for spec in args.compare_listing:
            steps.setdefault("compare_listing", []).append(compare_listing(spec, set(args.allow_added), repo))
        for spec in args.compare_file:
            steps.setdefault("compare_file", []).append(compare_file(spec, repo))
        if args.commit:
            steps["commit"] = commit_step(args.commit, [str(p) for p in record.get("changed_paths") or []], repo)
        if args.apply:
            steps["cleanup"] = cleanup_step(args, artifact)
        else:
            steps["cleanup"] = {"status": "done by accept"}
        summary["status"] = "landed"
    except Stop as stop:
        summary.update(failed_step=stop.step, error=stop.message)
    return summary


def render(summary: dict[str, Any]) -> str:
    lines = [f"land: {summary['status']}   task: {summary.get('task_id')}   repo: {summary.get('repo')}"]
    for name, value in summary["steps"].items():
        lines.append(f"  ok  {name}: {json.dumps(value)}")
    if summary.get("failed_step"):
        lines.append(f"  FAILED at {summary['failed_step']}: {summary['error']}")
        lines.append("  stopped here; later steps did not run" + (
            "; the bridge worktree was kept" if summary["failed_step"] != "cleanup" else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="land", description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--artifact", required=True, help="artifact folder, or 'latest' (the newest of --task)")
    parser.add_argument("--repo", type=Path, help="repository (default: repo_root of the task file)")
    parser.add_argument("--apply", action="store_true", help="git-apply diff.patch instead of the bridge's accept")
    parser.add_argument("--3way", dest="three_way", action="store_true")
    parser.add_argument("--expect-tree")
    parser.add_argument("--expect-patch-sha256")
    parser.add_argument("--compare-listing", action="append", default=[], metavar="GLOB=BASELINE")
    parser.add_argument("--allow-added", action="append", default=[], metavar="ENTRY")
    parser.add_argument("--compare-file", action="append", default=[], metavar="PRODUCED=BASELINE")
    parser.add_argument("--commit", metavar="MESSAGE")
    parser.add_argument("--build-json", help="the build command as a JSON array")
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--build", nargs=argparse.REMAINDER, default=[],
                        help="build command: every argument after this option (put it last)")
    args = parser.parse_args(argv)
    summary = land(args)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0 if summary["status"] == "landed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
