from pathlib import Path

import pytest
from pydantic import ValidationError

from ifm_advisor.config import (
    AdvisorConfig,
    EndpointConfig,
    ReasoningPrefillCue,
    SelfCheckCue,
)

ENDPOINT = {"model": "advisor-model", "api_base": "https://advisor.example/v1"}


def test_model_ids_with_a_slash_keep_the_provider_prefix():
    """A Hugging Face ID must never be read as a LiteLLM provider."""

    endpoint = EndpointConfig(
        model="IFM/K2-Horizon-375B-A23B", api_base="http://localhost:30000/v1"
    )

    assert endpoint.litellm_model == "openai/IFM/K2-Horizon-375B-A23B"


def test_empty_provider_sends_the_model_unchanged():
    endpoint = EndpointConfig(model="hosted_vllm/my-model", provider="", api_base="http://h/v1")

    assert endpoint.litellm_model == "hosted_vllm/my-model"


def test_endpoint_defaults():
    endpoint = EndpointConfig(**ENDPOINT)

    assert endpoint.provider == "openai"
    assert endpoint.api_key_env == "ADVISOR_API_KEY"
    assert endpoint.timeout_sec == 300.0
    assert endpoint.extra_body == {} and endpoint.extra_headers == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_base": "ftp://advisor.example"},
        {"api_base": ""},
        {"model": ""},
        {"api_key_env": "1INVALID"},
        {"api_key_env": "HAS-DASH"},
        {"timeout_sec": 0},
        {"timeout_sec": float("inf")},
        {"unknown_field": 1},
    ],
)
def test_invalid_endpoint_values_are_rejected(overrides):
    with pytest.raises(ValidationError):
        EndpointConfig(**{**ENDPOINT, **overrides})


def test_api_key_value_is_never_serialized(monkeypatch):
    monkeypatch.setenv("ADVISOR_API_KEY", "super-secret-value")

    serialized = AdvisorConfig(endpoint=EndpointConfig(**ENDPOINT)).model_dump_json()

    assert "super-secret-value" not in serialized
    assert "ADVISOR_API_KEY" in serialized


def test_advisor_defaults(advisor_config):
    config = AdvisorConfig(endpoint=EndpointConfig(**ENDPOINT))

    assert config.max_interventions == 2
    assert config.periodic_review_interval is None
    assert config.max_advisor_attempts == 2
    assert config.max_revision_attempts == 2
    assert config.cue == SelfCheckCue()
    assert config.screen_hints is False
    assert config.solver_profile == "generic"
    assert config.prompt_version == "advisor_v1"
    assert config.repeat_streak_escalation is False
    assert config.fail_open is True
    assert config.max_conversation_messages == 50
    assert config.max_message_chars == 8_000
    assert config.max_previous_cues == 8


@pytest.mark.parametrize(
    "field",
    [
        "max_interventions",
        "periodic_review_interval",
        "max_advisor_attempts",
        "max_revision_attempts",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_counts_are_rejected(field, value):
    with pytest.raises(ValidationError):
        AdvisorConfig(endpoint=EndpointConfig(**ENDPOINT), **{field: value})


def test_conversation_window_must_hold_the_head_and_a_tail():
    with pytest.raises(ValidationError):
        AdvisorConfig(endpoint=EndpointConfig(**ENDPOINT), max_conversation_messages=2)


@pytest.mark.parametrize("removed", ["mode", "reasoning_open_token", "max_private_file_bytes"])
def test_removed_fields_fail_loudly(removed):
    """Fields from the engine-era config must not be silently ignored."""

    with pytest.raises(ValidationError):
        AdvisorConfig(endpoint=EndpointConfig(**ENDPOINT), **{removed: "x"})


def test_cue_is_parsed_from_its_kind():
    config = AdvisorConfig.model_validate(
        {"endpoint": ENDPOINT, "cue": {"kind": "reasoning_prefill", "prefix": "<think>\n"}}
    )

    assert config.cue == ReasoningPrefillCue(prefix="<think>\n")


def test_unknown_cue_kind_is_rejected():
    with pytest.raises(ValidationError):
        AdvisorConfig.model_validate({"endpoint": ENDPOINT, "cue": {"kind": "telepathy"}})


def test_k2_horizon_sglang_preset_uses_an_empty_prefix():
    cue = ReasoningPrefillCue.k2_horizon_sglang(continuation_suffixes=("Hmm.",))

    assert cue.prefix == ""
    assert cue.continuation_suffixes == ("Hmm.",)


def test_solver_profile_keeps_names_and_paths_apart(tmp_path):
    named = AdvisorConfig(endpoint=EndpointConfig(**ENDPOINT), solver_profile="k2_horizon")
    pathed = AdvisorConfig(endpoint=EndpointConfig(**ENDPOINT), solver_profile=tmp_path / "p.txt")

    assert named.solver_profile == "k2_horizon"
    assert isinstance(pathed.solver_profile, Path)
