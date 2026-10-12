import csv
import json
import os

import pytest

from ifm_advisor.cli import main
from ifm_advisor.summarize import (
    PASS_RATE_COLUMNS,
    read_attempt,
    summarize_arm,
    wilson_interval,
)


def _attempt(
    root, name, *, reward=None, task="toy", summary=None, trace=(), audit=None, torn=False
):
    directory = root / name
    directory.mkdir(parents=True)
    if reward is not None:
        result = {"task_name": task, "verifier_result": {"rewards": {"reward": reward}}}
        (directory / "result.json").write_text(json.dumps(result))
    if summary is not None or trace or audit is not None:
        (directory / "advisor").mkdir()
    if summary is not None:
        (directory / "advisor" / "summary.json").write_text(json.dumps(summary))
    if trace:
        lines = [json.dumps(record) for record in trace]
        if torn:
            lines.append('{"trigger": "pre_sub')
        (directory / "advisor" / "private_trace.jsonl").write_text("\n".join(lines) + "\n")
    if audit is not None:
        (directory / "advisor" / "audit.json").write_text(json.dumps({"verdict": audit}))
    return directory


def _record(trigger, action, *, intervened=True):
    return {
        "trigger": trigger,
        "intervened": intervened,
        "decision": {"action": action, "hint": "SECRET-HINT", "plan": "SECRET-PLAN"},
    }


@pytest.fixture
def runs(tmp_path):
    base = tmp_path / "base"
    _attempt(base, "a1", reward=1.0, task="t1")
    _attempt(base, "a2", reward=0.0, task="t2")
    _attempt(base, "a3")  # no verifier output: unverified
    advised = tmp_path / "adv"
    _attempt(
        advised,
        "b1",
        reward=1.0,
        task="t1",
        summary={
            "task": "t1",
            "interventions": 1,
            "advisor_failures": 0,
            "hint_screen_fallbacks": 0,
        },
        trace=[_record("pre_submit", "RUN_BROADER_VALIDATION")],
        audit="pass_mechanical_only",
    )
    _attempt(
        advised,
        "b2",
        reward=0.0,
        task="t2",
        summary={
            "task": "t2",
            "interventions": 0,
            "advisor_failures": 2,
            "hint_screen_fallbacks": 0,
        },
        trace=[_record("periodic", "CONTINUE", intervened=False)],
        audit="fail",
    )
    _attempt(
        advised,
        "b3",
        reward=1.0,
        task="t2",
        summary={
            "task": "t2",
            "interventions": 1,
            "advisor_failures": 0,
            "hint_screen_fallbacks": 1,
        },
        trace=[_record("periodic", "CONFIRM_AND_SUBMIT")],
        torn=True,
    )
    return tmp_path


@pytest.mark.parametrize(
    ("successes", "trials", "low", "high"),
    [(5, 10, 0.2366, 0.7634), (10, 10, 0.7225, 1.0), (0, 69, 0.0, 0.0527)],
)
def test_wilson_interval(successes, trials, low, high):
    lower, upper = wilson_interval(successes, trials)

    assert lower == pytest.approx(low, abs=1e-3)
    assert upper == pytest.approx(high, abs=1e-3)


def test_wilson_interval_of_nothing_is_zero():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


def test_a_torn_trace_line_is_skipped(runs):
    outcome = read_attempt(runs / "adv" / "b3")

    assert dict(outcome.reviews_by_trigger) == {"periodic": 1}


def test_arm_summaries(runs):
    baseline = summarize_arm("baseline", sorted((runs / "base").iterdir()))
    advised = summarize_arm("advised", sorted((runs / "adv").iterdir()))

    assert (baseline["attempts"], baseline["correct"], baseline["unverified"]) == (3, 1, 1)
    assert baseline["tasks"] == 2
    assert baseline["advised_attempts"] == 0
    assert advised["attempts"] == 3 and advised["correct"] == 2
    assert advised["attempts_with_intervention"] == 2
    assert advised["interventions"] == 2
    assert advised["reviews_by_trigger"] == {"periodic": 2, "pre_submit": 1}
    assert advised["interventions_by_action"] == {
        "CONFIRM_AND_SUBMIT": 1,
        "RUN_BROADER_VALIDATION": 1,
    }
    assert advised["effectively_unadvised"] == 1
    assert advised["hint_screen_fallbacks"] == 1
    assert advised["audit"] == {"fail": 1, "pass_mechanical_only": 1}
    assert advised["pass_rate_ci95"] == list(wilson_interval(2, 3))


