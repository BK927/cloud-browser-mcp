# Obscura v0.2.3 도입 전 검증 — 안전성 조건 실패로 통합 보류

## 결론과 변경 범위

**Obscura를 서비스 엔진으로 제공하지 않는다.** 계획의 선행 안전성 시험에서
페이지가 조작한 위치·클릭 대상을 CDP가 그대로 사용하는 문제가 재현됐다.
이 결과는 Obscura의 모든 용도가 위험하다는 판단이 아니라, 이 MCP의 신뢰할 수 없는
페이지 관찰·민감 영역 마스킹·행동 대상 검증에 필요한 경계를 확보하지 못했다는 뜻이다.

사용자가 정한 “어댑터와 격리만으로 조건을 만족하지 못하면 전용 엔진 수정본을 만들지
않고 차단 원인과 재현 시험을 보고”하는 중단 조건을 적용했다.
안전하지 않은 어댑터나 로그인 UI를 만들어 지원 기능으로 광고하지 않는다.

- 기준 소스: `ac0a2c4ea1d7b912a9717c84b8efd1016f581789`.
- 실험 브랜치: `codex/obscura-engine-experiment`.
- 변경: 고정 릴리스 명세, 합성 페이지, 재현 도구, 회귀 시험, 별도 CI, 이 보고서.
- `src/`, OAuth, 작업 임대, 승인, MCP 출력 스키마, Docker/네이티브 운영 경로는 변경하지 않았다.
- **기본 DrissionPage 4.1.1.4 유지.** 베타 설치·기존 의존성 재설치·main 병합 없음.
- `CB_BROWSER_ENGINE=obscura`는 구현/지원하지 않는다. 설정해도 실험 엔진이 되는 것이 아니다.

## 검증 대상과 방법

