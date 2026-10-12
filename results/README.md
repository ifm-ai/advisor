# Results

`advisor_rescue.csv` holds the raw counts behind the results in the top-level README and
`assets/advisor_comparison.svg`: per category, the tasks, those the solver solved on its own, and
those it solved in an advised rerun after failing on its own. Rates are never stored; every
percentage and lift in the README is derived from these counts, and `tests/test_results.py` checks
that the README and the docs home page show exactly what the counts give.

| Column | Meaning |
|---|---|
| `category` | Task category; the last row, `Overall`, is the sum of the others |
| `tasks` | Tasks in the category |
| `solved_without_advisor` | Tasks the solver solved on its own |
| `solved_by_advisor` | Tasks the solver did not solve on its own and then solved in an advised rerun |

## Files for your own runs

| File | Columns | Meaning |
|---|---|---|
| `pass_rates.csv` | `benchmark,arm,solver,advisor,tasks,attempts,correct` | One row per benchmark and arm. `advisor` is empty for an unadvised arm. |
| `interventions.csv` | `benchmark,arm,trigger,action,count` | Optional. Interventions by review trigger (`pre_submit`, `periodic`) and action. |
| `plot.py` | | Draws `assets/pass_rates.svg` from `pass_rates.csv`. |

Rates are never stored. `plot.py` computes each pass rate and its 95% Wilson interval from
`correct` and `attempts`, so every number in the figure can be re-derived from the CSV.

## Producing the CSVs from your own runs

Put each arm's attempt directories in their own folder, then run:

```bash
ifm-advisor summarize \
  --arm baseline='runs/baseline/*' --arm advised='runs/advised/*' \
  --benchmark my-benchmark --solver IFM/K2-Horizon-375B-A23B --advisor IFM/K2-Horizon-375B-A23B \
  --csv results/pass_rates.csv --interventions-csv results/interventions.csv
python results/plot.py
```
