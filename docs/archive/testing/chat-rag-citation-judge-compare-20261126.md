# 복약 Chat RAG Citation 판정기 비교 구현 및 Wave 2 NLI 진단 (`citation-judge-compare`)

- **날짜**: 2026-11-26
- **Task ID**: `citation-judge-compare`
- **기준 revision**: `3297d83`
- **비교 대상 판정기**: `local_nli`, `rule_plus_nli`, `openai_llm_judge` (`gpt-4o-mini` 기본값)
- **안전 기준**: [ADR 0004](../../adr/0004-chat-rag-measured-slice-selection.md) (`Critical False-Support Rate <= 0.02`, 채택 참고 `Macro-F1 >= 0.80`)

## 1. Wave 2 `citation_nli_smoke.py` 한국어 `support -> neutral` 현상 진단

Wave 2 스모크([chat-rag-nli-smoke-20261126.md](chat-rag-nli-smoke-20261126.md))에서 `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`가 모순(`contradict`)과 중립(`neutral`)은 정확히 판정했으나 지지(`support`) 2건(`nli-smoke-01-support`, `nli-smoke-04-long-support`)을 모두 `neutral` (`0.9992`)로 예측한 원인을 코드와 로컬 캐시 모델(`HF_HUB_OFFLINE=1`)로 확인했다.

1. **주원인 — `(premise, hypothesis)` 입력 순서 역전 (`citation_nli_smoke.py` L650)**:
   - `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`의 `CrossEncoder`는 `(premise, hypothesis)` 순서를 요구하며 `id2label`은 `{0: 'entailment', 1: 'neutral', 2: 'contradiction'}`이다.
   - 그러나 [citation_nli_smoke.py](../../../apps/ai_worker/tasks/evaluation/citation_nli_smoke.py) L650은 `[(claim, chunk) for chunk in chunks]`로 `(hypothesis, premise)` 역순 입력을 전달했다.
   - 모순(`contradiction`)과 중립(`neutral`)은 대칭성이 강해 역순에서도 유지되지만, 함의(`entailment`)는 비대칭이므로 정보가 더 적은 `claim`을 `premise`로 두면 구체적 용법(`성인 기준 1일 3회, 1회 1정`)이 담긴 `evidence`를 함의하지 못해 `neutral`로 붕괴한다.
   - 로컬 오프라인 검증 결과(`nli-smoke-01-support`):
     - 역순 `(claim, evidence)`: `entailment=0.000535`, `neutral=0.999023`, `contradiction=0.000228` (`neutral`)
     - 정순 `(evidence, claim)`: `entailment=0.998535`, `neutral=0.001471`, `contradiction=0.000116` (`support`)
2. **부원인 — 장문 다청크 독립 max 집계 및 사전 토크나이즈(`citation_nli_smoke.py` L559, L659)**:
   - 25청크 장문 사례(`nli-smoke-04-long-support`)에서 라벨별 독립 `max`를 취해 관련 없는 24개 배경 청크의 높은 `neutral` 확률(`0.9992`)이 핵심 청크와 경쟁했고, 전체 25청크 추론으로 CPU P95가 `3645.742 ms`까지 증가했다.
   - 또한 예산 계산 중 미분할 1,055토큰 문자열을 `tokenizer.encode`에 전달해 `> 512` 경고가 발생했다.
3. **`citation_judge_compare.py` 개선 반영 사항**:
   - NLI 추론 전 한국어 어휘·바이그램·수치/단위 겹침 기반 **Top-k (`max_sentences=3`) 문장 사전 필터링** 및 `max_tokens=512` 예산 절삭을 적용해 1회 추론으로 제한했다.
   - 입력 쌍을 **`(evidence_premise, claim_hypothesis)`** 정순으로 고정하고 `id2label` 및 확률 임계치(`support_threshold=0.50`, `contradict_threshold=0.45`)를 명시 검증했다.

## 2. 생성 파일

| 파일 | 역할 |
| --- | --- |
| [citation_judge_compare.py](../../../apps/ai_worker/tasks/evaluation/citation_judge_compare.py) | 60건 한국어 합성 MFDS Claim-Evidence 벤치마크(`support` 20 / `neutral` 20 / `contradict` 20, `critical` 30 / `non_critical` 30), 3개 판정기(`local_nli`, `rule_plus_nli`, `openai_llm_judge`), 지표 계산, `--offline-smoke` 및 JSON/Markdown CLI |
| [test_citation_judge_compare.py](../../../apps/ai_worker/tests/evaluation/test_citation_judge_compare.py) | 오프라인 단위 테스트 7건 (데이터셋 균형, 지표 산식, Top-k/512토큰 제한, NLI 정순 및 fail-closed, 안전 규칙, OpenAI JSON 파싱/fail-closed/비용 계산, `--offline-smoke` CLI) |
| [chat-rag-citation-judge-compare-20261126.md](chat-rag-citation-judge-compare-20261126.md) | Wave 2 NLI 원인 진단, 오프라인 검증 결과, Live 실행 대기 상태 기록 |

