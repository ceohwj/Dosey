# Codex · Antigravity 역할·모델 설정과 검증 범위

Codex 설정 정본은 `.codex/config.toml`과 `.codex/agents/*.toml`이며, Antigravity 진입점은 [GEMINI.md](../../GEMINI.md)다. 운영 절차는 [workflow](workflow.md), 작업 입력/결과는 [task-template](task-template.md), 공통 권한은 [AGENTS](../../AGENTS.md)를 따른다. 이 문서는 설정을 실행하거나 모델 선택을 덮어쓰지 않는다.

## Codex 모델 배분

| 역할 | 설정 모델 | reasoning | 작업 |
|---|---|---|---|
| 프로젝트 main 기본값 | `gpt-6.2-sol` | `medium` | Integrator·작은 직접 작업 |
| `dosey_explore` | `gpt-6.2-sol` | `low` | bounded 조사 |
| `dosey_writer` | `gpt-6.2-sol` | `medium` | 확정 사실 문서화 |
| `dosey_executor` | `gpt-6.1-terra` | `medium` | 일반·혼합 구현 |
| `dosey_frontend` | `gpt-6.1-terra` | `medium` | UI·상태·API 소비 |
| `dosey_backend` | `gpt-6.1-terra` | `medium` | API·Service/Repository·Job 연결 |
| `dosey_llm` | `gpt-6.1-terra` | `medium` | Guide/Chat 생성·검증 |
| `dosey_ocr` | `gpt-6.1-terra` | `medium` | 인식·정규화·구조화 |
| `dosey_rag` | `gpt-6.1-terra` | `medium` | Source·검색·evidence/citation·평가 |
| `dosey_integration_verifier` | `gpt-6.2-sol` | `medium` | 계층 간 독립 검증 |
| `dosey_reviewer` | `gpt-6.2-sol` | `high` | 중요한 변경의 내부 독립 검토 |

2026-11-23 Human Owner 지시로 Codex 로컬 카탈로그(`~/.codex/models_cache.json`)에서 `gpt-6.2-sol` 및 지원 reasoning 값을 확인하고, 기존 `gpt-6.1-sol`을 사용하던 5개 역할(main 기본값·explore·writer·integration_verifier·reviewer)만 `gpt-6.2-sol`로 교체했다. 구현 6개 역할의 `gpt-6.1-terra`와 기존 reasoning·sandbox/권한·역할 지침은 그대로 유지했다. (2026-11-05 최초 6.1 Sol/Terra 배분 이력은 보존하며, 전역 Codex/OMX와 서비스 LLM 모델은 바꾸지 않는다.)

## Antigravity 역할별 모델 배분

Antigravity는 `.codex/agents/*.toml` 대신 [GEMINI.md](../../GEMINI.md)와 이 문서의 기준에 따라 `/teamwork`(Swarm) 워커의 `model` 핀 지정 또는 `invoke_subagent` 티어를 역할별로 배정한다.

| 역할 그룹 | 해당 역할 | Swarm (`/teamwork`) 모델 | Subagent 티어 | 작업 |
|---|---|---|---|---|
| **탐색** | `dosey_explore` | `gemini-4-hydrogen-low` | `flash` | bounded 조사 (read-only) |
| **문서** | `dosey_writer` | `gemini-4-hydrogen-medium` | `flash` | 확정 사실 문서화 |
| **구현** | `dosey_executor`, `dosey_frontend`, `dosey_backend`, `dosey_llm`, `dosey_ocr`, `dosey_rag` | `gemini-4-neon-medium` | `inherit` | 도메인·혼합 구현 (필요 시 복잡 작업만 `high`) |
| **통합 검증** | `dosey_integration_verifier` | `gemini-4-argon-medium` | `pro` | 계층 간 독립 검증 (read-only) |
| **내부 리뷰** | `dosey_reviewer` | `gemini-4-argon-high` | `pro` | 중요 변경 내부 독립 검토 (read-only) |

- 메인 코디네이터(Integrator)는 기본적으로 `gemini-4-neon-medium`에서 전체 맥락·파일 소유권·수리 루프(최대 2~3회 상한)를 관리하고, 계약·DB·안전·최종 승인 지점에서 Human Owner에게 확인을 요청한다.
- 이 표는 승인된 모델 라우팅 기준이며, 실행 검증이나 테스트 PASS 증거를 대신하지 않는다.

