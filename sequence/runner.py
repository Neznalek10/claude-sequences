"""Sequence runner. Reads a YAML sequence and runs each step as one fresh
`claude -p` call. State lives in a run folder on disk. The runner runs the
gates itself, retries a failed step with the failure attached, and stops
when attempts or the budget run out."""

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from . import content
from .gates import GATES, Ctx, load_features, run_gates, target_fingerprint

REPO = content.REPO
PLUGIN = "sequences"
DEFAULT_ALLOWED = ["Read", "Glob", "Grep", "Write", "Edit"]
DEFAULT_DENY = [
    "Bash(rm:*)", "Bash(sudo:*)", "Bash(git push:*)",
    "Bash(git reset --hard:*)", "Bash(git clean:*)",
]
DEFAULT_STEP_BUDGET = 2.0
EXIT_CODES = {"done": 0, "blocked": 1, "awaiting_approval": 3, "interrupted": 130}


def find_claude() -> str:
    explicit = os.environ.get("SEQUENCE_CLAUDE_BIN")
    if explicit:
        return explicit
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    found = list(Path.home().glob(".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"))
    if not found:
        raise ValueError("claude binary not found: put it on PATH or set SEQUENCE_CLAUDE_BIN")
    version = lambda p: [int(x) for x in re.search(r"code-(\d+(?:\.\d+)*)", str(p)).group(1).split(".")]
    return str(max(found, key=version))


def validate(seq: dict) -> None:
    if not isinstance(seq, dict) or not seq.get("steps"):
        raise ValueError("sequence needs a non-empty `steps` list")
    seen: list[str] = []
    for s in seq["steps"]:
        if s["id"] in seen:
            raise ValueError(f"duplicate step id {s['id']}")
        if ("skill" in s) == ("wait" in s):
            raise ValueError(f"step {s['id']} needs exactly one of `skill` or `wait`")
        freeze = s.get("freeze", [])
        if not isinstance(freeze, list) or any(not isinstance(f, str) or Path(f).name != f for f in freeze):
            raise ValueError(f"step {s['id']}: freeze must be a list of plain file names")
        for g in s.get("gate") or []:
            if not isinstance(g, dict) or len(g) != 1 or next(iter(g)) not in GATES:
                raise ValueError(f"step {s['id']} has an invalid gate entry: {g!r}")
        for d in s.get("depends_on", []):
            if d not in seen:
                raise ValueError(f"step {s['id']} depends on unknown or later step {d}")
        if s.get("on_fail") and s["on_fail"] not in seen + [s["id"]]:
            raise ValueError(f"step {s['id']} on_fail must name this or an earlier step")
        seen.append(s["id"])


def sequence_path(name: str) -> Path:
    return Path(name) if name.endswith((".yaml", ".yml")) else content.find(f"sequences/{name}.yaml")


def check_budget(value: float | None) -> None:
    if value is not None and not 0 < value < math.inf:
        raise ValueError("budget must be a positive number")


def load_sequence(name: str) -> dict:
    path = sequence_path(name)
    seq = yaml.safe_load(path.read_text())
    validate(seq)
    return seq


def ensure_git(target: Path) -> None:
    if not (target / ".git").exists():
        subprocess.run(["git", "init", "-b", "main", "-q"], cwd=target, check=True)
    exclude = target / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if ".sequence/" not in text:
        exclude.write_text(text + ".sequence/\n")


def new_run(seq: dict, brief: str, target: Path, budget: float | None, source: str = "") -> tuple[Path, dict]:
    check_budget(budget)
    if not brief.strip():
        raise ValueError("brief is empty")
    target.mkdir(parents=True, exist_ok=True)
    ensure_git(target)
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", str(seq["name"])).strip(".") or "run"
    run_dir = target / ".sequence" / "runs" / f"{datetime.now():%Y%m%dT%H%M%S}-{slug}"
    (run_dir / "steps").mkdir(parents=True)
    (run_dir / "brief.md").write_text(brief + "\n")
    state = {
        "name": seq["name"], "source": source or seq["name"], "target": str(target), "run_dir": str(run_dir),
        "budget_usd": seq.get("budget_usd", 6.0) if budget is None else budget, "total_cost": 0.0,
        "status": "running", "reason": "", "last_failure": "",
        "steps": {s["id"]: {"status": "pending", "attempts": 0, "cost": 0.0} for s in seq["steps"]},
    }
    save_state(run_dir, state)
    return run_dir, state


