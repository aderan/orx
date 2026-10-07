"""The refreshable model catalog (G008 T002): structured capability
discovery from local CLI output.

Probes feed this module raw text from free, local, programmable channels
only — Cursor: ``agent --list-models`` plus ``agent --help``; Codex:
``codex debug models`` — and it parses that output into an explicit
catalog of :class:`CatalogEntry` records: enumerated slugs, effort and
fast variants, current/default selection markers, and (Codex) the
``supported_reasoning_levels`` list. Nothing here is hardcoded: a model
exists in the catalog because the local CLI printed it, and catalog
parsing never launches a completion and never touches the network.

Division of labor
    * ``orx.models`` — the ModelSpec data contract (semantic intent).
    * this module — parsing local CLI output into catalog records, the
      persisted catalog container, and a process-level read cache.
    * ``orx.probes`` — running the probe commands, deciding success or
      failure, and persisting the catalog inside the capability snapshot
      at ``<user data dir>/probes/<harness>.json`` (the one persistence
      location; this module never writes files).

Parsing rules (Cursor text)
    Output lines are cleaned of ANSI escape sequences and zero-width
    characters first; a header ("Available models"), blank lines, and any
    line that is not ``<slug> - <description>`` with a slug-shaped first
    token are not models and never become catalog entries. ``(current)``
    and ``(default)`` trailing markers on the description become entry
    flags. The ``-fast`` suffix and a trailing ``-<effort>`` token
    (ORX effort vocabulary, longest match first) decode the speed/effort
    variant the slug names; the raw slug is always kept alongside, and
    family decoding is a documented derivation from the naming
    convention, never additional knowledge.

Parsing rules (Codex JSON)
    ``codex debug models`` prints one large JSON object (observed
    >256 KiB; the probe reads it without any stream cap). The document
    must be valid JSON shaped ``{"models": [...]}`` — anything else is a
    discovery failure, as is a non-zero exit or a timeout upstream. An
    entry contributes a catalog record when it is an object with a
    slug-shaped ``slug``; entries without one are skipped, but a list
    that yielded no parsable entry at all is a failure, not an empty
    success. Effort vocabulary is recorded exactly as the CLI prints it
    (Codex includes ``ultra``, which is beyond the ORX profile
    vocabulary) — the catalog is ground truth, adapters translate.

Parameterized syntax (Cursor) is kept strictly separate from the
enumerated models. Bracket examples such as
``<family>[context=1m,effort=high,fast=false]`` found in
``--help`` or the ``--list-models`` tip line are evidence that the
bracket parameter channel exists and which parameter keys it accepts;
the example families are examples of the syntax, not an enumeration of
supported parameterized models. The catalog therefore never lists them
under ``models``.

Failure and refresh semantics
    * A *discovery failure* (binary missing, non-zero exit, timeout,
      unparseable output) is recorded by the probe as a ``catalog_error``
      with ``model_catalog`` null; a *valid empty catalog* (exit 0,
      parseable, zero models) is a success with ``models: []``. The two
      states are distinguishable in the snapshot.
    * Authentication is orthogonal: a not-logged-in harness may still
      enumerate models, and an auth failure never fabricates a catalog —
      whatever the models probe returned is what was parsed.
    * Snapshots written before this mechanism existed (no
      ``model_catalog`` key) remain readable: reading one yields no
      catalog, not an error.
    * A failed refresh never relabels the old catalog as a fresh
      success: the refreshed snapshot either carries a genuinely new
      catalog (with a new ``generated_at`` and a bumped ``generation``)
      or carries no catalog plus the failure reason.
    * ``generation`` is a per-harness integer that starts at 1 and
      increments on every successful catalog refresh. The process-level
      cache (:func:`cached_catalog`) serves a catalog only while the
      persisted generation matches, so a successful refresh in the same
      process invalidates previously cached catalogs automatically (the
      probe also calls :func:`invalidate` when it persists).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from orx.models import EFFORTS

# Effort suffixes for Cursor slug decoding, longest first so "xhigh" is
# not misread as "high". Drawn from the single ORX effort vocabulary.
_EFFORT_SUFFIXES: tuple[str, ...] = tuple(
    sorted(EFFORTS, key=len, reverse=True)
)

# A slug-shaped token: how both harnesses spell model ids in local output.
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")

# One catalog line from `agent --list-models`: "<slug> - <description>".
# The separator makes headers, blank lines, tips, and prose non-models.
_CURSOR_LINE_RE = re.compile(
    r"^(?P<slug>[A-Za-z0-9][A-Za-z0-9._+-]*)\s+-\s+(?P<desc>.*)$"
)

# ANSI escape sequences (CSI/OSC/single-char) that some CLIs emit even
# off-tty; stripped before any line is examined.
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)

# Zero-width and BOM characters scrubbed from lines and slugs.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))

# A bracket parameterized model example: family[ key=value, key=value ].
# The bracket-delimited parameter section is searched FIRST (a linear
# scan for the literal brackets) and the family token is then read by a
# bounded backward scan — a regex that matches family-then-bracket
# backtracks quadratically on a long slug-shaped token run with no
# bracket after it (measured: 110 s on a 200 KB run), and launch-time
# discovery feeds this parser arbitrary CLI output.
_BRACKET_PARAMS_RE = re.compile(r"\[([^\[\]'\"]+)\]")
_BRACKET_PARAM_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$")
_FAMILY_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-"
)
# Families are short CLI tokens; the bound only guards pathological
# input (a longer run before a bracket is not a family name).
_FAMILY_MAX = 128

_CURRENT_MARKER = "(current)"
_DEFAULT_MARKER = "(default)"


class CatalogDiscoveryError(Exception):
    """Local model catalog output could not be parsed into a catalog.

    Raised for structurally invalid output (Codex: non-JSON or wrong
    shape). The probe records the message as ``catalog_error``; it never
    propagates out of a probe as a crash and never produces a catalog.
    """


def _clean_line(line: str) -> str:
    """One output line reduced to comparable text: no ANSI escapes, no
    zero-width characters, no surrounding whitespace."""
    return _ANSI_RE.sub("", line).translate(_ZERO_WIDTH).strip()


def _marker(desc: str, marker: str) -> tuple[bool, str]:
    """Whether ``desc`` ends with ``marker``; the description without it."""
    if desc.endswith(marker):
        return True, desc[: -len(marker)].rstrip()
    return False, desc


def decode_cursor_slug(slug: str) -> tuple[str, str | None, str]:
    """Decode a Cursor slug into (family, effort, speed).

    Documented derivation from the CLI's naming convention: a trailing
    ``-fast`` names the fast speed variant; a further trailing
    ``-<effort>`` token from the ORX effort vocabulary (longest match
    first) names the effort variant. The catalog always keeps the raw
    slug, so decoding adds structure without discarding anything.
    """
    speed = "standard"
    base = slug
    if base.endswith("-fast"):
        base = base[: -len("-fast")]
        speed = "fast"
    effort = None
    for token in _EFFORT_SUFFIXES:
        if base.endswith("-" + token):
            effort = token
            base = base[: -(len(token) + 1)]
            break
    return base, effort, speed


def parse_cursor_list_models(text: str) -> list[CatalogEntry]:
    """Parse ``agent --list-models`` text into catalog entries.

    Only ``<slug> - <description>`` lines with a slug-shaped first token
    become entries; headers, blank lines, ANSI/zero-width noise, tip
    lines, and other prose never do. ``(current)`` / ``(default)``
    description markers become entry flags. Duplicate slugs keep the
    first occurrence.
    """
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        match = _CURSOR_LINE_RE.match(line)
        if match is None:
            continue
        slug = match.group("slug")
        if _SLUG_RE.fullmatch(slug) is None:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        desc = match.group("desc").strip()
        is_current, desc = _marker(desc, _CURRENT_MARKER)
        is_default, desc = _marker(desc, _DEFAULT_MARKER)
        family, effort, speed = decode_cursor_slug(slug)
        entries.append(
            CatalogEntry(
                slug=slug,
                display_name=None,
                description=desc or None,
                family=family,
                effort=effort,
                speeds=(speed,),
                is_current=is_current,
                is_default=is_default,
            )
        )
    return entries


def parse_parameterized_syntax(texts: Mapping[str, str]) -> ParameterizedSyntax:
    """Extract parameterized bracket-syntax evidence from labeled texts.

    ``texts`` maps a source label (e.g. ``"agent --help"``) to its text.
    A bracket example like ``<family>[context=1m,effort=high,
    fast=false]`` is evidence that the parameter channel exists and
    which keys it accepts. Example families are recorded as examples of
    the syntax only — they are not enumerated models and never enter the
    catalog's ``models`` list; absence of bracket evidence means
    ``available=False``, never an invented family list.
    """
    example: str | None = None
    param_keys: list[str] = []
    families: list[str] = []
    sources: list[str] = []
    for label, text in texts.items():
        found_here = False
        for match in _BRACKET_PARAMS_RE.finditer(text):
            params_text = match.group(1)
            keys = []
            ok = True
            for part in params_text.split(","):
                part = part.strip()
                param = _BRACKET_PARAM_RE.match(part)
                if param is None:
                    ok = False
                    break
                keys.append(param.group(1))
            if not ok or not keys:
                continue
            # The family token ends immediately before '['; scan backward
            # over slug characters, bounded (see _FAMILY_MAX).
            start = match.start()
            index = start
            floor = max(0, start - _FAMILY_MAX)
            while index > floor and text[index - 1] in _FAMILY_CHARS:
                index -= 1
            family = text[index:start]
            if not family or not family[0].isalnum():
                continue
            found_here = True
            if example is None:
                example = f"{family}[{params_text.strip()}]"
            for key in keys:
                if key not in param_keys:
                    param_keys.append(key)
            if family not in families:
                families.append(family)
        if found_here and label not in sources:
            sources.append(label)
    return ParameterizedSyntax(
        available=example is not None,
        syntax_example=example,
        param_keys=tuple(param_keys),
        example_families=tuple(families),
        sources=tuple(sources),
    )


def parse_codex_models_json(text: str) -> list[CatalogEntry]:
    """Parse ``codex debug models`` JSON into catalog entries.

    Valid JSON shaped ``{"models": [...]}`` is required; a non-dict
    document, a missing or non-list ``models``, or a list whose entries
    all lack a parsable slug is a :class:`CatalogDiscoveryError` — a
    malformed or partial dump never becomes a successful catalog. An
    empty ``models`` list is a valid empty catalog. Individual entries
    without a slug are skipped (catalog evolution); effort levels are
    kept in the CLI's own vocabulary and order.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CatalogDiscoveryError(
            f"model catalog output is not valid JSON ({exc.msg} at line {exc.lineno})"
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise CatalogDiscoveryError(
            "model catalog JSON must be an object with a 'models' list"
        )
    entries: list[CatalogEntry] = []
    for raw in data["models"]:
        if not isinstance(raw, dict):
            continue
        slug = raw.get("slug")
        if not isinstance(slug, str) or _SLUG_RE.fullmatch(slug.strip()) is None:
            continue
        levels = raw.get("supported_reasoning_levels")
        efforts: list[str] = []
        if isinstance(levels, list):
            for level in levels:
                if isinstance(level, dict):
                    effort = level.get("effort")
                    if isinstance(effort, str) and effort.strip():
                        efforts.append(effort.strip())
        speeds = ["standard"]
        tiers = raw.get("additional_speed_tiers")
        if isinstance(tiers, list):
            for tier in tiers:
                if isinstance(tier, str) and tier.strip() and tier not in speeds:
                    speeds.append(tier)
        default_effort = raw.get("default_reasoning_level")
        if not isinstance(default_effort, str) or not default_effort.strip():
            default_effort = None
        entries.append(
            CatalogEntry(
                slug=slug.strip(),
                display_name=_optional_str(raw.get("display_name")),
                description=_optional_str(raw.get("description")),
                efforts=tuple(efforts),
                default_effort=default_effort,
                speeds=tuple(speeds),
                context_tokens=_positive_int(raw.get("context_window")),
                max_context_tokens=_positive_int(raw.get("max_context_window")),
                visibility=_optional_str(raw.get("visibility")),
            )
        )
    if not entries and data["models"]:
        raise CatalogDiscoveryError(
            f"model catalog listed {len(data['models'])} entries but none had a parsable slug"
        )
    return entries


