"""A minimal agent loop with the advisor in it, against any OpenAI-compatible solver endpoint.

WARNING: the default tool executor runs the model's shell commands on this machine, inside a
temporary copy of the toy task's workspace. Run this in a container or a throwaway VM. It is not
an isolated evaluation either: nothing stops those commands from reading the toy task's
`solution/` and `tests/` on this machine, so this demonstrates the integration contract only. For a
real evaluation, run the solver's tools in a sandbox that holds only the public workspace.

    pip install -e ".[dev]"     # the final check runs pytest
    export SOLVER_BASE_URL=http://localhost:30000/v1 SOLVER_API_KEY=...
    export ADVISOR_BASE_URL=http://localhost:30001/v1 ADVISOR_API_KEY=...
    export SOLVER_MODEL=IFM/K2-Horizon-375B-A23B ADVISOR_MODEL=IFM/K2-Horizon-375B-A23B  # defaults
    python examples/02_custom_agent_loop.py --out runs/custom-loop

The loop is the whole integration contract: get a draft, pass it to `advisor.review` with a
`regenerate` callback, commit whatever comes back, run its tool call, repeat.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import litellm

from ifm_advisor import Advisor, AdvisorConfig, EndpointConfig, Revision, TaskMaterial

TOY_TASK = Path(__file__).resolve().parent / "tasks" / "toy-task"
FINISH_TOOL = "complete"
MAX_TURNS = 30
COMMAND_TIMEOUT_SEC = 120
MAX_OUTPUT_CHARS = 10_000
SOLVER_MODEL = os.environ.get("SOLVER_MODEL", "IFM/K2-Horizon-375B-A23B")
ADVISOR_MODEL = os.environ.get("ADVISOR_MODEL", SOLVER_MODEL)
SYSTEM_PROMPT = (
    "You are a software engineer working in a repository in the current directory. Use the bash "
    "tool to read files, edit them and run commands. Call the complete tool when the task is done."
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the repository and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": FINISH_TOOL,
            "description": "Submit your work. Call it only when the task is complete.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def call_solver(
    messages: list[Any], tools: list[Any] | None, extra_body: dict | None = None
) -> Any:
    kwargs: dict[str, Any] = {
        "model": f"openai/{SOLVER_MODEL}",
        "messages": messages,
        "api_base": os.environ["SOLVER_BASE_URL"],
        "api_key": os.environ["SOLVER_API_KEY"],
        "temperature": 1.0,
        "top_p": 0.95,
    }
    if tools:
        kwargs["tools"] = tools
    if extra_body:
        kwargs["extra_body"] = extra_body
    return await litellm.acompletion(**kwargs)


async def regenerate(revision: Revision) -> Any:
    """Query the solver again with the advisor's revision request, unchanged."""

    return await call_solver(revision.messages, revision.tools, revision.extra_body)


def committed_message(message: Any) -> dict[str, Any]:
    """The committed turn as a plain dict for the history.

    reasoning_content is kept even when empty: K2-Horizon's chat template rejects an assistant
    message that has no thinking field.
    """

    calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments},
        }
        for call in (message.tool_calls or [])
    ]
    turn = {
        "role": "assistant",
        "content": message.content or "",
        "reasoning_content": getattr(message, "reasoning_content", None) or "",
    }
    if calls:
        turn["tool_calls"] = calls
    return turn


def run_bash(command: str, workspace: Path) -> str:
    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"(command timed out after {COMMAND_TIMEOUT_SEC}s)"
    output = (completed.stdout + completed.stderr)[-MAX_OUTPUT_CHARS:]
    return f"{output}\n(exit code {completed.returncode})"


def hidden_tests_pass(workspace: Path) -> bool:
    """Run the task's hidden tests against the final workspace, outside the solver's view."""

    with tempfile.TemporaryDirectory() as scratch:
        shutil.copy(TOY_TASK / "tests" / "test_outputs.py", Path(scratch) / "test_outputs.py")
        (Path(scratch) / "pytest.ini").write_text("[pytest]\n")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini"],
            cwd=scratch,
            env={**os.environ, "PYTHONPATH": str(workspace)},
            capture_output=True,
            text=True,
            timeout=120,
        )
    return completed.returncode == 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out", type=Path, default=Path("runs/custom-loop"), help="where the advisor writes"
    )
    args = parser.parse_args()

    task = TaskMaterial.from_task_dir(TOY_TASK)
    config = AdvisorConfig(
        endpoint=EndpointConfig(
            model=ADVISOR_MODEL,
            api_base=os.environ["ADVISOR_BASE_URL"],
            api_key_env="ADVISOR_API_KEY",
            timeout_sec=900.0,
        ),
        max_interventions=2,
        periodic_review_interval=4,
    )
    advisor = Advisor(
        config,
        task,
        finish_tool=FINISH_TOOL,
        trace_dir=args.out / "advisor",
        solver_model=SOLVER_MODEL,
        attempt_id="custom-loop",
    )

    with tempfile.TemporaryDirectory() as scratch:
        workspace = Path(scratch) / "workspace"
        shutil.copytree(TOY_TASK / "environment" / "workspace", workspace)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task.instruction},
        ]
        for number in range(1, MAX_TURNS + 1):
            draft = await call_solver(messages, TOOLS)
            response = await advisor.review(messages, draft, tools=TOOLS, regenerate=regenerate)
            if response is not draft:
                print(f"turn {number}: the advisor intervened; committing the revised turn")
            message = response.choices[0].message
            messages.append(committed_message(message))
            calls = message.tool_calls or []
            if not calls:
                messages.append({"role": "user", "content": "Respond with a tool call."})
                continue
            finished = False
            for call in calls:
                if call.function.name == FINISH_TOOL:
                    finished = True
                    output = "Submitted."
                else:
                    try:
                        command = json.loads(call.function.arguments or "{}").get("command", "")
                    except json.JSONDecodeError:
                        command = ""
                    print(f"turn {number}: $ {command[:100]}")
                    output = run_bash(command, workspace) if command else "(no command given)"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            if finished:
                break
        passed = hidden_tests_pass(workspace)

    advisor.write_summary()
    print(json.dumps(advisor.summary(), indent=2))
    print(f"hidden tests passed: {passed}")
    print(f"private trace (never publish): {args.out / 'advisor' / 'private_trace.jsonl'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
