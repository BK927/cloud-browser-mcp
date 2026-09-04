# 외부 계약 0.1

원래 8개 도구 이름을 유지하고 `browser_status`, `browser_configure`를 추가합니다.
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

`configuration`: viewport_width 320..1920, viewport_height 240..1440 (둘을 함께 지정),
screenshot_quality 25..95, max_chars 256..100000, wait_ms 0..10000. 생략한 값은 유지합니다.
초기값: 1024×768 / 품질 75 / 30000자 / 500ms.

`action`은 type별로 다른 필드를 받으며 알 수 없는 필드를 거부합니다:

```json
{"type":"click","node_id":"node_..."}
{"type":"double_click","node_id":"node_..."}
{"type":"fill","node_id":"node_...","text":"Godot"}
{"type":"keypress","node_id":"node_...","keys":["ENTER"]}
{"type":"select","node_id":"node_...","value":"recent"}
{"type":"check","node_id":"node_...","checked":true}
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

semantic은 접근성 tree와 렌더링된 본문, interactive는 보이는 조작 요소의 JSON Lines
문자열입니다. 최대 300개 조작 요소를 포함하고 잘리면 `interactive_truncated=true`를
표시합니다. 더 큰 페이지는 스크롤/추가 관찰이 필요합니다. 본문 내부 수집 한도는 250000자입니다.
결과 max_chars 페이지네이션은 하나의 고정 관찰 결과에 묶입니다. 같은 cursor를 반복해도
같은 조각이며 페이지가 바뀌면 `CURSOR_STALE`입니다. 조각이 JSON Lines 중간에서 끊길 수
있으므로 이어붙여 해석하세요. iframe은 자동화 대상이 아니며 해당 제한을 알립니다.

URL은 최종 목적지를 사용하되 userinfo/query 값/fragment를 마스킹합니다. 원문 URL이
그대로 필요하다는 이유로 비밀 query를 반환하지 않습니다. 이 마스킹 때문에 `page.url`은
원문 navigation input으로 다시 사용하기에 적합하지 않을 수 있습니다.

## 확인·제어

모든 클릭/입력/키/select/check는 보수적으로 confirmation_required입니다. 콘솔의
승인 기록 없이 token만 재전달하면 미실행입니다. 승인과 현재 페이지가 다르면
CONFIRMATION_STALE. 소비한 토큰은 CONFIRMATION_USED. 결과 불명은 RESULT_UNCERTAIN이며
그 세션의 다음 action도 수동 확인 전까지 차단합니다.

auth/handoff는 즉시 반환합니다. status에서 활성 제어권과 완료 결과를 확인합니다.
세션 전체 자동화를 잠그며 인증 중에는 탭 URL/제목도 수집하지 않습니다. 완료 후
`authenticated: null`, `verification: unverified`는 인증 성공/실패의 추측이 아닙니다.
사용자가 대상 탭을 닫으면 완료 결과에 TAB_NOT_FOUND가 들어갑니다.

## 명시적 제한

- private/local/file/javascript/data URL로의 자동 탐색은 지원하지 않습니다. HTTP(S),
  공개 DNS/IP, 80/443만 지원합니다. 새 빈 탭에 한해 내부적으로 about:blank를 사용합니다.
- GET으로 확인된 문서만 reload합니다. POST/알 수 없는 문서는 수동 제어를 요구합니다.
- navigation timeout은 성공으로 표시하지 않습니다. 후속 관찰로 확인해야 합니다.
- 외부 변경 여부가 불명확한 행동은 승인 대상이지만 범용 부작용 분석기는 아닙니다.
- 파일 업로드, iframe 자동 조작, WebMCP, passkey/보안 키 전달은 미구현입니다.
- 안전한 이미지 분리가 어려운 인증/iframe/알려진 token 화면은 전체 반환을 거부합니다.
- CAPTCHA/BOT 차단은 명확한 화면 문구를 증거로 구분합니다. 단순 HTTP 403/429나
  timeout만으로 BOT_BLOCKED를 반환하지 않습니다. 우회·자동 반복 새로고침은 없습니다.

## 추가 오류

원안 오류 외에 CURSOR_STALE, STALE_SCREENSHOT, SCREEN_CHANGED, SENSITIVE_SCREEN,
SENSITIVE_INPUT, NODE_NOT_ACTIONABLE, INVALID_COORDINATES, INVALID_INPUT,
OBSERVATION_FAILED, UNSUPPORTED_OPERATION, ENGINE_UNAVAILABLE, RESOURCE_PRESSURE, WORKER_TIMEOUT,
BROWSER_ERROR, CONFIRMATION_USED, AUTH_IN_PROGRESS, AUTH_ORIGIN_MISMATCH,
USER_CONTROL_ACTIVE, HANDOFF_UNAVAILABLE, HANDOFF_NOT_FOUND를 사용합니다.
관찰된 DOM 요소에 좌표 클릭을 시도하면 DOM_TARGET_AVAILABLE로 거부하고 node_id 사용을
요청합니다.
모두 실패를 성공처럼 숨기는 대신 나타내는 확장입니다.
