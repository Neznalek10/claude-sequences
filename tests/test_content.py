import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ["SEQUENCES_HOME"] = ""

from sequence import content, runner  # noqa: E402
from test_runner import FAKE, happy  # noqa: E402

REPO = content.REPO


def make_home(base: Path) -> Path:
    home = base / "home"
    (home / "sequences").mkdir(parents=True)
    (home / "checklists").mkdir()
    shutil.copytree(REPO / "plugin", home / "blocks")
    shutil.copy(REPO / "checklists" / "app-basics.yaml", home / "checklists" / "app-basics.yaml")
    text = (REPO / "sequences" / "build-app.yaml").read_text()
    (home / "sequences" / "build-app.yaml").write_text(text.replace("Spec, build and", "HOME COPY of spec, build and"))
    (home / "sequences" / "extra.yaml").write_text(text.replace("name: build-app", "name: extra"))
    return home


class ContentCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.gettempdir()) / "sequences-tests" / uuid.uuid4().hex
        self.base.mkdir(parents=True)
        self.home = make_home(self.base)

    def use_home(self, home):
        patcher = mock.patch.dict(os.environ, {"SEQUENCES_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestLookup(ContentCase):
    def test_empty_value_turns_the_home_off(self):
        self.assertIsNone(content.home())
        self.assertEqual(content.plugin_dir(), REPO / "plugin")
        self.assertEqual(content.find("sequences/build-app.yaml"), REPO / "sequences" / "build-app.yaml")

    def test_missing_home_folder_falls_back_to_bundled(self):
        self.use_home(self.base / "nope")
        self.assertIsNone(content.home())
        self.assertEqual(content.plugin_dir(), REPO / "plugin")

    def test_home_wins_and_bundled_fills_the_gaps(self):
        self.use_home(self.home)
        self.assertEqual(content.find("sequences/build-app.yaml"), self.home / "sequences" / "build-app.yaml")
        (self.home / "checklists" / "app-basics.yaml").unlink()
        self.assertEqual(content.find("checklists/app-basics.yaml"), REPO / "checklists" / "app-basics.yaml")
        with self.assertRaises(FileNotFoundError):
            content.find("sequences/missing.yaml")

    def test_blocks_folder_without_plugin_json_is_ignored(self):
        self.use_home(self.home)
        self.assertEqual(content.plugin_dir(), self.home / "blocks")
        (self.home / "blocks" / ".claude-plugin" / "plugin.json").unlink()
        self.assertEqual(content.plugin_dir(), REPO / "plugin")

    def test_list_shows_home_version_once_and_extra_sequences(self):
        self.use_home(self.home)
        listing = runner.list_sequences()
        self.assertEqual(listing.count("build-app:"), 1)
        self.assertIn("HOME COPY", listing)
        self.assertIn("extra:", listing)
        self.assertIn(str(self.home / "sequences" / "build-app.yaml"), listing)


class TestRunUsesHome(ContentCase):
    def test_run_uses_home_sequence_blocks_and_checklist(self):
        target = self.base / "app"
        wrapper = self.base / "claude"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        wrapper.chmod(0o755)
        (self.base / "scenario.json").write_text(json.dumps(happy()))
        token = "SEQ-MARK-HomeOnly77"
        spec_skill = self.home / "blocks" / "skills" / "seq-spec" / "SKILL.md"
        import re
        spec_skill.write_text(re.sub(r"SEQ-MARK-[A-Za-z0-9]+", token, spec_skill.read_text()))
        (self.home / "checklists" / "app-basics.yaml").write_text(
            (REPO / "checklists" / "app-basics.yaml").read_text() + "  - id: APP-99\n    requirement: marker\n    verify: 'bash: true'\n")
        self.use_home(self.home)
        env = {"SEQUENCE_CLAUDE_BIN": str(wrapper), "FAKE_LOG": str(self.base / "calls.jsonl"),
               "FAKE_SCENARIO": str(self.base / "scenario.json")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(sys, "stdin", io.StringIO("")):
                code = runner.main(["run", "build-app", "brief", "--target", str(target)])
        self.assertEqual(code, runner.EXIT_CODES["awaiting_approval"])
        run_dir = next((target / ".sequence" / "runs").iterdir())
        self.assertIn(token, (run_dir / "changelog.md").read_text())
        self.assertIn("APP-99", (run_dir / "checklist.yaml").read_text())
        argv = json.loads((self.base / "calls.jsonl").read_text().splitlines()[0])["argv"]
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(self.home / "blocks"))
        state = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(state["source"], str(self.home / "sequences" / "build-app.yaml"))


if __name__ == "__main__":
    unittest.main()
