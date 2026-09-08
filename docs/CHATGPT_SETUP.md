# 웹 ChatGPT 연결과 로컬 점검

2026-09-06 작성. 이 문서는 개발판의 실행 경로와 남아 있는 검증을 구분합니다.
OpenAI 내장 브라우저를 사용하는 것이 아니라, 이 서버의 별도 Chromium을 MCP로 조작합니다.
서버 안에서 LLM을 실행하거나 모델 API 키를 입력할 필요는 없습니다.

## 1. 먼저 로컬 엔진을 확인하기

Windows PowerShell에서 프로젝트 폴더로 이동한 뒤:

```powershell
uv sync --extra browser --extra dev --frozen
uv run cloud-browser self-test
```

자동 탐지가 안 되면 설치한 실행 파일을 정확하게 지정합니다.

```powershell
uv run cloud-browser self-test --chromium 'C:\Program Files\Google\Chrome\Application\chrome.exe'
```

Linux에서는 설치된 Chromium 경로를 사용합니다.

```sh
uv run cloud-browser self-test --chromium /usr/bin/chromium
```

이 명령은 임시 디렉터리·새 브라우저 프로필·테스트용 OAuth 저장소와 임의의 loopback 포트만
사용합니다. 운영 .env나 로그인 프로필은 재사용하지 않습니다. 빈 탭을 열고, 인증된 MCP로
스크린샷을 받아 1024×768 이미지를 디코딩한 뒤 세션·서버·임시 데이터를 정리합니다.
공개 인터넷 사이트를 방문하거나 외부 접속 설정을 바꾸지 않습니다.

성공 응답의 핵심:

```json
{
  "ok": true,
  "steps": ["mcp_initialized", "real_chromium_opened", "mcp_image_decoded", "session_closed"],
  "production_configuration_used": false,
  "chatgpt_vision_verified": false
}
```

`chatgpt_vision_verified: false`는 실패가 아닙니다. 이 로컬 시험은 웹 ChatGPT 모델이 실제로
이미지를 봤다는 증거를 만들지 않으므로 의도적으로 구분합니다.

```powershell
uv run cloud-browser doctor
```

이 명령은 브라우저 탐지, 의존성, 설정 유효성, OAuth·관리자 해시 설정 여부, 수동 콘솔
준비 상태를 보여줍니다. 비밀번호·토큰·해시 원문은 출력하지 않습니다. .env를 아직 만들지
않았다면 `configuration_valid: false`가 정상이며 self-test는 별도로 실행할 수 있습니다.
운영체제가 다른 경우 탐지된 Chrome과 `CB_CHROMIUM_PATH`가 다른지도 확인하세요.

## 2. 운영 서버를 준비하기

실제 서버 실행에는 [설치·접속](DEPLOYMENT.md)의 Docker·네트워크 격리·비공개 콘솔 절차를
따릅니다. 기존 .env는 덮어쓰지 마세요. 시작점은 .env.example이며 실제 origin과 관리자
암호 해시, OAuth callback을 사용자가 직접 채워야 합니다.

```sh
uv run cloud-browser hash-password
docker compose build
docker compose up -d
docker compose exec browser cloud-browser doctor
```

암호는 자신의 터미널의 숨김 입력에만 넣습니다. 채팅이나 MCP 인자로 보내지 않습니다.
이 작업에서 공개 터널 생성·Tailscale 설정·ChatGPT 설정·기존 서비스 재시작은 자동 수행하지
않습니다. 실제 공개 연결을 만들기 전에 DEPLOYMENT.md의 포트 분리와 인증 조건을 확인하세요.

개인 원격 제어 화면은 현재 noVNC 기반입니다. 해당 구성의 Xvfb/디스플레이·자산·WebSocket이
함께 실행되어야 합니다. Windows에서 headless self-test가 통과한 사실만으로 수동 로그인이나
Linux Docker 실행이 검증된 것은 아닙니다. manual control은 한 세션의 비-headless 화면에
대해서만 사용하세요. 생산 환경의 검증되지 않은 네트워크 차단을 끄는 것으로 연결 문제를
해결하지 않습니다.

## 3. 웹 ChatGPT에 등록하기

OpenAI 공식 안내:

