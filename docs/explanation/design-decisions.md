# Design decisions

Each section below is a choice the package makes and the reason for it. Where a case is cited, it is
a measured one.

## No confidence threshold

The decision carries a `confidence` number, but nothing is gated on it. The advisor's `intervene`
field, together with an action other than `CONTINUE`, is the whole gate. The advisor is told so: it
is the only judge of whether an intervention is warranted, and it should weigh that itself rather
than rely on something downstream to filter it.

## Withholding the finish tool on a revision

When the advisor intervenes, the finish tool is removed from the revision request unless the action
is `CONFIRM_AND_SUBMIT`. Every other action means "more work first", so the solver must take one real
action before it can try to finish again. Removing the tool is not a hard guarantee, because some
tool-call parsers (K2-Horizon's included) do not check a parsed function name against the tools
offered on the request. So a revision that calls the finish tool anyway is resampled, up to
`max_revision_attempts` times.

## CONFIRM_AND_SUBMIT

Every other action gates a finish the solver already attempted. A periodic review can find the work
correct at a moment when the solver is not trying to finish, and `CONTINUE` there merely declines to
interrupt. The measured case: a solver reached a tree matching the reference with its own full
battery of checks passing, reverted it four turns later, and went on thrashing until it ran out of
context and failed. Correctness and finish attempts never coincided, so nothing converted the one
into the other. `CONFIRM_AND_SUBMIT` is the one action that pushes toward finishing, the one whose
hint deliberately ends on a committed action, and the one whose revision keeps the finish tool.

## The plan may not carry a verdict

The plan is replayed to the advisor at every later review. A standing decision written into it, such
as "if the next review still shows X, let it submit", reads back as already settled and gets executed
instead of re-judged. The measured case: an early plan said "then let it submit". Four reviews later
the advisor allowed a submission while its own plan for that review still spelled out, with exact
parameters, the two tests it knew would fail. It had stopped judging and started executing a
promise. The prompt therefore says the plan records task state, never a future verdict.

## Repetition rules govern wording, never whether to block

Restating a hint is weaker than changing the angle, so the prompt asks the advisor to vary what it
says when it blocks the same gap again. That rule is about wording only. On a `pre_submit` review the
advisor must intervene whenever it believes the implementation would fail, including when its only
remaining argument is one it has already made: a submission it allows is final, while a repeated hint
costs one turn, and the intervention budget is what ends a standoff. Only a `periodic` review may
stand down for lack of a new angle. An earlier version inverted this and told the advisor to allow the
submission once it had nothing new to say. A run duly blocked five times and then waved through a
submission its own diagnosis said would score 0.

## `repeat_streak_escalation` is off by default

When the same `reason_code` recurs across consecutive interventions, the advisor can be shown that
streak and asked to escalate. Escalation pushes the advisor toward answer-shaped hints, whose solved
trajectories record decisions that were handed over rather than derived, which is worse than an
unsolved one. It is off by default. If you enable it, enable `screen_hints` with it.

## `fail_open` defaults to True

If the advisor times out, errors, or returns a malformed decision after `max_advisor_attempts`, or a
revision fails, the solver's draft is kept and the attempt goes on. A rollout farm should not lose
attempts to an advisor outage. The cost is that an advised attempt can silently run as a baseline.
Detect it with `ifm-advisor summarize`: `effectively_unadvised` counts advised attempts whose
advisor failed and never intervened, and `advised_without_reviews` counts advised attempts that were
never reviewed at all. Set `fail_open=False` to raise instead.

## `screen_hints` is off by default

The paper's runs did not screen hints. The default matches them, so a result produced with the
defaults is comparable. For a new run on the reasoning-prefill path, turn it on: it costs nothing
when the hint is clean, and replaces a leaking hint with the fixed self-check sentence.
