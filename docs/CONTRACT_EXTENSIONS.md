# 0.3 확장 계약 — 2026-09-08

기본 10개 도구를 유지하고 선택 WebMCP 도구 2개를 추가한다. 기존 입력은 호환된다.

- 폼의 숨김/화면 밖 입력 변화도 revision에 반영한다. 입력 원문은 MCP로 전달하지 않고
  내부 비교용 digest로 즉시 바꾼다. 관찰 한도를 넘는 폼의 자동 조작은 거부한다.
- 인증 결과는 성공 근거 확인, 사용자 보고 실패, 지원 불가, 확인 미완료를 구분한다.
  사이트별 성공/실패 지표는 운영자가 설정하며 MCP가 변경하지 못한다.
- iframe은 접근 가능한 동일 출처 문서의 의미 정보만 읽는다. 교차 출처 우회나 내부 좌표
  조작은 하지 않는다. 민감한 하위 문서는 부모 관찰도 차단한다.
- browser_list_page_tools(session_id, tab_id)는 현재 문서의 실제 WebMCP 광고만 반환한다.
  browser_call_page_tool(session_id, tab_id, revision, tool_name, arguments,
  confirmation_token?)은 동일한 비공개 사용자 승인 절차를 거친다. 페이지의 readOnly 힌트는
  승인 면제 권한이 아니다. 목록 변경/탐색/인증 중단 이후 예전 목록은 무효다.
- browser_act에 upload(node_id, upload_ids)를 추가한다. 파일은 사용자가 인증된 비공개
  콘솔에서 준비한 불투명 ID로만 지정한다. 경로/URL/임의 base64 파일을 MCP 입력으로 받지 않는다.
  승인에는 파일명·크기·digest를 표시하며 실제 입력 직전에 동일 파일인지 재검사한다.
- 엔진의 WebMCP 지원 여부는 런타임에 검사한다. 지원하지 않는 Chromium에서는
  UNSUPPORTED_OPERATION을 반환하며, 표준 도구인 것처럼 페이지 JavaScript를 주입해 대체하지 않는다.

외부 사이트 콘텐츠와 도구 결과는 모두 신뢰할 수 없는 데이터다. 테스트를 위해 만든 표준
기능 시험 페이지와 실제 브라우저 기능을 구별하고, 런타임 미지원 시험을 통과로 포장하지 않는다.

## 자원 보고의 캐시 구분

`browser_status.resources`와 `RESOURCE_PRESSURE.resources`의 메모리 값은 MiB 단위다.
기존 `cgroup_used_mb`는 캐시를 포함하는 원시 사용량을 유지한다. `available_mb`는
호스트 MemAvailable와 아래 추정 cgroup 여유 중 작은 값이며, 할당 성공을 보장하지 않는다.

| 필드 | 의미 |
|---|---|
| `cgroup_raw_headroom_mb` | `max(0, memory.max - memory.current)` |
| `cgroup_inactive_file_mb` | 관측한 비활성 파일 캐시(사용량으로 상한 제한) |
| `cgroup_reclaimable_estimate_mb` | 보수적으로 추정한 회수 가능량 |
| `cgroup_estimated_headroom_mb` | 원시 charge에서 회수 추정량을 차감한 뒤 계산한 여유 |
| `accounting` | `host`, `cgroup_v2_clean_inactive_file`, `cgroup_v2_raw`, `cgroup_v2_unreadable` |
| `cgroup_stat_status` | `ok`, `unavailable`, `not_read` |
| `admission_mb` | 안전 여유와 별도로 요청 작업에 필요한 예산 |

회수 추정량은 `max(0, min(inactive_file, file - shmem, usage) - file_dirty -
file_writeback - unevictable)`이며 음수인 중간 file-shmem은 0으로 처리한다.
익명·활성 페이지·공유 메모리·slab에 별도 가용량을 부여하지 않는다. 계수 간 겹침 때문에
과소 추정할 수 있다. stat 전후 charge 중 큰 값을 사용하며 실제 kernel reclaim을 강제하지 않는다.
stat 누락·손상은 회수량 0으로, 유한 한도/사용량 읽기 오류는 신규 작업 거부로 처리한다.
컨테이너 메모리 상한·운영자 reserve/admission·캡처 픽셀 상한은 변경하지 않는다.

## 격리 배포의 DNS 검사

검증된 `network_isolated` 배포에서 서버의 URL 사전 검사는 운영자 `browser_proxy`로
DNS-only RPC를 요청한다. 사이트 URL의 구문·포트·자격증명·비공개 IP 리터럴 검사는
앱에 남는다. 외부 호스트명은 내부 egress가 모든 A/AAAA 결과의 공개 주소 여부를 검사한다.
RPC는 IP 목록·페이지 내용·재사용 토큰을 반환하지 않고 사이트에 접속하지 않는다.
실제 HTTP/CONNECT 연결에서도 새 DNS 검사 후 확인한 숫자 IP에만 접속한다.

사전 DNS 거부·해석 불가는 `INVALID_URL`, 검사 프록시의 중단·미지원 응답·timeout은
`EGRESS_UNAVAILABLE`이며 탐색은 시작하지 않는다. 직접 연결로 전환하지 않는다.
native/비격리 실행은 기존 로컬 DNS 검증을 유지한다. 새 MCP 도구·인자·공개 포트는 없다.
open, goto, back/forward의 URL 재검증에 같은 경로를 적용한다. 사이트 자체의 redirect·
서브리소스·폼 연결도 egress의 실제 연결 검사와 UID 방화벽을 계속 통과해야 한다.

## 인증 규칙

