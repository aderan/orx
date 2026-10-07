"""Semantic model selection: the ModelSpec data contract (G008).

ORX model selection moves from bare CLI slug strings to a semantic
capability tuple. Configuration *declares intent* — provider, family,
version, effort, speed, context window, optional capability requirements —
and the runtime resolution layer (adapter-aware, fed by a refreshable
model catalog) absorbs the CLI-specific spelling: baked slugs,
parameterized bracket syntax, catalog drift. This module is the data
contract and its normalization rules only. It never parses, validates, or
synthesizes CLI slugs: a :class:`ModelSpec` built from a raw string id
keeps that string opaque, brackets included.

Identity
    A spec is identified by ``family`` + optional ``version``, or by a
    semantic ``id`` string — never both. :attr:`ModelSpec.identity`
    renders the stable string form used for display and records:
    ``provider/family@version``, ``family@version``, ``family``, or the
    raw ``id`` (a provider never prefixes an id — ids are self-contained).

Version policy (fixed vs explicitly floating)
    * a concrete ``version`` (e.g. ``"5.5"``) pins the family: resolution
      must map to that exact version or fail;
    * an omitted ``version`` — or the explicit floating spelling
      ``version = "latest"`` — floats: the family resolves against the
      refreshable catalog at launch time. Both spellings normalize to
      ``version=None`` so every stored spec has one canonical form.

Effort unification
    The profile-level ``effort`` is the single authoritative source. A
    model table may restate it, but only identically — a differing
    restatement is a conflict error, never a silent override.

Speed and context normalization
    ``speed`` accepts ``"standard" | "fast"``; ``fast = <bool>`` is an
    alias spelling of the same dimension and the two may not both be
    declared. ``context`` accepts a positive integer, a digit string, or
    a size string with a ``k``/``m`` suffix (``"1m"``, ``"128K"``,
    ``"1.5m"``) and normalizes to a canonical positive token count.
    Anything else is an error, never a guess.

Loading boundary
    :func:`parse_model_spec` is the single entry the config loader uses
    for semantic model tables (a TOML table, or an already-normalized
    dict such as a round-tripped :meth:`ModelSpec.to_dict`). It applies
    the loading rules — one declaration site per dimension, unknown
    fields rejected, unambiguous identity, normalized speed/context — and
    raises :class:`ModelSpecError` messages that name the ``model.*``
    field; the loader prepends the file and profile location. This module
    deliberately imports nothing from the rest of ``orx``: it is a leaf
    so the contract has no import cycles and no CLI knowledge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# The ORX effort vocabulary. Kept in lockstep with records.Effort (the
# enum mirrors these values; config derives its message vocabulary from
# here so there is exactly one list).
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Speed dimension vocabulary: the "-fast" CLI variants map to "fast";
# everything else is "standard". ``fast = <bool>`` is an accepted alias.
SPEEDS: tuple[str, ...] = ("standard", "fast")

# Explicit floating-version spelling at the config surface; every
# construction path canonicalizes it to ``version=None``.
FLOATING_VERSION = "latest"

# Fields a semantic model table accepts. Anything else is an error naming
# the offending field — unknown keys never pass through silently.
MODEL_TABLE_FIELDS: tuple[str, ...] = (
    "provider",
    "family",
    "version",
    "id",
    "effort",
    "speed",
    "fast",
    "context",
    "capabilities",
)

_CONTEXT_SIZE = re.compile(r"[0-9]+(?:\.[0-9]+)?[kKmM]?")
_CONTEXT_MULTIPLIER = {"": 1, "k": 1_000, "m": 1_000_000}

# dataclass field name -> the ``model.*`` spelling used in error messages.
_FIELD_NAMES = {
    "provider": "model.provider",
    "family": "model.family",
    "version": "model.version",
    "model_id": "model.id",
    "effort": "model.effort",
    "speed": "model.speed",
    "context_tokens": "model.context",
    "capabilities": "model.capabilities",
}


class ModelSpecError(ValueError):
    """One field-level problem in a model declaration.

    Messages name the ``model.*`` field they concern; the config loader
    catches this and prepends the file/profile location, so every surfaced
    error carries both the profile and the field position.
    """


def _optional_string(data: dict, key: str) -> str | None:
    """One string field from a model table: absent stays None; a present
    value must be a non-empty string."""
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ModelSpecError(f"model.{key} must be a non-empty string")
    return value


def normalize_context(value) -> int | None:
    """Canonical context-window size in tokens, or None when absent.

    Accepted spellings: a positive integer, a digit string, or a size
    string with a ``k``/``m`` suffix (case-insensitive, optional decimal
    such as ``"1.5m"``). Booleans, floats, negatives, zero, and other
    suffixes are errors — never coerced, never guessed.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ModelSpecError(
            "model.context must be a positive token count or a size string like '1m' or '128k'"
        )
    if isinstance(value, int):
        if value <= 0:
            raise ModelSpecError(
                f"model.context {value!r} invalid (expected a positive token count)"
            )
        return value
    match = _CONTEXT_SIZE.fullmatch(value.strip())
    if match is None:
        raise ModelSpecError(
            f"model.context {value!r} invalid "
            "(expected a positive token count or a size string like '1m' or '128k')"
        )
    digits = match.group(0)
    suffix = ""
    if digits[-1] in "kKmM":
        suffix = digits[-1].lower()
        digits = digits[:-1]
    tokens = int(round(float(digits) * _CONTEXT_MULTIPLIER[suffix]))
    if tokens <= 0:
        raise ModelSpecError(
            f"model.context {value!r} invalid (expected a positive token count)"
        )
    return tokens


