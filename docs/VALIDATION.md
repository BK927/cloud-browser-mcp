# 검증 기록과 출시 관문

작성 기준: 2026-09-05. **아래 로컬 통과를 Pi/Docker/웹 ChatGPT 통합 완료로 해석하지
않습니다.** 실제 계정·Funnel 주소·목표 장비 접근 없이 해당 결과를 만들 수 없습니다.

## 확인한 환경

- Windows x86_64, uv 관리 Python 3.13.11, Google Chrome 152.0.7977.82의 별도 임시 프로필.
- 공식 MCP Python SDK 2.x, Streamable HTTP, FastAPI.
- fake worker 계약/보안 테스트와 실제 DrissionPage Chromium 테스트를 분리.
- 사용자 로컬 프로필·쿠키는 가져오지 않음. Tailscale 공개 설정은 실행하지 않음.
- 이 기록의 로컬 검증 이후 사용자의 요청으로 GitHub에 프로토타입 소스를 공개합니다.
  소스 공개는 Docker/Pi/웹 ChatGPT 통합 관문을 통과했다는 의미가 아닙니다.

최종 로컬 실행: **47 passed**, 의존성 Starlette/AnyIO의 deprecation warning 1건.
공개 W3C live test와 HTTP → 별도 worker → 실제 Chromium → JPEG 전송 시험을 포함합니다.
정적 검사 및 Python 패키지 빌드도 별도로 수행합니다. Docker daemon은 이 개발 장비에
없으므로 Compose YAML/포트 분리·쉘 구문 검사만 가능했고 컨테이너 실행은 하지 않았습니다.

## 로컬 자동 검증 범위

| 항목 | 검증 |
|---|---|
| OAuth | 정확한 callback, PKCE, 발급자, token 재사용 거부, refresh 회전, grant 취소, login throttle |
| 포트/인증 경계 | public에 private 경로 없음, MCP bearer 필수, console cookie/Origin/CSRF |
| MCP transport | 실제 HTTP + 공식 클라이언트 initialize/tools/list/call, 도구 10개, structuredContent/image |
| 브라우저 | 별도 worker 프로세스, 호출 사이 탭 유지, 실제 JPEG 반환 |
| 상호작용 | 아코디언 aria-expanded, fill/select/check/scroll, 팝업·닫힌 탭 |
| 오래된 관찰 | 격리된 observer의 DOM 변경 감지, 동일 HTML DOM 교체, viewport 후 node/screenshot 거부, cursor 결합 |
| 승인 | 승인 전 0회 실행, token echo 미승인, 변경 거부, 동시에 같은 token 호출 시 1회만 실행 |
| 결과 불명 | 소비한 token 재시도 금지, 세션 잠금 |
| 사용자 제어 | 세션 전체 관찰/조작 차단, 종료 후 revision, 인증 성공 미추측, 수동으로 닫힌 탭 |
| 자원 | fake 메모리 압박 시 신규 탭 거부, 기존 탭 보존, 고정 3탭 제한 없음 |
| 네트워크 정책 | URL/프록시의 사설·metadata·IPv6 변환 주소 거부, DNS 확인 주소로 연결 |
| 추가 회귀 | 스크롤 후 실제 viewport 이미지, 긴 캡처 예산 거부, 가려진 요소 미클릭, DOM 우선, 취소된 RPC 세션 무효화 |
| 공개 W3C 페이지 | 실제 Personal Information 버튼 aria-expanded true → false, opt-in live test 통과 |

실행 명령:

```sh
uv sync --frozen --extra browser --extra dev
uv run pytest -q -m 'not browser'
CB_TEST_CHROMIUM=/usr/bin/chromium uv run pytest -q -m browser
CB_TEST_CHROMIUM=/usr/bin/chromium CB_TEST_LIVE=true uv run pytest -q tests/test_w3c.py
uv run ruff check src tests scripts
```

