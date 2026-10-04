# 외부 계약 0.4 초안

패키지의 배포 버전과 별개인 개발 중 외부 계약입니다. 기존 도구 이름은 유지하지만
0.4는 작업 격리를 위해 **세션 호출에 `lease_id`를 요구하는 호환성 변경**입니다.
클라이언트는 `tools/list`를 갱신하고 `browser_open`에서 받은 임대 ID를 보관해야 합니다.
관찰 문자열의 배치나 고정된 JSON 키 순서를 가정해서는 안 됩니다.

원래 8개 도구 이름을 유지하고 status/configure, 선택 WebMCP 2개 및
wait/dialog/logs/artifacts/clipboard 및 공개 읽기 browser_read를 더해 18개 도구를 제공합니다.
MCP SDK의 `tools/list` 입력 schema가 정확한 타입·범위의 기준입니다.

모든 정상 도구 실행 결과는 `status`, `request_id`, `session_id`, `tab_id`, `revision`,
`page`, `notices`, `error`를 갖습니다. 문맥이 없으면 null입니다. JSON은
`structuredContent`와 text content에 담고 이미지 바이트는 별도 MCP image content로
반환합니다. JSON의 이미지 설명에만 base64를 넣는 방식이 아닙니다.

18개 도구 모두 `tools/list`에 `outputSchema`를 선언합니다. 공통 응답과 함께 임대 ID,
관찰 커서·스크린샷 ID, 승인·작업 상태, 파일 핸들 등 도구별 결과 구조를 설명합니다.
오류·사용자 제어 요청에서는 도구별 성공 필드가 생략될 수 있으며 기존 null과 이미지
content는 유지합니다. 페이지 제공 도구의 임의 JSON과 확장 메타데이터도 보존합니다.
서버 업데이트 후 연결된 앱의 도구 목록을 다시 갱신해야 새 출력 스키마가 반영됩니다.

status: `ok`, `no_change`, `confirmation_required`, `user_action_required`, `blocked`, `error`.
JSON/schema 자체가 잘못된 요청은 SDK 단계의 표준 MCP 오류이며 브라우저 실행 전에
거부됩니다. `request_id`는 서버 추적 ID이고 idempotency key가 아닙니다.

탐색 결과와 관찰이 완료된 `action_result`는 `navigation_occurred` 외에
`url_changed`, `document_changed`, `navigation_kind`를 반환합니다.
`navigation_kind`는 `none`, `full_document`, `same_document`, `reload`입니다.
URL이 같더라도 문서가 교체되면 탐색이며, `reload`는 명시적 reload 요청에 사용합니다.
클릭이 같은 주소를 다시 불러왔지만 원인을 확정할 수 없으면 `full_document`입니다.
같은 문서 이동의 `same_document_kind`는 Chromium 이벤트로 확인한 경우에만
`hash`/`history_api`/`other`이며, 이벤트 근거가 없으면 null입니다.
문서가 바뀌지 않았다는 사실만으로 사이트의 모든 JS 상태 보존을 보장하지 않습니다.
`browser_status`는 계속 캐시를 사용하지만 탭 종료 결과의 새 선택 탭도 즉시 반영합니다.

역할·이름·레이블 조회는 결과 개수 제한 **이전**에 후보를 필터링하며 제목 등
비조작 요소도 조회할 수 있습니다. role은 정확히 일치해야 하고 name/label은
공백 정규화·대소문자 무시 부분 일치입니다. Chromium AX 보강은 제한된 개수만
수행하며 나머지는 native DOM 역할·레이블을 사용하고 `dom-fallback`으로 표시합니다.
open/nested Shadow DOM과 slot의 composed tree를 예산 안에서 탐색합니다.
완전한 ARIA 이름 계산이나 closed Shadow DOM 지원은 보장하지 않습니다.
쿼리 검사 예산을 초과하면 `query_scan_truncated`와 `interactive_truncated`가 true입니다.
`wait`의 present/absent는 숨겨진 요소를 포함한 DOM 존재 여부이고,
visible/hidden은 렌더링 여부(뷰포트 밖도 포함), enabled는 렌더링된 비활성 아님을 뜻합니다.
불완전한 조회로 부재나 숨김을 성공이라고 판정하지 않습니다.
단, `FRAME_NOT_VISIBLE`인 숨김·면적 0 프레임은 absent/hidden의 불완전 판정에서 제외합니다.
보호·예산 초과·검사 실패 등 다른 이유로 읽지 못한 프레임은 계속 불완전으로 취급합니다.
시간 제한은 조건 반복 대기의 한도이며 마지막 브라우저 관찰 호출 시간은 추가될 수 있습니다.

## browser_read