@dataclass(frozen=True)
class ModelSpec:
    """A semantic model declaration — the capability tuple, not a slug.

    ``family`` + ``version`` (or a semantic ``id``) identify the model;
    ``effort``/``speed``/``context_tokens``/``capabilities`` are the
    requested dimensions. The spec carries no CLI spelling: turning it
    into a harness ``--model`` argument is the runtime resolution
    layer's job (:class:`ResolvedModel` records what it decided).
    """

    provider: str | None = None
    family: str | None = None
    version: str | None = None
    model_id: str | None = None
    effort: str | None = None
    speed: str | None = None
    context_tokens: int | None = None
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Canonicalize the explicit floating spelling on every
        # construction path: "latest" and omitted mean the same thing.
        if isinstance(self.version, str) and self.version == FLOATING_VERSION:
            object.__setattr__(self, "version", None)
        for attr in ("provider", "family", "version", "model_id"):
            value = getattr(self, attr)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ModelSpecError(f"{_FIELD_NAMES[attr]} must be a non-empty string")
        if self.model_id is not None and self.family is not None:
            raise ModelSpecError(
                "model identity is ambiguous: declare either model.id or model.family, not both"
            )
        if self.model_id is not None and self.version is not None:
            raise ModelSpecError(
                "model identity is ambiguous: model.id cannot be combined with model.version"
            )
        if self.version is not None and self.family is None:
            raise ModelSpecError("model.version requires model.family")
        if self.family is None and self.model_id is None:
            raise ModelSpecError("model must declare either family or id")
        if self.effort is not None and self.effort not in EFFORTS:
            raise ModelSpecError(
                f"model.effort {self.effort!r} invalid (expected {' | '.join(EFFORTS)})"
            )
        if self.speed is not None and self.speed not in SPEEDS:
            raise ModelSpecError(
                f"model.speed {self.speed!r} invalid (expected {' | '.join(SPEEDS)})"
            )
        if self.context_tokens is not None and (
            isinstance(self.context_tokens, bool)
            or not isinstance(self.context_tokens, int)
            or self.context_tokens <= 0
        ):
            raise ModelSpecError(
                "model.context must be a positive token count or a size string like '1m' or '128k'"
            )
        if not all(isinstance(c, str) and c.strip() for c in self.capabilities):
            raise ModelSpecError("model.capabilities must be a list of non-empty strings")

    @property
    def identity(self) -> str:
        """The stable string form of this declaration.

        ``id`` renders verbatim (opaque, never parsed); ``family`` renders
        as ``provider/family@version`` / ``family@version`` / ``family``
        depending on what is declared. Config surfaces use this string
        for display and records while keeping the full spec alongside.
        """
        if self.model_id is not None:
            return self.model_id
        base = self.family or ""
        if self.provider:
            base = f"{self.provider}/{base}"
        if self.version:
            base = f"{base}@{self.version}"
        return base

    def to_dict(self) -> dict:
        """JSON-expressible form: a fixed key set, str/None/int/list values
        only — no dataclass or TOML-typed value survives serialization.
        Keys use the declaration-surface vocabulary (``context`` holds the
        normalized token count), so the output re-parses through
        :func:`parse_model_spec` unchanged."""
        return {
            "provider": self.provider,
            "family": self.family,
            "version": self.version,
            "id": self.model_id,
            "effort": self.effort,
            "speed": self.speed,
            "context": self.context_tokens,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class ResolvedModel:
    """What the runtime resolution layer produces from a :class:`ModelSpec`.

    Contract only at this stage — adapters populate it in later G008
    stages. ``cli_slug`` is the exact model argument for the harness CLI;
    ``strategy`` records how it was derived (adapter vocabulary, e.g.
    bracket synthesis, catalog-variant rewrite, bare enumeration);
    ``bracket_params`` are the parameterized dimensions actually encoded,
    as ordered key/value pairs; ``catalog_generation`` names the
    refreshable catalog snapshot the slug was resolved against (None when
    no catalog was involved). Instances are records of a decision, never
    inputs that re-enter configuration.
    """

    spec: ModelSpec
    cli_slug: str
    strategy: str
    bracket_params: tuple[tuple[str, str], ...] = ()
    catalog_generation: int | None = None

    def to_dict(self) -> dict:
        return {
            "spec": self.spec.to_dict(),
            "cli_slug": self.cli_slug,
            "strategy": self.strategy,
            "bracket_params": [[key, value] for key, value in self.bracket_params],
            "catalog_generation": self.catalog_generation,
        }


def parse_model_spec(raw: Mapping, *, profile_effort: str | None = None) -> ModelSpec:
    """Validate one semantic model declaration into a :class:`ModelSpec`.

    ``raw`` is the model table from TOML (or an already-serialized dict —
    None values are treated as absent so ``to_dict`` output round-trips).
    ``profile_effort`` is the profile-level effort, the single
    authoritative source: a model-table effort must be identical to it,
    and the unified value lands on the spec. Raises
    :class:`ModelSpecError` naming the ``model.*`` field on: unknown
    fields, duplicate dimension declarations (``speed`` + ``fast``),
    effort conflicts, invalid effort/speed/context values, wrong types,
    and ambiguous identity.
    """
    if not isinstance(raw, Mapping):
        raise ModelSpecError("model must be a table when declared semantically")
    data = {key: value for key, value in raw.items() if value is not None}

    unknown = [key for key in data if key not in MODEL_TABLE_FIELDS]
    if unknown:
        allowed = ", ".join(MODEL_TABLE_FIELDS)
        raise ModelSpecError(
            f"model.{unknown[0]} is not a recognized model field (allowed: {allowed})"
        )

    # speed / fast: two spellings of one dimension — never both.
    if "speed" in data and "fast" in data:
        raise ModelSpecError(
            "model.fast conflicts with model.speed (declare the speed dimension once)"
        )
    speed: str | None = None
    if "speed" in data:
        speed = data["speed"]
        if not isinstance(speed, str) or speed not in SPEEDS:
            raise ModelSpecError(
                f"model.speed {data['speed']!r} invalid (expected {' | '.join(SPEEDS)})"
            )
    elif "fast" in data:
        fast = data["fast"]
        if not isinstance(fast, bool):
            raise ModelSpecError("model.fast must be a boolean")
        speed = "fast" if fast else "standard"

    # effort: one source. The profile-level effort is authoritative; a
    # model-table restatement must be identical.
    effort = data.get("effort")
    if effort is not None:
        if not isinstance(effort, str) or effort not in EFFORTS:
            raise ModelSpecError(
                f"model.effort {data['effort']!r} invalid (expected {' | '.join(EFFORTS)})"
            )
        if profile_effort is not None and effort != profile_effort:
            raise ModelSpecError(
                f"model.effort {effort!r} conflicts with effort {profile_effort!r} "
                "(declare the effort once; the profile-level effort is authoritative)"
            )
    elif profile_effort is not None:
        effort = profile_effort

    context_tokens = normalize_context(data.get("context"))

    capabilities = data.get("capabilities")
    if capabilities is not None:
        if not isinstance(capabilities, (list, tuple)) or not all(
            isinstance(c, str) and c.strip() for c in capabilities
        ):
            raise ModelSpecError("model.capabilities must be a list of non-empty strings")
        capabilities = tuple(capabilities)

    return ModelSpec(
        provider=_optional_string(data, "provider"),
        family=_optional_string(data, "family"),
        version=_optional_string(data, "version"),
        model_id=_optional_string(data, "id"),
        effort=effort,
        speed=speed,
        context_tokens=context_tokens,
        capabilities=capabilities or (),
    )
