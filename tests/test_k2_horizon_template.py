"""K2-Horizon's published chat template, rendered the way SGLang serves continue_final_message.

The tests that download the template are marked `network`; `pytest -m "not network"` skips them
and still runs the offline tests of the download helper.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime

import pytest

jinja2 = pytest.importorskip("jinja2")

from jinja2.ext import Extension, loopcontrols  # noqa: E402
from jinja2.sandbox import ImmutableSandboxedEnvironment  # noqa: E402

from ifm_advisor.config import ReasoningPrefillCue  # noqa: E402
from ifm_advisor.cues import compile_cue  # noqa: E402
from ifm_advisor.decision import AdvisorDecision  # noqa: E402

TEMPLATE_URL = "https://huggingface.co/IFM/K2-Horizon-375B-A23B/raw/main/chat_template.jinja"
HINT = "Hmm. The odd-length case passes, but nothing shows what an even-length list returns."
HISTORY = [
    {"role": "user", "content": "Fix median()."},
    {
        "role": "assistant",
        "content": "I'll read the file.",
        "reasoning_content": "Let me read stats.py.",
    },
    {"role": "user", "content": "def median(values): ..."},
]


class GenerationTag(Extension):
    """transformers' {% generation %}...{% endgeneration %}: renders its body unchanged."""

    tags = {"generation"}

    def parse(self, parser):
        next(parser.stream)
        return parser.parse_statements(("name:endgeneration",), drop_needle=True)


def _raise(message):
    raise jinja2.exceptions.TemplateError(message)


def _tojson(value, indent=None, separators=None, sort_keys=False):
    return json.dumps(
        value, ensure_ascii=False, indent=indent, separators=separators, sort_keys=sort_keys
    )


# The template moved or was removed: a real failure. Any other HTTP error (429, 5xx) is the host's.
_GONE = frozenset({404, 410})


def _download_template() -> str:
    """The template's source. Fails when the template moved (HTTP 404 or 410); skips when the host
    is unreachable or answers with any other error."""

    try:
        with urllib.request.urlopen(TEMPLATE_URL, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code in _GONE:
            pytest.fail(
                f"the K2-Horizon chat template is gone from {TEMPLATE_URL}: HTTP {error.code}"
            )
        pytest.skip(f"cannot download the K2-Horizon chat template: HTTP {error.code}")
    except OSError as error:
        pytest.skip(f"cannot download the K2-Horizon chat template: {error}")


def _http_error(code: int, reason: str):
    def respond(*args, **kwargs):
        raise urllib.error.HTTPError(TEMPLATE_URL, code, reason, hdrs=None, fp=None)

    return respond


@pytest.mark.parametrize(("code", "reason"), [(404, "Not Found"), (410, "Gone")])
def test_a_moved_template_fails_instead_of_skipping(monkeypatch, code, reason):
    monkeypatch.setattr(urllib.request, "urlopen", _http_error(code, reason))

    with pytest.raises(pytest.fail.Exception):
        _download_template()


@pytest.mark.parametrize(
    ("code", "reason"), [(429, "Too Many Requests"), (503, "Service Unavailable")]
)
def test_a_server_error_skips(monkeypatch, code, reason):
    monkeypatch.setattr(urllib.request, "urlopen", _http_error(code, reason))

    with pytest.raises(pytest.skip.Exception):
        _download_template()


def test_an_unreachable_host_skips(monkeypatch):
    def unreachable(*args, **kwargs):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)

    with pytest.raises(pytest.skip.Exception):
        _download_template()


@pytest.fixture(scope="module")
def template():
    source = _download_template()
    environment = ImmutableSandboxedEnvironment(
        trim_blocks=True, lstrip_blocks=True, extensions=[GenerationTag, loopcontrols]
    )
    environment.filters["tojson"] = _tojson
    environment.globals["raise_exception"] = _raise
    environment.globals["strftime_now"] = lambda fmt: datetime.now().strftime(fmt)
    return environment.from_string(source)


def render_like_sglang(template, messages, **kwargs):
    """SGLang's continue_final_message: drop the trailing assistant message, render the rest with
    add_generation_prompt=True, then append the dropped content (serving_chat.py)."""

    assert messages[-1]["role"] == "assistant"
    rendered = template.render(
        messages=messages[:-1], add_generation_prompt=True, bos_token="", eos_token="", **kwargs
    )
    return rendered + messages[-1]["content"]


def _cue_message():
    decision = AdvisorDecision(
        intervene=True,
        confidence=0.9,
        reason_code="OTHER",
        action="RUN_BROADER_VALIDATION",
        plan="",
        hint=HINT,
        private_diagnosis="",
    )
    cue = ReasoningPrefillCue.k2_horizon_sglang()
    return compile_cue(cue, decision, intervention_index=0).message


@pytest.mark.network
@pytest.mark.parametrize(
    ("effort", "opener"),
    [("high", "<ifm|think>\n"), ("medium", "<ifm|think_fast>\n"), ("low", "<ifm|think_faster>\n")],
)
def test_the_hint_lands_inside_an_open_think_block(template, effort, opener):
    prompt = render_like_sglang(template, [*HISTORY, _cue_message()], reasoning_effort=effort)

    assert prompt.endswith("<|ifm|im_start|>assistant\n" + opener + HINT)
    # Only the earlier turn's reasoning block is closed; the hint's block stays open.
    assert prompt.count("</ifm|think>") == 1


@pytest.mark.network
def test_a_plain_assistant_prefill_is_rejected_by_the_template(template):
    """Why the empty prefix relies on SGLang's semantics: rendering the prefill as an ordinary
    assistant turn (HF-style continuation) fails, because it has no thinking field."""

    with pytest.raises(jinja2.exceptions.TemplateError, match="thinking field"):
        template.render(
            messages=[*HISTORY, _cue_message()],
            add_generation_prompt=False,
            bos_token="",
            eos_token="",
        )