def _optional_str(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass(frozen=True)
class CatalogEntry:
    """One model as the local CLI reported it, plus safe derivations.

    ``slug`` is the exact CLI spelling. Cursor entries carry the decoded
    ``family``/``effort`` variant and the ``speeds`` the slug names;
    Codex entries carry the CLI's ``efforts`` list (own vocabulary,
    own order), ``default_effort``, speed tiers, context windows, and
    visibility. ``is_current``/``is_default`` mirror the CLI's own
    selection markers (Cursor "(current)"/"(default)"; Codex prints
    none). Every field is either printed by the CLI or derived from it
    by a rule documented in this module — nothing is inferred from
    outside the probe output.
    """

    slug: str
    display_name: str | None = None
    description: str | None = None
    family: str | None = None
    effort: str | None = None
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    speeds: tuple[str, ...] = ("standard",)
    context_tokens: int | None = None
    max_context_tokens: int | None = None
    visibility: str | None = None
    is_current: bool = False
    is_default: bool = False

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "family": self.family,
            "effort": self.effort,
            "efforts": list(self.efforts),
            "default_effort": self.default_effort,
            "speeds": list(self.speeds),
            "context_tokens": self.context_tokens,
            "max_context_tokens": self.max_context_tokens,
            "visibility": self.visibility,
            "is_current": self.is_current,
            "is_default": self.is_default,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> CatalogEntry:
        if not isinstance(data, Mapping) or not isinstance(data.get("slug"), str):
            raise ValueError("catalog entry requires a string 'slug'")
        return cls(
            slug=data["slug"],
            display_name=_optional_str(data.get("display_name")),
            description=_optional_str(data.get("description")),
            family=_optional_str(data.get("family")),
            effort=_optional_str(data.get("effort")),
            efforts=tuple(
                e for e in _str_list(data.get("efforts")) if e
            ),
            default_effort=_optional_str(data.get("default_effort")),
            speeds=tuple(
                s for s in _str_list(data.get("speeds")) if s
            ) or ("standard",),
            context_tokens=_positive_int(data.get("context_tokens")),
            max_context_tokens=_positive_int(data.get("max_context_tokens")),
            visibility=_optional_str(data.get("visibility")),
            is_current=bool(data.get("is_current", False)),
            is_default=bool(data.get("is_default", False)),
        )


def _str_list(value) -> list[str]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


@dataclass(frozen=True)
class ParameterizedSyntax:
    """Bracket parameter-channel evidence, kept apart from models.

    ``syntax_example`` is a verbatim bracket example found in the probe
    texts; ``param_keys`` the parameter names it demonstrates;
    ``example_families`` the families those examples happened to use —
    examples of the syntax, never a claim that exactly these families
    are supported (the enumerated models list is the only support list).
    """

    available: bool = False
    syntax_example: str | None = None
    param_keys: tuple[str, ...] = ()
    example_families: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "syntax_example": self.syntax_example,
            "param_keys": list(self.param_keys),
            "example_families": list(self.example_families),
            "sources": list(self.sources),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> ParameterizedSyntax:
        if not isinstance(data, Mapping) or not data.get("available"):
            return cls()
        return cls(
            available=True,
            syntax_example=_optional_str(data.get("syntax_example")),
            param_keys=tuple(_str_list(data.get("param_keys"))),
            example_families=tuple(_str_list(data.get("example_families"))),
            sources=tuple(_str_list(data.get("sources"))),
        )


@dataclass(frozen=True)
class ModelCatalog:
    """A parsed, timestamped, generation-stamped model catalog.

    Persisted inside the capability snapshot by the probe (this module
    never writes). ``generation`` increments on every successful
    refresh, giving resolution layers a staleness handle
    (:attr:`orx.models.ResolvedModel.catalog_generation`).
    """

    harness: str
    source: str
    generated_at: str
    generation: int
    models: tuple[CatalogEntry, ...] = ()
    parameterized: ParameterizedSyntax | None = None

    @property
    def current_slug(self) -> str | None:
        """The slug the CLI marked "(current)", if any."""
        for entry in self.models:
            if entry.is_current:
                return entry.slug
        return None

    @property
    def default_slug(self) -> str | None:
        """The slug the CLI marked "(default)", if any."""
        for entry in self.models:
            if entry.is_default:
                return entry.slug
        return None

    @property
    def families(self) -> tuple[str, ...]:
        """Distinct families in first-seen order (slug when no family is
        decoded)."""
        out: list[str] = []
        for entry in self.models:
            name = entry.family or entry.slug
            if name not in out:
                out.append(name)
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "harness": self.harness,
            "source": self.source,
            "generated_at": self.generated_at,
            "generation": self.generation,
            "models": [entry.to_dict() for entry in self.models],
            "parameterized": (
                self.parameterized.to_dict()
                if self.parameterized is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> ModelCatalog:
        if not isinstance(data, Mapping):
            raise ValueError("catalog must be a mapping")
        models = data.get("models")
        if not isinstance(models, list):
            raise ValueError("catalog requires a 'models' list")
        generation = data.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("catalog requires an integer 'generation'")
        parameterized = data.get("parameterized")
        return cls(
            harness=data.get("harness") or "",
            source=data.get("source") or "",
            generated_at=data.get("generated_at") or "",
            generation=generation,
            models=tuple(CatalogEntry.from_dict(m) for m in models),
            parameterized=(
                ParameterizedSyntax.from_dict(parameterized)
                if isinstance(parameterized, Mapping)
                else None
            ),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_catalog(
    harness: str,
    source: str,
    models: list[CatalogEntry],
    generation: int,
    parameterized: ParameterizedSyntax | None = None,
    generated_at: str | None = None,
) -> ModelCatalog:
    """Assemble a catalog for persistence (timestamp defaulting to now)."""
    return ModelCatalog(
        harness=harness,
        source=source,
        generated_at=generated_at or _now(),
        generation=generation,
        models=tuple(models),
        parameterized=parameterized,
    )


def catalog_from_snapshot(snapshot: Mapping | None) -> ModelCatalog | None:
    """The catalog inside a capability snapshot, or None.

    Snapshots from before this mechanism (no ``model_catalog`` key) and
    snapshots whose catalog is unreadable both yield None — an absent or
    corrupt catalog is reported as unknown, never as an error state of
    the reader, and never as a fabricated catalog.
    """
    if not isinstance(snapshot, Mapping):
        return None
    raw = snapshot.get("model_catalog")
    if not isinstance(raw, Mapping):
        return None
    try:
        return ModelCatalog.from_dict(raw)
    except (ValueError, TypeError):
        return None


def next_generation(previous_snapshot: Mapping | None) -> int:
    """The generation for a new successful refresh.

    1 for a first refresh (or after a pre-catalog snapshot); otherwise
    the persisted generation plus one. Failed refreshes never consult or
    bump this — only a genuinely fresh catalog advances the generation.
    """
    previous = catalog_from_snapshot(previous_snapshot)
    if previous is None:
        return 1
    return previous.generation + 1


# ---------------------------------------------------------------------------
# Process-level read cache
#
# cached_catalog() mirrors the persisted snapshot: the cached object is
# served only while the persisted generation matches. A successful
# refresh (new generation persisted, or the catalog removed by a failed
# refresh) invalidates the cached entry on the next read; the probe also
# calls invalidate() eagerly when it persists.

_CACHE: dict[str, tuple[int, ModelCatalog]] = {}

# Launch-time discovery cache (G008 T003): the in-process result of the
# fallback probe run when no persisted catalog exists. One shared cache
# for every adapter — the per-adapter model-listing caches are gone.
# Cleared by the same invalidate() entry point.
_LAUNCH_CACHE: dict[str, ModelCatalog | None] = {}

# Generation stamp for a catalog that was discovered locally at launch
# time and never persisted: persisted generations start at 1, so 0
# always names "in-process discovery, not snapshot-backed".
LAUNCH_DISCOVERY_GENERATION = 0


def cached_catalog(harness: str) -> ModelCatalog | None:
    """The persisted catalog for ``harness``, cached per process.

    Reads the capability snapshot from disk and serves the cached object
    only while the persisted generation is unchanged — so a successful
    refresh by the same process (generation bumped) or a failed refresh
    (catalog gone) drops the stale cache entry automatically. Never
    probes, never writes, never launches a completion.
    """
    from orx import probes as _probes  # deferred: probes imports this module

    snapshot = _probes.load_snapshot(harness)
    catalog = catalog_from_snapshot(snapshot)
    generation = catalog.generation if catalog is not None else -1
    cached = _CACHE.get(harness)
    if cached is not None and cached[0] == generation:
        return cached[1]
    _CACHE.pop(harness, None)
    if catalog is None:
        return None
    _CACHE[harness] = (generation, catalog)
    return catalog


def invalidate(harness: str | None = None) -> None:
    """Drop cached catalog(s): one harness, or all when None.

    Clears both the generation-checked snapshot cache and the launch-time
    discovery cache, so every test/reset hook has one entry point.
    """
    if harness is None:
        _CACHE.clear()
        _LAUNCH_CACHE.clear()
    else:
        _CACHE.pop(harness, None)
        _LAUNCH_CACHE.pop(harness, None)


def launch_catalog(harness: str) -> ModelCatalog | None:
    """The one catalog source adapters resolve against at launch time.

    Precedence (G008 T003):

    1. the persisted capability snapshot's catalog, served through
       :func:`cached_catalog` — this is the user-controlled refresh
       surface (``orx agent probe``), and a persisted refresh therefore
       changes what the next launch resolves against;
    2. when no persisted catalog exists, ONE fresh local discovery via
       the same free probe the snapshot uses (the harness's models
       command, ``probes.MODELS_PROBE_TIMEOUT``), parsed by the same
       shared parsers, cached for the process. A launch never writes
       the user data dir — an implicit probe must not be able to wipe a
       previously persisted snapshot — so the in-process result carries
       :data:`LAUNCH_DISCOVERY_GENERATION` (0) and no timestamped
       persistence.

    Discovery failures (binary missing, non-zero exit, timeout, empty or
    unparseable output) yield None, cached per process. Never probes the
    network, never launches a completion.
    """
    persisted = cached_catalog(harness)
    if persisted is not None:
        return persisted
    if harness in _LAUNCH_CACHE:
        return _LAUNCH_CACHE[harness]
    from orx import probes as _probes  # deferred: probes imports this module

    catalog: ModelCatalog | None = None
    spec = _probes.HARNESSES.get(harness)
    if spec is not None and spec.models_argv is not None:
        code, text = _probes.probe_argv(
            list(spec.models_argv), timeout=_probes.MODELS_PROBE_TIMEOUT
        )
        if code == 0 and text.strip():
            try:
                entries = _probes._parse_catalog(spec, text)
            except CatalogDiscoveryError:
                entries = None
            if entries is not None:
                parameterized = None
                if spec.catalog_format == "cursor_text":
                    # Bracket evidence from the listing text; the adapter
                    # merges the help text it already fetched per launch.
                    parameterized = parse_parameterized_syntax(
                        {" ".join(spec.models_argv): text}
                    )
                catalog = build_catalog(
                    harness=harness,
                    source=" ".join(spec.models_argv),
                    models=entries,
                    generation=LAUNCH_DISCOVERY_GENERATION,
                    parameterized=parameterized,
                )
    _LAUNCH_CACHE[harness] = catalog
    return catalog


__all__ = [
    "CatalogDiscoveryError",
    "CatalogEntry",
    "LAUNCH_DISCOVERY_GENERATION",
    "ModelCatalog",
    "ParameterizedSyntax",
    "build_catalog",
    "cached_catalog",
    "catalog_from_snapshot",
    "decode_cursor_slug",
    "invalidate",
    "launch_catalog",
    "next_generation",
    "parse_codex_models_json",
    "parse_cursor_list_models",
    "parse_parameterized_syntax",
]
