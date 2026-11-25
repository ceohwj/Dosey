# Dosey: Antigravity workspace entry point

Follow `AGENTS.md` as the repository authority, and read `SECURITY.md`, `docs/privacy-safety.md`, `docs/ai/workflow.md`, and `docs/ai/task-template.md` for affected work.

## Role-based model routing

When spawning Antigravity `/teamwork` (Swarm) workers or subagents for Dosey tasks, apply the 4-tier Gemini 4 routing in `docs/ai/model-routing.md`:

- **Main Coordinator (Integrator)**: `gemini-4-neon-medium` (or `Orchestrator (Neon)`) for routine work; escalate to `gemini-4-carbon-medium` / `Orchestrator (Carbon)` for large architecture design, `/goal` long-horizon runs, or multi-domain `/teamwork` swarms
- **Explore / Writer (`dosey_explore`, `dosey_writer`)**: Gemini 4 Hydrogen (`gemini-4-hydrogen-low` / `gemini-4-hydrogen-medium`, or `flash` subagent tier)
- **Implementation lanes (`dosey_executor`, `dosey_frontend`, `dosey_backend`, `dosey_llm`, `dosey_ocr`, `dosey_rag`)**: Gemini 4 Neon (`gemini-4-neon-medium`, or `inherit` subagent tier; escalate to `gemini-4-neon-high` or `gemini-4-argon-medium` only for complex multi-file refactors/debugging)
- **Integration Verification (`dosey_integration_verifier`)**: Gemini 4 Argon (`gemini-4-argon-medium`, or `pro` subagent tier)
- **Internal Review (`dosey_reviewer`)**: Gemini 4 Argon (`gemini-4-argon-high`, or `pro` subagent tier) for routine changes; escalate to Gemini 4 Carbon (`gemini-4-carbon-medium`, or `high` for critical audits) for security, auth, medical safety, shared contract, or DB migration reviews

Keep file ownership disjoint across concurrent workers, bound auto-repair loops (max 2–3 cycles), and stop for Human Owner decisions on shared contracts, DB migrations, privacy/medical safety, merge, or release.

This file is a thin entry point to the shared policies, not a duplicate product specification or an approval authority.
