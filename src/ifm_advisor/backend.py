"""Call the advisor model.

A backend is any `async (messages) -> str` that returns the advisor model's raw answer. The default
sends the request through LiteLLM to the OpenAI-compatible endpoint in `EndpointConfig`.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .config import EndpointConfig
from .errors import AdvisorRequestError
from .responses import field

Complete = Callable[[list[dict[str, str]]], Awaitable[str]]

# Set while the advisor makes its own model calls and the revision call, so the LiteLLM hook
# never reviews them. A ContextVar is copied per asyncio task, so one attempt's bypass never
# leaks into another attempt running concurrently in the same process.
_BYPASS: ContextVar[bool] = ContextVar("ifm_advisor_bypass", default=False)


def advisor_bypassed() -> bool:
    """Whether the current call is one of the advisor's own."""

    return _BYPASS.get()


@contextmanager
def advisor_bypass() -> Iterator[None]:
    """Mark completion calls inside the block as the advisor's own."""

    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def resolve_api_key(endpoint: EndpointConfig, environ: Mapping[str, str] | None = None) -> str:
    """The endpoint's API key from the environment. Raises when the variable is not exported."""

    source = os.environ if environ is None else environ
    api_key = source.get(endpoint.api_key_env, "")
    if not api_key:
        raise AdvisorRequestError(
            f"advisor API key environment variable {endpoint.api_key_env!r} is not exported"
        )
    return api_key


def request_kwargs(
    endpoint: EndpointConfig,
    messages: list[dict[str, str]],
    *,
    api_key: str,
) -> dict[str, Any]:
    """LiteLLM keyword arguments for one call to *endpoint*."""

    kwargs: dict[str, Any] = {
        "model": endpoint.litellm_model,
        "messages": messages,
        "api_base": endpoint.api_base,
        "api_key": api_key,
        "timeout": float(endpoint.timeout_sec),
    }
    if endpoint.extra_body:
        kwargs["extra_body"] = dict(endpoint.extra_body)
    if endpoint.extra_headers:
        kwargs["extra_headers"] = dict(endpoint.extra_headers)
    return kwargs


def response_content(response: Any) -> str:
    """The first choice's text. Raises AdvisorRequestError when there is none."""

    choices = field(response, "choices")
    if not choices:
        raise AdvisorRequestError("advisor response contained no choices")
    message = field(choices[0], "message")
    if message is None:
        raise AdvisorRequestError("advisor response contained no message")
    content = field(message, "content")
    if not isinstance(content, str):
        raise AdvisorRequestError("advisor response content was not text")
    return content


def litellm_backend(
    endpoint: EndpointConfig,
    *,
    litellm_module: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> Complete:
    """A backend that calls *endpoint* through LiteLLM, reading the API key at call time."""

    async def complete(messages: list[dict[str, str]]) -> str:
        module = (
            litellm_module if litellm_module is not None else importlib.import_module("litellm")
        )
        api_key = resolve_api_key(endpoint, environ)
        response = await module.acompletion(**request_kwargs(endpoint, messages, api_key=api_key))
        return response_content(response)

    return complete
