"""Model catalog parsing and refresh semantics (G008 T002).

Fixtures under tests/fixtures/model-catalog/ are captures of the real
local CLI output shapes (cursor --list-models / --help including the
zero-width characters and tip line the real CLI prints; a trimmed but
real-shaped codex debug models JSON). No network, no completion, no
real CLI process: everything here parses text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orx import model_catalog
from orx.models import EFFORTS

FIXTURES = Path(__file__).parent / "fixtures" / "model-catalog"


def cursor_list_text() -> str:
    return (FIXTURES / "cursor-list-models.txt").read_text()


def agent_help_text() -> str:
    return (FIXTURES / "agent-help.txt").read_text()


def codex_models_text() -> str:
    return (FIXTURES / "codex-debug-models.json").read_text()


# -- cursor: enumerated slugs ----------------------------------------------


def test_cursor_fixture_parses_the_real_catalog():
    entries = model_catalog.parse_cursor_list_models(cursor_list_text())
    slugs = [entry.slug for entry in entries]
    # every enumerated slug from the live CLI, exact set, no more, no less
    assert slugs == [
        "auto", "composer-2.5", "cursor-grok-4.5-high",
        "cursor-grok-4.5-high-fast", "grok-4.7-low", "grok-4.7-low-fast",
        "grok-4.7-medium", "grok-4.7-medium-fast", "grok-4.7-high",
        "grok-4.7-high-fast", "grok-4.7-xhigh", "grok-4.7-xhigh-fast",
        "cursor-grok-4.6-low", "cursor-grok-4.6-low-fast",
        "cursor-grok-4.6-medium", "cursor-grok-4.6-medium-fast",
        "cursor-grok-4.6-high", "cursor-grok-4.6-high-fast",
        "cursor-grok-4.6-xhigh", "cursor-grok-4.6-xhigh-fast",
        "composer-2.5-fast", "cursor-grok-4.5-low", "cursor-grok-4.5-low-fast",
        "cursor-grok-4.5-medium", "cursor-grok-4.5-medium-fast",
        "kimi-k3-low", "kimi-k3-high", "kimi-k3-max", "kimi-k2.7-code",
        "glm-5.2-high", "glm-5.2-max",
    ]
    by_slug = {entry.slug: entry for entry in entries}
    assert by_slug["auto"].is_default is True
    assert by_slug["auto"].description == "Auto"
    assert by_slug["grok-4.7-high"].is_current is True
    assert by_slug["grok-4.7-high"].is_current is not by_slug["auto"].is_current
    # zero-width characters the real CLI emits must not survive anywhere
    for entry in entries:
        for value in (entry.slug, entry.description, entry.display_name):
            assert value is None or "\u200b" not in value


def test_cursor_header_blank_ansi_zero_width_and_prose_never_become_models():
    text = (
        "\x1b[1mAvailable models\x1b[0m\n"
        "\n"
        "\n"
        "Tip: use --model <id> (or /model <id> in interactive mode) to switch.\n"
        "Some prose about models - not a model line at all\n"
        "\u200b\u200b\n"
        "\x1b[32mgrok-4.7-low\x1b[0m - \x1b[1mGrok 4.7 Low\x1b[0m\n"
        "\u200bgrok-4.7-medium\u200b - Grok 4.7\u200b Medium\n"
        "auto - Auto (default)\n"
    )
    entries = model_catalog.parse_cursor_list_models(text)
    assert [entry.slug for entry in entries] == [
        "grok-4.7-low", "grok-4.7-medium", "auto",
    ]
    # ANSI-wrapped and zero-width-wrapped lines parse to clean fields
    assert entries[0].description == "Grok 4.7 Low"
    assert entries[1].description == "Grok 4.7 Medium"
    assert entries[2].is_default is True


def test_cursor_effort_and_fast_variant_decoding():
    entries = {
        entry.slug: entry
        for entry in model_catalog.parse_cursor_list_models(cursor_list_text())
    }
    # longest-effort match: xhigh is not misread as high
    e = entries["grok-4.7-xhigh-fast"]
    assert (e.family, e.effort, e.speeds) == ("grok-4.7", "xhigh", ("fast",))
    e = entries["grok-4.7-high"]
    assert (e.family, e.effort, e.speeds) == ("grok-4.7", "high", ("standard",))
    # fast variant of an effort-less family
    e = entries["composer-2.5-fast"]
    assert (e.family, e.effort, e.speeds) == ("composer-2.5", None, ("fast",))
    # non-effort suffixes stay part of the family
    e = entries["kimi-k2.7-code"]
    assert (e.family, e.effort, e.speeds) == ("kimi-k2.7-code", None, ("standard",))
    # provider-prefixed family kept verbatim; max is an effort token
    e = entries["cursor-grok-4.6-medium-fast"]
    assert (e.family, e.effort, e.speeds) == ("cursor-grok-4.6", "medium", ("fast",))
    e = entries["glm-5.2-max"]
    assert (e.family, e.effort) == ("glm-5.2", "max")
    e = entries["auto"]
    assert (e.family, e.effort) == ("auto", None)


def test_cursor_effort_decoding_covers_orx_vocabulary():
    for effort in EFFORTS:
        family, decoded, _ = model_catalog.decode_cursor_slug(f"family-x-{effort}")
        assert decoded == effort
        assert family == "family-x"


def test_cursor_duplicate_slug_keeps_first_occurrence():
    text = "m-a - First\nm-a - Second\n"
    entries = model_catalog.parse_cursor_list_models(text)
    assert len(entries) == 1 and entries[0].description == "First"


def test_cursor_valid_empty_catalog_is_not_an_error():
    text = "Available models\n\nTip: use --model <id> to switch.\n"
    entries = model_catalog.parse_cursor_list_models(text)
    assert entries == []


# -- cursor: parameterized bracket syntax -----------------------------------


def test_parameterized_syntax_from_real_help_and_tip():
    evidence = model_catalog.parse_parameterized_syntax({
        "agent --help": agent_help_text(),
        "agent --list-models": cursor_list_text(),
    })
    assert evidence.available is True
    assert set(evidence.param_keys) == {"context", "effort", "fast"}
    assert "claude-opus-4-8" in evidence.example_families
    assert "[context=1m,effort=high,fast=false]" in (evidence.syntax_example or "")
    assert "agent --help" in evidence.sources and "agent --list-models" in evidence.sources
    # parameterized evidence stays out of the enumerated models
    enumerated = {
        entry.slug for entry in model_catalog.parse_cursor_list_models(cursor_list_text())
    }
    for family in evidence.example_families:
        assert family not in enumerated


def test_parameterized_syntax_is_example_evidence_not_a_support_list():
    evidence = model_catalog.parse_parameterized_syntax({
        "agent --help": "  --model <model>  e.g. gpt-5, sonnet-4-thinking\n"
                        "  'claude-opus-4-8[context=1m,effort=high,fast=false]'\n"
                        "  'gpt-5[effort=high]'\n",
    })
    assert evidence.available is True
    assert set(evidence.param_keys) == {"context", "effort", "fast"}
    assert set(evidence.example_families) == {"claude-opus-4-8", "gpt-5"}
    # the to_dict shape carries the examples-but-not-support-list framing
    rendered = evidence.to_dict()
    assert rendered["available"] is True
    assert rendered["example_families"] == ["claude-opus-4-8", "gpt-5"]
    assert rendered["syntax_example"].startswith("claude-opus-4-8[")


def test_parameterized_syntax_absent_without_bracket_evidence():
    evidence = model_catalog.parse_parameterized_syntax({
        "agent --help": "--model <model> effort= --resume\n",
    })
    assert evidence.available is False
    assert evidence.param_keys == ()
    assert evidence.example_families == ()
    assert evidence.syntax_example is None


def test_parameterized_syntax_ignores_malformed_brackets():
    evidence = model_catalog.parse_parameterized_syntax({
        "text": "weird[not-params] other[=x] third[a=b,c] good[a=b,c=d]\n",
    })
    # brackets with any malformed segment are not trusted as evidence
    assert evidence.available is True
    assert set(evidence.param_keys) == {"a", "c"}
    assert evidence.example_families == ("good",)


# -- codex: JSON catalog ------------------------------------------------------


def test_codex_fixture_parses_supported_reasoning_levels_and_tiers():
    entries = model_catalog.parse_codex_models_json(codex_models_text())
    assert [entry.slug for entry in entries] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-5.5"]
    sol = entries[0]
    # the CLI's own vocabulary and order, including "ultra" (outside the
    # ORX profile effort vocabulary) — ground truth, not filtered
    assert sol.efforts == ("low", "medium", "high", "xhigh", "max", "ultra")
    assert sol.default_effort == "low"
    assert sol.speeds == ("standard", "fast")
    assert sol.context_tokens == 272000
    assert sol.max_context_tokens == 872000
    assert sol.visibility == "list"
    assert sol.display_name == "GPT-6.1-Sol"
    assert sol.description and sol.description.startswith("Latest workhorse")
    assert sol.is_current is False and sol.is_default is False
    luna = entries[1]
    assert luna.efforts == ("low", "medium", "high", "xhigh", "max")
    assert luna.default_effort == "medium"
    hidden = entries[2]
    assert hidden.visibility == "hide"
    assert hidden.max_context_tokens == 272000


def test_codex_invalid_json_is_discovery_failure():
    with pytest.raises(model_catalog.CatalogDiscoveryError, match="not valid JSON"):
        model_catalog.parse_codex_models_json("boom\n")


@pytest.mark.parametrize("payload", [
    "[]",
    '{"models": "nope"}',
    '{"nope": []}',
    '"just a string"',
    "null",
])
def test_codex_wrong_structure_is_discovery_failure(payload):
    with pytest.raises(model_catalog.CatalogDiscoveryError, match="'models' list"):
        model_catalog.parse_codex_models_json(payload)


def test_codex_entries_without_slug_are_skipped():
    entries = model_catalog.parse_codex_models_json(
        '{"models": [{"slug": "good-one"}, {"display_name": "no slug"}, "junk", 3]}'
    )
    assert [entry.slug for entry in entries] == ["good-one"]


def test_codex_all_entries_malformed_is_failure_not_empty_success():
    with pytest.raises(model_catalog.CatalogDiscoveryError, match="none had a parsable slug"):
        model_catalog.parse_codex_models_json('{"models": [{"x": 1}, {"y": 2}]}')


def test_codex_valid_empty_catalog_is_not_an_error():
    assert model_catalog.parse_codex_models_json('{"models": []}') == []


def test_codex_tolerates_unknown_fields_and_bad_optionals():
    entries = model_catalog.parse_codex_models_json(json.dumps({
        "models": [{
            "slug": "m1",
            "future_field": {"anything": True},
            "supported_reasoning_levels": [{"effort": "low"}, {"nope": 1}, "bad"],
            "additional_speed_tiers": ["fast", "fast", 5, None],
            "default_reasoning_level": "high",
            "context_window": 0,
            "max_context_window": "huge",
            "display_name": "  ",
            "description": "ok",
        }]
    }))
    entry = entries[0]
    assert entry.efforts == ("low",)
    assert entry.default_effort == "high"
    assert entry.speeds == ("standard", "fast")
    assert entry.context_tokens is None
    assert entry.max_context_tokens is None
    assert entry.display_name is None
    assert entry.description == "ok"


def test_codex_large_catalog_is_parsed_in_full():
    # the real codex dump is >256 KiB; a catalog this size must parse
    # completely (no stream cap applies to the probe's output reading)
    models = [
        {
            "slug": f"gen-family-{i:04d}",
            "display_name": f"Gen {i:04d}",
            "description": "x" * 80,
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "high")],
            "context_window": 272000,
        }
        for i in range(1200)
    ]
    payload = json.dumps({"models": models})
    assert len(payload) > 256 * 1024
    entries = model_catalog.parse_codex_models_json(payload)
    assert len(entries) == 1200
    assert entries[0].slug == "gen-family-0000"
    assert entries[-1].slug == "gen-family-1199"


# -- catalog container, generation, and cache --------------------------------


def test_model_catalog_round_trip_and_derived_views():
    catalog = model_catalog.build_catalog(
        harness="cursor",
        source="agent --list-models",
        models=model_catalog.parse_cursor_list_models(cursor_list_text()),
        generation=3,
        parameterized=model_catalog.parse_parameterized_syntax({
            "agent --help": agent_help_text(),
        }),
    )
    assert catalog.current_slug == "grok-4.7-high"
    assert catalog.default_slug == "auto"
    assert catalog.families[0] == "auto" and "grok-4.7" in catalog.families
    revived = model_catalog.ModelCatalog.from_dict(json.loads(json.dumps(catalog.to_dict())))
    assert revived == catalog


def test_next_generation_starts_at_one_and_increments():
    assert model_catalog.next_generation(None) == 1
    # pre-catalog (old-format) snapshots have no generation: start at 1
    old_format = {
        "harness": "codex", "binary": "/x/codex", "version": None,
        "probed_at": "2026-01-01T00:00:00+00:00", "features": {},
        "auth": "unknown", "models_discoverable": True,
    }
    assert model_catalog.next_generation(old_format) == 1
    minimal = {
        "harness": "codex", "source": "codex debug models",
        "generated_at": "2026-10-07T00:00:00+00:00", "generation": 4,
        "models": [], "parameterized": None,
    }
    assert model_catalog.next_generation({"model_catalog": minimal}) == 5
    # unreadable catalog data is absent, not garbage
    assert model_catalog.next_generation({"model_catalog": {"models": "junk"}}) == 1


def test_catalog_from_snapshot_tolerates_old_and_corrupt_snapshots():
    # pre-G008 snapshot: no catalog key -> None, no error
    assert model_catalog.catalog_from_snapshot({"harness": "codex"}) is None
    assert model_catalog.catalog_from_snapshot(None) is None
    # corrupt catalog payload -> None (unknown, never fabricated)
    assert model_catalog.catalog_from_snapshot({"model_catalog": {"models": 3}}) is None


def _write_snapshot(harness: str, snapshot: dict, tmp_path: Path) -> None:
    path = tmp_path / "data" / "probes" / f"{harness}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot))


def _snapshot_with_catalog(harness: str, generation: int) -> dict:
    return {
        "harness": harness, "binary": "/x", "version": "v",
        "probed_at": "2026-10-07T00:00:00+00:00",
        "features": {}, "auth": "logged_in", "models_discoverable": True,
        "model_catalog": model_catalog.build_catalog(
            harness=harness, source="src",
            models=model_catalog.parse_cursor_list_models("m-a - A\nm-b - B\n"),
            generation=generation,
        ).to_dict(),
        "catalog_error": None,
    }


def test_cached_catalog_tracks_persisted_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "data"))
    model_catalog.invalidate()
    _write_snapshot("cursor", _snapshot_with_catalog("cursor", 1), tmp_path)
    first = model_catalog.cached_catalog("cursor")
    assert first is not None and first.generation == 1
    # a successful refresh (generation 2 persisted) invalidates the
    # same-process cache: the next read serves the new generation
    _write_snapshot("cursor", _snapshot_with_catalog("cursor", 2), tmp_path)
    second = model_catalog.cached_catalog("cursor")
    assert second is not None and second.generation == 2
    assert second is not first
    # a failed refresh persists a snapshot without a catalog: the stale
    # cached entry must go, and the read reports no catalog
    _write_snapshot("cursor", {
        "harness": "cursor", "binary": "/x", "version": "v",
        "probed_at": "2026-10-07T01:00:00+00:00", "features": {},
        "auth": "logged_in", "models_discoverable": False,
        "model_catalog": None, "catalog_error": "models probe exited 1",
    }, tmp_path)
    assert model_catalog.cached_catalog("cursor") is None


def test_cached_catalog_old_format_snapshot_reads_as_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "data"))
    model_catalog.invalidate()
    _write_snapshot("codex", {
        "harness": "codex", "binary": "/x/codex", "version": "codex-cli 0.160.0",
        "probed_at": "2026-01-01T00:00:00+00:00",
        "features": {"headless": True}, "auth": "logged_in",
        "models_discoverable": True,
    }, tmp_path)
    assert model_catalog.cached_catalog("codex") is None


def test_invalidate_drops_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "data"))
    _write_snapshot("cursor", _snapshot_with_catalog("cursor", 7), tmp_path)
    assert model_catalog.cached_catalog("cursor") is not None
    model_catalog.invalidate("cursor")
    _ = model_catalog._CACHE  # explicit invalidation removed the entry
    assert "cursor" not in model_catalog._CACHE
    assert model_catalog.cached_catalog("cursor") is not None  # re-reads disk


def test_catalog_carries_no_hardcoded_model_names():
    """The parsers must not know any model: every name comes from input."""
    source = (Path(__file__).parent.parent / "src" / "orx" / "model_catalog.py").read_text()
    for baked in ("grok", "claude", "gpt-", "kimi", "composer", "glm", "astra", "sonnet"):
        assert baked not in source.lower(), f"hardcoded model fragment {baked!r}"
