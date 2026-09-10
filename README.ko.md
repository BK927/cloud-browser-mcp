# ChatGPT용 셀프 호스팅 브라우저 MCP — 사용자 승인 기반 원격 Chromium

**한국어** | [English](README.md)

웹 ChatGPT가 **내 서버에서 실행되는 별도 Chromium**을 읽고 조작하는 단일 사용자용
MCP입니다. 서버에서 LLM을 실행하지 않으며 모델 API 키가 필요하지 않습니다.

**현재 소스: 외부 계약 0.4 초안으로 개선 중인 개발판.** 세션 호출에는 새로 반환하는
`lease_id`가 필요하므로 기존 클라이언트의 도구 스키마를 갱신해야 합니다.
[개선 진행 기록](docs/IMPROVEMENT_WORKLOG.md)과 [현재 계약](docs/CONTRACT.md)을 확인하세요.
이전 0.3 배포 기록과 [2026-09-09 개선판 Pi 실측](docs/PI_COMPARISON_2026-09-09.md)을
구분합니다. 실측 커밋은 `3a172a1`이며, 측정 후 기존 운영판으로 복원했습니다.

현재 17개 도구, 작업 독점 임대, balanced-v2 편집, 프레임 조작과 확장 입력·파일 기능을
구현했습니다. 패키지 버전은 아직 0.1.0입니다. Docker와 Debian 13 네이티브 설치가 같은
코드를 쓰며 화면/수동 제어 프로세스는 필요한 때만 실행합니다. 로컬·Linux CI 결과와
실제 Pi/웹 ChatGPT 결과를 구분합니다. Pi 3방식×3회 비교는 완료했으며 네이티브의 추가
절감은 유휴 약 11.5MiB, 단일 페이지 약 6.0MiB였습니다. 긴 전체 페이지 캡처는 세 방식
모두 1GiB 예산에서 거절됐습니다. Docker와 네이티브 설치 경로를 모두 유지합니다.
운영 투입 전 [통합 검증](docs/VALIDATION.md)을 완료해야 합니다. 공개 SaaS·다중 사용자·
앱 디렉터리 제출은 범위 밖입니다.

이 비교에 사용한 Raspberry Pi 4 Model B(2GB RAM)는 벤치마크·통합 시험 장비일 뿐이며,
권장 장비나 최소 요구사항이 아닙니다.

## 먼저 실행해 보기

```sh
uv sync --extra browser --extra dev --frozen
uv run cloud-browser self-test
uv run cloud-browser doctor
```

self-test는 새 임시 프로필·로컬 포트로 **인증된 MCP → 실제 Chromium → 이미지 응답**을
검증하고 종료합니다. 운영 .env·쿠키·계정·공개 접속 설정은 바꾸지 않습니다.
브라우저 자동 탐지가 안 되면 `self-test --chromium <실행파일>`을 사용하세요.

웹 ChatGPT 연결 순서는 [연결·점검 안내](docs/CHATGPT_SETUP.md), 변경 내용과 검증 범위는
[구현 기록](docs/IMPLEMENTATION_REPORT.md)에 있습니다.
원래 명세와의 차이 및 2026-09-08 보완 범위는 [명세 대응 현황](docs/SPEC_STATUS.md)에 정리했습니다.

## 라이선스

직접 작성한 코드·문서는 [MIT](LICENSE)입니다. 기본 엔진 DrissionPage에는 **별도
개인 학습·합법적 비영리 사용 조건**이 있습니다. MIT가 의존성까지 재라이선스하지
않습니다. [필수 의존성 고지](THIRD_PARTY_NOTICES.md)를 먼저 읽으세요.

## 구성

```text
웹 ChatGPT ─ HTTPS / Funnel :443 ─ MCP + OAuth :8000
                                           │
사용자 ─ tailnet / Serve :8443 ─ 비공개 콘솔 :8001
                                           │
                 순차 작업 프로세스 ─ DrissionPage ─ Chromium
                                                      │
                                         공개 목적지만 허용하는 프록시
```

AI 스크린샷은 MCP의 실제 `image` content로 전달됩니다. 비공개 콘솔은 로그인·일회
승인·같은 Chromium의 수동 제어용입니다. 원본 CDP·VNC 포트는 공개하지 않습니다.

