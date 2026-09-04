# 설치·접속

이 안내는 개인 서버 한 대 기준입니다. Docker/arm64/실제 ChatGPT 연결은 검증 관문이
남아 있습니다. 실패 시 원인 확인 없이 sandbox·인증·내부망 차단을 끄지 마세요.

## 1. 준비

- Debian 13 64비트 arm64 또는 amd64, Docker Engine + Compose v2.
- Raspberry Pi 4B 2GB는 목표 장비이며 성능 보증치가 아닙니다.
- Windows/macOS는 Docker Desktop의 Linux 컨테이너를 사용합니다.
- 서버와 사용자 기기에 Tailscale 설치, 같은 tailnet 로그인.
- HTTPS Serve/Funnel 사용 권한과 사용 가능한 DNS 이름.
- 웹 ChatGPT 계정에서 사용자 지정 MCP/개발자 모드 사용 가능 여부 확인.

코드를 받은 디렉터리에서:

```sh
cp .env.example .env
uv sync --extra dev --frozen
uv run cloud-browser hash-password
```

암호 입력은 터미널의 숨김 입력으로 처리됩니다. 출력된 **해시만** `.env`에 복사합니다.
Argon2 해시의 `$`가 변형되지 않도록 `.env` 예시처럼 작은따옴표로 감싸세요.
`.env`에는 실제 public/control origin, 정확한 callback, client ID를 지정합니다.
`CB_ADMIN_PASSWORD_HASH`의 예시 문구 그대로 배포하면 로그인할 수 없습니다.

Python 설치를 원하지 않으면 이미지를 먼저 빌드하고 임시 컨테이너의 CLI만 실행할 수
있습니다. 이 명령은 브라우저나 공개 리스너를 시작하지 않습니다.

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint cloud-browser browser hash-password
```

## 2. 로컬 upstream 시작

```sh
docker compose up --build -d
docker compose ps
docker compose exec browser cloud-browser doctor
```

Docker는 현재 CPU의 네이티브 이미지를 빌드합니다. Chromium·Xvfb·noVNC는 Debian
Trixie 패키지를 사용합니다. 두 아키텍처의 **빌드만** 확인하는 명령은 다음과 같습니다.
실행 검증을 대체하지 않습니다.

```sh
docker buildx build --platform linux/arm64,linux/amd64 --target browser .
```

공개 MCP upstream은 `127.0.0.1:8000`, private control upstream은
`127.0.0.1:8001`입니다. 5900/6080/CDP 포트는 host에 publish하지 않습니다.
프로필은 `browser_data` 볼륨에 남습니다. 종료해도 프로필을 자동 삭제하지 않습니다.
새 세션 ID로 새 프로필을 만들며 이전 로그인 상태를 새 세션으로 몰래 옮기지 않습니다.
장기 운영 시 오래된 프로필 보관·디스크 사용량 정리는 운영자가 결정해야 합니다.

## 3. 공개·비공개 리스너 분리

먼저 기존 Tailscale 설정을 확인하고 충돌하는 포트를 피하세요. 아래 명령은 **사용자가
실행하면 외부 접속 범위가 변경**됩니다. 이 프로젝트가 자동 실행하지 않습니다.

```sh
tailscale serve status
tailscale funnel status
tailscale funnel --bg --https=443 http://127.0.0.1:8000
tailscale serve --bg --https=8443 http://127.0.0.1:8001
```

`.env` origin과 출력된 HTTPS 주소가 정확히 일치해야 합니다. 포트를 바꿨다면 둘 다
수정하세요. 같은 HTTPS 포트에서 Serve private와 Funnel public을 경로만으로 나누지
않습니다. 개인 제어용 **8443에 Funnel을 켜지 마세요**.

공식 문서: [Funnel](https://tailscale.com/docs/features/tailscale-funnel),
[Serve](https://tailscale.com/docs/features/tailscale-serve),
[Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve).

## 4. 웹 ChatGPT 등록

[공식 연결 안내](https://developers.openai.com/plugins/deploy/connect-chatgpt)와
[OAuth 안내](https://developers.openai.com/plugins/build/auth)를 기준으로 등록합니다.
계정·워크스페이스별 지원과 UI는 달라질 수 있습니다.

- MCP URL: `https://서버이름.tailnet.ts.net/mcp`
- 인증: OAuth. 사전 등록한 `CB_OAUTH_CLIENT_ID` 사용, 공개 클라이언트 PKCE(S256).
- callback: 설정 화면에서 제공한 URL을 `.env`의 목록에 **정확하게** 등록합니다.
- RFC 9207용 알려진 callback 예시는
  `https://chatgpt.com/connector_platform_oauth_redirect`이며 실제 UI 값을 우선합니다.
