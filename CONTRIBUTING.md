# Contributing

## Set up

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,docs,results]"
```

## Test and lint

```bash
.venv/bin/python -m pytest              # -m "not network" skips the template download
.venv/bin/ruff format src tests examples results scripts
.venv/bin/ruff check .
.venv/bin/mypy                          # strict type check of src/
.venv/bin/mkdocs build --strict         # the documentation must build without warnings
```

## Two project rules

1. **Prompts are never edited in place.** A changed prompt changes advisor behavior and must be
   traceable in results, so add a new version file and select it with `prompt_version` or
   `solver_profile`.
2. **No change may add text that reaches the solver without a test showing it is audit-clean.**
   Solver-visible text must contain none of the leak markers the audit checks for.

See [docs/development.md](docs/development.md) for the full guide. Report security problems
privately, as [SECURITY.md](SECURITY.md) describes, not in a public issue.
