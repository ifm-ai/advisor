# Use it with LiteLLM and Harbor

## When to use this

Use this when the agent you want to advise calls `litellm.acompletion` in your own process, exposes
a finish tool, and you do not want to change it. The hook wraps `litellm.acompletion`, so each call
the agent makes inside the block is one solver turn.

The hook sees only calls made in the process that installed it, through `litellm.acompletion`
looked up on the `litellm` module. It cannot see an agent that runs inside its container, one that
bound `acompletion` by name before `install()` ran, or one that uses the synchronous API. Such a
run is never reviewed: it is a baseline run labelled as advised. Two warnings catch this as it
happens:

- leaving an `advised` block in which no `litellm.acompletion` call was seen logs a warning;
- the `Advisor` logs a warning, once, when the tools offered to the solver do not include its
  `finish_tool`, because proposed finishes can then never be reviewed.

Afterwards, `ifm-advisor summarize` counts advised attempts that were never reviewed as
`advised_without_reviews`; see [Summarize runs](summarize-and-report.md).

## Install the hook and bind an advisor

- `litellm_hook.install()` once per process. It is idempotent.
- `litellm_hook.advised(advisor)` once per attempt. Leaving the block writes the summary.
- Only `litellm.acompletion` is wrapped. Calls outside a block, the advisor's own calls, and calls
  LiteLLM makes from inside a draft call (its fallbacks) pass straight through.

```python
from ifm_advisor import litellm_hook

litellm_hook.install()
with litellm_hook.advised(advisor):
    await run_agent()
```

Each attempt gets its own advisor, even when attempts run concurrently in one process, because the
binding is per asyncio task.

## Harbor

| Harbor agent | Supported | Why |
|---|---|---|
| `mini-swe-agent-external-finish` | Yes | Runs LiteLLM in the host process, where the hook is installed, and exposes a finish tool |
| `mini-swe-agent` (stock, installed) | No | Runs inside the task container, where the host hook cannot see its calls |
| `terminus-2` | No | Has no finish tool, so only periodic reviews could apply |

The `mini-swe-agent-external-finish` agent of the public `LLM360/harbor` fork exposes a finish tool.
Its `finish_tool_name` must equal the `Advisor`'s `finish_tool`.

Put `trace_dir` at `<trials_dir>/<trial_name>/advisor`. Then `ifm-advisor audit` and
`ifm-advisor summarize` find the advisor files next to Harbor's own. Never put it under
`<trials_dir>/<trial_name>/agent`: Harbor mounts that directory into the task container, so the
solver could read the private trace during the run.

The core of `examples/03_harbor_mini_swe_agent.py`:

```python
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
```

The agent's own settings in that example:

```python
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
```

This agent does not accept a top-level `api_base` argument. The solver URL reaches LiteLLM through
`model_overrides.model_kwargs.api_base` and the `OPENAI_API_BASE` environment variable, which the
example sets from `SOLVER_BASE_URL`. The solver key goes through `OPENAI_API_KEY`, which keeps it out
of the trial configuration Harbor serializes.

The example requires Docker, a Terminal-Bench task checkout and two OpenAI-compatible endpoints. For
reasoning prefill the solver must be served by SGLang; check it first with
[`examples/check_reasoning_prefill.py`](reasoning-prefill.md).

## Exporting trajectories

Copy `agent/trajectory.json`, never whole trial directories. A trial directory contains
`advisor/private_trace.jsonl`, which is derived from privileged material. See
[Privileged-information isolation](../explanation/isolation.md).
