# 보안 모델과 한계

개인 서버용 프로토타입이며 독립 보안 감사를 받지 않았습니다. 공개 MCP는 로그인
쿠키가 들어 있는 브라우저를 움직이는 고가치 진입점입니다.

## 경계

- 공개 앱: MCP, OAuth, 표준 discovery metadata만 제공합니다.
- 비공개 앱: 별도 리스너, 관리자 암호·HttpOnly cookie·Origin·CSRF 검사.
  MCP bearer로 콘솔에 로그인하거나 승인을 내릴 수 없습니다.
- 원격 화면: 콘솔 인증 + 활성 제어권이 있을 때만 WebSocket 중계. 완료 시 중계를
  닫은 후 자동화를 재개합니다. 원본 VNC/CDP는 노출하지 않습니다.
- Chromium: 별도 UID/프로필, 기본 sandbox 유지. 해당 UID의 새 IP 연결은 지정
  프록시로만 허용합니다. DNS는 프록시가 수행합니다. Docker도 internal 네트워크입니다.
- 프록시: 모든 DNS 응답을 검사한 뒤 **검사한 숫자 IP**에 연결합니다.
  사설·loopback·link-local·metadata·multicast·IPv6 변환/터널 주소를 거부합니다.
  격리된 앱의 URL 사전 검사도 이 프록시의 DNS-only RPC로 수행합니다. 사이트 연결이나
  주소 목록 반환은 하지 않습니다. 실제 HTTP/CONNECT 시 다시 DNS 검사와 IP 고정을
  수행하므로 사전 검사는 재사용 가능한 허가가 아닙니다. 프록시 중단·구버전 프로토콜·
  응답 불일치에는 EGRESS_UNAVAILABLE로 거부하며 직접 DNS/접속으로 fallback하지 않습니다.
- ingress: host loopback의 8000/8001을 browser의 동일 포트로만 바이트 중계합니다.
  목적지 선택·TLS 해제·헤더 변경·인증 대행·payload 로깅을 하지 않습니다.
  비특권/read-only이며 프로필·암호·토큰 볼륨을 공유하지 않습니다. 브라우저 UID는
  이 중계에도 새 연결을 만들 수 없습니다. 포트별 동시 32연결·5초 연결 대기·
  양방향 공통 300초 유휴 제한으로 대기열과 메모리 사용을 제한합니다.

`CB_NETWORK_ISOLATED=true`는 증명이 아닌 배포 단언입니다. 기본 entrypoint는 방화벽
설치 성공 후에만 설정합니다. 구성 변경·네이티브 실행에는 같은 보장이 없습니다.
개발 모드를 Funnel에 연결하지 마세요. `--no-sandbox`, `privileged: true`, 방화벽
실패 무시는 지원되는 해결책이 아닙니다. NET_ADMIN은 시작 시 UID 방화벽 설치용입니다.
rootless Docker나 강한 namespace 제한 런타임은 검증하지 않았습니다.

browser에 적용하는 seccomp 정책은 Docker 29.8.0 기본값 + amd64/arm64 전용 정확한
namespace 호출 6개입니다. [정책·추가 허용 범위](deploy/seccomp/README.md)를 참고하세요.
SYS_ADMIN·privileged·seccomp 전체 해제는 사용하지 않습니다. 커널 공격 표면이 늘어나는
제한적 예외이며, 고정 정책이 새 Docker 버전의 보강을 자동 상속하지는 않습니다.
실제 배포에서 Chromium의 PID/network namespace와 seccomp-BPF를 확인해야 합니다.

Docker 28 이전 localhost 포트 공개는 동일 L2에서 접근 가능한 예외가 있습니다.
최신 보안 업데이트 또는 검증된 별도 host 차단 없이는 private console 격리를
보장하지 않습니다. 일반 LAN 기기와 tailnet 밖에서 거부되는지도 별도 시험하세요.

## OAuth와 데이터

- 사전 등록한 단일 공개 클라이언트, 정확한 HTTPS callback, S256 PKCE,
  resource audience, 발급자·state 응답, 짧은 access TTL.
- DCR/CIMD 자동 등록은 없습니다. ChatGPT의 사전 등록 client ID 방식과의 통합을
  확인해야 합니다. 연결 실패를 callback wildcard 허용으로 해결하지 마세요.
- 승인 HTML의 CSP form-action은 self와 그 요청에서 검증한 callback만 포함합니다.
  query는 CSP source에 넣지 않으며 전체 URI 일치 검사는 OAuth에서 계속 수행합니다.
  CSP는 redirect 시 경로까지 보장하지 않으므로 정확한 callback 검사를 대체하지 않습니다.
  승인·콘솔 HTML은 same-origin Referrer-Policy로 정상 form POST의 Origin을 보존하고,
  외부 referrer를 억제합니다. OAuth 303/API 응답은 no-referrer입니다. null Origin은 거부합니다.
- 비공개 favicon은 내용 없는 204로 응답합니다. 자동 아이콘 조회가 로그인 화면으로
  redirect되어 현재 폼의 nonce 쿠키를 바꾸지 않도록 하며 콘솔 내용은 공개하지 않습니다.
- code/refresh는 일회 사용, grant 취소 시 연결된 토큰 모두 무효화.
- token은 DB에 digest 키로 저장합니다. 평문 access/refresh/code를 저장하지 않습니다.
  승인 제안의 원문·토큰은 활성 요청 동안 메모리에만 존재합니다.
