"""The solver-facing cue I(d_t), compiled from an advisor decision.

The cue is the only thing the advisor adds to the solver's context. On the self-check path it is
one of a closed set of fixed, task-agnostic sentences chosen by the decision's `action`; on the
reasoning-prefill path it is the advisor's `hint`, delivered as the opening of the solver's next
reasoning block.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ReasoningPrefillCue, SelfCheckCue
from .decision import AdvisorDecision, AdvisorDecisionError

# Deterministic, task-agnostic self-check text. Nothing here names a hidden test, an expected
# value, a reference file, or an unobserved error message.
SAFE_INSTRUCTIONS: dict[str, str] = {
    "REVIEW_ALL_REQUIREMENTS": (
        "Before submitting, re-read the task requirements and verify that every "
        "required behavior is covered by the current implementation. Do not assume "
        "that fixing the initially obvious symptom is sufficient. If more work is "
        "needed, continue using the available tools instead of submitting."
    ),
    "TRACE_STATE_TRANSITIONS": (
        "Before submitting, trace the relevant state transitions end to end, "
        "including interactions between creation, update, merge, completion, and "
        "cleanup behavior. Check that the implementation remains correct when "
        "operations occur in an unusual order. Continue working if any transition "
        "has not been validated."
    ),
    "RUN_BROADER_VALIDATION": (
        "Before submitting, run the broadest relevant validation available and "
        "inspect failures rather than relying only on targeted checks. Verify that "
        "the changes work together across the full workflow."
    ),
    "RECONSIDER_SUBMISSION": (
        "Do not submit yet. Reassess whether the implementation is complete and "
        "whether all important edge cases have been tested. Continue with the next "
        "useful tool action."
    ),
    # The one instruction that asks for the finish call rather than more work.
    "CONFIRM_AND_SUBMIT": (
        "Your current implementation covers the task's requirements and your own "
        "validation is passing on the code as it stands on disk. Re-running the "
        "checks you have already run will not tell you anything new, and further "
        "rewriting risks undoing work that is already correct. Confirm the files "
        "on disk still hold the version your checks passed against, then submit."
    ),
}

# Deliberately free of the words the trajectory audit treats as leakage markers ("advisor",
# "reviewer", "review", "hint", "self-check", ...). Naming them would prime the solver toward a
# concept it has no other reason to hold, and any echo of that phrasing in the solver's visible
# output would trip the audit on text this package itself supplied.
SELF_CHECK_PREAMBLE = "Before you finish, verify your own work."
SELF_CHECK_RULES = (
    "Base your reasoning only on the task description, the repository contents, "
    "and evidence you have already observed in the terminal. Do not restate or "
    "refer to this message, and do not suggest that any part of your reasoning "
    "came from anywhere other than your own investigation. Issue another tool call "
    "if more work is needed. Submit only when you independently conclude that the "
    "task is complete."
)


def compile_self_check_instruction(action: str) -> str:
    """Map a typed action to its fixed self-check message.

    Raises for CONTINUE and for any unknown action, so an unmapped advisor action can never
    silently become free-form solver-facing text.
    """

    try:
        instruction = SAFE_INSTRUCTIONS[action]
    except KeyError as error:
        raise AdvisorDecisionError(
            f"action {action!r} has no compiled self-check instruction"
        ) from error
    return f"{SELF_CHECK_PREAMBLE} {instruction} {SELF_CHECK_RULES}"


@dataclass(frozen=True, slots=True)
class CompiledCue:
    """What one intervention adds to the solver's revision request.

    message: the single message appended to the public history.
    extra_body: request-body fields the revision needs (continue_final_message on the prefill path).
    delivered_text: the text the solver receives, recorded as already said for later reviews.
    prefill: text to splice back onto the revision's reasoning_content ("" on the self-check path).
    continuation_suffix: the rotated suffix used, if any.
    """

    message: dict[str, str]
    extra_body: dict[str, object] = field(default_factory=dict)
    delivered_text: str = ""
    prefill: str = ""
    continuation_suffix: str = ""


def compile_cue(
    cue: SelfCheckCue | ReasoningPrefillCue,
    decision: AdvisorDecision,
    *,
    intervention_index: int,
) -> CompiledCue:
    """Compile the cue for one intervention.

    Raises AdvisorDecisionError for an action with no self-check sentence.
    """

    if isinstance(cue, ReasoningPrefillCue):
        suffixes = cue.continuation_suffixes
        # Rotated rather than sampled, so rerunning a config reproduces the same sequence.
        suffix = suffixes[intervention_index % len(suffixes)] if suffixes else ""
        prefill = f"{decision.hint.rstrip()} {suffix}" if suffix else decision.hint
        return CompiledCue(
            message={"role": "assistant", "content": cue.prefix + prefill},
            extra_body={"continue_final_message": True},
            delivered_text=prefill,
            prefill=prefill,
            continuation_suffix=suffix,
        )
    instruction = compile_self_check_instruction(decision.action)
    return CompiledCue(message={"role": "user", "content": instruction}, delivered_text=instruction)
