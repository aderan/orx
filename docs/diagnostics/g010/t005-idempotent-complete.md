# G010/T005 design: identity-constrained idempotent `complete` (2026-10-16)

Deliverable: an accepted `orx task complete` persists its ORIGINAL receipt
so a retransmission (response lost, new process, Goal already done) replays
it; the delivery summary is over normalized content; acceptance is one
atomic commit; G009's evidence validation and stale rejection order are
preserved. This note records the identity key, the digest normalization,
the transaction boundary, and the stale-priority reasoning. Behavior
regressions live in `tests/test_lifecycle.py` (G010 T005 section) and
`tests/test_state.py` (v15).

## 1. Identity key

```
receipt identity  = (attempt_id)                     -- UNIQUE, write-once
delivery identity = (revision, task, attempt_id, digest, metadata)
metadata          = (session_ref, actual_model) at acceptance
```

- One receipt per attempt, ever (`completion_receipts.attempt_id UNIQUE`;
  a second store raises ConflictError — the fact of the first acceptance
  is never rewritten).
- `revision_id`/`task_id` ride the row for audit; they are validated
  against the attempt's own columns, never trusted from the caller.
- The un-quoted compatibility path (`task complete` without `--attempt`)
  NEVER reaches the receipt: it keeps the status gate first, so it cannot
  guess which old delivery a caller might mean.

## 2. Digest normalization (`verify.delivery_digest`)

Canonical form is sorted-key compact JSON over exactly the four contract
fields, then sha256:

```python
{"status": ..., "summary": ...,
 "checks": [{"command": c, "exit_code": e}, ...],   # order kept, log dropped
 "artifacts": [...]}                                 # as declared
```

Excluded on purpose: the evidence file's path and mtime (same claim from
another file is the same delivery), the checks' `log` pointers (a re-run
writes new logs; the claim did not change), and ignored legacy keys
(`commands`). Any change to the claim itself — status, summary, a
command, an exit code, the artifact list — is a different digest, and a
retransmission whose digest differs from the stored receipt is a named
ConflictError, never a replay.

## 3. Transaction boundary

```
task_complete
  ├─ evidence validation (G009, unchanged)         -- no writes
  ├─ digest                                         -- no writes
  ├─ quoted-identity checks: role, revision/task,  -- no writes
  │   LATEST (moved before closed), closed→receipt
  │   replay | stale | conflict                     -- replay is read-only
  ├─ session late binding + reported model          -- pre-acceptance writes
  ├─ [failed/blocked] _accept_delivery ──────────────── ONE tx ──┐
  ├─ [passed] delivery gate (fresh command runs)   -- rows, own txs │
  └─ [passed, green] _accept_delivery ───────────────── ONE tx ───┤
                                                                   │
        evidence(completion) ─ replan chain (passed only) ─ attempt│
        closure ─ usage backfill ─ transition(verify/fail) ─       │
        run_command_verifications (no-op today) ─ apply_verdict ─  │
        refresh ─ cancel convergence ─ RECEIPT ────────── COMMIT ──┘
```

- Everything inside `_accept_delivery`'s `store.tx()` (BEGIN IMMEDIATE)
  commits together or not at all: completion evidence, replan provenance
  and snapshot, attempt closure, usage backfill, lifecycle transitions,
  derived run/Goal refresh, cancel convergence, and the receipt row.
- A fault before the commit rolls ALL of it back: the attempt stays open,
  no receipt, no orphan evidence, task keeps its status; the worker may
  complete again on the same attempt (pinned by fault injection).
- A fault after the commit is indistinguishable from response loss at the
  persistence boundary — the receipt is written IN the commit — so
  retransmission is the recovery, and it re-runs nothing.
- The delivery gate runs OUTSIDE any write transaction (it executes real
  commands; no lock is held across them). Concurrent duplicates therefore
  may both run the gate; the acceptance transaction re-checks the receipt
  INSIDE (serialized by BEGIN IMMEDIATE): first writer accepts, second
  replays. Exactly one acceptance lands — one receipt, one completion
  evidence row, one `complete` event, one usage row (the loser never
  reaches the backfill).

## 4. Stale priority (what a receipt must NOT do)

Judged in this order on the quoted path — the receipt is reachable only
after every ownership check passed:

1. role must be worker;
2. attempt's (revision, task) must equal the ACTIVE revision's task —
   a superseded revision, or a same-numbered task in a newer revision,
   is stale;
3. the attempt must still be the task's LATEST attempt — a retry/recover
   replacement is stale EVEN IF the old attempt carries an accepted
   receipt and the delivery body is byte-identical (迟到结果不会覆盖新
   尝试);
4. only then: a closed attempt with a receipt replays (task status
   verifying/passed/failed, Goal any status including done); a cancelled
   task is stale even with its receipt — the wrap-up's closure is not
   reopened by the historical acceptance;
5. a closed attempt WITHOUT a receipt keeps the exact G009
   "stale submission: already closed" diagnosis (recover/cancel closures,
   pre-v15 acceptances);
6. evidence-document problems are still named BEFORE identity problems
   (G009 ordering contract, unchanged).

## 5. Pre-existing behavior deliberately unchanged

- Red-gate rejection: no receipt, attempt open, task keeps its status;
  the same attempt fixes and resubmits (`check -> fix -> check ->
  complete`).
- `verify submit`, the independent verifier, heartbeat ownership gates,
  and the CLI passed-delivery gate flow are untouched; the CLI
  blocked/failed delivery branch now routes through the same atomic
  acceptance (same reason prefixes, same evidence contract, plus the
  receipt).
- Migration v15 is additive-only; a database upgraded from v14 has no
  receipts and nothing derives one.