[공식 v0.2.3 릴리스](https://github.com/h4ckf0r0day/obscura/releases/tag/v0.2.3),
소스 커밋 `1a3169da276d7720732c7b20535474942917fb83`을 사용했다.
렌더링 포함 Windows amd64 배포 압축 파일의 SHA256을 확인한 뒤 원본 그대로 실행했다.
Linux amd64/arm64 압축 파일 해시도 [고정 명세](../experiments/obscura/releases.json)에 기록했다.
CDP의 `Browser.getVersion`은 Obscura에서도 `Chrome/145.0.0.0`을 반환하므로
이 문자열을 실제 엔진 식별/릴리스 검증으로 사용하지 않는다.

전용 임시 프로세스·빈 저장소·루프백 CDP·합성 `data:` 페이지만 사용했다.
실제 사이트·계정·비밀번호·OAuth DB·기존 브라우저 프로필에는 접근하지 않았다.
파일 접근/내부망 접근 허용 옵션, stealth, Chromium `--no-sandbox`를 사용하지 않았다.
이 로컬 시험은 Linux namespace/egress/cgroup 격리를 검증한 것이 아니다.

대조군은 동일 스크립트로 실행한 Chrome 153.0.8010.48이다. 직접 CDP를 사용하므로
이번 결과 자체를 DrissionPage 어댑터 전체의 기능 검증으로 해석하지 않는다.

## 실측 결과

| 선행 검사 | Obscura v0.2.3 | Chromium 대조군 |
|---|---|---|
| 1024×768 렌더링과 세 영역의 실제 픽셀 | 통과 | 통과 |
| native DOM 속성과 전체 AX 트리 조회 | 통과 | 통과 |
| 정상 아코디언 클릭의 `aria-expanded` 변경 | 통과 | 통과 |
| named isolated world에서 페이지 전역 분리 | 실패: 페이지 marker가 보임 | 통과 |
| 페이지의 메서드 덮어쓰기에 영향받지 않는 요소 위치 | 실패 | 통과 |
| DOMSnapshot 위치와 실제 화면 일치 | 실패 | 통과 |
| native 좌표 적중 요소 조회 | 미지원 오류 | 통과 |
| 페이지 `elementFromPoint` 덮어쓰기에도 같은 버튼 클릭 | 실패: 다른 버튼 클릭 | 통과 |

핵심 재현 두 가지:

1. 합성 OTP 입력창은 화면 `(40,180)–(240,220)`에 있다. 페이지가 해당 요소의
   `getBoundingClientRect`를 덮어쓰자 `DOM.getBoxModel`과 `DOM.getContentQuads`가
   `(700,600)–(710,610)`을 반환했다. **변조 전후 캡처 픽셀은 완전히 동일**했다.
   이 위치를 신뢰해 민감 영역을 가리면 엉뚱한 곳을 가리게 된다.
   실제 자격증명 유출 시험을 한 것은 아니다.
2. 정상 버튼 좌표 `(50,50)`의 클릭은 처음에는 정상 동작했다. 페이지에서
   `document.elementFromPoint`가 다른 버튼을 반환하게 변경한 뒤 같은 CDP 입력을 보내면
   정상 버튼은 클릭되지 않고 다른 버튼의 클릭 처리기가 실행됐다.
   결과는 페이지 JavaScript의 보고가 아니라 native DOM의 변경된 속성으로 확인했다.

전체 raw 결과는 [실험 결과 폴더](../experiments/obscura/results/)와 해당 커밋의 CI
`obscura-blocked-synthetic-evidence` 아티팩트에 둔다. 결과의 `admitted`는 **이 소규모 선행
검사만** 의미한다. `true`여도 서비스 전체 지원, 보안 감사 완료, Pi 성능 검증을 뜻하지 않는다.
CI에서 Obscura 실패를 예상하는 테스트가 녹색인 것은 **차단 사유 재현 성공**이지 지원 성공이 아니다.

## 대체 경로 검토와 채택하지 않은 우회

- [DOM 구현](https://github.com/h4ckf0r0day/obscura/blob/v0.2.3/crates/obscura-cdp/src/domains/dom.rs):
  numeric node로 DOM을 읽을 수 있지만 위치 조회는 페이지의 `_wrap`와
  `getBoundingClientRect`에 의존한다. `getContentQuads`도 같은 경로다.
- [DOMSnapshot 구현](https://github.com/h4ckf0r0day/obscura/blob/v0.2.3/crates/obscura-cdp/src/domains/domsnapshot.rs):
  실제 렌더 레이아웃 대신 합성 위치를 반환한다. 시험 입력창의 반환 위치는
  `[0,342,1280,18]`이었다. 렌더 빌드를 사용해도 마스킹 위치의 대안이 되지 않았다.
- [Runtime 구현](https://github.com/h4ckf0r0day/obscura/blob/v0.2.3/crates/obscura-cdp/src/domains/runtime.rs):
  named isolated context는 소유권/라우팅만 검증하고 같은 페이지 V8 전역에서 실행한다.
  기존 Chromium 관찰 스크립트를 옮겨 붙여 안전해졌다고 주장할 수 없다.
- [Input 구현](https://github.com/h4ckf0r0day/obscura/blob/v0.2.3/crates/obscura-cdp/src/domains/input.rs):
  native 적중 검사 대신 페이지의 `document.elementFromPoint` 등으로 이벤트 대상을 정한다.
- [CDP dispatch](https://github.com/h4ckf0r0day/obscura/blob/v0.2.3/crates/obscura-cdp/src/dispatch.rs):
  CSS 도메인은 빈 성공 응답으로 처리돼 별도의 신뢰 가능한 CSS/위치 조회 경로로 쓸 수 없다.
- 전체 AX/DOM은 텍스트 관찰의 재료가 되지만 안전한 위치/입력 경계를 대신하지 않는다.
  격리 namespace는 호스트 파일과 다른 작업을 보호할 수 있어도 이 페이지 내부 혼동을 고치지 않는다.
- 페이지 전역/프로토타입 동결, 임의 스크립트 제거, 전 화면 마스킹이나 JavaScript 비활성화는
  일반 사이트·로그인·수동 조작 호환성을 보장하는 해결책으로 검증되지 않았다.
  안전 경계 대신 사용하지 않았다. 모든 어댑터 방식의 수학적 불가능성을 주장하는 것은 아니다.

## 지원표와 미완료 항목

| 항목 | 이번 결과 |
|---|---|
| 기존 Drission 엔진, VNC 로그인/handoff | 유지; 코드 변경 없음 |
| Obscura 릴리스/플랫폼 해시 고정 | 구현 |
| 원본 바이너리 검증·합성 페이지 재현 | 구현 |
| 실험용 Obscura 운영 어댑터/engine 선택/capability | 안전성 선행 조건 실패로 미제공 |
| Obscura 로그인/OTP/팝업/한글 조합/수동 제어 UI | 통합하지 않음, 미검증 |
| 노드·프레임 수명/민감 iframe/lease/승인 통합 | Obscura에서는 미검증 |
| Obscura Docker/네이티브 운영 설치 | 제공하지 않음; 기존 설치 경로 유지 |
| Linux namespace·파일/내부망 격리 | Obscura 운영 통합 미검증 |
| Pi 3회 교대 성능·CPU/PSS/USS/swap/OOM 비교 | 미실행, 절감량/성공률 주장 없음 |
| W3C/GitHub/YouTube, 실제 계정, 웹 ChatGPT 연결 | Obscura는 최종 연결 미검증 |

사용자가 요구한 전체 구현이 완료된 상태가 아니다. 필수 안전 시험을 통과하지 못한 엔진을
성공 작업 기준의 Pi 성능 비교에 넣거나, 가벼운 실패를 절감 효과로 계산하지 않는다.
운영 배포본·프로필·인증·기존 접속 경로는 건드리지 않았으므로 운영 복구 작업도 필요 없다.

## 재현 방법과 후속 조건

별도 개발/시험 장비에서 저장소의 고정 Python 환경을 준비한다. 서비스 설치 명령이 아니다.
공식 릴리스에서 자신의 플랫폼에 맞는 압축 파일을 다운로드한 뒤 다음을 실행한다.
압축 파일은 파싱/실행 **전에** 고정 SHA256으로 검증되며 현재 플랫폼과 맞지 않으면 거절된다.

```text
uv sync --frozen --extra dev --extra browser
uv run python scripts/obscura_gate.py --engine obscura --archive <official-release-archive> --output <new-result.json>
uv run python scripts/obscura_gate.py --engine chromium --binary <chromium-path> --output <new-control.json>
```

- 종료 코드 `0`: 이 선행 검사 통과, `2`: 안전성/호환성 검사 실패, `1`: 시험 실행 자체 오류.
- Windows 바이너리 직접 경로는 추가 고정 해시를 확인한다. Linux는 검증된 압축 파일을 사용한다.
- 기존 CDP 연결에 붙거나 임의 URL/사용자 프로필을 받는 옵션은 없다.
- 출력 파일은 새 파일로만 만들며 덮어쓰지 않는다. 임시 추출물·시험 프로필·자식 프로세스는 정리한다.
- 이 시험을 일반 사이트에 사용할 수 있는 브라우저 도구로 노출하지 않는다.

재개하려면 upstream의 신뢰 가능한 native geometry/hit-testing/input 또는 동등한 검증된
경로가 필요하다. 새 버전은 자동 채택하지 않고 버전/해시를 다시 검토해야 한다.
그 후 노드/프레임 수명과 격리 시험, private control UI와 인증 보호 시험을 통과한 뒤에만
`배포용 세션`이 유휴·복구본을 확인해 분리된 Pi 시험을 진행한다. main 병합/기본 엔진 전환은 별도다.
