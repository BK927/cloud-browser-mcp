# 외부 계약 0.3 초안

패키지의 배포 버전과 별개인 개발 중 외부 계약입니다. 기존 10개 도구 이름과 입력을
유지합니다. 0.1 클라이언트는 추가 응답 필드를 무시할 수 있지만, 관찰 문자열의 구체적인
배치나 고정된 JSON 키 순서를 가정해서는 안 됩니다.

원래 8개 도구 이름을 유지하고 `browser_status`, `browser_configure` 및 선택 WebMCP 2개를 제공합니다.
MCP SDK의 `tools/list` 입력 schema가 정확한 타입·범위의 기준입니다.

모든 정상 도구 실행 결과는 `status`, `request_id`, `session_id`, `tab_id`, `revision`,
`page`, `notices`, `error`를 갖습니다. 문맥이 없으면 null입니다. JSON은
`structuredContent`와 text content에 담고 이미지 바이트는 별도 MCP image content로
반환합니다. JSON의 이미지 설명에만 base64를 넣는 방식이 아닙니다.

status: `ok`, `no_change`, `confirmation_required`, `user_action_required`, `blocked`, `error`.
JSON/schema 자체가 잘못된 요청은 SDK 단계의 표준 MCP 오류이며 브라우저 실행 전에
거부됩니다. `request_id`는 서버 추적 ID이고 idempotency key가 아닙니다.

## 입력

| 도구 | 입력 |
|---|---|
| open | session_id?, url?, new_tab=true |
| list_tabs | session_id |
| navigate | session_id, tab_id, operation=goto/back/forward/reload, url? |
| observe | session_id, tab_id, mode=auto/semantic/interactive/visual, full_page=false, max_chars?, cursor? |
| act | session_id, tab_id, expected_revision, action, confirmation_token? |
| auth_request | session_id, tab_id, site_origin |
| handoff | session_id, tab_id, reason |
| close | session_id, scope=tab/session, tab_id? |
| status | session_id?（생략하면 모든 활성 세션） |
| configure | session_id, tab_id, configuration |
| list_page_tools | session_id, tab_id |
| call_page_tool | session_id, tab_id, revision, tool_name, arguments, confirmation_token? |

`configuration`: viewport_width 320..1920, viewport_height 240..1440 (둘을 함께 지정),
screenshot_quality 25..95, max_chars 256..100000, wait_ms 0..10000. 생략한 값은 유지합니다.
초기값: 1024×768 / 품질 75 / 30000자 / 500ms.
운영자 설정 `CB_NAVIGATION_TIMEOUT`은 1..30초, 기본 20초이며 MCP configure로 바꾸지 않습니다.

`action`은 type별로 다른 필드를 받으며 알 수 없는 필드를 거부합니다:

```json
{"type":"click","node_id":"node_..."}
{"type":"double_click","node_id":"node_..."}
{"type":"fill","node_id":"node_...","text":"Godot"}
{"type":"keypress","node_id":"node_...","keys":["ENTER"]}
{"type":"select","node_id":"node_...","value":"recent"}
{"type":"check","node_id":"node_...","checked":true}
{"type":"upload","node_id":"node_...","upload_ids":["upload_..."]}
{"type":"scroll","node_id":null,"delta_x":0,"delta_y":600}
{"type":"click_at","x":640,"y":420,"screenshot_id":"screen_..."}
```

좌표: click_at/double_click_at/move_to/scroll_at. viewport CSS 픽셀 기준이며
full_page 이미지 ID는 좌표 조작에 사용할 수 없습니다. scroll_at은 delta_x/delta_y를
추가합니다. 키는 한 번에 한 개의 지정 특수키만 지원하며 임의 키 조합은 노출하지 않습니다.

## revision·관찰

노드는 관찰한 실제 CDP backend ID에 연결합니다. 페이지 스크립트와 분리된 CDP isolated
world의 MutationObserver로 DOM 변경도 감지합니다. 현재 페이지에서 텍스트/선택자로
재검색하지 않습니다. 텍스트·노드 정체성/상태/위치·스크롤·뷰포트 변경이 발견되면
revision을 올리고 이전 node/screenshot/cursor를 폐기합니다. 구성 변경·제어권 반환도
새 revision을 발급합니다. 움직이는 이미지에 대해서는 좌표 전 픽셀 digest도 비교합니다.