Docker 구성은 browser·egress·ingress 3개 서비스입니다. ingress는 위 두 접속 포트만
고정 중계하고 browser는 internal 네트워크와 UID 방화벽 안에 유지합니다. Chromium
sandbox를 유지하기 위한 [최소 seccomp 정책](deploy/seccomp/README.md)을 동봉합니다.
Pi canary의 브라우저 시작·sandbox 확인은 전체 배포나 성능 검증 완료를 뜻하지 않습니다.

## 시작하기

배포 목표: Debian 13 arm64·amd64. Windows/macOS에서는 Linux 컨테이너를 실행하는
Docker Desktop 경로를 사용할 수 있지만, 해당 배포 자체는 실측·통합 시험하지
않았습니다. 현재 Windows의 별도 Chromium에서 실행을 확인했으며 이를 Linux/Pi
지원 인증으로 간주하지 않습니다.

1. [설치 가이드](docs/DEPLOYMENT.md)에 따라 Docker·Tailscale·`.env`를 준비합니다.
2. 관리자 암호를 Argon2id 해시로 설정하고 ChatGPT의 **정확한 OAuth callback**을
   등록합니다. 공개 client ID + PKCE 방식입니다.
3. 엔진 라이선스 확인 후 `docker compose up --build -d`로 시작합니다.
4. Funnel과 Serve를 서로 다른 HTTPS 포트에 연결합니다.
5. 웹 ChatGPT 개발자 모드에서 `/mcp`를 연결하고 최초 이미지 전달을 검증합니다.

Docker 없이 Debian 13 arm64/amd64에 설치하려면 [네이티브 설치](docs/NATIVE_INSTALL.md)를
사용하세요. 기존 Docker를 제거하거나 영구 전환하지 않습니다.
[비교 절차](docs/PERFORMANCE_COMPARISON.md)는 세 방식 모두 실제 1GiB 제한을 확인합니다.

서버 배포 가능 여부와 ChatGPT 계정에서 사용자 지정 MCP 기능을 사용할 수 있는지는
별개입니다. 이 저장소가 사용자를 대신해 공개 접속·ChatGPT 설정을 변경하지는 않습니다.

## 도구

| 도구 | 내용 |
|---|---|
| `browser_open` | 새 세션·탭, 기존 탭 재사용, 선택적 URL 이동 |
| `browser_list_tabs` | 탭 조회 |
| `browser_navigate` | URL·기록 이동, GET 문서 새로고침 |
| `browser_observe` | 본문 우선 렌더링 DOM, 완전한 JSON 노드, 이미지, 균형 페이지네이션 |
| `browser_act` | 클릭·입력·키·선택·체크·스크롤·좌표·승인된 준비 파일 입력 |
| `browser_auth_request` | 보호된 사용자 로그인 시작 |
| `browser_handoff` | 수동 제어 시작, 즉시 반환 |
| `browser_close` | 탭·세션 종료 |
| `browser_status` | 자원·탭·제어·인증 진행 조회 |
| `browser_configure` | 화면·이미지 품질·출력량·대기 시간 조절 |
| `browser_list_page_tools` | 현재 문서가 제공하는 네이티브 WebMCP 도구 목록 |
| `browser_call_page_tool` | 사용자 승인 후 페이지 제공 도구 1회 호출 |
| `browser_wait` | 제한 시간 내 URL·요소·대화상자·다운로드 조건 대기 |
| `browser_dialog` | 대화상자 조회·승인된 응답 |
| `browser_logs` | 비밀값 없는 제한된 실행 진단 메타데이터 |
| `browser_artifacts` | 작업별 다운로드·안전한 내보내기·정리 |
| `browser_clipboard` | 작업 전용 텍스트 버퍼, OS 클립보드와 분리 |

초기 화면은 1024×768, JPEG 품질 75입니다. 탭을 3개로 고정하지 않습니다. 호스트와
cgroup의 메모리 여유를 검사해 `RESOURCE_PRESSURE`를 반환하면 AI가 탭 재사용·정리·
캡처 축소를 선택합니다. 운영자 안전 예산은 AI가 바꾸지 못합니다.

## 안전 기본값