공개 페이지 읽기에는 `browser_read`를 우선 사용합니다. 절대 http(s) `url` 또는 이전
결과의 `read_id` 중 정확히 하나를 지정합니다. `offset`은 기본 0, `max_chars`는 기본
20000(1000..100000), `selector`는 본문 범위를 정하는 선택적 CSS 선택자입니다.
응답의 `page`는 URL·제목, `read`는 본문·링크·완료 여부와 생략·제한 플래그를 담습니다.
`session_id`, `tab_id`, `revision`은 null이며 임대·승인이 필요하지 않습니다.
`images`는 기본 true이며 이미지가 많은 페이지에서 메모리를 절약하려면 false로 지정합니다.
`video`(영상·오디오)와 `fonts`(웹 폰트)는 기본 false이며 필요할 때만 true로 켭니다.
`read.loaded`에 실제 활성화된 유형을 이름순으로 반환합니다. 생략 유형은
reader 페이지 타깃의 CDP Fetch에서 요청 단계에 `BlockedByClient`로 차단하며,
`read.blocked_requests`는 이번 읽기에서 차단한 요청 수입니다. 문서·iframe·스크립트·
스타일시트·XHR/Fetch·WebSocket은 계속 로드합니다. 선택은 탐색 전에 매번 갱신하고,
새 탭·팝업은 관리 탭으로 발견할 때 적용합니다(발견 전 요청은 차단되지 않을 수 있습니다).
별도 타깃인 out-of-process iframe의 리소스는 차단 범위 밖이며 세션 도구는 차단하지 않습니다.

`screenshot`도 기본 false입니다. true이면 본문 관찰 뒤 viewport 이미지를 실제 MCP 이미지
콘텐츠로 반환합니다(기본 JPEG, 마스킹 시 PNG). images·fonts를 자동으로 켜며(video는
지정값 유지), 이때 false였던
선택을 바꾸면 notice를 반환합니다. `browser_observe`와 같은 캡처 입장 검사·마스킹·
`SENSITIVE_SCREEN` 보호를 적용하며, 캡처 거절·자원 부족 시 본문은 그대로 반환하고
`read.screenshot_omitted`에 code와 가능한 reason을 둡니다.
긴 본문은 `read_id`와 `next_offset`으로 이어 읽습니다. 캐시는 호출자별 최대 4개,
10분 유효하며 브라우저나 페이지 변경에 영향을 받지 않습니다. 링크는 첫 조각에만
최대 200개 반환하며 인증 URL 값은 가립니다. 만료·다른 호출자 ID는 `READ_NOT_FOUND`입니다.
`read_id`로 받는 캐시 조각은 screenshot=true여도 이미지를 반환하지 않으며 로딩 선택도
변경하지 않습니다. `loaded`와 `blocked_requests`는 원래 읽기의 값을 유지합니다.

서버가 공개 읽기 전용 브라우저 하나를 필요할 때 시작합니다. 로그인하지 않는
`profiles/reader` 프로필을 유지하고 기본 300초 유휴 후 브라우저만 닫습니다.
실행 중에는 세션 한도를 차지하며 세션 도구·로그인·수동 제어·업로드로 접근할 수 없습니다.
기본 탐색 예산은 25초, 본문 캐시는 60000자이며 `CB_READER_*` 설정으로 조절합니다.
`interactive` 준비 상태와 안정화 후 읽고 탐색 시간 초과 시 `complete=false`로 부분 본문을
반환합니다. 기존 페이지 보호와 전역 사용자 제어 일시정지는 그대로 적용됩니다.
`CB_READER_ENABLED=false`이면 도구를 등록하지 않습니다.

## 입력

| 도구 | 입력 |
|---|---|
| open | session_id?, url?, new_tab=true, lease_id?, timeout_ms? |
| read | url? 또는 read_id? (정확히 하나), offset=0, max_chars=20000, selector?, images=true, video=false, fonts=false, screenshot=false |
| list_tabs | session_id |
| navigate | session_id, tab_id, operation=goto/back/forward/reload, url?, timeout_ms?, operation_id? |
| observe | session_id, tab_id, mode=auto/semantic/interactive/visual, full_page=false, max_chars?, cursor?, query? |
| act | session_id, tab_id, expected_revision, action, confirmation_token?, completion?, completion_timeout_ms?, follow_up? |
| auth_request | session_id, tab_id, site_origin |
| handoff | session_id, tab_id, reason |
| close | session_id, scope=tab/session, tab_id? |
| status | session_id?, lease_id?, operation_id? (임대 미제공 시 사용 중 여부·자원만) |
| configure | session_id, tab_id, configuration |
| list_page_tools | session_id, tab_id |
| call_page_tool | session_id, tab_id, revision, tool_name, arguments, confirmation_token? |
| wait | session_id, tab_id, condition, timeout_ms=5000 |
| dialog | session_id, tab_id, operation=get/accept/dismiss, dialog_id?, text?, confirmation_token? |
| logs | session_id, tab_id, after=0, limit=50 |
| artifacts | session_id, operation=list/get/delete/clear/export, artifact_id?, tab_id?, format=text/html/image |
| clipboard | session_id, operation=read/write/clear/copy/paste, text?, tab_id?, node_id?, expected_revision?, confirmation_token? |

