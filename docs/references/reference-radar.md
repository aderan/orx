# Reference radar

How ORX tracks upstream projects it borrows design from. This is an ORX
repo engineering process, NOT an ORX runtime feature (m1-plan P7).

- `references.toml` lists each upstream repo and the areas we actively
  borrow from (CLI lifecycle from uv + gh; agent config from codex +
  opencode; resource governance from litellm; task history from airflow;
  triggers from n8n + OpenHands).
- Review cadence: at every milestone boundary (M2, M3, ...) check each repo
  for release notes touching our borrowed areas; only then produce a
  "Reference Update" note (what changed upstream, impact on ORX, action).
- Never add runtime dependencies on these projects; borrowing is design
  only, with ORX-sized implementations.

Current status (2026-10-03, end of M1): first freeze; no updates pending.
