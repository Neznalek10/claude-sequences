# sequences

Saved, repeatable, multi-step Claude Code runs with gates the model cannot talk its way past.

A sequence is a YAML file that lists steps, for example `spec`, `build`, `test`, `approve`. A small Python runner executes each step as its own fresh `claude -p` call, checks the result with its own gate code, retries a failed step with the failure attached, enforces attempt and budget caps, and keeps all state in a folder on disk. A human approves before anything is declared finished.

The goal is not to make Claude run longer. The goal is a more predictable result on a hard job, end to end, without losing quality, context or time.

Status: v0, one shipped sequence (`build-app`), 73 unit tests, verified with live runs. See [Limitations](#limitations).

## Contents

- [Why this exists](#why-this-exists)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Where your sequences live](#where-your-sequences-live)
- [Use it from inside Claude Code](#use-it-from-inside-claude-code)
- [CLI reference](#cli-reference)
- [The build-app sequence](#the-build-app-sequence)
- [Writing a sequence](#writing-a-sequence)
- [Gates](#gates)
- [Checklists](#checklists)
- [Step skills](#step-skills)
- [Run folder](#run-folder)
- [Budget and cost](#budget-and-cost)
- [Safety model](#safety-model)
- [Testing](#testing)
- [Measured behaviour of claude -p](#measured-behaviour-of-claude--p)
- [Limitations](#limitations)
- [Roadmap](#roadmap)
- [Repository layout](#repository-layout)
- [License](#license)

## Why this exists

Asking one long Claude session to "build an app" has predictable failure modes: the context fills with its own history, the model marks its own work as done, a skipped requirement is never noticed, and cost is unbounded. Sequences address those directly:

| Problem | What sequences do about it |
|---|---|
| Context rot over a long session | Every step is a fresh `claude -p` call. State lives on disk, not in the conversation. |
| The builder grades its own work | The builder may only set `built`. A separate reviewer step, in a fresh context and on a different model tier, is the only one allowed to set `passes`. |
| "Done" is claimed, not proven | The runner runs the gate commands itself after every step. The model's own report is never trusted. |
| A skipped or forgotten step | A step must prove it loaded its skill (marker token check). Required output files must exist. |
| Runaway cost or endless loops | Per-call turn and dollar limits, a run budget, and attempt caps. When they run out the run stops as `blocked`. |
| Moving the goalposts | The spec and the test script are frozen after the spec step. A later step that edits them fails and the files are restored. |
| Irreversible actions | The run ends at an approval step for a human. Nothing in v0 deploys or pushes. |

A hook is not used as the hard gate on purpose. A Stop hook can be bypassed (a crashed or timed-out callback counts as allow), so the enforcement lives in the runner, outside the model.

## How it works

```
 sequence run build-app "brief" --target ./my-app
            |
            v
   +-------------------+     fresh `claude -p` call per step
   |  runner (Python)  | --------------------------------------+
   +-------------------+                                       |
            ^                                                  v
            |  gate results, state                    +------------------+
            |                                         |  step skill      |
   run folder on disk  <------ files, changelog ------|  (spec/build/    |
   (.sequence/runs/...)                               |   test)          |
                                                      +------------------+

 spec  ->  build  ->  test  ->  approve (human)
             ^          |
             +----------+   a failed test sends the run back to build
```

1. **One call per step.** Each step runs `claude -p --setting-sources project --plugin-dir <repo>/plugin`. Your user hooks, plugins and `CLAUDE.md` do not leak in. The prompt starts with `/sequences:<skill>`, so the step skill is named explicitly instead of hoping a description matches.
2. **Gates run after the call.** The runner runs the step's gates itself (see [Gates](#gates)).
3. **Retry with the failure attached.** A failed gate retries the step with the failure text in the prompt. Defaults: 3 attempts per step, 8 for the build loop. A failed `test` step goes back to `build` (`on_fail`). When attempts run out the run stops as `blocked` with the reason in `summary.md`.
4. **Resume.** `resume` continues a stopped run. A blocked run gets a fresh set of attempts for the stuck step. Attempt numbers keep counting up so old artifacts are never overwritten.
5. **Approval.** The `approve` step stops the run. With a terminal it asks `y/N`. Without one (a background shell, CI) it exits with code 3 and waits for `resume --approve`.

## Requirements

- Python 3.10 or newer
- [Claude Code](https://code.claude.com) CLI installed and logged in (the runner shells out to `claude`)
- `git` (the target project is made a git repo if it is not one yet)
- Linux or macOS with `bash` (gate commands and the generated `check.sh` are bash)
- One Python dependency: `pyyaml`

No API key is needed. The runner uses whatever login your `claude` CLI already has.

## Install

```bash
git clone <this repository> ~/Projects/sequences
cd ~/Projects/sequences
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m sequence list
```

The runner finds `claude` on `PATH`, then falls back to the newest VS Code extension binary. Override with:

```bash
export SEQUENCE_CLAUDE_BIN=/path/to/claude
```

## Quick start

```bash
cd ~/Projects/sequences
./venv/bin/python -m sequence run build-app \
  "Build a Python CLI that converts temperatures between Celsius and Fahrenheit" \
  --target ~/Projects/temp-convert
```

The run takes roughly 5 to 15 minutes and, on the measured runs, cost about 0.7 to 1.2 USD in list-price terms (see [Budget and cost](#budget-and-cost)). It stops at the approval step:

```
Status: **awaiting_approval** (step approve needs a human)
Cost: $0.66 of $6.00

| Step | Status | Attempts | Cost |
|---|---|---|---|
| spec | done | 1 | $0.30 |
| build | done | 1 | $0.13 |
| test | done | 1 | $0.22 |
| approve | pending | 0 | $0.00 |

Features: 6 total, 6 built, 6 passing
```

Look at the result in the target folder and the run folder, then approve:

```bash
./venv/bin/python -m sequence resume ~/Projects/temp-convert/.sequence/runs/<run folder> --approve
```

## Where your sequences live

The runner code and the content you edit can live in different places. The editable parts are:

| Part | Folder in a content home | Bundled example in this repo |
|---|---|---|
| Sequence files | `sequences/<name>.yaml` | `sequences/` |
| Checklists | `checklists/<name>.yaml` | `checklists/` |
| Step skills (the shareable building blocks) | `blocks/` (a Claude Code plugin: `.claude-plugin/plugin.json` and `skills/<skill>/SKILL.md`) | `plugin/` |

A content home is any folder with that layout. The runner looks things up in the home first and falls back to the bundled examples, so it works with or without a home.

- `SEQUENCES_HOME=/path/to/home` selects a home. An empty value (`SEQUENCES_HOME=`) turns the home off and uses only the bundled examples.
- Without the variable the home is `~/.claude/sequences`, if that folder exists. This puts your sequences next to your other Claude Code skills and agents. It can be a symlink to a folder in a notes or dotfiles repository.
- A `blocks/` folder replaces the bundled `plugin/` as a whole when it contains `.claude-plugin/plugin.json`, so copy all the step skills you need into it.
- `python -m sequence list` prints where each sequence was found. A sequence with the same name in the home hides the bundled one.

To start your own home:

```bash
mkdir -p ~/.claude/sequences/{sequences,checklists}
cp sequences/build-app.yaml ~/.claude/sequences/sequences/
cp checklists/app-basics.yaml ~/.claude/sequences/checklists/
cp -r plugin ~/.claude/sequences/blocks
```

## Use it from inside Claude Code

`entry/sequence/SKILL.md` is a thin `/sequence` slash command. It has `disable-model-invocation: true`, so Claude only runs it when you type it. Its whole job is to launch the runner and relay the summary. It never performs a step itself, never edits the target project, and never passes `--approve` without your reply.

Set it up:

```bash
ln -s ~/Projects/sequences/entry/sequence ~/.claude/skills/sequence
```

If you cloned somewhere other than `~/Projects/sequences`, edit the runner command prefix at the top of `entry/sequence/SKILL.md`.

Optional but recommended: add one line to your global `~/.claude/CLAUDE.md` so multi-step jobs go through the runner instead of being improvised:

```
- Multi-step jobs that match a saved sequence (for example build-app) go through `/sequence run <name> "<brief>"`, never improvised step by step
```

Then in any Claude Code session:

```
/sequence list
/sequence run build-app "Build a todo CLI in Python" --target ~/Projects/todo
/sequence status <run folder>
/sequence resume <run folder>
```

Measured: a fresh `claude -p` session given `/sequence run ...` made two tool calls (start the runner, read its output), changed no project file and stopped at the approval prompt. Launcher cost was under one cent. The launcher does not guarantee behaviour on its own; the runner is what enforces it.

## CLI reference

```
python -m sequence list
python -m sequence run <name | path/to/file.yaml> "<brief>" --target <dir> [--budget USD]
python -m sequence status <run folder> [--json]
python -m sequence resume <run folder> [--approve | --reject] [--budget USD] [--json]
```

`run` also takes `--json`.

| Command | What it does |
|---|---|
| `list` | Prints the sequences in `sequences/`. |
| `run` | Creates the target folder (and a git repo in it if needed), creates a run folder, executes the steps. `<name>` is a file in `sequences/` without `.yaml`, or a path to any sequence file. `--budget` overrides the sequence's `budget_usd`. |
| `status` | Prints `summary.md` of a run. |
| `resume` | Continues a run that stopped (blocked, interrupted or waiting for approval). `--approve` answers yes to the next approval step only; a later approval step waits again for its own yes. `--reject` answers no, and the run stops as blocked. `--budget` sets a new total budget, useful after a budget block. |
| `--json` | For `run` and `resume`: prints one JSON line when the run folder exists (`{"event": "started", "run_dir": ...}`) and one at the end (`{"event": "finished", "status", "reason", "total_cost", "budget_usd", "exit"}`) instead of the summary. For `status`: the same fields from `run.json`. Made for programs that start runs, such as a scheduler. |

Exit codes: `0` done, `1` blocked, `2` bad input or setup error, `3` waiting for a human, `130` interrupted (Ctrl+C or SIGTERM; `run.json` is saved as `interrupted` and the run can be resumed).

Bad input (a missing folder, a corrupt `run.json`, a bad budget, an empty brief, no `claude` binary) exits `2` with an `error:` line and no traceback.

## The build-app sequence

`sequences/build-app.yaml` takes a short brief and produces a small working app in a git repo, tested by a separate reviewer.

| Step | Skill | Model | What it does | Main gates |
|---|---|---|---|---|
| `spec` | `seq-spec` | opus | Writes `spec.md`, `features.json` (3 to 8 independently testable features, all `built: false, passes: false`) and `check.sh` (one command that runs the whole test suite). Writes nothing into the target. | files exist, features schema valid, skill marker |
| `build` | `seq-build` | sonnet | Implements features in the target with tests and commits, creates `README.md` and `.gitignore`, runs `check.sh`, sets `built: true` per feature. May not set `passes`. | `features_only [built]`, `check.sh` passes, all built, frozen files untouched |
| `test` | `seq-test` | opus | Independent reviewer in a fresh context. Runs the real program from a temp directory, sets `passes` per feature, writes `results.json` for the review items. May not change the target. | `features_only [passes]`, `target_unchanged`, checklist, all pass |
| `approve` | (human) | none | Stops and shows the summary. | none |

If `test` fails, the run loops back to `build` with the failure text, up to the attempt caps. Default budget is 6 USD.

Example of a real loop: the reviewer found that bad input printed a traceback (checklist item APP-06), the runner sent the run back to `build`, and the second `test` passed.

## Writing a sequence

A sequence is a YAML file. Put it in `sequences/` of your [content home](#where-your-sequences-live) (or in this repository's `sequences/`), or pass its path to `run`.

```yaml
name: build-app
description: Spec, build and independently test a small app from a short brief, then stop for human approval.
budget_usd: 6        # total for the run
max_retries: 3       # default attempts per step
steps:
  - id: spec
    skill: seq-spec
    model: opus
    max_turns: 20
    max_budget_usd: 1.5
    freeze: [spec.md, check.sh]
    gate:
      - files: [spec.md, features.json, check.sh]
      - features_schema: true

  - id: build
    depends_on: [spec]
    skill: seq-build
    model: sonnet
    max_turns: 40
    max_budget_usd: 2
    loop: {max_iterations: 8}
    allowed_tools: [Read, Glob, Grep, Write, Edit, Bash]
    gate:
      - features_only: [built]
      - bash: 'bash "$SEQ_RUN_DIR/check.sh"'
      - all_built: true

  - id: test
    depends_on: [build]
    skill: seq-test
    model: opus
    on_fail: build
    allowed_tools: [Read, Glob, Grep, Write, Edit, Bash]
    gate:
      - features_only: [passes]
      - target_unchanged: true
      - checklist: checklists/app-basics.yaml
      - all_pass: true

  - id: approve
    wait: approval
```

### Top-level keys

| Key | Meaning |
|---|---|
| `name` | Name of the sequence. Used in the run folder name (sanitised). |
| `description` | Shown by `list`. |
| `budget_usd` | Total budget for the run. Default 6. |
| `max_retries` | Default attempt cap per step. Default 3. |
| `model` | Default model for steps without one. Default `sonnet`. |
| `steps` | Non-empty list, run in order. |

### Step keys

Each step has exactly one of `skill` or `wait`.

| Key | Meaning |
|---|---|
| `id` | Unique step name. |
| `skill` | Name of a skill in the content home's `blocks/skills/` (or the bundled `plugin/skills/`). The prompt starts with `/sequences:<skill>`. |
| `wait: approval` | Human approval step. No Claude call. |
| `model` | `opus`, `sonnet`, `haiku` or a full model id. |
| `max_turns` | `--max-turns` for the call. Default 30. |
| `max_budget_usd` | `--max-budget-usd` for the call, capped by what is left of the run budget. Default 2. |
| `timeout_min` | Wall clock timeout for the call. Default 20. |
| `allowed_tools` | Tools the step may use. Default `Read, Glob, Grep, Write, Edit`. |
| `loop: {max_iterations: N}` | Attempt cap for this step, replacing `max_retries`. |
| `on_fail` | Step to go back to when this step runs out of fixes, for example the `test` step uses `build`. Must name this or an earlier step. |
| `depends_on` | Earlier steps this one needs. Validated at load time. |
| `freeze` | Plain file names in the run folder to copy aside after this step passes. Every later step is checked against the copies. |
| `gate` | List of gates, one key per entry. |

The runner validates the file before any Claude call: unique ids, one of `skill` or `wait`, known gate names, plain file names in `freeze`, and `depends_on` and `on_fail` pointing backward.

## Gates

Every gate returns pass or fail with a message. All gates for a step run, and every failure is collected and fed to the retry. A gate that crashes fails closed. A malformed gate entry is rejected at load time.

| Gate | Argument | Passes when |
|---|---|---|
| `skill_used` | automatic, every step | The step wrote its marker line to `changelog.md` (see below). |
| `frozen` | automatic on steps without `freeze` | Files frozen by an earlier step are byte-identical. On failure they are restored. |
| `files` | list of names | Each file exists in the run folder and is not empty. |
| `bash` | command | The command exits 0. Runs in the target folder with `SEQ_RUN_DIR` and `SEQ_TARGET` set. stdin is closed. 600 s timeout. |
| `features_schema` | `true` | `features.json` has a non-empty list of features with exactly the keys `id, description, steps, built, passes`, unique ids, and `built` and `passes` both false. |
| `features_only` | list of fields | Only the listed fields changed since the step started, and they are real booleans. A tampered file is restored from the snapshot. |
| `all_built` | `true` | Every feature has `built` exactly `true`. |
| `all_pass` | `true` | Every feature has `passes` exactly `true`. |
| `target_unchanged` | `true` | Tracked files, HEAD and untracked files in the target are unchanged (cache folders such as `__pycache__`, `.pytest_cache`, `.coverage`, `htmlcov` are ignored). Used on review-only steps. |
| `checklist` | path | See [Checklists](#checklists). |

### How `skill_used` works

A slash-invoked skill in `claude -p` emits no `Skill` tool event, so the runner cannot see the call. Instead each step skill contains a marker token (`SEQ-MARK-...`) and must append `<token> step=<id> attempt=<n>` to `changelog.md` as its last action. A step that never loaded its skill has no reason to know the token. The attempt number is part of the line and attempt numbers never repeat, so a line from an earlier attempt cannot satisfy a later one.

The token sits in a file a step could read, so this checks that the skill was used. It is not a security boundary.

### Anti-tampering

- `features.json` is snapshotted before each step. If a step changes a field it may not change, the file is restored from the snapshot.
- `spec.md` and `check.sh` are frozen after `spec`. A builder that weakens the check script fails the `frozen` gate and the file is restored.
- `results.json` is moved aside before a step with a checklist gate, so a review result cannot be pre-written or reused from an earlier attempt.

## Checklists

A checklist is a YAML list of requirements with an id and a way to verify each one (`checklists/app-basics.yaml`):

```yaml
items:
  - id: APP-02
    requirement: The automated test suite passes (check.sh)
    verify: 'bash: bash "$SEQ_RUN_DIR/check.sh"'
  - id: APP-03
    requirement: Every feature in spec.md has at least one automated test
    verify: review
```

- `verify: "bash: <cmd>"` is run by the runner itself in the target. Its result does not depend on the model.
- `verify: review` is judged by the reviewer step, which writes pass or fail per id with a one-line evidence to `results.json`. This is the only place the runner takes the model's word, and only for items that cannot be checked mechanically.

The shipped checklist has six items: README exists, tests pass, every feature has a test, no committed secrets or key files, `.gitignore` and a commit exist, and bad input gives a clear error instead of a traceback.

## Step skills

Step skills ship as a Claude Code plugin in `plugin/` and are loaded only by the runner through `--plugin-dir`. Nothing is written to your `~/.claude` or to the target project.

```
plugin/
  .claude-plugin/plugin.json
  skills/
    seq-spec/SKILL.md
    seq-build/SKILL.md
    seq-test/SKILL.md
```

Each skill receives its context as arguments in the prompt:

```
/sequences:seq-build step=build attempt=2
RUN_DIR: <run folder>
TARGET: <target project>
STEP: build
ATTEMPT: 2
PREVIOUS FAILURES, fix these first:
<gate output of the last failed attempt>
```

To add a step skill, create `plugin/skills/seq-<name>/SKILL.md`, give it a unique `SEQ-MARK-<random>` token, tell it to append the marker line as its last action, and reference it from a sequence with `skill: seq-<name>`.

## Run folder

`<target>/.sequence/runs/<timestamp>-<name>/`. The `.sequence/` folder is added to `.git/info/exclude`, so it never ends up in the target's commits.

```
run.json          state: status, per-step attempts and cost, budget, source file
brief.md          the brief you gave
spec.md           written by spec, frozen afterwards
features.json     the feature list with built and passes flags
check.sh          one-command test script, frozen afterwards
checklist.yaml    copy of the checklist used by the test step
results.json      the reviewer's verdicts for the review items
changelog.md      marker lines and one runner line per attempt
errors.md         every gate failure, per step and attempt
decisions.md      assumptions the steps wrote down
summary.md        human-readable result, rewritten at every stop
frozen/           copies of frozen files
steps/<id>-<n>/   prompt.md, result.json, gate.log, features.json snapshot
```

`summary.md` ends every run. It lists status, cost against budget, the step table, feature counts and, for a blocked run, the last failure.

## Budget and cost

- Each call gets `--max-turns` and `--max-budget-usd`, the latter capped by what is left of the run budget.
- The runner adds up `total_cost_usd` from each call and stops with `blocked: budget exhausted` when less than 0.05 USD is left. `resume --budget N` raises the total.
- Both per-call limits can overshoot slightly (measured: `--max-turns 2` ran 3 turns, `--max-budget-usd 0.001` spent 0.002).
- `total_cost_usd` is a client-side estimate at API list prices. On a subscription login it is not an invoice.
- A call that times out or returns unreadable output is recorded as cost 0, so that spend is not counted.

Measured on live runs: a todo CLI cost 0.66 USD, a word-count CLI that needed one build/test loop cost 1.11 USD, and a deliberately failing gate cost 0.03 USD for three attempts.

## Safety model

- The runner, not the model, decides pass or fail. The model's own report is never trusted.
- Every step is started with a deny list: `Bash(rm:*)`, `Bash(sudo:*)`, `Bash(git push:*)`, `Bash(git reset --hard:*)`, `Bash(git clean:*)`. The deny list is added after the step's own `allowed_tools`, so it cannot be dropped by a sequence file.
- Permission mode is `dontAsk`: anything not in `allowed_tools` is refused rather than prompted.
- The brief is user text. It is written to a file and never placed in a shell command.
- Run folder names are sanitised, so a sequence name cannot escape `.sequence/runs/`.
- A sequence file and a step skill are prompts with tool access. Read them before running ones you did not write.
- Nothing in v0 pushes, deploys or touches anything outside the target and the run folder.

## Testing

```bash
./venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
```

73 tests. They use `tests/fake_claude.py`, a stand-in binary driven by a scenario file, so they cost no tokens and need no login. They cover: gates passing and failing, retry and cap behaviour, budget exhaustion, a missing skill marker, tampering with `features.json`, `check.sh` and `results.json`, resume after a block and after an interrupt, the approval step with and without a terminal, bad input to the CLI, custom sequence files and path safety.

Tests leave temporary folders under `/tmp/sequences-tests/`.

## Measured behaviour of claude -p

Findings that shaped the design are in `docs/spike.md`. Short version:

- `--bare` returned "Not logged in" with an OAuth login, so isolation uses `--setting-sources project`.
- A slash-invoked skill in print mode emits no `Skill` tool event, hence the marker token.
- `--max-turns` and `--max-budget-usd` stop with `error_max_turns` and `error_max_budget_usd`, and both overshoot a little.
- Plugin skills load from any working directory with `--plugin-dir` plus `--setting-sources project`.
- Without a closed stdin `claude -p` waits about 3 seconds, so the runner passes `stdin=DEVNULL`.

## Limitations

- Steps with the `Bash` tool can run any command that is not on the deny list. There is no sandbox yet.
- `approve` can be answered by anything that can run `resume --approve`. The `/sequence` launcher forbids doing that without your reply, but nothing enforces it.
- A gate command that starts a background process and leaves it running makes the gate wait for its 600 second timeout.
- Cost is an estimate, and timeouts are counted as zero.
- The `skill_used` token check proves skill use, not skill obedience.
- Only one sequence ships. There is no router, no nested sequences and no parallel steps.
- Not verified: how real `claude` resolves deny against allow in edge cases, and bypasses of the `Bash(rm:*)` prefix match.
- Linux tested. macOS should work, Windows is not supported (bash gates).

## Roadmap

Ideas, not built. The runner and gates in this repository are the foundation they would build on.

- A `SEQUENCE.md` format: YAML frontmatter plus a `# Steps` section with one `##` heading per step, each with a small machine-readable block and the step's prompt in prose.
- Shared step blocks that sequences include by reference with a pinned version and a frozen copy in the run folder, plus shared output-format blocks enforced by a gate.
- Parallel steps: one worker per unit of work in its own git worktree, a file-ownership check so units never overlap, and a merge step.
- Human checkpoint steps that loop back to a coding step with the reviewer's notes, with a loop cap.
- A `sequence check` lint command and more shipped sequences.

## Repository layout

```
sequence/            runner.py (loop, state, claude calls), gates.py, content.py (where content is found), __main__.py
sequences/           bundled example: build-app.yaml
checklists/          bundled example: app-basics.yaml
plugin/              bundled example step skills, loaded with --plugin-dir
entry/sequence/      the /sequence launcher skill for Claude Code
tests/               unit tests and fake_claude.py
docs/spike.md        what was measured about claude -p
requirements.txt     pyyaml
```

## License

MIT. See [LICENSE](LICENSE).
