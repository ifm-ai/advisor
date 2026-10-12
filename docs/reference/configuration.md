# Configuration

All settings are frozen pydantic models. Unknown fields are rejected, so a misspelled option raises a
validation error instead of being ignored. Import them from `ifm_advisor`.

## `EndpointConfig`

An OpenAI-compatible endpoint serving the advisor, or the audit judge. Only the *name* of the API-key
variable is stored; the key is read from the environment when a call is made and is never written
anywhere.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | `str` | required | Model name sent to the endpoint, for example `IFM/K2-Horizon-375B-A23B` |
| `provider` | `str` | `"openai"` | LiteLLM provider prefix. The request model is `provider/model`, so a Hugging Face ID is never mistaken for a provider. `""` sends `model` unchanged |
| `api_base` | `str` | required | Endpoint URL; must start with `http://` or `https://` |
| `api_key_env` | `str` | `"ADVISOR_API_KEY"` | Name of the environment variable holding the key. Must be a valid variable name |
| `extra_body` | `dict[str, object]` | `{}` | Extra fields merged into the request body |
| `extra_headers` | `dict[str, str]` | `{}` | Extra HTTP headers |
| `timeout_sec` | `float` | `300.0` | Bound on one call. Must be positive and finite. The advisor request carries the whole privileged payload, so size this above the slowest review you will wait for: with `fail_open`, a timeout lets the unreviewed draft through |

## `AdvisorConfig`

How the advisor reviews one attempt. `B` and `k` refer to the [method](../explanation/method.md).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `endpoint` | `EndpointConfig` | required | The advisor model's endpoint |
| `max_interventions` | `int` (positive) | `2` | `B`: interventions per attempt. Reviews stop once it is spent |
| `periodic_review_interval` | `int` (positive) or `None` | `None` | `k`: also review every k solver turns. `None` reviews proposed finishes only |
| `max_advisor_attempts` | `int` (positive) | `2` | Total attempts, including the first, to get a well-formed decision before `fail_open` applies |
| `max_revision_attempts` | `int` (positive) | `2` | Total attempts to get a revision that does not itself call the finish tool |
| `cue` | `SelfCheckCue` or `ReasoningPrefillCue` | `SelfCheckCue()` | How an intervention reaches the solver. Discriminated by `kind` |
| `screen_hints` | `bool` | `False` | Reasoning-prefill only: screen each hint with the audit's mechanical checks, and fall back to the self-check sentence on a hit |
| `solver_profile` | `str` or `Path` | `"generic"` | A packaged profile name (`"generic"`, `"k2_horizon"`) or a `Path` to your own profile text |
| `prompt_version` | `str` | `"advisor_v1"` | Which packaged advisor prompt to use |
| `repeat_streak_escalation` | `bool` | `False` | Show the advisor that the same `reason_code` recurred. Enable only together with `screen_hints` |
| `fail_open` | `bool` | `True` | On an advisor or revision failure, keep the solver's draft instead of raising |
| `max_conversation_messages` | `int` (at least 3) | `50` | Public history shown to the advisor: the first two messages plus the most recent tail |
| `max_message_chars` | `int` (positive) | `8000` | Each message is clipped to this many characters |
| `max_previous_cues` | `int` (positive) | `8` | Delivered cues shown back to the advisor, newest last |

## `SelfCheckCue`

Delivers each intervention as one fixed, task-agnostic user message chosen by the advisor's action.
Works with any solver model and server; the advisor's free-text hint is never used. It has no fields
beyond its `kind` discriminator.

## `ReasoningPrefillCue`

Delivers each intervention as the opening of the solver's next reasoning block: the revision request
ends with an assistant message holding `prefix` plus the hint, with `continue_final_message` set.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `prefix` | `str` | `""` | Text placed before the hint, for example a reasoning-open token for a template whose generation prompt does not open a reasoning block |
| `continuation_suffixes` | `tuple[str, ...]` | `()` | Appended to the hint after a space, rotated by intervention index. Only strings the solver itself writes belong here. Empty is the control arm |

`ReasoningPrefillCue.k2_horizon_sglang(continuation_suffixes=())` is the preset for K2-Horizon served
by SGLang. It needs no prefix, because the template's generation prompt already opens the think
block. See [Enable reasoning prefill](../how-to/reasoning-prefill.md).

## `TaskLayout`

Where a task directory keeps its public instruction and privileged material. The defaults match
Harbor and Terminal-Bench tasks.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `instruction_file` | `str` | `"instruction.md"` | The public instruction |
| `privileged_roots` | `tuple[str, ...]` | `("README.md", "solution", "tests")` | Files or directories the advisor may read, in the order they are read |
| `excluded_dirs` | `frozenset[str]` | `cheat`, `__pycache__`, `.git`, `.pytest_cache` | Directory names skipped anywhere beneath a root |
| `excluded_suffixes` | `frozenset[str]` | `.pyc`, `.pyo`, `.so`, `.o`, `.a` | File suffixes skipped |

`instruction_file` and every `privileged_roots` entry must be a relative path that stays inside the
task directory. An empty entry, an absolute path, or a path containing `..` raises `ValueError`
("layout paths must be relative and stay inside the task directory").

## Size limits of `TaskMaterial`

Keyword arguments of `TaskMaterial.from_task_dir` and `TaskMaterial.from_texts`.

| Argument | Default | Meaning |
|---|---|---|
| `max_file_bytes` | `1_000_000` | Per-file content limit. A larger file is truncated, and the truncation is recorded |
| `max_total_bytes` | `4_000_000` | Total content limit. Files after the budget is spent are omitted and listed. Must not be below `max_file_bytes` |

## `Advisor` constructor

| Argument | Meaning |
|---|---|
| `config`, `task` | The `AdvisorConfig` and `TaskMaterial` |
| `finish_tool` | Required. The name of the solver's finish tool |
| `trace_dir` | Where to write artifacts. `None` writes nothing |
| `complete` | Replaces the advisor backend with any `async (messages) -> str` |
| `solver_model`, `attempt_id` | Labels recorded in the trace and summary |

With the default backend, the constructor raises `AdvisorRequestError` when the variable named by
`api_key_env` is not exported.

## Removed fields

The research prototype's settings were renamed. The old names are rejected with a validation error:

- `mode` is replaced by `cue`, which takes `SelfCheckCue()` or `ReasoningPrefillCue(...)`.
- `reasoning_open_token` is replaced by `ReasoningPrefillCue.prefix`.
- `hint_continuation_suffixes` is replaced by `ReasoningPrefillCue.continuation_suffixes`.
- `max_private_file_bytes` and `max_private_total_bytes` (the `max_private_*` fields) are replaced
  by the `max_file_bytes` and `max_total_bytes` arguments of `TaskMaterial.from_task_dir`.
