# 한국어 2단계 결정적 Reranker (`korean-lexical-concept-rerank@1`) 벤치마크 및 설계 기록

```text
DEV DIAGNOSTIC ONLY — NOT RELEASE PASS
```

- 날짜: 2026-11-24
- 브랜치: `feat/rag-korean-reranker-and-benchmark` (기준 `develop` `20e6880`)
- 데이터셋: `rag-natural-language-retrieval-dev@1.0.0` (합성 DEV 60건 / 100건 코퍼스, `HOLDOUT` 미실행)
- Release eligibility: `false` (임상 유효성 또는 운영 배포 승인 기록 아님)

## 1. 문제 정의와 범위

- **사용자/시스템 문제**: 기존 RAG 검색 파이프라인(`ai_worker/tasks/rag/evidence_search.py`, `evidence_rank_fusion.py`)은 `future_reranker_input_limit=20`과 `is_eligible_for_future_reranker` 경계를 예약해 두고도 2단계 reranker가 구현되어 있지 않았다. 1단계 Lexical RRF(`RET-L-OFFLINE`)만 적용할 경우 동의어(`EXPRESSION_SYNONYM`)와 어순·조사 변형(`EXPRESSION_WORD_ORDER_PARTICLE`) 질의에서 1순위 정확도가 하락했다.
- **포함 범위**:
  - 순수 Python 기반 2단계 결정적 한국어 Reranker (`ai_worker/tasks/rag/evidence_reranker.py`, `korean-lexical-concept-rerank@1`)
  - `HybridFusionResult` 연동 어댑터 (`rerank_hybrid_fusion_results`) 및 단위 테스트 59건
  - 오프라인 재현 가능한 60건 한국어 검색 벤치마크 및 CLI (`ai_worker/tasks/evaluation/rerank_benchmark.py`, 단위 테스트 6건)
- **제외 범위**:
  - PostgreSQL/pgvector 운영 경로 배선, 공유 API/DTO 계약 변경, DB 마이그레이션, 외부 유료 임베딩/LLM 호출, `HOLDOUT`/`SAFETY_REGRESSION` 실행

## 2. 설계 선택과 리뷰 보완

- **결정적 산술 및 동률 처리**: `Decimal` 6자리 반올림(`ROUND_HALF_EVEN`)과 `stable_coordinate_sort_key` UTF-8 오름차순 동률 처리를 적용해 `rrf-rank-fusion@1`과 동일한 결정성을 유지했다.
- **독립 리뷰(`dosey_reviewer`) 및 `prepare-repair` 루프 반영**:
  - 초기 초안에서 합성 DEV 전용 보일러플레이트(`평가용`, `가상`, `설정`)와 합성 방해 문구(`합성 색인의`, `포장 관리 코드는` 등)가 기본 런타임에 하드코딩된 과적합 위험(`HIGH`)이 지적되었다.
  - 기본 런타임에서는 합성 전용 문구를 제거하고 일반적인 한국어 빈 내용 표지(`DEFAULT_STUB_MARKERS`)만 유지하며, 도메인별 추가 표지와 불용어는 `RerankerConfig.stub_markers`, `extra_stopwords`로 명시적 opt-in 설정하도록 분리했다.
  - `rerank_hybrid_fusion_results`의 사전 절단으로 인한 `input_candidate_count` 왜곡 및 검증 누락을 수정하고, `_compute_config_hash`에 모든 랭킹 영향 테이블을 포함했다.

## 3. Baseline vs Candidate 실측 결과 (`DEV_DIAGNOSTIC_ONLY`)

- 재현 명령:
  ```bash
  PYTHONPATH=. .venv/bin/python -m ai_worker.tasks.evaluation.rerank_benchmark --format markdown
  ```
- `manifest_sha256`: `1305bdce20d3680af29ccf81c6204d8f175ee58cb06dd4edd12c3e6d4ccd6db8`
- `corpus_sha256`: `f8559cc16371ffe0d6d1c70912c37518afb96ce2ca8b7dca87b34ab82033940d`
- `reranker_config_hash`: `bc815b88c78feee93a57147f5074bce5d7fb85e8f7557aa199887ebe727a9ae5`
- `deterministic_result_sha256`: `fcdcc977c6a92a70b3043bde5d2201cb80e279fd2fcefb4902237bb4f88fcc3d`
- Rerank 지연시간: median `1.050 ms`, p95 `1.776 ms`

| Slice | N | Base R@5 | Cand R@5 | Δ R@5 | Base MRR@5 | Cand MRR@5 | Δ MRR@5 | Base NDCG@5 | Cand NDCG@5 | Base Top1 | Cand Top1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `ALL` | 60 | 0.9833 | 1.0000 | +0.0167 | 0.9194 | 0.9778 | +0.0583 | 0.9355 | 0.9833 | 0.8833 | 0.9667 |
| `EXPRESSION_SYNONYM` | 10 | 0.9000 | 1.0000 | +0.1000 | 0.5667 | 0.8667 | +0.3000 | 0.6500 | 0.9000 | 0.4000 | 0.8000 |
| `EXPRESSION_WORD_ORDER_PARTICLE` | 10 | 1.0000 | 1.0000 | +0.0000 | 0.9500 | 1.0000 | +0.0500 | 0.9631 | 1.0000 | 0.9000 | 1.0000 |
| `TOPIC_LIFESTYLE_MANAGEMENT` | 12 | 1.0000 | 1.0000 | +0.0000 | 0.8472 | 1.0000 | +0.1528 | 0.8859 | 1.0000 | 0.7500 | 1.0000 |
| `TOPIC_MISSED_DOSE` | 12 | 0.9167 | 1.0000 | +0.0833 | 0.8611 | 0.9444 | +0.0833 | 0.8750 | 0.9583 | 0.8333 | 0.9167 |
| `TOPIC_PRECAUTIONS` | 12 | 1.0000 | 1.0000 | +0.0000 | 0.9444 | 1.0000 | +0.0556 | 0.9583 | 1.0000 | 0.9167 | 1.0000 |
| `TOPIC_STORAGE` | 12 | 1.0000 | 1.0000 | +0.0000 | 0.9444 | 0.9444 | +0.0000 | 0.9583 | 0.9583 | 0.9167 | 0.9167 |

## 4. 제한 사항 및 남은 트레이드오프

- 1단계 `RET-L-OFFLINE`은 인메모리 Python 어휘 근사치이며 실제 PostgreSQL `pg_trgm`/FTS + `pgvector` 임베딩 검색이 아니다.
- 경량 오프라인 벤치마크는 95% cluster-bootstrap 신뢰구간을 계산하지 않는다.
- 100건 합성 DEV 코퍼스에서의 개선은 `DEV_DIAGNOSTIC_ONLY` 근거이며, 실제 식약처/e약은요 코퍼스나 `HOLDOUT` 성능·임상 안전성을 입증하지 않는다.
- 1단계 후보 풀(Top-20)에 정답이 포함되지 않은 질의는 2단계 reranker만으로 복구할 수 없다.

## 5. 멀티 에이전트 루프 적용 기록

- `scripts/agent_workflow.py plan-batch`: `rag-rerank-1`과 `rag-rerank-2`의 `allowed_files` 비중복 및 충돌 0건 검증 (`status: "ready"`).
- 병렬 구현 후 독립 리뷰(`dosey_reviewer`) -> `scripts/agent_workflow.py prepare-repair` (`status: "repair_required"`, `re_review_argv` 생성) -> 소유 에이전트 수정 -> `re_review_argv --dry-run` 및 전체 검증(`pytest 115 passed`, `mypy 977 files 0 issues`, `ruff` 0 errors) 완료.