위 표에서 `open`의 새 세션 생성과 전역 `status`를 제외한 모든 도구는 `lease_id`도
필수입니다. `act`의 선택적 `operation_id`는 재전송 결과 조회용이며 8..128자입니다.
서로 다른 작업은 기본 두 개까지 별도 세션·프로필·가상 화면으로 공존합니다.
실제 명령은 제한된 FIFO 대기열에서 한 번에 하나씩 실행합니다. 기존 작업의
URL·승인·파일은 다른 작업에 공개되지 않습니다. 운영자 작업 수 한도에 도달하면
`BROWSER_BUSY`와 `busy_reason: session_capacity`를 반환하며 기존 작업에 합류하지 않습니다.
같은 OAuth 연결이 같은 대화를 뜻하지 않습니다. 임대 ID를 다른 작업에 넘기지 마세요.
분실한 임대는 비공개 콘솔에서 해당 세션을 닫아 회수합니다.
초기 화면 확인 등으로 새 세션 열기가 부분 실패해도 서버에 세션이 남았다면 오류 응답에
해당 호출자만 사용할 `session_id`·`lease_id`를 반환합니다. 이를 보관해 상태 조회·정리에
사용하며 열기 성공으로 해석하지 않습니다. 만료 작업의 종료를 검증하지 못하면
`CLEANUP_REQUIRED`로 다음 작업을 막고 관리자의 비공개 정리를 요구합니다.

`status`의 탭은 `tabs_cached: true` 및 `tabs_observed_at`을 갖는 최근 확인 결과입니다.
최신 탭 목록이 필요하면 `list_tabs`를 사용합니다. 진행 중 행동이 있어도 `status`는
브라우저 IPC를 기다리지 않습니다. 같은 `operation_id`와 인자의 재전송은 완료 결과를
`replayed: true`로 반환하거나 진행 상태를 반환합니다. 다른 인자는 `OPERATION_CONFLICT`입니다.
결과 보관은 메모리 내 최대 128건이며 서버 재시작 후 복원하지 않습니다. 행동의 영속
중복 방지 기록은 별도로 유지합니다. 이미지 바이트는 결과 캐시에 보관하지 않습니다.
HTTP 취소는 이미 전달된 행동을 취소하지 않습니다. `RESULT_UNCERTAIN`은 재실행하지 않습니다.
작업이 만료되어도 캐시 상태는 `work_lease_expired: true`로 조회할 수 있으며, 인증·수동
제어가 진행 중인 세션의 수집 잠금은 만료만으로 풀리지 않습니다.
`CB_SESSION_TTL`은 유휴 시간입니다. 처리된 작업은 만료 시간을 갱신하지만 `status`
조회는 갱신하지 않습니다. 기본 15초 주기의 정리기가 만료 세션을 닫으며 실행·대기 중인
작업과 보호 중인 수동 제어는 중단하지 않습니다. 정리 완료 후에는 종료 사유를 반환합니다.
전역 `status.scheduler`는 작업 개수와 available/executing/session_capacity/
cleanup_pending/cleanup_required/user_control을 구분합니다. 다른 작업 식별자·내용은
포함하지 않습니다. `busy`만으로 실제 조작 중인지 또는 자원 부족인지 추론하지 마세요.
기본 대기 제한은 명령당 46초, 작업당 최대 네 요청입니다. 대기 초과는 실행 전
`BROWSER_BUSY`와 `busy_reason: queue_timeout`을 반환합니다. 15초 재시도 안내는
점유 만료 약속이 아닙니다. 새 세션 슬롯의 영속 예약 대기열은 제공하지 않습니다.

인증·수동 조작은 해당 작업의 독립 화면에서 수행하며 다른 작업의 다운로드를 취소하지
않습니다. 이 동안 새 자동 관찰·조작은 전체에서 일시 중지되고 상태 조회만 유지됩니다.
사용자 완료 후 재관찰하며, 작업 유휴 만료 때문에 완료 자체가 불가능해지지 않습니다.
여러 작업에서 수동 제어를 쓰려면 managed display가 필요합니다. 외부에서 관리하는
공유 화면에서는 명시적으로 `HANDOFF_UNAVAILABLE`을 반환합니다.
최초 open 응답을 잃어 임대 ID를 모르면 임대를 추측하거나 다른 작업에 공유하지 말고
비공개 콘솔에서 세션을 회수합니다. 승인 토큰을 추가한 재호출은 다른 인자이므로 새
`operation_id`를 사용합니다. 같은 전송의 재시도에만 기존 ID를 그대로 사용합니다.

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
추가합니다. 순차 입력·modifier·우클릭·중클릭·드래그·다중 선택과 추가 도구의 정확한
범위, 보안 제한 및 예시는 [확장 조작 계약](OPERATIONS.md)을 함께 적용합니다.

## revision·관찰