def save_state(run_dir: Path, state: dict) -> None:
    (run_dir / "run.json").write_text(json.dumps(state, indent=2))


def build_cmd(claude: str, seq: dict, step: dict, prompt: str, budget: float) -> list[str]:
    return [
        claude, "-p", prompt,
        "--model", step.get("model", seq.get("model", "sonnet")),
        "--output-format", "json",
        "--setting-sources", "project",
        "--plugin-dir", str(content.plugin_dir()),
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--max-turns", str(step.get("max_turns", 30)),
        "--max-budget-usd", f"{budget:.2f}",
        "--allowedTools", *step.get("allowed_tools", DEFAULT_ALLOWED),
        "--disallowedTools", *DEFAULT_DENY,
    ]


def call_claude(cmd: list[str], cwd: Path, timeout_s: int) -> dict:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"subtype": "timeout", "is_error": True, "total_cost_usd": 0.0, "result": f"timed out after {timeout_s}s"}
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"subtype": "bad_output", "is_error": True, "total_cost_usd": 0.0, "result": (p.stdout + p.stderr)[-500:]}


def make_prompt(step: dict, run_dir: Path, target: Path, attempt: int, last_failure: str) -> str:
    head = f"/{PLUGIN}:{step['skill']} step={step['id']} attempt={attempt}\n"
    head += f"RUN_DIR: {run_dir}\nTARGET: {target}\nSTEP: {step['id']}\nATTEMPT: {attempt}\n"
    if last_failure:
        head += f"PREVIOUS FAILURES, fix these first:\n{last_failure}\n"
    return head


def cap_for(seq: dict, step: dict) -> int:
    return step.get("loop", {}).get("max_iterations") or seq.get("max_retries", 3)


def out_of_attempts(seq: dict, step: dict, st: dict) -> bool:
    return st["attempts"] - st.get("cap_base", 0) >= cap_for(seq, step)


def attempt(seq: dict, state: dict, run_dir: Path, step: dict, claude: str) -> bool:
    target = Path(state["target"])
    st = state["steps"][step["id"]]
    st["attempts"] += 1
    n = st["attempts"]
    save_state(run_dir, state)
    sdir = run_dir / "steps" / f"{step['id']}-{n}"
    sdir.mkdir(parents=True, exist_ok=True)
    if (run_dir / "features.json").is_file():
        shutil.copy(run_dir / "features.json", sdir / "features.json")
    gates = list(step.get("gate") or [])
    if "freeze" not in step:
        gates.append({"frozen": True})
    for g in gates:
        if "checklist" in g:
            shutil.copy(content.find(g["checklist"]), run_dir / "checklist.yaml")
            if (run_dir / "results.json").is_file():
                shutil.move(run_dir / "results.json", sdir / "results-before.json")
    if any("target_unchanged" in g for g in gates):
        (sdir / "target.txt").write_text(target_fingerprint(target))
    prompt = make_prompt(step, run_dir, target, n, state["last_failure"])
    (sdir / "prompt.md").write_text(prompt)
    budget = min(step.get("max_budget_usd", DEFAULT_STEP_BUDGET), state["budget_usd"] - state["total_cost"])
    result = call_claude(build_cmd(claude, seq, step, prompt, budget), target, step.get("timeout_min", 20) * 60)
    (sdir / "result.json").write_text(json.dumps(result, indent=2))
    cost = float(result.get("total_cost_usd") or 0)
    st["cost"] += cost
    state["total_cost"] += cost

    ctx = Ctx(run_dir, target, step["id"], n, step["skill"], sdir)
    failures = run_gates(ctx, [{"skill_used": True}] + gates)
    if failures and result.get("subtype") != "success":
        failures.insert(0, f"[claude] step ended with {result.get('subtype')}: {str(result.get('result'))[:300]}")
    if any(f.startswith("[features_only]") for f in failures) and (sdir / "features.json").is_file():
        shutil.copy(sdir / "features.json", run_dir / "features.json")
    if any(f.startswith("[frozen]") for f in failures):
        for kept in (run_dir / "frozen").glob("*"):
            shutil.copy(kept, run_dir / kept.name)
    if not failures:
        for name in step.get("freeze", []):
            (run_dir / "frozen").mkdir(exist_ok=True)
            shutil.copy(run_dir / name, run_dir / "frozen" / name)
    (sdir / "gate.log").write_text("\n".join(failures) or "all gates passed")
    verdict = "ok" if not failures else "failed"
    with (run_dir / "changelog.md").open("a") as f:
        f.write(f"- runner: step {step['id']} attempt {n} {verdict} (cost ${cost:.4f})\n")
    if failures:
        with (run_dir / "errors.md").open("a") as f:
            f.write(f"## {step['id']} attempt {n}\n" + "\n".join(failures) + "\n\n")
    state["last_failure"] = "\n".join(failures)[:3000]
    return not failures