semantic은 화면에 렌더링된 DOM을 문서 순서로 읽으며 main/article을 우선합니다.
제목·목록·표의 간단한 구조를 보존하고 메뉴/푸터와 접근성 트리의 중복을 줄입니다.
`semantic_source`는 선택한 본문 종류, `semantic_source_truncated`는 내부 수집 한도 도달을
표시합니다. 본문 내부 한도는 250000자/탐색 노드 30000개이며, 이 한도 초과 부분은 cursor로
복원되지 않습니다. 이는 원문 전체 보존이나 모든 웹사이트의 완벽한 본문 추출을 보장하지 않습니다.

interactive는 현재 viewport의 조작 요소를 담은 **완전한 JSON Lines**입니다. 빈 기본값을
생략하고 좌표를 반올림하지만 실제 조작 대상은 기존 backend ID를 사용합니다. 최대 300개이며
viewport의 후보가 더 많으면 `interactive_truncated=true`입니다. 화면 밖 요소는 스크롤 후
관찰합니다. 일반 모드에서 폼 목적지/메서드 같은 내부 승인 메타데이터는 노출하지 않습니다.

관찰된 backend ID에 대해서만 Chromium 접근성 정보를 조회해 이름·역할·상태를 보완합니다.
관련 없는 AX 하위 트리나 입력값은 병합하지 않으며 보호된 입력 화면에서는 AX 조회를 하지
않습니다. `accessibility_source`는 `chromium-ax` 또는 `dom-fallback`이며 후자는 경고도
반환합니다. 변경 없는 DOM에서는 AX 이름을 재사용합니다.

select 노드는 최대 200개 option의 label/value/selected/disabled를 제공합니다. 초과는
`options_truncated=true`입니다. 관찰하지 못했거나 비활성인 옵션을 선택하지 않고, 중복 value는
NODE_AMBIGUOUS입니다. readonly 입력·radio 직접 해제·multi-select는 명시적으로 거부합니다.
중첩 option 메타데이터에도 토큰 제거를 적용합니다.

일반 overflow 스크롤 영역에도 node_id와 scrollable/scroll 정보를 부여합니다. 후보 탐색은
30000개 요소로 제한하고 초과하면 `scroll_scan_truncated=true`입니다. 영역 내부 스크롤도
revision에 반영합니다. 이 지원은 Shadow DOM 또는 iframe 내부 자동화를 뜻하지 않습니다.

max_chars는 두 snapshot 문자열의 합산 예산입니다. auto는 조작 목록에 예산을 먼저
확보하므로 긴 본문 때문에 버튼이 전부 밀려나지 않습니다. 각 interactive 행은 독립적으로
JSON 파싱할 수 있습니다. 너무 긴 행은 설명 일부를 생략하고 `details_omitted=true`를
반환하지만 node_id는 유지합니다. 원래의 대상 메타데이터는 서버에서 그대로 검증합니다.
`semantic_truncated`와 `interactive_page_truncated`는 각각 다음 페이지의 존재를 표시합니다.

cursor는 고정 snapshot·두 문자열의 위치·mode·예산·revision에 묶입니다. 같은 cursor를
반복하면 같은 내용이며 페이지가 바뀌면 `CURSOR_STALE`입니다. cursor 호출에서는 처음의
mode/예산을 유지하고 이미지는 다시 생성하지 않습니다. iframe 자동 조작은 미지원입니다.

URL은 최종 목적지를 사용하되 userinfo/query 값/fragment를 마스킹합니다. 원문 URL이
그대로 필요하다는 이유로 비밀 query를 반환하지 않습니다. 이 마스킹 때문에 `page.url`은
원문 navigation input으로 다시 사용하기에 적합하지 않을 수 있습니다.

## 확인·제어

