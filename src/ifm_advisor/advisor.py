"""The Advisor: one per task attempt.

    advisor = Advisor(config, task, finish_tool="complete", trace_dir="out/attempt-1/advisor")
    response = await advisor.review(messages, draft, tools=tools, regenerate=regenerate)

Each `review` call is one solver turn t. The draft is reviewed when it calls the finish tool, or
every `periodic_review_interval` turns, while interventions remain. A review asks the advisor model
for a typed decision; unless that decision is CONTINUE, the solver regenerates its turn after a
solver-safe cue, and the regenerated response is returned in place of the draft.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, time
from typing import Any

from .artifacts import ArtifactWriter
from .audit.checks import screen_text
from .backend import Complete, advisor_bypass, litellm_backend, resolve_api_key
from .config import AdvisorConfig, ReasoningPrefillCue, SelfCheckCue
from .cues import CompiledCue, compile_cue
from .decision import AdvisorDecision, parse_advisor_decision, should_intervene
from .errors import AdvisorRequestError
from .material import TaskMaterial
from .prompting import build_advisor_messages, load_system_prompt
from .responses import field as read_field
from .responses import (
    prepend_reasoning_content,
    response_attempts_finish,
    response_reasoning_tokens,
)

logger = logging.getLogger(__name__)

CONFIRM_AND_SUBMIT = "CONFIRM_AND_SUBMIT"


@dataclass(frozen=True, slots=True)
class Revision:
    """The solver request for one revision, ready to send.

    messages: the public history h_t followed by the cue message.
    tools: the tools offered on the draft's call, without the finish tool unless the decision is
        CONFIRM_AND_SUBMIT; None when the draft's call offered none.
    extra_body: only the fields the advisor adds, to merge into the caller's own request body:
        {"continue_final_message": True} on the reasoning-prefill path, otherwise empty.
    """

    messages: list[Any]
    tools: list[Any] | None
    extra_body: dict[str, object] = field(default_factory=dict)


Regenerate = Callable[[Revision], Awaitable[Any]]


def without_tool(tools: Sequence[Any] | None, name: str) -> list[Any] | None:
    """*tools* without the function tool called *name*. None stays None."""

    if tools is None:
        return None
    return [
        tool
        for tool in tools
        if not (
            isinstance(tool, dict)
            and isinstance(tool.get("function"), dict)
            and tool["function"].get("name") == name
        )
    ]


def offers_tool(tools: Sequence[Any], name: str) -> bool:
    """Whether *tools* includes a function tool called *name* (dict or attribute style)."""

    return any(read_field(read_field(tool, "function"), "name") == name for tool in tools)


class Advisor:
    """Reviews one solver attempt with privileged task material.

    Counters (read them; the advisor maintains them): `solver_calls`, `finish_reviews`,
    `periodic_reviews`, `interventions`, `advisor_failures`, `revision_calls`,
    `hint_screen_fallbacks`, `empty_hint_fallbacks`. `plan` and `delivered_cues` carry the
    advisor's private state from one review to the next.
    """

    def __init__(
        self,
        config: AdvisorConfig,
        task: TaskMaterial,
        *,
        finish_tool: str,
        trace_dir: str | Path | None = None,
        complete: Complete | None = None,
        solver_model: str = "",
        attempt_id: str = "",
    ) -> None:
        if not finish_tool:
            raise ValueError("finish_tool must name the solver's finish tool")
        self.config = config
        self.task = task
        self.finish_tool = finish_tool
        self.solver_model = solver_model
        self.attempt_id = attempt_id
        if complete is None:
            # Fail now: an unexported key would otherwise fail open on every review and silently
            # turn an advised run into an unadvised one.
            resolve_api_key(config.endpoint)
            complete = litellm_backend(config.endpoint)
        self._complete = complete
        self._system_prompt = load_system_prompt(config.prompt_version, config.solver_profile)
        self._artifacts = ArtifactWriter(trace_dir) if trace_dir is not None else None

        self.solver_calls = 0
        self.finish_reviews = 0
        self.periodic_reviews = 0
        self.interventions = 0
        self.advisor_failures = 0
        self.revision_calls = 0
        self.hint_screen_fallbacks = 0
        self.empty_hint_fallbacks = 0
        # Private, like private_diagnosis: carried from review to review and written only to the
        # private trace, so the advisor judges drift against its own plan instead of re-deriving.
        self.plan = ""
        # The cue text the solver actually received at each intervention, newest last.
        self.delivered_cues: list[str] = []
        self.last_reason_code = ""
        self.reason_code_streak = 0
        self._warned_missing_finish_tool = False

        if not task.has_privileged_text():
            logger.warning(
                "the advisor has no privileged material to review against (task %r); check the "
                "task directory and its layout",
                task.name,
            )
        if self._artifacts is not None:
            self._artifacts.write_task_material_manifest(task.manifest())
            self.write_summary()

    @property
    def interventions_remaining(self) -> int:
        return max(0, self.config.max_interventions - self.interventions)

    async def review(
        self,
        messages: Sequence[Any],
        draft: Any,
        *,
        tools: Sequence[Any] | None = None,
        regenerate: Regenerate,
    ) -> Any:
        """Review the solver's draft turn; return the draft or the committed revision.

        messages: the public history h_t the draft was generated from (never mutated).
        draft: the solver's chat-completion response for this turn (object or dict).
        tools: the tools offered on that call. If they do not include the finish tool, a warning
            is logged once: proposed finishes can then never be reviewed.
        regenerate: queries the solver again with a Revision and returns its response.
        """

        self.solver_calls += 1
        if (
            tools is not None
            and not self._warned_missing_finish_tool
            and not offers_tool(tools, self.finish_tool)
        ):
            self._warned_missing_finish_tool = True
            logger.warning(
                "finish tool %r is not among the tools offered to the solver; its proposed "
                "finishes cannot be reviewed",
                self.finish_tool,
            )
        finishing = response_attempts_finish(draft, self.finish_tool)
        interval = self.config.periodic_review_interval
        periodic_due = interval is not None and self.solver_calls % interval == 0
        if not (finishing or periodic_due) or self.interventions_remaining <= 0:
            return draft
        trigger = "pre_submit" if finishing else "periodic"
        return await self._review(list(messages), draft, tools, regenerate, trigger)

    def summary(self) -> dict[str, Any]:
        """Counters and settings only; never a plan, a hint or a diagnosis."""

        profile = self.config.solver_profile
        return {
            "enabled": True,
            "attempt_id": self.attempt_id,
            "task": self.task.name,
            "solver_model": self.solver_model,
            "advisor_model": self.config.endpoint.model,
            "prompt_version": self.config.prompt_version,
            "solver_profile": profile if isinstance(profile, str) else Path(profile).name,
            "cue": self.config.cue.kind,
            "solver_calls": self.solver_calls,
            "finish_reviews": self.finish_reviews,
            "periodic_reviews": self.periodic_reviews,
            "interventions": self.interventions,
            "advisor_failures": self.advisor_failures,
            "revision_calls": self.revision_calls,
            "hint_screen_fallbacks": self.hint_screen_fallbacks,
            "empty_hint_fallbacks": self.empty_hint_fallbacks,
        }

    def write_summary(self) -> None:
        """Write summary.json now. Reviews do this themselves; call it when an attempt ends."""

        if self._artifacts is not None:
            self._artifacts.write_summary(self.summary())

    async def _review(
        self,
        messages: list[Any],
        draft: Any,
        tools: Sequence[Any] | None,
        regenerate: Regenerate,
        trigger: str,
    ) -> Any:
        config = self.config
        if trigger == "pre_submit":
            self.finish_reviews += 1
        else:
            self.periodic_reviews += 1
        record = self._new_record(trigger)

        started = monotonic()
        try:
            advisor_messages = build_advisor_messages(
                system_prompt=self._system_prompt,
                task=self.task,
                solver_messages=messages,
                draft_response=draft,
                finish_tool_name=self.finish_tool,
                trigger=trigger,
                previous_plan=self.plan,
                previous_cues=self.delivered_cues,
                repeat_reason_code=self.last_reason_code if config.repeat_streak_escalation else "",
                repeat_streak=self.reason_code_streak if config.repeat_streak_escalation else 0,
                max_conversation_messages=config.max_conversation_messages,
                max_message_chars=config.max_message_chars,
                max_previous_cues=config.max_previous_cues,
            )
            decision, attempts = await self._decide(advisor_messages)
        except Exception as error:
            # Endpoint failures, timeouts and strict-parse rejections. CancelledError is a
            # BaseException and deliberately propagates.
            self.advisor_failures += 1
            record["advisor_latency_sec"] = monotonic() - started
            record["advisor_attempts"] = config.max_advisor_attempts
            record["error"] = f"{type(error).__name__}: {error}"
            self._record(record)
            if not config.fail_open:
                raise
            logger.warning("advisor review failed; keeping the solver draft (%s)", self.attempt_id)
            return draft

        record["advisor_latency_sec"] = monotonic() - started
        record["advisor_attempts"] = attempts
        record["decision"] = decision.model_dump()
        self.plan = decision.plan
        if not should_intervene(decision):
            self.reason_code_streak = 0
            self.last_reason_code = ""
            self._record(record)
            return draft
        if decision.reason_code == self.last_reason_code:
            self.reason_code_streak += 1
        else:
            self.reason_code_streak = 1
            self.last_reason_code = decision.reason_code

        revision_started = monotonic()
        try:
            cue = self._compile_cue(decision, record)
            revised, revision_attempts = await self._revise(
                messages, tools, regenerate, decision, cue
            )
        except Exception as error:
            self.advisor_failures += 1
            record["error"] = f"revision failed: {type(error).__name__}: {error}"
            record["revision_latency_sec"] = monotonic() - revision_started
            self._record(record)
            if not config.fail_open:
                raise
            return draft

        self.interventions += 1
        self.revision_calls += 1
        # Only a delivered cue counts as said: a failed revision keeps the draft, so the solver
        # never saw that cue and saying it later is not repetition.
        self.delivered_cues.append(cue.delivered_text)
        record["intervened"] = True
        record["delivered_cue"] = cue.delivered_text
        record["revision_attempts"] = revision_attempts
        record["revision_latency_sec"] = monotonic() - revision_started
        record["revision_attempted_finish"] = response_attempts_finish(revised, self.finish_tool)
        # Tokens the solver generated after the cue. A handful on the prefill path means the hint
        # closed the reasoning block instead of opening it.
        record["revision_reasoning_tokens"] = response_reasoning_tokens(revised)
        record["hint_continuation_suffix"] = cue.continuation_suffix
        if cue.prefill:
            # continue_final_message returns only the tokens after the prefill; splice the hint
            # back so the committed turn reads as one continuous thought.
            record["hint_persisted_to_trajectory"] = prepend_reasoning_content(revised, cue.prefill)
        self._record(record)
        return revised

    async def _decide(self, advisor_messages: list[dict[str, str]]) -> tuple[AdvisorDecision, int]:
        """Ask the advisor model, retrying a failed or malformed answer. Returns the attempt."""

        last_error: Exception = AdvisorRequestError("no advisor attempt was made")
        for attempt in range(1, self.config.max_advisor_attempts + 1):
            try:
                with advisor_bypass():
                    async with asyncio.timeout(self.config.endpoint.timeout_sec):
                        answer = await self._complete(advisor_messages)
                return parse_advisor_decision(answer), attempt
            except Exception as error:
                last_error = error
        raise last_error

    def _compile_cue(self, decision: AdvisorDecision, record: dict[str, Any]) -> CompiledCue:
        cue: SelfCheckCue | ReasoningPrefillCue = self.config.cue
        if isinstance(cue, ReasoningPrefillCue):
            if not decision.hint.strip():
                self.empty_hint_fallbacks += 1
                record["cue_fallback"] = "empty_hint"
                cue = SelfCheckCue()
            elif self.config.screen_hints:
                hits = screen_text(decision.hint, self.task.privileged_text())
                if hits:
                    self.hint_screen_fallbacks += 1
                    record["cue_fallback"] = "hint_screen"
                    record["hint_screen_hits"] = hits
                    cue = SelfCheckCue()
        return compile_cue(cue, decision, intervention_index=self.interventions)

    async def _revise(
        self,
        messages: list[Any],
        tools: Sequence[Any] | None,
        regenerate: Regenerate,
        decision: AdvisorDecision,
        cue: CompiledCue,
    ) -> tuple[Any, int]:
        confirm = decision.action == CONFIRM_AND_SUBMIT
        # CONFIRM_AND_SUBMIT asks the solver to finish, so it alone keeps the finish tool. Every
        # other action means "more work first": the solver must take one real action before it
        # can try to finish again.
        revision_tools = (
            (list(tools) if tools is not None else None)
            if confirm
            else without_tool(tools, self.finish_tool)
        )
        revision = Revision(
            messages=[*messages, cue.message],
            tools=revision_tools,
            extra_body=dict(cue.extra_body),
        )
        if confirm:
            with advisor_bypass():
                return await regenerate(revision), 1
        revised: Any = None
        attempts = self.config.max_revision_attempts
        for attempt in range(1, attempts + 1):
            with advisor_bypass():
                revised = await regenerate(revision)
            # Removing the finish tool is not a hard guarantee: some tool-call parsers accept any
            # function name the model writes. A resampled response is usually compliant.
            if not response_attempts_finish(revised, self.finish_tool):
                return revised, attempt
        return revised, attempts

    def _new_record(self, trigger: str) -> dict[str, Any]:
        return {
            "timestamp": time(),
            "attempt_id": self.attempt_id,
            "task": self.task.name,
            "solver_call_index": self.solver_calls,
            "trigger": trigger,
            "draft_attempted_finish": trigger == "pre_submit",
            "solver_model": self.solver_model,
            "advisor_model": self.config.endpoint.model,
            "cue_kind": self.config.cue.kind,
            "advisor_latency_sec": 0.0,
            "advisor_attempts": 0,
            "decision": None,
            "intervened": False,
            "cue_fallback": None,
            "hint_screen_hits": [],
            "delivered_cue": None,
            "revision_latency_sec": None,
            "revision_attempts": 0,
            "revision_attempted_finish": None,
            "revision_reasoning_tokens": None,
            "hint_persisted_to_trajectory": None,
            "hint_continuation_suffix": "",
            "privileged_files": len(self.task.files),
            "privileged_truncated_paths": list(self.task.truncated_paths),
            "privileged_omitted_paths": list(self.task.omitted_paths),
            "error": None,
        }

    def _record(self, record: dict[str, Any]) -> None:
        if self._artifacts is not None:
            self._artifacts.append_trace(record)
            self.write_summary()
