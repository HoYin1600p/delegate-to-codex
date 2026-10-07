# delegate-to-codex

A Claude Code skill that makes Claude the lead and Codex the worker. Claude settles the requirements, design and scope, writes one task file, and reviews and accepts the result. A bounded Codex CLI worker, running on your ChatGPT subscription, writes the code and its tests in its own Git worktree and sandbox.

The aim is to spend Claude's tokens on judgment and Codex's on typing: Claude never pre-writes the code, reads a compact run report instead of transcripts, and sends failures back to the same Codex session with concrete findings.

## How a task runs

1. **Assign.** Claude writes one task file outside the repository: objective, the few files to read first, the exact files the worker may change, acceptance criteria, and a validation command. `check-task` validates it without starting Codex.
2. **Run.** `codex_bridge.py run` checks sign-in and plan usage, creates a worktree from the task's base commit, and runs the Codex worker in its sandbox with a step budget. The lead decides how many workers to run: independent tasks with non-overlapping files can run in parallel. If capacity is exhausted, unknown or blocked, nothing starts and the command exits with code 3.
3. **Report.** The bridge runs the validation command itself, outside the sandbox, and prints one compact report: status, changed paths, a per-file diff summary (after a revision, only that round's changes), the worker's findings, validation result, and tokens and usage. The report also carries the patch's tree id and checksum so acceptance can be pinned to what was reviewed. `show-diff` prints chosen files or just the last round.
4. **Accept or revise.** When validation passes, every changed path was allowed and the diff is right, `accept` applies the reviewed patch to the real checkout (never committing) and removes the worker's worktree and branch. Otherwise `revise` sends findings back to the same session, and `cleanup` discards rejected work. A validation that only timed out can be re-run with a longer limit using `revalidate --timeout`.
5. **Recover.** Every run leaves a record from its first moment, so a killed run can be cleared (`clear-stale-lock`) and cleaned up, and an interrupted continuation or revision can be resumed or discarded.

Exit codes: 0 ran, 1 the task or an apply check failed, 2 the bridge refused or hit an error, 3 capacity paused, 4 a setting must be chosen first (`settings_required`, see [Auto-review](#auto-review)).

The full workflow is in [`skills/delegate-to-codex/SKILL.md`](skills/delegate-to-codex/SKILL.md). The reference pages cover [first-use setup](skills/delegate-to-codex/references/setup.md), the [task file](skills/delegate-to-codex/references/task-file.md), [results and exit codes](skills/delegate-to-codex/references/results.md), the [recovery workflow](skills/delegate-to-codex/references/codex-workflow.md), the [safety model](skills/delegate-to-codex/references/safety.md) and the [routing policy](skills/delegate-to-codex/references/routing-policy.md).

## Safety in short

- Workers run in Codex's sandbox with network, web search and subagents off, and with a scrubbed environment (credentials are dropped).
- The validation command runs outside that sandbox, as your user, on code the worker wrote, the way CI would. Read the diff before you accept, and delegate only repositories whose tests you would run yourself. [Details](skills/delegate-to-codex/references/safety.md).
- Files a worker hides from the patch by creating git-ignored files fail the result; changes to the primary repository's hooks or to the Git settings that run code (aliases, filters, `core.hooksPath` and the like) fail it too, and Python validation can only run the sources that are in the patch.
- API-key billing is refused: the bridge only runs on a ChatGPT sign-in and stops if a billing override is set.

## Auto-review

Codex can route a worker's requests to go beyond its sandbox (a command that needs more access, a network call the sandbox blocks, a write outside its folders) to a reviewer model instead of refusing them. Codex calls this auto-review or "Approve for me". It is your choice: the installer asks, and until you have chosen, `run`, `continue` and `revise` refuse with `settings_required` (exit 4) before creating anything, naming the question and the command that records the answer. It does not widen the sandbox, and the bridge's own checks and your review still apply; a model makes the call, so mistakes are possible. A task can opt out with `"auto_review": false`. The report shows the choice and its source, and the commands Codex declined. [Details](skills/delegate-to-codex/references/safety.md#auto-review).

```
python skills/delegate-to-codex/scripts/codex_bridge.py settings show
python skills/delegate-to-codex/scripts/codex_bridge.py settings set auto-review on     # or off
```

## Requirements

- **Claude Code** on native Windows 10/11. This is the verified target; see [PLATFORMS.md](skills/delegate-to-codex/PLATFORMS.md) for porting to macOS.
- **Python 3.11+** and **Git**. No pip packages are needed.
- **Codex CLI** signed in with a ChatGPT plan: the standalone CLI or the one bundled with the Codex desktop app.

## Install

Clone or download the whole repository, then run:

```
python install.py
```

This copies only the skill's files (the skill, its readme and licence, references, scripts, schemas and source; not the tests) to `~/.claude/skills/delegate-to-codex` (or `$CLAUDE_CONFIG_DIR/skills/...`) and refuses to overwrite an existing installation; `--destination` picks another folder. On a terminal it then asks the auto-review question (answer `y` or `n`; anything else asks again) and records the answer through the installed bridge. `--auto-review on|off` records it without asking (also `true`, `false`, `enabled`, `disabled`), for unattended installs. Without a terminal and without the flag it leaves the choice unset and the first run asks. Restart Claude Code, then ask it to use the delegate-to-codex skill and run first-use setup. That runs:

- `scripts/setup.py self-test`: builds a throwaway Git fixture and checks the launcher, schemas and command construction offline. Status `passed`.
- `scripts/setup.py doctor --offline`: checks Python, platform, Git and the bundled files. Status `installation_ready`.
- `scripts/setup.py doctor`: also checks the Codex sign-in and capacity without generating model work. Status `codex_ready`.

Any failed check gives `setup_required` (exit 2) with a next action per check. That status also appears when plan usage is exhausted, blocked or unknown; wait for the reset rather than reinstalling. `doctor` also reports whether other accounts can read the state folder (`state_permissions`, a warning only).

## Privacy

No accounts, credentials, sessions, usage caches or work artifacts are included. Runtime state lives outside every Git repository, in `~/.claude/delegate-to-codex-state/` by default, or wherever `DELEGATE_TO_CODEX_STATE_DIR` points. Keep it out of Git and release archives: live worker output can contain private data.

## Tests

```
python -B -m unittest discover -s tests -v
cd skills/delegate-to-codex
python -B -m unittest discover -s tests
```

The first command tests the installer. The second runs the bridge's own suite against a fake Codex, with no network access and no model calls (it takes a few minutes). Both commands are the same in bash, PowerShell and `cmd`.

## History

This replaces the earlier `codex-delegation-skills` repository, where Codex led and Claude implemented. Running it the other way round turned out to be more efficient.

## License

[MIT](LICENSE)
