# Dosey: Antigravity workspace entry point

Follow `AGENTS.md` as the repository authority, and read `SECURITY.md`, `docs/privacy-safety.md`, `docs/ai/workflow.md`, and `docs/ai/task-template.md` for affected work.

## Role-based model routing

When spawning Antigravity `/teamwork` (Swarm) workers or subagents for Dosey tasks, apply the role-to-model routing in `docs/ai/model-routing.md`:

- Explore / Writer (`dosey_explore`, `dosey_writer`): Gemini 4 Hydrogen (`gemini-4-hydrogen-low` / `gemini-4-hydrogen-medium`, or `flash` subagent tier)
- Implementation lanes (`dosey_executor`, `dosey_frontend`, `dosey_backend`, `dosey_llm`, `dosey_ocr`, `dosey_rag`): Gemini 4 Neon (`gemini-4-neon-medium`, or `inherit` subagent tier)
- Independent Verification / Review (`dosey_integration_verifier`, `dosey_reviewer`): Gemini 4 Argon (`gemini-4-argon-medium` / `gemini-4-argon-high`, or `pro` subagent tier)

Keep file ownership disjoint across concurrent workers, bound auto-repair loops (max 2–3 cycles), and stop for Human Owner decisions on shared contracts, DB migrations, privacy/medical safety, merge, or release.

This file is a thin entry point to the shared policies, not a duplicate product specification or an approval authority.
