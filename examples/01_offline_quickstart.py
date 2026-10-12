"""Walk through the advisor offline: no endpoint, no network, no API key.

    python examples/01_offline_quickstart.py

A scripted solver and a scripted advisor stand in for the two models, so you can see exactly what
the advisor commits at each turn: a proposed finish blocked and revised, a draft kept unchanged,
and a correct implementation confirmed for submission.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from ifm_advisor import Advisor, AdvisorConfig, EndpointConfig, Revision, TaskMaterial

TOY_TASK = Path(__file__).resolve().parent / "tasks" / "toy-task"
FINISH_TOOL = "complete"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {"name": FINISH_TOOL, "parameters": {"type": "object", "properties": {}}},
    },
]


def turn(reasoning: str, tool: str, command: str | None = None) -> dict[str, Any]:
    """A chat completion whose message calls one tool, as an OpenAI-compatible server returns it."""

    arguments = json.dumps({"command": command} if command else {})
    call = {
        "id": f"call_{tool}",
        "type": "function",
        "function": {"name": tool, "arguments": arguments},
    }
    message = {
        "role": "assistant",
        "content": "",
        "reasoning_content": reasoning,
        "tool_calls": [call],
    }
    return {"choices": [{"finish_reason": "tool_calls", "message": message}]}


def decision(
    action: str, *, intervene: bool = True, reason_code: str = "OTHER", plan: str = ""
) -> str:
    """The JSON an advisor model returns."""

    return json.dumps(
        {
            "intervene": intervene,
            "confidence": 0.9,
            "reason_code": reason_code,
            "action": action,
            "plan": plan,
            "hint": "",
            "private_diagnosis": "(private: written only to the private trace)",
        }
    )


# What the solver drafts at each turn.
DRAFTS = [
    turn("Let me look at the code first.", "bash", "cat stats.py"),
    turn("Sorting and taking the middle element is what it does. Looks fine.", FINISH_TOOL),
    turn("Averaging the middle pair for even lengths.", "bash", "python - <<'PY'\n...\nPY"),
    turn(
        "Let me check both cases.",
        "bash",
        "python -c 'import stats; print(stats.median([4, 1, 3, 2]))'",
    ),
    turn("And the empty list.", "bash", "python -c 'import stats; stats.median([])'"),
    turn("Maybe I should restructure this into a class.", "bash", "cat stats.py"),
]
# What the solver produces when asked to revise.
REVISIONS = [
    turn(
        "I only tried one list. What does an even-length list return?",
        "bash",
        "python -c 'import stats; print(stats.median([4, 1, 3, 2]))'",
    ),
    turn("My checks pass on the code as it is on disk, so I'll submit.", FINISH_TOOL),
]
# The advisor's decisions, in review order.
DECISIONS = [
    decision(
        "RUN_BROADER_VALIDATION",
        reason_code="INSUFFICIENT_END_TO_END_VALIDATION",
        plan="1. even length 2. empty list",
    ),
    decision(
        "CONTINUE",
        intervene=False,
        reason_code="COMPLETE",
        plan="1. even length: fixed 2. empty list: open",
    ),
    decision(
        "CONFIRM_AND_SUBMIT",
        reason_code="COMPLETE",
        plan="1. even length: fixed 2. empty list: fixed",
    ),
]


def describe(response: dict[str, Any]) -> str:
    call = response["choices"][0]["message"]["tool_calls"][0]["function"]
    command = json.loads(call["arguments"]).get("command", "")
    return f"{call['name']}({command.splitlines()[0][:60]!r})" if command else f"{call['name']}()"


async def main() -> None:
    decisions = iter(DECISIONS)
    revisions = iter(REVISIONS)

    async def advisor_model(messages: list[dict[str, str]]) -> str:
        sections = [
            line.strip("= ")
            for line in messages[1]["content"].splitlines()
            if line.startswith("=== ")
        ]
        print(f"    advisor reviews; its request has sections: {', '.join(sections)}")
        return next(decisions)

    async def regenerate(revision: Revision) -> dict[str, Any]:
        cue = revision.messages[-1]
        print(f"    cue to the solver ({cue['role']} message): {cue['content'][:80]}...")
        offered = [tool["function"]["name"] for tool in revision.tools]
        print(f"    tools offered for the revision: {offered}")
        return next(revisions)

    task = TaskMaterial.from_task_dir(TOY_TASK)
    config = AdvisorConfig(
        # Never called: advisor_model below stands in for the advisor endpoint.
        endpoint=EndpointConfig(model="scripted-advisor", api_base="http://localhost/v1"),
        max_interventions=2,
        periodic_review_interval=2,
    )
    with tempfile.TemporaryDirectory() as scratch:
        trace_dir = Path(scratch) / "advisor"
        advisor = Advisor(
            config, task, finish_tool=FINISH_TOOL, trace_dir=trace_dir, complete=advisor_model
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": task.instruction}]
        for number, draft in enumerate(DRAFTS, start=1):
            print(f"turn {number}: solver drafts {describe(draft)}")
            committed = await advisor.review(messages, draft, tools=TOOLS, regenerate=regenerate)
            outcome = "kept the draft" if committed is draft else "committed the revision"
            print(f"  -> {outcome}: {describe(committed)}")
            message = committed["choices"][0]["message"]
            messages.append(message)
            call = message["tool_calls"][0]
            if call["function"]["name"] == FINISH_TOOL:
                print("solver submitted.")
                break
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": "(output omitted offline)"}
            )
        advisor.write_summary()
        print(json.dumps(advisor.summary(), indent=2))
        records = (trace_dir / "private_trace.jsonl").read_text().splitlines()
        print(f"private trace: {len(records)} review records (privileged; never publish this file)")


if __name__ == "__main__":
    asyncio.run(main())
