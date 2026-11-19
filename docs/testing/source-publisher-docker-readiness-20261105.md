# Source publisher Docker·credential 검증 — 2026-11-05

상태: **Docker 기본 3건·추가 권한 4건 PASS / 실제 image 빌드 PASS / helper fixture 수정 후 전체 publisher 통합 probe PASS (2026-11-18 재검증 완료)**. 합성 내부 작업이며 운영 DB·실제 Source·배포·공개·Current 승인이 아니다. [B1 기존 증거](source-publisher-b1-20261105.md)의 1,516 PASS와 이번 결과를 합산하지 않는다.

## 확인된 결과와 남은 공백

| 검사 | 최신 결과 | 증거와 한계 |
|---|---|---|
| 기본 Docker 3건 | **3 PASS / 실패·skip 0** | 전용 새 PostgreSQL에서 bootstrap/provision/redeploy, Catalog approval bootstrap, Source management bootstrap 실행. |
| 추가 Publisher/Writer 권한 4건 | **4 PASS / 실패·skip 0** | 전체 graph·stale grant·반복 policy, role membership/owner/default 권한, T1/T2 rollback, 별도 credential·미래 table 차단 검증. |
| 실제 Worker image | **빌드 PASS** | 기존 Dockerfile·frozen lockfile·ai/worker 그룹 그대로 사용. 빌드 성공은 전체 실행 성공을 뜻하지 않는다. |
| 실제 publisher 시작·음성 검사 | **일부 확인 / 초기 FAIL** | UID/GID 10001에서 import, 혼입 credential의 ingestion 진입 전 거부, RO/RW mount 검사를 통과했다. 이후 시험 helper의 잘못된 UID 0 단정에서 중단했다. |
| helper fixture 수정 후 전체 probe | **PASS (2026-11-18 재검증 완료)** | 호스트 여유 공간 8.81GiB(`>= 8GiB`) 확보 후 `python3 scripts/ci/source_publisher_container_check.py` 실행(exit 0). Docker volume의 helper UID 501·mode 755와 caller UID 10001 분리, helper 불변성, child `env={}` 실행, 대역 reader readback 완료(`synthetic config and mount checks passed`), 별도 CLI(`mfds_product_ingestion`) 음성 검사(sentinel 미노출·일반 실패 반환) 및 작업 소유 container·image·volume 정리를 모두 확인했다. |
| 단위·설정 계약 | **139 PASS / 실패·오류·skip 0** | 최신 helper fixture 수정을 포함한 최종 JUnit 기준. Docker 결과와 분리 집계한다. |
| 정적 검사 | **PASS** | Ruff·format 7개 Python 파일, mypy 실행기 2개 파일. inventory·금지 DB logic·protected table write 결과는 로컬 증거에 보존한다. |
| 임시 메모리 설정 복원 | **완료** | 승인받아 4096MiB로 시험 후 원래 **8092MiB**로 복원. UI Apply 비활성·Engine running 및 API MemTotal 8,218,316,800 bytes 확인. CPU 8·swap 1024MiB·disk limit·Resource Saver/300초 유지. |
| 공식 공간 회수 | **승인된 1회 실행 exit 0 / 공간 확보 효과 없음** | 아래 자원 복구 기록 참조. 회수 직후 약 6.48GiB, 복원 확인 시 약 6.41GiB로 probe의 8GiB 조건에 미달했다. |

별도 owner/reader는 시험 대역이다. 실제 owner IPC·vault PUT/verified READ·긍정 전체 발행·운영 준비도는 이번 결과로 확정하지 않는다. credential 음성 검사는 혼입 거부를 입증하며, 전체 운영 credential 격리 완료를 의미하지 않는다.

## 코드·설정 변경

