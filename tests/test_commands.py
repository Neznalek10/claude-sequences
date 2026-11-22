import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ["SEQUENCES_HOME"] = ""

import yaml  # noqa: E402

from sequence import runner  # noqa: E402
from test_runner import FAKE  # noqa: E402


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.gettempdir()) / "sequences-tests" / uuid.uuid4().hex
        self.base.mkdir(parents=True)
        self.target = self.base / "app"
        patcher = mock.patch.dict(os.environ, {"SEQUENCE_CLAUDE_BIN": "/nonexistent/claude"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_seq(self, steps, brief="hello world", budget=None, extra=(), **top):
        path = self.base / "cmds.yaml"
        path.write_text(yaml.safe_dump({"name": "cmds", "steps": steps, **top}))
        argv = ["run", str(path), brief, "--target", str(self.target), *extra]
        if budget is not None:
            argv += ["--budget", str(budget)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(sys, "stdin", io.StringIO("")):
                code = runner.main(argv)
        self.out = out.getvalue()
        self.run_dir = next((self.target / ".sequence" / "runs").iterdir())
        self.state = json.loads((self.run_dir / "run.json").read_text())
        return code

    def output(self, step_id):
        return (self.run_dir / "outputs" / f"{step_id}.txt").read_text()

    def errors(self):
        path = self.run_dir / "errors.md"
        return path.read_text() if path.exists() else ""


class TestRunStep(CommandCase):
    def test_command_only_sequence_needs_no_claude_and_costs_nothing(self):
        code = self.run_seq([{"id": "hi", "run": "echo hi"}], budget=0.01)
        self.assertEqual(code, 0)
        self.assertEqual(self.state["status"], "done")
        self.assertEqual(self.state["total_cost"], 0.0)
        self.assertEqual(self.output("hi"), "hi\n")

    def test_runs_in_the_target_folder_with_run_context_in_env(self):
        self.run_seq([{"id": "ctx", "run": 'echo "$PWD|$SEQ_TARGET|$SEQ_RUN_DIR|$SEQ_STEP|$SEQ_ATTEMPT"'}])
        target = str(self.target.resolve())
        self.assertEqual(self.output("ctx").strip(), f"{target}|{target}|{self.run_dir}|ctx|1")

    def test_brief_reaches_the_command_as_data_not_as_shell_text(self):
        self.run_seq([{"id": "echo", "run": 'printf "%s" "$SEQ_BRIEF"'}], brief="$(touch pwned) `touch pwned2`")
        self.assertEqual(self.output("echo"), "$(touch pwned) `touch pwned2`")
        self.assertFalse((self.target / "pwned").exists() or (self.target / "pwned2").exists())

    def test_later_step_reads_an_earlier_steps_stdout(self):
        steps = [
            {"id": "make-it", "run": "echo from-step-one"},
            {"id": "use", "run": 'cat "$SEQ_OUT_MAKE_IT" > "$SEQ_RUN_DIR/copy.txt"', "gate": [{"files": ["copy.txt"]}]},
        ]
        self.assertEqual(self.run_seq(steps), 0)
        self.assertEqual((self.run_dir / "copy.txt").read_text(), "from-step-one\n")

    def test_script_stops_at_the_first_failing_line(self):
        self.assertEqual(self.run_seq([{"id": "strict", "run": "false\necho after"}]), 1)
        self.assertNotIn("after", (self.run_dir / "steps" / "strict-1" / "stdout.txt").read_text())

    def test_failure_blocks_with_exit_code_and_output_tail(self):
        code = self.run_seq([{"id": "bad", "run": "echo to-out; echo to-err >&2; exit 3"}])
        self.assertEqual(code, 1)
        self.assertEqual(self.state["status"], "blocked")
        self.assertEqual(self.state["steps"]["bad"]["attempts"], 1)
        self.assertIn("[run] exit 3", self.errors())
        self.assertIn("to-err", self.errors())
        self.assertFalse((self.run_dir / "outputs" / "bad.txt").exists())

    def test_command_text_is_logged_and_kept_in_the_step_folder(self):
        self.run_seq([{"id": "show", "run": "echo one\necho two"}])
        self.assertIn("step show attempt 1 runs run: echo one", (self.run_dir / "changelog.md").read_text())
        self.assertEqual((self.run_dir / "steps" / "show-1" / "command.sh").read_text(), "echo one\necho two\n")

    def test_retries_rerun_a_flaky_command(self):
        flaky = 'n=$(cat "$SEQ_TARGET/count" 2>/dev/null || echo 0); echo $((n+1)) > "$SEQ_TARGET/count"; [ "$n" -ge 1 ]'
        self.assertEqual(self.run_seq([{"id": "flaky", "run": flaky, "retries": 1}]), 0)
        self.assertEqual(self.state["steps"]["flaky"]["attempts"], 2)

    def test_without_retries_a_failure_is_final(self):
        flaky = 'n=$(cat "$SEQ_TARGET/count" 2>/dev/null || echo 0); echo $((n+1)) > "$SEQ_TARGET/count"; [ "$n" -ge 1 ]'
        self.assertEqual(self.run_seq([{"id": "flaky", "run": flaky}]), 1)
        self.assertEqual(self.state["steps"]["flaky"]["attempts"], 1)

    def test_timeout_blocks_and_kills_background_children(self):
        code = self.run_seq([{"id": "slow", "run": 'sleep 60 &\necho $! > "$SEQ_TARGET/pid"\nwait', "timeout_min": 0.02}])
        self.assertEqual(code, 1)
        self.assertIn("timed out", self.errors())
        pid = int((self.target / "pid").read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("background child still running after timeout")

    def test_gates_still_apply_after_a_command(self):
        ok = [{"id": "w", "run": 'echo data > "$SEQ_RUN_DIR/made.txt"', "gate": [{"files": ["made.txt"]}]}]
        self.assertEqual(self.run_seq(ok), 0)

    def test_gate_failure_fails_a_command_that_exited_zero(self):
        steps = [{"id": "w", "run": "true", "gate": [{"files": ["never.txt"]}]}]
        self.assertEqual(self.run_seq(steps), 1)
        self.assertIn("[files]", self.errors())

    def test_json_output_and_status_for_a_command_run(self):
        self.assertEqual(self.run_seq([{"id": "hi", "run": "true"}], extra=["--json"]), 0)
        events = [json.loads(line) for line in self.out.splitlines()]
        self.assertEqual([e["event"] for e in events], ["started", "finished"])
        self.assertEqual(events[1]["status"], "done")


class TestPythonStep(CommandCase):
    def test_runs_code_with_env_and_target_on_the_import_path(self):
        steps = [
            {"id": "make", "run": "printf 'VALUE = 42\\n' > helper.py"},
            {"id": "calc", "python": "import os, helper\nprint(helper.VALUE, os.environ['SEQ_BRIEF'])"},
        ]
        self.assertEqual(self.run_seq(steps, brief="go"), 0)
        self.assertEqual(self.output("calc"), "42 go\n")
        self.assertTrue((self.run_dir / "steps" / "calc-1" / "code.py").is_file())

    def test_exception_fails_the_step_with_the_traceback(self):
        self.assertEqual(self.run_seq([{"id": "boom", "python": "raise RuntimeError('kaboom')"}]), 1)
        self.assertIn("[python] exit 1", self.errors())
        self.assertIn("RuntimeError: kaboom", self.errors())

    def test_python_step_can_write_run_folder_files_for_a_gate(self):
        steps = [{"id": "w", "python": "import os, pathlib\npathlib.Path(os.environ['SEQ_RUN_DIR'], 'r.json').write_text('{}')",
                  "gate": [{"files": ["r.json"]}]}]
        self.assertEqual(self.run_seq(steps), 0)


class TestMixedWithClaude(CommandCase):
    def setUp(self):
        super().setUp()
        wrapper = self.base / "claude"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        wrapper.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {
            "SEQUENCE_CLAUDE_BIN": str(wrapper), "FAKE_LOG": str(self.base / "calls.jsonl"),
            "FAKE_SCENARIO": str(self.base / "scenario.json")})
        patcher.start()
        self.addCleanup(patcher.stop)

    STEPS = [
        {"id": "build", "skill": "seq-build"},
        {"id": "check", "run": 'test -f "$SEQ_TARGET/ok.txt" || { echo "ok.txt missing" >&2; exit 1; }', "on_fail": "build"},
    ]

    def calls(self):
        return [json.loads(line) for line in (self.base / "calls.jsonl").read_text().splitlines()]

    def test_failed_command_sends_its_output_back_to_the_claude_step(self):
        (self.base / "scenario.json").write_text(json.dumps(
            {"build": [{"target_files": {"x.txt": "1"}}, {"target_files": {"ok.txt": "1"}}]}))
        self.assertEqual(self.run_seq(self.STEPS), 0)
        self.assertEqual(self.state["steps"]["build"]["attempts"], 2)
        self.assertEqual(self.state["steps"]["check"]["attempts"], 2)
        second = self.calls()[1]["prompt"]
        self.assertIn("PREVIOUS FAILURES", second)
        self.assertIn("ok.txt missing", second)

    def test_loop_ends_at_the_attempt_cap(self):
        (self.base / "scenario.json").write_text(json.dumps({"build": [{"target_files": {"x.txt": "1"}}]}))
        self.assertEqual(self.run_seq(self.STEPS), 1)
        self.assertEqual(self.state["reason"], "step check used all 3 attempts")
        self.assertEqual(len(self.calls()), 3)

    def test_claude_budget_block_does_not_apply_to_a_command_step(self):
        (self.base / "scenario.json").write_text(json.dumps({"build": [{"cost": 1.0}]}))
        steps = [{"id": "build", "skill": "seq-build"}, {"id": "after", "run": "echo still-runs"}]
        self.assertEqual(self.run_seq(steps, budget=1.0), 0)
        self.assertEqual(self.output("after"), "still-runs\n")


class TestLoopBackThroughCommandSteps(TestMixedWithClaude):
    def test_command_step_between_the_fix_and_the_check_runs_again(self):
        steps = [
            {"id": "build", "skill": "seq-build"},
            {"id": "lint", "run": "echo linting"},
            self.STEPS[1],
        ]
        (self.base / "scenario.json").write_text(json.dumps(
            {"build": [{"target_files": {"x.txt": "1"}}, {"target_files": {"ok.txt": "1"}}]}))
        self.assertEqual(self.run_seq(steps), 0)
        self.assertEqual(self.state["steps"]["lint"]["attempts"], 2)

    def test_on_fail_can_point_at_a_command_step(self):
        steps = [
            {"id": "gen", "run": 'echo line >> "$SEQ_TARGET/gen.log"'},
            {"id": "check", "run": '[ "$(wc -l < "$SEQ_TARGET/gen.log")" -ge 2 ]', "on_fail": "gen"},
        ]
        self.assertEqual(self.run_seq(steps), 0)
        self.assertEqual(self.state["steps"]["gen"]["attempts"], 2)
        self.assertEqual(self.state["steps"]["check"]["attempts"], 2)

    def test_loop_through_a_command_step_still_ends_at_the_failing_steps_cap(self):
        steps = [
            {"id": "gen", "run": 'echo line >> "$SEQ_TARGET/gen.log"'},
            {"id": "check", "run": "false", "on_fail": "gen"},
        ]
        self.assertEqual(self.run_seq(steps), 1)
        self.assertEqual(self.state["reason"], "step check used all 3 attempts")
        self.assertEqual(self.state["steps"]["gen"]["attempts"], 3)

    def test_two_failing_command_steps_cannot_keep_a_loop_alive(self):
        flaky = 'n=$(cat "$SEQ_TARGET/b" 2>/dev/null || echo 0); echo $((n+1)) > "$SEQ_TARGET/b"; [ $((n % 2)) -eq 1 ]'
        steps = [
            {"id": "a", "run": "echo a"},
            {"id": "b", "run": flaky, "on_fail": "a"},
            {"id": "c", "run": "exit 1", "on_fail": "a"},
        ]
        self.assertEqual(self.run_seq(steps), 1)
        self.assertEqual(self.state["status"], "blocked")
        self.assertLessEqual(sum(s["attempts"] for s in self.state["steps"].values()), 20)

    def test_a_passing_rerun_does_not_use_up_a_command_steps_cap(self):
        steps = [
            {"id": "a", "run": "echo a"},
            {"id": "b", "run": '[ "$(wc -l < "$SEQ_TARGET/n.log")" -ge 3 ]', "on_fail": "a", "retries": 4},
        ]
        steps[0]["run"] = 'echo x >> "$SEQ_TARGET/n.log"'
        self.assertEqual(self.run_seq(steps), 0)
        self.assertEqual(self.state["steps"]["a"]["attempts"], 3)
        self.assertEqual(self.state["steps"]["a"].get("failures", 0), 0)

    def test_blocked_command_loop_resumes_with_fresh_attempts(self):
        steps = [
            {"id": "gen", "run": 'echo line >> "$SEQ_TARGET/gen.log"'},
            {"id": "check", "run": '[ "$(wc -l < "$SEQ_TARGET/gen.log")" -ge 5 ]', "on_fail": "gen"},
        ]
        self.assertEqual(self.run_seq(steps), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["resume", str(self.run_dir)]), 0)


class TestOutputsAndEnvironment(CommandCase):
    def test_output_is_kept_only_when_the_step_passes_its_gates(self):
        steps = [{"id": "a", "run": "echo data", "gate": [{"files": ["never.txt"]}]}]
        self.assertEqual(self.run_seq(steps), 1)
        self.assertFalse((self.run_dir / "outputs" / "a.txt").exists())

    def test_a_step_does_not_see_the_outputs_of_later_steps_after_a_loop_back(self):
        steps = [
            {"id": "x", "run": 'echo "${SEQ_OUT_Y:-unset}" >> "$SEQ_TARGET/seen.txt"'},
            {"id": "y", "run": "echo y-out"},
            {"id": "z", "run": '[ -f "$SEQ_TARGET/seen.txt" ] && [ "$(wc -l < "$SEQ_TARGET/seen.txt")" -ge 2 ]', "on_fail": "x"},
        ]
        self.assertEqual(self.run_seq(steps), 0)
        self.assertEqual((self.target / "seen.txt").read_text(), "unset\nunset\n")

    def test_output_copy_does_not_write_through_a_planted_symlink(self):
        victim = self.base / "victim.txt"
        victim.write_text("untouched")
        steps = [
            {"id": "a", "run": f'mkdir -p "$SEQ_RUN_DIR/outputs" && ln -s "{victim}" "$SEQ_RUN_DIR/outputs/b.txt"'},
            {"id": "b", "run": "echo new-output"},
        ]
        self.assertEqual(self.run_seq(steps), 0)
        self.assertEqual(victim.read_text(), "untouched")
        self.assertEqual(self.output("b"), "new-output\n")

    def test_sighup_stays_ignored_when_started_under_nohup(self):
        import signal
        old = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, old)
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        self.run_seq([{"id": "hi", "run": "true"}])
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_brief_is_passed_exactly(self):
        brief = "  lead\r\nmid\rend\n\n"
        self.run_seq([{"id": "b", "run": 'printf "%s" "$SEQ_BRIEF" > "$SEQ_TARGET/brief.out"'}], brief=brief)
        self.assertEqual((self.target / "brief.out").read_bytes(), brief.encode())

    def test_oversized_brief_fails_the_step_instead_of_crashing_the_runner(self):
        code = self.run_seq([{"id": "big", "run": "true"}], brief="x" * 300000)
        self.assertEqual(code, 1)
        self.assertEqual(self.state["status"], "blocked")
        self.assertIn("could not start", self.errors())

    def test_failure_text_for_the_next_step_keeps_the_end_of_the_output(self):
        code = self.run_seq([{"id": "p", "python": "for i in range(60):\n    print('x' * 120)\nraise RuntimeError('FINAL-ERROR-LINE')"}])
        self.assertEqual(code, 1)
        self.assertIn("FINAL-ERROR-LINE", self.state["last_failure"])
        self.assertIn("[python] exit 1", self.state["last_failure"])
        self.assertLessEqual(len(self.state["last_failure"]), 3000)

    def test_timeout_message_is_clean_and_has_the_output_tail(self):
        code = self.run_seq([{"id": "slow", "run": "echo before-hang\nsleep 30", "timeout_min": 0.03}])
        self.assertEqual(code, 1)
        self.assertIn("timed out after 1.8s", self.errors())
        self.assertIn("before-hang", self.errors())


class TestValidationAndListing(CommandCase):
    def check(self, step, ok=False):
        seq = {"steps": [dict({"id": "s"}, **step)]}
        if ok:
            runner.validate(seq)
        else:
            with self.assertRaises(ValueError):
                runner.validate(seq)

    def test_exactly_one_step_kind(self):
        self.check({"run": "true", "python": "pass"})
        self.check({"run": "true", "skill": "seq-build"})
        self.check({"run": "true", "wait": "approval"})
        self.check({})

    def test_command_must_be_a_non_empty_string(self):
        self.check({"run": ""})
        self.check({"run": "  \n"})
        self.check({"python": ["print(1)"]})
        self.check({"run": "true"}, ok=True)
        self.check({"python": "print(1)"}, ok=True)

    def test_retries_must_be_a_whole_number_on_a_command_step(self):
        self.check({"run": "true", "retries": -1})
        self.check({"run": "true", "retries": 1.5})
        self.check({"run": "true", "retries": True})
        self.check({"skill": "seq-build", "retries": 1})
        self.check({"run": "true", "retries": 2}, ok=True)

    def test_step_ids_must_be_plain_names(self):
        for bad in ("a/b", "../escape", ".hidden", "", 5, None, "a b"):
            self.check({"id": bad, "run": "true"})
        for good in ("build", "make-it", "v1.2", "step_2", "7"):
            self.check({"id": good, "run": "true"}, ok=True)

    def test_command_steps_whose_output_variables_collide_are_rejected(self):
        with self.assertRaises(ValueError):
            runner.validate({"steps": [{"id": "a-b", "run": "true"}, {"id": "a_b", "run": "true"}]})
        runner.validate({"steps": [{"id": "a-b", "skill": "seq-build"}, {"id": "a_b", "run": "true"}]})

    def test_timeout_must_be_a_positive_number(self):
        for bad in (0, -1, "5", True, None, float("inf")):
            self.check({"run": "true", "timeout_min": bad})
        self.check({"run": "true", "timeout_min": 0.5}, ok=True)

    def test_steps_must_be_mappings_with_an_id(self):
        for bad in (5, "run", None, {"run": "true"}):
            with self.assertRaises(ValueError):
                runner.validate({"steps": [bad]})

    def test_readme_example_is_a_valid_sequence(self):
        import re
        readme = (runner.REPO / "README.md").read_text()
        section = readme.split("## Command and code steps")[1].split("\n## ")[0]
        example = re.search(r"```yaml\n(.*?)```", section, re.S).group(1)
        runner.validate(yaml.safe_load(example))

    def test_list_survives_a_sequence_with_a_broken_step_and_no_description(self):
        home = self.base / "home"
        (home / "sequences").mkdir(parents=True)
        (home / "sequences" / "odd.yaml").write_text(yaml.safe_dump({"name": "odd", "steps": [5, {"id": "a", "run": "true"}]}))
        with mock.patch.dict(os.environ, {"SEQUENCES_HOME": str(home)}):
            self.assertIn("odd: [runs commands]\n", runner.list_sequences())

    def test_bad_command_step_exits_2_before_anything_runs(self):
        path = self.base / "bad.yaml"
        path.write_text(yaml.safe_dump({"name": "bad", "steps": [{"id": "x", "run": ""}]}))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(runner.main(["run", str(path), "b", "--target", str(self.target)]), 2)
        self.assertIn("non-empty string", err.getvalue())
        self.assertFalse(self.target.exists())

    def test_list_marks_sequences_that_run_commands(self):
        home = self.base / "home"
        (home / "sequences").mkdir(parents=True)
        (home / "sequences" / "cmds.yaml").write_text(yaml.safe_dump(
            {"name": "cmds", "description": "d", "steps": [{"id": "a", "run": "true"}]}))
        with mock.patch.dict(os.environ, {"SEQUENCES_HOME": str(home)}):
            listing = runner.list_sequences()
        self.assertIn("cmds: d [runs commands]", listing)
        build_app = next(line for line in listing.splitlines() if line.startswith("build-app:"))
        self.assertNotIn("[runs commands]", build_app)


if __name__ == "__main__":
    unittest.main()
