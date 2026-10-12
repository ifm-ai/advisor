"""Read the parts of a chat-completion response the advisor needs.

LiteLLM returns pydantic objects, while custom agent loops and test doubles often pass plain
dictionaries. Every accessor here accepts both shapes.
"""

from __future__ import annotations

from typing import Any

_MISSING = object()


def field(source: Any, name: str, default: Any = None) -> Any:
    """Read *name* from a mapping-like or attribute-like object."""

    if isinstance(source, dict):
        return source.get(name, default)
    value = getattr(source, name, _MISSING)
    return default if value is _MISSING else value


def _sequence(value: Any) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes, dict)):
        return ()
    try:
        return tuple(value)
    except TypeError:
        return ()


def response_message(response: Any) -> Any:
    """Return the first choice's assistant message, or ``None``."""

    choices = _sequence(field(response, "choices"))
    if not choices:
        return None
    return field(choices[0], "message")


def response_tool_call_names(response: Any) -> tuple[str, ...]:
    """Return the ordered tool names requested by a completion response."""

    message = response_message(response)
    if message is None:
        return ()
    names: list[str] = []
    for tool_call in _sequence(field(message, "tool_calls")):
        function = field(tool_call, "function")
        name = field(function, "name") if function is not None else None
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def response_attempts_finish(response: Any, finish_tool_name: str) -> bool:
    """Return whether the response invokes the configured finish tool.

    Detection is structural: only a real tool call counts.  Prose that merely
    mentions the tool name never triggers a review.
    """

    if not finish_tool_name:
        return False
    return finish_tool_name in response_tool_call_names(response)


def response_text(response: Any) -> str:
    """Return the assistant message content, or an empty string."""

    message = response_message(response)
    if message is None:
        return ""
    content = field(message, "content")
    return content if isinstance(content, str) else ""


def response_reasoning_tokens(response: Any) -> int | None:
    """Return ``usage.completion_tokens_details.reasoning_tokens``, if reported.

    This is the measurable signal for whether a spliced hint opened the solver's
    reasoning or closed it: a continuation of only a handful of tokens means the
    prefill ended on something the model had nothing to add to.
    """

    usage = field(response, "usage")
    if usage is None:
        return None
    details = field(usage, "completion_tokens_details")
    if details is None:
        return None
    tokens = field(details, "reasoning_tokens")
    return tokens if isinstance(tokens, int) else None


def prepend_reasoning_content(response: Any, prefix: str) -> bool:
    """Splice *prefix* onto the front of the response's ``reasoning_content``.

    ``continue_final_message`` makes the endpoint return only the tokens
    generated *after* the prefill, so without this the advisor's hint never
    reaches the harness and the recorded trajectory shows a reasoning block that
    begins mid-thought. Restoring it is what makes an advised turn coherent to
    read and usable as training data.

    Returns whether the response was modified.
    """

    if not prefix:
        return False
    message = response_message(response)
    if message is None:
        return False
    existing = field(message, "reasoning_content")
    merged = prefix + (existing if isinstance(existing, str) else "")
    if isinstance(message, dict):
        message["reasoning_content"] = merged
        return True
    try:
        message.reasoning_content = merged
    except (AttributeError, ValueError):
        # A frozen or slotted message object: the trace still records the hint,
        # so this degrades to the old behavior rather than failing the turn.
        return False
    return True


def proposed_submission_summary(response: Any, finish_tool_name: str) -> str:
    """Render the solver's proposed finish action for the advisor prompt."""

    names = response_tool_call_names(response)
    lines = [
        f"tool_calls: {', '.join(names) if names else '(none)'}",
        f"attempts_finish: {response_attempts_finish(response, finish_tool_name)}",
        "assistant_message:",
        response_text(response) or "(empty)",
    ]
    return "\n".join(lines)
