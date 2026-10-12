import asyncio
import json
import logging

import pytest
from helpers import (
    FINISH_TOOL,
    ScriptedAdvisor,
    ScriptedSolver,
    bash_response,
    continue_text,
    decision_text,
    dict_response,
    finish_response,
)

from ifm_advisor.advisor import Advisor, without_tool
from ifm_advisor.config import ReasoningPrefillCue
from ifm_advisor.cues import compile_self_check_instruction
from ifm_advisor.errors import AdvisorRequestError
from ifm_advisor.material import TaskMaterial

MESSAGES = [
    {"role": "system", "content": "You are a software engineer."},
    {"role": "user", "content": "Fix the session window processor."},
    {"role": "assistant", "content": "Reading the design document."},
    {"role": "user", "content": "DESIGN.md contents"},
]
TOOLS = [
    {"type": "function", "function": {"name": "bash"}},
    {"type": "function", "function": {"name": FINISH_TOOL}},
]
PREFILL = ReasoningPrefillCue.k2_horizon_sglang()
HINT = "Hmm. Only the single-session path has run so far."
SENTENCE = compile_self_check_instruction("RUN_BROADER_VALIDATION")


def make(config, task, tmp_path, *answers, **kwargs):
    model = ScriptedAdvisor(*answers)
    advisor = Advisor(
        config,
        task,
        finish_tool=FINISH_TOOL,
        trace_dir=tmp_path / "advisor",
        complete=model,
        attempt_id="attempt-1",
        **kwargs,
    )
    return advisor, model


def configure(config, **updates):
    return config.model_copy(update=updates)


def trace(tmp_path):
    path = tmp_path / "advisor" / "private_trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- gate -------------------------------------------------------------------------------------


async def test_non_finish_draft_is_returned_unchanged(advisor_config, task, tmp_path):
    advisor, model = make(advisor_config, task, tmp_path)
    solver = ScriptedSolver()
    draft = bash_response()

    assert await advisor.review(MESSAGES, draft, tools=TOOLS, regenerate=solver) is draft
    assert (advisor.solver_calls, advisor.finish_reviews) == (1, 0)
    assert model.requests == [] and solver.revisions == [] and trace(tmp_path) == []


@pytest.mark.parametrize("dict_shape", [False, True])
async def test_finish_draft_gets_one_review_and_one_revision(
    advisor_config, task, tmp_path, dict_shape
):
    advisor, model = make(advisor_config, task, tmp_path, decision_text())
    revised = bash_response("pytest -q tests/", dict_shape=dict_shape)
    solver = ScriptedSolver(revised)
    messages = [dict(message) for message in MESSAGES]

    response = await advisor.review(
        messages, finish_response(dict_shape=dict_shape), tools=TOOLS, regenerate=solver
    )

    assert response is revised
    assert len(model.requests) == 1 and len(solver.revisions) == 1
    assert advisor.finish_reviews == 1
    assert advisor.interventions == 1
    assert advisor.revision_calls == 1
    assert advisor.advisor_failures == 0
    assert messages == MESSAGES  # the caller's history is never mutated


async def test_continue_keeps_the_draft(advisor_config, task, tmp_path):
    advisor, _ = make(advisor_config, task, tmp_path, continue_text())
    draft = finish_response()

    assert await advisor.review(MESSAGES, draft, regenerate=ScriptedSolver()) is draft
    assert advisor.interventions == 0
    records = trace(tmp_path)
    assert len(records) == 1 and records[0]["intervened"] is False


async def test_a_finish_on_a_periodic_turn_is_reviewed_once_as_pre_submit(
    advisor_config, task, tmp_path
):
    config = configure(advisor_config, periodic_review_interval=1)
    advisor, model = make(config, task, tmp_path, continue_text())

    await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver())

    assert len(model.requests) == 1
    assert (advisor.finish_reviews, advisor.periodic_reviews) == (1, 0)
    assert "=== REVIEW TRIGGER ===\npre_submit" in model.user_contents[0]


