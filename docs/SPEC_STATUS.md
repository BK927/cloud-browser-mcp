# 원래 명세 대응 현황 — 2026-09-08

대상은 외부 계약 0.3 초안의 현재 로컬 작업본입니다. 배포·실장비 시험과 코드 구현 상태를
분리합니다. 코드·문서의 MIT 및 DrissionPage의 별도 라이선스 경계는 변경하지 않았습니다.

후속 Pi 배포에서 발견한 포트 연결·Chromium namespace 결함 수정 및 29.8.0 canary는
[배포 수정 기록](DEPLOYMENT_FIXES.md)에 있습니다. 이 후속 작업은 기존 구현을 유지하며
추가한 배포 보정입니다. 도구 계약과 인증정보는 변경하지 않았습니다.

## 이번에 재현하고 보완한 핵심 동작

| 원래 요구 | 보완 내용 |
|---|---|
| 의미·조작 관찰 | 실제 Chromium AX 이름·역할·상태를 관찰한 backend ID에 병합. 불가하면 DOM fallback 경고 |
| 선택·입력 | option의 값·이름·선택/비활성 상태 제공. 없는 옵션·중복 값·readonly·radio 해제의 명시적 거부 |
| 스크롤 | 일반 내부 스크롤 영역의 node_id와 위치 제공, 스크롤을 revision에 반영 |
| 탭 상태 | 실제 포커스/가시성으로 사용자 선택을 읽고 선택 순서·기존 탭 재사용에 반영. 조회가 탭을 선택하지 않음 |
| 탐색 완료 | 요청한 문서 loader/방문 기록과 로딩 완료를 함께 확인. 이전 화면·네트워크 오류를 성공으로 표시하지 않음 |
| 안전한 기록 이동 | GET으로 확인한 기록만 자동 재탐색. POST/알 수 없는 기록은 handoff 요구 |
| 오래된 관찰 거부 | 탐색 실패·timeout에도 기존 node/screenshot/cursor를 무효화 |
| 승인·중복 호출 | 동일 행동의 승인 제안을 하나로 합치고, 실행 결합의 소비를 영속 기록. 변화 없는 화면에서도 중복 실행 차단 |
| 불확실한 결과 | 실행 뒤 관찰 실패를 RESULT_UNCERTAIN으로 전환. 후속 act뿐 아니라 탐색·URL open도 차단 |
| 인증·제어권 반환 | 연결 종료 + 민감 화면 검사 + 재관찰 성공 후에만 재개. 실패 시 잠금과 불확실 상태 유지, 콘솔도 실패 표시 |
| 자원 제한 | 빈 세션을 new_tab=false로 재사용해도 실제 탭 생성 시 메모리 검사 |
| CAPTCHA·차단 | observe뿐 아니라 실행 직전에도 명확한 challenge 증거 검사. 자동 해결·우회 없음 |

동일 상태의 동일 행동은 의도적 반복과 통신 중복을 구분할 수 없으므로 보수적으로 차단합니다.
새로고침으로 승인 기록을 우회해 위험 행동을 반복하라는 뜻이 아닙니다.

회귀 테스트는 `test_spec_completion.py`, `test_browser_completion.py`,
`test_navigation_completion.py`에 있습니다. 기존 기능 시험도 함께 실행합니다.
단위 테스트의 실패 상황 주입은 실제 Pi 메모리 압박·네트워크 단절 시험을 대신하지 않습니다.

## 후속 구현 완료 항목

| 남아 있던 기능 | 현재 구현·로컬 검증 |
|---|---|
| 폼 제출 승인 | 폼 항목명 표시, 숨김·화면 밖 값도 digest로 revision 결합, 내부 한도 초과 시 자동 조작 거부 |
| 인증 완료·실패 | 정확한 출처의 운영자 성공/실패 표시 검사, 근거 없으면 null/unverified. 사용자 보고와 실제 판정을 구분 |
| 지원 불가 인증 | 운영자 규칙에서 passkey/보안 키만 지정되면 AUTH_METHOD_UNSUPPORTED. 비공개 사용자 보고 경로도 제공 |
| 복합 페이지 관찰 | 접근 가능한 동일 출처 iframe 본문 포함, 민감한 하위 문서 발견 시 부모 관찰도 차단 |
| 파일 입력 | 비공개 준비 → 불투명 ID → 파일 정보 승인 → 동일 파일 재검사 → 실제 Chromium 파일 입력. 승인 전 0회·승인 후 1회·재호출 미실행 검사 |
| WebMCP | 실제 Chromium CDP 광고·호출 연결. 모든 호출 승인, schema 검증, 목록 변경·제어권 전환 이후 stale 거부 |

