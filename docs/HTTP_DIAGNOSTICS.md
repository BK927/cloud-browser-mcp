# 선택적 HTTP 진단

`CB_HTTP_DIAGNOSTICS=false`가 기본값입니다. 운영자가 명시적으로 true로 설정해 시작한
public 앱에만 적용합니다. private 콘솔, 브라우저, OAuth 동작, 인증 검사, HEAD 처리,
포트·라우팅은 바꾸지 않습니다. 종료 후 false로 복원하는 배포 판단도 운영자가 합니다.

기존 `uvicorn log_level=warning`, `access_log=false`를 유지하면서 별도 독립 logger가
stderr에 JSON 한 줄씩 출력합니다. 공개 앱의 PublicGuard 바깥에서 관찰하므로 인증/Host
거부와 요청 본문 대기 전의 도착도 구분할 수 있습니다. TCP/TLS/HTTP 헤더 파싱 이전은
ASGI 앱에서 볼 수 없으므로 이 로그로 OpenAI 측 접속 실패 원인을 단정하지 않습니다.

필드는 UTC `ts`, 서버가 새로 만든 12자리 상관 ID `request_id`, 고정된 `event`,
허용 목록의 `method`와 정확한 정적 `path`, `status`(없으면 null), `elapsed_ms`뿐입니다.
상관 ID는 사용자 데이터의 토큰/해시가 아니며 응답 헤더에도 추가하지 않습니다.

- `request_received`: ASGI 호출 도착. 본문을 먼저 읽지 않습니다.
- `response_started`: ASGI response.start의 send가 성공적으로 반환됨.
- `response_completed`: 최종 body(또는 선언된 trailers)의 send가 반환됨.
- `request_cancelled`: 앱/송신 과정이 취소됨. 취소를 삼키지 않습니다.
- `request_failed`: 앱/송신 실패 또는 최종 응답 없이 앱 반환. 예외 정보는 기록하지 않습니다.

완료 후 앱 정리 코드가 실패하면 completed 뒤 failed가 올 수 있습니다. send 성공은
클라이언트 수신이나 ChatGPT 처리 성공의 증거가 아닙니다. ASGI 서버는 start만 받은
시점에는 실제 네트워크 헤더 송신을 미룰 수 있습니다.

## 기록하지 않는 값과 상한

쿼리·원본/임의 path·모든 헤더·쿠키·Authorization·User-Agent·IP·body·폼 입력·암호·토큰·
사용자 데이터 해시·exception message/traceback/locals는 진단 레코드에 넣지 않습니다.
GET/POST/HEAD/OPTIONS/PUT/PATCH/DELETE 및 정확한 `/mcp`, `/mcp/`, `/authorize`, `/token`,
`/revoke`, 현재 OAuth/discovery 경로만 식별합니다. 나머지는 잘라 출력하지 않고 고정
`unknown`으로 바꿉니다. 정적 경로로 분류해도 query는 항상 제외합니다.

한 public 앱에서 최근 60초 동안 최대 120개 이벤트, 레코드 최대 512바이트,
elapsed 최대 86,400,000ms입니다. 속도 상한은 진단 출력만 생략하며 요청을 거부하거나
지연·재시도하지 않습니다. 메모리는 최대 120개의 시간 값만 유지합니다. 로그 기록 실패도
요청 응답이나 원래 예외를 바꾸지 않습니다. 상한/출력 실패로 중간·종료 로그가 빠질 수
있으므로, 로그 부재만으로 요청 미도착·서버 멈춤을 단정하지 마세요. 감사 로그가 아닙니다.

이 기능은 다른 라이브러리의 기존 로그 설정을 변경하지 않습니다. 원래 예외 전파와
응답 동작도 유지합니다. 일반 access/debug 로그를 켜거나 원시 로그·HAR·요청 헤더를
채팅에 전달하는 방식으로 진단을 확장하지 마세요.

근거: [ASGI HTTP 이벤트와 send 의미](https://asgi.readthedocs.io/en/stable/specs/www.html).