W3C는 [원본 아코디언 예제](https://www.w3.org/WAI/ARIA/apg/patterns/accordion/examples/accordion/)를
읽고 UI만 조작합니다. 네트워크 접근이 없는 일반 테스트에서는 live 항목을 건너뜁니다.

## 필수 미완료 관문 A — 실제 웹 ChatGPT

1. 운영 HTTPS origin과 정확한 callback으로 OAuth 연결. 도구 10개 확인.
2. 운영자가 `tests/fixtures/visual-probe.html`을 본인 소유의 **별도 공개 시험 origin**에
   올립니다. MCP public listener에는 새 정적 콘텐츠 경로를 추가하지 않습니다.
3. ChatGPT에 그 URL을 열고 semantic/interactive만 관찰하게 합니다. 무작위 숫자가
   관찰 텍스트에 없어야 합니다. 스크립트를 읽거나 값을 추출하는 도구는 없습니다.
4. visual 관찰 후 **이미지에만 그려진 숫자·도형·색**을 설명하도록 합니다.
5. 사용자 private 원격 화면에서 같은 숫자를 직접 비교합니다. 페이지를 새로고침하면
   숫자가 바뀌므로 동일 탭/동일 화면을 비교하세요.
6. ChatGPT 연결 성공, image 수신, 시각 내용 인식 세 조건을 각각 기록합니다.

로컬에서 image content를 생성/해독한 사실만으로 4번을 통과 처리하지 않습니다.

## 필수 미완료 관문 B — Docker·네트워크·수동 화면

- Debian 13 amd64/arm64에서 build 및 실제 기동.
- Chromium sandbox 켜진 상태로 실행. namespace 오류를 `--no-sandbox`로 우회하지 않음.
- browser UID에서 proxy 이외 직접 외부 연결, localhost/CDP, private IP, metadata가
  모두 거부되는지 운영자 진단으로 확인. proxy를 끄면 사이트 접근도 실패해야 함.
- tailnet에 없는 기기에서 public MCP/OAuth만 접근되고 :8443/private는 접근 불가.
- tailnet 기기에서도 login+활성 handoff 없이는 WebSocket 원격 제어 불가.
- 로그인 중 MCP DOM/이미지/탭 제목 수집이 차단되고 noVNC와 동일 탭이 유지되는지 확인.
- 종료 버튼을 누르면 이미 열린 WebSocket도 닫히는지 확인.
- 비밀번호/OTP/카드/iframe 화면 screenshot이 차단되고 로그에 입력이 없는지 확인.
- 실제 브라우저 강제 종료·서버 재시작 후 기존 ID가 만료되는지 확인.

## 필수 미완료 관문 C — Raspberry Pi 4B 2GB

초기 예산값은 실측 전 제안일 뿐입니다. 다른 세션을 종료한 후 컨테이너 안에서
운영자 전용 벤치마크를 실행합니다. 스크립트는 짧은 로컬 진단 grant를 만들고 종료 시
취소하며, 생성한 세션만 닫습니다. 토큰·DOM 원문은 출력하지 않습니다.

```sh
docker compose exec -T browser python scripts/benchmark.py --rounds 3 --extra-tabs 4
docker stats --no-stream
```

공개된 긴 시험 페이지를 `--url`로 지정하면 viewport/full-page 캡처도 측정합니다.
출력의 platform/machine, 각 호출 지연·상태·이미지 크기, 호출 전후 메모리를 기록하세요.
별도로 peak RSS/cgroup peak, swap, OOM 이벤트, CPU throttling, 온도, 설치 소요시간,
최대 안정 탭 수, 긴 캡처 거부, worker/browser 종료 동작을 측정해야 합니다.

자원을 고갈시키는 stress 작업은 기본 스크립트가 자동 실행하지 않습니다. 별도 테스트
컨테이너의 메모리 예산을 낮춰 `RESOURCE_PRESSURE`를 확인하고, 기존 로그인 작업을
수행 중인 서버를 고의로 종료하지 마세요.

이 관문을 마친 뒤 장비별 결과·제한을 본 문서에 추가해야 정식 지원 범위를 확정할 수
있습니다. 현재 Docker/하드웨어 실행 결과나 ChatGPT 이미지 인식 결과는 없습니다.