async def test_periodic_review_waits_for_the_interval(advisor_config, task, tmp_path):
    config = configure(advisor_config, periodic_review_interval=4)
    advisor, model = make(config, task, tmp_path)

    for _ in range(3):
        await advisor.review(MESSAGES, bash_response(), regenerate=ScriptedSolver())

    assert model.requests == [] and advisor.periodic_reviews == 0


async def test_periodic_review_runs_every_k_turns(advisor_config, task, tmp_path):
    config = configure(advisor_config, periodic_review_interval=2)
    advisor, _ = make(config, task, tmp_path, continue_text(), continue_text())

    for _ in range(4):
        await advisor.review(MESSAGES, bash_response(), regenerate=ScriptedSolver())

    records = trace(tmp_path)
    assert (advisor.periodic_reviews, advisor.finish_reviews) == (2, 0)
    assert [record["trigger"] for record in records] == ["periodic", "periodic"]
    assert [record["solver_call_index"] for record in records] == [2, 4]
    assert records[0]["draft_attempted_finish"] is False


async def test_periodic_review_can_intervene_mid_task(advisor_config, task, tmp_path):
    config = configure(advisor_config, periodic_review_interval=1)
    advisor, _ = make(config, task, tmp_path, decision_text())
    revised = bash_response("pytest -q tests/")
    solver = ScriptedSolver(revised)

    assert await advisor.review(MESSAGES, bash_response(), regenerate=solver) is revised
    assert (advisor.periodic_reviews, advisor.interventions) == (1, 1)
    assert solver.revisions[0].messages[-1]["role"] == "user"


async def test_the_intervention_budget_ends_reviews(advisor_config, task, tmp_path):
    config = configure(advisor_config, max_interventions=1, max_revision_attempts=1)
    advisor, model = make(config, task, tmp_path, decision_text())
    final_draft = finish_response()

    await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(finish_response()))
    second = await advisor.review(MESSAGES, final_draft, regenerate=ScriptedSolver())

    assert second is final_draft
    assert advisor.interventions == 1 and advisor.finish_reviews == 1
    assert len(model.requests) == 1
    assert advisor.interventions_remaining == 0


# --- what reaches the solver ------------------------------------------------------------------


async def test_only_the_compiled_sentence_reaches_the_solver(advisor_config, task, tmp_path):
    advisor, _ = make(
        advisor_config, task, tmp_path, decision_text(action="TRACE_STATE_TRANSITIONS")
    )
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), tools=TOOLS, regenerate=solver)

    revision = solver.revisions[0]
    assert revision.messages[:-1] == MESSAGES
    assert revision.messages[-1] == {
        "role": "user",
        "content": compile_self_check_instruction("TRACE_STATE_TRANSITIONS"),
    }
    assert revision.extra_body == {}
    serialized = json.dumps(revision.messages)
    for marker in (
        "PRIVILEGED-DIAGNOSIS-MARKER",
        "fix the gc path",  # the decision's private plan
        "REFERENCE_GC_IMPLEMENTATION",
        "test_unfired_survives_gc",
        "CHEAT_MARKER",
        HINT,
    ):
        assert marker not in serialized


async def test_revision_removes_the_finish_tool_but_keeps_the_others(
    advisor_config, task, tmp_path
):
    advisor, _ = make(advisor_config, task, tmp_path, decision_text())
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), tools=TOOLS, regenerate=solver)

    assert solver.revisions[0].tools == [TOOLS[0]]


async def test_no_tools_stays_no_tools(advisor_config, task, tmp_path):
    advisor, _ = make(advisor_config, task, tmp_path, decision_text())
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), regenerate=solver)

    assert solver.revisions[0].tools is None


