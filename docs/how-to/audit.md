# Audit trajectories

## When to use this

Use this before you release or train on an advised trajectory. The audit reads the public
trajectory, never modifies it, and answers whether the advised run still looks like an ordinary
solver run. The audit fails closed: anything suspicious fails it, a missing judge cannot turn a
finding into a pass, and input it cannot fully read or parse is an error (exit 2) with no verdict
at all, never a pass. That includes privileged material it cannot read and a trajectory it cannot
decode or parse.

## The five checks

1. **Coherence.** A judge reads the public task and the trajectory, and says whether it reads like
   one solver working a problem.
2. **Unsupported direction changes.** The same judge lists every change of direction that has no
   support in the public task or in terminal output the solver had already seen.
3. **Mentions of an advisor.** Mechanical regular expressions (offline, no model) and a leakage
   judge look for references to an advisor, a reviewer, hidden tests, a reference or gold solution,
   or any privileged artifact.
4. **Privileged overlap.** Mechanical verbatim matching of long privileged lines (40 characters or
   more) against the trajectory, including the decoded string values inside it, plus the leakage
   judge's view of near-verbatim overlap. The coherence judge never sees privileged material.
5. **Verification.** The verifier's own verdict, read from Harbor's `result.json` or
   `verifier/reward.txt`. It is never judged by a model. A missing file, or one whose content is
   not valid JSON or a number, gives `reward: null`; a file that exists but cannot be read is
   unusable input (exit 2).

## Run it

Without a judge, which needs no endpoint:

```bash
ifm-advisor audit --attempt-dir runs/attempt-1 --task-dir path/to/task \
  --output runs/attempt-1/advisor/audit.json --no-judge
```

With a judge:

```bash
export JUDGE_BASE_URL=http://localhost:30001/v1
export JUDGE_API_KEY=...
export JUDGE_MODEL=IFM/K2-Horizon-375B-A23B

ifm-advisor audit --attempt-dir runs/attempt-1 --task-dir path/to/task \
  --output runs/attempt-1/advisor/audit.json
```

Judge settings come from `--judge-model` and `--judge-base-url`, which default to `JUDGE_MODEL` and
`JUDGE_BASE_URL`, falling back to `ADVISOR_MODEL` and `ADVISOR_BASE_URL`. The key is read from the
variable named by `--judge-api-key-env` (default `JUDGE_API_KEY`). `--judge-provider` sets the
LiteLLM provider prefix (default `openai`).

Write the audit to `<attempt-dir>/advisor/audit.json`: that is where `summarize` looks.

## Trajectories that are not Harbor's

`--trajectory` audits any JSON file, for example a list of chat messages from your own loop, a
JSON Lines file, or a plain-text transcript. An explicit `--trajectory` path is used as given. `--attempt-dir` must still be an existing directory,
and `--task-dir` supplies the privileged material.

Without `--trajectory`, the audit looks for `agent/trajectory.json`, then
`agent/mini-swe-agent.trajectory.json`, then `trajectory.json`, then any `*trajectory*.json` beneath
the attempt directory. Harbor's files (the trajectory, `result.json` and `verifier/reward.txt`) are
read only when they are regular files, never symlinks.

## Which privileged material is checked

The audit checks the trajectory against the privileged material the advisor saw:

1. `--privileged-root PATH` (repeatable; `privileged_roots=` in `run_audit`) names the privileged
   roots, relative to `--task-dir`. It overrides everything else.
2. Otherwise, if `<attempt-dir>/advisor/task_material_manifest.json` exists and records
   `privileged_roots`, the audit uses that layout: its `privileged_roots` and
   `excluded_directory_names`, with the other `TaskLayout` fields at their defaults. Without
   `--privileged-root`, the audit refuses (exit 2) rather than guess when the layout it should use
   is unknown: an `advisor/` that is a symlink or not a directory, a manifest that is a symlink or
   not a regular file, a manifest that cannot be read or cannot be read as a layout, and an
   `advisor/` directory the auditor has no permission to read are all unusable input. Symlinks are
   never followed, and a special file is never opened.
3. Otherwise, when `advisor/` or the manifest is missing, or the manifest records no layout
   (material built with `TaskMaterial.from_texts`), the default Harbor layout: `README.md`,
   `solution` and `tests`.

`privileged_roots` in `audit.json` records the roots used, and `layout_source` where they came
from: `flag`, `manifest` or `default`. The material is loaded the way the
advisor loads it, with the same symlink rules and the default size limits (1,000,000 bytes per
file, 4,000,000 in total). If you built the advisor's material another way, such as with
`TaskMaterial.from_texts` or a custom `instruction_file`, pass the roots that hold the same files.

If a privileged file or directory cannot be read, for example for lack of permission, the audit
refuses to judge: it prints `error: cannot read privileged material in <task-dir>: <paths>` and exits
2 without writing `--output`. It cannot vouch for a trajectory against material it never saw.

