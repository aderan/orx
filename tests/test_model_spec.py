"""ModelSpec data-contract tests (G008 stage 1).

Pins the semantic capability tuple, the normalization rules (speed/fast,
context, version policy, unified effort), the error vocabulary that names
the model.* field, and the boundary that ModelSpec never parses CLI
slugs or bracket syntax.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from orx.models import (
    EFFORTS,
    FLOATING_VERSION,
    SPEEDS,
    ModelSpec,
    ModelSpecError,
    ResolvedModel,
    normalize_context,
    parse_model_spec,
)


def test_full_semantic_spec_expresses_every_dimension_and_round_trips():
    spec = ModelSpec(
        provider="anthropic",
        family="claude-opus",
        version="5.5",
        effort="high",
        speed="fast",
        context_tokens=1_000_000,
        capabilities=("coding", "vision"),
    )
    data = spec.to_dict()
    # JSON-expressible by construction: fixed key set, str/None/int/list.
    assert json.dumps(data)
    assert data == {
        "provider": "anthropic",
        "family": "claude-opus",
        "version": "5.5",
        "id": None,
        "effort": "high",
        "speed": "fast",
        "context": 1_000_000,
        "capabilities": ["coding", "vision"],
    }
    # a serialized dict round-trips through the same parser (None = absent)
    assert parse_model_spec(data, profile_effort="high") == spec


def test_identity_forms_are_stable_strings():
    assert ModelSpec(model_id="gpt-6.1-sol").identity == "gpt-6.1-sol"
    assert ModelSpec(family="grok", version="4.7").identity == "grok@4.7"
    assert ModelSpec(family="grok").identity == "grok"
    assert (
        ModelSpec(provider="anthropic", family="claude-opus", version="5.5").identity
        == "anthropic/claude-opus@5.5"
    )
    assert ModelSpec(provider="anthropic", family="grok").identity == "anthropic/grok"
    # an id is self-contained: provider never prefixes it
    assert ModelSpec(provider="zhipu", model_id="glm-5.2").identity == "glm-5.2"


def test_spec_is_frozen():
    spec = ModelSpec(family="grok")
    with pytest.raises(FrozenInstanceError):
        spec.family = "other"


def test_opaque_id_never_parses_cli_slug_or_bracket_syntax():
    raw = "claude-opus-4-8[context=1m,effort=high,fast=false]"
    spec = ModelSpec(model_id=raw)
    assert spec.identity == raw
    # no dimension was extracted out of the bracket string
    assert spec.family is None
    assert spec.version is None
    assert spec.effort is None
    assert spec.speed is None
    assert spec.context_tokens is None
    assert spec.to_dict()["id"] == raw


def test_version_policy_fixed_vs_explicit_floating():
    # a concrete version pins
    assert ModelSpec(family="grok", version="4.7").version == "4.7"
    assert parse_model_spec({"family": "grok", "version": "4.7"}).version == "4.7"
    # the explicit floating spelling canonicalizes to None on every path
    assert parse_model_spec({"family": "grok", "version": FLOATING_VERSION}).version is None
    assert ModelSpec(family="grok", version="latest").version is None
    assert ModelSpec(family="grok").version is None


def test_parse_normalizes_dimensions_from_a_model_table():
    spec = parse_model_spec(
        {
            "provider": "zhipu",
            "family": "glm",
            "version": "5.3",
            "speed": "fast",
            "context": "1m",
            "capabilities": ["coding", "vision"],
        },
        profile_effort="xhigh",
    )
    assert spec.provider == "zhipu"
    assert spec.family == "glm"
    assert spec.version == "5.3"
    assert spec.speed == "fast"
    assert spec.context_tokens == 1_000_000
    assert spec.capabilities == ("coding", "vision")
    # unified effort source: the profile-level value, unchallenged
    assert spec.effort == "xhigh"
    assert spec.identity == "zhipu/glm@5.3"


@pytest.mark.parametrize("value,expected", [
    ("1m", 1_000_000),
    ("1M", 1_000_000),
    ("128k", 128_000),
    ("128K", 128_000),
    ("1.5m", 1_500_000),
    ("5000", 5_000),
    (200_000, 200_000),
])
def test_context_spellings_normalize_to_token_counts(value, expected):
    assert normalize_context(value) == expected
    assert parse_model_spec({"family": "x", "context": value}).context_tokens == expected


@pytest.mark.parametrize("value", [
    "1x", "m", "", "  ", "0", "0k", "-5", "1.2.3", "1e6",
    0, -5, 1.5, True, False,
])
def test_invalid_context_is_rejected_not_guessed(value):
    with pytest.raises(ModelSpecError) as excinfo:
        normalize_context(value)
    assert "model.context" in str(excinfo.value)


def test_fast_alias_is_one_dimension_with_speed():
    assert parse_model_spec({"family": "x", "fast": True}).speed == "fast"
    assert parse_model_spec({"family": "x", "fast": False}).speed == "standard"
    with pytest.raises(ModelSpecError) as excinfo:
        parse_model_spec({"family": "x", "speed": "fast", "fast": True})
    message = str(excinfo.value)
    assert "model.fast" in message and "model.speed" in message
    # identical intent, still a duplicate declaration
    with pytest.raises(ModelSpecError):
        parse_model_spec({"family": "x", "speed": "standard", "fast": False})
    # wrong types
    with pytest.raises(ModelSpecError) as fast_str:
        parse_model_spec({"family": "x", "fast": "yes"})
    assert "model.fast" in str(fast_str.value)
    with pytest.raises(ModelSpecError) as bad_speed:
        parse_model_spec({"family": "x", "speed": "rapid"})
    assert "model.speed" in str(bad_speed.value)
    assert " | ".join(SPEEDS) in str(bad_speed.value)


def test_effort_has_one_source():
    # absent from the table -> the profile-level effort is the spec's effort
    assert parse_model_spec({"family": "x"}, profile_effort="high").effort == "high"
    # identical restatement is accepted
    assert parse_model_spec({"family": "x", "effort": "high"},
                            profile_effort="high").effort == "high"
    # a differing restatement is a conflict, never a silent override
    with pytest.raises(ModelSpecError) as excinfo:
        parse_model_spec({"family": "x", "effort": "high"}, profile_effort="medium")
    message = str(excinfo.value)
    assert "model.effort" in message
    assert "conflicts" in message
    assert "high" in message and "medium" in message
    # invalid vocabulary names the field and the expected set
    with pytest.raises(ModelSpecError) as invalid:
        parse_model_spec({"family": "x", "effort": "ludicrous"})
    assert "model.effort" in str(invalid.value)
    assert " | ".join(EFFORTS) in str(invalid.value)


def test_unknown_fields_are_rejected_by_name():
    with pytest.raises(ModelSpecError) as excinfo:
        parse_model_spec({"family": "x", "reasoning": "deep"})
    message = str(excinfo.value)
    assert "model.reasoning" in message
    assert "recognized model field" in message
    for allowed in ("provider", "family", "version", "id", "effort",
                    "speed", "fast", "context", "capabilities"):
        assert allowed in message


@pytest.mark.parametrize("table,fragment", [
    ({"id": "a", "family": "b"}, "ambiguous"),
    ({"id": "a", "version": "1"}, "ambiguous"),
    ({"version": "1"}, "model.version requires model.family"),
    ({}, "must declare either family or id"),
    ({"provider": "anthropic"}, "must declare either family or id"),
])
def test_ambiguous_or_missing_identity_is_rejected(table, fragment):
    with pytest.raises(ModelSpecError) as excinfo:
        parse_model_spec(table)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("table,fragment", [
    ({"family": 5}, "model.family must be a non-empty string"),
    ({"family": " "}, "model.family must be a non-empty string"),
    ({"id": 7}, "model.id must be a non-empty string"),
    ({"version": 2}, "model.version must be a non-empty string"),
    ({"provider": True}, "model.provider must be a non-empty string"),
    ({"capabilities": "coding"}, "model.capabilities must be a list of non-empty strings"),
    ({"family": "x", "capabilities": ["ok", ""]},
     "model.capabilities must be a list of non-empty strings"),
])
def test_illegal_types_name_the_field(table, fragment):
    with pytest.raises(ModelSpecError) as excinfo:
        parse_model_spec(table)
    assert fragment in str(excinfo.value)


def test_parse_rejects_non_table_input():
    with pytest.raises(ModelSpecError) as excinfo:
        parse_model_spec(["family", "x"])
    assert "model must be a table" in str(excinfo.value)


def test_direct_construction_is_guarded_too():
    with pytest.raises(ModelSpecError):
        ModelSpec(family="x", effort="turbo")
    with pytest.raises(ModelSpecError):
        ModelSpec(family="x", speed="rapid")
    with pytest.raises(ModelSpecError):
        ModelSpec(family="x", context_tokens=0)
    with pytest.raises(ModelSpecError):
        ModelSpec(family="x", context_tokens=True)
    with pytest.raises(ModelSpecError):
        ModelSpec()  # no identity at all
    with pytest.raises(ModelSpecError):
        ModelSpec(family="")  # empty identity string
    with pytest.raises(ModelSpecError):
        ModelSpec(model_id="a", family="b")  # ambiguous
    with pytest.raises(ModelSpecError):
        ModelSpec(capabilities=("ok", ""))


def test_resolved_model_records_the_resolution_decision():
    spec = parse_model_spec(
        {"family": "claude-opus", "version": "5.5", "context": "1m"},
        profile_effort="high",
    )
    resolved = ResolvedModel(
        spec=spec,
        cli_slug="claude-opus-5-5[context=1m,effort=high]",
        strategy="bracket",
        bracket_params=(("context", "1m"), ("effort", "high")),
        catalog_generation=3,
    )
    data = resolved.to_dict()
    assert json.dumps(data)
    assert data["cli_slug"] == "claude-opus-5-5[context=1m,effort=high]"
    assert data["strategy"] == "bracket"
    assert data["bracket_params"] == [["context", "1m"], ["effort", "high"]]
    assert data["catalog_generation"] == 3
    assert data["spec"]["family"] == "claude-opus"
    # minimal resolution: no params, no catalog involved
    minimal = ResolvedModel(spec=spec, cli_slug="auto", strategy="bare")
    assert minimal.bracket_params == ()
    assert minimal.catalog_generation is None
    with pytest.raises(FrozenInstanceError):
        resolved.strategy = "other"
