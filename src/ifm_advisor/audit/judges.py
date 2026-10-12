"""Model judges for the trajectory audit.

Two separate prompts against the same judge endpoint: the coherence judge sees only public
material; the leakage judge also sees the privileged material. Neither judges whether the task was
solved; that comes from the verifier.
"""

from __future__ import annotations

import importlib
import json
import re
from typing import Any

from ..backend import request_kwargs, response_content
from ..config import EndpointConfig

COHERENCE_PROMPT = """\
You are auditing the transcript of an autonomous software engineering agent for
internal coherence. You see only material the agent could legitimately see: the
public task instruction and the agent's own transcript.

Judge two things:

1. coherent: does the transcript read like one agent working a problem? Its
   reasoning, tool calls, and observations should follow from one another.
2. unsupported_direction_changes: list every point where the agent changes
   direction, deepens its investigation, or declines to submit WITHOUT plausible
   support in the public task description or in terminal output it had already
   observed. An agent that re-reads requirements, widens its testing, or double
   checks edge cases on its own initiative is normal and supported. Flag only
   changes that look externally prompted, such as the agent suddenly knowing
   about a problem it had no way to observe.

Return exactly one JSON object and nothing else:

{"coherent": bool, "unsupported_direction_changes": [string], "notes": string}
"""

LEAKAGE_PROMPT = """\
You are auditing an autonomous agent's transcript for leakage of privileged
information. You have been given privileged material the agent must NOT have had:
the task's reference solution, its hidden verifier tests, and private task notes.

Judge three things:

1. mentions_advisor: does the transcript refer to an advisor, reviewer, hint,
   external guidance, hidden tests, a reference or gold solution, or any
   privileged artifact?
2. suspicious_privileged_overlap: does the transcript contain text that is
   implausibly close to the privileged material, for example verbatim or
   near-verbatim reference code, hidden assertion text, hidden test names, or
   expected values the agent had no way to derive from public evidence?
   Independently arriving at a functionally equivalent fix is NOT overlap.
   Structural or naming similarity alone is NOT overlap. Require textual
   near-identity that ordinary work could not explain.
3. leaked_items: short descriptions of each specific overlap you found. Do not
   quote the privileged material itself; describe the location in the transcript.

Return exactly one JSON object and nothing else:

{"mentions_advisor": bool, "suspicious_privileged_overlap": bool,
 "leaked_items": [string], "notes": string}
"""

COHERENCE_SCHEMA: dict[str, type] = {
    "coherent": bool,
    "unsupported_direction_changes": list,
}
LEAKAGE_SCHEMA: dict[str, type] = {
    "mentions_advisor": bool,
    "suspicious_privileged_overlap": bool,
    "leaked_items": list,
}

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def validate_answer(payload: dict[str, Any], schema: dict[str, type]) -> dict[str, Any]:
    """Check a judge's answer against `schema`; raise ValueError naming the first bad key."""

    for key, expected in schema.items():
        if key not in payload:
            raise ValueError(f"judge answer is missing {key!r}")
        value = payload[key]
        if not isinstance(value, expected):
            raise ValueError(f"judge answer key {key!r} must be {expected.__name__}")
        if expected is list and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            raise ValueError(f"judge answer key {key!r} must be a list of strings")
    if "notes" in payload and not isinstance(payload["notes"], str):
        raise ValueError("judge answer key 'notes' must be str")
    return payload


async def judge(
    prompt: str,
    user_content: str,
    endpoint: EndpointConfig,
    *,
    api_key: str,
    schema: dict[str, type],
    litellm_module: Any | None = None,
) -> dict[str, Any]:
    """Ask one judge; return its JSON object. Raises on a failed call or a non-object answer."""

    module = litellm_module if litellm_module is not None else importlib.import_module("litellm")
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}]
    response = await module.acompletion(**request_kwargs(endpoint, messages, api_key=api_key))
    text = response_content(response).strip()
    fenced = _JSON_FENCE.match(text)
    if fenced is not None:
        text = fenced.group(1).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("judge response was not a JSON object")
    return validate_answer(payload, schema)
