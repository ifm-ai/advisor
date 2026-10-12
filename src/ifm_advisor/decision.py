"""The advisor's typed decision d_t and its strict parser.

`private_diagnosis` and `plan` are private: they may reference hidden tests or the reference
solution and are written only to the private trace. `hint` is the one free-text field that can
reach the solver, and only on the reasoning-prefill cue path (see cues.py); the advisor's system
prompt requires it to be solver-safe, first-person reasoning.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from pydantic_core import ErrorDetails

ReasonCode = Literal[
    "COMPLETE",
    "INCOMPLETE_REQUIREMENT_COVERAGE",
    "UNVERIFIED_STATE_TRANSITIONS",
    "INSUFFICIENT_END_TO_END_VALIDATION",
    "PREMATURE_SUBMISSION",
    "OTHER",
]
AdvisorAction = Literal[
    "CONTINUE",
    "REVIEW_ALL_REQUIREMENTS",
    "TRACE_STATE_TRANSITIONS",
    "RUN_BROADER_VALIDATION",
    "RECONSIDER_SUBMISSION",
    # The only action that pushes toward finishing rather than away from it.
    # Every other action gates a finish the solver already attempted; this one
    # exists because a periodic review can find the work correct at a moment
    # when the solver is not attempting to finish at all, and CONTINUE there
    # merely declines to interrupt. Measured on one run: the solver reached
    # a reference-matching tree with its full battery passing at turn 112, then
    # reverted it by turn 116 and went on thrashing. Correctness and finish
    # attempts never coincided, so nothing converted the one into the other.
    "CONFIRM_AND_SUBMIT",
]


class AdvisorDecisionError(ValueError):
    """The advisor response was not a valid typed decision."""


class AdvisorDecision(BaseModel):
    """Strict decision schema; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intervene: bool
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    reason_code: ReasonCode
    action: AdvisorAction
    plan: str
    hint: str
    private_diagnosis: str


def _enum_field(values: tuple[str, ...]) -> str:
    """Describe an enum field as prose, never as a JSON array.

    A raw JSON array here (``"reason_code": ["A", "B", ...]``) reads as an
    invitation to answer with a list; K2-Horizon has echoed exactly that shape
    back as its actual field value (e.g. ``"action": ["CONTINUE"]``).
    """

    return "string - exactly one of: " + " | ".join(values)


RESPONSE_SCHEMA_TEXT = json.dumps(
    {
        "intervene": "boolean",
        "confidence": "number between 0.0 and 1.0",
        "reason_code": _enum_field(get_args(ReasonCode)),
        "action": _enum_field(get_args(AdvisorAction)),
        "plan": (
            "string, the task's current to-do list/plan and progress against it "
            "(see THE `plan` FIELD); carried forward to your next review"
        ),
        "hint": (
            "string, first-person continuation of the solver's own reasoning "
            "(see THE `hint` FIELD); empty string when not intervening"
        ),
        "private_diagnosis": "string, private, never shown to the solver",
    },
    indent=2,
)

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_advisor_decision(content: str) -> AdvisorDecision:
    """Strictly parse a typed decision from raw advisor output."""

    if not isinstance(content, str) or not content.strip():
        raise AdvisorDecisionError("advisor response was empty")
    candidate = content.strip()
    fenced = _JSON_FENCE.match(candidate)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        return AdvisorDecision.model_validate_json(candidate)
    except ValidationError as error:
        problems = "; ".join(
            _describe(problem) for problem in error.errors(include_url=False, include_input=False)
        )
    # Raised outside the except block, so the error carries no __context__: pydantic's error quotes
    # the advisor's output, which may hold a private diagnosis, and with fail_open=False this error
    # reaches the agent.
    raise AdvisorDecisionError(f"advisor response was not a valid decision: {problems}")


UNKNOWN_FIELD = "<unknown field>"


def _describe(problem: ErrorDetails) -> str:
    """Location and message of one validation problem, without any of the advisor's text."""

    # A name that is not one of the decision's fields came from the advisor; list indices stay.
    location = [
        UNKNOWN_FIELD
        if isinstance(part, str) and part not in AdvisorDecision.model_fields
        else str(part)
        for part in problem["loc"]
    ]
    return f"{'.'.join(location) or 'response'}: {problem['msg']}"


def should_intervene(decision: AdvisorDecision) -> bool:
    """Apply the narrow intervention gate to a parsed decision.

    There is no numeric confidence threshold: the advisor itself is the only
    judge of whether an intervention is warranted, per its system prompt.
    """

    return decision.intervene and decision.action != "CONTINUE"
