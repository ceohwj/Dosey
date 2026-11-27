# 복약 Chat RAG 식약처 비공개 로컬 평가 코퍼스 구축 기록 (`mfds-eval-corpus-builder`)

```text
EVAL_CORPUS_LOCAL_ONLY = true — LIVE API FETCH COMPLETED (RELEASE_ELIGIBLE = false)
```

- **날짜**: 2026-11-26
- **Task ID**: `mfds-eval-corpus-builder`
- **브랜치 / 기준 revision**: `feat/chat-rag-measured-slice-v1` (`3297d83`)
- **관련 요구사항**: [chat-rag-measured-slice-requirements.md](../../designs/guide-chat/chat-rag-measured-slice-requirements.md) (`CRM-01`)
- **정책 플래그**: `EVAL_CORPUS_LOCAL_ONLY = true`, `RELEASE_ELIGIBLE = false`, `PUBLIC_TRACK_F_ENABLED = false`

## 1. Human Owner 결정 (2026-11-26 A2)

- 사용자의 활성 `data.go.kr` 개발계정 API(`15095677` 식품의약품안전처 의약품 제품 허가정보, `getDrugPrdtPrmsnDtlInq06`)를 **비공개 로컬 평가 코퍼스(30~50개 품목)** 구축 목적에 한해 사용 승인했다.
- 기존 공개 경로 차단(`PUBLIC_TRACK_F_ENABLED=false`)과 적재 승인 경계(`apps/ai_worker/admin/mfds_label_writer.py`의 `LICENSE_VERIFIED` 규칙)는 변경하지 않는다.
- 수집된 원문과 청크(`corpus_chunks.jsonl`, `manifest.json`)는 git 저장소 외부 경로(기본값 `/tmp/dosey-mfds-eval-corpus`)에만 저장하며 저장소에 커밋하지 않는다.

## 2. 생성 파일

| 파일 | 역할 |
| --- | --- |
| `apps/ai_worker/tasks/evaluation/mfds_eval_corpus.py` | 식약처 `15095677` (`getDrugPrdtPrmsnDtlInq06`) 비공개 로컬 평가 코퍼스 빌더. 8개 질환군 40개 대표 약물 seed(`DEFAULT_EVAL_SEED_ITEMS`), `EE`/`UD`/`NB` `DOC` XML 파싱·청킹(`parse_mfds_doc_xml`), 결정적 `EvalCorpusChunk`(`chunk_id`, `content_sha256`), 저장소 내부 출력 차단 가드, `RAG_MFDS_API_KEY` fail-closed 및 비노출 처리, `--dry-run` CLI 제공 |
| `apps/ai_worker/tests/evaluation/test_mfds_eval_corpus.py` | `httpx.MockTransport` 기반 100% 오프라인 단위 테스트 14건 (`DOC` XML 파싱, 잘못된/빈 XML fail-closed, 저장소 내부 출력 거부, API 키 누락 시 무호출 차단, 에러·출력 내 키 비노출, `--dry-run` CLI 검증) |
| `docs/archive/testing/chat-rag-mfds-eval-corpus-20261126.md` | 본 A2 결정 기록, 오프라인 단위 테스트·정적 검사 결과, Live 실행 절차 |

## 3. 오프라인 단위 테스트 및 정적 검사 결과 (`PASS`)

| 검사 | 명령 | 결과 |
| --- | --- | --- |
| 오프라인 단위 테스트 | `PYTHONPATH=apps:packages:. uv run --no-sync pytest apps/ai_worker/tests/evaluation/test_mfds_eval_corpus.py -q` | `14 passed in 0.37s` |
| 린트 | `uv run --no-sync ruff check apps/ai_worker/tasks/evaluation/mfds_eval_corpus.py apps/ai_worker/tests/evaluation/test_mfds_eval_corpus.py` | `All checks passed!` |
| 포맷 | `uv run --no-sync ruff format --check apps/ai_worker/tasks/evaluation/mfds_eval_corpus.py apps/ai_worker/tests/evaluation/test_mfds_eval_corpus.py` | `2 files already formatted` |
| 타입 검사 | `uv run --no-sync mypy apps/ai_worker/tasks/evaluation/mfds_eval_corpus.py apps/ai_worker/tests/evaluation/test_mfds_eval_corpus.py` | `Success: no issues found in 2 source files` |
| CLI Dry-Run | `PYTHONPATH=apps:packages:. uv run --no-sync python -m ai_worker.tasks.evaluation.mfds_eval_corpus --dry-run` | `PASS` (`planned_seed_count: 40`, `output_dir: /private/tmp/dosey-mfds-eval-corpus`) |

## 4. Live API 수집 실행 결과 (`PASS` — 저장소 외부 저장)

실제 공공데이터포털(`apis.data.go.kr`)에서 `DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06` 경로가 HTTP `200`으로 동작함을 확인하고(`DEFAULT_ENDPOINT_URL`도 함께 정합화 완료), `RAG_MFDS_API_KEY`로 40개 시드를 조회해 저장소 외부 경로에 적재했다.

- **실행 명령**: `python -m ai_worker.tasks.evaluation.mfds_eval_corpus --endpoint-url https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06 --output-dir /tmp/dosey-mfds-eval-corpus --limit 40`

| 항목 | 관측 값 | 비고 |
| --- | --- | --- |
| 입력 시드 수 (`seed_count`) | `40` | 8개 질환군 대표 약물 |
| 수집 성공 품목 수 (`fetched_item_count`) | `26` | 7개 질환군 (`hypertension` 4, `diabetes` 5, `dyslipidemia` 5 등) |
| 스킵 시드 수 (`skipped_seed_count`) | `14` | `XML_PARSE_ERROR` (`EE` 또는 `UD` 빈 DOC XML) `13`건, `NO_MATCH_WITH_DOC` `1`건 (fail-closed 제외) |
| 생성 청크 수 (`chunk_count`) | `964` | `EE: 51`, `UD: 113`, `NB: 800` |
| 출력 경로 (`output_dir`) | `/private/tmp/dosey-mfds-eval-corpus` | `eval_corpus_local_only: true`, `release_eligible: false`, `public_track_f: false` |
| `chunks_sha256` | `4bc6f64ee9439865c0b2d06c6d419ac41d7887a717282276d9cf949b497eb628` | `corpus_chunks.jsonl` |
| `manifest_sha256` | `64b1fc14854d70f32de7d1e14b5a0a34ab7ad7c6454b6a2f66168bdcd6b8640b` | `manifest.json` |
