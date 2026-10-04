# Routing strategy — model tiers, roles, and validation layers

Status: active policy (adopted 2026-10-03). This document records *why* the
routing tables look the way they do; `.orx/config.toml` and
`~/.config/orx/profiles.toml` are the executed form. Terminology lives in
[CONTEXT.md](../CONTEXT.md); the frozen M0 mechanics are in
`docs/m0-plan.md`.

## Model tiering convention

Three classes, mapped to vendors by product tier (not by benchmark):

| ORX class | Convention | Current members |
|---|---|---|
| `frontier` | latest flagships of codex / claude code | `gpt-6.1-sol` (codex 6.1), `claude-opus-5-5` (claude code 5.5, via cursor) |
| `strong` | other vendors' pro models | glm 5.3 (the zcode host), `grok-4.7`, deepseek |
| `economy` | flash / compose tiers | glm 5.3 flash, `composer-2.5` |

deepseek and glm 5.3 flash have no harness adapter yet — they are part of the
convention but get no profile until a harness exists.

Class ≠ effort. Effort (`low | medium | high | xhigh | max`) is the reasoning
depth a profile requests; it makes a model use its existing ability more
fully but never raises the model's ceiling. A `strong + max` profile does not
become a frontier model.

**House rule:** GLM models default to `max` effort — the strong tier
compensates with maximum reasoning depth (the host profile runs this way).

### Effort vocabulary (renamed 2026-10-03)

`quick | standard | deep | max` → `low | medium | high | xhigh | max`
(`quick→low`, `standard→medium`, `deep→high`, `max` unchanged, `xhigh` new).
Historical rows in `state.db` and pre-M2 docs keep the old words; nothing is
back-written. Provider-facing levels map 1:1 today (`EFFORT_MAP` in
`adapters/base.py` stays as the seam), and adapters still validate against
each provider's catalog before passing a level.

Plan depth (`light | standard | deep`) is a different axis — how much planning
a Goal needs, not how hard a model thinks. It keeps its vocabulary.

## Role × class × effort defaults

| Stage | Default | Escalation |
|---|---|---|
| Controller (`[controller]`) | strong + max (`orx-host`) | escalate by moving work to a stronger class, not by deliberating more |
| Plan, light depth | strong + medium (`cursor-strong`) | — |
| Plan, standard/deep | frontier + high (`codex-frontier`, `cursor-frontier`) | deep keeps the frontier-only gate (`allow_class_downgrade = false`) |
| Build (worker) | strong + medium (`cursor-strong`) | ladder in list order, see below |
| Verify, structural | economy + medium (`cursor-economy`) | strong fallback (`cursor-strong`, `orx-host`) |
| Verify, semantic | strong + medium (fallback rungs of `[verify]`) | pin a stronger profile for the retry |
| Verify, visual | frontier + vision (`cursor-frontier`, `agent[vision]:`) | — |
| Replan | frontier + high (planner pool) | — |

The intelligence budget is deliberately asymmetric: **planning is
intelligence-heavy, building is execution-heavy, verification is
evidence-heavy, the controller is state-heavy.** M1 paid-run data backs this —
see `docs/m1-report.md`: the frontier planner cost ~363k input tokens per
draft, strong workers carried the builds, and the economy verifier ran every
audit at high frequency for ~1/7 the input. The expensive mistake of M1 was
not tiering but *planner drafts that were thrown away* (finding #5) — which
is why planner runs are gated behind `replan` discipline, not made cheaper.

### Worker escalation ladder

`[worker] profiles` order *is* the ladder.

**2026-10-04 amendment (zcode preset).** The executed default is now
ZCode-first: `orx preset install zcode` + user-layer `orx config set` made
the defaults

    controller = zcode-controller (the ZCode host session, GLM-5.3)
    worker   = zcode-worker (GLM-5.3 subagent) → cursor-strong (grok-4.7)
    verify   = zcode-verifier-flash (Flash subagent) → cursor-economy (composer-2.5)

A project that should burn Cursor first flips the two entries in its own
`.orx/config.toml`. The historical ladder below remains valid for projects
that keep the pre-preset order:

    cursor-strong (strong+medium) → cursor-strong-high (strong+high)
    → orx-host (strong, host) → cursor-frontier / codex-frontier (frontier+high)

Automatic routing picks the first usable entry (availability/cooldown
filtered); a human/controller-driven escalation pins a stronger profile for a
retry. Triggers, from the controller contract:

- fails once → retry with feedback, same rung;
- same acceptance criterion fails twice → pin the next rung up;
- plan/verification disagreement, scope drift, wrong dependency graph →
  `orx replan` (frontier pool), never another blind retry.

## Validation layers (V0–V3)

Plans express verification through the existing syntax; the layering is a
policy about *what proves what*, not new machinery:

| Layer | Mechanism | Cost | Trust |
|---|---|---|---|
| V0 deterministic | shell command entries | free, no LLM | highest — compile/tests/lint/fixtures |
| V1 structural | `agent:` on economy | cheap | tasks complete, files changed, evidence present, no TODO/stub residue |
| V2 semantic | `agent:` on strong | medium | logic meets the Goal, edge/error paths, hidden regressions, happy-path-only tests |
| V3 visual | `agent[vision]:` on a vision profile | medium | layout/spacing/overflow/state vs acceptance criteria |

**V0-first rule** (baked into the planner prompt): every verification list
leads with the shell checks that can prove the criterion; agent checks cover
only what shell cannot prove; `agent[vision]:` only for rendered-UI
acceptance. A verifier never relaxes acceptance criteria to produce a pass —
it reports, it does not repair.

## Final acceptance

A run lands `done` only when all three hold (the controller contract repeats
this): **acceptance criteria met, evidence files complete, verifications
passed** — arbitrated by `orx status --json`, never by output "looking
finished".

## Project-level overrides

Layering already provides the override points; no per-project dialect exists:

- **Profile override:** a same-name profile in the project's
  `.orx/profiles.toml` completely replaces the user-layer one — redeclare
  with a different `effort`/`class` to re-tier a model for one project.
- **Routing override:** a project `[worker]` / `[verify]` / `[plan.*]`
  section replaces the whole user-layer section — reorder or swap profiles
  per project.
- **Effort variation without new models:** effort is baked into profiles, so
  "same model, deeper" is a second profile (`cursor-strong` /
  `cursor-strong-high` is the pattern).

## Future work (explicitly not done here)

- `[worker.low|medium|high]` per-complexity routing — Plan IR already carries
  `routing.complexity` (validated, persisted) but dispatch does not consume
  it yet.
- Automatic retry escalation (climb the ladder on retry count instead of
  controller-pinned profiles).
- Field-level profile merge (project overrides one field instead of the whole
  profile).
- Harness adapters for deepseek / glm-flash CLI surfaces.
