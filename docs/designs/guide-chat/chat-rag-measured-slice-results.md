# 복약 Chat RAG 실측 고도화 v1 중간 결과 및 팀·개인 프로젝트 비교

- **기준 브랜치 / 기준 리비전**: `feat/chat-rag-measured-slice-v1` (base `3297d83`)
- **작성일**: 2026-11-26 (KST)
- **관련 정본**:
  - [ADR 0004: 복약 Chat RAG 실측 고도화 v1 선정과 측정 기준](../../adr/0004-chat-rag-measured-slice-selection.md)
  - [제품 요구사항](chat-rag-measured-slice-requirements.md) (`CRM-01`–`CRM-13`)
  - [기능 명세](chat-rag-measured-slice-design.md) · [구현 계획](chat-rag-measured-slice-plan.md)
  - [공유 계약 제안/승인본 (`chat-claim-citation-v1.md`)](../../contracts/proposed/chat-claim-citation-v1.md)

> **상태 및 공개 경계**: 이 문서는 **Wave 1~4(기반 점검·계약·스키마·UI·식약처 평가 코퍼스 수집·검색/판정기 실측)** 완료 결과를 요약한다. Wave 5(런타임 검증기 연결·포트 `5433` 실시간 E2E·`SAFETY_REGRESSION` 93건·독립 리뷰)는 아직 남아 있으며, `PUBLIC_TRACK_F_ENABLED=false`와 [외부 승인 게이트](../../release-gates/post-mvp-1-external-approvals.md)는 닫힌 상태를 유지한다.

---

## 1. 한눈에 보는 결론 및 파이프라인 흐름

Wave 4까지 **검색(Retrieval)과 근거 검증(NLI + 안전 규칙)**을 같은 한국어 복약 평가 기준으로 정량 측정했다. 총 OpenAI 실험 비용은 **\$0.061792**(4주 상한 \$30의 약 `0.2%`)다.

```mermaid
flowchart LR
    Q["복약 질문"] --> R["검색 3종 비교 (90질의)<br/>dense_vector Recall@5 0.767 / rrf_hybrid 0.771"]
    R --> G["구조화 생성 (v7)<br/>Claim + chunk_id 매핑"]
    G --> V["판정 3종 비교 (60쌍)<br/>rule_plus_nli: 치명 오답 통과 0%, 0.33초, $0"]
    V -->|"SUPPORTED"| S["근거 확인 (DB·DTO·UI 구현 완료)"]
    V -->|"비치명적 NEEDS_VERIFICATION"| N["확인 필요 (DB·DTO·UI 구현 완료)"]
    V -->|"CONTRADICT / 치명적 미확인"| X["fail-closed 차단"]
```

- **검색 핵심 결과**: `text-embedding-3-large` 기반 의미 검색(`dense_vector`, `Recall@5 0.7671`)과 혼합 검색(`rrf_hybrid`, `Recall@5 0.7714`)이 글자 일치 기반 `bm25_lexical`(`0.5513`)보다 **`+22.0%p`** 유의하게 높았다(Paired Bootstrap 95% CI `[+0.1517, +0.2949]`).
- **판정 핵심 결과**: 로컬 NLI 모델(`MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`) 단독(`local_nli`)은 `Critical False-Support Rate 0.0500`으로 `ADR 0004` 상한(`<= 0.02`)을 넘겼으나, 한국어 수치·복용 시점·금기 규칙을 결합한 **`rule_plus_nli`**는 **치명적 오답 통과 `0.0000`**, **모순 재현율 `1.0000`**, **`Macro-F1 0.9666`**, **CPU `P95 332.594 ms`**, **추가 API 비용 `\$0`**을 기록했다.

---

## 2. 팀 프로젝트(`AH_05_04`) vs 개인 프로젝트(`Dosey`) 비교

과거 팀 프로젝트 시절의 기능 구현과, 개인 프로젝트로 전환한 뒤 **실측·고도화·안전 경계 보강**을 거치며 달라진 점은 아래와 같다.

