"""Test doubles and response builders. Nothing here needs a real endpoint or the real litellm."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

FINISH_TOOL = "complete"


def object_response(
    *,
    content: str = "",
    tool_calls: tuple[tuple[str, str], ...] = (),
) -> SimpleNamespace:
    """An attribute-style chat completion, shaped like LiteLLM's ModelResponse."""

    calls = [
        SimpleNamespace(
            id=f"call_{index}",
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for index, (name, arguments) in enumerate(tool_calls)
    ]
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls" if calls else "stop",
                message=SimpleNamespace(
                    role="assistant", content=content, tool_calls=calls or None
                ),
            )
        ]
    )


def dict_response(
    *,
    content: str = "",
    tool_calls: tuple[tuple[str, str], ...] = (),
    reasoning_content: str | None = None,
    reasoning_tokens: int | None = None,
) -> dict[str, Any]:
    """A dictionary-style chat completion."""

    calls = [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        for index, (name, arguments) in enumerate(tool_calls)
    ]
    message: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": calls or None}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    response: dict[str, Any] = {
        "choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": message}]
    }
    if reasoning_tokens is not None:
        response["usage"] = {"completion_tokens_details": {"reasoning_tokens": reasoning_tokens}}
    return response


def finish_response(*, dict_shape: bool = False) -> Any:
    builder = dict_response if dict_shape else object_response
    return builder(content="Done.", tool_calls=((FINISH_TOOL, "{}"),))


def bash_response(command: str = "pytest -q", *, dict_shape: bool = False) -> Any:
    builder = dict_response if dict_shape else object_response
    return builder(
        content="Running the tests.", tool_calls=(("bash", json.dumps({"command": command})),)
    )


def decision_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "intervene": True,
        "confidence": 0.9,
        "reason_code": "INSUFFICIENT_END_TO_END_VALIDATION",
        "action": "RUN_BROADER_VALIDATION",
        "plan": "1. fix the gc path. 2. add regression coverage.",
        "hint": "Hmm. Only the single-session path has run so far.",
        "private_diagnosis": "PRIVILEGED-DIAGNOSIS-MARKER",
    }
    payload.update(overrides)
    return payload


def decision_text(**overrides: Any) -> str:
    """The raw text an advisor model returns for a typed decision."""

    return json.dumps(decision_payload(**overrides))


def continue_text(**overrides: Any) -> str:
    return decision_text(
        **{
            "intervene": False,
            "action": "CONTINUE",
            "reason_code": "COMPLETE",
            "hint": "",
            **overrides,
        }
    )


def decision_response(**overrides: Any) -> dict[str, Any]:
    """An advisor endpoint response carrying a typed decision."""

    return dict_response(content=decision_text(**overrides))


class FakeLiteLLM:
    """A stand-in ``litellm`` module recording every completion call."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._responses = list(responses or [])

    def queue(self, *responses: Any) -> None:
        self._responses.extend(responses)

    async def acompletion(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if not self._responses:
            raise AssertionError("FakeLiteLLM ran out of queued responses")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return await response(*args, **kwargs)
        return response

    @property
    def call_messages(self) -> list[list[Any]]:
        return [
            kwargs.get("messages", args[1] if len(args) > 1 else []) for args, kwargs in self.calls
        ]


class ScriptedAdvisor:
    """A ``complete`` backend: returns queued answers and records each advisor request."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.requests.append(messages)
        if not self.answers:
            raise AssertionError("ScriptedAdvisor ran out of answers")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return await answer(messages)
        return answer

    @property
    def user_contents(self) -> list[str]:
        return [request[1]["content"] for request in self.requests]


class ScriptedSolver:
    """A ``regenerate`` callback: returns queued revisions and records each Revision."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.revisions: list[Any] = []

    async def __call__(self, revision: Any) -> Any:
        self.revisions.append(revision)
        if not self.responses:
            raise AssertionError("ScriptedSolver ran out of revisions")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def finishes[T](function: Callable[[], T], timeout: float = 10.0) -> T:
    """Run *function* in a daemon thread and return its result; fail if it blocks."""

    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = function()
        except BaseException as error:  # re-raised below, in the test thread
            result["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "the call blocked"
    if "error" in result:
        raise result["error"]
    return result["value"]