async def test_confirm_and_submit_keeps_the_finish_tool(advisor_config, task, tmp_path):
    """The one action that asks the solver to finish must leave it able to."""

    config = configure(advisor_config, periodic_review_interval=1)
    advisor, _ = make(
        config, task, tmp_path, decision_text(action="CONFIRM_AND_SUBMIT", reason_code="COMPLETE")
    )
    solver = ScriptedSolver(finish_response())

    await advisor.review(MESSAGES, bash_response(), tools=TOOLS, regenerate=solver)

    assert solver.revisions[0].tools == TOOLS
    assert len(solver.revisions) == 1  # a finishing revision is the goal here, not retried


async def test_revision_is_resampled_when_it_still_finishes(advisor_config, task, tmp_path):
    advisor, _ = make(advisor_config, task, tmp_path, decision_text())
    second = bash_response()

    response = await advisor.review(
        MESSAGES, finish_response(), regenerate=ScriptedSolver(finish_response(), second)
    )

    assert response is second
    record = trace(tmp_path)[0]
    assert record["revision_attempts"] == 2
    assert record["revision_attempted_finish"] is False


async def test_revision_gives_up_after_max_revision_attempts(advisor_config, task, tmp_path):
    config = configure(advisor_config, max_revision_attempts=1)
    advisor, _ = make(config, task, tmp_path, decision_text())
    stuck = finish_response()

    assert (
        await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(stuck)) is stuck
    )
    record = trace(tmp_path)[0]
    assert record["revision_attempts"] == 1
    assert record["revision_attempted_finish"] is True


# --- reasoning prefill ------------------------------------------------------------------------


async def test_prefill_delivers_the_hint_as_an_assistant_prefix(advisor_config, task, tmp_path):
    config = configure(advisor_config, cue=PREFILL)
    advisor, _ = make(config, task, tmp_path, decision_text(hint=HINT))
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), tools=TOOLS, regenerate=solver)

    revision = solver.revisions[0]
    assert revision.messages[:-1] == MESSAGES
    assert revision.messages[-1] == {"role": "assistant", "content": HINT}
    assert revision.extra_body == {"continue_final_message": True}
    assert revision.tools == [TOOLS[0]]


async def test_prefill_prefix_precedes_the_hint(advisor_config, task, tmp_path):
    config = configure(advisor_config, cue=ReasoningPrefillCue(prefix="<think>\n"))
    advisor, _ = make(config, task, tmp_path, decision_text(hint=HINT))
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), regenerate=solver)

    assert solver.revisions[0].messages[-1]["content"] == "<think>\n" + HINT


async def test_hint_is_spliced_back_into_the_recorded_reasoning(advisor_config, task, tmp_path):
    """continue_final_message returns only new tokens; the committed turn must read whole."""

    config = configure(advisor_config, cue=PREFILL)
    hint = "Hmm. `gc.collect` reclaimed that session but it never fired."
    continuation = " That doesn't square with what DESIGN.md says retention is for."
    revised = dict_response(
        content="Checking.",
        tool_calls=(("bash", '{"command": "cat gc.py"}'),),
        reasoning_content=continuation,
        reasoning_tokens=412,
    )
    advisor, _ = make(config, task, tmp_path, decision_text(hint=hint))

    response = await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(revised))

    assert response["choices"][0]["message"]["reasoning_content"] == hint + continuation
    record = trace(tmp_path)[-1]
    assert record["hint_persisted_to_trajectory"] is True
    assert record["revision_reasoning_tokens"] == 412


