import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ["SEQUENCES_HOME"] = ""

from sequence import gates
from sequence.gates import Ctx

REPO = Path(__file__).resolve().parent.parent


def feature(fid="F1", built=False, passes=False, **extra):
    return {"id": fid, "description": "d", "steps": ["s"], "built": built, "passes": passes, **extra}


class GateCase(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()) / "sequences-tests" / uuid.uuid4().hex
        self.run_dir, self.target, self.snap = base / "run", base / "app", base / "snap"
        for d in (self.run_dir, self.target, self.snap):
            d.mkdir(parents=True)
        self.ctx = Ctx(self.run_dir, self.target, "build", 1, "seq-build", self.snap)

    def write_features(self, feats, where=None):
        (where or self.run_dir).joinpath("features.json").write_text(json.dumps({"features": feats}))


class TestFiles(GateCase):
    def test_missing_and_empty_files_fail(self):
        (self.run_dir / "a.md").write_text("x")
        (self.run_dir / "empty.md").write_text("")
        ok, msg = gates.g_files(self.ctx, ["a.md", "empty.md", "gone.md"])
        self.assertFalse(ok)
        self.assertIn("empty.md", msg)
        self.assertIn("gone.md", msg)
        self.assertTrue(gates.g_files(self.ctx, ["a.md"])[0])


class TestFeatureSchema(GateCase):
    def test_valid(self):
        self.write_features([feature("F1"), feature("F2")])
        self.assertTrue(gates.g_features_schema(self.ctx)[0])

    def test_rejects_extra_key_duplicate_id_and_preset_flags(self):
        for bad in ([feature(extra=1)], [feature("F1"), feature("F1")], [feature(built=True)], []):
            self.write_features(bad)
            self.assertFalse(gates.g_features_schema(self.ctx)[0], bad)

    def test_rejects_unreadable_file(self):
        (self.run_dir / "features.json").write_text("not json")
        self.assertFalse(gates.g_features_schema(self.ctx)[0])


class TestFeaturesOnly(GateCase):
    def test_allows_listed_field_only(self):
        self.write_features([feature("F1")], self.snap)
        self.write_features([feature("F1", built=True)])
        self.assertTrue(gates.g_features_only(self.ctx, ["built"])[0])
        self.assertFalse(gates.g_features_only(self.ctx, ["passes"])[0])

    def test_rejects_added_or_removed_features(self):
        self.write_features([feature("F1")], self.snap)
        self.write_features([feature("F1"), feature("F2")])
        self.assertIn("ids added", gates.g_features_only(self.ctx, ["built"])[1])

    def test_rejects_new_field(self):
        self.write_features([feature("F1")], self.snap)
        self.write_features([feature("F1", note="hi")])
        self.assertFalse(gates.g_features_only(self.ctx, ["built"])[0])


class TestAllFlags(GateCase):
    def test_all_built_and_all_pass_name_the_missing_ids(self):
        self.write_features([feature("F1", built=True, passes=True), feature("F2")])
        self.assertIn("F2", gates.g_all_built(self.ctx)[1])
        self.assertFalse(gates.g_all_built(self.ctx)[0])
        self.assertFalse(gates.g_all_pass(self.ctx)[0])
        self.write_features([feature("F1", True, True)])
        self.assertTrue(gates.g_all_built(self.ctx)[0] and gates.g_all_pass(self.ctx)[0])


class TestSkillUsed(GateCase):
    def test_needs_exact_marker_line_for_this_attempt(self):
        token = gates.skill_marker("seq-build")
        log = self.run_dir / "changelog.md"
        self.assertFalse(gates.g_skill_used(self.ctx)[0])
        log.write_text(f"{token} step=build attempt=2\n")
        self.assertFalse(gates.g_skill_used(self.ctx)[0])
        log.write_text(f"{token} step=build attempt=1\n")
        self.assertTrue(gates.g_skill_used(self.ctx)[0])

    def test_every_shipped_skill_has_its_own_token(self):
        tokens = {s: gates.skill_marker(s) for s in ("seq-spec", "seq-build", "seq-test")}
        self.assertEqual(len(set(tokens.values())), 3)

    def test_skill_without_token_raises(self):
        with self.assertRaises(FileNotFoundError):
            gates.skill_marker("no-such-skill")


