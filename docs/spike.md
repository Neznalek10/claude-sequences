# Spike results (2026-11-16, Claude Code 2.1.325, haiku, tiny prompts)

Three unknowns from the plan, plus two extras that changed the design.

| # | Question | Result | Design impact |
|---|---|---|---|
| 1 | Does `--bare` still allow `/skill-name`? | No. `--bare` returns `Not logged in` (exit 1, `is_error: true`) on this machine because it skips keychain/OAuth reads. | Do not use `--bare`. Use `--setting-sources project` instead: it drops user hooks, plugins, skills and user CLAUDE.md (init event lists only project and built-in skills), so each step starts clean and cheaper (0.0019 vs 0.0050 USD for the probe). |
| 2 | Stream-json shape for a skill call | A slash-invoked skill in `-p` (`/seq-probe go`) loads the skill but emits **no Skill tool_use event**. Events seen: `system init` (lists skills), `assistant` thinking, `assistant` text, `rate_limit_event`, `result`. | The planned `skill_used` gate cannot read a tool call. Replaced by a marker token that exists only inside the SKILL.md body. The step must write `<token> step=<id> attempt=<n>` to `changelog.md`; the runner finds the token in the skill file and checks for the line. |
| 3 | `--max-turns` / `--max-budget-usd` stops | Both exit 1 with JSON `subtype` `error_max_turns` / `error_max_budget_usd` and `terminal_reason` `max_turns` / `budget_exhausted`. Limits overshoot: `--max-turns 2` ran 3 turns; `--max-budget-usd 0.001` spent 0.0020. | Runner treats any non-`success` subtype as a failed attempt, sums real `total_cost_usd`, and checks the run budget before every call. Caps are guard rails, not exact. |
| 4 | Plugin skills from another cwd | `--plugin-dir <dir>` plus `--setting-sources project` works from any cwd: `/seqplug:plug-probe` loaded. | Step skills ship as a plugin inside the repo (`plugin/skills/seq-*`), invoked as `/sequences:seq-build`. Nothing is written into the target project or `~/.claude`. |
| 5 | `--json-schema` | `structured_output` is returned (`{"verdict":"pass"}`) and `result` holds the same JSON string. | Not needed in v0 (gates read files), kept for later steps. |
| 6 | Multi-line prompt after the slash command | `/seqplug:plug-probe step=build attempt=2` followed by `RUN_DIR: ...` lines reaches the skill as its arguments (probe echoed RUN_DIR and ATTEMPT back). | Step prompt format is safe. |
| 7 | stdin | Without redirected stdin, `claude -p` waits 3s (`no stdin data received in 3s`). | Runner passes `stdin=subprocess.DEVNULL`. |

Not checked: exit codes other than 0 and 1, `stop_hook_active` on Stop input, hooks under `--settings`.