노드는 관찰한 실제 CDP backend ID에 연결합니다. 페이지 스크립트와 분리된 CDP isolated
world의 MutationObserver로 DOM 변경도 감지합니다. 현재 페이지에서 텍스트/선택자로
재검색하지 않습니다. 페이지 관찰 revision과 대상의 유효성은 별개입니다. 같은 문서에서
대상의 의미·입력 상태·소속 폼 데이터가 유지되면 광고 갱신 후에도 실제 backend 노드의
ID를 유지합니다. 대상 교체는 `STALE_NODE`, 이전 문서의 revision은 `STALE_REVISION`입니다.
페이지 스크롤은 같은 문서의 최근 256개 revision을 허용합니다. 커서는 여전히 관찰 revision에
묶입니다. 좌표는 화면 검사도 통과해야 하며 DOM 승인 거절의 우회 수단이 아닙니다.
마지막 탭을 닫으면 `session_closed: true`, `termination_reason: last_tab_closed`를 반환하며
후속 호출은 `SESSION_CLOSED`입니다. 정상 종료를 브라우저 크래시로 보고하지 않습니다.

발급 노드 저장소는 이번 조회 결과와 분리됩니다. 좁은 조회가 다른 살아 있는 노드 ID를
폐기하지 않으며, 행동 직전에 같은 backend를 isolated world에서 재검증합니다.
작업 전체 탭·프레임의 primitive 메타데이터는 기본 2MiB/1024건 LRU로 제한합니다.
`CB_NODE_REGISTRY_BYTES`는 직렬화 메타데이터 예산이지 프로세스 RSS 상한이 아닙니다.
예산으로 폐기된 ID는 `STALE_NODE`와 `reason: registry_evicted`이며 다른 요소로 대체하지 않습니다.
폐기 이유 이력도 최근 256개로 제한합니다. 이력 밖의 오래된 ID는 여전히 `STALE_NODE`지만
세부 이유는 `target_unavailable`일 수 있습니다.
로그인·수동 제어 전환은 부분 조회와 달리 해당 작업의 모든 이전 노드·좌표·커서를
무효화하고 승인 세대도 갱신합니다. 이미 실행한 행동의 중복 방지 기록은 지우지 않습니다.

semantic은 화면에 렌더링된 DOM을 문서 순서로 읽으며 main/article을 우선합니다.
제목·목록·표의 간단한 구조를 보존하고 메뉴/푸터와 접근성 트리의 중복을 줄입니다.
`semantic_source`는 선택한 본문 종류, `semantic_source_truncated`는 내부 수집 한도 도달을
표시합니다. 본문 내부 한도는 250000자/탐색 노드 10000개이며, 이 한도 초과 부분은 cursor로
복원되지 않습니다. 이는 원문 전체 보존이나 모든 웹사이트의 완벽한 본문 추출을 보장하지 않습니다.

메모리 압박에서도 `auto`는 최신의 짧은 본문과 요소를 함께 반환합니다(출력 최대 4000자,
본문 수집 8000자/1000 탐색 노드). 실제 캡처 직전 worker가 폭·높이와 현재 자원을 검사하며
이미지 거절은 `auto`의 텍스트를 버리지 않습니다. `screenshot_omitted`는 이유와 가능한
자원 상태를 표시합니다. 명시적 `visual` 요청은 정확한 오류를 반환합니다.
`observation_revision`은 텍스트 관찰 시점이며 선택적 캡처 검사 후 응답 revision은 더 최신일 수 있습니다.
촬영 중 revision이 바뀌면 이전 continuation은 반환하지 않고, 잘린 본문은
`pagination_stale=true`로 새 관찰이 필요함을 표시합니다.
`query_match_count`와 `query_empty_reason`은 일치 대상 수와 missing/hidden/protected/empty/scan_budget을
구분합니다. 내부 수집 제한과 wire 출력 잘림/커서는 서로 다른 제한입니다.

interactive는 현재 viewport의 조작 요소를 담은 **완전한 JSON Lines**입니다. 빈 기본값을
생략하고 좌표를 반올림하지만 실제 조작 대상은 기존 backend ID를 사용합니다. 최대 300개이며
viewport의 후보가 더 많으면 `interactive_truncated=true`입니다. 화면 밖 요소는 스크롤 후
관찰합니다. 일반 모드에서 폼 목적지/메서드 같은 내부 승인 메타데이터는 노출하지 않습니다.

관찰된 backend ID에 대해서만 Chromium 접근성 정보를 조회해 이름·역할·상태를 보완합니다.
관련 없는 AX 하위 트리나 입력값은 병합하지 않으며 보호된 입력 화면에서는 AX 조회를 하지
않습니다. 알려진 보호 영역이 있으면 AX 보강을 중단하고 개인정보를 제외한 DOM만 사용합니다.
`accessibility_source`는 `chromium-ax`, `dom-fallback`, `withheld`이며 DOM fallback은 경고도
반환합니다. 변경 없는 DOM에서는 AX 이름을 재사용합니다.

