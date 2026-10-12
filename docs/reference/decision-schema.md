# Decision schema

The advisor returns exactly one JSON object per review. `AdvisorDecision` validates it strictly.

## Fields

| Field | Type | Meaning |
|---|---|---|
| `intervene` | `bool` | Whether to interrupt the solver. With an action other than `CONTINUE`, this is the whole gate: there is no confidence threshold |
| `confidence` | `float`, 0.0 to 1.0 | The advisor's confidence. Recorded, never gated on |
| `reason_code` | enum | Why: see below |
| `action` | enum | What the cue asks of the solver: see below |
| `plan` | `str` | Private. The task's current to-do list and the solver's progress against it, carried to the next review. It records task state, never a future verdict |
| `hint` | `str` | First-person continuation of the solver's own reasoning. Used only on the reasoning-prefill path; empty when not intervening |
| `private_diagnosis` | `str` | Private. Never shown to the solver |

`plan` and `private_diagnosis` may reference hidden tests or the reference solution, and are written
only to the private trace. `hint` is the one free-text field that can reach the solver.

An intervention happens when `intervene` is true and `action` is not `CONTINUE`.

## Reason codes

`COMPLETE`, `INCOMPLETE_REQUIREMENT_COVERAGE`, `UNVERIFIED_STATE_TRANSITIONS`,
`INSUFFICIENT_END_TO_END_VALIDATION`, `PREMATURE_SUBMISSION`, `OTHER`.

## Actions

| Action | Meaning |
|---|---|
| `CONTINUE` | The solver may submit. Set `intervene` to false |
| `REVIEW_ALL_REQUIREMENTS` | Requirement coverage looks incomplete |
| `TRACE_STATE_TRANSITIONS` | Interacting state changes have not been reasoned through end to end |
| `RUN_BROADER_VALIDATION` | Validation was too narrow to support submission |
| `RECONSIDER_SUBMISSION` | Submission looks premature for another reason |
| `CONFIRM_AND_SUBMIT` | The work is correct and complete right now, and the solver is not trying to finish. Set `intervene` to true. It is the only action that keeps the finish tool for the revision |

On the self-check path, each action except `CONTINUE` maps to one fixed sentence. An action with no
sentence raises `AdvisorDecisionError`, so an unmapped action can never become free-form text for the
solver.

## Strict parsing

- Unknown fields are rejected.
- Enum fields must be plain strings. A list-wrapped enum such as `"action": ["CONTINUE"]` is
  rejected.
- A single JSON code fence around the object is tolerated. Prose before or after it is not.
- A malformed or failed answer is retried up to `max_advisor_attempts` times in total. After that,
  `fail_open` decides whether the draft is kept or the error is raised.
- The `AdvisorDecisionError` message gives, for each problem, the field's location and pydantic's
  description of what is wrong. It never contains the advisor's output: an unknown field is shown
  as `<unknown field>` rather than by its name, and the error carries no chained exception. With
  `fail_open=False` the error reaches the agent, which may write it into the trajectory.

## Example

```json
{"intervene": false, "confidence": 0.8, "reason_code": "COMPLETE", "action": "CONTINUE", "plan": "1. verify the merge path handles ties. 2. confirm GC respects the new watermark.", "hint": "", "private_diagnosis": "implementation covers the reported symptoms; no gaps found"}
```