async def test_continuation_suffix_is_appended_rotated_and_spliced(advisor_config, task, tmp_path):
    config = configure(
        advisor_config,
        cue=ReasoningPrefillCue(continuation_suffixes=("Let me think.", "Hmm.")),
        periodic_review_interval=1,
        max_interventions=3,
    )
    hint = "The retention check still reclaims a session that never fired."
    revised = dict_response(
        content="Checking.",
        tool_calls=(("bash", "{}"),),
        reasoning_content=" So retention cannot be the whole story.",
    )
    advisor, _ = make(config, task, tmp_path, decision_text(hint=hint), decision_text(hint=hint))
    solver = ScriptedSolver(revised, bash_response())

    first = await advisor.review(MESSAGES, bash_response(), regenerate=solver)
    await advisor.review(MESSAGES, bash_response(), regenerate=solver)

    assert [revision.messages[-1]["content"] for revision in solver.revisions] == [
        hint + " Let me think.",
        hint + " Hmm.",
    ]
    assert first["choices"][0]["message"]["reasoning_content"] == (
        hint + " Let me think. So retention cannot be the whole story."
    )
    assert [record["hint_continuation_suffix"] for record in trace(tmp_path)] == [
        "Let me think.",
        "Hmm.",
    ]


async def test_self_check_path_never_touches_reasoning_content(advisor_config, task, tmp_path):
    revised = dict_response(
        content="Checking.",
        tool_calls=(("bash", "{}"),),
        reasoning_content="Independent reasoning.",
        reasoning_tokens=77,
    )
    advisor, _ = make(advisor_config, task, tmp_path, decision_text())

    response = await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(revised))

    assert response["choices"][0]["message"]["reasoning_content"] == "Independent reasoning."
    record = trace(tmp_path)[-1]
    assert record["hint_persisted_to_trajectory"] is None
    assert record["revision_reasoning_tokens"] == 77


async def test_empty_hint_on_the_prefill_path_falls_back_to_the_sentence(
    advisor_config, task, tmp_path
):
    config = configure(advisor_config, cue=PREFILL)
    advisor, _ = make(config, task, tmp_path, decision_text(hint="   "))
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), regenerate=solver)

    assert solver.revisions[0].messages[-1] == {"role": "user", "content": SENTENCE}
    assert solver.revisions[0].extra_body == {}
    assert advisor.empty_hint_fallbacks == 1
    assert trace(tmp_path)[0]["cue_fallback"] == "empty_hint"


# --- hint screening ---------------------------------------------------------------------------

LEAKY_TASK = TaskMaterial.from_texts(
    "Fix the processor.",
    {
        "tests/test_gc.py": (
            "def test_unfired_sessions_survive_garbage_collection(processor):\n    ...\n"
        )
    },
    name="toy",
)


@pytest.mark.parametrize(
    "hint",
    [
        "Hmm. Per the hidden tests, an empty list must raise.",
        "def test_unfired_sessions_survive_garbage_collection(processor): is what matters.",
    ],
)
async def test_screen_hints_replaces_a_leaky_hint_with_the_sentence(advisor_config, tmp_path, hint):
    config = configure(advisor_config, cue=PREFILL, screen_hints=True)
    advisor, _ = make(config, LEAKY_TASK, tmp_path, decision_text(hint=hint))
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), regenerate=solver)

    assert solver.revisions[0].messages[-1] == {"role": "user", "content": SENTENCE}
    assert hint not in json.dumps(solver.revisions[0].messages)
    record = trace(tmp_path)[0]
    assert record["cue_fallback"] == "hint_screen"
    assert record["hint_screen_hits"]
    assert advisor.hint_screen_fallbacks == 1
    assert advisor.delivered_cues == [SENTENCE]


async def test_screen_hints_lets_a_clean_hint_through(advisor_config, tmp_path):
    config = configure(advisor_config, cue=PREFILL, screen_hints=True)
    advisor, _ = make(config, LEAKY_TASK, tmp_path, decision_text(hint=HINT))
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), regenerate=solver)

    assert solver.revisions[0].messages[-1] == {"role": "assistant", "content": HINT}
    assert advisor.hint_screen_fallbacks == 0


async def test_without_screen_hints_the_hint_is_delivered_as_written(advisor_config, tmp_path):
    """The default matches the paper's runs: leaks are caught by the audit, not inline."""

    leaky = "Hmm. Per the hidden tests, an empty list must raise."
    config = configure(advisor_config, cue=PREFILL)
    advisor, _ = make(config, LEAKY_TASK, tmp_path, decision_text(hint=leaky))
    solver = ScriptedSolver(bash_response())

    await advisor.review(MESSAGES, finish_response(), regenerate=solver)

    assert solver.revisions[0].messages[-1] == {"role": "assistant", "content": leaky}