select 노드는 최대 200개 option의 label/value/selected/disabled를 제공합니다. 초과는
`options_truncated=true`입니다. 관찰하지 못했거나 비활성인 옵션을 선택하지 않고, 중복 value는
NODE_AMBIGUOUS입니다. readonly 입력·radio 직접 해제는 명시적으로 거부합니다.
다중 선택은 `select_multiple`로 관찰한 고유 value만 선택합니다.
중첩 option 메타데이터에도 토큰 제거를 적용합니다.

일반 overflow 스크롤 영역에도 node_id와 scrollable/scroll 정보를 부여합니다. 후보 탐색은
10000개 요소(압박 모드 1000개)로 제한하고 초과하면 `scroll_scan_truncated=true`입니다. 영역 내부 스크롤도
revision에 반영합니다. Shadow DOM의 완전한 탐색은 아직 보장하지 않습니다.

max_chars는 두 snapshot 문자열의 합산 예산입니다. auto는 조작 목록에 예산을 먼저
확보하므로 긴 본문 때문에 버튼이 전부 밀려나지 않습니다. 각 interactive 행은 독립적으로
JSON 파싱할 수 있습니다. 너무 긴 행은 설명 일부를 생략하고 `details_omitted=true`를
반환하지만 node_id는 유지합니다. 원래의 대상 메타데이터는 서버에서 그대로 검증합니다.
본문이 남아 있으면 첫 조작 행도 분할 예산에 맞춰 요약해, 긴 select 메타데이터 하나로
공개 본문이 전부 빠지지 않게 합니다. interactive-only 조회의 예산은 줄이지 않습니다.
`semantic_truncated`와 `interactive_page_truncated`는 각각 다음 페이지의 존재를 표시합니다.

cursor는 고정 snapshot·두 문자열의 위치·mode·예산·revision에 묶입니다. 같은 cursor를
반복하면 같은 내용이며 페이지가 바뀌면 `CURSOR_STALE`입니다. cursor 호출에서는 처음의
mode/예산을 유지하고 이미지는 다시 생성하지 않습니다.

`frames`는 불투명 frame_id, parent_frame_id, origin, readable/actionable, 제한 reason을
반환합니다. CDP로 연결 가능한 동일·교차 출처 iframe, frame/frameset과 중첩 프레임을
최대 16개·깊이 4(압박 모드 최대 4개)까지 검사합니다. 내부 노드도 실제 backend에
연결하며 frame_id가 있는 노드의 rect는 해당 프레임 viewport CSS 좌표입니다.
노드 조작은 부모 프레임 경계를 투영하고 가림을 검사합니다. 프레임 교체·이동·분리 시
이전 노드는 재검색하지 않습니다. 읽지 못한 프레임은 부분 결과와 이유를 반환합니다.
보호 폼 안의 프레임은 자체 입력 필드가 없더라도 `PROTECTED_PARENT`로 내용을 수집하지
않습니다. 읽기 불가가 된 프레임의 이전 노드와 범위 조회도 거절합니다. 프레임의 경계·가림은
실제 backend를 isolated world에서 검사하며 페이지의 전역 함수 변조를 사용하지 않습니다.

URL은 최종 목적지를 사용하되 userinfo와 비밀값을 마스킹합니다. 일반 검색·탐색 query 및
문서 fragment는 보존하고 알 수 없는 query 값과 인증 fragment는 가립니다. `page.url`은
원문 navigation input으로 다시 사용하기에 적합하지 않을 수 있습니다.

## 확인·제어

기본 `CB_APPROVAL_POLICY=strict`에서는 모든 클릭/입력/키/select/check가
confirmation_required입니다. 운영자 선택 `balanced`에서는 일반 HTTP(S) 링크,
식별된 펼침/접기·탭, 일반 비민감 입력·선택·체크·편집 키, 구조화 검색과 GET 검색,
Tab/Escape를 자동 허용합니다. 실제 전송·구매·삭제·권한 변경·업로드·불명확한 실행은
계속 승인 대상입니다. 좌표가 관찰한 DOM 대상과 일치하면 같은 요소 정책을 적용합니다.
자동 허용 및 승인 응답에는 `action_policy`의 모드·판정 이유가 포함됩니다.
세부 범위와 스크립트 부작용의 한계는 [승인 정책](APPROVAL_POLICY.md)을 참고하세요. 콘솔의
승인 기록 없이 token만 재전달하면 미실행입니다. 승인과 현재 페이지가 다르면
CONFIRMATION_STALE. 소비한 토큰은 CONFIRMATION_USED. 거절한 토큰은 CONFIRMATION_DENIED이며
자동 재요청하지 않습니다. 결과 불명은 RESULT_UNCERTAIN이며 그 세션의 다음 action도
수동 확인 전까지 차단합니다. `balanced`는 알려진 외부 변경을 허용하는 모드가 아니며,
페이지 JavaScript의 부작용을 완벽히 증명하는 보안 경계도 아닙니다.
입력 전달 전 실패는 `action_result.performed=false`인 일반 오류이며 세션을 잠그지 않습니다.
이 경우에만 해당 실행 기록을 지우고 소비한 승인을 남은 유효 시간으로 복원합니다.
이미 전달했거나 전달 여부가 불명확하면 승인·중복 방지 기록을 복원하지 않습니다.

