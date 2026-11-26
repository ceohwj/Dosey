# 복약 Chat RAG 검색 Ablation 실행기 구현 및 검증 기록 (`retrieval-ablation`)

```text
LOCAL_EVAL_CORPUS_ONLY — LIVE 3-ARM ABLATION COMPLETED (RELEASE_ELIGIBLE = false)
```

- **날짜**: 2026-11-26
- **Task ID**: `retrieval-ablation`
- **브랜치 / 기준 revision**: `feat/chat-rag-measured-slice-v1` (`3297d83`)
- **관련 요구사항**: [chat-rag-measured-slice-requirements.md](../../designs/guide-chat/chat-rag-measured-slice-requirements.md) (`CRM-01`–`CRM-04`, `CRM-12`), [ADR 0004](../../adr/0004-chat-rag-measured-slice-selection.md)
- **평가 범위 / 정책**: `LOCAL_EVAL_CORPUS_ONLY`, `RELEASE_ELIGIBLE = false`

## 1. 생성 파일

| 파일 | 역할 |
| --- | --- |
| `apps/ai_worker/tasks/evaluation/chat_rag_retrieval_ablation.py` | `corpus_chunks.jsonl` 기반 한국어 복약 검색 ablation 실행기. 결정적 grounded 질의셋 생성(`build_eval_queries`, `EE`/`UD`/`NB`/구어체 + out-of-corpus negative), 3개 검색 arm(`bm25_lexical` 단어+2-gram, `dense_vector` OpenAI `text-embedding-3-large` 1536차원 + 저장소 외부 JSON 캐시·토큰/비용 추적·`ADR 0004` 예산 가드, `rrf_hybrid` `k=60`), `Recall@5`·`MRR@5`·`nDCG@5`·`P50`/`P95` 지연·`neg_top1_mean`·`pos_neg_top1_gap`·Evidence Gate 임계값 후보·paired bootstrap 95% CI, `--dry-run`/`--offline-bm25-only`/`--format json\|markdown` CLI 제공 |
| `apps/ai_worker/tests/evaluation/test_chat_rag_retrieval_ablation.py` | 100% 오프라인 단위 테스트 6건 (누락/빈/잘못된 코퍼스 fail-closed, 저장소 내부 캐시 차단·`OPENAI_API_KEY` 누락 차단·예산 상한 가드, grounded 질의셋·구어체·negative 생성 검증, 한국어 subword BM25 및 `Recall@5`/`MRR@5`/`nDCG@5`/지연 백분위 계산 검증, RRF 동점 tie-breaking 및 단일 arm 보완 승격 검증, sync/async fake embedder 캐시 hit/miss 재사용·비용 계산 및 JSON/Markdown CLI 출력 검증) |
| `docs/archive/testing/chat-rag-retrieval-ablation-20261126.md` | 본 구현 요약, 오프라인 단위 테스트·정적 검사 결과, Live 실행 대기(`NOT_RUN`) 절차 기록 |

## 2. 오프라인 단위 테스트 및 정적 검사 결과 (`PASS`)

| 검사 | 명령 | 결과 |
| --- | --- | --- |
| 오프라인 단위 테스트 | `PYTHONPATH=apps:packages:. uv run --no-sync pytest apps/ai_worker/tests/evaluation/test_chat_rag_retrieval_ablation.py -q` | `6 passed in 0.48s` |
| 린트 | `uv run --no-sync ruff check apps/ai_worker/tasks/evaluation/chat_rag_retrieval_ablation.py apps/ai_worker/tests/evaluation/test_chat_rag_retrieval_ablation.py` | `All checks passed!` |
| 포맷 | `uv run --no-sync ruff format --check apps/ai_worker/tasks/evaluation/chat_rag_retrieval_ablation.py apps/ai_worker/tests/evaluation/test_chat_rag_retrieval_ablation.py` | `2 files already formatted` |
| 타입 검사 | `uv run --no-sync mypy apps/ai_worker/tasks/evaluation/chat_rag_retrieval_ablation.py apps/ai_worker/tests/evaluation/test_chat_rag_retrieval_ablation.py` | `Success: no issues found in 2 source files` |

## 3. Live Ablation 실측 결과 (`PASS` — 90개 질의, OpenAI `text-embedding-3-large`)

실행 명령:

```bash
PYTHONPATH=apps:packages:. uv run --no-sync python -m ai_worker.tasks.evaluation.chat_rag_retrieval_ablation \
  --corpus-path /tmp/dosey-mfds-eval-corpus/corpus_chunks.jsonl \
  --cache-path /tmp/dosey-mfds-eval-corpus/embeddings_cache.json \
  --format markdown
```

- **대상 코퍼스**: `/private/tmp/dosey-mfds-eval-corpus/corpus_chunks.jsonl` (`chunks=964`, `items=26`, `sha256=4bc6f64ee9439865...`)
- **평가 질의셋 (`chat-rag-eval-queries@1`)**: 총 `90`건 (`pos=78`, `neg=12`, `sha256=3022029b716038b4...`)
- **임베딩 모델 및 비용**: `text-embedding-3-large` (`dims=1536`, `cache_hits=0`, `cache_misses=1053`, `prompt_tokens=444414`, **`cost_usd=$0.057774`**)
- **설정 (`Config Hash: 184e3d3c1aa82238...`)**: `top_k=5`, `pool_size=20`, `bm25=(1.5, 0.75)`, `rrf_k=60`

### 3.1 3-Arm 검색 성능 비교

| Arm | Recall@5 | MRR@5 | nDCG@5 | No-hit@5 | SecHit@5 | PosTop1 | NegTop1 | Gap | P50 (ms) | P95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25_lexical` | 0.5513 | 0.5141 | 0.4631 | 0.2692 | 0.7949 | 52.1412 | 24.3696 | 27.7716 | 1.547 | 3.555 |
| `dense_vector` | 0.7671 | **0.7442** | 0.6963 | **0.0769** | **0.9487** | 0.6614 | 0.4565 | **0.2049** | 47.845 | 97.278 |
| `rrf_hybrid` | **0.7714** | 0.7417 | **0.6974** | 0.1026 | 0.9103 | 0.0319 | 0.0293 | 0.0026 | 50.178 | 99.398 |

### 3.2 Paired Bootstrap 95% 신뢰구간 (`Recall@5` 차이, `N=78`)

| Candidate | Baseline | Mean Diff | 95% CI Low | 95% CI High | 해석 |
| --- | --- | ---: | ---: | ---: | --- |
| `rrf_hybrid` | `bm25_lexical` | **`+0.2201`** | `+0.1517` | `+0.2949` | `rrf_hybrid`가 `bm25_lexical` 대비 통계적으로 유의하게 우위 (`Recall@5` +22.0%p) |
| `dense_vector` | `bm25_lexical` | **`+0.2158`** | `+0.1132` | `+0.3141` | `dense_vector`가 `bm25_lexical` 대비 통계적으로 유의하게 우위 |
| `rrf_hybrid` | `dense_vector` | `+0.0043` | `-0.0727` | `+0.0748` | `Recall@5`·`nDCG@5`는 통계적 동률이며, `dense_vector`가 `No-hit@5`(`0.0769`)·`SecHit@5`(`0.9487`) 및 기권 분리폭(`0.6614` vs `0.4565`)에서 유리함 |