# --- history-awareness ------------------------------------------------------------------------


async def test_delivered_hints_are_fed_back_to_the_next_review(advisor_config, task, tmp_path):
    config = configure(advisor_config, cue=PREFILL, periodic_review_interval=1, max_interventions=3)
    first = "The retention check still reclaims a session that never fired."
    second = "A merged session keeps the older created_at."
    advisor, model = make(
        config,
        task,
        tmp_path,
        decision_text(hint=first),
        decision_text(hint=second),
        decision_text(hint="third"),
    )
    solver = ScriptedSolver(bash_response(), bash_response(), bash_response())

    for _ in range(3):
        await advisor.review(MESSAGES, bash_response(), regenerate=solver)

    contents = model.user_contents
    assert "HINTS YOU HAVE ALREADY DELIVERED" not in contents[0]
    assert "You have intervened 1 time(s) so far." in contents[1] and first in contents[1]
    assert "You have intervened 2 time(s) so far." in contents[2]
    assert first in contents[2] and second in contents[2]
    assert advisor.delivered_cues == [first, second, "third"]


async def test_self_check_path_records_the_sentence_the_solver_saw(advisor_config, task, tmp_path):
    """The advisor must be told what the solver received, not a hint it never saw."""

    config = configure(advisor_config, periodic_review_interval=1)
    advisor, model = make(
        config, task, tmp_path, decision_text(hint="NEVER-DELIVERED-HINT"), continue_text()
    )

    await advisor.review(MESSAGES, bash_response(), regenerate=ScriptedSolver(bash_response()))
    await advisor.review(MESSAGES, bash_response(), regenerate=ScriptedSolver())

    assert advisor.delivered_cues == [SENTENCE]
    assert SENTENCE in model.user_contents[1]
    assert "NEVER-DELIVERED-HINT" not in model.user_contents[1]


async def test_an_undelivered_cue_does_not_count_as_already_said(advisor_config, task, tmp_path):
    config = configure(advisor_config, cue=PREFILL, periodic_review_interval=1)
    advisor, _ = make(config, task, tmp_path, decision_text(hint="never reaches the solver"))
    draft = bash_response()

    response = await advisor.review(
        MESSAGES, draft, regenerate=ScriptedSolver(RuntimeError("revision endpoint failed"))
    )

    assert response is draft
    assert advisor.delivered_cues == []
    record = trace(tmp_path)[-1]
    assert record["intervened"] is False
    assert "revision failed" in record["error"]


async def test_plan_carries_forward_to_the_next_review(advisor_config, task, tmp_path):
    config = configure(advisor_config, periodic_review_interval=1)
    advisor, model = make(
        config, task, tmp_path, continue_text(plan="step 1 done"), continue_text(plan="step 2 done")
    )

    await advisor.review(MESSAGES, bash_response(), regenerate=ScriptedSolver())
    assert advisor.plan == "step 1 done"
    await advisor.review(MESSAGES, bash_response(), regenerate=ScriptedSolver())

    assert advisor.plan == "step 2 done"
    assert "=== YOUR PREVIOUS PLAN ===\nstep 1 done" in model.user_contents[1]


