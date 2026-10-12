# Prompts and versioning

The advisor's prompts ship inside the package, so an installed wheel can read them.

## Files

| File | Contents |
|---|---|
| `prompts/advisor_v1.txt` | The core system prompt, version `advisor_v1`: the advisor's job, how to judge, the `plan` and `hint` rules, the action semantics and the output format |
| `prompts/solver_profiles/generic.txt` | The default solver profile: match the solver's register as it appears in the conversation |
| `prompts/solver_profiles/k2_horizon.txt` | The K2-Horizon profile, written from measurements of its own reasoning |

## The `{{SOLVER_PROFILE}}` marker

The core prompt contains the marker `{{SOLVER_PROFILE}}` on its own line. When the system prompt is
built, the chosen profile replaces it. `AdvisorConfig.solver_profile` selects a packaged profile by
name, or takes a `Path` to your own file. A core prompt without the marker raises
`AdvisorRequestError`. See [Writing cues](../explanation/writing-cues.md#solver-profiles).

## Versioning policy

A released prompt or profile file is never edited. A change adds a new version file, selected with
`AdvisorConfig.prompt_version`, because a changed prompt changes advisor behavior and must be
traceable in results. The version used is recorded in `summary.json` as `prompt_version`.

## The paper's prompt

`tests/fixtures/pre_submit_v1.txt` is the prompt used for the paper's runs. The test
`test_k2_horizon_prompt_is_the_paper_prompt_byte_for_byte` builds the system prompt from
`advisor_v1` with the `k2_horizon` profile and checks that it equals that fixture exactly, apart
from two phrases in which the model is named K2-Horizon. The refactor into a core plus a profile
therefore cannot drift from the published behavior.

The advisor's request itself (the user message) is built from labeled sections: the review trigger,
the public task, the public conversation, the solver's latest turn, the advisor's previous plan, the
hints already delivered, the privileged reference material and the required JSON schema.
