# Pi 배포 결함 수정 — 2026-09-08

기존 구현·운영 자격증명·프로필 볼륨은 유지하며 원본 수정과 Pi 실행 소유자를 분리했습니다.
이 문서는 완료된 전체 배포나 성능 보증서가 아닙니다.

## 재현과 수정

1. Docker 26.1.5에서 browser internal-only 네트워크의 host PortBindings가 실제
   생성되지 않았습니다. 당시 앱 내부 health/MCP initialize/도구 12개 조회만 통과했습니다.
   browser의 직접 publish를 제거하고, 별도 비특권 ingress를 통해 8000/8001을
   각각 browser의 같은 포트로만 중계합니다. browser의 네트워크 격리는 그대로입니다.
2. 같은 Pi에서 일반 사용자의 user/PID/network namespace 생성은 성공하지만 컨테이너의
   Chromium은 EPERM로 실패했습니다. 기본 seccomp에 Chromium이 쓰는 정확한 호출
   인자 6개만 허용합니다. SYS_ADMIN·privileged·unconfined·no-sandbox는 쓰지 않습니다.

후속 검토에서 Docker 26의 localhost publish 동일-L2 예외와 오래된 seccomp 기본값의
보안 보강 누락을 확인했습니다. 따라서 운영 후보는 Docker 29.8.0 기본 정책에 맞춰
갱신했습니다. host 패키지 업그레이드·백업·재실행은 배포 담당자가 별도로 수행합니다.

## 후속 자원 계산 보정

첫 번들로 Pi MCP session open 및 실제 1024×768 이미지 반환, 네트워크/포트 검증이
통과했으나, 후속 blank 1탭의 observe가 `_admit()`에서 거부됐습니다.
거부 시 host available 824MiB, cgroup raw 사용량 약 878MiB/1024MiB,
raw 여유 약 145MiB였고 reserve는 256MiB였습니다. 이 charge에는 inactive_file
322,920,448바이트가 포함됐으며 dirty 31,227,904바이트, writeback 0이었습니다.
memory.high=max, memory.events max/oom/oom_kill=0이었습니다.

원시 charge 전체를 비가용으로 계산하던 로직을 보정합니다. clean inactive file에만
보수적인 회수 추정량을 부여하며 raw charge를 보고서에서 숨기지 않습니다.
host available·hard limit·reserve·admission budget은 유지하고, stat 누락은 credit 0,
유한 cgroup budget 읽기 실패는 신규 작업 거부로 처리합니다. cache drop/reclaim 쓰기,
swap 확대·메모리 상한 변경·프로필 정리는 하지 않습니다.