async def test_repeat_streak_is_surfaced_when_enabled(advisor_config, task, tmp_path):
    config = configure(
        advisor_config,
        periodic_review_interval=1,
        max_interventions=3,
        repeat_streak_escalation=True,
    )
    same = decision_text(reason_code="INCOMPLETE_REQUIREMENT_COVERAGE")
    advisor, model = make(
        config, task, tmp_path, same, same, decision_text(reason_code="PREMATURE_SUBMISSION")
    )
    solver = ScriptedSolver(bash_response(), bash_response(), bash_response())

    await advisor.review(MESSAGES, bash_response(), regenerate=solver)
    assert (advisor.reason_code_streak, advisor.last_reason_code) == (
        1,
        "INCOMPLETE_REQUIREMENT_COVERAGE",
    )
    await advisor.review(MESSAGES, bash_response(), regenerate=solver)
    assert advisor.reason_code_streak == 2
    await advisor.review(MESSAGES, bash_response(), regenerate=solver)

    assert (advisor.reason_code_streak, advisor.last_reason_code) == (1, "PREMATURE_SUBMISSION")
    assert "=== REPEAT SIGNAL ===" not in model.user_contents[0]
    assert (
        "'INCOMPLETE_REQUIREMENT_COVERAGE' has now recurred across 1 consecutive"
        in (model.user_contents[1])
    )
    assert "2 consecutive review" in model.user_contents[2]


async def test_repeat_streak_is_tracked_but_not_shown_by_default(advisor_config, task, tmp_path):
    config = configure(advisor_config, periodic_review_interval=1)
    same = decision_text(reason_code="INCOMPLETE_REQUIREMENT_COVERAGE")
    advisor, model = make(config, task, tmp_path, same, same)
    solver = ScriptedSolver(bash_response(), bash_response())

    await advisor.review(MESSAGES, bash_response(), regenerate=solver)
    await advisor.review(MESSAGES, bash_response(), regenerate=solver)

    assert advisor.reason_code_streak == 2
    assert "=== REPEAT SIGNAL ===" not in model.user_contents[1]


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        decision_text(action="NOT_A_REAL_ACTION"),
        "not json at all",
        '{"intervene": true}',
        decision_text()[:-1] + ', "extra": 1}',
        AdvisorRequestError("advisor response contained no choices"),
        RuntimeError("advisor endpoint refused the connection"),
    ],
)
async def test_advisor_failures_fail_open(advisor_config, task, tmp_path, answer):
    advisor, model = make(advisor_config, task, tmp_path, answer, answer)
    draft = finish_response()

    assert await advisor.review(MESSAGES, draft, regenerate=ScriptedSolver()) is draft
    assert len(model.requests) == 2  # the default allows one retry
    assert advisor.advisor_failures == 1 and advisor.interventions == 0
    record = trace(tmp_path)[0]
    assert record["error"]
    assert record["intervened"] is False
    assert record["advisor_attempts"] == 2


async def test_a_malformed_answer_is_retried_and_recovers(advisor_config, task, tmp_path):
    """A model that wraps an enum in a one-item list usually answers correctly on a resample."""

    malformed = decision_text(reason_code=["OTHER"], action=["CONTINUE"])
    advisor, _ = make(advisor_config, task, tmp_path, malformed, decision_text())

    await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(bash_response()))

    assert advisor.advisor_failures == 0 and advisor.interventions == 1
    record = trace(tmp_path)[0]
    assert record["error"] is None and record["advisor_attempts"] == 2


async def test_advisor_timeout_fails_open(advisor_config, task, tmp_path):
    endpoint = advisor_config.endpoint.model_copy(update={"timeout_sec": 0.01})
    config = configure(advisor_config, endpoint=endpoint)

    async def never_returns(_messages):
        await asyncio.sleep(10)

    advisor, _ = make(config, task, tmp_path, never_returns, never_returns)
    draft = finish_response()

    assert await advisor.review(MESSAGES, draft, regenerate=ScriptedSolver()) is draft
    assert advisor.advisor_failures == 1
    assert "TimeoutError" in trace(tmp_path)[0]["error"]


async def test_fail_closed_propagates_the_advisor_error(advisor_config, task, tmp_path):
    config = configure(advisor_config, fail_open=False)
    error = RuntimeError("advisor endpoint is down")
    advisor, _ = make(config, task, tmp_path, error, error)

    with pytest.raises(RuntimeError, match="advisor endpoint is down"):
        await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver())
    assert trace(tmp_path)[0]["error"]