def write_summary(seq: dict, state: dict, run_dir: Path) -> str:
    lines = [
        f"# Run summary: {state['name']}", "",
        f"Status: **{state['status']}**" + (f" ({state['reason']})" if state["reason"] else ""),
        f"Cost: ${state['total_cost']:.2f} of ${state['budget_usd']:.2f}",
        f"Target: {state['target']}", f"Run folder: {run_dir}", "",
        "| Step | Status | Attempts | Cost |", "|---|---|---|---|",
    ]
    for s in seq["steps"]:
        st = state["steps"][s["id"]]
        lines.append(f"| {s['id']} | {st['status']} | {st['attempts']} | ${st['cost']:.2f} |")
    try:
        feats = load_features(run_dir)
        lines += ["", f"Features: {len(feats)} total, {sum(f['built'] for f in feats)} built, {sum(f['passes'] for f in feats)} passing"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if state["status"] == "blocked" and state["last_failure"]:
        lines += ["", "Last failure:", "```", state["last_failure"], "```"]
    if state["status"] in ("blocked", "awaiting_approval", "interrupted"):
        extra = {"awaiting_approval": " --approve", "blocked": " (add --budget N if the budget ran out)"}.get(state["status"], "")
        lines += ["", f"Resume: python -m sequence resume {run_dir}{extra}"]
    text = "\n".join(lines) + "\n"
    (run_dir / "summary.md").write_text(text)
    return text


def finish(seq: dict, state: dict, run_dir: Path, status: str, reason: str = "") -> str:
    state["status"], state["reason"] = status, reason
    save_state(run_dir, state)
    return status


def prepare_resume(seq: dict, state: dict) -> None:
    """A blocked run gets fresh attempts for the step it stopped at. When that step sends
    failures back to an earlier one (`on_fail`), the fix runs first: re-checking unchanged
    work would only fail again."""
    steps = seq["steps"]
    index = {s["id"]: i for i, s in enumerate(steps)}
    for s in steps:
        st = state["steps"][s["id"]]
        if "wait" in s:
            continue
        stuck = state.get("status") == "blocked" and st["status"] == "failed"
        if stuck or out_of_attempts(seq, s, st):
            st["cap_base"] = st["attempts"]
        back = s.get("on_fail")
        if stuck and back and back != s["id"]:
            for later in steps[index[back]:]:
                if "wait" not in later:
                    state["steps"][later["id"]]["status"] = "pending"


def tty_input(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise EOFError
    return input(prompt)


def answer_once(answer: str):
    """Answers the first approval step only; the next one waits again, so every approval
    step needs its own yes."""
    given = []

    def ask(_prompt: str) -> str:
        if given:
            raise EOFError
        given.append(answer)
        return answer

    return ask


def result_json(state: dict, run_dir: Path) -> dict:
    return {
        "run_dir": str(run_dir), "status": state["status"], "reason": state["reason"],
        "total_cost": round(state["total_cost"], 4), "budget_usd": state["budget_usd"],
        "exit": EXIT_CODES.get(state["status"], 2),
    }


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def _terminate(_signum, _frame):
    raise KeyboardInterrupt


def execute(seq: dict, state: dict, run_dir: Path, claude: str, input_fn=tty_input, quiet: bool = False) -> str:
    steps = seq["steps"]
    index = {s["id"]: i for i, s in enumerate(steps)}
    i = next((k for k, s in enumerate(steps) if state["steps"][s["id"]]["status"] != "done"), len(steps))
    state["status"], state["reason"] = "running", ""
    while i < len(steps):
        step = steps[i]
        st = state["steps"][step["id"]]
        if "wait" in step:
            text = write_summary(seq, state, run_dir)
            if not quiet:
                print(text)
            try:
                answer = input_fn("Approve and finish? [y/N] ").strip().lower()
            except EOFError:
                return finish(seq, state, run_dir, "awaiting_approval", f"step {step['id']} needs a human")
            if answer != "y":
                return finish(seq, state, run_dir, "blocked", f"step {step['id']} not approved")
            st["status"] = "done"
            i += 1
            continue
        if state["budget_usd"] - state["total_cost"] < 0.05:
            return finish(seq, state, run_dir, "blocked", "budget exhausted")
        if out_of_attempts(seq, step, st):
            return finish(seq, state, run_dir, "blocked", f"step {step['id']} used all {cap_for(seq, step)} attempts")
        ok = attempt(seq, state, run_dir, step, claude)
        if ok:
            st["status"] = "done"
            i += 1
        else:
            st["status"] = "failed"
            if out_of_attempts(seq, step, st):
                save_state(run_dir, state)
                return finish(seq, state, run_dir, "blocked", f"step {step['id']} used all {cap_for(seq, step)} attempts")
            back = index[step.get("on_fail", step["id"])]
            for s in steps[back:]:
                if s is not step:
                    state["steps"][s["id"]]["status"] = "pending"
            i = back
        save_state(run_dir, state)
    return finish(seq, state, run_dir, "done")


def list_sequences() -> str:
    found: dict[str, str] = {}
    for root in reversed(content.roots()):
        for p in sorted((root / "sequences").glob("*.yaml")):
            seq = yaml.safe_load(p.read_text())
            found[seq["name"]] = f"{seq['name']}: {seq.get('description', '')}\n    {p}"
    return "\n".join(found.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sequence")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("brief")
    r.add_argument("--target", required=True)
    r.add_argument("--budget", type=float)
    r.add_argument("--json", action="store_true", help="print JSON lines (started, finished) instead of the summary")
    rs = sub.add_parser("resume")
    rs.add_argument("--budget", type=float, help="new total budget in USD")
    rs.add_argument("run_dir")
    decide = rs.add_mutually_exclusive_group()
    decide.add_argument("--approve", action="store_true", help="say yes to the next approval step (only that one)")
    decide.add_argument("--reject", action="store_true", help="say no to the next approval step; the run stops as blocked")
    rs.add_argument("--json", action="store_true", help="print JSON lines (started, finished) instead of the summary")
    st = sub.add_parser("status")
    st.add_argument("run_dir")
    st.add_argument("--json", action="store_true", help="print the run state as JSON")
    sub.add_parser("list")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        print(list_sequences())
        return 0
    try:
        if args.cmd == "status":
            if args.json:
                run_dir = Path(args.run_dir).resolve()
                emit(result_json(json.loads((run_dir / "run.json").read_text()), run_dir))
            else:
                print((Path(args.run_dir) / "summary.md").read_text())
            return 0
        claude = find_claude()
        if not (shutil.which(claude) or Path(claude).is_file()):
            raise ValueError(f"claude binary not found: {claude}")
        if args.cmd == "run":
            seq = load_sequence(args.name)
            run_dir, state = new_run(
                seq, args.brief, Path(args.target).expanduser().resolve(), args.budget, str(sequence_path(args.name).resolve()))
            input_fn = tty_input
        else:
            run_dir = Path(args.run_dir).resolve()
            state = json.loads((run_dir / "run.json").read_text())
            seq = load_sequence(state.get("source") or state["name"])
            if set(state["steps"]) != {s["id"] for s in seq["steps"]}:
                raise ValueError("run.json steps do not match the sequence file")
            check_budget(args.budget)
            if args.budget is not None:
                state["budget_usd"] = args.budget
            prepare_resume(seq, state)
            input_fn = answer_once("y") if args.approve else answer_once("n") if args.reject else tty_input
    except (ValueError, KeyError, TypeError, AttributeError, OSError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        emit({"event": "started", "run_dir": str(run_dir)})
    # A service stopping the runner (SIGTERM) leaves the run resumable, like Ctrl+C.
    signal.signal(signal.SIGTERM, _terminate)
    try:
        status = execute(seq, state, run_dir, claude, input_fn, quiet=args.json)
    except KeyboardInterrupt:
        status = finish(seq, state, run_dir, "interrupted", "stopped by Ctrl+C or a signal")
        print(f"interrupted. Resume: python -m sequence resume {run_dir}", file=sys.stderr)
    text = write_summary(seq, state, run_dir)
    if args.json:
        emit({"event": "finished", **result_json(state, run_dir)})
    else:
        print(text)
    return EXIT_CODES[status]
