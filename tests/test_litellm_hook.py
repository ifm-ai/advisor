import asyncio
import copy
import json
import logging

import pytest
from helpers import FINISH_TOOL, FakeLiteLLM, bash_response, decision_response, finish_response

from ifm_advisor import litellm_hook
from ifm_advisor.advisor import Advisor
from ifm_advisor.backend import litellm_backend
from ifm_advisor.config import ReasoningPrefillCue
from ifm_advisor.cues import SELF_CHECK_PREAMBLE

SOLVER_MESSAGES = [
    {"role": "system", "content": "You are a software engineer."},
    {"role": "user", "content": "Fix the session window processor."},
]
TOOLS = [
    {"type": "function", "function": {"name": "bash"}},
    {"type": "function", "function": {"name": FINISH_TOOL}},
]


def _advisor(config, task, tmp_path, fake, *, name="attempt-1"):
    return Advisor(
        config,
        task,
        finish_tool=FINISH_TOOL,
        trace_dir=tmp_path / name / "advisor",
        complete=litellm_backend(config.endpoint, litellm_module=fake),
        attempt_id=name,
    )


async def _call(fake, messages=None, *, positional=False, **extra):
    messages = SOLVER_MESSAGES if messages is None else messages
    if positional:
        return await fake.acompletion("openai/solver-model", messages, **extra)
    return await fake.acompletion(model="openai/solver-model", messages=messages, **extra)


def _install(fake):
    assert litellm_hook.install(fake) is True


def test_installation_is_idempotent_and_preserves_the_original():
    fake = FakeLiteLLM()
    original = fake.acompletion

    assert litellm_hook.install(fake) is True
    wrapped = fake.acompletion
    assert litellm_hook.install(fake) is False

    assert fake.acompletion is wrapped
    assert getattr(fake, litellm_hook.MARKER) == original


def test_uninstall_restores_the_original():
    fake = FakeLiteLLM()
    original = fake.acompletion
    litellm_hook.install(fake)

    assert litellm_hook.uninstall(fake) is True
    assert fake.acompletion == original
    assert not hasattr(fake, litellm_hook.MARKER)
    assert litellm_hook.uninstall(fake) is False


def test_installation_is_skipped_without_acompletion():
    class NoCompletion:
        pass

    assert litellm_hook.install(NoCompletion()) is False


async def test_calls_outside_an_advised_block_pass_straight_through():
    draft = finish_response()
    fake = FakeLiteLLM([draft])
    _install(fake)

    assert await _call(fake) is draft
    assert len(fake.calls) == 1
    assert litellm_hook.active_advisor() is None


@pytest.mark.parametrize("dict_shape", [False, True])
@pytest.mark.parametrize("positional", [False, True])
async def test_a_finish_inside_a_block_is_reviewed_and_revised(
    advisor_config, task, tmp_path, dict_shape, positional
):
    draft = finish_response(dict_shape=dict_shape)
    revised = bash_response("pytest -q tests/", dict_shape=dict_shape)
    fake = FakeLiteLLM([draft, decision_response(), revised])
    _install(fake)
    original = copy.deepcopy(SOLVER_MESSAGES)
    advisor = _advisor(advisor_config, task, tmp_path, fake)

    with litellm_hook.advised(advisor):
        response = await _call(fake, positional=positional)

    assert response is revised
    assert len(fake.calls) == 3
    assert (advisor.finish_reviews, advisor.interventions) == (1, 1)
    assert original == SOLVER_MESSAGES
    revision_messages = fake.call_messages[2]
    assert revision_messages[:-1] == SOLVER_MESSAGES
    assert SELF_CHECK_PREAMBLE in revision_messages[-1]["content"]


async def test_the_advisor_call_targets_the_advisor_endpoint(advisor_config, task, tmp_path):
    fake = FakeLiteLLM([finish_response(), decision_response(), bash_response()])
    _install(fake)

    with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake)):
        await _call(fake)

    _, kwargs = fake.calls[1]
    assert kwargs["model"] == "openai/advisor-model"
    assert kwargs["api_base"] == "https://advisor.example/v1"
    assert kwargs["api_key"] == "test-advisor-key"
    assert kwargs["timeout"] == 300.0


