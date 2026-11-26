# ADR 0004: 복약 Chat RAG 실측 고도화 v1 선정과 측정 기준

- 상태: Accepted (기능 선정과 측정 기준만. 구현·공유 계약 변경·Source 이용·공개는 승인하지 않음)
- 결정일: 2026-11-25 (KST)
- 결정자: Human Owner
- 근거 문서: [제품 요구사항](../designs/guide-chat/chat-rag-measured-slice-requirements.md) 10절, [기능 명세](../designs/guide-chat/chat-rag-measured-slice-design.md), [구현 계획](../designs/guide-chat/chat-rag-measured-slice-plan.md)

## 배경

[PRD](../../PRD.md)는 Human Owner가 다음 고도화 기능을 선정하고, 기능마다 baseline·목표·평가 데이터·안전 조건을 먼저 기록하도록 정했다. 복약 Chat은 답변에 근거와 인용을 연결하지 않는다. 검색 구성과 Citation 판정기도 같은 평가셋으로 비교한 기록이 없다. 2026-11-25 확인 결과 로컬 pgvector 인덱스는 비어 있었다.

## 결정

1. **기능 선정**: "복약 Chat RAG 실측 고도화 v1"(Chat Q&A 한 흐름, 4주)을 다음 고도화 기능으로 선정한다.
2. **측정 기준**: 결과를 본 뒤 기준을 바꾸지 않는다.
   - 최종 검색 구성 Recall@5 ≥ 0.90 (평가용 분할)
   - 단계 제거: Recall@5 차이의 paired bootstrap 95% CI 하한 ≥ −0.03이고 P95 지연이 줄 때
   - Claim Support Precision ≥ 0.95, Citation Coverage ≥ 0.95
   - Critical Unsupported Claim 0건. Critical Claim이 30건 이상일 때만 판정
   - 판정기 채택: Macro F1 ≥ 0.80, κ ≥ 0.70, FSR ≤ 0.05, Critical 오인용 0건. 모두 탈락하면 검증기를 연결하지 않고 인용 표시를 막음
   - `neutral` Claim은 비치명적 일반 안내만 "확인 필요"로 표시하고 지표 분모에 포함. Critical·`contradict` Claim은 항상 제외
   - Safety 회귀셋 `SAFETY_REGRESSION` 93건은 최종 1회만 사용, 악화 0건
3. **Source**: nedrug 허가사항 XML(효능효과·용법용량·사용상의 주의사항), 약 200개 제품, 흔한 성분 군집 중심. 원문은 로컬 private 디렉터리에만 둔다. 이용 조건이 불명확하면 코퍼스 작업을 멈추고 다시 결정한다.
4. **Chat DTO**: 하위 호환 선택 필드 추가만 허용한다. 실제 계약 변경은 baseline 추적 뒤 `CONTRACT_REQUEST`로 따로 결정한다.
5. **외부 API**: OpenAI만 사용하고 LLM Judge는 생성 모델과 다른 모델을 쓴다. 보내는 내용은 합성 질의와 공개 문서로 한정한다. 4주 합계 US$30, 80%에서 멈춘다.

## 검토한 대안

| 대안 | 제외 이유 |
| --- | --- |
| 단계 제거 허용폭 −0.02 유지 | 질의 150~200건에서는 CI 폭이 넓어 어떤 단계도 제거할 수 없을 가능성이 큼 |
| e약은요 OpenAPI 함께 사용 | natural-key blocker와 새 청크 경로가 필요해 4주 범위를 넘음. 후속 확장 후보 |
| 판정기 모두 탈락 시 최고점 채택 | 기준 미달 검증기를 의료 답변에 연결하는 것은 fail-closed 원칙과 맞지 않음 |
| Judge를 다른 Provider로 | 새 의존성·키·비용 경로가 추가됨 |

## 영향

- 보안·개인정보: 실제 환자정보·처방전을 쓰지 않는다. 외부 API에는 합성 질의와 공개 문서만 보낸다.
- 운영: Production 공개나 실제 사용자 활성화를 포함하지 않는다.
- 테스트: 평가 실행마다 dataset·corpus·config hash, model·prompt version, commit, 명령을 기록한다.
- 이 ADR은 `LICENSE_VERIFIED` 등 Source 이용 승인, 공유 계약 변경, 외부 승인 gate를 대신하지 않는다.

## 관련 Issue와 PR

없음 (2026-11-25 기준 미생성).