근거: [kernel memory.stat 정의](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory)
및 [Docker stats의 캐시 제외 설명](https://docs.docker.com/reference/cli/docker/container/stats/).
Docker stats의 working set 표시를 그대로 빈 메모리로 쓰지는 않습니다.
이는 추정 admission 검사이며 실제 reclaim 지연이나 OOM 부재를 보증하지 않습니다.

자원 계산·서비스 집중 회귀 42개와 ruff/diff 검사를 통과했습니다. 최종 후보로 전체
로컬 회귀와 Pi warm 반복 관찰을 다시 통과하기 전에는 공개·승격하지 않습니다.

## 후속 DNS 경로 보정

메모리 보정 번들에서 blank 관찰·이미지·handoff WebSocket·제어 반환·재시작 후 만료는
Pi에서 통과했으나 외부 goto가 INVALID_URL로 거부됐습니다. browser internal-only의
외부 DNS 조회는 EAI_AGAIN, egress의 동일 DNS/공개 CONNECT는 성공했습니다.
브라우저의 외부 DNS 차단은 유지하고 URL 사전 DNS 검사만 내부 egress로 옮깁니다.

기존 내부 3128 포트의 `CB-DNS-CHECK host:port`는 DNS-only 사전 검사입니다.
공인 A/AAAA 검사 후 버전 표식과 204/403만 반환하며 사이트 연결·IP 목록·허가 토큰은
없습니다. 실제 HTTP/CONNECT는 새로 DNS 검사하고 확인한 숫자 IP에 연결합니다.
proxy 중단·구버전·표식 불일치·timeout은 EGRESS_UNAVAILABLE로 탐색 전에 거부합니다.
native는 기존 DNS를 사용하며 URL 구문·포트·자격증명·비공개 IP 검사는 유지합니다.
외부 DNS/직접 연결·방화벽 완화·새 포트·운영 자격증명 변경은 없습니다.

Pi에서 메모리 보정 번들의 blank benchmark는 interactive 58–95ms, visual 144–264ms,
warm open 약2.93초였습니다. blank 4탭 뒤 5번째는 실제 reserve+admission 부족으로
거부됐고 status/close가 가능했습니다. 이는 실제 웹페이지 안정 탭 수 보증이 아닙니다.
Docker daemon PSS 약71.7MiB, containerd 약24.2MiB는 브라우저와 별도로 측정했습니다.
두 관리 프로세스의 합 약95.9MiB에는 기타 shim·브라우저·프록시·ingress가 포함되지 않습니다.

## 현재 증거와 남은 관문

### 실제 Chrome OAuth·콘솔 폼 보정

HTTP 클라이언트는 Origin을 수동으로 넣고 CSP를 집행하지 않아 다음 결함을 놓쳤습니다.
임시 Chrome 프로필과 서로 다른 로컬 origin을 사용해 순서대로 재현했습니다.

1. no-referrer 승인 문서의 form POST는 Chrome에서 Origin:null로 전송돼 403이었습니다.
2. 문서 Referrer-Policy만 same-origin으로 바꾸면 POST303은 성공하지만,
   기존 form-action self가 외부 callback을 차단하며 Chrome Log에 정책 오류가 남았습니다.
3. private 로그인도 같은 Origin 문제가 있었고, favicon 자동 조회가 /login으로 redirect해
   이미 보이는 폼의 nonce 쿠키를 덮어쓰면서 Invalid login form을 일으켰습니다.

승인 문서는 self와 검증된 단일 callback source만 CSP에 넣으며, 별도 제한 CSP를 중복으로
붙이지 않습니다. 문서의 same-origin 정책으로 정상 Origin을 유지하지만 callback 303/API는
no-referrer로 유지합니다. private HTML도 same-origin이며 favicon은 무내용 204입니다.
Origin:null 허용·CSRF/PKCE 해제·callback wildcard·CSP 전체 해제는 하지 않았습니다.
callback 설정도 빈 호스트·userinfo·제어문자·잘못된 포트를 거부합니다.

실제 Chrome에서 callback GET에 password/본문/referrer가 없고 code/state/issuer만 도착,
PKCE 코드 교환과 재사용 거부, 미등록 form 목적지 차단을 확인했습니다. private 로그인,
잘못된 CSRF 거부, 실제 승인 버튼(실행은 별도), 동일 tab/revision을 확인하는 제어 반환도
집중 회귀에 포함합니다. 이 테스트는 로컬 HTTP origin을 사용하는 한정된 테스트 대체이며
생산 HTTPS callback 설정을 완화하지 않습니다. 실제 ChatGPT UI 통합은 별도 관문입니다.

참고: [MDN form-action의 redirect 주의사항](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/form-action).

### 기존 단계의 기록

- Pi Docker 26의 격리 canary: 기본 정책 EPERM, 정확한 6규칙 추가 후 blank DOM 통과.
  내부 sandbox 진단은 Namespace, PID/network, seccomp-BPF, TSYNC 모두 활성으로 표시.
  Yama ptrace는 No였습니다. 개인 프로필·인증정보·host 포트 없는 임시 컨테이너였습니다.
- 최종 정책은 Docker 29.8.0 vendored 기본 JSON과 동일한 seccomp v0.2.3 + 6규칙입니다.
  26 후보를 그대로 출하하지 않습니다. AF_ALG/AF_VSOCK 및 나머지 기본 차단을 보존합니다.
- Pi Engine 29.8.0에서 최종 정책 SHA-256
  `50797a4acbdc5b4146763f9ce8a787b3f5e78a16877a8db819ce4c2ea6b62100` 재검증:
  기본 정책 EPERM(1.46초), 후보 blank DOM 통과(6.60초), 실제 sandbox 표 확인(6.31초).
  Namespace/PID/network/seccomp-BPF/TSYNC 활성, Yama 두 항목 No를 확인했습니다.
- 로컬 테스트는 고정 목적지·헤더 무해석·바이너리/WebSocket/SSE 바이트 전달,
  1MiB 전달·half-close 후 응답·연결 상한·유휴 회수·종료·upstream 실패와 정책 차이를 검사합니다.
- 전체 로컬 회귀: 136 passed, 1 skipped, 1 warning / 156.55초. 실제 별도 Chrome,
  W3C live와 HTTP MCP 이미지 시험을 포함합니다. 기본 엔진 WebMCP 미노출 1개 skip이며,
  남은 경고는 Starlette/AnyIO 의존성 deprecation입니다. 최종 29 정책 갱신 후 추가한
  AF_ALG 회귀를 포함한 집중 시험 17 passed / 2.72초, ruff 전체 통과도 확인했습니다.
- 최종 패키지로 ingress의 host upstream, 인증된 HTTP MCP/이미지,
  private console/실제 WebSocket, 프록시 중단 시 차단, 다른 MCP 회귀 검증이 필요합니다.
- Docker 관리 프로세스 RAM과 browser/egress/ingress RAM은 분리해 측정해야 합니다.
  컨테이너 상한과 실제 사용량은 다릅니다. 시험에 사용한 Raspberry Pi 4 Model B(2GB RAM)는
  권장 장비나 최소 요구사항이 아닙니다. 해당 장비의 성능·긴 캡처·메모리 압박,
  실제 웹 ChatGPT OAuth/이미지 인식은 별도 관문입니다.

운영 .env·DB·프로필·토큰은 배포 아카이브나 이 기록에 넣지 않습니다.
이번 수리는 패키지 버전 0.1.0의 미커밋 작업본이며, GitHub 출시 완료를 뜻하지 않습니다.
