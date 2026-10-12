# Command line

`ifm-advisor` has two subcommands, `audit` and `summarize`, and `--version`. The help text
below is the program's own output. `python -m ifm_advisor` runs the same command.

## `audit`

Audit one advised attempt: offline leak checks, a public-coherence judge, a privileged-leakage judge,
and the verifier's verdict. See [Audit trajectories](../how-to/audit.md) for how to use it.

```text
usage: ifm-advisor audit [-h] --attempt-dir ATTEMPT_DIR --task-dir TASK_DIR
                         --output OUTPUT [--trajectory TRAJECTORY]
                         [--privileged-root PATH] [--judge-model JUDGE_MODEL]
                         [--judge-base-url JUDGE_BASE_URL]
                         [--judge-provider JUDGE_PROVIDER]
                         [--judge-api-key-env JUDGE_API_KEY_ENV]
                         [--judge-timeout-sec JUDGE_TIMEOUT_SEC] [--no-judge]

Audit one advised attempt: offline leak checks, a public-coherence judge, a
privileged-leakage judge, and the verifier's verdict. Exit 0 on a pass, 1 on a
fail or an inconclusive audit, 2 on unusable input.

options:
  -h, --help            show this help message and exit
  --attempt-dir ATTEMPT_DIR
                        Harbor attempt directory
  --task-dir TASK_DIR   task directory holding the privileged material
  --output OUTPUT       where to write the audit JSON
  --trajectory TRAJECTORY
                        audit this file (any JSON, e.g. a list of chat
                        messages) instead of the attempt's
  --privileged-root PATH
                        a privileged root relative to the task directory;
                        repeat for each root. Overrides the layout in the
                        attempt's advisor/task_material_manifest.json
                        (default: that layout, else README.md, solution and
                        tests)
  --judge-model JUDGE_MODEL
                        judge model (default: $JUDGE_MODEL, then
                        $ADVISOR_MODEL)
  --judge-base-url JUDGE_BASE_URL
                        judge endpoint (default: $JUDGE_BASE_URL, then
                        $ADVISOR_BASE_URL)
  --judge-provider JUDGE_PROVIDER
                        LiteLLM provider prefix (default: openai)
  --judge-api-key-env JUDGE_API_KEY_ENV
                        environment variable holding the judge key (default:
                        JUDGE_API_KEY)
  --judge-timeout-sec JUDGE_TIMEOUT_SEC
  --no-judge            run only the offline checks
```

`--attempt-dir`, `--task-dir` and `--output` are required. Judge options fall back to the
environment: `JUDGE_MODEL`, then `ADVISOR_MODEL`; `JUDGE_BASE_URL`, then `ADVISOR_BASE_URL`; and the
key variable named by `--judge-api-key-env` (default `JUDGE_API_KEY`).

