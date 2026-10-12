import asyncio

import pytest
from helpers import FakeLiteLLM, decision_response, decision_text

from ifm_advisor.backend import (
    advisor_bypass,
    advisor_bypassed,
    litellm_backend,
    request_kwargs,
    resolve_api_key,
    response_content,
)
from ifm_advisor.config import EndpointConfig
from ifm_advisor.errors import AdvisorRequestError

ENDPOINT = EndpointConfig(
    model="IFM/K2-Horizon-375B-A23B",
    api_base="https://advisor.example/v1",
    api_key_env="TEST_ADVISOR_API_KEY",
)
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_request_kwargs_target_the_advisor_endpoint():
    kwargs = request_kwargs(ENDPOINT, MESSAGES, api_key="k")

    assert kwargs == {
        "model": "openai/IFM/K2-Horizon-375B-A23B",
        "messages": MESSAGES,
        "api_base": "https://advisor.example/v1",
        "api_key": "k",
        "timeout": 300.0,
    }


def test_extra_body_and_headers_are_forwarded_only_when_set():
    endpoint = ENDPOINT.model_copy(
        update={"extra_body": {"reasoning_effort": "high"}, "extra_headers": {"X-Example": "1"}}
    )

    kwargs = request_kwargs(endpoint, MESSAGES, api_key="k")

    assert kwargs["extra_body"] == {"reasoning_effort": "high"}
    assert kwargs["extra_headers"] == {"X-Example": "1"}


def test_missing_api_key_names_the_variable():
    with pytest.raises(AdvisorRequestError, match="TEST_ADVISOR_API_KEY"):
        resolve_api_key(ENDPOINT, environ={})


def test_api_key_is_read_from_the_environment():
    assert resolve_api_key(ENDPOINT) == "test-advisor-key"


async def test_litellm_backend_returns_the_answer_text():
    fake = FakeLiteLLM([decision_response()])
    complete = litellm_backend(ENDPOINT, litellm_module=fake)

    assert await complete(MESSAGES) == decision_text()
    _, kwargs = fake.calls[0]
    assert kwargs["model"] == "openai/IFM/K2-Horizon-375B-A23B"
    assert kwargs["api_key"] == "test-advisor-key"


async def test_litellm_backend_reads_the_key_at_call_time(monkeypatch):
    fake = FakeLiteLLM([decision_response()])
    complete = litellm_backend(ENDPOINT, litellm_module=fake)
    monkeypatch.setenv("TEST_ADVISOR_API_KEY", "rotated-key")

    await complete(MESSAGES)

    assert fake.calls[0][1]["api_key"] == "rotated-key"


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": ["not", "text"]}}]},
    ],
)
def test_unusable_responses_raise(response):
    with pytest.raises(AdvisorRequestError):
        response_content(response)


def test_bypass_is_scoped_and_reset_after_an_exception():
    assert advisor_bypassed() is False
    with advisor_bypass():
        assert advisor_bypassed() is True
    assert advisor_bypassed() is False

    with pytest.raises(RuntimeError), advisor_bypass():
        raise RuntimeError("boom")
    assert advisor_bypassed() is False


async def test_bypass_in_one_task_does_not_leak_into_another():
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: list[bool] = []

    async def bypassing() -> None:
        with advisor_bypass():
            entered.set()
            await release.wait()

    async def observing() -> None:
        await entered.wait()
        observed.append(advisor_bypassed())
        release.set()

    await asyncio.gather(bypassing(), observing())

    assert observed == [False]
