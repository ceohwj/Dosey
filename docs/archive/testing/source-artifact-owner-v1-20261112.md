# Source Artifact Owner Protocol v1 Verification Evidence (2026-11-12)

## 1. Scope & Governance

- **Decision Record**: `docs/governance/decisions/2026-11-12-source-artifact-owner-protocol-v1.md` (`PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`)
- **Boundary Contract**: `docs/contracts/proposed/post-mvp-1/source-artifact-finalizer-owner-boundary-v1.md`
- **Target Contract**: `docs/contracts/targets/post-mvp-1/rag-source-ingestion-v1.md`
- **Contract Index**: `docs/contracts/README.md`

This verification covers only **synthetic fixtures** and **contract/adapter boundaries** for `source-artifact-owner@1`, `source-artifact-intent@1`, and `source-artifact-outcome@1`:
- No live MFDS network calls or service keys were used.
- No live DB promotion or `docs/contracts/current/` promotion was performed.
- No business `DELETE` protocol was opened on the new owner-held `0700` vault.

---

## 2. Verified Capabilities (S1–S4)

| Slice | Verified Boundary | Test / Check | Result |
| --- | --- | --- | --- |
| **S1** | Decision `PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`, proposed boundary contract, target ingestion contract, and contract index alignment | `tests/contract/rag/test_source_artifact_owner_contract_docs.py` | **PASS** (1 passed) |
| **S2** | `source-artifact-owner@1` UDS framing (`4096`-byte exact accept, `4097`-byte reject, duplicate JSON key reject, `NaN`/`Infinity` reject), role enforcement (`publisher_uids` vs `reader_uids` vs unregistered), `DELETE` op fail-closed (`PROTOCOL_REJECTED`), `byte_size ± 1` fail-closed (`INPUT_MISMATCH`) with staging unlink, `PREPARED_LOCAL` refusing wire `OUTCOME`, lost terminal reply coexistence (`observed.json = INDETERMINATE` while owner `terminal.json = COMPLETE`), `UNKNOWN_GENERATION` on owner restart, trailing control-frame bytes rejection, `max_journal_attempts` enforcement (`LIMIT_REJECTED`), and post-complete vault corruption reconciliation (`INDETERMINATE` / `IMMUTABLE_OBJECT_MISMATCH`) | `ai_worker/tests/rag/source_ingestion/test_source_artifact_owner_protocol.py` + legacy finalizer/store regression suite | **PASS** (28 passed) |
| **S3** | `mfds_product_rematerialization` transient `0700` input spool (`0600` staged files) when vault root is `0000` unreadable, post-staging mutation detection (`BLOCKED_BY_PRODUCT_ARTIFACT`) and automatic cleanup, consumer reader protocol seam (`mfds_product_source`, `catalog_writer`, `catalog_approval`, `knowledge_materialization`), and suppression of legacy rollback cleanup journal dispatch when `supports_transaction_cleanup is False` | `test_mfds_product_rematerialization.py`, `test_mfds_product.py`, `test_mfds_label_writer.py`, `test_mfds_product_source.py`, `test_catalog_writer_execution.py`, `test_knowledge_materialization.py` | **PASS** (136 passed) |
| **S4** | End-to-end synthetic multi-UID boundary across `owner_uid=10010`, `publisher_uid=10001` (`PUT` + `READ` + rematerialization via transient spool), `reader_uid=10002` (Catalog & Knowledge `READ` allowed; `PUT` and `OUTCOME` rejected with `AUTH_REJECTED`), and `cleanup_uid=10003` (`READ` rejected with `AUTH_REJECTED`) | `tests/contract/rag/test_source_artifact_owner_boundary.py` | **PASS** (1 passed) |

---

## 3. Repository Guard & Hygiene Checks

- `python3 scripts/ci/check_python_test_inventory.py`: **PASS** (`Python test inventory is fully classified.`)
- `python3 scripts/ci/check_database_logic.py`: **PASS** (`DB 함수/프로시저/Trigger/RLS 재도입 검사 통과`)
- `python3 scripts/ci/check_protected_table_writes.py`: **PASS** (`보호 테이블 Python 쓰기 경계 검사 통과`)
- `git diff --check`: **PASS** (exit code `0`)

---

## 4. Remaining External / Container Gates

- Container-level multi-UNIX-account probe (`source_artifact_owner`, `source_publisher`, `source_reader`, `source_cleanup_executor` inside the actual Worker container image) remains gated on host disk free space (`>= 8 GiB` required by `scripts/ci/source_publisher_container_check.py`).
- External legal/terms/operational gates in `docs/release-gates/post-mvp-1-external-approvals.md` remain closed.