def test_cli_writes_the_results_csvs(runs, capsys):
    pass_rates = runs / "out" / "pass_rates.csv"
    interventions = runs / "out" / "interventions.csv"

    code = main(
        [
            "summarize",
            "--arm",
            f"baseline={runs / 'base' / '*'}",
            "--arm",
            f"advised={runs / 'adv' / '*'}",
            "--csv",
            str(pass_rates),
            "--interventions-csv",
            str(interventions),
            "--benchmark",
            "toy-bench",
            "--solver",
            "K2-Horizon",
            "--advisor",
            "K2-Horizon",
        ]
    )

    assert code == 0
    assert pass_rates.read_text().splitlines()[0] == ",".join(PASS_RATE_COLUMNS)
    rows = list(csv.DictReader(pass_rates.open()))
    assert rows == [
        {
            "benchmark": "toy-bench",
            "arm": "baseline",
            "solver": "K2-Horizon",
            "advisor": "",
            "tasks": "2",
            "attempts": "3",
            "correct": "1",
        },
        {
            "benchmark": "toy-bench",
            "arm": "advised",
            "solver": "K2-Horizon",
            "advisor": "K2-Horizon",
            "tasks": "2",
            "attempts": "3",
            "correct": "2",
        },
    ]
    intervention_rows = list(csv.DictReader(interventions.open()))
    assert {(row["trigger"], row["action"], row["count"]) for row in intervention_rows} == {
        ("periodic", "CONFIRM_AND_SUBMIT", "1"),
        ("pre_submit", "RUN_BROADER_VALIDATION", "1"),
    }
    assert "baseline" in capsys.readouterr().out


def test_cli_never_prints_hints_or_plans(runs, capsys):
    assert main(["summarize", "--arm", f"advised={runs / 'adv' / '*'}", "--json"]) == 0

    out = capsys.readouterr().out
    assert "SECRET-HINT" not in out and "SECRET-PLAN" not in out
    assert json.loads(out)[0]["arm"] == "advised"


def test_cli_rejects_a_malformed_arm(capsys):
    assert main(["summarize", "--arm", "no-equals-sign"]) == 2
    assert "NAME=GLOB" in capsys.readouterr().err


def test_cli_warns_about_an_empty_arm(tmp_path, capsys):
    assert main(["summarize", "--arm", f"empty={tmp_path / 'nothing' / '*'}"]) == 0
    assert "matched no directories" in capsys.readouterr().err


def test_advised_attempts_without_reviews_are_counted(tmp_path, capsys):
    arm = tmp_path / "arm"
    counters = {"task": "t", "interventions": 0, "advisor_failures": 0, "hint_screen_fallbacks": 0}
    _attempt(
        arm,
        "never-reviewed",
        reward=0.0,
        summary={**counters, "solver_calls": 40, "finish_reviews": 0, "periodic_reviews": 0},
    )
    _attempt(
        arm,
        "reviewed",
        reward=1.0,
        summary={**counters, "solver_calls": 12, "finish_reviews": 1, "periodic_reviews": 0},
        trace=[_record("pre_submit", "CONTINUE", intervened=False)],
    )
    _attempt(arm, "unadvised", reward=1.0)

    assert summarize_arm("advised", sorted(arm.iterdir()))["advised_without_reviews"] == 1

    assert main(["summarize", "--arm", f"advised={arm / '*'}"]) == 0
    header, _, row = capsys.readouterr().out.splitlines()
    assert header.split()[-1] == "unreviewed"
    assert row.split()[-1] == "1"


def test_review_counts_fall_back_to_the_trace(runs):
    """A summary without review counters is judged by its trace, like interventions."""

    advised = summarize_arm("advised", sorted((runs / "adv").iterdir()))

    assert advised["advised_without_reviews"] == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_cli_reports_an_unreadable_attempt_file_as_unusable_input(tmp_path, capsys):
    attempt = _attempt(tmp_path / "runs", "a1", reward=1.0)
    (attempt / "result.json").chmod(0)
    try:
        code = main(["summarize", "--arm", f"base={tmp_path / 'runs' / '*'}"])
    finally:
        (attempt / "result.json").chmod(0o644)

    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "result.json" in err