기본 `CB_APPROVAL_POLICY=strict`에서는 모든 클릭/입력/키/select/check가
confirmation_required입니다. 운영자 선택 `balanced`에서는 일반 HTTP(S) 링크,
식별된 펼침/접기·탭, 검색 입력·GET 검색 폼 조작, Tab/Escape를 자동 허용합니다.
좌표 클릭·일반 폼 편집·제출·업로드·페이지 도구·불명확한 행동은 계속 승인 대상입니다.
자동 허용 및 승인 응답에는 `action_policy`의 모드·판정 이유가 포함됩니다.
세부 범위와 스크립트 부작용의 한계는 [승인 정책](APPROVAL_POLICY.md)을 참고하세요. 콘솔의
승인 기록 없이 token만 재전달하면 미실행입니다. 승인과 현재 페이지가 다르면
CONFIRMATION_STALE. 소비한 토큰은 CONFIRMATION_USED. 거절한 토큰은 CONFIRMATION_DENIED이며
자동 재요청하지 않습니다. 결과 불명은 RESULT_UNCERTAIN이며 그 세션의 다음 action도
수동 확인 전까지 차단합니다. `balanced`는 알려진 외부 변경을 허용하는 모드가 아니며,
페이지 JavaScript의 부작용을 완벽히 증명하는 보안 경계도 아닙니다.

동일 세션·탭·revision·행동의 미완료 승인 요청은 하나로 합칩니다. 실행 전 해당 결합의 소비
기록도 저장하므로 화면 변화가 없더라도 토큰을 빼고 재호출해 중복 실행할 수 없습니다.
의도적인 같은 행동의 반복도 새 페이지 상태 또는 수동 확인이 필요합니다. 결과 불명 상태에서는
navigate와 URL을 포함한 open도 차단합니다. 관찰·상태 조회·탭 정리·수동 제어는 가능합니다.

승인 응답의 `current_page`와 `destination`은 다릅니다. 후자는 링크의 href나 제출 폼의
action에서 추출한 **선언된** 목적지이며 없으면 null입니다. `destination_kind`는
`declared_link`/`declared_form`/`unknown`, `destination_verified`는 false입니다.
스크립트·리다이렉트·자동 저장의 실제 전송 목적지를 보장하지 않으며 URL의 비밀값은 마스킹합니다.
`browser_status.approvals`로 pending/approved/denied를 조회할 수 있습니다. 이 조회는 승인을
소비하지 않고 입력값과 승인 토큰도 반환하지 않습니다.

auth/handoff는 즉시 반환합니다. status에서 활성 제어권과 완료 결과를 확인합니다.
세션 전체 자동화를 잠그며 인증 중에는 탭 URL/제목도 수집하지 않습니다. 완료 후
`authenticated: null`, `verification: unverified`는 인증 성공/실패의 추측이 아닙니다.
사용자가 대상 탭을 닫으면 완료 결과에 TAB_NOT_FOUND가 들어갑니다.
`automation_paused`와 `control_access_expired`를 구별합니다. 제어 화면 접근이 만료되어도
자동화는 계속 잠겨 있습니다. 비공개 콘솔에서만 기간 연장·완료·취소를 할 수 있으며,
**취소는 인증 화면을 노출하지 않도록 세션 전체를 닫습니다.**

제어권 반환은 원격 화면 연결 종료와 새 상태 관찰이 모두 성공해야 완료됩니다. 연결 종료
실패·관찰 실패·민감 화면 잔존 시 살아 있는 세션의 잠금을 유지합니다. 기존 RESULT_UNCERTAIN도
해제하지 않습니다. 콘솔은 이 경우 성공 화면 대신 HTTP 409와 실패 상태를 표시합니다.
auth의 supported_methods/unsupported_methods는 비공개 콘솔의 입력 지원 범위이며,
`methods_scope=manual_console_capabilities`, `site_methods_verified=false`입니다. 임의 사이트의
로그인 수단이나 passkey-only 여부를 자동으로 확정한다는 뜻이 아닙니다.

status의 capabilities는 관찰 포맷, 이미지 응답, 승인 정책, iframe 캡처 정책, 파일 업로드,
WebMCP, passkey 전달, 인증 검증 지원 여부를 표시합니다. 지원 광고는 ChatGPT의 계정별 연결
성공이나 실제 이미지 인식 성공을 인증하는 것이 아닙니다.

