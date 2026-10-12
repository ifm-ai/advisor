"""Assemble the private advisor request.

The request has two messages: the system prompt (a versioned core prompt with a solver profile
inserted) and one user message made of clearly separated sections. Only the advisor sees it.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from .decision import RESPONSE_SCHEMA_TEXT
from .errors import AdvisorRequestError
from .material import TaskMaterial
from .responses import proposed_submission_summary

PROMPTS_PACKAGE = "ifm_advisor.prompts"
PROFILE_MARKER = "{{SOLVER_PROFILE}}\n"
_TOOL_ARGUMENT_CHARS = 2_000
_CUE_CHARS = 2_000


@lru_cache(maxsize=16)
def _packaged_text(relative: str) -> str:
    try:
        return resources.files(PROMPTS_PACKAGE).joinpath(relative).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as error:
        raise AdvisorRequestError(f"packaged prompt file {relative!r} is unavailable") from error


def load_solver_profile(profile: str | Path) -> str:
    """A packaged profile by name, or a profile text file by Path."""

    if isinstance(profile, Path):
        try:
            return profile.read_text(encoding="utf-8")
        except OSError as error:
            raise AdvisorRequestError(f"solver profile {str(profile)!r} is unreadable") from error
    return _packaged_text(f"solver_profiles/{profile}.txt")


def load_system_prompt(version: str = "advisor_v1", solver_profile: str | Path = "generic") -> str:
    """The advisor's system prompt: the core prompt with the solver profile inserted."""

    core = _packaged_text(f"{version}.txt")
    if PROFILE_MARKER not in core:
        raise AdvisorRequestError(f"prompt {version!r} has no solver-profile marker")
    profile = load_solver_profile(solver_profile)
    if not profile.endswith("\n"):
        profile += "\n"
    return core.replace(PROFILE_MARKER, profile, 1)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[{len(text) - limit} characters omitted]"


def _message_text(message: Any, max_chars: int) -> str:
    if not isinstance(message, dict):
        return _clip(str(message), max_chars)
    parts = [f"[{message.get('role', 'unknown')}]"]
    content = message.get("content")
    if content:
        parts.append(_clip(content if isinstance(content, str) else str(content), max_chars))
    names = []
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if isinstance(function, dict):
            names.append(str(function.get("name", "?")))
            arguments = _clip(str(function.get("arguments", "")), _TOOL_ARGUMENT_CHARS)
            if arguments:
                parts.append(f"tool arguments: {arguments}")
    if names:
        parts.append(f"tool calls: {', '.join(names)}")
    return "\n".join(parts)


def render_public_conversation(
    messages: Sequence[Any],
    *,
    max_messages: int = 50,
    max_chars: int = 8_000,
) -> str:
    """Render the solver's conversation, keeping the opening and the recent tail."""

    if not messages:
        return "(no conversation recorded)"
    if len(messages) <= max_messages:
        selected = list(messages)
        elided = 0
    else:
        # The opening holds the task; the tail holds the latest reasoning, tool calls and output.
        # The middle is the least decision-relevant part.
        selected = [*messages[:2], *messages[-(max_messages - 2) :]]
        elided = len(messages) - len(selected)
    blocks = [_message_text(message, max_chars) for message in selected]
    if elided:
        blocks.insert(2, f"...[{elided} earlier messages omitted]")
    return "\n\n".join(blocks)


def _previous_cues_section(cues: Sequence[str], *, max_cues: int) -> str:
    """The cues already delivered this attempt, newest last.

    The solver has seen each of these, so an argument repeated here is one it has already worked
    through. The tail is the informative end: it is what the solver most recently rejected.
    """

    if not cues:
        return ""
    shown = list(cues[-max_cues:])
    elided = len(cues) - len(shown)
    lines = [
        "=== HINTS YOU HAVE ALREADY DELIVERED THIS ATTEMPT ===",
        f"You have intervened {len(cues)} time(s) so far.",
    ]
    if elided:
        lines.append(f"...[{elided} earlier hint(s) omitted]")
    lines.extend(
        f"--- hint {elided + offset + 1} ---\n{_clip(cue, _CUE_CHARS)}"
        for offset, cue in enumerate(shown)
    )
    return "\n".join(lines) + "\n\n"


def build_advisor_messages(
    *,
    system_prompt: str,
    task: TaskMaterial,
    solver_messages: Sequence[Any],
    draft_response: Any,
    finish_tool_name: str,
    trigger: str = "pre_submit",
    previous_plan: str = "",
    previous_cues: Sequence[str] = (),
    repeat_reason_code: str = "",
    repeat_streak: int = 0,
    max_conversation_messages: int = 50,
    max_message_chars: int = 8_000,
    max_previous_cues: int = 8,
) -> list[dict[str, str]]:
    """Compose the private advisor request in clearly separated sections."""

    truncation_note = ""
    if task.truncated_paths or task.omitted_paths:
        truncation_note = (
            "\nNOTE: privileged material was size-limited. Truncated: "
            f"{list(task.truncated_paths)}. Omitted: {list(task.omitted_paths)}."
        )
    plan_section = f"=== YOUR PREVIOUS PLAN ===\n{previous_plan}\n\n" if previous_plan else ""
    repeat_section = (
        "=== REPEAT SIGNAL ===\n"
        f"reason_code {repeat_reason_code!r} has now recurred across "
        f"{repeat_streak} consecutive review(s) without resolution.\n\n"
        if repeat_streak > 0
        else ""
    )
    conversation = render_public_conversation(
        solver_messages, max_messages=max_conversation_messages, max_chars=max_message_chars
    )
    user_content = (
        f"=== REVIEW TRIGGER ===\n{trigger}\n\n"
        f"=== PUBLIC TASK ===\n{task.instruction.strip()}\n\n"
        f"=== PUBLIC CONVERSATION ===\n{conversation}\n\n"
        "=== SOLVER'S LATEST TURN ===\n"
        f"{proposed_submission_summary(draft_response, finish_tool_name)}\n\n"
        f"{plan_section}"
        f"{_previous_cues_section(previous_cues, max_cues=max_previous_cues)}"
        f"{repeat_section}"
        f"=== PRIVILEGED REFERENCE MATERIAL ===\n{task.render()}{truncation_note}\n\n"
        f"=== REQUIRED JSON RESPONSE SCHEMA ===\n{RESPONSE_SCHEMA_TEXT}\n"
        "Return only this JSON object."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