- [연결과 테스트](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [개발자 모드](https://developers.openai.com/api/docs/guides/developer-mode)
- [MCP 도구 결과 형식](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

계정과 워크스페이스에 실제로 표시되는 사용자 지정 앱/MCP 등록 화면을 사용합니다.
요금제 이름만으로 모든 도구 권한을 보장하지 않습니다. 이 프로젝트의 배포 경로에서는
ChatGPT가 도달할 수 있는 HTTPS 주소의 `/mcp`를 지정합니다.

| 항목 | 값 |
|---|---|
| 서버 URL | 설정한 public origin + `/mcp` |
| 인증 | OAuth, 사전 등록한 공개 client ID, PKCE S256 |
| client ID | `CB_OAUTH_CLIENT_ID`와 동일한 값 |
| callback | 실제 ChatGPT 설정 화면의 정확한 HTTPS callback |
| client secret | 이 구현은 공개 클라이언트이므로 사용하지 않음 |
| 비공개 콘솔 | `CB_CONTROL_ORIGIN`, 별도 사용자 접속용 |

공개 upstream과 비공개 콘솔을 서로 바꿔 등록하지 마세요. 원본 CDP/VNC 포트는 등록하지
않습니다. UI가 다른 인증 방식만 제공한다면 현재 구현과의 호환을 확인해야 하며 OAuth를
끄거나 임의 토큰을 붙여 우회하지 않습니다.

## 4. 연결 후 최소 대화 시험

앱을 선택하고 다음과 같이 요청합니다.

> 이 Cloud Browser MCP의 browser_status를 호출해 지원 기능을 확인해줘.
> 그다음 example.com을 열고 제목과 본문을 읽은 뒤 visual 관찰도 해줘.
> 다른 검색 도구로 대체하지 말고 이 MCP의 결과만 사용해줘.

관찰·탭·이미지 응답이 정상이라면 다음 조작 시험을 합니다.

> W3C 아코디언 예제의 Personal Information 영역을 닫아줘.
> 클릭 뒤 다시 관찰해서 expanded가 false인지 확인해줘.

현재 승인 정책은 strict-per-action입니다. 평범한 클릭도 비공개 콘솔의 1회 승인이
필요합니다. ChatGPT가 반환받은 토큰을 단순히 다시 보내는 것만으로는 실행되지 않습니다.
콘솔 승인 후 원래 행동과 토큰을 재호출하고, browser_status로 pending/approved/denied를
확인할 수 있습니다. 거절한 토큰은 CONFIRMATION_DENIED입니다. ChatGPT 자체의 도구 확인과
서버 콘솔 승인은 서로 별개이며, 쓰기 도구를 읽기 전용이라고 표시해서 승인을 숨기지 않습니다.

이미지 **인식** 검증에는 DOM에 정답이 없는 시각 페이지가 필요합니다. `tests/fixtures/visual-probe.html`
같은 캔버스 시험 페이지를 자신이 관리하는 공개 HTTPS에 배치하고, ChatGPT가 screenshot만
보고 도형의 색과 상대 위치를 설명하는지 직접 대조하세요. 로컬 file URL을 허용하거나
이미지 생성 성공만으로 이 시험을 통과했다고 기록하지 않습니다.

## 5. 로그인·수동 조작

browser_auth_request 또는 browser_handoff는 즉시 제어 화면 안내를 반환합니다.
사용자는 비공개 콘솔에서 같은 원격 브라우저를 조작합니다. 제어 중에는 전체 세션의 자동
조작과 페이지 관찰이 중단됩니다.

- **Finish control**: 민감 화면을 닫은 뒤 자동화로 반환합니다. 로그인 성공을 확인할 수
  없는 사이트는 authenticated=null, verification=unverified입니다.
- **Extend private control access**: 제어 화면 접근 시간을 연장합니다. 자동화를 재개하지 않습니다.
- **Cancel and close**: 세션 전체를 닫습니다. 입력하다 만 인증 화면을 AI에 공개하지 않습니다.

제어 시간 만료와 자동화 잠금 해제는 다릅니다. 단순히 사용자 제어 창을 닫거나 시간을
초과했다고 자동화를 재개하지 않습니다. 상태는 browser_status로 확인합니다.

## 6. 선택 옵션: iframe을 가리고 화면 보기

기본값은 iframe 포함 페이지의 이미지 반환 차단입니다. 운영자가 제한된 부분 캡처를
선택하려면 .env에 다음을 지정하고 자신의 배포 절차로 서버를 다시 실행합니다.

```dotenv
CB_IFRAME_SCREENSHOT_POLICY=mask
```

viewport의 iframe/object/embed 영역을 여백 포함 검게 가린 PNG를 반환합니다. 마스킹 영역의
좌표 행동, 전체 페이지 캡처, 안전하게 경계를 계산할 수 없는 합성 효과는 차단합니다.
이 기능은 iframe 자동화나 임의 비밀값 탐지의 보장이 아니며, MCP가 정책을 변경할 수 없습니다.

## 완료 여부를 판단하는 기준

로컬 엔진 시험, HTTPS/OAuth 연결, ChatGPT 이미지 인식, 실제 수동 로그인, Linux/Pi 실행은
서로 다른 검증 항목입니다. 일부 통과를 전체 통과로 해석하지 않습니다. 상세 실행 기록과
남은 항목은 VALIDATION.md 및 IMPLEMENTATION_REPORT.md를 참고하세요.
