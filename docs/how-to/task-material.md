# Use your own task material

## When to use this

Use this when you need to tell the advisor what the task is (`x`) and what it may read but the solver
may not (`z`): the reference solution, hidden tests and private notes. The material is loaded once,
in the advisor's own process. Nothing is written to a task directory or to the solver's sandbox.

## The default Harbor and Terminal-Bench layout

`TaskMaterial.from_task_dir("path/to/task")` reads a Harbor or Terminal-Bench task directory:

| Path | Role |
|---|---|
| `instruction.md` | Public instruction, the `x` shown to both the solver and the advisor |
| `README.md` | Privileged. Read first, because it explains the intended solution and how it is verified |
| `solution/**` | Privileged |
| `tests/**` | Privileged |
| `cheat/`, `__pycache__/`, `.git/`, `.pytest_cache/` | Skipped (`cheat` is an anti-cheat tripwire, not review material) |
| `*.pyc`, `*.pyo`, `*.so`, `*.o`, `*.a` | Skipped |
| `environment/`, `task.toml` and anything else | Never read, because they are not privileged roots |

If `instruction.md` is missing or unreadable the advisor sees the placeholder
`(public task instruction unavailable)`. The instruction is read up to 100,000 bytes.

## Symlinks and special files

`from_task_dir` never follows symlinks and never reads anything that is not a regular file. A
symlinked file, a symlinked directory, a symlinked privileged root, a named pipe, a socket or a
device node is recorded in the manifest as skipped, with `skipped_reason` set to `"symlink"` or
`"not a regular file"`. It is never read, hashed or sent to the advisor. A symlinked instruction
file is treated as unavailable, so the placeholder instruction is used. Symlinks are checked
statically, before reading: a symlink swapped into an intermediate directory while a load is running
is a known limitation, not a guarantee.

A symlink anywhere along a nested layout path counts too. With `privileged_roots=("hidden/tests",)`
and `hidden` a symlink, `hidden/tests` is recorded as skipped `"symlink"` and nothing beneath it is
read; an `instruction_file` such as `docs/TASK.md` behind a symlinked `docs` gives the placeholder.

## A custom `TaskLayout`

```python
from ifm_advisor import TaskLayout, TaskMaterial

layout = TaskLayout(
    instruction_file="PROBLEM.md",
    privileged_roots=("NOTES.md", "reference", "hidden_tests"),
    excluded_dirs=frozenset({"__pycache__", ".git", "fixtures_large"}),
)
task = TaskMaterial.from_task_dir("path/to/task", layout)
```

Every `instruction_file` and `privileged_roots` entry must be a relative path that stays inside the
task directory. An empty entry, an absolute path or a path containing `..` raises `ValueError`
("layout paths must be relative and stay inside the task directory"). Privileged roots are read in
the order given; files inside a directory root are read in sorted path order.

## `TaskMaterial.from_texts`

For tasks that are not laid out as a directory, pass the material directly. The keys are display
paths, used in the prompt and the manifest.

```python
task = TaskMaterial.from_texts(
    "Fix `median` so that even-length lists return the mean of the middle pair.",
    {
        "solution/stats.py": reference_source,
        "tests/test_stats.py": hidden_test_source,
    },
    name="median-task",
)
```

## Size limits

`max_file_bytes` (default 1,000,000) and `max_total_bytes` (default 4,000,000) bound what is sent to
the advisor. Both are keyword arguments of `from_task_dir` and `from_texts`; the total may not be
below the per-file limit.

- A file larger than `max_file_bytes`, or one that exceeds what remains of the total, is
  **truncated**. The truncation is recorded in `truncated_paths` and the manifest, and the advisor's
  prompt marks the file `TRUNCATED`.
- Once the total budget is spent, the remaining files are **omitted**. They are listed in
  `omitted_paths`, and the advisor's prompt names them.
- A file that is not valid UTF-8 is recorded by hash only (`skipped_reason` is `"not valid utf-8"`)
  and costs nothing against the budget. A cut inside a multi-byte character caused by truncation does
  not count as binary.
- A directory that does not exist raises `FileNotFoundError` rather than producing an advisor with
  no privileged material.
- A privileged file or directory that cannot be read, for example for lack of permission, is
  recorded as skipped (`skipped_reason` is `"unreadable: <ExceptionType>"`, such as
  `"unreadable: PermissionError"`), and loading continues without it. For a directory nothing
  beneath it is listed. `ifm-advisor audit` refuses such material: it exits 2 without a verdict.

The manifest holds paths, sizes and hashes only, never contents and never the task's absolute path.