async def test_fail_closed_propagates_a_revision_error(advisor_config, task, tmp_path):
    config = configure(advisor_config, fail_open=False)
    advisor, _ = make(config, task, tmp_path, decision_text())

    with pytest.raises(RuntimeError, match="solver endpoint failed"):
        await advisor.review(
            MESSAGES,
            finish_response(),
            regenerate=ScriptedSolver(RuntimeError("solver endpoint failed")),
        )


async def test_revision_failure_falls_back_to_the_draft(advisor_config, task, tmp_path):
    advisor, _ = make(advisor_config, task, tmp_path, decision_text())
    draft = finish_response()

    response = await advisor.review(
        MESSAGES, draft, regenerate=ScriptedSolver(RuntimeError("solver endpoint failed"))
    )

    assert response is draft
    assert advisor.interventions == 0
    assert "revision failed" in trace(tmp_path)[0]["error"]


async def test_cancellation_during_the_advisor_call_propagates(advisor_config, task, tmp_path):
    """An attempt timeout or a shutdown drain must never be swallowed by fail_open."""

    started = asyncio.Event()

    async def hangs(_messages):
        started.set()
        await asyncio.sleep(10)

    advisor, _ = make(advisor_config, task, tmp_path, hangs)
    review = asyncio.create_task(
        advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver())
    )
    await started.wait()
    review.cancel()

    with pytest.raises(asyncio.CancelledError):
        await review
    assert advisor.advisor_failures == 0


# --- construction, summary and artifacts ------------------------------------------------------


def test_missing_api_key_fails_at_construction(advisor_config, task, monkeypatch):
    monkeypatch.delenv("TEST_ADVISOR_API_KEY")

    with pytest.raises(AdvisorRequestError, match="TEST_ADVISOR_API_KEY"):
        Advisor(advisor_config, task, finish_tool=FINISH_TOOL)


def test_a_custom_backend_needs_no_api_key(advisor_config, task, monkeypatch):
    monkeypatch.delenv("TEST_ADVISOR_API_KEY")

    advisor = Advisor(advisor_config, task, finish_tool=FINISH_TOOL, complete=ScriptedAdvisor())

    assert advisor.interventions_remaining == 2


def test_unknown_solver_profile_fails_at_construction(advisor_config, task):
    with pytest.raises(AdvisorRequestError, match="unavailable"):
        Advisor(
            configure(advisor_config, solver_profile="no_such_profile"),
            task,
            finish_tool=FINISH_TOOL,
            complete=ScriptedAdvisor(),
        )


def test_finish_tool_is_required(advisor_config, task):
    with pytest.raises(ValueError, match="finish_tool"):
        Advisor(advisor_config, task, finish_tool="", complete=ScriptedAdvisor())


MISSING_FINISH_TOOL = "is not among the tools offered to the solver"


async def test_a_missing_finish_tool_is_warned_about_once(advisor_config, task, tmp_path, caplog):
    advisor, _ = make(advisor_config, task, tmp_path)
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    for _ in range(2):
        await advisor.review(
            MESSAGES, bash_response(), tools=[TOOLS[0]], regenerate=ScriptedSolver()
        )

    warnings = [record for record in caplog.records if MISSING_FINISH_TOOL in record.getMessage()]
    assert len(warnings) == 1
    assert repr(FINISH_TOOL) in warnings[0].getMessage()
    assert warnings[0].levelno == logging.WARNING


@pytest.mark.parametrize("tools", [TOOLS, None])
async def test_an_offered_finish_tool_is_not_warned_about(
    advisor_config, task, tmp_path, caplog, tools
):
    advisor, _ = make(advisor_config, task, tmp_path)
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    await advisor.review(MESSAGES, bash_response(), tools=tools, regenerate=ScriptedSolver())

    assert not [record for record in caplog.records if MISSING_FINISH_TOOL in record.getMessage()]


