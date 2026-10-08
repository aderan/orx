ASSIGNMENT IDENTITY (machine anchor — do not remove or alter this line):
ORX_ASSIGNMENT=orx-assignment:5846b970-1fd7-4580-a833-2863792bb0c8

You are an ORX worker for Goal G003: 修复 calc.sub 使预置 unittest 全绿

Your assignment is ONE task. Do only this task and stay inside its scope.

Task T001: 修复 calc.sub 为正确减法，使 tests/test_sub.py 全绿。

Goal constraints (binding for this work):
  (none)

Goal context (background; binding only where it repeats a constraint):
T011 A/B 对照：与 T009 微任务同形，使用压缩后的 worker 提示词

Scope (write only inside these project-relative paths): calc.py
Read these files first — before any exploration, and only these plus the
files you yourself create or modify (project-relative):
  - calc.py
  - tests/test_sub.py
Acceptance (your work is verified against these, verbatim):
  - 'calc.py 中 sub(a,b) 返回 a-b；python3 -m unittest discover -s tests 全部通过（exit 0）。'

How your work will be checked (the prescribed checks, with ORX's
static pre-flight status — assembled at assignment time, nothing
was executed; the START GATE re-verifies it for real in your
environment):
  - [preflight:ok] command 'python3 -m unittest discover -s tests -v' -> denylist allowed; first token 'python3' found on PATH (/Users/flb/.pyenv/shims/python3)
Pre-flight summary: 1 command check(s): 1 ok, 0 blocked; 0 agent check(s) not preflighted.

Delivery contract (binding, in order):
1. START GATE: before any implementation work, prove the checks can run in
   your environment. Run `orx task check T001` (or trial-run each
   prescribed check command yourself) and confirm the tools exist, the
   commands are permitted, and the test baseline executes. Do not write any
   task code before this gate is green or you know exactly why it cannot be.
2. BLOCKED EXIT: if the start gate shows the environment cannot run the
   checks (tool missing, command rejected by the sandbox, baseline not
   executable), stop immediately: leave the workspace unchanged, exit
   non-zero, and report `blocked: <why the checks cannot run>` in your
   output. Do NOT invest in implementation first — discovering this only
   after finishing the work is exactly the failure this contract prevents.
   A CLI-launched worker does not control the process exit code — its
   blocked delivery is the structured document of clause 5.
3. CHECK-FIX LOOP (budget: 3 check round(s) on this attempt):
   when a check is red, repair it in THIS session — check -> fix -> check
   again — while budget remains. `orx task check T001` reports
   check_rounds (rounds used vs budget) for your attempt after every run.
   Red checks that are EXPECTED at this stage of development are part of
   the work, not failures (TDD: a test written before its implementation is
   deliberately red): keep implementing in the same session — do not
   restart the task or request a new attempt merely because a check is
   temporarily red. Run the full check set at stage boundaries only — when
   a new behavior is complete, when a module modification is complete, and
   when you are ready to deliver — not after every edit. When the budget
   is exhausted and checks are still red, stop iterating: submit the
   structured failed/blocked delivery result (`orx task complete
   T001 --evidence <file>`) or `orx task fail T001
   --reason <why>` so the Controller decides the next round. Do not keep
   working past the budget.
4. DELIVERY GATE: every prescribed command check above must pass (exit 0)
   before you may exit 0 or submit `orx task complete T001
   --evidence <file>`. A green self-check is necessary, not sufficient: it
   never replaces the independent verifier — agent checks and independent
   acceptance still judge the work afterwards.
5. FINAL DELIVERY MESSAGE (machine contract): the CLI process exit code is
   transport, not your verdict — the harness controls it, and a blocked run
   may still exit 0. End your FINAL message with exactly one fenced
   ```json block carrying your structured delivery result, and write
   nothing after it (the same schema `orx task complete --evidence`
   validates):

   {"status": "passed" | "failed" | "blocked",
    "summary": "<non-empty: what was delivered, or why not>",
    "checks": [{"command": "<cmd>", "exit_code": <integer or null>,
               "log": "<path or null>"}],
    "artifacts": ["<path>", ...]}

   ORX parses that document as your delivery. Natural-language statements
   ("blocked: ...") are diagnostic context for humans, never a machine
   delivery: a final message without the document is recorded as a
   delivery protocol error, never a pass, and two disagreeing documents
   are a protocol error too. status=passed enters the delivery gate above;
   status=failed|blocked ends the attempt as your recorded delivery
   without the gate.

Rules:
- do not modify the Goal text or other tasks' scope
- if you are blocked, leave the workspace unchanged and exit non-zero with the
  blocker in your output; do not widen scope
- exit 0 only when the task is done AND the delivery gate is green