async def test_revision_keeps_sampling_arguments_and_drops_only_the_finish_tool(
    advisor_config, task, tmp_path
):
    fake = FakeLiteLLM([finish_response(), decision_response(), bash_response()])
    _install(fake)

    with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake)):
        await _call(fake, tools=TOOLS, temperature=0.7)

    _, kwargs = fake.calls[2]
    assert kwargs["tools"] == [TOOLS[0]]
    assert kwargs["temperature"] == 0.7
    assert kwargs["model"] == "openai/solver-model"


async def test_confirm_and_submit_keeps_the_finish_tool(advisor_config, task, tmp_path):
    config = advisor_config.model_copy(update={"periodic_review_interval": 1})
    fake = FakeLiteLLM(
        [
            bash_response(),
            decision_response(action="CONFIRM_AND_SUBMIT", reason_code="COMPLETE"),
            finish_response(),
        ]
    )
    _install(fake)

    with litellm_hook.advised(_advisor(config, task, tmp_path, fake)):
        await _call(fake, tools=TOOLS)

    assert fake.calls[2][1]["tools"] == TOOLS


async def test_prefill_merges_into_the_callers_extra_body(advisor_config, task, tmp_path):
    config = advisor_config.model_copy(update={"cue": ReasoningPrefillCue.k2_horizon_sglang()})
    fake = FakeLiteLLM([finish_response(), decision_response(), bash_response()])
    _install(fake)

    with litellm_hook.advised(_advisor(config, task, tmp_path, fake)):
        await _call(fake, extra_body={"chat_template_kwargs": {"reasoning_effort": "high"}})

    assert fake.calls[2][1]["extra_body"] == {
        "chat_template_kwargs": {"reasoning_effort": "high"},
        "continue_final_message": True,
    }


async def test_self_check_path_adds_no_extra_body(advisor_config, task, tmp_path):
    fake = FakeLiteLLM([finish_response(), decision_response(), bash_response()])
    _install(fake)

    with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake)):
        await _call(fake)

    assert "extra_body" not in fake.calls[2][1]


async def test_the_advisors_own_calls_are_never_reviewed(advisor_config, task, tmp_path):
    config = advisor_config.model_copy(update={"max_revision_attempts": 1})
    fake = FakeLiteLLM([finish_response(), decision_response(), finish_response()])
    _install(fake)
    advisor = _advisor(config, task, tmp_path, fake)

    with litellm_hook.advised(advisor):
        await _call(fake)

    assert len(fake.calls) == 3
    assert (advisor.finish_reviews, advisor.solver_calls) == (1, 1)
    trace = (tmp_path / "attempt-1" / "advisor" / "private_trace.jsonl").read_text()
    assert json.loads(trace.splitlines()[0])["revision_attempted_finish"] is True


async def test_a_call_without_messages_is_not_reviewed(advisor_config, task, tmp_path):
    draft = finish_response()
    fake = FakeLiteLLM([draft])
    _install(fake)
    advisor = _advisor(advisor_config, task, tmp_path, fake)

    with litellm_hook.advised(advisor):
        response = await fake.acompletion(model="openai/solver-model")

    assert response is draft
    assert advisor.finish_reviews == 0


async def test_leaving_the_block_writes_the_final_summary(advisor_config, task, tmp_path):
    fake = FakeLiteLLM([bash_response(), bash_response()])
    _install(fake)

    with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake)):
        await _call(fake)
        await _call(fake)

    summary = json.loads((tmp_path / "attempt-1" / "advisor" / "summary.json").read_text())
    assert summary["solver_calls"] == 2


