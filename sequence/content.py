"""Where the editable parts of a sequence live: sequence files, checklists and
step skills. They are looked up in the content home first, then in the examples
bundled with this repository, so the runner works with or without a home.

Content home: $SEQUENCES_HOME if set (an empty value turns the home off),
otherwise ~/.claude/sequences. Layout of a home:

    sequences/<name>.yaml
    checklists/<name>.yaml
    blocks/.claude-plugin/plugin.json
    blocks/skills/<skill>/SKILL.md
"""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def home() -> Path | None:
    raw = os.environ.get("SEQUENCES_HOME")
    if raw == "":
        return None
    path = Path(raw).expanduser() if raw else Path.home() / ".claude" / "sequences"
    return path if path.is_dir() else None


def roots() -> list[Path]:
    found = home()
    return [found, REPO] if found else [REPO]


def find(rel: str) -> Path:
    for root in roots():
        if (root / rel).is_file():
            return root / rel
    raise FileNotFoundError(f"{rel} not found in {', '.join(str(r) for r in roots())}")


def plugin_dir() -> Path:
    found = home()
    if found and (found / "blocks" / ".claude-plugin" / "plugin.json").is_file():
        return found / "blocks"
    return REPO / "plugin"