The privileged roots come from `--privileged-root` when given, otherwise from the advisor's
`advisor/task_material_manifest.json` in the attempt directory, otherwise from the default layout
(`README.md`, `solution`, `tests`). Without `--privileged-root`, a symlinked `advisor/` or manifest,
or one of the wrong type, is an error (exit 2), not a reason to fall back to the default. See
[which privileged material is checked](../how-to/audit.md#which-privileged-material-is-checked).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | `pass` or `pass_mechanical_only` |
| 1 | `fail` or `inconclusive` |
| 2 | Unusable input: a missing directory, no trajectory found, an invalid judge endpoint, an unusable privileged layout (a `--privileged-root` that is absolute or contains `..`, or a task-material manifest that cannot be read as a layout), an attempt file, `advisor/` directory or manifest that cannot be read, an `advisor/` that is a symlink or not a directory or a manifest that is a symlink or not a regular file (without `--privileged-root`), a privileged file or directory that cannot be read, a trajectory that is empty, does not decode, or starts with `{` or `[` but is neither one JSON document nor JSON Lines, a `result.json` or `verifier/reward.txt` that exists but cannot be read, an `--output` that cannot be written (a directory, or no permission), or `--output` resolving to the audited trajectory (nothing is written) |

### `audit.json`

The audit is printed and written to `--output`, readable by its owner only (mode 0600). It is
private: `mechanical.verbatim_privileged_lines` quotes up to the first 60 characters of each matched
privileged line.

| Key | Meaning |
|---|---|
| `attempt_dir`, `task_dir`, `trajectory_path` | Resolved paths audited |
| `privileged_roots` | The privileged roots the material was loaded from |
| `layout_source` | Where those roots came from: `flag` (`--privileged-root`), `manifest` (the advisor's task-material manifest) or `default` |
| `verdict` | `fail`, `pass_mechanical_only`, `pass` or `inconclusive` |
| `issues` | Human-readable list of every finding and every judge failure, and `no privileged material was loaded from the task directory` when nothing was loaded |
| `mechanical.leak_pattern_hits` | Leak terms found, such as `advisor` or `hidden test` |
| `mechanical.verbatim_privileged_lines` | Privileged lines of 40 or more characters found verbatim, each quoted to 60 characters, up to 20 |
| `judge_input_truncated` | Whether the judges received clipped text. The mechanical checks always scan the whole trajectory |
| `privileged_input_truncated` | Whether the privileged material shown to the leakage judge was capped at 120,000 characters. The mechanical checks always use the whole loaded material |
| `judge_model` | The judge model, or `null` when no judge ran |
| `coherent` | The coherence judge's answer, or `null` |
| `unsupported_direction_changes` | Direction changes the coherence judge found unsupported (present when it completed) |
| `coherence_notes` | The coherence judge's notes (present when it completed) |
| `mentions_advisor` | A mechanical hit or the leakage judge says the trajectory refers to an advisor or privileged material |
| `direct_leakage` | Same as `mentions_advisor` |
| `suspicious_privileged_overlap` | A verbatim line was found, or the leakage judge reports overlap |
| `leaked_items` | The leakage judge's descriptions of overlaps (present when it completed). A non-empty list fails the audit even when the judge set neither flag |
| `leakage_notes` | The leakage judge's notes (present when it completed) |
| `verification` | `result_json_present`, `reward` and `passed`, read from the verifier's own files. A missing file, or one whose content is not valid JSON or a number, gives `reward: null`; a file that exists but cannot be read is an error (exit 2) |

A flag the audit could not determine is `null`.

## `summarize`

Aggregate attempt directories into per-arm pass rates and advisor statistics. See
[Summarize runs and report results](../how-to/summarize-and-report.md).

```text
usage: ifm-advisor summarize [-h] --arm NAME=GLOB [--json] [--csv CSV]
                             [--interventions-csv INTERVENTIONS_CSV]
                             [--benchmark BENCHMARK] [--solver SOLVER]
                             [--advisor ADVISOR]

Aggregate attempt directories into per-arm pass rates and advisor statistics.
Reads verifier output, advisor/summary.json, advisor/audit.json and the
trigger and action fields of the private trace; never hints, plans or
diagnoses.

options:
  -h, --help            show this help message and exit
  --arm NAME=GLOB       an arm and a glob of its attempt directories; repeat
                        for each arm or glob
  --json                print JSON instead of a table
  --csv CSV             write pass-rate rows (results/pass_rates.csv format)
  --interventions-csv INTERVENTIONS_CSV
                        write intervention rows (results/interventions.csv
                        format)
  --benchmark BENCHMARK
                        benchmark name for the CSV rows
  --solver SOLVER       solver model name for the CSV rows
  --advisor ADVISOR     advisor model name for the CSV rows
```

`summarize` exits 0, or 2 when an `--arm` is not of the form `NAME=GLOB` or an attempt file
exists but cannot be read (for example a `result.json` without read permission). An arm that matches no
directories prints a warning and is reported with zero attempts.