동일 문서·프레임·대상·행동·전송 데이터의 미완료 승인 요청은 하나로 합칩니다.
사용자 승인 시점부터 `CB_APPROVAL_TTL`의 새 유효 시간을 부여하며 `expires_at`도 갱신합니다.
토큰 없이 같은 요청을 보내면 `approval_state: pending/approved`로 현재 상태를 알립니다.
approved라도 실행하려면 만료 전 동일 인자에 `confirmation_token`을 추가해야 합니다.
비공개 콘솔의 목록 페이지는 10초마다 자동으로 갱신합니다.
무관한 페이지 갱신은 허용하되 대상·폼·경로가 달라지면 승인을 폐기합니다. 실행 전 결합의 소비
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
  passkey/보안 키 전달은 제공하지 않습니다.
- 명시적 인증 중에는 DOM·이미지·로그 수집을 중단합니다. 일반 페이지의 알려진 민감 필드와
  소유 폼은 hidden 여부와 관계없이 값·노드·본문·폼 digest에서 제외하고 공개 영역을 읽습니다.
  `protected_regions_omitted`가 이 분리를 표시합니다. 보수적인 보호 경계가 안전하면 이미지에서
  가리고, 경계·합성이 불확실하면 이미지를 거절합니다. 보호 대상·폼 제출·현재 보호 영역의
  좌표 조작은 거절합니다. 보호 영역이 있는 페이지의 subtree clipboard copy도 거절합니다.
  DOM 20000개/입력 1000개 보안 검사 상한을 넘으면 `PRIVACY_INSPECTION_INCOMPLETE`이며
  이를 실제 로그인 요구로 가장하지 않습니다. 임의 HTML/canvas에 복제된 비밀값까지 찾지는 못합니다.
- 알려진 token 화면은 이미지 반환을 거부합니다. 기본 `CB_IFRAME_SCREENSHOT_POLICY=inspect`는
  검사 가능한 일반 프레임을 표시하고 민감·검사 불가 영역만 마스킹합니다.
- 운영자 설정 `mask`는 모든 iframe/object/embed 영역을, `block`은 전체 프레임 캡처를
  차단합니다. 마스킹 위치에 transform/filter 등 지원하지 않는 합성이 있으면 캡처를
  거절합니다. 안전하게 위치를 확인한 full_page 마스킹은 허용하며 가린 영역 조작은 거부합니다.
  일반 캡처는 JPEG이고, 마스킹은 시각 정보를 제거하는 제한적 선택 기능이지 임의 비밀값 탐지나
  사이트 스크립트의 정보 유출 방지를 보장하지 않습니다. MCP configure로 정책을 완화할 수 없습니다.
- 숨김·현재 캡처 밖 일반 프레임은 inventory에 남겨도 캡처를 불필요하게 막지 않습니다.
  위험한 합성이 캡처 안으로 그릴 가능성을 배제할 수 없으면 여전히 거절합니다.
- CAPTCHA/BOT 차단은 보이는 도전 컨트롤·제공자 프레임·차단 패널과 화면 맥락으로 구분합니다.
  기사에서 관련 문구를 인용하는 것만으로 차단하지 않습니다. 단순 HTTP 403/429나
  timeout만으로 BOT_BLOCKED를 반환하지 않습니다. 우회·자동 반복 새로고침은 없습니다.

## 추가 오류

오류 envelope의 `error`는 `code`, `message` 외에 `category`, `retryable`,
`suggested_tool`, `next_step`을 제공합니다. `next_step`은 페이지 데이터가 없는 고정된
짧은 영어 안내이며, `retryable`이 true여도 새 관찰·상태 확인 등 안내된 선행 조건을
따라야 합니다. `privacy_guard`와 `page_limit`은 로컬 안전 제한으로, 사이트의 차단이나
보안상 사용자 요청 거부를 뜻하지 않습니다.

