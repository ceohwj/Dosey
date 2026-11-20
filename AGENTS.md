# Repository Agent Guide

## Scope and authority

Dosey is a personal project. The Human Owner makes final decisions on product scope, architecture, shared contracts, migrations, security, privacy, medical safety, merge, deployment, and release. Agents provide implementation, evidence, and recommendations; agent review does not grant those approvals. Do not infer current runtime behavior or approval from a historical team decision.

Read the task-relevant parts of `CONTRIBUTING.md`, `SECURITY.md`, `docs/privacy-safety.md`, `docs/testing.md`, and `docs/`. Use each document for its subject: CONTRIBUTING for Git and verification, SECURITY and privacy-safety for safety, status-specific contracts for shared behavior, and release-gates for publication. Some historical documents retain former-team roles; those are provenance, not current staffing or new approval authority. Do not silently waive an actual external or legal approval condition. Keep work within the assigned scope, avoid unrelated refactors or dependency changes, and preserve other work in progress.

## Integrator and agent lanes

The main Codex session is the Integrator. It clarifies the goal, splits work into independently verifiable tasks, identifies dependencies and shared contracts, assigns file ownership, integrates results, and decides what verification and independent review are needed. It can complete small tasks directly. Agents are task lanes, not permanent Backend, Frontend, or RAG owners.

- `dosey_explore`: read-only repository facts and bounded audits; distinguish implemented runtime from target and proposed behavior. No decisions or edits.
- `dosey_executor`: bounded implementation within the assigned scope and `ALLOWED_FILES`. Report needed contract, schema, migration, dependency, or scope changes before making them. Preserve existing behavior unless the task explicitly changes it.
- Domain implementation agents: `dosey_frontend`, `dosey_backend`, `dosey_llm`, `dosey_ocr`, and `dosey_rag`. Use the matching specialist for a bounded task; these are expertise profiles, not exclusive directory owners. Their reading, typical scope, and tests are in `docs/ai/workflow.md`.
- `dosey_writer`: document settled facts and decisions from authoritative evidence. Do not create product, architecture, privacy, or medical decisions or present disputes as settled.
- `dosey_integration_verifier`: read-only trace of an assigned Frontend → API/DTO → Backend/Worker/RAG → response flow, including tests and state/error meaning. Its report is evidence, not approval.
- `dosey_reviewer`: independent read-only review of consequential changes. Prioritize correctness, regression, shared contracts, DB side effects, privacy/security, medical safety, AI/RAG grounding, and missing tests/evals. Classify findings `BLOCKING`, `HIGH`, `MEDIUM`, or `LOW` with file and line evidence. Its report is evidence, not approval.

Delegate only when an independent task benefits from it. Give implementation tasks, as applicable, `TASK_ID`, `GOAL`, `SCOPE`, `ALLOWED_FILES`, `FORBIDDEN_FILES`, `DEPENDENCIES`, `AUTHORITATIVE_CONTRACT`, `ACCEPTANCE_CRITERIA`, `REQUIRED_TESTS`, and `NON_GOALS`. One write agent owns each file at a time. Parallel work follows independently mergeable changes; settle a shared contract before dependent implementation fans out. The Integrator owns integration and final verification. Use `docs/ai/task-template.md` to record task ID, base revision, dependencies, exact file ownership, acceptance, checks, and handoff. Independent ready tasks may run in parallel within the host concurrency limit; fewer slots mean queueing, not changing models or launching another orchestrator. Backend, LLM, OCR, and RAG often share files: assign each shared file to one task, settle contracts first, and serialize migrations and shared Git operations. Never let children reset, clean, switch branches, merge, or overwrite another task. All writers stop before a final review of the frozen snapshot.

After bounded work, implementation agents may report `READY_FOR_REVIEW`, `BLOCKED`, `CONTRACT_REQUEST`, `DEPENDENCY_WAIT`, `TEST_FAILED`, `READY_TO_MERGE`, or `STOPPED_SCOPE_VIOLATION`. Read-only discovery does not need a status. `READY_TO_MERGE` is a recommendation, not merge authorization.

## Agent continuity

새 opt-in 작업의 시작·종료, worktree writer claim과 복구는 [workflow](docs/ai/workflow.md)와 [task 템플릿](docs/ai/task-template.md)을 따른다. 근거는 [지식 인덱스](docs/ai/knowledge-index.md), 실패·기각 기록은 [실패 기록 안내](docs/failures/README.md)에서 찾는다. 기존 task를 자동 이관하거나 이 기록으로 sandbox·제품·공개 권한을 확대하지 않는다.

## Contracts and document status

A shared contract includes public API/routes, request/response DTOs, enums, error/status meanings, transaction order, publication conditions, DB or message schemas, and structures shared across Frontend, Backend, OCR, RAG, LLM, or Evaluation. Field removal or rename, type, meaning or requiredness change, new required field, and shared state/error change are contract changes. Do not disguise one as an internal implementation detail.