async def test_concurrent_attempts_keep_independent_state(advisor_config, task, tmp_path):
    async def route(*_args, **kwargs):
        # Content-addressed, so the outcome never depends on how the tasks interleave.
        await asyncio.sleep(0)
        if kwargs.get("api_base") == "https://advisor.example/v1":
            return decision_response()
        last = kwargs["messages"][-1]["content"]
        return bash_response() if SELF_CHECK_PREAMBLE in last else finish_response()

    fake = FakeLiteLLM([route] * 6)
    _install(fake)
    advisors = {}

    async def attempt(name):
        advisor = _advisor(advisor_config, task, tmp_path, fake, name=name)
        advisors[name] = advisor
        with litellm_hook.advised(advisor):
            await _call(fake)

    await asyncio.gather(attempt("attempt-a"), attempt("attempt-b"))

    for name, advisor in advisors.items():
        assert (advisor.solver_calls, advisor.finish_reviews, advisor.interventions) == (1, 1, 1)
        lines = (tmp_path / name / "advisor" / "private_trace.jsonl").read_text().splitlines()
        assert [json.loads(line)["attempt_id"] for line in lines] == [name]


NO_CALLS_SEEN = "no litellm.acompletion calls"


async def test_leaving_a_block_without_any_call_warns(advisor_config, task, tmp_path, caplog):
    fake = FakeLiteLLM()
    _install(fake)
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake)):
        await asyncio.sleep(0)

    warnings = [record for record in caplog.records if NO_CALLS_SEEN in record.getMessage()]
    assert len(warnings) == 1
    assert "inside its container" in warnings[0].getMessage()


async def test_leaving_a_block_with_calls_does_not_warn(advisor_config, task, tmp_path, caplog):
    fake = FakeLiteLLM([bash_response()])
    _install(fake)
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake)):
        await _call(fake)

    assert not [record for record in caplog.records if NO_CALLS_SEEN in record.getMessage()]


async def test_a_call_made_from_inside_the_draft_call_is_not_reviewed(
    advisor_config, task, tmp_path
):
    """LiteLLM's fallback path can call the hooked acompletion again from inside the first call."""

    async def fallback(*args, **kwargs):
        return await fake.acompletion(*args, **kwargs)

    revised = bash_response()
    fake = FakeLiteLLM([fallback, finish_response(), decision_response(), revised])
    _install(fake)
    advisor = _advisor(advisor_config, task, tmp_path, fake)

    with litellm_hook.advised(advisor):
        response = await _call(fake)

    assert advisor.solver_calls == 1
    assert advisor.finish_reviews + advisor.periodic_reviews == 1
    assert response is revised
    assert len(fake.calls) == 4
    assert SELF_CHECK_PREAMBLE in fake.call_messages[3][-1]["content"]


UNREVIEWED_CALLS = "were seen inside the advised block"


async def test_calls_without_messages_are_seen_but_not_reviewed(
    advisor_config, task, tmp_path, caplog
):
    fake = FakeLiteLLM([bash_response()])
    _install(fake)
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake)):
        await fake.acompletion(model="openai/solver-model")

    messages = [record.getMessage() for record in caplog.records]
    assert [message for message in messages if UNREVIEWED_CALLS in message]
    assert not [message for message in messages if NO_CALLS_SEEN in message]


async def test_a_failing_draft_call_is_seen_but_not_reviewed(
    advisor_config, task, tmp_path, caplog
):
    fake = FakeLiteLLM([RuntimeError("solver down")])
    _install(fake)
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    advisor = _advisor(advisor_config, task, tmp_path, fake)
    with litellm_hook.advised(advisor), pytest.raises(RuntimeError):
        await _call(fake)

    warnings = [r.getMessage() for r in caplog.records if UNREVIEWED_CALLS in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].startswith("1 litellm.acompletion call(s)")


async def test_concurrent_blocks_report_only_their_own_calls(
    advisor_config, task, tmp_path, caplog
):
    fake = FakeLiteLLM([bash_response()])
    _install(fake)
    caplog.set_level(logging.WARNING, logger="ifm_advisor")

    async def attempt(name, calls):
        with litellm_hook.advised(_advisor(advisor_config, task, tmp_path, fake, name=name)):
            for _ in range(calls):
                await _call(fake)

    await asyncio.gather(attempt("busy", 1), attempt("idle", 0))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert [message for message in warnings if NO_CALLS_SEEN in message and "idle" in message]
    assert not [message for message in warnings if "busy" in message]