| category | 오류 코드 |
| --- | --- |
| `stale_state` | STALE_NODE, STALE_REVISION, STALE_SCREENSHOT, CURSOR_STALE, FRAME_STALE, SCREEN_CHANGED, DOM_TARGET_AVAILABLE |
| `capacity` | BROWSER_BUSY, RESOURCE_PRESSURE, CAPTURE_TIMEOUT, WORKER_TIMEOUT, CLEANUP_REQUIRED |
| `approval` | CONFIRMATION_REQUIRED, CONFIRMATION_STALE, CONFIRMATION_USED, CONFIRMATION_DENIED, ACTION_ALREADY_DISPATCHED |
| `navigation` | NAVIGATION_TIMEOUT, NAVIGATION_FAILED, NAVIGATION_CANCELLED, NAVIGATION_IN_PROGRESS |
| `target` | NODE_NOT_ACTIONABLE, NODE_AMBIGUOUS, NODE_NOT_FOUND, TAB_NOT_FOUND, FRAME_UNAVAILABLE, ACTION_GOAL_NOT_MET |
| `input` | INVALID_INPUT, INVALID_URL, INVALID_SELECTOR, INVALID_COORDINATES, READ_NOT_FOUND, LEASE_REQUIRED, LEASE_INVALID, OPERATION_CONFLICT, UNSUPPORTED_OPERATION |
| `page_limit` | PRIVACY_INSPECTION_INCOMPLETE |
| `privacy_guard` | SENSITIVE_SCREEN, SENSITIVE_INPUT, SENSITIVE_TARGET, SENSITIVE_CONTENT, POLICY_BLOCKED |
| `site_challenge` | CAPTCHA_REQUIRED, BOT_BLOCKED, AUTH_REQUIRED |
| `human_control` | USER_CONTROL_ACTIVE, AUTH_IN_PROGRESS, HANDOFF_UNAVAILABLE |
| `session` | SESSION_EXPIRED, SESSION_NOT_FOUND, SESSION_CLOSED |
| `uncertain` | RESULT_UNCERTAIN |
| `browser` | 위에 없는 오류 코드 |

재시도 가능 표시는 모든 `stale_state` 코드와 BROWSER_BUSY, RESOURCE_PRESSURE,
CAPTURE_TIMEOUT, CONFIRMATION_STALE에만 true입니다. RESULT_UNCERTAIN,
ACTION_ALREADY_DISPATCHED, CONFIRMATION_USED/DENIED는 계속 false이며 이미 전달된
행동을 반복하지 않습니다. stale/target/navigation 및 CONFIRMATION_STALE,
ACTION_ALREADY_DISPATCHED, CONFIRMATION_USED는 `browser_observe`, READ_NOT_FOUND는
`browser_read`, session과 LEASE_REQUIRED는 `browser_open`, capacity는 `browser_status`를
안내합니다. RESULT_UNCERTAIN, CAPTCHA_REQUIRED, BOT_BLOCKED,
PRIVACY_INSPECTION_INCOMPLETE의 `browser_handoff` 안내는 운영자 수동 제어가 활성화되고
해당 도구가 등록된 경우에만 제공합니다. 그 밖에는 RESULT_UNCERTAIN에 `browser_close`,
나머지 세 코드에 null을 반환합니다. AUTH_REQUIRED의 `browser_auth_request`와
SENSITIVE_SCREEN의 `browser_observe` 등 기존 안내는 유지됩니다.

`reason`은 기존 STALE_NODE와 같이 응답 최상위 오류 details에 둡니다. 선택적 캡처가
생략되면 `observation.screenshot_omitted.reason`에 둡니다. 원문 URL·호스트·본문·선택자를
넣지 않고 다음 고정값만 사용합니다.

- INVALID_URL: `scheme`(미지원 스킴/URL 구문), `credentials`(userinfo), `port`(미지원/잘못된 포트),
  `private_address`(비공개·미지원 IP 또는 DNS 결과), `unresolved`(DNS 확인 실패),
  `egress_rejected`(egress 정책 거절), `missing`(URL/호스트 누락).
- SENSITIVE_SCREEN: `secret_text`(알려진 비밀 문자열), `iframe_policy`(프레임 정책/가려진 영역),
  `privacy_incomplete`(검사 미완료), `mask_unsafe`(안전한 마스킹 불가),
  `mask_unbounded`(경계를 확정할 수 없음), `too_many_regions`(마스킹 영역 수 초과).

PRIVACY_INSPECTION_INCOMPLETE는 이제 `blocked` 대신 `error`이며 메시지는
`Page exceeds the bounded privacy scan (element/input budget); size limit, not a detected secret`입니다.
요소/입력 검사 예산을 넘긴 관찰·행동의 거절 동작은 유지되며 실제 비밀값 발견을 의미하지 않습니다.

`browser_status.recent_errors`는 호출 principal의 최근 오류 요약 최대 20건을 시간 순서로
반환합니다. 세션/lease 없는 조회와 소유 세션/lease 조회 모두 제공하며, 각 항목은
`{tool, code, category, request_id, at}`입니다. `tool`은 MCP 메서드명, `at`은 UTC ISO 시각입니다.
페이지·입력 데이터는 없고 메모리에만 보관하며 종료 시 지웁니다. 전체 principal 저장소도
최근 사용한 최대 128개로 제한합니다. 세션/lease 없는 status의 `approvals`에는 호출 principal이
소유한 세션들의 승인 요약만 반환합니다. 세션별 조회와 동일한 summary/state/expires_at 및
존재할 경우 approval_state를 제공하고, 승인 토큰과 입력값은 반환하지 않습니다.

