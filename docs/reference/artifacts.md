# Artifacts

The advisor writes three files to its `trace_dir`; `ifm-advisor audit` adds a fourth. Writes
are best effort: a failure is logged and never fails the attempt. `summary.json` and the manifest are
replaced atomically; `private_trace.jsonl` is append-only, one JSON object per line. All four are
created readable and writable by their owner only (mode 0600), whatever the umask.

| File | Written by | Publishable |
|---|---|---|
| `private_trace.jsonl` | the advisor, one record per review | **No.** Derived from privileged material |
| `summary.json` | the advisor | Yes |
| `task_material_manifest.json` | the advisor, once at creation | Only if those paths are already public |
| `audit.json` | `ifm-advisor audit` | No. It may quote privileged lines |

## `private_trace.jsonl`

A record is written for every review, including reviews that kept the draft and reviews that failed.

| Key | Meaning |
|---|---|
| `timestamp` | Unix time the review started |
| `attempt_id` | The `attempt_id` given to the `Advisor` |
| `task` | Task name |
| `solver_call_index` | The solver turn `t` this review belongs to (1-based) |
| `trigger` | `pre_submit` (a proposed finish) or `periodic` |
| `draft_attempted_finish` | Whether the draft called the finish tool |
| `solver_model`, `advisor_model` | Model names |
| `cue_kind` | `self_check` or `reasoning_prefill` |
| `advisor_latency_sec` | Time spent obtaining the decision |
| `advisor_attempts` | Attempts used to get a well-formed decision (the maximum when it failed) |
| `decision` | The full decision (all seven fields, `plan`, `hint` and `private_diagnosis` included), or `null` when the review failed |
| `intervened` | Whether a revision was committed |
| `cue_fallback` | `empty_hint` or `hint_screen` when the reasoning-prefill cue was replaced by the self-check sentence; otherwise `null` |
| `hint_screen_hits` | Why `screen_hints` rejected the hint, as a list; empty otherwise |
| `delivered_cue` | The exact text the solver received, or `null` |
| `revision_latency_sec` | Time spent on the revision, or `null` |
| `revision_attempts` | Revision requests made |
| `revision_attempted_finish` | Whether the committed revision still called the finish tool |
| `revision_reasoning_tokens` | Tokens the solver generated after the cue, when the server reports them |
| `hint_persisted_to_trajectory` | Whether the hint was spliced back onto the front of the revision's `reasoning_content`; `null` on the self-check path |
| `hint_continuation_suffix` | The rotated suffix used, or `""` |
| `privileged_files` | Number of privileged files loaded |
| `privileged_truncated_paths`, `privileged_omitted_paths` | Files truncated or omitted by the size limits |
| `error` | `ExceptionType: message` for a failed review, or `revision failed: ...`; otherwise `null` |

## `summary.json`

Counters and settings only. It never contains a plan, a hint or a diagnosis. The `Advisor` writes it
at creation and after every review; `litellm_hook.advised(...)` writes it again when the block ends.

| Key | Meaning |
|---|---|
| `enabled` | Always `true`. Its presence marks an attempt as advised |
| `attempt_id`, `task`, `solver_model`, `advisor_model` | Labels |
| `prompt_version` | Advisor prompt used, for example `advisor_v1` |
| `solver_profile` | A packaged profile name, or the file name of a profile you supplied |
| `cue` | `self_check` or `reasoning_prefill` |
| `solver_calls` | Solver turns seen by `review` |
| `finish_reviews` | Reviews triggered by a proposed finish |
| `periodic_reviews` | Reviews triggered by the interval |
| `interventions` | Revisions committed |
| `advisor_failures` | Reviews or revisions that failed |
| `revision_calls` | Revisions made |
| `hint_screen_fallbacks` | Hints replaced because `screen_hints` rejected them |
| `empty_hint_fallbacks` | Empty hints replaced by the self-check sentence |

## `task_material_manifest.json`

Paths, sizes and hashes only. It never holds file contents or the task's absolute path, but it does
list privileged file paths, sizes and hashes: hidden-test file names can reveal what is tested, and
the hash of a small reference file confirms a guessed copy. Share it only when those paths are
already public, such as a public benchmark's layout.

| Key | Meaning |
|---|---|
| `task` | Task name (the directory name for `from_task_dir`) |
| `privileged_roots` | The layout's roots, or `null` for `from_texts` |
| `excluded_directory_names` | The layout's excluded directory names, or `null` |
| `file_count` | Number of entries in `files` |
| `total_content_bytes` | Content bytes sent to the advisor |
| `truncated_paths`, `omitted_paths` | Files cut short, and files left out when the total budget ran out |
| `files` | One entry per file: `relative_path`, `size_bytes`, `sha256`, `text` (whether its text is available to the advisor), `truncated`, `skipped_reason` |

`skipped_reason` is `null` for a file that was read, otherwise `"symlink"`, `"not a regular file"`,
`"not valid utf-8"`, or `"unreadable: <ExceptionType>"`. An entry can also be a directory: one that
is skipped as a symlink, or one that could not be listed (`"unreadable: <ExceptionType>"`), whose
contents are then unknown. See [Use your own task material](../how-to/task-material.md).

## `audit.json`

See [Command line](cli.md#auditjson) for every key.