class TestBashAndChecklist(GateCase):
    def test_bash_reports_exit_code_and_tail(self):
        ok, msg = gates.g_bash(self.ctx, "echo boom; exit 3")
        self.assertFalse(ok)
        self.assertIn("exit 3", msg)
        self.assertIn("boom", msg)
        self.assertTrue(gates.g_bash(self.ctx, "test -d .")[0])

    def test_bash_sees_run_dir_variable(self):
        (self.run_dir / "flag").write_text("x")
        self.assertTrue(gates.g_bash(self.ctx, 'test -f "$SEQ_RUN_DIR/flag"')[0])

    def test_checklist_runs_bash_items_itself_and_reads_review_items(self):
        checklist = self.run_dir / "c.yaml"
        checklist.write_text(
            "items:\n"
            "  - {id: A1, requirement: file exists, verify: 'bash: test -f x.txt'}\n"
            "  - {id: A2, requirement: reviewed, verify: review}\n")
        ctx_path = str(checklist)
        self.assertFalse(gates.g_checklist(self.ctx, ctx_path)[0])
        (self.target / "x.txt").write_text("1")
        self.assertIn("A2", gates.g_checklist(self.ctx, ctx_path)[1])
        (self.run_dir / "results.json").write_text(json.dumps({"items": [{"id": "A2", "status": "pass"}]}))
        self.assertTrue(gates.g_checklist(self.ctx, ctx_path)[0])
        (self.run_dir / "results.json").write_text(json.dumps({"items": [{"id": "A2", "status": "fail"}]}))
        self.assertFalse(gates.g_checklist(self.ctx, ctx_path)[0])

    def test_checklist_failure_carries_the_reviewers_reasons(self):
        checklist = self.run_dir / "c.yaml"
        checklist.write_text("items:\n  - {id: A2, requirement: facts correct, verify: review}\n")
        (self.run_dir / "results.json").write_text(json.dumps({
            "items": [{"id": "A2", "status": "fail", "evidence": "line 49 writes O(II) instead of O(-II)"}],
            "notes": "Fix line 49 and nothing else."}))
        ok, message = gates.g_checklist(self.ctx, str(checklist))
        self.assertFalse(ok)
        self.assertIn("line 49 writes O(II)", message)
        self.assertIn("Reviewer notes: Fix line 49", message)

    def test_reviewer_cannot_pass_a_bash_item_by_claiming_it(self):
        checklist = self.run_dir / "c.yaml"
        checklist.write_text("items:\n  - {id: A1, requirement: r, verify: 'bash: false'}\n")
        (self.run_dir / "results.json").write_text(json.dumps({"items": [{"id": "A1", "status": "pass"}]}))
        self.assertFalse(gates.g_checklist(self.ctx, str(checklist))[0])


class TestRunGates(GateCase):
    def test_collects_all_failures_and_fails_closed_on_crash(self):
        out = gates.run_gates(self.ctx, [{"files": ["nope"]}, {"bash": "exit 1"}, {"all_built": True}])
        self.assertEqual(len(out), 3)
        self.assertTrue(out[2].startswith("[all_built]"))
        self.assertIn("crashed", out[2])

    def test_empty_list_passes(self):
        self.assertEqual(gates.run_gates(self.ctx, []), [])


if __name__ == "__main__":
    unittest.main()


class TestHardening(GateCase):
    def test_malformed_gate_entries_fail_closed(self):
        for bad in ({}, {"bash": "true", "files": []}, "bash", None):
            self.assertEqual(len(gates.run_gates(self.ctx, [bad])), 1, bad)

    def test_flag_values_must_be_real_booleans(self):
        self.write_features([feature(passes="false")], self.snap)
        self.write_features([feature(passes="false")])
        self.assertFalse(gates.g_all_pass(self.ctx)[0])
        self.write_features([feature()], self.snap)
        self.assertFalse(gates.g_features_only(self.ctx, ["passes"])[0])
        self.write_features([feature(passes=True)])
        self.assertTrue(gates.g_features_only(self.ctx, ["passes"])[0])

    def test_frozen_files_gate(self):
        (self.run_dir / "frozen").mkdir()
        (self.run_dir / "frozen" / "check.sh").write_text("a")
        (self.run_dir / "check.sh").write_text("a")
        self.assertTrue(gates.g_frozen(self.ctx)[0])
        (self.run_dir / "check.sh").write_text("b")
        self.assertFalse(gates.g_frozen(self.ctx)[0])
        (self.run_dir / "check.sh").unlink()
        self.assertFalse(gates.g_frozen(self.ctx)[0])

    def test_fingerprint_sees_new_files_but_not_cache_noise(self):
        git = lambda *a: subprocess.run(["git", *a], cwd=self.target, capture_output=True, check=True)
        git("init", "-b", "main", "-q")
        (self.target / "a.py").write_text("x")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        before = gates.target_fingerprint(self.target)
        (self.target / "__pycache__").mkdir()
        (self.target / "__pycache__" / "a.pyc").write_text("x")
        self.assertEqual(gates.target_fingerprint(self.target), before)
        (self.target / ".coverage").write_text("x")
        (self.target / "htmlcov").mkdir()
        (self.target / "htmlcov" / "i.html").write_text("x")
        self.assertEqual(gates.target_fingerprint(self.target), before)
        (self.target / "evil.py").write_text("x")
        self.assertNotEqual(gates.target_fingerprint(self.target), before)
