# Enable reasoning prefill

## When to use this

Use this when your solver is K2-Horizon served by SGLang and you want the advisor's hint delivered as
the opening of the solver's next reasoning block, instead of as a fixed self-check message. For any
other model or server, keep the default `SelfCheckCue`.

## What it changes

With `SelfCheckCue` the solver receives one of a closed set of fixed, task-agnostic user messages,
chosen by the advisor's action. With `ReasoningPrefillCue` the revision request ends with an
assistant message holding the advisor's `hint`, and the request sets `continue_final_message`. The
solver continues the hint as its own thought, and the committed turn reads as one continuous block
of reasoning because the advisor splices the hint back onto the front of the revision's
`reasoning_content`.

The hint is the only advisor-authored free text that can reach the solver. See
[Privileged-information isolation](../explanation/isolation.md) for what guards it.

## K2-Horizon on SGLang

```python
from ifm_advisor import AdvisorConfig, EndpointConfig, ReasoningPrefillCue

config = AdvisorConfig(
    endpoint=EndpointConfig(model="IFM/K2-Horizon-375B-A23B", api_base="http://localhost:30001/v1"),
    cue=ReasoningPrefillCue.k2_horizon_sglang(),
    solver_profile="k2_horizon",
    screen_hints=True,
)
```

The preset needs no `prefix`, because of how the server builds the prompt:

1. SGLang's `continue_final_message` removes the trailing assistant message from the request.
2. It renders the remaining messages with the generation prompt. For K2-Horizon the generation
   prompt is the assistant header followed by an open `<ifm|think>` block (`<ifm|think_fast>` or
   `<ifm|think_faster>` for medium and low reasoning effort).
3. It appends the removed text as tokens.

So the hint lands inside an open reasoning block, and the template's rule that an assistant message
needs a thinking field is not triggered, because the prefill message is gone before rendering.
`tests/test_k2_horizon_template.py` pins these mechanics against the published chat template.

Set `solver_profile="k2_horizon"` to give the advisor the profile written from measurements of
K2-Horizon's own reasoning. Turn on `screen_hints=True`: each hint is checked with the audit's
mechanical leak checks before delivery, and a hint that fails is replaced by the self-check
sentence for that action. Fallbacks are counted in `hint_screen_fallbacks` and the hits are recorded
in the trace.

An empty hint also falls back to the self-check sentence, counted in `empty_hint_fallbacks`.

## Check your server first

Before a long run, check that the endpoint continues a prefilled reasoning block:

```bash
export SOLVER_BASE_URL=http://localhost:30000/v1 SOLVER_API_KEY=...
python examples/check_reasoning_prefill.py
```

It sends one short conversation that ends in an assistant message holding the start of a thought,
with `continue_final_message=True`. It passes (exit 0, "OK") when the response has non-empty
`reasoning_content` and at least 16 reasoning tokens. Otherwise it prints "NOT OK" and exits 1: use
`SelfCheckCue` with that endpoint.

## Other models

Set `prefix` to whatever text the model's chat template needs to open a reasoning block, for example
a reasoning-open token for a template whose generation prompt does not open one:

```python
cue = ReasoningPrefillCue(prefix="<your reasoning-open token>")
```

vLLM and other servers are not verified for reasoning prefill. Use `SelfCheckCue` there.

## The continuation-suffix A/B

`continuation_suffixes` appends a rotating open-ended phrase to each hint, chosen by intervention
index so that rerunning a config reproduces the sequence. Only strings the solver itself writes
belong there. Empty is the control arm.

Why test this: a hint that ends on a committed external action ("let me run ...") stops the solver's
reasoning, because that is the model's own ritual for closing a block and handing off to a tool
call. The advisor prompt forbids such endings. The suffix tests whether also appending an open-ended
phrase helps beyond that.

Measured mid-block in vanilla K2-Horizon traces, P(the block continues past this sentence):

| sentence starts with | n | P(continue) | mean sentences after |
| --- | --- | --- | --- |
| `Let me think` | 3,819 | 1.000 | 37-39 |
| `Hmm.` | 2,945 | 1.000 | 34-36 |
| `Let me reconsider` | 350 | 1.000 | 37-43 |
| *any sentence (baseline)* | 612,364 | 0.909-0.935 | 21-27 |
| `Let me check` | 39,054 | 0.726-0.783 | 12-16 |
| `Let me run` | 3,202 | 0.494-0.531 | 3.5-4.2 |

The table is **observational**: the model chose those phrases and then continued, so it ranks
candidates but cannot show that forcing one causes the continuation. That is what the A/B measures.
Length is also not the objective: the highest-continuation phrases are the vocabulary of the
corpus's re-derivation churn mode, so read `revision_reasoning_tokens` alongside whether the next
tool call addresses what the hint pointed at.

```python
control = ReasoningPrefillCue.k2_horizon_sglang()
treatment = ReasoningPrefillCue.k2_horizon_sglang(
    continuation_suffixes=("Let me think.", "Hmm.", "Let me reconsider.")
)
```

## How to compare arms

Compare `revision_reasoning_tokens` for each advised turn across the two arms. A handful of tokens
(single digits) means the hint closed the reasoning instead of opening it.

```bash
jq -r 'select(.intervened) | [.solver_call_index, .hint_continuation_suffix,
  .revision_reasoning_tokens] | @tsv' runs/attempt-1/advisor/private_trace.jsonl
```

`private_trace.jsonl` is privileged. Run this on your own machine and do not paste its other fields.