기존 용량 이벤트는 `browser_capacity` 형식을 유지합니다. 그 밖의 오류는
`{"event":"browser_error","request_id":"req_…","code":"…","category":"…"}`로 기록하며,
두 종류가 분당 120건 예산과 request_id 검증을 공유합니다. 메시지·URL·호스트·선택자·details는
기록하지 않습니다. 선택적 캡처/완료 확인 오류도 외부 응답 request_id로 연결하고, 같은
request_id/code의 작업 결과 재조회는 제한된 최근 중복 이력 안에서 다시 기록하지 않습니다.

탐색 호출별 `timeout_ms`와 탭 설정 `navigation_timeout_ms`를 지원합니다.
우선순위는 호출 → 탭 → 운영자 기본값(새 설치 60000ms)이며 운영자 상한은 기본
300000ms입니다. 탐색의 첫 응답은 최대 5초를 기다리고, 미완료이면 `no_change`와
`navigation.pending=true`, 서버 `operation_id`, `session_id`·`lease_id`를 반환합니다.
초기 브라우저 준비 중에는 `tab_id=null`일 수 있습니다. `browser_status`의 소유 작업
진행 상태/완료 결과에서 실제 탭을 얻어야 하며 pending을 로딩 성공으로 해석하면 안 됩니다.
타임아웃은 `NAVIGATION_TIMEOUT`과 실패 단계, 확인된 `current_page`를 별도로 반환합니다.
동일한 `browser_navigate.operation_id`는 동일 입력의 진행·완료 결과를 재조회하며
다른 입력으로 재사용하면 `OPERATION_CONFLICT`입니다. 동시 추가 탐색은
`NAVIGATION_IN_PROGRESS`, 명시적 정리는 `NAVIGATION_CANCELLED`로 구분합니다.

이미지는 고정된 `capture_reasons`와 `capture_attempts`를 반환할 수 있습니다.
확인된 표시 변화만 200ms 후 한 번 다시 촬영하며 총 처리 예산은 15초입니다.
문서·보호 영역·보호 변경 이력·마스킹 불확실·자원 부족에는 재촬영하지 않습니다.
타임아웃은 `CAPTURE_TIMEOUT`입니다. 자세한 진행/촬영 규칙은
[운영 계약](OPERATIONS.md#bounded-asynchronous-navigation)에 있습니다.

원안 오류 외에 CURSOR_STALE, STALE_SCREENSHOT, SCREEN_CHANGED, SENSITIVE_SCREEN,
SENSITIVE_INPUT, NODE_NOT_ACTIONABLE, INVALID_COORDINATES, INVALID_INPUT,
OBSERVATION_FAILED, UNSUPPORTED_OPERATION, ENGINE_UNAVAILABLE, RESOURCE_PRESSURE, WORKER_TIMEOUT,
BROWSER_ERROR, CONFIRMATION_USED, CONFIRMATION_DENIED, AUTH_IN_PROGRESS, AUTH_ORIGIN_MISMATCH,
USER_CONTROL_ACTIVE, HANDOFF_UNAVAILABLE, HANDOFF_NOT_FOUND를 사용합니다.
ACTION_GOAL_NOT_MET, SENSITIVE_TARGET, PRIVACY_INSPECTION_INCOMPLETE도 사용합니다.
입력·선택·체크는 같은 backend의 실제 목표 값을 확인하고 `target_state_verified`를 반환합니다.
알려진 불일치는 ACTION_GOAL_NOT_MET, 확인 불가는 RESULT_UNCERTAIN이며 이미 전달한 행동을
자동 재실행하지 않습니다. 명시적 completion 실패도 `ok`로 표시하지 않습니다.
completion의 CSS 선택자는 입력 전달 전 검증합니다. 완료 관찰 중 일시적인 페이지 변경 오류는
남은 제한 시간 안에서 재관찰하며 행동은 반복하지 않습니다. 확정된 미충족은
ACTION_GOAL_NOT_MET, 불완전·확인 불가는 RESULT_UNCERTAIN과 세션 잠금으로 반환합니다.
디시 게시글 HTTPS 경로의 숫자형 `no`는 공개 식별자로 보존하며 그 밖의 비밀성 URL 값은 제거합니다.
CONTROL_DISCONNECT_FAILED는 원격 제어 연결을 안전하게 끊지 못한 경우입니다.
좌표는 문서·viewport·스크롤·적중 요소·가림·의미를 재검증합니다. DOM 대상이 없으면
엄격한 픽셀 일치 검사를 유지합니다. 일반 이미지 관찰은 동영상 픽셀 변화만으로 실패하지
않으며 `captured_at`을 반환합니다. 가능한 경우 항상 node_id 조작을 우선합니다.
모두 실패를 성공처럼 숨기는 대신 나타내는 확장입니다.

0.3의 폼 값 결합, iframe 의미 읽기, 인증 규칙·실패 보고, 업로드·WebMCP 계약은
[확장 계약](CONTRACT_EXTENSIONS.md)을 함께 적용합니다. 기본 인증 규칙이 없으면 앞서 설명한
unverified 상태이며, 운영자 규칙을 설정한 경우에만 근거가 확인된 인증 결과를 제공합니다.
