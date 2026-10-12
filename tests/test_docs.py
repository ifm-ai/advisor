import re
from pathlib import Path

from ifm_advisor.config import (
    AdvisorConfig,
    EndpointConfig,
    ReasoningPrefillCue,
    SelfCheckCue,
)
from ifm_advisor.material import TaskLayout

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
MERMAID_BLOCK = re.compile(r"^```mermaid[ \t]*\n(.*?)^```", re.M | re.S)
MERMAID_PARTICIPANT = re.compile(r"^\s*(?:participant|actor)\s+(\S+)", re.M)
# Words a sequence diagram reads as syntax, in any case, wherever a participant id uses them.
MERMAID_KEYWORDS = frozenset(
    {
        "activate",
        "actor",
        "alt",
        "and",
        "autonumber",
        "box",
        "break",
        "create",
        "critical",
        "deactivate",
        "destroy",
        "else",
        "end",
        "loop",
        "note",
        "opt",
        "option",
        "par",
        "participant",
        "rect",
        "title",
    }
)
BLOCK = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.M | re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
INLINE_MATH = re.compile(r"\$([^$\n]+)\$")


def _markdown_pages() -> list[Path]:
    return [ROOT / "README.md", *DOCS.rglob("*.md")]


def _formulas(text: str) -> list[str]:
    """The math in a Markdown page: ```math blocks, then $...$ spans outside code."""

    blocks = [body for info, body in BLOCK.findall(text) if info.strip() == "math"]
    prose = INLINE_CODE.sub("", BLOCK.sub("", text))
    return blocks + INLINE_MATH.findall(prose)


def test_sequence_diagram_participants_are_not_mermaid_keywords():
    for page in _markdown_pages():
        for block in MERMAID_BLOCK.findall(page.read_text()):
            for name in MERMAID_PARTICIPANT.findall(block):
                assert name.lower() not in MERMAID_KEYWORDS, f"{page.relative_to(ROOT)}: {name!r}"


def test_math_has_no_raw_angle_brackets():
    # GitHub's sanitizer can read "<t" inside a formula as an HTML tag; write \lt and \gt.
    for page in _markdown_pages():
        for formula in _formulas(page.read_text()):
            assert "<" not in formula and ">" not in formula, f"{page.relative_to(ROOT)}: {formula}"


def test_the_method_formulas_are_math():
    method = (DOCS / "explanation" / "method.md").read_text()

    assert method.count("```math") == 3
    assert "1{n_t" not in method and "A_φ" not in method


def _nav_pages() -> list[str]:
    return re.findall(r":\s*([\w/.-]+\.md)\s*$", (ROOT / "mkdocs.yml").read_text(), re.M)


def test_every_config_field_is_documented():
    reference = (DOCS / "reference" / "configuration.md").read_text()
    models = (EndpointConfig, AdvisorConfig, SelfCheckCue, ReasoningPrefillCue)
    fields = [name for model in models for name in model.model_fields if name != "kind"]
    fields += list(TaskLayout.__dataclass_fields__) + ["max_file_bytes", "max_total_bytes"]

    missing = [name for name in fields if f"`{name}`" not in reference]

    assert missing == []


def test_the_nav_and_the_docs_directory_agree():
    pages = set(_nav_pages())
    on_disk = {path.relative_to(DOCS).as_posix() for path in DOCS.rglob("*.md")}

    assert pages == on_disk


def test_relative_links_resolve():
    for page in [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *DOCS.rglob("*.md")]:
        for target in LINK.findall(page.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (page.parent / target).exists(), f"{page.relative_to(ROOT)} -> {target}"


HTML_LINK = re.compile(r'(?:src|srcset|href)="([^"#]+)(?:#[^"]*)?"')


def test_html_attribute_links_resolve():
    for page in _markdown_pages():
        for target in HTML_LINK.findall(page.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (page.parent / target).exists(), f"{page.relative_to(ROOT)} -> {target}"


def test_docs_assets_are_copies_of_the_repository_assets():
    copies = sorted(path.name for path in (DOCS / "assets").iterdir())

    assert {"advisor_rollout.svg", "ifm-advisor-light.svg", "ifm-advisor-dark.svg"} <= set(copies)
    for name in copies:
        assert (DOCS / "assets" / name).read_bytes() == (ROOT / "assets" / name).read_bytes(), name


def test_the_banners_are_vector_and_titled():
    for name in ("ifm-advisor-light.svg", "ifm-advisor-dark.svg"):
        svg = (ROOT / "assets" / name).read_text()

        assert '<title id="title">IFM | Advisor</title>' in svg
        assert svg.count("<image") == 1  # the IFM mark; the wordmark is outlines
        assert "<text" not in svg and "@font-face" not in svg


def test_the_readme_follows_the_house_layout():
    readme = (ROOT / "README.md").read_text()
    headings = re.findall(r"^#{1,3} .+$", readme, re.M)

    assert headings[0] == "# Advisor"
    for heading in (
        "## Repository",
        "## How it works",
        "## Use the advisor",
        "### 1. Install",
        "### 5. Audit and summarize runs",
        "## License",
        "## About IFM",
    ):
        assert heading in headings, heading
    assert 'alt="IFM | Advisor"' in readme and 'href="#use-the-advisor"' in readme
    assert "private_trace.jsonl" in readme


def test_no_unfinished_markers():
    for page in [ROOT / "README.md", *DOCS.rglob("*.md")]:
        text = page.read_text()
        for marker in ("TODO", "TBD", "FIXME", "XXX"):
            assert marker not in text, f"{page.relative_to(ROOT)}: {marker}"