| 비교 항목 | 과거 팀 프로젝트 (`AH_05_04` 기준) | 개인 프로젝트 고도화 (`Dosey` Wave 1–4) |
| --- | --- | --- |
| **개선 목표 및 방식** | 여러 기능 동시 구축, Chat RAG는 제한 시연(`CLOSED_DEMO`) 및 미연결 골격 위주 | `ADR 0004` 단일 수직 슬라이스 선정 → 지표·예산(\$30) 고정 후 ablation 기반 선택 |
| **생성 모델·프롬프트** | `chat-prompt-v6` 자유 텍스트 답변만 반환 (`citations` 없음) | `chat-prompt-v7-claim-citation` 구조화 JSON(`answer` + `claims[]` + `chunk_id`), 미등록 `chunk_id` fail-closed 차단 |
| **추가된 AI 모델** | OpenAI 생성·임베딩 중심 (로컬 NLI 판정 모델 미사용) | **`MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`** 로컬 CPU NLI(`551.51 MB`, `torch 2.11`·`sentence-transformers 5.2`) 추가 및 `gpt-4o-mini` 비교 기준선 확보 |
| **근거 검증 방식** | 답변 내 각 주장(Claim) 단위 지지·모순 자동 검증 미구현 | **`rule_plus_nli`**: 한국어 수치·용법(`1일 1회`/`2회`)·시점(`식전`/`식후`)·금기 규칙 + NLI 결합 (`Macro-F1 0.9666`, 치명 오답 통과 `0%`) |
| **검색 평가 기준** | 단일·합성 구성 중심, 한국어 구어체/기권 분리 정량 비교 없음 | 식약처 실데이터 `26품목·964청크` 기반 `90질의`(`pos 78` / `neg 12`)로 `bm25`·`dense`·`rrf` 동시 비교 및 95% CI 검증 |
| **공유 계약·DB 스키마** | `chat_citation` 테이블 미사용, `POST`/`GET` Chat DTO에 인용 필드 없음 | 승인 계약([chat-claim-citation-v1.md](../../contracts/proposed/chat-claim-citation-v1.md)) 기반 `SendChatMessageData`·`ChatMessageData` 선택 `citations` 추가, forward migration `926a1b2c3d4e` (Trigger/RLS 없이 `CHECK` 제약만 사용) |
| **프론트엔드 UI** | `AssistantMessageContent` 평문/Markdown 답변만 표시 | Markdown 안전 경계는 유지하면서 답변 하단에 `근거 확인` / `확인 필요` 출처 카드 분리 렌더링 (`ChatPage.tsx`) |
| **로컬 데이터·DB 격리** | 단일 Compose `postgres:5432` 전제, `nedrug` 수동 XML 의존 | 기존 `ah_05_04`(`5432`) 무변경 보호 + 포트 `5433` 격리 `pgvector 0.8.6` 컨테이너, 승인된 `data.go.kr` `15095677` (`DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06`) 비공개 로컬 수집 |

### 실측을 통해 발견·수정한 핵심 엔지니어링 사례

1. **NLI 입력 순서 역전 버그 수정 (`Support Recall 0.00 -> 1.00`)**: Wave 2 첫 스모크([nli-smoke 기록](../../archive/testing/chat-rag-nli-smoke-20261126.md))에서 `support` 2건이 모두 `neutral(0.9992)`로 무너졌다. 원인 분석 결과 `citation_nli_smoke.py:650`이 `(premise, hypothesis)` 대신 `(claim, evidence)` 역순을 넣었음을 확인했고, `citation_judge_compare.py`에서 `(evidence, claim)` 정순으로 고쳐 `Support Recall 1.0000`을 회복했다.
2. **CPU 지연 단축 (`P95 3645.7 ms -> 332.6 ms`)**: 25청크 장문을 전부 NLI에 넣어 3.65초가 걸리던 문제를 **한국어 어휘·바이그램 기반 Top-3 문장 사전 필터링**으로 줄여 약 `11배` 빠르게 만들었다.
3. **식약처 `15095677` 실제 운영 경로 정합화**: `DrugPrdtPrmsnInfoService07` 서비스 아래 상세 조회 오퍼레이션은 `getDrugPrdtPrmsnDtlInq06`이 HTTP `200`으로 응답함을 실측으로 확인해 [mfds_eval_corpus.py](../../../apps/ai_worker/tasks/evaluation/mfds_eval_corpus.py) 기본 경로에 반영했다.

---

## 3. Wave 1–4 정량 실측 결과

### 3.1 식약처 비공개 로컬 평가 코퍼스 (`mfds-eval-corpus-builder`)

상세 기록: [chat-rag-mfds-eval-corpus-20261126.md](../../archive/testing/chat-rag-mfds-eval-corpus-20261126.md)

| 항목 | 실측 값 | 비고 |
| --- | --- | --- |
| 입력 시드 / 수집 성공 / 스킵 | `40` / `26` / `14` | 스킵 14건: `EE`·`UD` 빈 DOC XML `13`건, 미매칭 `1`건(fail-closed 제외) |
| 생성 청크 수 (`EE` / `UD` / `NB`) | `964` (`51` / `113` / `800`) | 저장소 외부(`/private/tmp/dosey-mfds-eval-corpus`)에만 보관 |
| `chunks_sha256` / `manifest_sha256` | `4bc6f64e...` / `64b1fc14...` | `eval_corpus_local_only: true`, `release_eligible: false` |