현재 17개 MCP 도구를 제공합니다. 입력·출력·운영자 설정은
[확장 계약](CONTRACT_EXTENSIONS.md)에 있습니다. 단순 미지원 메시지로 위 기능을 대신하지 않습니다.

2026-09-08 최종 전체 로컬 회귀: **123 passed, 1 skipped, 1 warning in 142.51s**.
W3C 실페이지, 인증된 HTTP MCP → worker → 실제 Chromium → 이미지 반환을 포함합니다.
WebMCP 기본 Chrome API 미노출 시험 1개는 skip이며, 별도 임시 프로필에서 공식 실험 플래그를
켜고 네이티브 등록·호출·새 도구 등록 후 stale 거부를 실제로 확인했습니다. 가짜 페이지 API를
주입해 네이티브 지원인 것처럼 표시하지 않습니다. 단위 테스트의 CDP 대역은 별도로 구분합니다.

정적 검사(ruff), uv frozen 설치, wheel/sdist 빌드도 통과했습니다. 패키지에 추가 Python 모듈,
snapshot.js, 라이선스 고지와 소스 문서가 포함되고 운영 .env·프로필·SQLite는 포함되지 않음을
확인했습니다. 남은 경고 1건은 Starlette/AnyIO의 의존성 사용 중단 예정 경고입니다.

최종 실행은 `CB_TEST_CHROMIUM`에 별도 Chrome 실행 파일, `CB_TEST_LIVE=true`를 지정하고
`pytest -q -rs -p no:cacheprovider --basetemp <새 임시 디렉터리>`로 재현합니다.
pytest가 사용하는 Chrome은 모두 테스트 프로필이며 실제 계정·사용자 프로필을 열지 않습니다.
업로드·Funnel·Serve·운영 .env·Git 커밋/푸시는 이번 후속 구현에서 변경하지 않았습니다.

## 의도된 제한

- 범용 외부 변경 분류기·모든 사이트의 인증 수단 자동 판정기는 아닙니다. 사이트별 근거가
  없으면 인증 성공이나 passkey-only를 추측하지 않습니다.
- iframe 내부 자동 조작, 닫힌 Shadow DOM 전체 관찰, passkey/보안 키 전달은 제공하지 않습니다.
  원안의 iframe 관찰은 접근 가능한 경우의 선택 동작이며 passkey 전달은 원안부터 미지원입니다.
- 임의 화면·외부 도구 결과의 모든 비밀값 탐지를 보장하지 않습니다. 알려진 민감 화면 차단,
  제한적 마스킹, 인증 중 수집 중단, 알려진 토큰 제거를 적용합니다.
- WebMCP는 실험적 런타임 기능입니다. 엔진 자체가 지원하지 않으면 UNSUPPORTED_OPERATION입니다.
  도구 설명·schema·readOnly 힌트를 신뢰하거나 승인 면제 근거로 사용하지 않습니다.
- 사이트 JavaScript의 임의 부작용·검사 직후의 경쟁적 변경까지 원자적으로 통제하는 서버는 아닙니다.
  사전 재검사·후속 실제 상태 확인·결과 불명 시 잠금으로 대응합니다.

## 다른 세션에서 진행할 배포 검증

- Debian arm64/amd64 Docker 실제 실행 및 네트워크 격리·noVNC.
- Raspberry Pi 4 Model B(2GB RAM)의 실측 성능·메모리 압박·긴 페이지 캡처·브라우저 종료.
  이 장비는 시험 장비일 뿐이며 권장 장비나 최소 요구사항이 아닙니다.
- 공개 HTTPS/Funnel/OAuth에서 웹 ChatGPT가 실제 도구와 이미지 내용을 인식하는지 확인.

공개 커밋 `4e119f4`의 GitHub Actions에서는 Linux 브라우저 시험과 amd64/arm64 이미지
**빌드**가 성공했습니다. 이것을 컨테이너 실행 또는 현재 미커밋 작업본의 원격 검사 결과로
해석하지 않습니다. 기존 실행 기록은 VALIDATION.md와 IMPLEMENTATION_REPORT.md를 참고하세요.

구현에서 사용한 Chromium 기본 인터페이스:
[접근성 정보](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/),
[탐색·방문 기록](https://chromedevtools.github.io/devtools-protocol/tot/Page/).