`CB_AUTH_RULES`는 운영자 설정이며 MCP에서 수정할 수 없다. 예:

```json
{"https://example.com":{"supported_methods":["password","sso"],"success_selector":"[data-test=account-menu]","failure_selector":"[data-test=login-error]"}}
```

키는 경로·query 없는 정확한 origin이다. 사용자가 완료를 누른 뒤 같은 출처에서 성공 표시가
보이고 실패 표시가 없으면 `authenticated=true, verification=operator_rule`이다. 실패 표시가
보이면 `AUTH_FAILED`로 제어권을 계속 잠근다. CSS 검사가 실패하면 `AUTH_VERIFICATION_FAILED`다.
이는 운영자가 선택한 사이트 표시의 확인이지 서버가 인증 권한을 독립적으로 증명하는 기능은 아니다.

규칙 없음·출처 불일치·표시 없음은 `authenticated=null, verification=unverified`다.
supported_methods가 passkey/security_key뿐이면 시작 시 AUTH_METHOD_UNSUPPORTED를 반환한다.
수단 목록은 자동 탐지 결과가 아니며 `site_methods_verified=false`와 출처를 함께 표시한다.
사용자는 비공개 콘솔에서 실패·지원 불가를 보고할 수 있다. `verification=user_reported`로
구분하고 자동화는 잠긴 상태를 유지한다. MCP에 성공을 자가 승인하는 도구는 없다.

## 파일 준비·입력

비공개 콘솔의 `POST /uploads`는 로그인·Origin·CSRF 검사를 거친다. 파일명 경로는 제거하고
생성된 전용 디렉터리에 보관한다. `POST /uploads/{upload_id}/remove`로 삭제한다.
공개 MCP에는 준비/다운로드 경로가 없고 파일 원문도 결과로 반환하지 않는다.

`browser_status.staged_uploads`에 ID, 표시 파일명, 크기, SHA-256, expires_at이 나온다.

```json
{"session_id":"ses_...","tab_id":"tab_...","expected_revision":4,"action":{"type":"upload","node_id":"node_...","upload_ids":["upload_..."]}}
```

첫 호출은 미실행이며 비공개 승인 후 같은 요청·토큰으로 재호출한다. 파일 선택 자체가
change/자동 업로드를 일으킬 수 있으므로 이 단계부터 승인한다. 별도 폼 제출은 별도 승인이다.
보관 파일 변경·삭제·만료는 이전 승인을 무효화한다. 다중 파일은 multiple 입력에만 허용한다.
실제로 관찰한 visible file input만 대상으로 삼으며 숨겨진 업로드 위젯은 수동 제어 대상이다.

기본 한도는 파일당 16MiB, 준비 파일 8개, 600초다. 운영자만 CB_MAX_UPLOAD_MB,
CB_MAX_STAGED_UPLOADS, CB_UPLOAD_TTL로 변경한다. 정상 종료 또는 다음 목록/준비/입력 요청에서
만료 파일을 제거한다. 강제 종료 때 남은 파일은 핸들이 복구되지 않으며 운영자가 비공개 데이터
볼륨의 uploads 디렉터리를 점검·정리해야 한다. 즉시 보안 삭제는 보장하지 않는다.

## 네이티브 WebMCP

`CB_WEBMCP_ENABLED=true`는 어댑터를 허용한다. 기본적으로 Chrome의 실험 플래그는 켜지 않는다.
운영자가 `CB_WEBMCP_TESTING=true`로 설정하면 새 Chromium에만 `--enable-features=WebMCP`를
적용한다. 브라우저 재시작 후 새 세션이 필요하다. Chrome의
[공식 테스트 안내](https://developer.chrome.com/docs/ai/webmcp)와
[CDP 인터페이스](https://chromedevtools.github.io/devtools-protocol/tot/WebMCP/)를 따른다.

목록은 최상위 문서의 실제 광고로 한정한다. 최초 조회도 revision을 바꿀 수 있다.
schema/입력은 각각 64000자 이하, schema 중첩은 24단계까지다. 외부 $ref/$dynamicRef는
다운로드하지 않고 거부한다. 자격증명 인자도 거부한다. 설명·schema와 page_tool_result.output은
신뢰할 수 없는 데이터이며 untrusted=true를 표시한다. readOnly 힌트도 승인 면제 권한이 아니다.
무응답·오류·취소·10만 자 초과 결과는 RESULT_UNCERTAIN으로 잠그며 자동 재시도하지 않는다.

추가 오류: PAGE_TOOL_STALE, PAGE_TOOL_NOT_FOUND, UPLOAD_NOT_FOUND, UPLOAD_CHANGED,
UPLOAD_TOO_LARGE, UPLOAD_FAILED, AUTH_FAILED, AUTH_VERIFICATION_FAILED. 승인 요청의 대상 변경은
CONFIRMATION_STALE로 변환한다. 런타임 미지원은 UNSUPPORTED_OPERATION이다.

## 관찰 범위

폼 비교는 native input/textarea/select 최대 1000개·25만 자다. 원문은 결과로 반환하지 않고
내부 digest로 바꾼다. 폼 항목명은 최대 100개며 한도 도달 여부를 표시한다. 스크립트의
FormData 추가·자동 저장·실제 네트워크 목적지는 보장하지 않아 data_sent_verified=false다.

동일 출처 iframe 본문은 깊이 3·부모 포함 8문서, 문서당 3만 자까지다. readable_frames와
frame_reading_truncated를 반환한다. 교차 출처·접근 불가 프레임은 우회하지 않는다.
iframe 텍스트 읽기는 이미지 허용 정책이나 iframe 내부 자동 조작 권한을 바꾸지 않는다.