### 3.2 검색 Ablation 실측 (`retrieval-ablation`, `90`질의)

상세 기록: [chat-rag-retrieval-ablation-20261126.md](../../archive/testing/chat-rag-retrieval-ablation-20261126.md) · 임베딩: `text-embedding-3-large` (`444,414` 토큰, **`$0.057774`**)

| Arm | Recall@5 | MRR@5 | nDCG@5 | No-hit@5 | SecHit@5 | PosTop1 | NegTop1 | Gap | P50 (ms) | P95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25_lexical` | 0.5513 | 0.5141 | 0.4631 | 0.2692 | 0.7949 | 52.1412 | 24.3696 | 27.7716 | 1.547 | 3.555 |
| `dense_vector` | 0.7671 | **0.7442** | 0.6963 | **0.0769** | **0.9487** | 0.6614 | 0.4565 | **0.2049** | 47.845 | 97.278 |
| `rrf_hybrid` | **0.7714** | 0.7417 | **0.6974** | 0.1026 | 0.9103 | 0.0319 | 0.0293 | 0.0026 | 50.178 | 99.398 |

- **Paired Bootstrap 95% CI (`Recall@5` 차이, `N=78`)**: `rrf_hybrid - bm25_lexical = +0.2201` (`[+0.1517, +0.2949]`), `dense_vector - bm25_lexical = +0.2158` (`[+0.1132, +0.3141]`), `rrf_hybrid - dense_vector = +0.0043` (`[-0.0727, +0.0748]`).

### 3.3 Citation 판정기 실측 (`citation-judge-compare`, `60`쌍)

상세 기록: [chat-rag-citation-judge-compare-20261126.md](../../archive/testing/chat-rag-citation-judge-compare-20261126.md) · OpenAI Judge 비용: **`$0.004018`**

| Judge | Macro-F1 | Support Recall | Neutral Recall | Contradict Recall | Crit Contradict Recall | Crit False-Support Rate | ADR 0004 (`<=0.02`) | P50 (ms) | P95 (ms) | Total Cost ($) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| `local_nli` | 0.9158 | 1.0000 | 0.9000 | 0.8500 | 0.7000 | 0.0500 | **FAIL** | 190.587 | 356.475 | 0.000000 |
| `rule_plus_nli` | **0.9666** | 1.0000 | 0.9000 | **1.0000** | **1.0000** | **0.0000** | **PASS** | **197.658** | **332.594** | **0.000000** |
| `openai_llm_judge` | **1.0000** | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0000 | **PASS** | 1286.966 | 2091.049 | 0.004018 |

---

## 4. 자동 검증 현황 및 Wave 5 남은 범위

| 구분 | 실행 범위 및 결과 |
| --- | --- |
| **Backend / AI Worker / 계약 (`PASS`)** | `chat_ai`·`test_chat_citation_contract.py`·`test_chat_service.py`·`evaluation` 신규 4종 테스트 통과, `tests/contract/` (`chat_ai`, `database_logic_policy`, `backend_ai_worker_import_boundary`) `12 passed`, `ruff`/`mypy`(`987` files)/`verify_database_head.py`(`926a1b2c3d4e`)/`check_agent_hygiene.py` 통과 |
| **Frontend (`PASS`)** | `ChatApi`·`ChatPage`·`AssistantMessageContent` `88 passed` (전체 `vitest` 49개 파일 `992 passed`), `tsc -b`, `vite build`, `oxlint` 0 오류 |
| **격리 PostgreSQL (`PASS`)** | 포트 `5433` `dosey-rag-eval-pg`에서 `alembic upgrade head` (`926a1b2c3d4e`) 및 `pgvector 0.8.6`·`pg_trgm 1.6` 적용 완료 |
| **Wave 5 남은 작업 (`NOT_RUN`)** | `chat-citation-validator-wire` (`rule_plus_nli` 런타임 연결), 실시간 Backend↔DB↔UI E2E 검증, `SAFETY_REGRESSION` 93건 1회 실행, `dosey_reviewer` 최종 리뷰 |
| **참고 한계** | 1) 검색 90질의·판정 60쌍은 현재 결정적 합성 평가셋 기준이며, 2) `evaluation/` 전체 묶음의 기존 `atomic publish` 테스트 88건은 macOS 샌드박스 `/var/folders` 제약(`EVAL_ATOMIC_PUBLISH_UNSUPPORTED`)으로 별도 격리 실행이 필요하다. |
