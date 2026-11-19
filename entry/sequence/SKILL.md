---
name: sequence
description: Run a saved multi-step sequence such as build-app through the sequence runner. Use only when the user types /sequence.
disable-model-invocation: true
argument-hint: list | run <name> "<brief>" [--target dir] | status <run_dir> | resume <run_dir>
---

You are a thin launcher. The runner does the work: it runs every step in a fresh Claude call and checks every gate itself. Never perform a step yourself, never edit the target project, never rerun a step by hand.

Runner command prefix: `cd ~/Projects/sequences && ./venv/bin/python -m sequence`

- `list`: run `<prefix> list` and show the output.
- `run <name> "<brief>"`: target is the folder the user names, otherwise `~/Projects/<kebab-case slug of the brief, at most 4 words>`. Run `<prefix> run <name> "<brief>" --target <dir>` in the background (a run takes 10 minutes or more) and wait for it to finish.
- `status <run_dir>`: run `<prefix> status <run_dir>`.
- `resume <run_dir>`: run `<prefix> resume <run_dir>`.

When a run ends, relay the printed summary: status, cost against budget, step table, feature counts, run folder path. Then:

- `done`: say where the project is and stop.
- `awaiting_approval`: show the summary and ask the user to approve. Only after the user replies yes, run `<prefix> resume <run_dir> --approve`. Never pass `--approve` on your own.
- `blocked`: show the reason and last failure. Do not fix the code and do not restart. Ask what the user wants.
- Runner error (exit 2 or a traceback): show it unchanged.
