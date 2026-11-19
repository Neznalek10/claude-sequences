import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ["SEQUENCES_HOME"] = ""

from sequence import runner

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
FEATURES = {"features": [
    {"id": f, "description": f"feature {f}", "steps": ["run it"], "built": False, "passes": False}
    for f in ("F1", "F2")
]}
REVIEW = {"items": [{"id": i, "status": "pass", "evidence": "x"} for i in ("APP-03", "APP-06")], "notes": ""}


def happy() -> dict:
    return {
        "spec": [{"run_files": {
            "spec.md": "# spec", "features.json": json.dumps(FEATURES), "check.sh": "#!/bin/bash\nexit 0\n"}}],
        "build": [{"target_files": {"README.md": "how to run and test", ".gitignore": "__pycache__/\n"},
                   "commit": True, "set": {"*": {"built": True}}}],
        "test": [{"run_files": {"results.json": json.dumps(REVIEW)}, "set": {"*": {"passes": True}}}],
    }


class RunnerCase(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()) / "sequences-tests" / uuid.uuid4().hex
        base.mkdir(parents=True)
        self.base, self.target = base, base / "app"
        wrapper = base / "claude"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        wrapper.chmod(0o755)
        os.environ.update(
            SEQUENCE_CLAUDE_BIN=str(wrapper), FAKE_LOG=str(base / "calls.jsonl"),
            FAKE_SCENARIO=str(base / "scenario.json"),
        )

    def run_sequence(self, scenario, budget=None, input_fn=lambda _: "y"):
        (self.base / "scenario.json").write_text(json.dumps(scenario))
        seq = runner.load_sequence("build-app")
        run_dir, state = runner.new_run(seq, "make a thing", self.target, budget)
        self.seq, self.run_dir, self.state = seq, run_dir, state
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.execute(seq, state, run_dir, runner.find_claude(), input_fn)
        runner.write_summary(seq, state, run_dir)
        return status

    def calls(self):
        log = self.base / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def order(self):
        return [(c["step"], c["attempt"]) for c in self.calls()]

    def errors(self):
        path = self.run_dir / "errors.md"
        return path.read_text() if path.exists() else ""


class TestHappyPath(RunnerCase):
    def test_runs_all_steps_and_writes_summary(self):
        self.assertEqual(self.run_sequence(happy()), "done")
        self.assertEqual(self.order(), [("spec", 1), ("build", 1), ("test", 1)])
        summary = (self.run_dir / "summary.md").read_text()
        self.assertIn("Status: **done**", summary)
        self.assertIn("Features: 2 total, 2 built, 2 passing", summary)
        self.assertAlmostEqual(self.state["total_cost"], 0.3)

    def test_each_call_is_isolated_and_uses_plugin_skill(self):
        self.run_sequence(happy())
        argv = self.calls()[1]["argv"]
        self.assertNotIn("--bare", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(runner.REPO / "plugin"))
        self.assertTrue(self.calls()[1]["prompt"].startswith("/sequences:seq-build step=build attempt=1"))
        self.assertIn("Bash(git push:*)", argv)

    def test_run_folder_is_excluded_from_git(self):
        self.run_sequence(happy())
        self.assertIn(".sequence/", (self.target / ".git" / "info" / "exclude").read_text())


class TestRetries(RunnerCase):
    def test_gate_failure_retries_with_failure_attached(self):
        sc = happy()
        sc["build"].insert(0, {"target_files": {"README.md": "run test"}, "commit": True})
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertEqual(self.state["steps"]["build"]["attempts"], 2)
        self.assertIn("features not built yet", self.errors())
        second = [c for c in self.calls() if c["step"] == "build"][1]["prompt"]
        self.assertIn("PREVIOUS FAILURES", second)

    def test_cap_of_three_blocks_the_run(self):
        sc = happy()
        sc["spec"] = [{"run_files": {"spec.md": "x", "features.json": "{}", "check.sh": "x"}}]
        self.assertEqual(self.run_sequence(sc), "blocked")
        self.assertIn("used all 3 attempts", self.state["reason"])
        self.assertEqual([c["step"] for c in self.calls()], ["spec"] * 3)

    def test_missing_skill_marker_fails_the_step(self):
        sc = happy()
        sc["build"].insert(0, dict(sc["build"][0], marker=False))
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertIn("[skill_used]", self.errors())
        self.assertEqual(self.state["steps"]["build"]["attempts"], 2)

    def test_claude_error_with_passing_gates_still_counts_as_done(self):
        sc = happy()
        sc["spec"][0]["subtype"] = "error_max_turns"
        self.assertEqual(self.run_sequence(sc), "done")

    def test_claude_error_with_failing_gates_is_logged(self):
        sc = happy()
        sc["spec"] = [{"subtype": "error_max_budget_usd"}]
        self.assertEqual(self.run_sequence(sc), "blocked")
        self.assertIn("error_max_budget_usd", self.errors())


