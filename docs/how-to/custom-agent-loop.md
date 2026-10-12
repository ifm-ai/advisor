# Add the advisor to your agent loop

## When to use this

Use this when you own the loop that calls the solver model, and can change it. The advisor needs
three things from you: the public history, the solver's draft response, and a `regenerate`
callback that queries the solver again.

## The contract

- Create one `Advisor` per attempt.
- Call `review(messages, draft, tools=..., regenerate=...)` on every solver turn.
- Commit the returned response, not the draft. It is the draft itself when the advisor did not
  intervene, and the solver's revision when it did.
- `regenerate` receives a `Revision`. Send `revision.messages` and `revision.tools`, and merge
  `revision.extra_body` into your request body.
- Call `advisor.write_summary()` when the attempt ends.

The core of `examples/02_custom_agent_loop.py`:

```python
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

async def regenerate(revision: Revision) -> Any:
    """Query the solver again with the advisor's revision request, unchanged."""

    return await call_solver(revision.messages, revision.tools, revision.extra_body)

for number in range(1, MAX_TURNS + 1):
    draft = await call_solver(messages, TOOLS)
    response = await advisor.review(messages, draft, tools=TOOLS, regenerate=regenerate)
    if response is not draft:
        print(f"turn {number}: the advisor intervened; committing the revised turn")
    message = response.choices[0].message
    messages.append(committed_message(message))
    ...  # run the message's tool calls and append their outputs

advisor.write_summary()
```

## Notes

- `messages` is never mutated. The advisor copies what it needs.
- Pass the same `tools` you offered on the draft's call. The advisor removes the finish tool from
  that list for a revision, unless its action is `CONFIRM_AND_SUBMIT`.
- Keep `reasoning_content` on the assistant turns you replay to K2-Horizon, even when it is empty.
  Its chat template rejects an assistant message that has no thinking field.
- Concurrency: one `Advisor` per attempt is safe across asyncio tasks. Do not share one `Advisor`
  between attempts, because it carries the attempt's plan, counters and delivered cues.
- Custom advisor backends: `complete=` accepts any `async (messages) -> str` that returns the
  advisor model's raw answer. The offline quickstart uses this to script the advisor.
- A missing advisor API key raises when the `Advisor` is created, not on every review. Pass
  `complete=` to skip the key lookup.

The example runs the model's shell commands on your machine. Run it in a container or a throwaway VM.
It is not an isolated evaluation either: those commands can read the toy task's `solution/` and
`tests/`, so it demonstrates the integration contract only. For a real evaluation, run the solver's
tools in a sandbox that holds only the public workspace.