- Argon2id 관리자 암호. 로그인은 5분당 출처별 8회/전체 40회 제한하며 DB에 유지됩니다.
  임의 forwarded header를 믿지 않으므로 프록시 뒤에서는 출처가 합쳐질 수 있습니다.
- 전체 취소: 비공개 콘솔 또는 `cloud-browser revoke-all`.
- `.env`, DB, 프로필·쿠키, 볼륨/백업은 민감합니다. 디스크 암호화·접근 권한은 운영자
  책임입니다. 브라우저 UID는 DB 디렉터리를 읽을 권한이 없고 프로필만 공유합니다.

## 관찰·행동

페이지 텍스트·접근성 이름·도구 설명은 신뢰할 수 없는 데이터입니다. 새 사용자 권한으로
취급하지 않습니다. 임의 JavaScript 실행이나 CDP·쿠키 추출 도구를 노출하지 않습니다.

클릭/fill/키/select/check/좌표 클릭은 전부 사용자 승인 대상입니다. 탐색과 스크롤도
HTTP 요청·웹 스크립트를 일으킬 수 있으므로 **모든 웹 부작용을 탐지한다는 보장은
없습니다**. 악성 사이트·중요한 거래는 수동 조작을 사용하세요.

승인 토큰은 실제 사용자 승인과 정확한 행동/revision에 묶입니다. 소비 후 실패해도
되살리지 않습니다. 실제 요소의 CDP backend ID로 재검증하고 선택자를 추측하지 않습니다.
페이지의 이벤트 처리를 완전히 원자적으로 멈출 수는 없으므로 매우 동적인 화면에는
수동 제어가 필요합니다.

같은 행동/revision의 중복 승인 요청을 합치고 실행 결합의 소비 기록도 유지합니다. 화면이
그대로라는 이유로 토큰을 생략해 다시 제안할 수 없습니다. 실행 후 관찰 실패는
RESULT_UNCERTAIN으로 처리하며 탐색 경로를 통한 재시도도 막습니다. 제어권 반환 중 연결 종료나
재관찰에 실패하면 잠금을 유지하고 인증 성공 또는 반환 성공으로 표시하지 않습니다.

revision은 수집 범위의 텍스트·backend ID·상태·위치·스크롤·뷰포트를 반영합니다.
페이지의 무한한 모든 변화를 추적하지는 않습니다. 좌표는 실제 픽셀도 비교하므로
애니메이션에서 보수적으로 실패할 수 있습니다.

## 비밀값·이미지

- 알려진 password·OTP·카드·secret 입력 화면은 DOM/이미지 반환을 차단합니다.
- 로그인 중 관찰하지 않으며 입력 내용을 로깅하지 않습니다.
- URL userinfo/query 값/fragment, 알려진 token 패턴을 결과에서 제거합니다.
- 안전한 분리가 어려운 iframe/민감 화면은 이미지 전체를 거부합니다. OCR 기반 탐지나
  범용 이미지 마스킹 보장은 없습니다.
- iframe 이미지는 기본 차단입니다. 운영자만 `CB_IFRAME_SCREENSHOT_POLICY=mask`를 선택할
  수 있습니다. 이때 viewport 내 iframe/object/embed의 사각 영역을 JPEG 블록 여백 포함
  검게 덮고 무손실 PNG로 반환합니다. 계산 불가능한 경계·지원하지 않는 합성 효과·전체
  페이지 캡처는 차단합니다. 가린 영역의 좌표 행동도 차단합니다. 이것은 iframe 자동화나
  사이트 스크립트가 다른 곳에 복제한 비밀값까지 숨겨주는 기능이 아닙니다.
- 캔버스에 그려진 비밀값, 사용자 정의 인증 필드, 예상치 못한 본문/제목의 비밀값을
  완벽히 찾을 수는 없습니다. 인증 화면은 자동 관찰하지 말고 인증 제어권을 사용하세요.
- 원문 요청·응답, 입력값, VNC payload를 로그에 남기지 않습니다. 외부 MCP 클라이언트의
  대화/도구 기록은 서버가 통제하지 못합니다.

## 추가 기능의 경계

파일은 사용자가 비공개 콘솔에서 준비한 핸들만 허용합니다. MCP에 임의 경로를 받지 않으며
승인 전·입력 직전에 크기와 SHA-256을 재검사합니다. 정상 종료·요청 시 만료 정리를 하지만
강제 종료 후 보관 잔여물의 보안 삭제를 보장하지 않습니다. 운영자 볼륨 관리가 필요합니다.

WebMCP의 설명·schema·readOnly 힌트·출력은 신뢰할 수 없는 사이트 데이터입니다. 모든 호출을
사용자가 승인하고 외부 schema 참조를 가져오지 않습니다. 실패 후 자동 재시도하지 않습니다.
운영자 인증 규칙 역시 특정 사이트 표시를 확인하는 범위이며 범용 인증 판정기가 아닙니다.

## 검증과 보고

세션이 사라지면 만료로 알리고 몰래 새 세션으로 바꾸지 않습니다. sandbox·UID 방화벽·
제어권 게이트를 실제 목표 환경에서 검증하기 전에는 안전한 운영 배포라고 주장하지
않습니다. [미완료 관문](docs/VALIDATION.md)을 확인하세요.

취약점 보고에 쿠키·토큰·암호를 넣지 마세요. 공개 저장소 게시 전 운영자가 비공개
보안 연락 경로를 추가해야 합니다.
