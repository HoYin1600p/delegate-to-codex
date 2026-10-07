"""The Codex CLI side: discovery, app-server lifecycle, usage classification and cache, preflight, event parsing.

No provider is contacted; Codex is the test double in fake_codex.py.
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import BridgeCase, FAKE, PYTHON
import fake_codex
import claude_codex_bridge
from claude_codex_bridge import bridge, codexcli, gitops


STUB_APP_SERVER = """
import json, os, sys, time
log = os.environ["STUB_LOG"]
mode = os.environ.get("STUB_MODE", "ok")
for line in sys.stdin:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(line)
    message = json.loads(line)
    if mode == "silent":
        time.sleep(300)
    if mode == "die":
        sys.exit(0)
    if "id" in message:
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
"""


class UsageClassification(unittest.TestCase):
    def test_null_five_hour_is_trusted_and_weekly_governs(self):
        record = codexcli.classify_usage({"rateLimits": {"primary": {"usedPercent": 19, "windowDurationMins": 10080},
                                                         "secondary": None}, "ordinaryUsageAllowed": True})
        self.assertFalse(record["five_hour"]["applicable"])
        self.assertIn("trusted", record["five_hour"]["reason"])
        self.assertEqual(record["weekly"]["remaining_percent"], 81.0)
        self.assertEqual(record["governing_windows"], ["weekly"])
        self.assertEqual(record["gate"], "available")

    def test_weekly_exhaustion_without_five_hour_blocks(self):
        record = codexcli.classify_usage({"rateLimits": {"primary": {"usedPercent": 100, "windowDurationMins": 10080}}})
        self.assertEqual(record["gate"], "exhausted")
        self.assertIn("weekly resets", record["gate_reason"])

    def test_both_windows_by_duration_not_slot(self):
        record = codexcli.classify_usage({"rateLimits": {
            "primary": {"usedPercent": 40, "windowDurationMins": 10080},
            "secondary": {"usedPercent": 100, "windowDurationMins": 300}}})
        self.assertEqual(record["five_hour"]["slot"], "secondary")
        self.assertEqual(record["gate"], "exhausted")
        self.assertIn("five_hour", record["gate_reason"])

    def test_no_windows_is_unknown(self):
        record = codexcli.classify_usage({"rateLimits": {"primary": None, "secondary": None}})
        self.assertEqual(record["gate"], "unknown")

    def test_credit_depletion_blocks(self):
        record = codexcli.classify_usage({"rateLimits": {
            "primary": {"usedPercent": 10, "windowDurationMins": 10080},
            "rateLimitReachedType": "workspace_member_credits_depleted"}})
        self.assertEqual(record["gate"], "blocked")

    def test_ordinary_usage_false_blocks(self):
        record = codexcli.classify_usage({"rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 10080}},
                                          "ordinaryUsageAllowed": False})
        self.assertEqual(record["gate"], "blocked")


class UsageCache(BridgeCase):
    def test_missing_partial_invalid_and_corrupt_caches_are_misses(self):
        cache = self.tmp / "usage.json"
        command = codexcli.find_codex()
        invalid = [None, b'{"_cached_at":', b'[]', b'null', b'\xff',
                   json.dumps({"_cached_at": time.time()}).encode(),
                   json.dumps({"gate": "available", "_cached_at": "invalid"}).encode(),
                   json.dumps({"gate": "available", "_cached_at": 10 ** 1000}).encode(),
                   json.dumps({"gate": "available", "_cached_at": float("nan")}).encode()]
        for data in invalid:
            with self.subTest(data=data):
                if data is None:
                    cache.unlink(missing_ok=True)
                else:
                    cache.write_bytes(data)
                result = codexcli.fetch_usage(command, cache)
                self.assertEqual(result["retrieval"]["mode"], "live")
                self.assertEqual(result["gate"], "available")
                self.assertEqual(codexcli.fetch_usage(command, cache)["retrieval"]["mode"], "cache")

    def test_fresh_cache_skips_version_subprocess_but_keeps_live_auth_and_gate(self):
        bridge._gate_preflight(bridge.preflight())
        with mock.patch.object(codexcli.subprocess, "Popen", wraps=subprocess.Popen) as start:
            record = bridge.preflight()
        self.assertEqual(start.call_count, 1, "only live account/usage app-server should start")
        self.assertEqual(record["version"], "codex-cli 0.159.2 (fake)")
        self.assertEqual(record["usage"]["gate"], "available")
        cached = json.loads(bridge.usage_cache().read_text(encoding="utf-8"))
        cached["_cached_at"] -= 61
        bridge.usage_cache().write_text(json.dumps(cached), encoding="utf-8")
        with mock.patch.object(codexcli.subprocess, "Popen", wraps=subprocess.Popen) as start:
            bridge.preflight()
        self.assertEqual(start.call_count, 2, "stale cache requires version and live account/usage probes")

    def test_post_run_cache_is_used_even_after_ttl_but_near_exhaustion_refreshes(self):
        before = bridge._gate_preflight(bridge.preflight())
        cached = json.loads(bridge.usage_cache().read_text(encoding="utf-8"))
        cached["_cached_at"] -= 61
        bridge.usage_cache().write_text(json.dumps(cached), encoding="utf-8")
        command = codexcli.find_codex()
        with mock.patch.object(codexcli.subprocess, "Popen", wraps=subprocess.Popen) as start:
            after = bridge._usage_after(command, before)
        self.assertEqual(start.call_count, 0)
        self.assertEqual(after["retrieval"]["mode"], "cache")
        for window in ("weekly", "five_hour", "other"):
            near = {**before, "weekly": {"applicable": True, "remaining_percent": 81}}
            value = {"applicable": True, "remaining_percent": 10}
            if window == "other":
                near["other_windows"] = [value]
            else:
                near[window] = value
            with self.subTest(window=window), mock.patch.object(codexcli.subprocess, "Popen", wraps=subprocess.Popen) as start:
                after = bridge._usage_after(command, near)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(after["retrieval"]["mode"], "live")

    def test_preflight_and_usage_both_write_atomically(self):
        with mock.patch.object(bridge, "atomic_write_json", wraps=gitops.atomic_write_json) as write:
            bridge._gate_preflight(bridge.preflight())
            write.assert_called_once()
        with mock.patch.object(codexcli, "atomic_write_json", wraps=gitops.atomic_write_json) as write:
            codexcli.fetch_usage(codexcli.find_codex(), bridge.usage_cache(), use_cache=False)
            write.assert_called_once()


class Preflight(BridgeCase):
    def test_preflight_reads_account_and_null_five_hour_usage(self):
        record = bridge.preflight()
        self.assertEqual(record["auth_method"], "chatgpt")
        self.assertEqual(record["usage"]["gate"], "available")
        self.assertFalse(record["usage"]["five_hour"]["applicable"])
        self.assertNotIn("fixture@example.invalid", json.dumps(record))

    def test_api_key_environment_is_rejected(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        with self.assertRaises(Exception):
            bridge.preflight()

    def test_api_key_account_is_rejected(self):
        os.environ["FAKE_CODEX_ACCOUNT"] = json.dumps({"type": "apiKey"})
        with self.assertRaises(codexcli.CodexCliError):
            bridge.preflight()

    def test_signed_out_is_rejected(self):
        os.environ["FAKE_CODEX_ACCOUNT"] = "{}"
        with self.assertRaises(codexcli.CodexCliError):
            bridge.preflight()

    def test_unavailable_usage_pauses_run(self):
        os.environ["FAKE_CODEX_LIMITS"] = "error"
        with self.assertRaises(bridge.CapacityPaused) as caught:
            bridge.run(self.task())
        self.assertEqual(caught.exception.usage["gate"], "unknown")

    def test_exhausted_weekly_pauses_run(self):
        os.environ["FAKE_CODEX_LIMITS"] = json.dumps({"rateLimits": {"primary": {"usedPercent": 100, "windowDurationMins": 10080}}})
        with self.assertRaises(bridge.CapacityPaused) as caught:
            bridge.run(self.task())
        self.assertEqual(caught.exception.usage["gate"], "exhausted")
        self.assertIn("weekly resets", caught.exception.usage["gate_reason"])


class WindowsSandbox(unittest.TestCase):
    def test_windows_runs_pass_an_explicit_sandbox(self):
        os.environ.pop("CODEX_BRIDGE_WINDOWS_SANDBOX", None)
        self.assertEqual(bridge._windows_args(True), ["-c", 'windows.sandbox="elevated"'])
        self.assertEqual(bridge._windows_args(False), [])
        os.environ["CODEX_BRIDGE_WINDOWS_SANDBOX"] = "unelevated"
        try:
            self.assertEqual(bridge._windows_args(True), ["-c", 'windows.sandbox="unelevated"'])
            os.environ["CODEX_BRIDGE_WINDOWS_SANDBOX"] = "off"
            with self.assertRaises(Exception):
                bridge._windows_args(True)
        finally:
            os.environ.pop("CODEX_BRIDGE_WINDOWS_SANDBOX", None)


class AppServerLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub = Path(self.tmp.name) / "stub.py"
        self.stub.write_text(STUB_APP_SERVER, encoding="utf-8")
        self.log = Path(self.tmp.name) / "stub.log"
        self.command = codexcli.CodexCommand(PYTHON, (str(self.stub),))

    def _env(self, mode):
        return mock.patch.dict(os.environ, {"STUB_LOG": str(self.log), "STUB_MODE": mode})

    def test_failed_initialize_stops_the_spawned_server(self):
        with self._env("silent"):
            server = codexcli.AppServer(self.command, timeout=1)
            with self.assertRaises(codexcli.CodexCliError):
                with server:
                    self.fail("initialize should not succeed")
            self.assertIsNotNone(server.process.poll(), "app-server was left running after __enter__ failed")

    def test_initialize_reports_the_package_version(self):
        with self._env("ok"):
            with codexcli.AppServer(self.command, timeout=10):
                pass
        first = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["params"]["clientInfo"]["version"], claude_codex_bridge.__version__)

    def test_send_to_a_dead_server_raises_codex_cli_error(self):
        with self._env("ok"):
            server = codexcli.AppServer(self.command, timeout=5)
            try:
                server.process.stdin.close()
                server.process.wait(timeout=10)
                with self.assertRaises(codexcli.CodexCliError):
                    server.request("account/read")
            finally:
                server.close()

    def test_broken_pipe_on_write_is_wrapped(self):
        server = codexcli.AppServer.__new__(codexcli.AppServer)
        server.process = SimpleNamespace(stdin=mock.Mock(write=mock.Mock(side_effect=BrokenPipeError(32, "pipe"))))
        with self.assertRaises(codexcli.CodexCliError):
            server._send({"id": 1, "method": "x"})


class UsageCacheVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "usage.json"
        self.command = codexcli.CodexCommand(PYTHON, (str(FAKE),))

    def test_fetch_usage_keeps_the_recorded_codex_version(self):
        self.cache.write_text(json.dumps({"gate": "available", "_cached_at": time.time() - 3600,
                                          "_codex_version": "codex-cli 9.9.9",
                                          "_codex_executable": codexcli.executable_fingerprint(self.command)}),
                              encoding="utf-8")
        record = codexcli.fetch_usage(self.command, self.cache, use_cache=False)
        self.assertEqual(record["gate"], "available")
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8"))["_codex_version"], "codex-cli 9.9.9")

    def test_the_report_does_not_leak_private_cache_keys(self):
        codexcli.fetch_usage(self.command, self.cache, use_cache=False)
        record = codexcli.read_usage_cache(self.cache)
        self.assertEqual(record["retrieval"]["mode"], "cache")
        self.assertEqual([key for key in record if key.startswith("_")], [])
        self.assertEqual([key for key in codexcli.fetch_usage(self.command, self.cache) if key.startswith("_")], [])

    def test_fetch_usage_without_a_recorded_version_asks_codex(self):
        codexcli.fetch_usage(self.command, self.cache, use_cache=False)
        self.assertTrue(json.loads(self.cache.read_text(encoding="utf-8")).get("_codex_version"))


    def test_a_version_recorded_for_another_executable_is_not_reused(self):
        other = Path(self.tmp.name) / "other-codex"
        other.write_bytes(b"x")
        stale = {"gate": "available", "_cached_at": time.time() - 3600, "_codex_version": "codex-cli 9.9.9",
                 "_codex_executable": codexcli.executable_fingerprint(codexcli.CodexCommand(other))}
        for recorded in (stale, {k: v for k, v in stale.items() if k != "_codex_executable"}):
            with self.subTest(recorded=sorted(recorded)):
                self.cache.write_text(json.dumps(recorded), encoding="utf-8")
                codexcli.fetch_usage(self.command, self.cache, use_cache=False)
                written = json.loads(self.cache.read_text(encoding="utf-8"))
                self.assertNotEqual(written["_codex_version"], "codex-cli 9.9.9")
                self.assertEqual(written["_codex_executable"], codexcli.executable_fingerprint(self.command))

    def test_a_changed_executable_file_changes_its_fingerprint(self):
        binary = Path(self.tmp.name) / "codex-bin"
        binary.write_bytes(b"one")
        before = codexcli.executable_fingerprint(codexcli.CodexCommand(binary))
        binary.write_bytes(b"longer content")
        self.assertNotEqual(before, codexcli.executable_fingerprint(codexcli.CodexCommand(binary)))


class CodexVersionOutput(unittest.TestCase):
    def version_for(self, stdout, stderr="", exit_code=0):
        result = SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code, timed_out=False)
        with mock.patch.object(codexcli, "run_process", return_value=result):
            return codexcli.codex_version(codexcli.CodexCommand(PYTHON))

    def test_the_version_line_is_found(self):
        self.assertEqual(self.version_for("codex-cli 0.159.2 (fake)\n"), "codex-cli 0.159.2 (fake)")
        self.assertEqual(self.version_for("warning: update\ncodex-cli 1.2.3\n"), "codex-cli 1.2.3")
        self.assertEqual(self.version_for("", "codex-cli 1.2.3\n"), "codex-cli 1.2.3")

    def test_output_without_a_version_is_an_error(self):
        for stdout, stderr in (("", ""), ("", "WARNING: crash log\n"), ("hello\n", "")):
            with self.subTest(stdout=stdout, stderr=stderr), self.assertRaises(codexcli.CodexCliError):
                self.version_for(stdout, stderr)
        with self.assertRaises(codexcli.CodexCliError):
            self.version_for("codex-cli 1.2.3", exit_code=1)


class WindowFields(unittest.TestCase):
    def window(self, **fields):
        return codexcli._window({"usedPercent": 10, **fields})

    def test_integral_floats_are_accepted_for_duration_and_reset(self):
        window = self.window(windowDurationMins=300.0, resetsAt=1791086516.0)
        self.assertEqual(window["window_minutes"], 300)
        self.assertEqual(window["resets_at"], self.window(resetsAt=1791086516)["resets_at"])
        record = codexcli.classify_usage({"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 10080.0}}})
        self.assertEqual(record["governing_windows"], ["weekly"])

    def test_non_integral_or_odd_numbers_become_none(self):
        for value in (300.5, float("nan"), float("inf"), True, "300", None):
            with self.subTest(value=value):
                window = self.window(windowDurationMins=value, resetsAt=value)
                self.assertIsNone(window["window_minutes"])
                self.assertIsNone(window["resets_at"])

    def test_an_impossible_reset_time_degrades_instead_of_raising(self):
        for value in (10 ** 18, -10 ** 18, 10 ** 30, -1e15):
            with self.subTest(value=value):
                self.assertIsNone(self.window(resetsAt=value)["resets_at"])
        record = codexcli.classify_usage({"rateLimits": {"primary": {
            "usedPercent": 100, "windowDurationMins": 300, "resetsAt": 10 ** 30}}})
        self.assertEqual(record["gate"], "exhausted")
        self.assertIn("at an unreported time", record["gate_reason"])

    def test_a_millisecond_epoch_means_the_same_moment(self):
        seconds = self.window(resetsAt=1791086516)["resets_at"]
        self.assertIsNotNone(seconds)
        self.assertEqual(self.window(resetsAt=1791086516000)["resets_at"], seconds)


class ExplicitBlockOutranksExhaustion(unittest.TestCase):
    WEEKLY_SPENT = {"primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 1791086516}}

    def test_credit_block_with_spent_window_is_blocked(self):
        record = codexcli.classify_usage({"rateLimits": {**self.WEEKLY_SPENT,
                                                         "rateLimitReachedType": "workspace_member_credits_depleted"}})
        self.assertEqual(record["gate"], "blocked")

    def test_ordinary_usage_denied_with_spent_window_is_blocked(self):
        record = codexcli.classify_usage({"rateLimits": self.WEEKLY_SPENT, "ordinaryUsageAllowed": False})
        self.assertEqual(record["gate"], "blocked")

    def test_plain_exhaustion_and_provider_rate_limit_marker_stay_exhausted(self):
        self.assertEqual(codexcli.classify_usage({"rateLimits": self.WEEKLY_SPENT})["gate"], "exhausted")
        record = codexcli.classify_usage({"rateLimits": {**self.WEEKLY_SPENT, "rateLimitReachedType": "rate_limit_reached"},
                                          "ordinaryUsageAllowed": False})
        self.assertEqual(record["gate"], "exhausted")
        self.assertIn("weekly resets", record["gate_reason"])


class ParsingEdges(unittest.TestCase):
    def reply(self, **request):
        value = fake_codex.claim("extension_requested", turns=3, remaining=["finish"])
        value["extension_request"].update(request)
        return json.dumps(value)

    def test_extension_request_fields_are_type_checked(self):
        for change in ({"reason": None}, {"reason": 5}, {"completed_work": "text"}, {"remaining_work": [1]},
                       {"completed_work": None}):
            with self.subTest(change=change):
                claim, error = bridge.parse_claim(self.reply(**change))
                self.assertIsNone(claim)
                self.assertIn("extension_request", error)
        self.assertIsNotNone(bridge.parse_claim(self.reply())[0])

    def test_extension_of_never_raises_on_odd_requests(self):
        for request in ({"remaining_work": ["x"], "reason": None, "requested_turns": 3}, {"reason": "r"}, None, [],
                        {"remaining_work": ["x"], "reason": "r", "requested_turns": True},
                        {"remaining_work": ["x"], "reason": "r", "requested_turns": "3"}):
            with self.subTest(request=request):
                self.assertIsNone(bridge.extension_of({"status": "extension_requested", "extension_request": request}))
        good = {"remaining_work": ["x"], "reason": "r", "requested_turns": 3}
        self.assertEqual(bridge.extension_of({"status": "extension_requested", "extension_request": good}), good)

    def test_error_labels_need_whole_words_or_http_context(self):
        classify = bridge.classify_error
        for text in ("HTTP 429 Too Many Requests", "usage limit reached", "Rate limit exceeded", "status: 429",
                     "quota exhausted"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "rate_limit")
        for text in ("401 Unauthorized", "HTTP 401", "not logged in", "please log in again", "login required",
                     "unauthorized"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "authentication")
        for text in ("failed at line 4012 of the file", "listening on port 14290", "version 4.0.1 crashed",
                     "token 12401 expired in blogin", "exit code 1", "line 401 of foo.py"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "codex_error")
        self.assertIsNone(classify(""))

    def test_malformed_but_parseable_events_never_raise(self):
        lines = [{"type": "turn.completed", "usage": [1, 2]},
                 {"type": "turn.failed", "error": {"message": {"code": 5, "detail": "x"}}},
                 {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": True}},
                 {"type": "turn.failed", "error": "plain text"},
                 {"type": ["not", "a", "string"], "usage": 5}]
        parsed = bridge.parse_events("\n".join(json.dumps(line) for line in lines))
        self.assertEqual(parsed["usage"], {"input_tokens": 3})
        self.assertEqual(parsed["turns_completed"], 2)
        self.assertEqual(parsed["turn_failed"], "plain text")
        only_dict = bridge.parse_events(json.dumps(lines[1]))
        self.assertIsInstance(only_dict["turn_failed"], str)
        self.assertEqual(bridge.classify_error(only_dict["turn_failed"]), "codex_error")

    def test_declined_commands_and_turn_bookkeeping_come_from_the_exec_stream(self):
        """The shapes `codex exec --json` prints (codex-rs/exec exec_events.rs): a declined command has status
        "declined" and no exit code; nothing in the stream carries a reviewer verdict or reason."""
        stream = [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "curl x",
                                              "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "curl x",
                                                "aggregated_output": "", "exit_code": None, "status": "declined"}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "dir",
                                                "aggregated_output": "ok", "exit_code": 0, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution", "command": "rd /s x",
                                                "aggregated_output": "denied by policy", "exit_code": None,
                                                "status": "declined"}},
            {"type": "item.completed", "item": {"id": "item_3", "type": "file_change", "status": "failed",
                                                "changes": [{"path": "a.py", "kind": "update"}]}},
        ]
        parsed = bridge.parse_events("\n".join(json.dumps(line) for line in stream))
        self.assertEqual(parsed["declined"], [{"command": "curl x", "output": ""},
                                              {"command": "rd /s x", "output": "denied by policy"}])
        self.assertEqual([c["status"] for c in parsed["commands"]], ["declined", "completed", "declined"])
        self.assertEqual((parsed["turns_started"], parsed["turns_completed"], parsed["turns_failed"]), (1, 0, 0))
        finished = bridge.parse_events("\n".join(json.dumps(line) for line in [
            {"type": "turn.started"}, {"type": "turn.completed", "usage": {}},
            {"type": "turn.started"}, {"type": "turn.failed", "error": {"message": "x"}}]))
        self.assertEqual((finished["turns_started"], finished["turns_completed"], finished["turns_failed"]), (2, 1, 1))
        self.assertEqual(bridge.parse_events("")["declined"], [])

    def test_a_declined_command_is_not_a_forbidden_tool_and_does_not_fail_the_run_by_itself(self):
        parsed = bridge.parse_events(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "x", "status": "declined", "exit_code": None}}))
        self.assertEqual(parsed["forbidden_items"], [])

    def test_claim_fields_of_the_wrong_type_are_rejected_not_raised(self):
        base = fake_codex.claim()
        odd_check = [{"description": "d", "reported_outcome": ["passed"], "evidence": "e"}]
        for change in ({"status": ["complete"]}, {"status": {"a": 1}}, {"checks": odd_check}):
            with self.subTest(change=change):
                claim, error = bridge.parse_claim(json.dumps({**base, **change}))
                self.assertIsNone(claim)
                self.assertTrue(error)

    def test_a_claimed_file_name_that_cannot_be_a_path_counts_as_outside_the_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            inside, outside = bridge._normalized_claims(Path(tmp), ["ok.txt", "bad\0name.txt", "../escape.txt"])
        self.assertEqual(inside, {"ok.txt"})
        self.assertEqual(outside, ["bad\0name.txt", "../escape.txt"])


if __name__ == "__main__":
    unittest.main()
