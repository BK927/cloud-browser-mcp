# 구현·검증 기록 — 2026-09-06

대상: Personal Cloud Browser MCP, 기존 checkout `4e119f4`에서 시작한 미커밋 개발 변경.
외부 계약 표기는 `0.2-draft`; 패키지/서버 배포 버전은 기존 0.1.0이며 릴리스 발행은 하지 않았습니다.

## 이번에 실제로 구현한 내용

| 영역 | 변경 |
|---|---|
| 본문 관찰 | 렌더링 DOM에서 main/article 우선, 문서 순서·제목·목록·표의 간단한 구조 유지, AX/DOM 중복 제거 |
| 조작 목록 | 빈 기본값 제거, aria-disabled·native summary 지원, viewport 기준 후보 제한 |
| 분할 읽기 | 본문·노드의 예산 분리, 완전한 JSON Lines, 고정 snapshot/cursor, 작은 예산에서 node_id 보존 |
| 승인 | 현재 페이지와 선언된 링크/폼 목적지 분리, 모르는 목적지는 null, 거절 토큰의 명시적 오류 |
| 승인 상태 | browser_status에서 pending/approved/denied 조회, 조회만으로 승인 소비하지 않음 |
| 제어권 | 비공개 콘솔에서 연장/취소, 취소는 세션 종료, 종료 실패 시 살아 있는 세션의 잠금 유지 |
| iframe 이미지 | 기본 차단 유지, 운영자 선택의 viewport 마스킹, 가린 영역 조작·불명확한 합성·전체 캡처 차단 |
| 상태/기능 광고 | 관찰 형식·승인 정책·이미지·마스킹 및 미지원 기능을 capabilities로 명시 |
| 실행 진단 | cloud-browser self-test, 설정이 불완전해도 동작하는 doctor, 비밀값을 드러내지 않는 설정 오류 보고 |
| 문서 | CHATGPT_SETUP.md, CONTRACT.md, README.md, SECURITY.md, .env.example 보완 |

기존 세션/탭/노드 API, OAuth/PKCE, 원격 작업 프로세스, egress 격리 구성, noVNC 기반 수동
제어 구조는 교체하지 않았습니다. 클릭/입력의 엄격한 승인 정책도 그대로입니다. 일반 동작과
외부 변경을 완벽하게 구분한다고 주장하는 자동 분류기를 추가하지 않았습니다.

## 실행한 검증

Windows의 새 임시 Chromium 프로필만 사용했습니다. 사용자 기본 Chrome 프로필은 사용하지 않았습니다.

```powershell
$env:CB_TEST_CHROMIUM='C:\Program Files\Google\Chrome\Application\chrome.exe'
$env:CB_TEST_LIVE='true'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m cloud_browser.cli self-test --chromium $env:CB_TEST_CHROMIUM
```

최종 전체 실행 결과:

```text
76 passed, 1 warning in 94.32s
All checks passed!
```

경고는 Starlette 테스트 클라이언트에서의 AnyIO BlockingPortal alias 사용 중단 예고입니다.
테스트 실패는 아니며 이 작업에서 의존성 버전을 강제로 갱신하지 않았습니다.

self-test 결과:

```json
{
  "ok": true,
  "steps": ["mcp_initialized", "real_chromium_opened", "mcp_image_decoded", "session_closed"],
  "image": {"mime_type": "image/jpeg", "width": 1024, "height": 768},
  "production_configuration_used": false,
  "chatgpt_vision_verified": false
}
```

### 공개 사이트 통합 시험

`tests/test_live_mcp.py`에서 실제 Streamable HTTP MCP 클라이언트, 작업 프로세스와 Chromium,
테스트용 비공개 콘솔 세션을 사용했습니다.

1. W3C 아코디언 페이지를 열고 Personal Information의 expanded=true를 관찰.
2. browser_act가 승인 없이 실행되지 않는 것, 토큰 echo가 승인이 아닌 것을 확인.
3. 잘못된 CSRF로는 콘솔 승인이 거부되고, 올바른 테스트 콘솔 승인으로만 approved가 되는 것 확인.
4. 같은 행동/토큰으로 클릭한 뒤 후속 관찰에서 expanded=false 확인.
5. 같은 브라우저 세션에 Python 공식 문서 탭을 추가.
6. max_chars=1000의 auto 관찰에서도 본문과 조작 노드가 함께 반환됨을 확인.
7. 실제 MCP image 콘텐츠를 디코딩해 1024×768 확인 후 전체 세션 종료.

별도 실제 브라우저 시험에서는 iframe 마스킹의 픽셀, 가린 좌표 거부, 변형된 프레임의 차단,
페이지네이션의 본문 복원과 노드 중복 없음, 선언된 목적지의 query 비밀값 마스킹도 확인했습니다.
테스트 성공은 모든 사이트의 의미 추출 정확도나 보안 감사를 완료했다는 뜻은 아닙니다.

## 아직 확인하지 않은 것

- 웹 ChatGPT에 앱을 등록하고 실제 대화에서 도구/이미지를 사용하는 전체 과정.
- 모델이 DOM에 없는 색·배치 정보를 실제 이미지에서 인식하는지 여부.
- 공개 HTTPS/OAuth 연결 및 실제 사용자 로그인·noVNC 수동 화면의 배포 환경 검증.
- Linux amd64/arm64 Docker 실행과 Raspberry Pi 4B 2GB 성능.
- 다양한 동적 사이트·가상 스크롤·Shadow DOM의 완전한 관찰 호환성.

위 기록 작성 당시에는 파일 입력·WebMCP·인증 규칙이 없었습니다. 이후 2026-09-08 작업에서
준비 파일 입력, WebMCP 목록·호출, 운영자 인증 규칙, 동일 출처 iframe 본문 읽기를 추가했습니다.
최신 구현·검증 결과는 [명세 대응 현황](SPEC_STATUS.md)을 따릅니다. iframe 내부 자동 조작,
passkey 전달, 임의 사이트의 범용 인증 판정기는 제공하지 않습니다.

## 현 로컬 설정의 점검 결과

이 작업에서 실행한 doctor는 Chrome과 필수 의존성을 찾았지만, 운영 설정은 아직 유효하지
않다고 보고했습니다. 관리자 해시, OAuth callback, 운영 네트워크 격리 설정과 noVNC 자산이
설정/준비되어 있지 않은 상태였습니다. 이는 로컬 self-test와 구분됩니다.

다음 실행 경로는 CHATGPT_SETUP.md입니다. 실제 origin·callback·관리자 해시는 사용자가
자신의 환경에서 설정해야 하며 비밀번호/토큰을 채팅으로 보낼 필요가 없습니다.

## 변경 범위와 보존

코드는 기존 저장소에 수정/추가했습니다. 실제 .env, 로그인 쿠키/프로필, 배포 볼륨,
Tailscale/Funnel, ChatGPT 앱 설정, Git 커밋/태그/푸시는 변경하지 않았습니다.
테스트용 임시 서버와 브라우저는 종료했습니다. 검증 중 사용하는 캐시는 일반 테스트 캐시이며
운영 데이터가 아닙니다. 변경 목록은 git status와 git diff로 검토할 수 있습니다.