- 기본 `strict`는 조작마다 비공개 승인을 요구합니다. 운영자가 `CB_APPROVAL_POLICY=balanced`를
  선택하면 balanced-v2의 비민감 입력·선택·체크·편집·일반 링크/검색은 자동 실행됩니다.
  전송·구매·삭제·권한 변경·민감 입력·효과가 불명확한 실행은 확인을 유지합니다.
- 토큰 echo만으로는 승인되지 않습니다. 실제 사용자 승인·동일 문서·대상·행동·전송 데이터를
  확인하고 한 번 소비한 뒤 실행합니다.
- 로그인 중 세션 전체의 DOM·이미지·탭 제목 수집을 중단합니다. 제어 시간이 만료되어도
  자동화를 자동 재개하지 않습니다.
- 로그인 완료는 인증 성공 증거가 아닙니다. 운영자가 `CB_AUTH_RULES`에 지정한 정확한
  출처·성공/실패 표시로만 확인하며, 근거가 없으면 `authenticated: null`을 반환합니다.
- 알려진 민감 입력 화면은 이미지를 거부합니다. 기본 프레임 정책은 검사 가능한 일반
  프레임을 표시하고 민감·검사 불가 프레임만 안전한 위치에서 마스킹합니다.
  마스킹 영역의 좌표 행동은 거부합니다. 임의 비밀값을 완벽하게 탐지한다는 보장은 없습니다.
- 승인 화면은 현재 페이지와 선언된 목적지를 구분합니다. 실제 목적지를 추측하지 않습니다.
- 제어 만료 시에도 자동화는 잠겨 있습니다. 비공개 콘솔에서 연장하거나, 취소와 함께
  세션 전체를 닫을 수 있습니다.
- `RESULT_UNCERTAIN`을 재시도하지 않습니다. 수동 확인 후 다음 행동을 허용합니다.
- 사용자 로컬 Chrome 프로필·쿠키·방문 기록을 가져오지 않습니다.

[보안 경계](SECURITY.md)와 [외부 계약·제한](docs/CONTRACT.md)을 확인하세요.

## 개발과 테스트

Python 3.12 이상과 uv를 사용합니다.

```sh
uv sync --extra browser --extra dev --frozen
uv run pytest -q -m 'not browser'
uv run ruff check src tests scripts
CB_TEST_CHROMIUM=/usr/bin/chromium uv run pytest -q -m browser
```

Windows PowerShell:

```powershell
$env:CB_TEST_CHROMIUM='C:\Program Files\Google\Chrome\Application\chrome.exe'
uv run pytest -q
```

실제 테스트는 새 임시 Chromium 프로필만 사용합니다. 테스트 내부망 예외는 특정
fixture에 한정한 코드 패치이며 운영에서 차단을 해제하는 옵션이 아닙니다.

## 추가 기능 사용

- 파일은 비공개 콘솔에서 직접 준비합니다. `browser_status.staged_uploads`의 ID를
  `browser_act`의 `upload`에 넣고, 파일명·크기·해시가 표시된 별도 승인을 거칩니다.
  MCP로 임의 서버 경로·파일 URL·base64를 입력할 수 없습니다.
- WebMCP는 실제 Chromium 지원이 필요합니다. `CB_WEBMCP_TESTING=true`는 운영자가
  새 브라우저에 실험 플래그를 적용하는 선택 사항이며 기본은 false입니다. AI가 켤 수 없습니다.
  미지원 엔진은 명시적 오류를 반환합니다. 운영자가 정확한 출처·도구·스키마를 읽기용으로
  사전 허용한 경우를 제외하고 호출은 사용자 승인을 요구합니다.
- CDP로 접근 가능한 동일·교차 출처·중첩·구형 프레임의 본문과 요소 조작을 지원합니다.
  작업량을 넘거나 검사 불가인 프레임은 이유를 표시합니다. passkey/보안 키 전달은 미지원입니다.

구체적인 입력·인증 규칙·파일 보관 조건은 [확장 계약](docs/CONTRACT_EXTENSIONS.md)을 참고하세요.

추가된 순차 입력·modifier·드래그·다중 선택, 범위 관찰, 대기·대화상자·진단,
작업별 다운로드·내보내기·클립보드는 [확장 조작](docs/OPERATIONS.md)에 설명합니다.
총 17개 도구이며 작업 임대 계약에 맞춰 클라이언트의 도구 목록을 갱신해야 합니다.