## 명시적 제한

- private/local/file/javascript/data URL로의 자동 탐색은 지원하지 않습니다. HTTP(S),
  공개 DNS/IP, 80/443만 지원합니다. 새 빈 탭에 한해 내부적으로 about:blank를 사용합니다.
- GET으로 확인된 문서/방문 기록만 reload/back/forward합니다. POST/알 수 없는 기록은
  `user_action_required` + UNSUPPORTED_OPERATION으로 handoff를 요청합니다. 실행할 수 없는
  confirmation_token을 발급하지 않습니다. 수동 제어 중의 요청 방식은 수집하지 않으므로
  제어권 반환 후의 알 수 없는 기록도 수동 조작 대상입니다.
- navigation timeout은 성공으로 표시하지 않습니다. 후속 관찰로 확인해야 합니다.
- 탐색은 요청한 loader 또는 history entry와 문서 로딩 완료를 함께 확인합니다. 탐색 실패·
  timeout에서도 이전 노드·좌표·cursor는 폐기합니다. 단순히 이전 문서가 보이는 것은 성공이 아닙니다.
- 외부 변경 여부가 불명확한 행동은 승인 대상이지만 범용 부작용 분석기는 아닙니다.
- 파일 입력과 WebMCP는 [확장 계약](CONTRACT_EXTENSIONS.md)의 제한·승인 절차를 따릅니다.
  iframe 내부 자동 조작과 passkey/보안 키 전달은 제공하지 않습니다.
- 인증/알려진 token 화면은 이미지 반환을 거부합니다. iframe 화면도 기본적으로 차단합니다.
- 운영자 설정 `CB_IFRAME_SCREENSHOT_POLICY=mask`는 viewport 캡처에서만 iframe/object/embed
  영역을 여백 포함 검게 가린 PNG를 허용합니다. 좌표가 불명확하거나 transform/filter 등 지원하지
  않는 합성이 있으면 전체 차단하며, full_page도 차단합니다. 가린 영역의 좌표 조작은 거부합니다.
  일반 캡처는 JPEG이고, 마스킹은 시각 정보를 제거하는 제한적 선택 기능이지 임의 비밀값 탐지나
  사이트 스크립트의 정보 유출 방지를 보장하지 않습니다. MCP configure로 정책을 완화할 수 없습니다.
- CAPTCHA/BOT 차단은 명확한 화면 문구를 증거로 구분합니다. 단순 HTTP 403/429나
  timeout만으로 BOT_BLOCKED를 반환하지 않습니다. 우회·자동 반복 새로고침은 없습니다.

## 추가 오류

원안 오류 외에 CURSOR_STALE, STALE_SCREENSHOT, SCREEN_CHANGED, SENSITIVE_SCREEN,
SENSITIVE_INPUT, NODE_NOT_ACTIONABLE, INVALID_COORDINATES, INVALID_INPUT,
OBSERVATION_FAILED, UNSUPPORTED_OPERATION, ENGINE_UNAVAILABLE, RESOURCE_PRESSURE, WORKER_TIMEOUT,
BROWSER_ERROR, CONFIRMATION_USED, CONFIRMATION_DENIED, AUTH_IN_PROGRESS, AUTH_ORIGIN_MISMATCH,
USER_CONTROL_ACTIVE, HANDOFF_UNAVAILABLE, HANDOFF_NOT_FOUND를 사용합니다.
CONTROL_DISCONNECT_FAILED는 원격 제어 연결을 안전하게 끊지 못한 경우입니다.
관찰된 DOM 요소에 좌표 클릭을 시도하면 DOM_TARGET_AVAILABLE로 거부하고 node_id 사용을
요청합니다.
모두 실패를 성공처럼 숨기는 대신 나타내는 확장입니다.

0.3의 폼 값 결합, iframe 의미 읽기, 인증 규칙·실패 보고, 업로드·WebMCP 계약은
[확장 계약](CONTRACT_EXTENSIONS.md)을 함께 적용합니다. 기본 인증 규칙이 없으면 앞서 설명한
unverified 상태이며, 운영자 규칙을 설정한 경우에만 근거가 확인된 인증 결과를 제공합니다.