현재 main의 UI/CLI/profile 선택이나 이미 실행한 에이전트의 모델을 소급 변경하지 않는다. 프로젝트 main 기본값은 이후 해당 설정을 로드하는 실행에 적용된다. 갱신 뒤 현재 App의 Dosey 역할 도구 정의에서도 구현 Terra/검수 Sol 6.1 배분이 노출된 것을 확인했으며, 남은 검수는 새 호출로 수행한다. 도구 정의·TOML 로딩과 실제 provider 모델 실행 기록은 구분하며 이 표 자체는 실행 증거가 아니다.

## 역할과 권한

도메인은 전문성 기준이며 영구 디렉터리 소유권이 아니다. 메인 Integrator가 겹치지 않는 파일·선행 계약·검증을 지정해 준비된 작업만 병렬 실행한다. 현재 host 동시 실행 한도를 따른다. 작은 작업은 직접 처리하며 역할별 팀을 상시 띄우지 않는다.

탐색·review·통합 verifier의 지침은 read-only이고 구현·writer는 지정 파일 수정이다. 기존 역할 파일의 sandbox_mode 선언은 보존했다. 현재 공식 문서는 역할이 부모의 sandbox·승인·provider·MCP를 상속한다고 설명하므로, 파일의 sandbox_mode나 read-only 문구만으로 별도 OS 격리가 보장된다고 주장하지 않는다. 부모 세션의 실제 정책과 tool 권한을 확인한다. 전역 Codex, Claude, OMX Team 설정이나 서비스 LLM 모델은 변경하지 않는다.

## 외부 도구

Claude Code는 Codex 내부 역할의 model로 등록하지 않았다. Integrator가 `scripts/claude_review.py`로 선택한 packet에 대한 외부 검토를 요청한다. 명령·자료 범위·로그인·결과·실패 처리는 workflow에 있다. 내부 reviewer는 빠른 검토/외부 검토 불가 시 명시적인 대안이며 외부 검토를 실행한 것처럼 기록하지 않는다.

Antigravity는 Human Owner의 수동 구현 도구다. 현재 자동 호출·로그인 wrapper·새 MCP나 framework는 구성하지 않는다. 각 도구에 별도 공통 규칙을 복제하지 않고 CLAUDE.md 등의 진입점과 task 계약으로 연결한다.

## 증거와 한계

- 이전 문서의 2026-09-24 CLI 0.156.1 환경에서 dosey_explore를 실행해 Luna/low를 관찰한 기록은 과거 증거다. 새 다섯 역할의 현재 실행 검증으로 재사용하지 않는다.
- 이번 작업에서 설치 CLI는 Codex 0.163.0, Claude Code 2.1.301로 확인했다. 버전 정보는 실제 모델 실행이나 권한 검증을 대신하지 않는다.
- `codex debug prompt-input`에서 새 다섯 이름이 모두 발견됨을 확인했다(모델 추론 없음). TOML 10개 구문·이름/경로 참조와 main 설정 보존을 확인했다. 이 결과는 각 역할의 실제 모델 실행이나 병렬 구현 품질을 검증한 것이 아니다.
- Claude 로그인은 sandbox 밖의 기존 인증에서 유효했고, 합성 덧셈 회귀 packet의 실제 CLI 검토가 구조화된 지적을 반환했다. 이어 Human Owner가 명시 승인한 선택 파일 packet 검토도 보고서를 반환했다. 지적 처리·최종 회귀 결과는 해당 task의 보고와 `outputs/claude-reviews/` metadata에 구분해서 기록한다. 실제 CLI 성공도 테스트 실행·공개 승인 증거가 아니다.
- 이미 열린 App 대화는 기존 역할 목록을 유지할 수 있다. 새 작업/세션에서 다섯 이름이 도구에 노출되는지 확인해야 한다. 설정 로딩이 실패하면 일반 executor로 task를 처리하면서 정확한 gap을 보고한다.

## 참고

- [공식 Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [공식 Claude CLI](https://code.claude.com/docs/en/cli-reference)
- [PRD](../../PRD.md): 개인 프로젝트 목표; 새 역할 추가 자체를 포트폴리오 성과로 취급하지 않는다.
