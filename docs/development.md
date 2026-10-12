# Development

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,docs,results]"
```

## Tests

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest -m "not network"
```

The second form skips the tests that download K2-Horizon's published chat template; the offline
tests of the download helper still run. A template that is gone (HTTP 404 or 410) fails; an
unreachable host or any other HTTP error skips. Tests that
involve a model use scripted doubles or a fake LiteLLM module, so no endpoint is needed.

```bash
.venv/bin/mypy                                     # strict type check of src/
.venv/bin/python -m pytest --cov=ifm_advisor --cov-fail-under=90
```

`tests/test_properties.py` checks properties with generated inputs (Hypothesis): the indexed overlap
check agrees with a plain scan, a rejected decision never repeats the advisor's text, and the task
material budgets hold.

## Lint and format

```bash
.venv/bin/ruff format src tests examples results scripts
.venv/bin/ruff check .
```

Ruff targets Python 3.12 with a line length of 100 and the rules `E, F, I, UP, B, SIM`.

## Documentation

```bash
.venv/bin/mkdocs serve          # live preview
.venv/bin/mkdocs build --strict # must exit 0 with no warnings
```

`tests/test_docs.py` checks that every configuration field is documented, that the navigation and
the `docs/` directory agree, that relative links resolve, and that the figure matches the one in
`assets/`. The generated `site/` directory is not committed.

## Diagrams

```bash
python3 scripts/check_mermaid.py
```

Renders every Mermaid diagram in the repository's Markdown with the real Mermaid parser
(`@mermaid-js/mermaid-cli`, fetched by `npx`; needs Node.js). GitHub and the docs site draw
diagrams in the reader's browser, so a diagram that does not parse is otherwise only seen after
release. CI runs it on every push and pull request.

## Changing a prompt

Never edit a released prompt or profile. Add a new version file (`advisor_v2.txt`, or a new profile),
select it with `prompt_version` or `solver_profile`, and add a test. Do not change
`tests/fixtures/pre_submit_v1.txt`: it is the prompt the paper's runs used. See
[Prompts and versioning](reference/prompts.md).

## Rules for solver-visible text

No change may add text that reaches the solver without a test showing it is audit-clean, that is,
that it contains none of the audit's leak markers.

## Releasing

```bash
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

Publishing is a deliberate manual step; nothing in the repository publishes automatically. Update
`CHANGELOG.md` and `__version__` in `src/ifm_advisor/__init__.py` first.