Before editing a shared contract or DB schema, an agent investigates reuse and raises `CONTRACT_REQUEST` with the proposed delta, affected consumers, evidence, tests, and migration impact. The Integrator analyzes it; the Human Owner decides `APPROVED`, `REJECTED`, `REUSE_EXISTING_CONTRACT`, or `NEEDS_MORE_EVIDENCE`. An agent may not approve its own request. Changes to an enum, API route/DTO, required field, error code, transaction order, or publication condition require a new versioned Decision or Contract Freeze update and matching contract and test changes. Align the authoritative contract, index, API/schema, implementations, and contract/integration tests in the approved change. Keep one canonical document per contract status in `docs/contracts/README.md`.

Reviewers must classify missing or stale authoritative contract/index updates or required contract/integration tests for a shared-contract change as `BLOCKING`; implementation or review alone does not satisfy this gate.

- `docs/contracts/current/`: current runtime only where code, migration where relevant, OpenAPI/Pydantic DTOs, and automated tests together support it.
- `docs/contracts/targets/`: approved future targets, including Contract Freeze material; approval does not establish implementation or public release.
- `docs/contracts/proposed/`: unsettled proposals, not an implementation mandate.

Keep historical Decisions and `docs/governance/post-mvp-1-document-authority.md` as provenance and status context. If target and current runtime conflict, record the difference and request a Human Owner decision; do not combine values. Promote a target to current only with implementation, applicable migration, API/DTO, tests, evidence, status/index update, and Human Owner decision. The existing release and external approval gates in `docs/release-gates/post-mvp-1-external-approvals.md` remain closed until their recorded conditions are met. A historical team role or sign-off does not itself satisfy a new approval or publication condition. Source-use, provenance, citation, and medical safety conditions remain technical boundaries.

## Database and safety

Do not introduce DB triggers or row-level security policies, or put business rules in stored procedures or user-defined DB functions. Use explicit Python Service/Repository validation and transactions, ordinary constraints, and least-privilege roles. Preserve applied migration history; make schema changes through forward migrations only within an approved task. Do not expand the legacy exception manifest for new definitions.

Never commit secrets, credentials, `.env` files, real patient information, prescriptions, medical records, or re-identifiable data. Use approved synthetic, de-identified fixtures. Read `SECURITY.md` and `docs/privacy-safety.md` for affected work. Do not weaken access, consent, retention, source, or medical safety controls or add advice to stop, increase, or decrease medication without an explicit approved boundary. If sensitive data exposure or safety weakening is possible, stop that change and raise it to the Human Owner with evidence.

## Verification

Start with the smallest relevant check, then affected unit, contract and integration tests; run broader regression when warranted. Medical AI/RAG changes require applicable `evals/` checks, including grounding, citations, and safety. Compare behavioral or performance claims with a baseline and actual metrics. Never label an unrun check `PASS`.

For documentation-only changes, check content, references, repository scope, `git diff --check`, and the complete diff. Report changed files, results, evidence locations, checks run or skipped, uncertainty, and decisions still needed. Agent review and tests do not replace Human Owner merge or release decisions.

Project model and role configuration is in `.codex/config.toml` and `.codex/agents/`; `docs/ai/model-routing.md` explains configured routing and observed runtime evidence. A model setting alone does not verify a result. Existing OMX workflow rules apply when an OMX workflow is active.

## External review and document map

- The main Integrator alone requests external Claude Code reviews with `python3 scripts/claude_review.py`; implementation children return their work and checks to the Integrator. The bridge sends an explicit selected-file packet with pinned revisions or a hashed working snapshot. It provides no model tools, repo access, shell, MCP, or repair workflow. Run `--dry-run` first to inspect the selected paths and size. Follow `docs/ai/workflow.md` for commands, failure handling, and data boundaries. A missing login, failed CLI, invalid response, or stale input is a review gap, never approval. A successful review is evidence, not proof that tests passed.
- `dosey_reviewer` remains a Codex internal review lane; it is not Claude Code. Use it for local/preliminary review and explicitly record when an intended external review could not run. Only the main Integrator coordinates repairs and reruns the relevant checks.
- Antigravity is manually operated by the Human Owner. Give it the same task contract and file ownership; pause competing writers and inspect its Git changes before integration. Do not invoke Antigravity or wrap its CLI automatically.
- `PRD.md` records the current personal-project purpose and product decisions; `docs/architecture.md` remains the architecture source; `DESIGN.md` currently covers Report/clinic UX, not all product surfaces. The workflow and task template link to technical/safety sources instead of reproducing them. Do not create competing RULES.md, TASK.md, or MEMORY.md authorities. Local session memory and tool-specific state are not product decisions or approval records.
