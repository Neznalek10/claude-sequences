"""Stand-in for the claude binary. Reads a scenario file, performs the scripted
side effects for the step named in the prompt, and prints a result JSON."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sequence import content  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    prompt = argv[argv.index("-p") + 1]
    skill = re.match(r"/sequences:(\S+)", prompt).group(1)
    ctx = dict(re.findall(r"^(RUN_DIR|TARGET|STEP|ATTEMPT): (.*)$", prompt, re.M))
    run_dir, target = Path(ctx["RUN_DIR"]), Path(ctx["TARGET"])
    step, attempt = ctx["STEP"], int(ctx["ATTEMPT"])
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"argv": argv, "step": step, "attempt": attempt, "prompt": prompt}) + "\n")

    plans = json.load(open(os.environ["FAKE_SCENARIO"])).get(step, [{}])
    act = plans[min(attempt - 1, len(plans) - 1)]

    for rel, text in act.get("run_files", {}).items():
        (run_dir / rel).write_text(text)
    for rel, text in act.get("target_files", {}).items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if act.get("commit"):
        subprocess.run(["git", "add", "-A"], cwd=target)
        subprocess.run(["git", "commit", "-qm", "fake"], cwd=target)
    if "set" in act:
        path = run_dir / "features.json"
        data = json.loads(path.read_text())
        for feature in data["features"]:
            feature.update(act["set"].get(feature["id"], act["set"].get("*", {})))
        path.write_text(json.dumps(data))
    if act.get("marker", True):
        body = (content.plugin_dir() / "skills" / skill / "SKILL.md").read_text()
        token = re.search(r"SEQ-MARK-[A-Za-z0-9]+", body).group(0)
        with (run_dir / "changelog.md").open("a") as f:
            f.write(f"{token} step={step} attempt={attempt}\n")

    subtype = act.get("subtype", "success")
    print(json.dumps({
        "subtype": subtype, "is_error": subtype != "success",
        "result": "ok", "total_cost_usd": act.get("cost", 0.1),
    }))
    return 0 if subtype == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