If nothing decodable is loaded (a wrong `--task-dir`, a layout that matches nothing, or only binary
files), the audit adds the issue `no privileged material was loaded from the task directory`, and
the verdict can be neither `pass` nor `pass_mechanical_only`: it is `fail` if anything was found
and `inconclusive` otherwise.

## Audit every attempt in a run

```bash
for attempt in runs/advised/*/; do
  ifm-advisor audit --attempt-dir "$attempt" --task-dir path/to/task \
    --output "$attempt/advisor/audit.json" || true
done
```

For a benchmark with a different task directory per attempt, look the task directory up from the
attempt name inside the loop.

## What it checks and how the verdict is decided

- The mechanical leak checks scan the whole trajectory: the rendered JSON, followed by every
  string key and value in it, decoded (duplicate keys included). A string that itself holds a JSON
  object or array, such as a tool call's `arguments`, is decoded too, up to three layers deep. A
  copied line that contains quotes, backslashes or tabs, or that sits inside a tool call's
  arguments, is therefore matched as written.
  A JSON Lines trajectory is decoded line by line, after its raw text. A plain-text transcript, one
  that does not start with `{` or `[`, is scanned as it is, followed by the decoded strings of any
  of its lines that are JSON values.
- The trajectory is read once, as bytes. Its encoding is detected the way JSON detects it (UTF-8
  with or without a byte order mark, UTF-16 or UTF-32) and decoded strictly. A file that starts
  with `{` or `[` (after whitespace) must be one JSON document, or JSON Lines in which every
  non-blank line parses. Anything else that starts that way, such as a truncated file, two
  concatenated documents, a trailing comma or nesting too deep to parse, is unusable input (exit 2):
  scanning its raw text would miss every escaped copy. So are an empty file and one that does not
  decode. A plain-text transcript that happens to start with `[`, such as a log line beginning
  `[INFO]`, counts as JSON too: convert it, or prefix it with a line of text.
- The mechanical checks match exact text, with runs of whitespace collapsed. By design they do not
  catch a paraphrase, a privileged line shorter than 40 characters, a copy disguised with
  homoglyphs or zero-width characters, or text encoded deeper than the decoding described above
  (for example base64, or JSON nested more than three layers inside strings). The leakage judge
  exists for those.
- Only the judges receive the trajectory clipped to 200,000 characters (both ends kept);
  `judge_input_truncated` in `audit.json` says whether clipping happened.
- The mechanical overlap check uses all the privileged material that was loaded. Only the leakage
  judge's copy of it is capped, at 120,000 characters, with a note saying how much was left out;
  `privileged_input_truncated` in `audit.json` says whether that happened.
- Judge answers are validated against a schema. For the coherence judge: `coherent` (bool) and
  `unsupported_direction_changes` (a list of strings). For the leakage judge: `mentions_advisor`
  (bool), `suspicious_privileged_overlap` (bool) and `leaked_items` (a list of strings). `notes` is
  an optional string for both. An invalid answer counts as that judge failing.
- The verdict is, in order:
    1. `fail` if any mechanical check hit or any judge that completed reported a problem, even if the
       other judge failed or no judge endpoint is configured. A leakage judge that lists
       `leaked_items` while setting neither flag has reported a problem too.
    2. Otherwise `inconclusive` if no privileged material was loaded.
    3. Otherwise `pass_mechanical_only` with `--no-judge`.
    4. Otherwise `pass` if both judges completed.
    5. Otherwise `inconclusive`.

## Output and exit codes

The audit JSON is printed and written to `--output`. See [Command line](../reference/cli.md) for
every key.

| Exit code | Meaning |
|---|---|
| 0 | `pass` or `pass_mechanical_only` |
| 1 | `fail` or `inconclusive` |
| 2 | Unusable input: a missing directory, no trajectory found, an invalid judge endpoint, an unusable privileged layout (a bad `--privileged-root`, or a manifest that cannot be read as a layout), an unreadable `advisor/` directory or manifest, an `advisor/` or manifest that is a symlink or of the wrong type (without `--privileged-root`), a privileged file or directory that cannot be read, a trajectory that is empty, does not decode, or starts like JSON but is neither one JSON document nor JSON Lines, a `result.json` or `verifier/reward.txt` that exists but cannot be read, an `--output` that cannot be written (a directory, or no permission), or an `--output` that would overwrite the audited trajectory |

`--output` may not resolve to the audited trajectory. The command prints an error and exits 2
without writing.

!!! warning "Treat `audit.json` as private"
    `mechanical.verbatim_privileged_lines` quotes up to the first 60 characters of each matched
    privileged line. The command writes `audit.json` readable by its owner only (mode 0600). Do not
    publish it alongside the trajectory.