- 클라이언트 secret은 없습니다. 서버는 `token_endpoint_auth_methods_supported: ["none"]`을
  광고합니다. UI가 다른 방식만 제공하면 연결 지원을 검증해야 하며 임의 workaround로
  공개 인증을 끄지 않습니다.
- discovery: `/.well-known/oauth-protected-resource/mcp`,
  `/.well-known/oauth-authorization-server`.
- DCR/CIMD 자동 등록은 구현하지 않았습니다.

최초 연결 시 공개 OAuth 페이지에서 **서버 관리자 암호**로 승인합니다. 웹사이트 암호를
이 페이지에 입력하지 마세요. 웹사이트 로그인은 tailnet 전용 콘솔의 원격 화면에서만 합니다.

도구 10개를 확인한 다음 [검증 절차](VALIDATION.md)의 이미지 인식 시험을 수행합니다.
OAuth HTTP 테스트 성공만으로 실제 ChatGPT 호환을 완료 처리하지 않습니다.

## 5. 수동 로그인·승인

휴대폰/PC에서 Tailscale을 켜고 control origin(:8443)을 열어 콘솔에 로그인합니다.
AI가 요청한 행동의 대상·입력값을 확인한 뒤 한 번 승인합니다. 승인은 행동을 즉시
실행하지 않습니다. 이후 AI가 동일한 토큰·행동을 재호출해야 실행됩니다.

로그인/handoff가 활성화되면 콘솔의 원격 브라우저 링크를 엽니다. 제어를 마치면
민감 창을 닫고 ‘Finish control’ 버튼을 누릅니다. 단순히 원격 화면 탭을 닫는 것으로는
자동화가 재개되지 않습니다. 만료되어도 잠금은 유지됩니다.

WebAuthn passkey·보안 키 전달은 지원하지 않습니다. 가능한 다른 인증 수단이 없다면
중단해야 합니다. CAPTCHA 우회 기능은 없습니다.

## 자원·운영

예시 1400MB browser/128MB proxy/256MB shared memory는 시작 제안값일 뿐 Pi 실측치가
아닙니다. 물리 RAM, 다른 서비스, cgroup headroom을 함께 확인하세요. `/dev/shm`도
메모리 예산에 포함됩니다. 장시간 swap 의존은 권장하지 않습니다.

AI 변경 가능: viewport, JPEG quality, max_chars, wait_ms.
운영자만 변경: 메모리 reserve/admission 예산, 최대 캡처 픽셀, 세션 수·TTL, 인증·포트.
수동 화면은 하나이므로 `CB_MAX_SESSIONS=1`일 때만 handoff를 제공합니다.

API/브라우저 재시작 후 기존 session ID는 만료 오류를 반환합니다. 새 대화를 시작할
필요는 없지만 새 세션은 명시적으로 열어야 합니다. 세션 만료는 다음 도구 호출에서
정리됩니다. 유휴 만료가 즉시 자원을 회수하는 background scheduler는 아직 없습니다.

토큰 전체 취소:

```sh
docker compose exec browser cloud-browser revoke-all
```

컨테이너를 멈추되 로그인 프로필 볼륨은 유지:

```sh
docker compose down
```

`down -v`는 쿠키·프로필·DB를 삭제하므로 이 안내에서 자동 실행하지 않습니다.
