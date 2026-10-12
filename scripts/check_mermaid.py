"""Render every Mermaid block in the repository's Markdown with the real Mermaid parser.

    python scripts/check_mermaid.py

Needs Node.js (npx) and git. Each ```mermaid block in a tracked .md file is rendered with
@mermaid-js/mermaid-cli; a block that fails to render fails the check. GitHub and the docs site
render diagrams in the reader's browser, so a broken diagram is otherwise only seen after release.
Exit 0 when every block renders, 1 when one does not, 2 when npx or git is unavailable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MERMAID_CLI = "@mermaid-js/mermaid-cli@11"
BLOCK = re.compile(r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


def markdown_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    return [ROOT / name for name in listed]


def main() -> int:
    npx = shutil.which("npx")
    if npx is None or shutil.which("git") is None:
        print("error: check_mermaid.py needs npx (Node.js) and git on PATH", file=sys.stderr)
        return 2
    blocks = [
        (path, number, match.group(1))
        for path in markdown_files()
        for number, match in enumerate(BLOCK.finditer(path.read_text(encoding="utf-8")), 1)
    ]
    failures = 0
    with tempfile.TemporaryDirectory() as scratch:
        # Headless Chromium cannot create its sandbox on many CI runners and containers.
        config = Path(scratch) / "puppeteer.json"
        config.write_text(json.dumps({"args": ["--no-sandbox"]}), encoding="utf-8")
        for index, (path, number, source) in enumerate(blocks):
            diagram = Path(scratch) / f"{index}.mmd"
            diagram.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [
                    npx,
                    "-y",
                    MERMAID_CLI,
                    "-p",
                    str(config),
                    "-i",
                    str(diagram),
                    "-o",
                    str(diagram.with_suffix(".svg")),
                ],
                capture_output=True,
                text=True,
            )
            label = f"{path.relative_to(ROOT)} block {number}"
            if result.returncode == 0:
                print(f"ok    {label}")
            else:
                failures += 1
                print(f"FAIL  {label}\n{(result.stderr or result.stdout).strip()}", file=sys.stderr)
    print(f"{len(blocks) - failures}/{len(blocks)} Mermaid blocks render")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
