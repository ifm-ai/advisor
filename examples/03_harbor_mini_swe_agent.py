"""Advise one Harbor trial: mini-swe-agent-external-finish, K2-Horizon served by SGLang.

This agent runs LiteLLM in this process, where the hook can see its calls, and exposes a finish
tool. Harbor's stock installed mini-swe-agent runs inside the task container instead, where the hook
cannot see its calls, so it cannot be advised this way.

Requires
  - the public LLM360 Harbor fork:
        pip install "harbor @ git+https://github.com/LLM360/harbor.git@prod"
  - Docker, for Harbor's local `docker` environment;
  - a Terminal-Bench task checkout, for example tasks/session-window-debug from
    https://github.com/harbor-framework/terminal-bench ;
  - two OpenAI-compatible endpoints. For reasoning prefill the solver must be served by SGLang with
    `--reasoning-parser k2_horizon --tool-call-parser k2_horizon`; check it first with
    examples/check_reasoning_prefill.py.

    export SOLVER_BASE_URL=http://localhost:30000/v1  SOLVER_API_KEY=...
    export ADVISOR_BASE_URL=http://localhost:30001/v1 ADVISOR_API_KEY=...
    python examples/03_harbor_mini_swe_agent.py \\
        --task-dir ~/terminal-bench/tasks/session-window-debug --trials-dir runs/advised

The advisor writes to <trials-dir>/<trial-name>/advisor/, next to Harbor's own files, which is
where `ifm-advisor audit` and `summarize` look. Export trajectories from
agent/trajectory.json, never whole trial directories: advisor/private_trace.jsonl is privileged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from ifm_advisor import (
    Advisor,
    AdvisorConfig,
    EndpointConfig,
    ReasoningPrefillCue,
    TaskMaterial,
    litellm_hook,
)

K2_HORIZON = "IFM/K2-Horizon-375B-A23B"
FINISH_TOOL = "complete"


def trial_config(task_dir: Path, trials_dir: Path, trial_name: str):
    """A Harbor TrialConfig for mini-swe-agent-external-finish, on local Docker."""

    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
        VerifierConfig,
    )

    solver_base = os.environ["SOLVER_BASE_URL"]
    # This agent calls LiteLLM in this process. Passing the key through these variables keeps
    # it out of the TrialConfig that Harbor serializes next to the trajectory.
    os.environ["OPENAI_API_BASE"] = solver_base
    os.environ["OPENAI_API_KEY"] = os.environ["SOLVER_API_KEY"]
    return TrialConfig(
        task=TaskConfig(path=task_dir),
        trial_name=trial_name,
        trials_dir=trials_dir,
        agent=AgentConfig(
            name="mini-swe-agent-external-finish",
            model_name=f"openai/{K2_HORIZON}",
            kwargs={
                "finish_tool_name": FINISH_TOOL,
                "finish_tool_require_output_files": False,
                "command_timeout_sec": 600,
                "litellm_timeout_sec": 1200.0,
                "model_overrides": {
                    "model_kwargs": {
                        "temperature": 1.0,
                        "top_p": 0.95,
                        "timeout": 1200.0,
                        "api_base": solver_base,
                    }
                },
            },
        ),
        environment=EnvironmentConfig(type="docker", delete=True),
        verifier=VerifierConfig(),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--trials-dir", type=Path, default=Path("runs/advised"))
    parser.add_argument("--trial-name", default=None)
    parser.add_argument("--max-interventions", type=int, default=8)
    parser.add_argument("--periodic-review-interval", type=int, default=4)
    args = parser.parse_args()

    from harbor.trial.trial import Trial

    task_dir = args.task_dir.expanduser().resolve()
    trials_dir = args.trials_dir.expanduser().resolve()
    trial_name = args.trial_name or f"{task_dir.name}-advised"
    config = AdvisorConfig(
        endpoint=EndpointConfig(
            model=K2_HORIZON,
            api_base=os.environ["ADVISOR_BASE_URL"],
            api_key_env="ADVISOR_API_KEY",
            timeout_sec=900.0,
        ),
        max_interventions=args.max_interventions,
        periodic_review_interval=args.periodic_review_interval,
        cue=ReasoningPrefillCue.k2_horizon_sglang(),
        solver_profile="k2_horizon",
        screen_hints=True,
    )
    advisor = Advisor(
        config,
        TaskMaterial.from_task_dir(task_dir),
        finish_tool=FINISH_TOOL,
        trace_dir=trials_dir / trial_name / "advisor",
        solver_model=K2_HORIZON,
        attempt_id=trial_name,
    )

    litellm_hook.install()
    with litellm_hook.advised(advisor):
        trial = await Trial.create(config=trial_config(task_dir, trials_dir, trial_name))
        result = await trial.run()

    rewards = result.verifier_result.rewards if result.verifier_result else None
    print(json.dumps({"advisor": advisor.summary(), "rewards": rewards}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
