# 모바일 화면 외부 리뷰 후속 검증 — 2026-11-05

범위: 기존 승인된 오늘·복약확인·일정·기록·처방등록/확인·가이드 UI 변경. API/DTO/DB/경로정의/의료공개gate 변경 없음. 기존 외부리뷰 미완료 사유였던 Claude 인증을 macOS 인증 저장소 접근이 가능한 실행에서 확인했다. 사용자 요청에 따라 main Integrator가 tools/MCP/hooks 없는 CLI bridge로 선택 소스만 검토했다. 실제 OpenAI/OCR 호출 없음.

## 최초 외부 리뷰

4개 묶음, 보고서 총30개 지적. 보고서의 심각도는 코드 검증 전 의견이며, 모든 항목을 실제 결함으로 집계하지 않는다. 각 packet SHA256:

- today-checkin:23ddce796ab99e2343ade343751f1aa2d44b579c2ec31980acba5eefe62643de
- schedule-records:4cd646dc1921684cfa4b512d2e9a2b745d6abcd2c2838fcccf038060c12299d8
- prescription-review:c227d5ad87d03c164cd387771f9618faf22e24e2e0e800408d68cafcfbe6ecdb
- upload-guide:29429412a374d7a33938c4a51fbad58a787ce6e17e28ea204b347976573b3141

## 수정

1. Schedule의 새 기록 링크는 오늘/과거에만 표시. 미래 날짜 기록 링크로 선택 불가 날짜에 진입하지 않는다. 기존 경로와 기록 날짜 query 정책은 유지.
2. 단일 기록의 즉시 저장 안내는 예정 시각 도달 뒤에만 표시하고, 무조건 나중 수정 가능하다는 약속을 제거.
3. 그룹 완료 요약에서 TAKEN/NOT_TAKEN을 명시적으로 구분하고 그 외 상태는 확인 필요로 표시.
4. Home의 잘못된 날짜/시각 응답은 차단을 유지하고 네트워크 문제로 단정하던 안내만 일반 조회 실패 문구로 변경.
5. 처방 확인 필수/선택 라벨은 #52616b로 대비 개선.
6. Guide의 overflow-hidden 컨테이너 내부 토글/summary에는 실제 우선순위가 높은 -3px 포커스 테두리 적용. 일반 버튼은+3px 유지.
7. Guide 자세히 보기/접기 문구를 버튼의 접근성 이름에 포함하고 장식 화살표만 숨김.
8. Upload의 파일 선택 live region은 최초부터 유지. 파일명도 포함해 재선택 시 내용이 갱신됨. 기존 시각 안내 유지.
9. Upload의 중복 아이콘/그리드 스타일을 원래 선언으로 합치고 좁은 화면의 이전 padding34px를24px로 정렬.

## 문맥 누락·기존 동작·선택 제안

- HIGH로 분류됐던 Home 서비스 fixture와 단계형 Checkin 테스트 없음은 packet이 소스만 포함해 생긴 증거 부족이다. 실제 HomeTodaySection/MvpDesignPages/CheckinPage 테스트와 연결E2E는 이미 존재하며 이전 전체981/981, 관련브라우저18/21개 실행 기록이 .omx/plans의 각 UI task에 있다.
- /records는 src/routes/AppRouter.tsx에 이미 보호 경로로 등록됨. MobileShell disabledNavigation도 이미 구현됨. 해당 route/prop가 없다는 추정은 반증됨.
- records의 Back→/menu는 기존 명시 계약이며 MedicationRecordPage 단위와 medication-record E2E가 검증한다. navigate(-1)로 바꾸지 않는다.
- conflictReloadRef는 실제 conflict 후 GET실패가 이어질 때 다음 성공 GET에서 그 사실을 안내하려고 유지한다. 무조건 clear하면 최신 상태를 읽기 전 충돌 안내를 잃는다.
- Home 날짜/identity fail-closed,403분류,가장 이른 미확인 회차 우선과 로그인 홈 약명 표시는 기존 채택 방향. 새로운 제품/privacy 결정을 이 리뷰로 생성하지 않는다.
- 320px 날짜4+3 배열은 최소44px 터치 개선을 위한 의도. medication-record E2E가 320/390/412의44px·overflow·nav 가림을 검사한다.
- MvpPages.css의 Home/Upload 변경은 각각 후속 사용자 요청에 포함된다. Home TSX는 hero mascot을 제거했다. mvp-ai-job-page는 현재 PrescriptionUploadPage에서만 사용됨(rg로 확인).
- 처방 확인 문구 테스트4곳과 Guide 토글 문구 테스트가 이미 갱신됨. 원본 열기/닫기·긴약명·입력 오류·명시확정·가이드키보드·근거는 visible Playwright 합성 흐름으로 검증했다.
- record 달력 정규식의 약한 검증은 기존 이슈이며, 새 링크는 Schedule의 달력 검증을 거친 날짜만 사용한다. 별도 개선 후보로 기록.
- 유사 체크/기록 버튼의 추가 설명, Home 미확인 여러 회차 표시, 기존 CSS 중복과 과거 Figma 주석 추가 정리는 낮은 우선순위 개선 후보. 기존 워크플로/탭 변경은 하지 않음.

## 검증 근거와 경계

이후 전체 단위/E2E/타입/build/lint 결과는 .omx/plans/2026-11-05-external-ui-review/README.md에 최종 기록한다. 새로운 회귀는 미래기록링크 차단, 예정시간 전/후 저장안내, UNCONFIRMED 중립표시, persistent 파일선택 live region, 토글 접근성 이름. Guide 작업자는 단위47/47 및 실제 Tab 포커스 computed outline3px/-3px(토글/summary)·+3px(일반 버튼)을 확인했다.

외부 Claude는 테스트를 실행하지 않았으며 검토는 merge/release/의료·Source 공개 승인 아님. 실제 환자·Provider·스마트폰 카메라 검증 범위가 아니다. 테스트 데이터는 합성. 임시 packet/raw response는 검토 정리 후 삭제하고 hash·보고서·처리 결과를 남긴다.
