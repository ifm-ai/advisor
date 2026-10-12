# Writing cues

A cue is the only thing the advisor adds to the solver's context. On the self-check path it is a
fixed sentence, so there is nothing to write. This page is about the reasoning-prefill path, where
the cue is the advisor's `hint`, spliced in as the opening of the solver's next reasoning block, and
the quality of that hint decides whether the intervention works.

## Keeping the solver reasoning after a hint

A spliced hint opens the solver's next reasoning block, and the solver may keep reasoning or stop
dead. It stops when the hint ends on a *committed external action*: "let me run ...", "let me
check ...". That is the model's own ritual for closing a block and handing off to a tool call, so
the solver emits the command with no reasoning behind it.

Measured mid-block in vanilla K2-Horizon traces, P(the block continues past this sentence):

| sentence starts with | n | P(continue) | mean sentences after |
| --- | --- | --- | --- |
| `Let me think` | 3,819 | 1.000 | 37-39 |
| `Hmm.` | 2,945 | 1.000 | 34-36 |
| `Let me reconsider` | 350 | 1.000 | 37-43 |
| *any sentence (baseline)* | 612,364 | 0.909-0.935 | 21-27 |
| `Let me check` | 39,054 | 0.726-0.783 | 12-16 |
| `Let me run` | 3,202 | 0.494-0.531 | 3.5-4.2 |

The advisor prompt forbids ending a hint on a committed action for this reason. It asks instead for
an ending on something unresolved: an open question, a two-sided tension, a named gap in the
evidence. The one exception is `CONFIRM_AND_SUBMIT`, where the tool call is the behavior wanted and
an open ending would buy the solver another turn to talk itself out of working code. The
`continuation_suffixes` option tests whether also appending an open-ended phrase helps beyond that;
see [Enable reasoning prefill](../how-to/reasoning-prefill.md).

Two caveats. The table is **observational**: the model chose those phrases and then continued, so it
ranks candidates but cannot show that forcing one *causes* the continuation. That is exactly what the
A/B measures. And length is not the objective: the highest-continuation phrases are also the
vocabulary of the corpus's re-derivation churn mode, so read `revision_reasoning_tokens` alongside
whether the next tool call actually addresses what the hint pointed at.

Two fields in the private trace measure whether a hint worked as a reasoning prefill rather than a
stop sign:

| Field | Meaning |
| --- | --- |
| `revision_reasoning_tokens` | Tokens the solver generated *after* the hint. A handful (single digits) means the hint closed the reasoning instead of opening it, usually because it ended on a committed action or a finished conclusion. |
| `hint_persisted_to_trajectory` | Whether the hint was spliced back onto the front of the revision's `reasoning_content` (`null` on the self-check path, where no hint is delivered). |

`continue_final_message` makes the endpoint return only the tokens generated after the prefill, so
the hint is re-attached to the front of the recorded `reasoning_content`. Without that the stored
trajectory would show a reasoning block that begins mid-thought.

## Why a hint stops working when it repeats

The solver's reasoning is replayed to it on later turns, so it can see arguments it has already
worked through. On two full runs, how much *new* vocabulary a hint carried tracked how far the solver
reasoned afterwards:

| | run 1 (n=33) | run 2 (n=42) |
| --- | --- | --- |
| r(novelty, reasoning tokens) | +0.46 | +0.43 |
| r(turn position, reasoning tokens) | -0.19 | -0.20 |
| **partial r(novelty \| turn)** | **+0.45** | **+0.42** |

Treat that as suggestive, not established. A later pair of runs on *identical* code and config
landed on opposite sides of the same measurement: one with a median of 3 reasoning tokens per
intervention and 4 of 5 interventions collapsing, the other a median of 93 with none. So the
run-to-run spread on a single rollout is wider than the effect. What has reproduced is the input
side: feeding the history back keeps hint novelty from decaying (a floor of about 0.45 across a run,
against about 0.08 when each review is blind to the last).

So each review receives a section listing the hints already delivered this attempt (the last
`max_previous_cues`, newest last), and the prompt requires that a repeat intervention change the
angle: a different failing scenario, a trace never run, an unnamed consequence.

That rule governs *what the advisor says*, never *whether it blocks*. See
[Design decisions](design-decisions.md).

## Solver profiles

The advisor prompt has two parts: a versioned core, and a **solver profile** that is inserted at the
`{{SOLVER_PROFILE}}` marker. The profile is the one section that describes how a particular solver
writes, because a hint only works if it reads as the solver's own thought. Text in a different
register reads as foreign, and foreign text is either ignored or treated as an outside instruction.

Two profiles ship:

- `generic` (the default) says to match the solver's register as it appears in the public
  conversation, and makes no claim about any particular model.
- `k2_horizon` carries measurements of K2-Horizon's own reasoning, taken over about 1.26 million of
  its reasoning blocks: the median sentence is short (about 45-65 characters), identifiers go in
  backticks (about one sentence in four), and self-instruction ("I should / I need to / I must") is
  under 1% of its sentences, where earlier hints ran 45%. It lists the self-interruptions the model
  uses verbatim, and example mid-block sentences. It is the profile used for the paper's runs.

To write a profile for another solver:

1. Collect the solver's own reasoning blocks from ordinary, unadvised runs.
2. Measure the register: sentence length, how identifiers are formatted, how often it instructs
   itself, which self-interruptions it uses, and how it opens a thought it will keep developing.
3. Write those measurements and a few verbatim sentences as a short profile text file.
4. Pass its path: `AdvisorConfig(solver_profile=Path("my_profile.txt"))`.
5. Check the resulting `reasoning_content` and `revision_reasoning_tokens` on a few interventions
   before a long run.

Every profile must forbid second person and any mention of a review. The shipped profiles contain the
sentence `No second person, no "the user", no reference to a review, an advisor, a reviewer, or any
external process.`, and a test requires it of every packaged profile. A profile you write yourself is
not checked, so keep that line.