- `scripts/ci/source_publisher_docker_check.py`와 test-only Compose는 UUID project·tmpfs·합성 credential·동적 loopback port를 사용한다. local endpoint/daemon과 host/container marker를 확인하고 기본 3 node를 직렬 실행하며 skip 0을 요구한다. `--publisher-role-checks`는 별도 4건을 추가한다. 소유권을 확인해 정리하고 private 자원 manifest를 보존한다. 512MiB host 여유 조건은 VM 쓰기 가능성을 보장하지 않는다.
- `tests/integration/rag/test_source_management.py`는 SQL이 요구하는 합성 `SOURCE_PUBLISHER_USER`와 빈 값/충돌 거부 검사를 보완한다. bootstrap SQL과 권한 의미는 변경하지 않았다.
- `tests/integration/rag/test_database_role_provisioning.py`는 Alembic 자식의 cwd를 빈 임시 폴더로 제한한다. 과거 398 schema에서 최신 ORM을 사용하는 시험 구간에만 nullable 924 필드 3개를 임시 제공하고 실제 migration 전에 제거한다. 적용 migration과 실제 schema 계약은 변경하지 않았다. 최신 Docker 기본 3건에 이 fixture 수정의 통과 결과가 포함된다.
- `scripts/ci/source_publisher_container_check.py`는 실제 Worker Dockerfile을 제한된 context에서 빌드한다. `.env`·symlink·model/cache를 제외하고, 새 anonymous Docker config와 설치된 Docker bundle의 Buildx를 사용한다. 기존 login은 변경하지 않으며 legacy builder fallback은 금지한다. **8GiB host 여유 조건을 유지한다.**
- 실제 image에서 UV가 `/.cache/uv`에 쓰려다 실패한 문제는 publisher의 기존 쓰기 허용 경로 `/publisher-spool/uv-cache`를 `UV_CACHE_DIR`로 지정해 해결했다. production Compose와 probe, 기존 배포 계약 test만 정렬했으며 운영 배포는 하지 않았다.
- macOS bind mount의 소유권 문제는 시험 helper만 작업 소유 Docker volume에 복사하고 runtime에서 read-only로 연결하도록 수정했다. 초기화 container는 생성만 하고 실행하지 않는다. root process·setuid·production guard 완화 없이 UID 501 helper와 UID 10001 caller를 구분한다. label로 확인한 초기화/runtime container·image·새 volume만 정리한다.

## 실패·복구 이력

처음에는 daemon 조회 timeout과 host 약 174MiB 여유, Docker metadata/blob I/O 오류로 검증을 중단했다. Owner가 운영 작업 없음과 중단 수용·재시작을 허용했다. 공식 restart가 stop timeout으로 실패한 뒤 Docker.app PID·실행 경로·시작 시각을 재확인해 종료하고 앱을 다시 열었다. 초기화와 disk image limit 변경은 하지 않았다.

초기 단위 시험 10건, 후속 2건의 오류는 공유 임시 venv의 pytest plugin/distribution metadata 문제였다. 기존 환경은 보존하고 별도 작업 소유 venv에 frozen 설치한 뒤 최종 139건을 통과했다. plugin autoload를 끄고 `pytest_asyncio.plugin`을 명시하며, 빈 cwd·환경 허용 목록·합성 credential을 사용했다. 이전 실패를 PASS로 바꾸거나 중복 실행을 합산하지 않는다.

초기 image 빌드는 기존 Docker Hub token 만료, isolated config의 Buildx 미발견과 legacy builder의 read-only filesystem 문제로 실패했다. 이후 anonymous config·bundled Buildx로 실제 빌드를 완료했다. 첫 runtime 실패는 UV cache 권한, 다음 실패는 helper fixture의 잘못된 UID 0 단정이었다. 최신 fixture 수정의 단위 검사와 소규모 실제 volume 소유권 진단은 통과했으나, 전체 image probe는 재실행하지 못했다.

초기 legacy build의 종료 container `5574592409b5a8ef88014bf59f78e786bd0f02b9af9a57a33428514854ec640b`는 처음 metadata I/O 오류로 삭제하지 못했다. 복구 후 exact ID·명령·exited 상태를 다시 확인해 삭제했다. 후속 전용 DB의 container/network, image probe의 이름·label로 추적한 초기화/runtime container·image·volume도 정리했다. 이 결과를 전체 Docker cache 정리로 확대하지 않는다.

자체 build 기록·시간·label로 확인한 reclaimable cache 22개를 정확한 ID 필터로 정리해 Docker가 1.729GB 회수를 보고했다. 설치 버전에서 일치하지 않은 boolean 필터의 0B 결과도 보존했다. 다른 프로젝트 cache·기존 image/volume의 broad prune과 factory reset은 실행하지 않았다.