## 3. 오프라인 검증 및 정적 검사 결과 (`PASS`)

외부 유료 OpenAI API 호출 및 HuggingFace 다운로드 없이 오프라인 검증을 완료했다.

| 검사 항목 | 명령 | 결과 |
| --- | --- | --- |
| 단위 테스트 | `PYTHONPATH=apps:packages:. uv run --no-sync pytest apps/ai_worker/tests/evaluation/test_citation_judge_compare.py -q` | `7 passed in 0.37s` (`PASS`) |
| 린트 | `uv run --no-sync ruff check apps/ai_worker/tasks/evaluation/citation_judge_compare.py apps/ai_worker/tests/evaluation/test_citation_judge_compare.py` | `All checks passed!` (`PASS`) |
| 포맷 | `uv run --no-sync ruff format --check apps/ai_worker/tasks/evaluation/citation_judge_compare.py apps/ai_worker/tests/evaluation/test_citation_judge_compare.py` | `2 files already formatted` (`PASS`) |
| 타입 검사 | `uv run --no-sync mypy apps/ai_worker/tasks/evaluation/citation_judge_compare.py apps/ai_worker/tests/evaluation/test_citation_judge_compare.py` | `Success: no issues found in 2 source files` (`PASS`) |
| CLI 오프라인 스모크 | `PYTHONPATH=apps:packages:. uv run --no-sync python -m ai_worker.tasks.evaluation.citation_judge_compare --offline-smoke --format markdown` | 정상 종료(`0`), 60건·3개 판정기 표 출력 (`PASS`) |

## 4. Live 실행 실측 결과 (`PASS` — `gpt-4o-mini` + 로컬 CPU NLI)

실행 명령:

```bash
PYTHONPATH=apps:packages:. .venv/bin/python -m ai_worker.tasks.evaluation.citation_judge_compare --format markdown
```

- **대상 벤치마크**: 60건 (`support` 20 / `neutral` 20 / `contradict` 20, `critical` 30 / `non_critical` 30)
- **모델 구성**: NLI `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` (`cpu`), LLM Judge `gpt-4o-mini`

| 판정기 | Macro-F1 | Support Recall | Neutral Recall | Contradict Recall | Crit Contradict Recall | Crit False-Support Rate | ADR 0004 (`<=0.02`) | P50 (ms) | P95 (ms) | Total Cost ($) | $/100 Claims | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: | ---: |
| `local_nli` | 0.9158 | 1.0000 | 0.9000 | 0.8500 | 0.7000 | 0.0500 | **FAIL** | 190.587 | 356.475 | 0.000000 | 0.000000 | 0 |
| `rule_plus_nli` | **0.9666** | 1.0000 | 0.9000 | **1.0000** | **1.0000** | **0.0000** | **PASS** | **197.658** | **332.594** | **0.000000** | **0.000000** | 0 |
| `openai_llm_judge` | **1.0000** | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0000 | **PASS** | 1286.966 | 2091.049 | 0.004018 | 0.006697 | 0 |

### 판정기 선택 판단 근거

1. `(evidence, claim)` 정순 정렬과 Top-k(`3`문장) 사전 필터링으로 `local_nli`의 `Support Recall`이 Wave 2의 `0.0000`에서 **`1.0000`**으로 회복되었고 CPU `P95` 지연도 **`356.475 ms`**(`500 ms` 이하)로 내려갔다.
2. 그러나 `local_nli` 단독은 수치·복용 시점·금기 모순을 놓쳐 `Critical Contradict Recall = 0.7000`, `Critical False-Support Rate = 0.0500`(`ADR 0004` 상한 `0.02` 초과)으로 `FAIL`했다.
3. **`rule_plus_nli`**는 결정적 한국어 수치·단위·시점·금기 규칙 가드를 결합해 **`Critical False-Support Rate = 0.0000`**, **`Critical Contradict Recall = 1.0000`**, **`Macro-F1 = 0.9666`**을 추가 API 비용 `$0`과 CPU `P95 332.594 ms`로 달성했다.
4. `openai_llm_judge`(`gpt-4o-mini`)는 `Macro-F1 = 1.0000`으로 가장 높았으나 `P95 = 2091.049 ms`로 지연이 크므로, 런타임 기본 검증기 후보는 **`rule_plus_nli`**를 우선 채택한다.
