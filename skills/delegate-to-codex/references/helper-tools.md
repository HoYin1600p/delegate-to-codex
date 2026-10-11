# Helper scripts

Four optional scripts in `$SKILL/scripts/`, standard library only. None starts Codex, none pushes, and none carries
project-specific text: everything per use is an argument.

## bridge_report.py

A compact summary of a result, from the bridge's saved stdout JSON, a `result.json`, an artifact folder, or
`--task T --latest`:

```bash
python -B "$SKILL/scripts/bridge_report.py" "<artifact>" --task "<task.json>" [--json]
python -B "$SKILL/scripts/bridge_report.py" --task "<task.json>" --latest
```

It prints the task id, lifecycle and validation status, the worker's summary, findings, blockers and checks,
failures, warnings (including `orphans_killed`), files changed with `+`/`-` counts, `review_binding` and the artifact
path. When validation failed it also prints the unique compiler errors (`path:line: error: message`, paths made
repository-relative), Gradle's "What went wrong" block and the failed test names, plus a revise skeleton. For a
`REVIEW_PENDING` or `IMPLEMENTED` result it prints the `accept` command with `--expect-tree` and
`--expect-patch-sha256` filled in.

## task_lint.py and task_template.py

```bash
python -B "$SKILL/scripts/task_lint.py" "<task.json>" [--strict] [--json]
python -B "$SKILL/scripts/task_template.py" --id my-task --mode implement --objective "..." \
    --allowed src/a.py "tests/**" --acceptance "observable result" --defaults project-defaults.json \
    --out "<task.json>" --validation python -B -m unittest
```

`task_lint.py` runs before `check-task`. It reports as errors a wildcard other than a trailing `/**`, a `:` or an
absolute path in `allowed_changed_paths`, `context_paths` or `copy_ignored`, `max_turns` outside 1-12,
`auto_continue` outside 0-3, allowed paths on a read-only task, and a `validation_command` that is not an argument
array; it also runs the bridge's own contract validation. It warns about a PowerShell command with an unquoted
`-Pa.b=c` option (PowerShell splits it at the dot), a Gradle command without `--no-daemon` (a leftover daemon can hold
the bridge's pipes), and TODO placeholders. Absolute paths in prose such as the objective are fine. Exit 0 means no
errors (`--strict` also fails on warnings).

`task_template.py` writes a starter task file and lints it. `--defaults` names a JSON file of reusable task fields kept
with the project (`locked_decisions`, `acceptance_criteria`, `stop_conditions`, `risk`, ...): list fields from the file
come first, the arguments are appended, scalar arguments override. `--validation` takes every argument after it, so put
it last, or pass `--validation-json '["prog", "arg"]'`. An existing `--out` file is kept unless `--force` is given.

## land.py

Integrates a reviewed result, then builds, compares and optionally commits, stopping at the first failing step:

```bash
python -B "$SKILL/scripts/land.py" --task "<task.json>" --artifact "<artifact>" \
    [--expect-tree TREE --expect-patch-sha256 SHA] [--apply] [--3way] \
    [--compare-listing "build/libs/*.jar=baseline.txt" --allow-added META-INF/new.txt] \
    [--compare-file produced.bin=baseline.bin] [--commit "message"] [--json] \
    [--build gradlew.bat build --no-daemon]
```

- Without `--apply` it runs the bridge's `accept`, bound to `--expect-tree` and `--expect-patch-sha256` (pass the
  values you reviewed; if omitted, the artifact's own record is used). With `--apply` it `git apply`s the artifact's
  `diff.patch` after a check, for a blocked or partial result, and ends with the bridge's `cleanup`.
- `--build` must be the last option; `--build-json` takes the array instead. It runs in the repository.
- `--compare-listing` diffs the sorted zip entry list of the one file matching the glob against a baseline listing
  (one entry per line); added entries must be named with `--allow-added`, removed ones always fail.
  `--compare-file` compares bytes.
- `--commit` stages and commits only the changed paths the artifact reports. Nothing is pushed.
