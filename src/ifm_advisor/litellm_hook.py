"""Run an agent's LiteLLM completions through an Advisor without changing the agent.

    from ifm_advisor import litellm_hook
    litellm_hook.install()                  # once per process
    with litellm_hook.advised(advisor):     # once per task attempt
        await run_agent()

Inside a block, every `litellm.acompletion` call the agent makes is one solver turn: the hook gets
the draft with the original function and hands it to `advisor.review`, which may query the solver
again through the original function. Calls outside a block, the advisor's own calls, and calls
made from inside a draft call (LiteLLM's fallbacks) pass straight through.

Only `litellm.acompletion` looked up on the module, in this process, is wrapped. The agent must
call it on the host and expose a finish tool. In Harbor that is the `mini-swe-agent-external-finish`
agent, which runs LiteLLM in the host process. Harbor's stock installed `mini-swe-agent` runs inside
the task container, where this hook cannot see its calls, and Terminus-2 has no finish tool, so
only periodic reviews could apply to it; neither is a supported target. Leaving a block logs a
warning when no call was seen, or when calls were seen but none reached the advisor.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from .advisor import Advisor, Revision
from .backend import advisor_bypass, advisor_bypassed

logger = logging.getLogger(__name__)

# Set on the litellm module to hold the original acompletion; its presence means "installed".
MARKER = "_ifm_advisor_original_acompletion"


@dataclass(slots=True)
class _Binding:
    """One advised block: its advisor and how many calls the hook saw inside it."""

    advisor: Advisor
    calls_seen: int = 0


# A ContextVar is copied per asyncio task, so concurrent attempts in one process each see their
# own binding; the binding object itself is shared, so calls in child tasks are counted.
_ACTIVE: ContextVar[_Binding | None] = ContextVar("ifm_advisor_active", default=None)


def active_advisor() -> Advisor | None:
    """The advisor bound to the current attempt, if any."""

    binding = _ACTIVE.get()
    return binding.advisor if binding is not None else None


@contextmanager
def advised(advisor: Advisor) -> Iterator[Advisor]:
    """Route the agent's completions inside the block through *advisor*.

    Leaving the block writes the summary, and logs a warning when no call was seen, or when calls
    were seen but none reached the advisor.
    """

    binding = _Binding(advisor)
    reviews_before = advisor.solver_calls
    token = _ACTIVE.set(binding)
    try:
        yield advisor
    finally:
        _ACTIVE.reset(token)
        advisor.write_summary()
        attempt = advisor.attempt_id or advisor.task.name
        if binding.calls_seen == 0:
            logger.warning(
                "no litellm.acompletion calls were seen in this process inside the advised block "
                "(%s); nothing was reviewed. The agent may run inside its container, import "
                "acompletion directly, or use the synchronous API",
                attempt,
            )
        elif advisor.solver_calls == reviews_before:
            logger.warning(
                "%d litellm.acompletion call(s) were seen inside the advised block (%s) but none "
                "reached the advisor: each had no messages argument or its draft call failed",
                binding.calls_seen,
                attempt,
            )


def _extract_messages(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[Any] | None:
    """The `messages` argument, whether it arrived by keyword or positionally."""

    if "messages" in kwargs:
        candidate = kwargs["messages"]
    elif len(args) >= 2:
        candidate = args[1]
    else:
        return None
    return list(candidate) if isinstance(candidate, (list, tuple)) else None


def _with_revision(
    args: tuple[Any, ...], kwargs: dict[str, Any], revision: Revision
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """The original call's arguments with the revision substituted; inputs are never mutated."""

    if "messages" in kwargs:
        kwargs = {**kwargs, "messages": revision.messages}
    else:
        args = (args[0], revision.messages, *args[2:])
        kwargs = dict(kwargs)
    if revision.tools is not None:
        kwargs["tools"] = revision.tools
    if revision.extra_body:
        kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), **revision.extra_body}
    return args, kwargs


def install(litellm_module: Any | None = None) -> bool:
    """Wrap `litellm.acompletion` once. Returns whether this call installed the wrapper."""

    module: Any = (
        litellm_module if litellm_module is not None else importlib.import_module("litellm")
    )
    original = getattr(module, "acompletion", None)
    if not callable(original) or getattr(module, MARKER, None) is not None:
        return False

    @wraps(original)
    async def advised_completion(*args: Any, **kwargs: Any) -> Any:
        binding = _ACTIVE.get()
        if binding is None or advisor_bypassed():
            return await original(*args, **kwargs)
        binding.calls_seen += 1
        advisor = binding.advisor
        # A call LiteLLM makes from inside this one (a fallback) belongs to the same turn.
        with advisor_bypass():
            draft = await original(*args, **kwargs)
        messages = _extract_messages(args, kwargs)
        if messages is None:
            logger.warning("advisor review skipped: the call has no messages argument")
            return draft

        async def regenerate(revision: Revision) -> Any:
            revised_args, revised_kwargs = _with_revision(args, kwargs, revision)
            return await original(*revised_args, **revised_kwargs)

        return await advisor.review(
            messages, draft, tools=kwargs.get("tools"), regenerate=regenerate
        )

    setattr(module, MARKER, original)
    module.acompletion = advised_completion
    return True


def uninstall(litellm_module: Any | None = None) -> bool:
    """Restore the original `acompletion`. Returns whether a wrapper was removed."""

    module: Any = (
        litellm_module if litellm_module is not None else importlib.import_module("litellm")
    )
    original = getattr(module, MARKER, None)
    if original is None:
        return False
    module.acompletion = original
    delattr(module, MARKER)
    return True