def test_empty_privileged_material_is_warned_about(advisor_config, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    make(advisor_config, TaskMaterial.from_texts("Fix it.", {}), tmp_path)

    assert [
        record
        for record in caplog.records
        if "no privileged material to review against" in record.getMessage()
    ]


def test_whitespace_only_privileged_material_is_warned_about(advisor_config, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    make(advisor_config, TaskMaterial.from_texts("Fix it.", {"tests/t.py": "  \n\n"}), tmp_path)

    assert [
        record
        for record in caplog.records
        if "no privileged material to review against" in record.getMessage()
    ]


def test_loaded_privileged_material_is_not_warned_about(advisor_config, task, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    make(advisor_config, task, tmp_path)

    assert not [record for record in caplog.records if "no privileged material" in record.message]


def test_construction_writes_the_manifest_and_a_summary(advisor_config, task, tmp_path):
    make(advisor_config, task, tmp_path)

    manifest = json.loads((tmp_path / "advisor" / "task_material_manifest.json").read_text())
    assert manifest["task"] == "session-window-debug"
    assert "REFERENCE_GC_IMPLEMENTATION" not in json.dumps(manifest)
    summary = json.loads((tmp_path / "advisor" / "summary.json").read_text())
    assert summary["solver_calls"] == 0


async def test_no_trace_dir_writes_nothing(advisor_config, task, tmp_path):
    advisor = Advisor(
        advisor_config, task, finish_tool=FINISH_TOOL, complete=ScriptedAdvisor(decision_text())
    )

    await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(bash_response()))

    assert not (tmp_path / "advisor").exists()


async def test_summary_holds_counters_and_no_private_text(advisor_config, task, tmp_path):
    advisor, _ = make(advisor_config, task, tmp_path, decision_text(), solver_model="solver-model")

    await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(bash_response()))

    assert advisor.summary() == {
        "enabled": True,
        "attempt_id": "attempt-1",
        "task": "session-window-debug",
        "solver_model": "solver-model",
        "advisor_model": "advisor-model",
        "prompt_version": "advisor_v1",
        "solver_profile": "generic",
        "cue": "self_check",
        "solver_calls": 1,
        "finish_reviews": 1,
        "periodic_reviews": 0,
        "interventions": 1,
        "advisor_failures": 0,
        "revision_calls": 1,
        "hint_screen_fallbacks": 0,
        "empty_hint_fallbacks": 0,
    }
    written = (tmp_path / "advisor" / "summary.json").read_text()
    for secret in ("PRIVILEGED-DIAGNOSIS-MARKER", "fix the gc path", HINT):
        assert secret not in written


async def test_write_summary_captures_unreviewed_turns(advisor_config, task, tmp_path):
    advisor, _ = make(advisor_config, task, tmp_path)

    for _ in range(3):
        await advisor.review(MESSAGES, bash_response(), regenerate=ScriptedSolver())
    advisor.write_summary()

    summary = json.loads((tmp_path / "advisor" / "summary.json").read_text())
    assert summary["solver_calls"] == 3


async def test_trace_keeps_the_full_decision_privately(advisor_config, task, tmp_path):
    advisor, _ = make(advisor_config, task, tmp_path, decision_text())

    await advisor.review(MESSAGES, finish_response(), regenerate=ScriptedSolver(bash_response()))

    record = trace(tmp_path)[0]
    assert record["decision"]["private_diagnosis"] == "PRIVILEGED-DIAGNOSIS-MARKER"
    assert record["delivered_cue"] == SENTENCE
    assert record["attempt_id"] == "attempt-1"
    assert record["task"] == "session-window-debug"
    assert record["cue_kind"] == "self_check"
    assert record["privileged_files"] == 5


def test_without_tool():
    assert without_tool(None, FINISH_TOOL) is None
    assert without_tool(TOOLS, FINISH_TOOL) == [TOOLS[0]]
    assert without_tool([{"type": "function"}, "raw"], FINISH_TOOL) == [{"type": "function"}, "raw"]
