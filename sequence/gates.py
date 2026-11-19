"""Gate checks. The runner runs these itself after each step and never trusts
the model's own report. Every gate returns (ok, message)."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import content

FEATURE_KEYS = {"id", "description", "steps", "built", "passes"}


@dataclass
class Ctx:
    run_dir: Path
    target: Path
    step_id: str
    attempt: int
    skill: str | None = None
    snapshot_dir: Path | None = None


def sh(cmd: str, cwd: Path, run_dir: Path, timeout: int = 600) -> tuple[int, str]:
    env = {**os.environ, "SEQ_RUN_DIR": str(run_dir), "SEQ_TARGET": str(cwd)}
    try:
        p = subprocess.run(
            ["bash", "-c", cmd], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    out = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode, "\n".join(out[-40:])


def load_features(run_dir: Path) -> list[dict]:
    return json.loads((run_dir / "features.json").read_text())["features"]


def skill_marker(skill: str) -> str:
    body = (content.plugin_dir() / "skills" / skill / "SKILL.md").read_text()
    m = re.search(r"SEQ-MARK-[A-Za-z0-9]+", body)
    if not m:
        raise ValueError(f"skill {skill} has no SEQ-MARK token")
    return m.group(0)


def g_bash(ctx: Ctx, cmd: str):
    rc, out = sh(cmd, ctx.target, ctx.run_dir)
    return rc == 0, f"bash `{cmd}` exit {rc}\n{out}"


def g_files(ctx: Ctx, names: list[str]):
    missing = [n for n in names if not (ctx.run_dir / n).is_file() or not (ctx.run_dir / n).stat().st_size]
    return not missing, f"missing or empty files in run folder: {missing}"


def g_skill_used(ctx: Ctx, _=None):
    token = skill_marker(ctx.skill)
    log = ctx.run_dir / "changelog.md"
    line = f"{token} step={ctx.step_id} attempt={ctx.attempt}"
    ok = log.is_file() and line in log.read_text()
    return ok, f"skill {ctx.skill} not used: changelog.md has no line `{token} step={ctx.step_id} attempt={ctx.attempt}`"


def g_features_schema(ctx: Ctx, _=None):
    try:
        feats = load_features(ctx.run_dir)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"features.json unreadable: {e}"
    if not isinstance(feats, list) or not feats:
        return False, "features.json needs a non-empty `features` list"
    ids = [f.get("id") for f in feats if isinstance(f, dict)]
    if len(ids) != len(feats) or len(set(ids)) != len(ids):
        return False, "feature ids must be unique"
    for f in feats:
        if set(f) != FEATURE_KEYS or not isinstance(f["steps"], list):
            return False, f"feature {f.get('id')} must have exactly keys {sorted(FEATURE_KEYS)}"
        if f["built"] is not False or f["passes"] is not False:
            return False, f"feature {f['id']} must start with built=false and passes=false"
    return True, ""


def g_features_only(ctx: Ctx, allowed: list[str]):
    before = json.loads((ctx.snapshot_dir / "features.json").read_text())["features"]
    try:
        after = load_features(ctx.run_dir)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"features.json unreadable after step: {e}"
    if [f.get("id") for f in before] != [f.get("id") for f in after]:
        return False, "feature list changed (ids added, removed or reordered)"
    for b, a in zip(before, after):
        changed = {k for k in FEATURE_KEYS | set(a) if b.get(k) != a.get(k)}
        if changed - set(allowed):
            return False, f"feature {b['id']}: fields {sorted(changed - set(allowed))} may not change in step {ctx.step_id}"
        if any(not isinstance(a[k], bool) for k in changed):
            return False, f"feature {b['id']}: {sorted(changed)} must be true or false"
    return True, ""


def g_all_built(ctx: Ctx, _=None):
    todo = [f["id"] for f in load_features(ctx.run_dir) if f["built"] is not True]
    return not todo, f"features not built yet: {todo}"


def g_all_pass(ctx: Ctx, _=None):
    bad = [f["id"] for f in load_features(ctx.run_dir) if f["passes"] is not True]
    return not bad, f"features not passing: {bad}"


def g_target_unchanged(ctx: Ctx, _=None):
    now = target_fingerprint(ctx.target)
    was = (ctx.snapshot_dir / "target.txt").read_text()
    return now == was, "tracked files or HEAD in the target changed during a review-only step"


NOISE = ("__pycache__", ".pytest_cache", ".sequence/", ".coverage", ".mypy_cache", ".ruff_cache", "htmlcov", ".egg-info")


def target_fingerprint(target: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=target, capture_output=True, text=True).stdout

    untracked = [ln for ln in git("status", "--porcelain", "--untracked-files=all").splitlines()
                 if ln.startswith("??") and not any(n in ln for n in NOISE)]
    return git("rev-parse", "HEAD") + git("diff", "HEAD") + "\n".join(untracked)


def g_frozen(ctx: Ctx, _=None):
    frozen = ctx.run_dir / "frozen"
    changed = [p.name for p in sorted(frozen.glob("*"))
               if not (ctx.run_dir / p.name).is_file() or (ctx.run_dir / p.name).read_bytes() != p.read_bytes()]
    return not changed, f"files frozen after an earlier step were changed (restored): {changed}"


def g_checklist(ctx: Ctx, path: str):
    items = yaml.safe_load(content.find(path).read_text())["items"]
    results_file = ctx.run_dir / "results.json"
    reviewed, evidence, notes = {}, {}, ""
    if results_file.is_file():
        results = json.loads(results_file.read_text())
        for r in results.get("items", []):
            reviewed[r["id"]] = r.get("status")
            evidence[r["id"]] = str(r.get("evidence") or "")[:600]
        notes = str(results.get("notes") or "")[:1500]
    fails = []
    for it in items:
        verify = it["verify"]
        if verify.startswith("bash:"):
            rc, out = sh(verify[5:].strip(), ctx.target, ctx.run_dir)
            if rc != 0:
                fails.append(f"{it['id']} {it['requirement']} (command exit {rc}: {out[-200:]})")
        elif reviewed.get(it["id"]) != "pass":
            # The step that fixes the work gets the reviewer's reasons, not just the verdict.
            why = f": {evidence[it['id']]}" if evidence.get(it["id"]) else ""
            fails.append(f"{it['id']} {it['requirement']} (review status: {reviewed.get(it['id'], 'missing')}{why})")
    message = "checklist failures:\n" + "\n".join(f"- {f}" for f in fails)
    if fails and notes:
        message += f"\nReviewer notes: {notes}"
    return not fails, message


GATES = {
    "bash": g_bash,
    "files": g_files,
    "skill_used": g_skill_used,
    "features_schema": g_features_schema,
    "features_only": g_features_only,
    "all_built": g_all_built,
    "all_pass": g_all_pass,
    "target_unchanged": g_target_unchanged,
    "checklist": g_checklist,
    "frozen": g_frozen,
}


def run_gates(ctx: Ctx, gates: list[dict]) -> list[str]:
    """Run every gate and return the failure messages (empty list means pass)."""
    failures = []
    for gate in gates:
        name = "?"
        try:
            (name, arg), = gate.items()
            ok, msg = GATES[name](ctx, arg)
        except Exception as e:  # a crashing or malformed gate must fail closed
            ok, msg = False, f"gate {name} crashed: {type(e).__name__}: {e}"
        if not ok:
            failures.append(f"[{name}] {msg}")
    return failures
