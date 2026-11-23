# 챗봇 UI·필요 범위 검수 — 2026-11-05

현재 승인된 처방 기반 Chat의 기능을 유지한 UI 변경이다. 일반의약품 독립세션/LangGraph/API/DTO/DB/Provider/공개gate 변경은 없다. 기존 다른 작업은 보존했다.

## 화면과 흐름

본문18px·보조16px, 같은 파란색/밝은 바탕·둥근 말풍선, 넓은 질문 예시·전송 버튼을 적용했다. 입력 라벨은 접근성 이름과 같은 '복약 질문', 보이는 전송 문구는 '질문 전송' 접근성 이름에 포함된다. 두 줄 질문을 읽을 수 있도록 rows2·전체 테두리·16px곡률 입력창을 사용한다. 질문 예시는 draft만 채우며 실제 전송은 명시적 버튼/기존Enter 처리다. 기존 IME·Shift+Enter·세션 조회/생성·중복/재시도·동의·오류·답변 markdown·피드백 기능은 유지한다.

## 검수 비용 절감

scripts/claude_review.py는 기본적으로 변경 hunk와앞뒤6줄만 보낸다. 전체 final_source 중복 전송을 제거했다. 필요한 호출부/테스트 문맥은 --context-range FILE:START:END(120줄상한), 재검수는 --review-range로필요hunk만 지정한다. hunk의6줄문맥은유지하므로범위는텍스트절단이아닌hunk선택기준이다. 생략부분은검수완료로표시하지않는다. 정책3문서는안전경계로유지한다.

같은baseline·파일·범위·문맥·정책·내용hash의완료report는 status=reused,claude_called=false로재사용한다. 실패·dryrun·stale은완료cache가아니다. 범위/내용이바뀌면새범위만검수한다. 경로/민감파일/크기/symlink/권한/도구차단/snapshot검사는유지했다.

이번실제Claude호출1회:ChatTSX/CSS+연결E2E의hunk packet42,171bytes, SHA2569a285ff11aaf722ae3eb7139ed6ecebe16ae6d7a9d1928f45a260edef85ba9ed. 동일요청의두번째실행은같은report를반환하고Claude호출0을확인했다. 바이트는실측이며토큰수측정은아니다. native범위/보안review추가지적없음.

Claude보고서6개는HIGH/BLOCKING없음. 실제수리:요청대기를expect.poll로,Shift+Enter값명시검사,실제휠스크롤로자동smoothscroll경쟁회피,키보드답변영역96px이상검사,활성nav글자12px,지역팔레트변수재사용/중복border제거. 전송aria-label과grid는현재원본/타입/E2E로확인했다. 페이지지역색상은요청된UI범위이며전역토큰을변경하지않았다. 수리후외부전체재검수는반복하지않고내부검사와실행으로검증했다.

## 검증

- Frontend전체984/984 PASS. 최종Chat59/59 PASS. baseline도59/59.
- 최종관련Playwright19/19 PASS:Guide→동일처방상담→질문draft/줄바꿈→명시전송→재전송가능한503복구→긴답변→기존재진입/오류/피드백/일정CTA/스크롤.
- Visible playwright-skill:320/390/412,844/480높이,safe-area34. 입력/전송nav겹침없음,문서/답변가로넘침없음,키보드높이답변96px이상,14문단스크롤,동일세션1회생성·질문중복표시없음. 합성응답만사용했고외부요청0/pageerror0/OpenAI호출0.
- Reviewhelper:기존18→24pytest PASS(환경:/opt/homebrew/Caskroom/miniforge/base/bin/python),같은환경Ruff·구문검사PASS. 실제bridge실행은/opt/homebrew/bin/python3(3.14),호스트기본3.9는프로젝트지원보다낮아사용하지않았다.
- tsc -b,대상oxlint,Vite build,TrackC gate,diffcheck PASS. 기존JS500kB초과chunk경고남음.
- 초기테스트의DOM이전safe-area설정/잘못된500재전송가정/자동스크롤경쟁은대역과테스트구동을보정했다. 제품에새재전송정책을넣지않았다.

증거:.omx/plans/2026-11-05-chat-design의unit-final.log,unit-chat-final.log,e2e-final.log,playwright-results.json,claude-review.json,claude-reuse.json와PNG. 실제Backend/의료AI/실사용스마트폰키보드의검증이나배포·의료승인은아니다. merge/commit/deploy없음.
