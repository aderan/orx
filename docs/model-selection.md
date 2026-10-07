# Model selection — ModelSpec, the refreshable catalog, and per-adapter resolution

Status: active (2026-10-07, G008). This document is the *mechanism* reference
for how a profile's model declaration becomes a harness `--model` argument.
The *policy* layer — which class of model each role should use — is unchanged
and lives in [routing-strategy.md](routing-strategy.md); the config surface is
described in the [README](../README.md#profiles-orxprofilestoml).

## The three layers and their boundary

G008 splits model selection into three layers with one direction of flow:

1. **Configuration declares semantics** (`profiles.toml`). A profile states
   *what* it wants — family, version, effort, speed, context window,
   capability requirements — as a `ModelSpec`, or keeps a legacy bare string.
   Configuration never spells a CLI slug (the legacy form aside) and never
   encodes catalog knowledge.
2. **The catalog reports capability** (`src/orx/model_catalog.py`, fed by
   `src/orx/probes.py`). Only what the *local CLI itself prints* through free,
   programmable channels becomes catalog truth: Cursor `agent --list-models`
   plus `agent --help`, Codex `codex debug models`. Nothing is hardcoded, no
   network call is made, no completion is ever launched to discover models.
3. **Resolution happens at launch, per adapter** (`src/orx/adapters/`). The
   adapter turns the spec into the exact `--model` argument — absorbing the
   CLI's baked slugs, its parameterized bracket syntax, and catalog drift —
   or refuses with a diagnostic that names what was requested, what the
   catalog offers, and the refresh command. It never substitutes a different
   model, version, family, or `auto`.

A fact therefore always has exactly one home: *intent* lives in the spec,
*availability* lives in the catalog, *spelling* lives in the resolver. When
the three disagree, resolution fails loudly instead of guessing.

## The ModelSpec form

```toml
# orx-doc-example: semantic-population
# The semantic equivalents of the existing bare-slug population.
schema_version = 1

[profiles.sem-opus]
driver = "cli"
harness = "cursor"
model = { family = "claude-opus", version = "5.5" }
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.sem-gpt]
driver = "cli"
harness = "cursor"
model = { family = "gpt", version = "6.1-sol" }
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.sem-gpt-max]
driver = "cli"
harness = "cursor"
model = { family = "gpt", version = "6.1-sol", effort = "max" }
class = "frontier"
effort = "max"
capabilities = ["coding"]

[profiles.sem-grok]
driver = "cli"
harness = "cursor"
model = { family = "grok", version = "4.7" }
class = "strong"
effort = "xhigh"
capabilities = ["coding"]

[profiles.sem-grok-high]
driver = "cli"
harness = "cursor"
model = { family = "grok", version = "4.7" }
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.sem-composer]
driver = "cli"
harness = "cursor"
model = { family = "composer-2.5", speed = "fast" }
class = "economy"
effort = "high"
capabilities = ["coding"]
```

Field rules (`src/orx/models.py`, `parse_model_spec` is the single loader
entry):

| Field | Meaning | Rules |
|---|---|---|
| `provider` | optional namespace (display only today) | non-empty string; never prefixes an id |
| `family` + `version` | semantic identity | `version` requires `family`; a concrete version pins the family at resolution time |
| `id` | opaque identity (e.g. a Codex slug or a host account id) | either `id` or `family`, never both; kept verbatim, brackets included — the loader never parses it |
| `effort` | reasoning depth | `low \| medium \| high \| xhigh \| max`; the profile-level `effort` is authoritative — a model-table restatement must be identical or the load fails |
| `speed` / `fast` | one dimension, two spellings | `standard \| fast`; `fast = <bool>` is an alias; declaring both is an error |
| `context` | context window request | positive int, digit string, or size string (`"1m"`, `"128K"`, `"1.5m"`) normalized to a token count; anything else is an error |
| `capabilities` | capability requirements | list of non-empty strings; consumed by routing, not by the CLI |

Unknown fields are rejected by name. The **identity string**
(`provider/family@version`, `family@version`, `family`, or the raw `id`) is
what display and records carry; the full spec travels alongside it.

**Version policy — fixed vs explicitly floating.** A concrete `version`
(e.g. `"5.5"`) pins: resolution must map to exactly that version or fail. An
omitted version — or the explicit spelling `version = "latest"`, which
normalizes to the same thing — floats: the family resolves against the
refreshable catalog at launch. Both spellings are one canonical form
(`version=None`), so there is exactly one floating behavior, not two.

```toml
# orx-doc-example: floating-population
# Floating policy: version omitted (left) and version = "latest" (right)
# are the SAME policy. Loader-verified; resolution semantics in the text.
schema_version = 1

[profiles.float-grok]
driver = "cli"
harness = "cursor"
model = { family = "grok" }
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.float-grok-latest]
driver = "cli"
harness = "cursor"
model = { family = "grok", version = "latest" }
class = "strong"
effort = "high"
capabilities = ["coding"]
```

Floating is not "always newest": it follows the catalog only when the family
has **exactly one** version in it. On the 2026-10-07 capture the grok family
exists at three versions at once (see
[the grok evidence below](#grok-prefix-and-version-evolution)), so a floating
grok request is an explicit ambiguity diagnostic naming all three — never a
silent pick of the newest.

## Old configurations keep working (compatibility and migration)

The bare string form is unchanged and remains fully supported — same loader,
same launches, no hand-editing required:

```toml
# orx-doc-example: legacy-population
# The existing population: bare slugs, an account id, one bracket
# direct-write. All still load and launch exactly as before G008.
schema_version = 1

[profiles.legacy-opus]
driver = "cli"
harness = "cursor"
model = "claude-opus-5-5"
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-gpt]
driver = "cli"
harness = "cursor"
model = "gpt-6.1-sol"
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-grok]
driver = "cli"
harness = "cursor"
model = "grok-4.7"
class = "strong"
effort = "xhigh"
capabilities = ["coding"]

[profiles.legacy-composer]
driver = "cli"
harness = "cursor"
model = "composer-2.5"
class = "economy"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-account]
driver = "cli"
harness = "cursor"
model = "account:bigmodel-individual-coding-plan/GLM-5.3"
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-bracket]
driver = "cli"
harness = "cursor"
model = "gpt-6.1-sol[effort=max]"
class = "frontier"
effort = "max"
capabilities = ["coding"]
```

Migration is per-profile and representation-only. Equivalence (proven by
`tests/test_model_docs.py` against the replayed real capture — see the
[evidence register](#evidence-register-and-sample-marking)):

| Legacy declaration | Semantic equivalent | Same resolved target on the 2026-10-07 capture |
|---|---|---|
| `model = "claude-opus-5-5"` + `effort = "high"` | `model = { family = "claude-opus", version = "5.5" }` | `claude-opus-5-5[effort=high]` — the bracket's version spelling follows the *declared* spelling (`5.5` vs `5-5`), same model |
| `model = "gpt-6.1-sol"` | `model = { family = "gpt", version = "6.1-sol" }` | `gpt-6.1-sol[effort=high]` |
| `model = "grok-4.7"` + `effort = "xhigh"` | `model = { family = "grok", version = "4.7" }` | `grok-4.7-xhigh` (enumerated variant) |
| `model = "composer-2.5"` | `model = { family = "composer-2.5" }` | `composer-2.5` (bare enumerated); `speed = "fast"` adds `-fast` |
| `model = "gpt-6.1-sol[effort=max]"` (bracket direct-write) | `model = { family = "gpt", version = "6.1-sol", effort = "max" }` + `effort = "max"` | `gpt-6.1-sol[effort=max]` — a user bracket always passes verbatim, never rewritten |
| `model = "account:…/GLM-5.3"` (host account id) | no semantic form — opaque id, keep the bare string | identity untouched; host profiles are not resolved by CLI adapters |

Class, effort, capabilities, and routing order are properties of the
*profile*, not of the declaration form: both forms produce the same `class`,
the same `effort`, the same `capabilities`, and the same routing ladder
order. What changes is that the semantic form gains evolution diagnostics
(the legacy string is opaque and cannot tell you *why* it stopped resolving).

## The refreshable catalog

### Sources (free, local, programmable — nothing else)

| Harness | Catalog channel | Bracket-syntax evidence |
|---|---|---|
| cursor | `agent --list-models` (text: `<slug> - <description>` lines) | `agent --help` + the listing's Tip line |
| codex | `codex debug models` (one JSON document, observed >256 KiB) | — |

The probe (`orx agent probe`) reads these without any stream cap, parses
them with the shared parsers, and persists the catalog **inside the existing
capability snapshot** at `<user data dir>/probes/<harness>.json` (user data
dir = `ORX_DATA_DIR`, else `$XDG_DATA_HOME/orx`, else `~/.local/share/orx`).
Two keys were added to the snapshot: `model_catalog` and `catalog_error`.
No new file locations, no network calls, no completions.

Catalog records keep: every enumerated slug verbatim, the decoded
effort/speed variant (`-fast` suffix, trailing `-<effort>` token),
`(current)`/`(default)` markers, and — Codex — the model's own
`supported_reasoning_levels` (in the CLI's vocabulary and order, including
`ultra`, which is beyond the ORX profile vocabulary), speed tiers, and
context windows.

**Parameterized syntax is recorded separately from models.** A bracket
example like `claude-opus-4-8[context=1m,effort=high,fast=false]` found in
`--help` or the Tip line is evidence that the bracket parameter channel
*exists* and which keys it accepts. The example families are examples of the
syntax — the catalog never lists them under `models`, and the enumerated
list remains the only support list for baked slugs.

### Refresh and view — programmable entries only

```sh
orx agent probe cursor   # refresh: runs the local probes, persists the
                         # snapshot (catalog generation +1 on success)
orx agent probe codex
orx agent info cursor    # view: reads the latest snapshot (never probes);
                         # prints catalog size, source, generated_at,
                         # generation, current/default, families, and the
                         # parameterized-syntax evidence
```

Both take `--json` and keep the CLI exit-code contract (0/1/2). These two
commands — plus the CLI's own `--list-models` / `--help` /
`codex debug models` outputs they consume — are the entire refresh/view
surface. There is deliberately no interactive step in the loop: ORX does not
require, simulate, or script any UI mode.

*Background only, not an operating procedure:* Cursor's interactive Tune
mode is why the normal-mode enumeration looks converged — Tune exposes a
fuller model directory and finer effort variants, while normal-mode
`--list-models` prints baked slugs, and Cursor's forum/docs describe the
model list as showing a default effort with post-selection switching that is
remembered. This explains why parameterized families are absent from the
enumeration. ORX never opens, enables, or depends on Tune; every capability
fact it uses comes from the free channels above.

### Generation, precedence, and failure semantics

- `generation` is a per-harness integer starting at 1, incremented on every
  **successful** catalog refresh. A catalog discovered implicitly at launch
  carries generation `0` ("in-process discovery, not snapshot-backed").
- **Launch precedence:** the persisted snapshot's catalog first; only when
  no persisted catalog exists does the adapter run **one** fresh local
  discovery through the same probe command, cached for the process. A launch
  never writes the user data dir — an implicit probe must not be able to
  wipe a previously persisted snapshot.
- **A stale persisted snapshot wins over a CLI that has already moved.**
  Local drift never overrides the persisted catalog; the user's refresh is
  the only lever. Diagnostics name the generation they decided against.
- **A failed refresh never relabels the old catalog as fresh:** the
  refreshed snapshot either carries a genuinely new catalog (new
  `generated_at`, bumped generation) or carries no catalog plus the failure
  reason in `catalog_error`.
- **Discovery failure vs valid empty catalog are distinct:** binary
  missing, non-zero exit, timeout, empty output, or unparseable output
  record `catalog_error` with `model_catalog: null`; exit 0 with a parseable
  zero-model listing is a success with `models: []`. Authentication is
  orthogonal — a not-logged-in harness may still enumerate models, and an
  auth failure never fabricates or hides a catalog.
- **Historical snapshots stay readable:** a pre-G008 snapshot (no
  `model_catalog` key) yields "no catalog", not an error — launches then
  fall back to the one-time local discovery.

## Resolution rules, per adapter

### cursor (semantic spec)

Tried in order; the first successful rung wins and is recorded as the
`strategy`:

1. **Catalog variant** (`catalog_variant`). The spec's family+version is
   matched against the enumerated catalog with **dash/dot twins**
   (`version = "5.5"` matches a `5.5` or `5-5` spelling) and **prefix
   absorption** (a declared `grok` family matches enumerated `grok-*` and
   `cursor-grok-*`; the stem families are version-accounted together). The
   entry matching the requested effort and speed gives the exact variant
   slug (`grok-4.7-xhigh`, `grok-4.7-high-fast`). A missing effort variant
   or speed variant is a diagnostic naming what survives — never a quieter
   level, never a speed downgrade.
2. **Version-evolution gate.** A *fixed* version that the catalog no longer
   enumerates, when the family offers other versions, fails with a
   diagnostic naming the requested version and the offered one (the message
   says `another fixed version` verbatim) — never silently switching, never
   `auto`, never a disguised bracket. A
   *floating* family follows the catalog only when exactly one version
   exists; multiple versions is an ambiguity diagnostic naming all of them.
3. **Bracket synthesis** (`bracket_synthesis`). A family or id the catalog
   does not enumerate, when the bracket channel is evidenced (the help/Tip
   keys), is launched as `<declared-spelling>[…]` with the requested
   dimensions in the bracket — `effort` when an effort is requested;
   `context`/`fast` only when the evidenced parameter keys still include
   them. A requested dimension the evidence no longer supports is a
   "supports only …" diagnostic, never a bracket with a key the CLI no
   longer accepts. With **no** catalog and **no** bracket evidence the
   diagnostic is "catalog: none available locally" plus the refresh hint;
   an empty-but-valid catalog with bracket evidence degrades *in family*
   (same identity, bracket parameters); without bracket evidence it is
   "catalog: 0 models".

### cursor (legacy bare string)

The pre-G008 ladder, unchanged: a user bracket passes verbatim → a listed
slug with an effort dimension rewrites to the enumerated variant slug
(`grok-4.7` + `xhigh` → `grok-4.7-xhigh`) → a bare enumerated slug passes
(`composer-2.5`) → an unlisted id with bracket evidence gets the bracket
rung with effort (`claude-opus-5-5[effort=high]`), identity never rewritten.
The legacy string is opaque: it cannot produce evolution diagnostics, which
is the migration argument, not a behavior change.

### codex

Semantic specs are strict: the identity is matched against the catalog's
slugs, and the effort is validated against that model's
`supported_reasoning_levels` — passing `-c model_reasoning_effort=<level>`
only when validated, else a diagnostic naming the levels and the catalog
generation. Legacy bare strings keep the tolerated passthrough (no `-c`
flag, effort recorded as `provider_default`) — the legacy/strict split is
deliberate.

### shell / host

No resolution: the identity string (or bare string) is carried into the
execution spec verbatim. Host account ids (`account:…`) are opaque and never
rewritten.

### Dispatch behavior (all three roles)

Planner, worker, and verifier launches all resolve **before** the process
starts. A resolution failure records a failed attempt
(`error_kind = model_resolution`) whose reason names the requested identity,
the catalog state and generation, and `orx agent probe <harness>`; the CLI
is never started and no completion or pass is written. Every launched
attempt records its decision (`*.resolution.json`: spec, `cli_slug`,
`strategy`, `catalog_generation`) separately from the argv log, and the
attempt row carries the identity string plus the EffortOutcome.

### Effort verification evidence (EffortOutcome)

- **Enumerated variant slug:** a successful run confirms the effort
  (`requested_validated`) — the CLI enumerated and accepted that exact slug.
- **Bracket launch:** success alone proves nothing about the parameters; a
  run whose JSON output echoes the model argument verbatim
  (`"model": "<the bracketed slug>"`) and succeeds confirms
  (`requested_validated`). A successful silent run records
  `provider_default` — an honest unknown, never an assumed match.
- Profiles whose model has no effort dimension (e.g. `composer-2.5`) record
  `provider_default` with no source, as before.

## Strict failure boundary

No resolution path may substitute. Concretely, the resolver never: selects
`auto`; selects another family; silently switches a fixed version to the
offered one; downgrades effort or speed; launches a bracket with parameters
the evidenced keys do not support; or fabricates a catalog from a failed
discovery. Every failure is a pre-launch diagnostic with: the requested
identity (`family=grok@4.7`), the catalog state (models on offer, or
none/0), the generation it decided against when snapshot-backed, and the
refresh command. `tests/test_model_evolution.py` sweeps the whole evolution
matrix under this invariant.

## Claude/GPT: not enumerated, reached through the parameterized path

On the 2026-10-07 catalog (agent 2026.10.01), `agent --list-models`
enumerates **only baked slugs**: `grok-4.7-{low,medium,high,xhigh}{,-fast}`
(no prefix), `cursor-grok-4.6-*` and `cursor-grok-4.5-*` (prefixed),
`kimi-k3-*`, `kimi-k2.7-code`, `glm-5.2-*`, `composer-2.5{,-fast}`, and
`auto`. **No Claude, GPT, or Gemini model appears as an enumerated row** —
the only mention of those families in the listing is the Tip line's bracket
example (`claude-opus-4-8[context=1m,effort=high,fast=false]`), and
`--help` documents `--model` with the examples `gpt-5`,
`sonnet-4-thinking` and the same bracket form.

That is exactly the split the resolver implements: Claude Opus 5.5 and
GPT-6.1-sol are *usable but unlisted* on the cursor harness, so they ride
the bracket parameter channel (`claude-opus-5-5[effort=high]`), while Grok
4.7 rides the enumerated variant channel (`grok-4.7-xhigh`). The same
GPT-6.1-sol *is* enumerated on the codex side (`codex debug models` lists
it with its reasoning levels) — the channels are per harness, which is why
resolution is per adapter.

**What is and is not claimed.** The help/Tip evidence proves the bracket
*syntax* is supported and which parameter keys it accepts — it does not
prove that any specific model accepts any specific parameter value at run
time. Runtime verification of a parameterized launch is per-run and
echo-based: the run's own output echoing the bracketed model argument on
success is the confirmation channel (EffortOutcome above). G008 performed
**no real model runs**; the run-time behavior documented here is the
resolver's decision logic, tested against replayed captures and simulated
catalogs.

## Grok prefix and version evolution

The 2026-10-07 capture shows the grok family mid-evolution, all three
generations visible at once:

| Enumerated spelling | Prefix | Version |
|---|---|---|
| `cursor-grok-4.5-*` | `cursor-` | 4.5 |
| `cursor-grok-4.6-*` | `cursor-` | 4.6 |
| `grok-4.7-*` | none | 4.7 |

The resolver absorbs the prefix (a declared `grok` family matches both
spellings) but **not** the version change: `version = "4.6"` resolves to
`cursor-grok-4.6-high`, `version = "4.7"` to `grok-4.7-high`, and a
floating `grok` is an ambiguity diagnostic naming 4.5, 4.6, and 4.7. When a
later refresh removes 4.7 in favor of 4.8, a pinned `4.7` fails explicitly
(naming `grok-4.8` as what is offered) and only an explicitly floating
policy may follow. The evolution matrix
(`tests/test_model_evolution.py`) walks prefix rename + version bump,
variant add/remove, enumerated↔parameterized transitions in both
directions, parameter-evidence narrowing, and discovery failure — under the
no-substitution invariant.

## Evidence register and sample marking

Every empirical claim above traces to one of these recorded observations.
Nothing in G008 was a paid or real-model run.

| ID | Observation | Source | When |
|---|---|---|---|
| E1 | `agent --list-models` (agent 2026.10.01) enumerates only baked slugs; no Claude/GPT/Gemini rows; Tip line carries the bracket example | Controller local probe (Goal intake background) | 2026-10-07 |
| E2 | `agent --help` documents `--model` examples `gpt-5`, `sonnet-4-thinking`, and the parameterized bracket form | same | 2026-10-07 |
| E3 | During the G008 T002 fixture capture: `agent --help` succeeded; **one** `agent --list-models` invocation returned an authentication failure (transient) before later free local calls succeeded and produced the stored capture | Controller session during T002 development | 2026-10-07 |
| E4 | Re-verified in the G008 T005 worker session: `agent --version` = 2026.10.01-e373342; `agent --help` exit 0; `agent --list-models` exit 0; live listing byte-identical to the stored capture except the `(current)` marker (on `grok-4.7-high` at capture time) | T005 worker session (this document's author session) | 2026-10-07 |
| E5 | `codex debug models` dumps one large JSON document (observed >256 KiB; the stored fixture is trimmed to its real shape) | Controller session during T002 development | 2026-10-07 |

Sample marking:

- `tests/fixtures/model-catalog/cursor-list-models.txt` and `agent-help.txt`
  — **real captured CLI output** (E3), replayed verbatim by the tests; they
  are dated evidence, not a list ORX maintains or a support claim.
- `tests/fixtures/model-catalog/codex-debug-models.json` — **rebuilt
  sample**: the real document's shape with trimmed content, for parser
  testing; not a full capture.
- The evolution worlds in `tests/test_model_evolution.py`
  (`GEN_GROK46`, `GEN_GROK47`, …) and every stand-in launch in the test
  suite — **simulated**: fictional catalog generations and scripted CLI
  responses on `PATH`, used to prove evolution resilience and the
  no-substitution invariant without any real CLI or model.
- **No real model run was executed anywhere in G008.** All launch, echo,
  and EffortOutcome evidence in tests comes from stand-in binaries; no
  claim is made about the runtime behavior of any specific model or
  parameter value beyond the per-run echo-confirmation rule.

## Diagnostics: symptom → meaning → action

| Symptom (in the failure or `model_resolution` reason) | Meaning | Action |
|---|---|---|
| `family=grok@4.7 … another fixed version … grok-4.8` | pinned version no longer enumerated; the offered version is named | re-pin to the offered version deliberately, or make the policy floating (single-version families only) |
| `family=grok … will not guess` (names 4.5/4.6/4.7) | floating family, multiple candidate versions | pin the version you mean |
| `effort=high … not among them` (surviving levels named) | requested effort variant not enumerated for that family/version | pick among the named levels |
| `speed=fast … no enumerated variant` | fast variant missing | drop the speed dimension or refresh first |
| `supports only …` (e.g. effort, not context) | bracket evidence no longer includes that parameter key | drop the dimension, or re-check `agent --help` |
| `catalog: none available locally … orx agent probe cursor` | discovery failed (binary/exit/timeout/unparseable) — `catalog_error` in the snapshot | fix the CLI, then `orx agent probe cursor` |
| `catalog: 0 models` | valid but empty catalog (format change) | refresh; with bracket evidence the launch degrades in-family, never to another family |
| failure names `generation N` | decision was made against persisted snapshot generation N | `orx agent probe <harness>`; check `orx agent info <harness>` for the current generation |
| refreshed but launches still resolve the old world | the persisted snapshot is authoritative over local CLI drift; a *failed* refresh drops the catalog rather than relabeling the old one | `orx agent info <harness>`: a fresh generation with the new world, or `catalog_error` explaining the failed refresh |
| legacy bare slug resolves but records `bare_unlisted` / `provider_default` | opaque string passthrough — no spec, no diagnostics | migrate that profile to the semantic form |
| run succeeded, `effort_source` is null (`provider_default`) | bracket launch, success without echo | per-run unknown; treat echo presence (see EffortOutcome) as the only run-time confirmation |

## What this does not change

- Routing policy: tiers, role×class×effort defaults, escalation ladders,
  and the worker ladder order are untouched —
  [routing-strategy.md](routing-strategy.md) remains the policy authority;
  this document only adds the declaration/resolution layer underneath it.
- No interactive UI anywhere in the loop (Tune is background explanation
  only); no new network calls; catalog persistence reuses the existing
  capability snapshot mechanism and location.
- No version release: G008 changes no version number, tag, or release
  material; the release decision stays with the user.