[Docker 공식 공간 회수 절차](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/macfaqs/)의 VM 전체 관리 권한 때문에 첫 시도는 자동 승인 검토에서 거절되어 실행되지 않았다. 이후 Owner가 **공식 도구 1회**를 명시 승인했다. `docker/desktop-reclaim-space`를 `--privileged --pid=host --network none`으로 한 번 실행해 exit 0을 확인했고, `--rm` container가 남지 않음을 확인했다. 플랫폼 차이 경고가 있었으며 공간은 6634.1MiB에서 6633.4MiB로, 유의미하게 늘지 않았다. 도구를 재실행하지 않았다. 내려받은 공식 helper image는 보존했고 기존 image/volume은 삭제하지 않았다.

## 자원 조건과 재개 지점

Owner 승인에 따라 Docker Memory Limit을 임시 4096MiB로 낮춰 시험했고, 시험 종료 후 정확히 8092MiB로 복원했다. CPU·swap·disk limit·Resource Saver는 바꾸지 않았다. `memory-revalidation.json`과 `memory-restored-runtime.json`에 복원 근거를 보존한다.

영상 조사에서 다른 프로젝트 영상 약 10.6GiB와 동일 사본 logical 상한 약 1.8GiB를 확인했으나 참조·검수 보호 경계를 유지했다. 화면 녹화·팀 시연을 포함해 **영상 삭제 0건**이다. 기존 사용자 cache·공유 venv도 보존했고 작업 소유 uv 다운로드 cache만 제거했다. 여유 공간 변동의 원인을 모두 Docker로 단정하지 않는다.

2026-11-18에 호스트 여유 공간 8.81GiB(`>= 8GiB`)를 확인한 뒤 수정된 실제 image probe(`scripts/ci/source_publisher_container_check.py`)를 끝까지 실행해 helper command 불변성·child `env={}`·대역 readback·별도 CLI 음성 검사와 작업 소유 container/image/volume 정리 완료(`exit 0`)를 확인했다. 용량 조건을 낮추거나 기존 데이터를 임의로 삭제하지 않았다. 현재 상태는 **합성 Docker·컨테이너 격리 probe 전체 통과**다.

## Finalizer와 승인 경계

Owner는 [저장·읽기 우선 방향](../governance/decisions/2026-11-05-source-artifact-owner-read-direction.md)에 이어 [프로토콜 v1 동결 결정](../governance/decisions/2026-11-12-source-artifact-owner-protocol-v1.md)(`PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`)을 승인해 [Finalizer owner 경계 계약](../contracts/proposed/post-mvp-1/source-artifact-finalizer-owner-boundary-v1.md)의 합성 내부 UDS `PUT`·verified `READ`·`OUTCOME`과 임시 rematerialization input spool을 구현·검증([검증 근거](source-artifact-owner-v1-20261112.md))했다. 새 vault의 business DELETE는 닫고 기존 로컬 cleanup·B1 전체 caller transaction·Candidate 재시도·안전·공개 차단은 유지한다.

내부 독립 검토에서 harness의 false-PASS·cleanup·비밀 혼입과 proposal의 pathname·삭제 경합·generation/응답 불명 문제를 보완했다. 검토는 Human Owner·의료·Privacy·운영·공개 승인을 대신하지 않는다. 운영 DB·실제 Source·외부 Provider 호출은 하지 않았다. API·DTO·schema·적용 migration·공개 flag는 변경하지 않았다.

로컬 증거: `.omx/plans/evidence/publisher-docker-execution-20261105/`. 기본/추가 DB 검사는 `docker-three-recovered/`의 7개 JUnit, 최신 단위 검사는 `final-139-owner-unit.xml`, 초기 실제 image의 부분 실패는 `publisher-container-final4096/`, 2026-11-18 전체 통과 재검증 로그는 `publisher-container-revalidated-20261118/`, 공식 회수는 `docker-official-space-reclaim-approved.json`에 기록한다. private 합성 로그를 사용하며 실제 비밀값·환자 데이터·Source 원문은 포함하지 않는다.
