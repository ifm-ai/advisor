# Changelog

## 0.1.0 (unreleased)

First standalone release of the privileged advisor.

- `Advisor`: review gate (proposed finishes, and every k turns), typed decisions, and solver
  revision through a `regenerate` callback, for any agent loop.
- `litellm_hook`: run an agent that calls `litellm.acompletion` in your own process through the
  advisor without changing it. For Harbor that is the `mini-swe-agent-external-finish` agent, which
  runs LiteLLM on the host. Harbor's stock installed `mini-swe-agent` runs inside the task
  container, where the hook cannot see its calls, and Terminus-2 has no finish tool, so only
  periodic reviews could apply; neither is a supported target.
- Cues: `SelfCheckCue` (default) and `ReasoningPrefillCue`, with a K2-Horizon/SGLang preset.
- Solver profiles: `generic` (default) and `k2_horizon`, the paper's prompt.
- `TaskMaterial`: Harbor/Terminal-Bench task directories, custom layouts, or in-memory material.
- Type information: the package ships `py.typed` and passes `mypy --strict`.
- `screen_hints`: an optional inline leak screen for reasoning-prefill hints.
- `ifm-advisor audit` and `ifm-advisor summarize`.
- Runtime dependencies are bounded below the next major versions: `pydantic<3`, `litellm<2`.

Changes from the research prototype used for the paper's runs:

- The advisor is told the cue the solver actually received; on the self-check path that is the
  fixed sentence, not a hint the solver never saw.
- A missing advisor API key fails when the `Advisor` is created instead of failing open on every
  review.
- Model IDs containing `/` are sent with the provider prefix.
- Harbor's `verifier_result.rewards` is read before the `verifier/reward.txt` fallback.
- A privileged file truncated inside a multi-byte character keeps its text.
- A missing task directory raises instead of producing an advisor with no privileged material.
- An empty hint on the reasoning-prefill path falls back to the self-check sentence.
- Task material is read only from regular files reached without symlinks, including every
  directory along a nested layout path; symlinks and special files are recorded as skipped. Each
  file's type is checked on the open descriptor, so a FIFO or device at a task path, even one
  swapped in after the check, never blocks loading. Symlinks are checked statically, before
  reading: a symlink swapped into an intermediate directory during a load is a known limitation.
- Whitespace-only privileged material counts as no material: the advisor warns, and the audit
  cannot pass.
- A truncated privileged file is kept as text only when the cut falls inside a multi-byte
  character, not when the bytes before the cut are invalid UTF-8.
- A layout entry of `.` (the task directory itself) is rejected, and a task loaded as `.` is named
  after its directory.
- The audit's mechanical checks scan the whole trajectory instead of the text clipped for the
  judges, including every string value decoded, so a copied line with quotes, backslashes or tabs,
  or one inside a tool call's JSON-encoded arguments, is found.
- The audit checks the privileged material the advisor saw: the layout from the advisor's
  task-material manifest (or `--privileged-root`) and the loader's default size limits, instead of
  the default layout capped at 120,000 bytes. Only the leakage judge's copy is capped.
- An audit with no privileged material to check against is `inconclusive` (or `fail` on a finding);
  it used to pass.
- A mechanical hit without a judge is now `fail`; it was `inconclusive`. Any finding fails, even when
  a judge failed.
- Judge answers are validated against a schema; an invalid answer counts as a judge failure.
- The audit reads Harbor's files (the trajectory, `result.json`, `verifier/reward.txt`) only when
  they are regular files, never symlinks, and `--output` may not overwrite the audited trajectory.
- `summarize` counts `pass_mechanical_only` audits separately from `pass`, and reports advised
  attempts that were never reviewed (`advised_without_reviews`).
- The advisor warns when the solver is not offered its finish tool or it has no privileged
  material, and `litellm_hook.advised` warns when no call was seen, or when calls were seen but none reached the advisor (no `messages` argument, or a failed draft call).
- A call LiteLLM makes from inside a hooked call (its fallbacks) is no longer reviewed as a second
  turn.
- A rejected advisor decision raises an error that names the failing fields without quoting the
  advisor's output, never names an unknown field (its name is the advisor's text), and carries no
  chained exception.
- The audit also decodes dict keys and JSON Lines trajectories, and an unreadable `advisor/`
  directory or manifest is an `error:` (exit 2) instead of a traceback or a silent fallback to the
  default layout.
- `task_material_manifest.json` is not treated as publishable by default: it lists privileged file
  paths, sizes and hashes.
- A privileged file or directory that cannot be read is recorded in the manifest as skipped
  `unreadable: <ExceptionType>` instead of being dropped (a directory that could not be listed used
  to vanish without a trace), and the audit refuses such material with exit 2 instead of passing
  without it.
- The audit reads the trajectory once, detects UTF-16 and UTF-32 as well as UTF-8, and decodes it
  strictly. A trajectory that is empty, does not decode, or starts like JSON but is neither one
  JSON document nor JSON Lines (truncated, concatenated, a trailing comma, nested too deeply) is
  unusable input (exit 2); it used to be scanned as raw text, which missed every escaped copy, and
  could pass.
- Without `--privileged-root`, an `advisor/` that is a symlink or not a directory, or a
  task-material manifest that is a symlink or not a regular file, is unusable input (exit 2). Such
  a manifest used to be ignored in favor of the default layout, which could pass a copy of material
  the advisor saw. `audit.json` records `layout_source` (`flag`, `manifest` or `default`).
- A `result.json` or `verifier/reward.txt` that exists but cannot be read is unusable input
  (exit 2) instead of a silent `reward: null`. A missing file, or invalid content, still gives no
  reward.
- `private_trace.jsonl` and `audit.json` are created readable by their owner only (mode 0600), like
  `summary.json` and the manifest; they used to follow the umask, usually 0644. An existing trace is
  tightened to 0600 on the next append, the trace is never appended through a symlink or into a
  FIFO, and an advisor directory the writer creates is 0700.
- A trajectory without a byte order mark that decodes as UTF-16 or UTF-32 must be JSON; that
  encoding is only guessed from NUL bytes, so UTF-8 text with a NUL near its start is refused
  (exit 2) instead of being misread.
- `summarize` stops with `error:` and exit 2 on an attempt file that exists but cannot be read,
  instead of a traceback.
- A leakage judge that lists `leaked_items` while setting neither flag fails the audit; it used to
  pass.
