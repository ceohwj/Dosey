# 복약 Chat RAG 로컬 NLI Citation Smoke 기록 (`nli-smoke`)

```text
LOCAL_NLI_SMOKE_ONLY — LIVE HUGGINGFACE MODEL EXECUTED (RELEASE_ELIGIBLE = false)
```

- **날짜**: 2026-11-26
- **Task ID**: `nli-smoke`
- **기준 revision**: `3297d83`
- **대상 모델**: `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`
- **Release Eligible**: `false`

## 1. 생성 파일

| 파일 | 역할 |
| --- | --- |
| `apps/ai_worker/tasks/evaluation/citation_nli_smoke.py` | 로컬 NLI citation smoke 실행기 (문장 단위 분할·클래스별 max 점수 집계·3-class 정규화·지연 P50/P95·Peak RSS·JSON/Markdown CLI·lazy import fail-closed) |
| `apps/ai_worker/tests/evaluation/test_citation_nli_smoke.py` | 오프라인 단위 테스트 6건 (네트워크·HuggingFace 다운로드 없음) |
| `docs/archive/testing/chat-rag-nli-smoke-20261126.md` | 본 오프라인 테스트 및 Live HuggingFace 스모크 실측 기록 |

## 2. 오프라인 단위 테스트 및 정적 검사 결과 (`PASS`)

`.venv`에 `[dependency-groups] ai`를 설치하기 전 오프라인 scorer 상태에서 단위 테스트와 정적 검사를 통과했고, 이후 `--all-groups`(`ai` 포함) 동기화 후 통합 검증에서도 재확인했다.

| 검사 | 명령 | 결과 |
| --- | --- | --- |
| 단위 테스트 | `PYTHONPATH=apps:packages:. uv run --no-sync pytest apps/ai_worker/tests/evaluation/test_citation_nli_smoke.py -q` | `6 passed in 0.33s` |
| 린트 | `uv run --no-sync ruff check apps/ai_worker/tasks/evaluation/citation_nli_smoke.py apps/ai_worker/tests/evaluation/test_citation_nli_smoke.py` | `All checks passed!` |
| 포맷 | `uv run --no-sync ruff format --check apps/ai_worker/tasks/evaluation/citation_nli_smoke.py apps/ai_worker/tests/evaluation/test_citation_nli_smoke.py` | `2 files already formatted` |
| 타입 검사 | `uv run --no-sync mypy apps/ai_worker/tasks/evaluation/citation_nli_smoke.py apps/ai_worker/tests/evaluation/test_citation_nli_smoke.py` | `Success: no issues found in 2 source files` |

## 3. `.venv` AI 의존성 동기화 (`PASS`)

`pyproject.toml`과 `uv.lock`은 수정하지 않고 기존 선언된 `[dependency-groups] ai`를 `.venv`에 설치했다(이후 `uv sync --all-groups`로 `app`·`worker`·`dev`·`ai` 그룹 전체 동기화 확인).

- **확인된 런타임 패키지**: `torch==2.11.0`, `sentence-transformers==5.2.0` (`ai_group_ready=True`)
- **저장소 오염 없음**: `pyproject.toml`, `uv.lock` 미수정 및 모델 가중치 저장소 미포함(`git status --short` 확인)

## 4. Live HuggingFace 모델 실측 결과 (`PASS` — 단, 품질·지연 결정 항목 관측)

### 4.1 실행 환경 및 자원 측정치

실행 명령:

```bash
PYTHONPATH=apps:packages:. .venv/bin/python -m ai_worker.tasks.evaluation.citation_nli_smoke --format markdown
```

| 측정 항목 | 관측 값 | 비고 |
| --- | --- | --- |
| 대상 모델 (`Model`) | `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` | `device=cpu`, `max_tokens=512`, `max_chars=None` |
| 모델 캐시 크기 (`Model Cache Size`) | `551.51 MB` | HuggingFace 로컬 캐시(`~/.cache/huggingface`), 저장소 외부 |
| 설치·다운로드 후 남은 디스크 (`/System/Volumes/Data`) | `14Gi` (`228Gi` 중 `178Gi` 사용, `93%`) | `df -h /System/Volumes/Data` |
| 첫 로드 시간 (`Load Seconds`) | `36.949 s` | 최초 모델 파일 다운로드(`model.safetensors` 558M 등) + 가중치 적재 포함 |
| 지연 중앙값 (`Latency P50`) | `246.331 ms` | 단문 1청크 쌍 기준 212~622ms |
| 지연 95백분위 (`Latency P95`) | `3645.742 ms` | 25청크 장문 사례(`nli-smoke-04-long-support`) 영향 |
| 최대 메모리 (`Peak RSS`) | `778.22 MB` | CPU 프로세스 기준 |

### 4.2 한국어 합성 Claim-근거 쌍 실제 판정 결과

| Case ID | Expected Hint | Predicted | Support | Contradict | Neutral | Chunks | Selected Chunk | Latency (ms) |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `nli-smoke-01-support` | `support` | `neutral` | 0.0005 | 0.0002 | 0.9992 | 1 | 0 | 621.752 |
| `nli-smoke-02-contradict` | `contradict` | `contradict` | 0.0011 | 0.9981 | 0.0008 | 1 | 0 | 246.331 |
| `nli-smoke-03-neutral` | `neutral` | `neutral` | 0.0044 | 0.0003 | 0.9954 | 1 | 0 | 236.045 |
| `nli-smoke-04-long-support` | `support` | `neutral` | 0.0027 | 0.0059 | 0.9992 | 25 | 4 | 3645.742 |
| `nli-smoke-05-contradict-dose` | `contradict` | `contradict` | 0.0252 | 0.9669 | 0.0079 | 1 | 0 | 212.103 |

## 5. `ADR 0004` / `citation-judge-compare` 단계로 넘길 핵심 관측 및 결정 사항

1. **한국어 `support` 판정 보수성/오차 관측**: 합성 모순 사례 2건(`contradict`: `0.9981`, `0.9669`)과 중립 사례 1건(`neutral`: `0.9954`)은 정확히 구분했으나, **지지(`support`) 기대 사례 2건(`nli-smoke-01-support`, `nli-smoke-04-long-support`) 모두 `neutral` (`0.9992`)로 예측되었다.** (`support` 점수는 각각 `0.0005`, `0.0027`)
   - 후속 `citation-judge-compare` 단계에서 `(premise, hypothesis)` 입력 순서·프롬프트/문장 단위 매칭 방식 점검과 함께, `LLM-judge` 및 `규칙(수치/단위/부정어) + NLI` 조합 또는 다른 NLI 모델 후보 비교 측정이 필요하다.
2. **장문 청크 CPU 지연 (`P95 = 3645.742 ms`)**: 1청크 단문은 약 `212~246 ms`(초회 warmup `621.752 ms`)이나, 25문장 장문 청크(`nli-smoke-04-long-support`)에서는 `3645.742 ms`로 500ms 기준을 크게 초과했다.
   - 런타임 NLI 적용 시 청크 크기 상한(`max_tokens`/`top-k` 문장 사전 필터링) 또는 비차단/하이브리드 판정 예산 설계가 필요하다.
3. **토크나이저 길이 경고**: stderr에 `Token indices sequence length is longer than the specified maximum sequence length for this model (1055 > 512)` 경고가 출력되었다. 예산 계산용 사전 토크나이즈 경로가 `max_length` 없이 전체 텍스트를 토크나이즈했기 때문으로 보이며, 후속 비교 스크립트 개선 시 점검 대상이다.
