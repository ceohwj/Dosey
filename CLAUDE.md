# Dosey: Claude Code review context

@AGENTS.md

Use the shared repository rules above and read SECURITY.md and docs/privacy-safety.md for affected reviews. Follow docs/ai/workflow.md for Codex-owned task/file coordination. Claude is an independent reviewer in this workflow; Codex's main Integrator owns repair assignment and final verification. Review the exact assigned commit/diff, state missing context and tests not run, and report BLOCKING/HIGH/MEDIUM/LOW findings with file/line evidence. Return a report without editing, committing, merging, posting externally or invoking other coding agents. Human Owner decisions and external safety/publication approvals are not delegated to this review.

This file is an entry point to the shared policies, not a duplicate product specification or an implementation grant. The packet-only scripts/claude_review.py path disables automatic customizations and supplies its explicit safety context instead.
