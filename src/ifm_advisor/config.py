"""Advisor settings: frozen pydantic models that reject unknown fields."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, field_validator

_ENVIRONMENT_VARIABLE = r"^[A-Za-z_][A-Za-z0-9_]*$"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EndpointConfig(_Frozen):
    """An OpenAI-compatible endpoint serving the advisor (or audit judge) model.

    Only the name of the API-key variable is stored; the key is read from the environment when a
    call is made and is never written anywhere.
    """

    model: str = Field(min_length=1)
    # LiteLLM provider prefix. The request model is f"{provider}/{model}", so a Hugging Face ID such
    # as "IFM/K2-Horizon-375B-A23B" is never mistaken for a provider. "" sends `model` unchanged.
    provider: str = "openai"
    api_base: str = Field(min_length=1)
    api_key_env: str = Field(default="ADVISOR_API_KEY", pattern=_ENVIRONMENT_VARIABLE)
    extra_body: dict[str, object] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # The advisor request carries the whole privileged payload, so it is far larger than a solver
    # turn and its latency tracks endpoint load. Size this above the slowest review you are willing
    # to wait for: with fail_open, a timeout lets the unreviewed draft through.
    timeout_sec: PositiveFloat = 300.0

    @field_validator("api_base")
    @classmethod
    def _require_http(cls, value: str) -> str:
        if not re.match(r"^https?://", value, flags=re.IGNORECASE):
            raise ValueError("api_base must start with http:// or https://")
        return value

    @field_validator("timeout_sec")
    @classmethod
    def _require_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout_sec must be finite")
        return value

    @property
    def litellm_model(self) -> str:
        """The model string LiteLLM receives."""

        return f"{self.provider}/{self.model}" if self.provider else self.model


class SelfCheckCue(_Frozen):
    """Deliver each intervention as one fixed, task-agnostic user message chosen by `action`.

    Works with any solver model and server; the advisor's free-text `hint` is never used.
    """

    kind: Literal["self_check"] = "self_check"


class ReasoningPrefillCue(_Frozen):
    """Deliver each intervention as the opening of the solver's next reasoning block.

    The revision request ends with an assistant message holding `prefix + hint` and sets
    `continue_final_message`, so the solver continues the advisor's sentences as its own thought.
    This needs a server and chat template that continue a prefilled reasoning block; see
    `k2_horizon_sglang()`.
    """

    kind: Literal["reasoning_prefill"] = "reasoning_prefill"
    # Text placed before the hint, e.g. a reasoning-open token for a template whose generation
    # prompt does not already open a reasoning block.
    prefix: str = ""
    # Appended to the hint after a space, rotated deterministically by intervention index. Only
    # strings the solver itself writes belong here; empty is the control arm.
    continuation_suffixes: tuple[str, ...] = ()

    @classmethod
    def k2_horizon_sglang(
        cls, *, continuation_suffixes: tuple[str, ...] = ()
    ) -> ReasoningPrefillCue:
        """K2-Horizon served by SGLang.

        SGLang's continue_final_message drops the trailing assistant message, renders the rest
        with the generation prompt (for K2-Horizon, the assistant header followed by an open
        `<ifm|think>` block), then appends the dropped text, so the hint already lands inside the
        open reasoning block and no prefix is needed.
        """

        return cls(prefix="", continuation_suffixes=continuation_suffixes)


Cue = Annotated[SelfCheckCue | ReasoningPrefillCue, Field(discriminator="kind")]


class AdvisorConfig(_Frozen):
    """How the advisor reviews one attempt. B and k refer to the method's notation."""

    endpoint: EndpointConfig
    # B: interventions allowed per attempt. Reviews stop once it is spent.
    max_interventions: PositiveInt = 2
    # k: also review every k solver turns. None reviews proposed finishes only.
    periodic_review_interval: PositiveInt | None = None
    # Total attempts, including the first, to get a well-formed decision before fail_open applies.
    # Some models occasionally wrap an enum field in a one-item list; a resample is usually fine.
    max_advisor_attempts: PositiveInt = 2
    # Total attempts to get a revision that does not itself call the finish tool. Some tool-call
    # parsers (K2-Horizon's included) do not check a parsed function name against the tools
    # offered on the request, so removing the finish tool is not a hard guarantee.
    max_revision_attempts: PositiveInt = 2
    cue: Cue = Field(default_factory=SelfCheckCue)
    # Reasoning-prefill only: screen each hint with the audit's mechanical leak checks before
    # delivery, and fall back to the self-check sentence when it fails.
    screen_hints: bool = False
    # A packaged profile name ("generic", "k2_horizon") or a Path to your own profile text.
    solver_profile: str | Path = "generic"
    prompt_version: str = Field(default="advisor_v1", min_length=1)
    # Show the advisor that the same reason_code recurred across consecutive interventions. Off by
    # default: escalation pushes the advisor toward answer-shaped hints, whose solved trajectories
    # record decisions that were handed over rather than derived. Enable only together with
    # screen_hints.
    repeat_streak_escalation: bool = False
    # On an advisor or revision failure, keep the solver's draft instead of raising.
    fail_open: bool = True
    # Bounded rendering of the public history: the first two messages plus the most recent tail.
    max_conversation_messages: int = Field(default=50, ge=3)
    max_message_chars: PositiveInt = 8_000
    # Cues already delivered this attempt that are shown back to the advisor, newest last.
    max_previous_cues: PositiveInt = 8
