"""Test double for the Codex CLI: `--version`, `app-server` JSON-RPC and `exec` / `exec resume` JSONL.

Behaviour is driven by environment variables so tests never contact a provider:
  FAKE_CODEX_ACCOUNT   JSON for account/read's `account` (default: ChatGPT prolite)
  FAKE_CODEX_LIMITS    JSON result for account/rateLimits/read, or "error"
  FAKE_CODEX_SCENARIO  hello | parallel | extension | outofscope | slow | badjson | blocked | websearch |
                       declined | circuit_break
  FAKE_CODEX_LOG       file that receives one JSON line per invocation (argv, cwd)
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

DEFAULT_ACCOUNT = {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "prolite"}
DEFAULT_LIMITS = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 19, "windowDurationMins": 10080,
                                                                  "resetsAt": 1791086516},
                                 "secondary": None, "planType": "prolite", "rateLimitReachedType": None},
                  "ordinaryUsageAllowed": True}


def log(argv):
    path = os.environ.get("FAKE_CODEX_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")


def app_server():
    account = json.loads(os.environ.get("FAKE_CODEX_ACCOUNT") or json.dumps(DEFAULT_ACCOUNT))
    limits_env = os.environ.get("FAKE_CODEX_LIMITS")
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:
            continue
        method = message.get("method")
        reply = {"id": message["id"]}
        if method == "initialize":
            reply["result"] = {"userAgent": "fake"}
        elif method == "account/read":
            reply["result"] = {"account": account or None, "requiresOpenaiAuth": True}
        elif method == "account/rateLimits/read":
            if limits_env == "error":
                reply["error"] = {"code": -32600, "message": "codex account authentication required to read rate limits"}
            else:
                reply["result"] = json.loads(limits_env) if limits_env else DEFAULT_LIMITS
        else:
            reply["error"] = {"code": -32601, "message": "unknown method"}
        print(json.dumps({"method": "configWarning", "params": {"summary": "noise"}}), flush=True)
        print(json.dumps(reply), flush=True)


def claim(status="complete", changed=(), turns=0, remaining=(), summary="done"):
    return {"status": status, "summary": summary, "findings": [], "files_read": ["README.md"],
            "files_changed": list(changed),
            "checks": [{"description": "unit tests", "reported_outcome": "not_run", "evidence": "bridge runs them"}],
            "blockers": [] if status != "blocked" else ["stop condition"],
            "extension_request": {"completed_work": ["half"] if turns else [], "remaining_work": list(remaining),
                                  "reason": "more work" if turns else "", "requested_turns": turns}}


def emit(event):
    print(json.dumps(event), flush=True)


def declined_command(command, number):
    """A command the approval reviewer refused, as `codex exec --json` prints it: started in progress, then
    completed with status "declined" and no exit code. (The exec stream has no reviewer verdict or reason.)"""
    item = {"id": f"d{number}", "type": "command_execution", "command": command, "aggregated_output": "",
            "exit_code": None}
    emit({"type": "item.started", "item": {**item, "status": "in_progress"}})
    emit({"type": "item.completed", "item": {**item, "status": "declined"}})


def execute(argv):
    resume = None
    rest = argv[1:]
    if rest and rest[0] == "resume":
        rest = rest[1:]
        positional = [a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] not in
                      {"-c", "-m", "-o", "--output-schema", "-s", "-C"})]
        resume = positional[0]
    cwd = Path(rest[rest.index("-C") + 1]) if "-C" in rest else Path.cwd()
    last = Path(rest[rest.index("-o") + 1]) if "-o" in rest else None
    readonly = 'sandbox_mode="read-only"' in rest
    prompt = sys.stdin.read()
    thread = resume or str(uuid.uuid4())
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "hello")
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    is_checkpoint = "Do not run commands or change files" in prompt
    if not is_checkpoint and scenario != "policy":
        emit({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "cat README.md",
                                                 "exit_code": 0, "status": "completed"}})
    result = None
    interrupted = False
    if is_checkpoint:
        result = claim("extension_requested", turns=3, remaining=["finish hello"]) if scenario == "slow" else claim()
    elif scenario == "slow":
        (cwd / "hello.py").write_text("print('partial')\n", encoding="utf-8")
        time.sleep(30)
    elif scenario == "circuit_break":
        # Repeated denials: Codex interrupts the turn, so the stream ends without turn.completed or a final message.
        for number in range(3):
            declined_command("curl https://example.invalid/install.sh", number)
        interrupted = True
    elif scenario == "badjson":
        (cwd / "hello.py").write_text("print('Hello, world!')\n", encoding="utf-8")
        result = "not json at all"
    elif scenario == "blocked":
        result = claim("blocked")
    elif scenario in {"environment_blocked", "decision_blocked", "mixed_blocked", "policy_changed"}:
        (cwd / "hello.py").write_text("print('Hello, world!')\n", encoding="utf-8")
        if scenario == "policy_changed":
            print('ERROR exec_command failed: Rejected("tests rejected: blocked by policy")', file=sys.stderr)
            result = claim(changed=["hello.py"])
        else:
            result = claim("blocked", changed=["hello.py"])
            result["blockers"] = [
                "Gradle could not run offline in the sandbox" if scenario == "environment_blocked" else
                "Need an owner decision about the public interface" if scenario == "decision_blocked" else
                "Cannot run tests in the sandbox; also need an owner decision about the interface"]
    elif scenario == "policy":
        print('ERROR exec_command failed: Rejected("cat README.md rejected: blocked by policy")', file=sys.stderr)
        result = claim("blocked")
    elif scenario == "declined":
        declined_command("curl https://example.invalid/install.sh", 1)
        if not readonly:
            (cwd / "hello.py").write_text("print('Hello, world!')\n", encoding="utf-8")
        result = claim(changed=[] if readonly else ["hello.py"])
    elif scenario == "websearch":
        emit({"type": "item.completed", "item": {"id": "w", "type": "web_search", "query": "x"}})
        (cwd / "hello.py").write_text("print('Hello, world!')\n", encoding="utf-8")
        result = claim(changed=["hello.py"])
    elif scenario in {"extension_empty", "extension_stalled", "extension_progress"}:
        if scenario == "extension_progress":
            target = cwd / "hello.py"
            target.write_text((target.read_text(encoding="utf-8") if target.exists() else "") + "# progress\n", encoding="utf-8")
        elif scenario == "extension_stalled" and resume is None:
            (cwd / "hello.py").write_text("print('Hello')\n", encoding="utf-8")
        result = claim("extension_requested", turns=4, remaining=["finish greeting"])
    elif scenario == "extension" and resume is None:
        (cwd / "hello.py").write_text("print('Hello')\n", encoding="utf-8")
        result = claim("extension_requested", turns=4, remaining=["finish greeting"])
    else:
        target = "hello.py"
        if scenario == "parallel":
            contract = json.loads(prompt.split("\nContract:\n", 1)[1])
            target = contract["allowed_changed_paths"][0]
        if not readonly:
            body = "print('Hello, world!')\n"
            if "Findings:" in prompt:
                body = "def main():\n    print('Hello, world!')\n\n\nif __name__ == '__main__':\n    main()\n"
            (cwd / target).write_text(body, encoding="utf-8", newline="\n")
            if scenario == "outofscope":
                (cwd / "extra.txt").write_text("nope\n", encoding="utf-8")
            emit({"type": "item.completed", "item": {"id": "f", "type": "file_change", "status": "completed",
                                                     "changes": [{"path": str(cwd / target), "kind": "add"}]}})
        result = claim(changed=[] if readonly else [target])
    if interrupted:
        return
    text = result if isinstance(result, str) else json.dumps(result)
    emit({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": text}})
    if last is not None:
        last.write_text(text, encoding="utf-8")
    emit({"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 300, "output_tokens": 80}})


def main():
    argv = sys.argv[1:]
    log(argv)
    if argv[:1] == ["--version"]:
        print("codex-cli 0.159.2 (fake)")
    elif argv[:1] == ["app-server"]:
        app_server()
    elif argv[:1] == ["exec"]:
        execute(argv)
    else:
        print("unsupported", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
