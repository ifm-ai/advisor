# Getting started

This tutorial runs the advisor offline. A scripted solver and a scripted advisor stand in for the
two models, so you see exactly what the advisor commits at each turn without an endpoint, a network
connection or an API key.

## 1. Install

From a clone of the repository, with Python 3.12 or newer:

```bash
pip install -e .
```

## 2. Run the offline walkthrough

```bash
python examples/01_offline_quickstart.py
```

The output:

```text
turn 1: solver drafts bash('cat stats.py')
  -> kept the draft: bash('cat stats.py')
turn 2: solver drafts complete()
    advisor reviews; its request has sections: REVIEW TRIGGER, PUBLIC TASK, PUBLIC CONVERSATION, SOLVER'S LATEST TURN, PRIVILEGED REFERENCE MATERIAL, REQUIRED JSON RESPONSE SCHEMA
    cue to the solver (user message): Before you finish, verify your own work. Before submitting, run the broadest rel...
    tools offered for the revision: ['bash']
  -> committed the revision: bash("python -c 'import stats; print(stats.median([4, 1, 3, 2]))'")
turn 3: solver drafts bash("python - <<'PY'")
  -> kept the draft: bash("python - <<'PY'")
turn 4: solver drafts bash("python -c 'import stats; print(stats.median([4, 1, 3, 2]))'")
    advisor reviews; its request has sections: REVIEW TRIGGER, PUBLIC TASK, PUBLIC CONVERSATION, SOLVER'S LATEST TURN, YOUR PREVIOUS PLAN, HINTS YOU HAVE ALREADY DELIVERED THIS ATTEMPT, PRIVILEGED REFERENCE MATERIAL, REQUIRED JSON RESPONSE SCHEMA
  -> kept the draft: bash("python -c 'import stats; print(stats.median([4, 1, 3, 2]))'")
turn 5: solver drafts bash("python -c 'import stats; stats.median([])'")
  -> kept the draft: bash("python -c 'import stats; stats.median([])'")
turn 6: solver drafts bash('cat stats.py')
    advisor reviews; its request has sections: REVIEW TRIGGER, PUBLIC TASK, PUBLIC CONVERSATION, SOLVER'S LATEST TURN, YOUR PREVIOUS PLAN, HINTS YOU HAVE ALREADY DELIVERED THIS ATTEMPT, PRIVILEGED REFERENCE MATERIAL, REQUIRED JSON RESPONSE SCHEMA
    cue to the solver (user message): Before you finish, verify your own work. Your current implementation covers the ...
    tools offered for the revision: ['bash', 'complete']
  -> committed the revision: complete()
solver submitted.
{
  "enabled": true,
  "attempt_id": "",
  "task": "toy-task",
  "solver_model": "",
  "advisor_model": "scripted-advisor",
  "prompt_version": "advisor_v1",
  "solver_profile": "generic",
  "cue": "self_check",
  "solver_calls": 6,
  "finish_reviews": 1,
  "periodic_reviews": 2,
  "interventions": 2,
  "advisor_failures": 0,
  "revision_calls": 2,
  "hint_screen_fallbacks": 0,
  "empty_hint_fallbacks": 0
}
private trace: 3 review records (privileged; never publish this file)
```

## 3. Read the output turn by turn

The example configures `max_interventions=2` and `periodic_review_interval=2`, so the advisor
reviews every proposed finish and every second solver turn.

- **Turn 1** is an ordinary action. The gate is closed (it is not a finish and not a second turn),
  so the advisor is not called and the draft is kept.
- **Turn 2** is a proposed finish. The advisor reviews it as `pre_submit` and intervenes. The cue
  is a fixed self-check sentence chosen by the advisor's action (`RUN_BROADER_VALIDATION`), not text
  the advisor wrote. The finish tool is withheld for the revision: the solver is offered only
  `bash`, so it must take one more real action. The revision is committed in place of the draft.
- **Turn 4** is a periodic review. Its request now includes the advisor's previous plan and the
  cues already delivered. The decision is `CONTINUE`, so the draft is kept.
- **Turn 6** is a periodic review that finds the work complete although the solver is not trying
  to finish. The decision is `CONFIRM_AND_SUBMIT`, so the revision request keeps the finish tool
  (`['bash', 'complete']`) and the solver submits.

Turns 3 and 5 are not review turns, so the advisor is not called for them.

## 4. What was written

The advisor wrote three files to its `trace_dir`. The example uses a temporary directory and
deletes it, but in a real run they persist. See [Artifacts](../reference/artifacts.md) for every key.

- `summary.json` holds counters and settings only. It is the JSON printed above.
- `task_material_manifest.json` holds the paths, sizes and SHA-256 hashes of the privileged files,
  never their contents.
- `private_trace.jsonl` holds one record per review, including the advisor's plan and diagnosis.

**`private_trace.jsonl` is derived from privileged material. Never publish it or ship it with a
trajectory.**

## 5. Next steps

- [Add the advisor to your own agent loop](../how-to/custom-agent-loop.md)
- [Use it with LiteLLM and Harbor](../how-to/litellm-and-harbor.md)