class TestGuards(RunnerCase):
    def test_budget_exhaustion_blocks_before_next_step(self):
        sc = happy()
        sc["spec"][0]["cost"] = 3.0
        self.assertEqual(self.run_sequence(sc, budget=3.0), "blocked")
        self.assertEqual(self.state["reason"], "budget exhausted")
        self.assertEqual([c["step"] for c in self.calls()], ["spec"])

    def test_step_budget_is_capped_by_what_is_left(self):
        sc = happy()
        sc["spec"][0]["cost"] = 2.5
        self.run_sequence(sc, budget=3.0)
        argv = [c for c in self.calls() if c["step"] == "build"][0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.50")

    def test_tampering_with_features_is_rejected_and_restored(self):
        sc = happy()
        sc["build"].insert(0, {
            "target_files": {"README.md": "run test"}, "commit": True,
            "set": {"F1": {"built": True, "description": "changed"}, "F2": {"built": True}}})
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertIn("may not change", self.errors())
        feats = json.loads((self.run_dir / "features.json").read_text())["features"]
        self.assertEqual(feats[0]["description"], "feature F1")

    def test_reviewer_touching_target_code_fails(self):
        sc = happy()
        sc["test"].insert(0, dict(sc["test"][0], target_files={"README.md": "edited by reviewer"}))
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertIn("[target_unchanged]", self.errors())

    def test_builder_cannot_mark_passes(self):
        sc = happy()
        sc["build"][0]["set"] = {"*": {"built": True, "passes": True}}
        self.run_sequence(sc)
        self.assertIn("['passes'] may not change in step build", self.errors())


class TestLoopBack(RunnerCase):
    def test_failed_test_returns_to_build_then_retests(self):
        sc = happy()
        sc["test"].insert(0, {"run_files": {"results.json": json.dumps(REVIEW)},
                              "set": {"F1": {"passes": True}, "F2": {"passes": False}}})
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertEqual(self.order(), [("spec", 1), ("build", 1), ("test", 1), ("build", 2), ("test", 2)])
        self.assertIn("features not passing: ['F2']", self.state["last_failure"] + self.errors())

    def test_three_failed_tests_block_without_extra_build(self):
        sc = happy()
        sc["test"] = [{"run_files": {"results.json": json.dumps(REVIEW)}, "set": {"*": {"passes": False}}}]
        self.assertEqual(self.run_sequence(sc), "blocked")
        self.assertEqual(self.state["steps"]["test"]["attempts"], 3)
        self.assertEqual(self.state["steps"]["build"]["attempts"], 3)

    def test_resume_after_a_failed_review_fixes_first_then_reviews(self):
        sc = happy()
        failing = {"run_files": {"results.json": json.dumps(REVIEW)}, "set": {"*": {"passes": False}}}
        sc["test"] = [failing, failing, failing] + sc["test"]
        self.assertEqual(self.run_sequence(sc), "blocked")
        before = len(self.calls())
        runner.prepare_resume(self.seq, self.state)
        self.assertEqual(self.state["steps"]["build"]["status"], "pending", "the fix step runs first")
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.execute(self.seq, self.state, self.run_dir, runner.find_claude(), lambda _: "y")
        self.assertEqual(status, "done")
        self.assertEqual([step for step, _ in self.order()[before:]], ["build", "test"])

    def test_checklist_review_item_marked_fail_blocks_pass(self):
        sc = happy()
        bad = {"items": [{"id": "APP-03", "status": "fail", "evidence": "no tests"},
                         {"id": "APP-06", "status": "pass", "evidence": "x"}], "notes": "F1 has no test"}
        sc["test"].insert(0, {"run_files": {"results.json": json.dumps(bad)}, "set": {"*": {"passes": True}}})
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertIn("APP-03", self.errors())


class TestApprovalAndResume(RunnerCase):
    def test_no_human_means_awaiting_approval_then_resume_finishes(self):
        def no_tty(_):
            raise EOFError

        self.assertEqual(self.run_sequence(happy(), input_fn=no_tty), "awaiting_approval")
        calls_before = len(self.calls())
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.execute(self.seq, self.state, self.run_dir, runner.find_claude(), lambda _: "y")
        self.assertEqual(status, "done")
        self.assertEqual(len(self.calls()), calls_before)

    def test_cli_without_terminal_exits_awaiting_approval_not_hang(self):
        (self.base / "scenario.json").write_text(json.dumps(happy()))
        argv = ["run", "build-app", "make a thing", "--target", str(self.target)]
        with contextlib.redirect_stdout(io.StringIO()):
            from unittest import mock
            with mock.patch.object(sys, "stdin", io.StringIO("")):
                code = runner.main(argv)
        self.assertEqual(code, runner.EXIT_CODES["awaiting_approval"])

    def test_tty_input_raises_eof_when_stdin_is_not_a_terminal(self):
        from unittest import mock
        with mock.patch.object(sys, "stdin", io.StringIO("y\n")):
            with self.assertRaises(EOFError):
                runner.tty_input("? ")

    def test_rejected_approval_blocks(self):
        self.assertEqual(self.run_sequence(happy(), input_fn=lambda _: "n"), "blocked")

    def test_resume_after_block_continues_from_failed_step(self):
        sc = happy()
        sc["build"] = [{"commit": False}] * 8 + sc["build"]
        self.assertEqual(self.run_sequence(sc), "blocked")
        self.seq["steps"][1]["loop"]["max_iterations"] = 9
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.execute(self.seq, self.state, self.run_dir, runner.find_claude(), lambda _: "y")
        self.assertEqual(status, "done")
        self.assertEqual(self.state["steps"]["spec"]["attempts"], 1)


class TestJsonAndSignals(RunnerCase):
    def cli(self, argv, stdin=""):
        from unittest import mock
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = runner.main(argv)
        return code, [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]

    def two_approvals(self):
        seq = runner.load_sequence("build-app")
        seq["name"] = "two-gates"
        seq["steps"].insert(3, {"id": "first-ok", "wait": "approval"})
        path = self.base / "two-gates.yaml"
        import yaml
        path.write_text(yaml.safe_dump(seq))
        return str(path)

    def test_json_lines_report_start_and_finish(self):
        (self.base / "scenario.json").write_text(json.dumps(happy()))
        code, lines = self.cli(["run", "build-app", "make a thing", "--target", str(self.target), "--json"])
        self.assertEqual(code, 3)
        self.assertEqual([line["event"] for line in lines], ["started", "finished"])
        self.assertEqual(lines[1]["status"], "awaiting_approval")
        self.assertEqual(lines[0]["run_dir"], lines[1]["run_dir"])
        code, status = self.cli(["status", lines[0]["run_dir"], "--json"])
        self.assertEqual((code, status[0]["status"], status[0]["exit"]), (0, "awaiting_approval", 3))

    def test_approve_answers_only_the_next_approval_step(self):
        (self.base / "scenario.json").write_text(json.dumps(happy()))
        code, lines = self.cli(["run", self.two_approvals(), "make a thing", "--target", str(self.target), "--json"])
        run_dir = lines[0]["run_dir"]
        self.assertEqual(code, 3)
        code, lines = self.cli(["resume", run_dir, "--approve", "--json"])
        self.assertEqual((code, lines[-1]["status"]), (3, "awaiting_approval"), "the second approval waits for its own yes")
        code, lines = self.cli(["resume", run_dir, "--approve", "--json"])
        self.assertEqual((code, lines[-1]["status"]), (0, "done"))

    def test_reject_blocks_the_run(self):
        (self.base / "scenario.json").write_text(json.dumps(happy()))
        _, lines = self.cli(["run", "build-app", "make a thing", "--target", str(self.target), "--json"])
        code, lines = self.cli(["resume", lines[0]["run_dir"], "--reject", "--json"])
        self.assertEqual((code, lines[-1]["status"]), (1, "blocked"))
        self.assertIn("not approved", lines[-1]["reason"])

    def test_a_signal_leaves_a_resumable_interrupted_run(self):
        def stop(_):
            raise KeyboardInterrupt

        (self.base / "scenario.json").write_text(json.dumps(happy()))
        from unittest import mock
        with mock.patch.object(runner, "tty_input", stop), mock.patch.object(sys, "stdin", io.StringIO("")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = runner.main(["run", "build-app", "make a thing", "--target", str(self.target), "--json"])
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual((code, lines[-1]["status"]), (130, "interrupted"))
        state = json.loads((Path(lines[0]["run_dir"]) / "run.json").read_text())
        self.assertEqual(state["status"], "interrupted")
        self.assertIn("Resume:", (Path(lines[0]["run_dir"]) / "summary.md").read_text())
        self.assertIs(runner.signal.getsignal(runner.signal.SIGTERM), runner._terminate)


class TestSequenceFile(unittest.TestCase):
    def test_rejects_step_with_skill_and_wait(self):
        with self.assertRaises(ValueError):
            runner.validate({"steps": [{"id": "a", "skill": "x", "wait": "approval"}]})

    def test_rejects_unknown_dependency(self):
        with self.assertRaises(ValueError):
            runner.validate({"steps": [{"id": "a", "skill": "x", "depends_on": ["zzz"]}]})

    def test_rejects_on_fail_to_later_step(self):
        with self.assertRaises(ValueError):
            runner.validate({"steps": [{"id": "a", "skill": "x", "on_fail": "b"}, {"id": "b", "skill": "y"}]})

    def test_shipped_sequence_is_valid(self):
        self.assertEqual(runner.load_sequence("build-app")["steps"][0]["id"], "spec")

    def test_list_names_the_shipped_sequence(self):
        self.assertIn("build-app", runner.list_sequences())

    def test_find_claude_honours_override(self):
        os.environ["SEQUENCE_CLAUDE_BIN"] = "/x/claude"
        self.assertEqual(runner.find_claude(), "/x/claude")


if __name__ == "__main__":
    unittest.main()


class TestQaFixes(RunnerCase):
    def cli(self, *argv, stdin=""):
        from unittest import mock
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                return runner.main(list(argv))

    def only_run_dir(self, target=None):
        return next(((target or self.target) / ".sequence" / "runs").iterdir())

    def test_interrupted_attempt_is_counted_and_resumable(self):
        from unittest import mock
        with mock.patch.object(runner, "call_claude", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_sequence(happy())
        saved = json.loads((self.run_dir / "run.json").read_text())
        self.assertEqual(saved["steps"]["spec"]["attempts"], 1)
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.execute(self.seq, saved, self.run_dir, runner.find_claude(), lambda _: "y")
        self.assertEqual(status, "done")
        self.assertEqual(saved["steps"]["spec"]["attempts"], 2)

    def test_resume_gives_a_blocked_run_fresh_attempts(self):
        sc = happy()
        broken = {"spec": [{"run_files": {"spec.md": "x", "features.json": "{}", "check.sh": "x"}}]}
        self.assertEqual(self.run_sequence(dict(sc, **broken)), "blocked")
        (self.base / "scenario.json").write_text(json.dumps(sc))
        self.assertEqual(self.cli("resume", str(self.run_dir), "--approve"), 0)
        self.assertEqual(json.loads((self.run_dir / "run.json").read_text())["status"], "done")

    def test_builder_cannot_weaken_the_spec_check(self):
        sc = happy()
        sc["build"].insert(0, dict(sc["build"][0], run_files={"check.sh": "exit 0 # weakened\n"}))
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertIn("[frozen]", self.errors())
        self.assertEqual((self.run_dir / "check.sh").read_text(), "#!/bin/bash\nexit 0\n")
        self.assertEqual(self.state["steps"]["build"]["attempts"], 2)

    def test_stale_review_results_do_not_satisfy_a_later_attempt(self):
        sc = happy()
        first = {"run_files": {"results.json": json.dumps(REVIEW)}}
        sc["test"] = [first, {"set": {"*": {"passes": True}}}, happy()["test"][0]]
        self.assertEqual(self.run_sequence(sc), "done")
        self.assertEqual(self.state["steps"]["test"]["attempts"], 3)
        self.assertTrue(any(self.run_dir.glob("steps/test-*/results-before.json")))

    def test_bad_gate_entries_and_empty_steps_are_rejected_up_front(self):
        base = runner.load_sequence("build-app")
        for gate in ({}, {"nope": True}, {"bash": "a", "files": []}, "bash", None):
            seq = json.loads(json.dumps(base))
            seq["steps"][0]["gate"] = [gate]
            with self.assertRaises(ValueError):
                runner.validate(seq)
        with self.assertRaises(ValueError):
            runner.validate({"name": "x", "steps": []})

    def test_budget_and_brief_are_checked(self):
        seq = runner.load_sequence("build-app")
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                runner.new_run(seq, "x", self.target, bad)
        with self.assertRaises(ValueError):
            runner.new_run(seq, "   ", self.target, 1.0)

    def test_cli_errors_exit_2_not_a_traceback(self):
        self.assertEqual(self.cli("status", str(self.base / "nope")), 2)
        bad = self.base / "bad-run"
        bad.mkdir()
        (bad / "run.json").write_text("[]")
        self.assertEqual(self.cli("resume", str(bad)), 2)
        (bad / "run.json").write_text(json.dumps({"name": "build-app", "steps": {}}))
        self.assertEqual(self.cli("resume", str(bad)), 2)

    def test_missing_claude_binary_exits_2_and_leaves_nothing_behind(self):
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "SEQUENCE_CLAUDE_BIN"}
        env.update(PATH="/nonexistent", HOME=str(self.base))
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.cli("run", "build-app", "b", "--target", str(self.target)), 2)
        self.assertFalse(self.target.exists())

    def test_custom_sequence_file_can_be_resumed(self):
        (self.base / "scenario.json").write_text(json.dumps(happy()))
        custom = self.base / "custom.yaml"
        custom.write_text((runner.REPO / "sequences" / "build-app.yaml").read_text().replace("name: build-app", "name: custom-one"))
        self.assertEqual(self.cli("run", str(custom), "b", "--target", str(self.target)), 3)
        self.assertEqual(self.cli("resume", str(self.only_run_dir()), "--approve"), 0)

    def test_sequence_name_cannot_escape_the_run_folder(self):
        seq = dict(runner.load_sequence("build-app"), name="../../../escaped")
        run_dir, _ = runner.new_run(seq, "b", self.target, 1.0)
        self.assertEqual(run_dir.parent, self.target / ".sequence" / "runs")

    def test_attempt_numbers_stay_unique_after_resume_so_old_markers_do_not_count(self):
        sc = happy()
        broken = {"spec": [{"run_files": {"spec.md": "x", "features.json": "{}", "check.sh": "x"}}]}
        self.assertEqual(self.run_sequence(dict(sc, **broken)), "blocked")
        sc["spec"][0]["marker"] = False
        (self.base / "scenario.json").write_text(json.dumps(sc))
        self.cli("resume", str(self.run_dir), "--approve")
        state = json.loads((self.run_dir / "run.json").read_text())
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["steps"]["spec"]["attempts"], 6)
        self.assertTrue((self.run_dir / "steps" / "spec-6").is_dir())
        self.assertIn("[skill_used]", (self.run_dir / "steps" / "spec-4" / "gate.log").read_text())

    def test_freeze_must_be_plain_file_names(self):
        base = runner.load_sequence("build-app")
        for bad in ("spec.md", ["sub/x.md"], [1]):
            seq = json.loads(json.dumps(base))
            seq["steps"][0]["freeze"] = bad
            with self.assertRaises(ValueError):
                runner.validate(seq)

    def test_unusable_claude_binary_exits_2(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"SEQUENCE_CLAUDE_BIN": "/nonexistent/claude"}):
            self.assertEqual(self.cli("run", "build-app", "b", "--target", str(self.target)), 2)
        self.assertFalse(self.target.exists())
