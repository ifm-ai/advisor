# Summarize runs and report results

## When to use this

Use this when you have attempt directories for two or more arms (for example a baseline and an
advised arm) and want pass rates with confidence intervals, intervention statistics, and the CSVs
behind a figure.

## Arms as globs

Each `--arm NAME=GLOB` names an arm and the glob of its attempt directories. Repeat `--arm` for each
arm; repeating a name adds another glob to the same arm.

```bash
ifm-advisor summarize \
  --arm baseline='runs/baseline/*' --arm advised='runs/advised/*' \
  --benchmark my-benchmark \
  --solver IFM/K2-Horizon-375B-A23B --advisor IFM/K2-Horizon-375B-A23B \
  --csv results/pass_rates.csv --interventions-csv results/interventions.csv
```

`--json` prints JSON instead of a table. Quote the globs so your shell does not expand them.

It reads only the verifier's verdict (`result.json` or `verifier/reward.txt`),
`advisor/summary.json`, `advisor/audit.json`, and the `trigger`, `intervened` and `decision.action`
fields of `advisor/private_trace.jsonl`. It never reads hints, plans or diagnoses.

## Reported fields

| Field | Meaning |
|---|---|
| `attempts`, `tasks` | Attempt directories matched, and distinct task names among them |
| `correct` | Attempts whose verifier reward is at least 1.0 |
| `unverified` | Attempts with no verifier reward (no `result.json` reward and no `verifier/reward.txt`, or invalid content). They count as not correct; a file that exists but cannot be read stops `summarize` with exit 2 instead |
| `pass_rate`, `pass_rate_ci95` | `correct / attempts` and its 95% Wilson interval |
| `advised_attempts` | Attempts that have an `advisor/summary.json` |
| `attempts_with_intervention` | Attempts with at least one intervention |
| `interventions` | Total interventions |
| `reviews_by_trigger` | Reviews by `pre_submit` or `periodic` |
| `interventions_by_action` | Interventions by advisor action |
| `interventions_by_trigger_action` | Interventions by `trigger:action` |
| `advisor_failures` | Reviews or revisions that failed and fell back to the draft |
| `hint_screen_fallbacks` | Hints replaced by the self-check sentence by `screen_hints` |
| `effectively_unadvised` | Advised attempts whose advisor failed and never intervened. With `fail_open` these ran as baselines |
| `advised_without_reviews` | Advised attempts with no review at all: `finish_reviews + periodic_reviews` is 0 in `advisor/summary.json` (or, without those counters, the private trace has no records) |
| `audit` | Count of audit verdicts, one key per verdict: `pass`, `pass_mechanical_only`, `fail`, `inconclusive` |

Check `effectively_unadvised` and `advised_without_reviews` before you compare arms. A nonzero
`effectively_unadvised` means part of the "advised" arm was not advised at all, usually because of
an advisor endpoint or timeout problem. A nonzero `advised_without_reviews` means the advisor was
never consulted: the agent's calls did not reach the hook (it runs inside its container, or uses
an API the hook does not wrap), or its finish tool is not the `finish_tool` the `Advisor` was given.
The table shows them as `unadvised` and `unreviewed`.

## Plot

`results/pass_rates.csv` and `results/interventions.csv` hold counts, not rates. Draw the figure with:

```bash
pip install -e ".[results]"
python results/plot.py
```

`plot.py` reads `results/pass_rates.csv` and writes `assets/pass_rates.svg`. It computes each pass
rate and its 95% Wilson interval from `correct` and `attempts`, so every number in the figure can be
re-derived from the CSV, and the output is deterministic. SVG text is written as real text, not as
paths.

The CSV columns are `benchmark,arm,solver,advisor,tasks,attempts,correct`, and `benchmark,arm,trigger,action,count`
for interventions. The `advisor` column is empty for an unadvised arm.
