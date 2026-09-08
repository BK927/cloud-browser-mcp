# Docker·네이티브 동일 예산 비교

[2026-09-09 실측 보고서](PI_COMPARISON_2026-09-09.md)에 `3a172a1`의 9회 비교와
기존 운영 복원 결과를 기록했습니다. 아래는 같은 조건으로 새 실험을 수행하는 절차입니다.
구현/CI와 실제 Pi 실측을 구분하며, 운영 배포는 기존 배포용 작업이 소유합니다.
직접 sudo 인증이 가능한 시점에만 비교합니다.
비밀번호를 채팅/명령 인자에 넣지 않습니다. 이번 비교는 네이티브 영구 전환 승인이 아닙니다.

## 고정 조건

- 동일 Pi, RAM **1GiB** browser 실행 집단 제한, 같은 swap 한도·화면·사이트 순서.
- 기존 Docker `b7bd34b97d7391a1c691985c8e7714b8d493262e`, 개선 Docker, 개선 native를 각 3회.
- Docker daemon/다른 MCP는 계속 유지. Docker 전체 데몬 소비를 native 절감량에 포함하지 않음.
- 유휴, 단일 무거운 페이지, 관찰, 이미지, 추가 탭, 수동 제어, 세션 종료 후 회수를 기록.
- 새 전용 비교 데이터/프로필을 사용. 기존 auth DB·프로필·env·배포 경로·rollback image는 보존.
- 같은 `benchmark.py` 바이트를 세 설치에 사용. 기존 배포 소스는 수정하지 않음.
- OS cache를 강제로 비우거나 기존 사용자 탭을 닫지 않음. 각 run 사이 안정화 시간을 동일하게 둠.
  웹 광고/네트워크/온도 영향과 실행 순서 편향을 보고하고, 가능하면 세 변형을 교차 순환.

설치기/스크립트가 운영 Docker를 자동 중단하지 않습니다. 배포 담당자가 기존 소유권과
진행 중 작업을 확인한 후 **이 브라우저만** 정리·일시 중단·복원할 수 있습니다.
구형 baseline의 sudo/namespace/상주 화면 구성은 임의로 개선하지 않고 baseline 그대로 측정합니다.

## 실측 전 시작 관문

전체 9회 반복에 앞서 새 네이티브 설치의 UID import뿐 아니라 network → egress →
분리된 ingress health → 인증된 MCP status까지 짧게 확인합니다. 현재 작업 유무·주소/포트
충돌·메모리 여유를 먼저 확인하고, 이 관문에는 실제 로그인/게시나 무거운 브라우저 작업을
넣지 않습니다. 단순 API 시작 전에 실패하는 문제 때문에 Docker 측정부터 반복하지 않습니다.

실패하면 정확한 원인·시험 unit 상태·veth 주소·NM 장치 상태·cgroup events만 안전하게
수집한 뒤 기존 운영을 복원합니다. 전체 환경변수/로그/토큰/연결 프로필을 출력하지 않습니다.
확인 후 시험 역할을 정지하고 원래 조건으로 안정화한 뒤 별도 측정 세트를 시작합니다.
이전 코드의 부분 표본을 수정본의 3회 결과에 합치지 않습니다.

## 측정 도구

`scripts/sample_memory.py`는 호스트에서 실행하는 읽기 전용 표본 수집기입니다.
각 서비스의 정확한 host PID를 지정합니다. root cgroup/호스트 전체 wildcard는 거부합니다.
프로세스 명·PID·PSS/RSS/swap, cgroup 원시 charge/stat/events, host MemAvailable/swap을
JSONL로 출력하며 argv·환경변수·토큰·페이지 내용은 출력하지 않습니다.

```sh
sudo python3 scripts/sample_memory.py --pid BROWSER_HOST_PID \
  --pid EGRESS_HOST_PID --pid INGRESS_HOST_PID --duration 180 \
  --label improved-native-1 > /private/results/improved-native-1.memory.jsonl
```

숫자 PID는 담당자가 `docker inspect` 또는 `systemctl show -p MainPID --value`로 **직전 확인**합니다.
sampler는 별도 호스트 프로세스이고 측정 대상 cgroup에 들어가지 않습니다. 권한 부족으로
PSS를 못 읽은 PID는 `unreadable_pids`로 표시하며 RSS로 PSS를 대체하지 않습니다.

동일한 workload 예시(대상 URL은 비교 시작 전에 확정하고 세 방식에 동일하게 적용):

```sh
docker exec -i --user app EXACT_BROWSER_CONTAINER python - \
  --url https://en.wikipedia.org/wiki/World_Wide_Web --rounds 3 --extra-tabs 1 \
  --settle-seconds 5 --manual-control --require-budget-mb 1024 \
  --label baseline-docker-1 < scripts/benchmark.py > /private/results/baseline-docker-1.benchmark.json

sudo python3 scripts/native_benchmark.py --pid NATIVE_API_PID \
  --url https://en.wikipedia.org/wiki/World_Wide_Web --rounds 3 --extra-tabs 1 \
  --settle-seconds 5 --manual-control --require-budget-mb 1024 \
  --label improved-native-1 > /private/results/improved-native-1.benchmark.json
```

benchmark는 임시 로컬 OAuth grant를 발급하고 끝에 폐기합니다. 사용자 grant를 초기화하지
않습니다. 전용 native launcher는 이 benchmark 자식만 API의 namespace/cgroup/UID로
옮겨 Docker exec와 동일하게 클라이언트 부하도 예산에 포함합니다. 서버의 env는 자식에게만
전달하며 출력하지 않습니다. 이 도구는 운영자 로컬 실행용이고 MCP 기능이 아닙니다.

`--manual-control`은 **비어 있는 시험 작업**의 제어 화면을 잠깐 열고 비공개 console의
CSRF 경로로 완료합니다. 사용자 인증/실제 전송/승인을 대행하지 않습니다. 기존 세션이
있으면 시작하지 않습니다. 1GiB와 다른 실제 cgroup 제한이면 비교를 거부합니다.
오류와 `RESOURCE_PRESSURE`, 캡처 실패도 measurements에 남으며 성공 표본으로 대체하지 않습니다.

## 결과 보고

각 변형의 3회 원자료, 정확한 commit/image, kernel/architecture, 메모리·swap 예산,
화면·page 순서·전용 프로필 여부·주변 부하를 함께 보관합니다. 아래는 **새 실험용 빈 양식**이며
완료한 9월 9일 측정의 누락을 뜻하지 않습니다. 단계별 중앙값과 최소/최대, 실패 횟수를
보고합니다. 완료 수치는 [보고서](PI_COMPARISON_2026-09-09.md)와
[9회 공개 데이터](benchmarks/pi-2026-09-09.json)에 있습니다.

| phase | browser cgroup raw/cache | 전체 세 역할 PSS/RSS | host available/swap | 시간/거절/OOM |
|---|---|---|---|---|
| idle | 측정 필요 | 측정 필요 | 측정 필요 | 측정 필요 |
| single_page | 측정 필요 | 측정 필요 | 측정 필요 | 측정 필요 |
| text/long observation | 측정 필요 | 측정 필요 | 측정 필요 | 측정 필요 |
| viewport/full-page image | 측정 필요 | 측정 필요 | 측정 필요 | 측정 필요 |
| additional_tabs | 측정 필요 | 측정 필요 | 측정 필요 | 측정 필요 |
| manual_control | 측정 필요 | 측정 필요 | 측정 필요 | 측정 필요 |
| after_close | 측정 필요 | 측정 필요 | 측정 필요 | 측정 필요 |

raw charge에서 cache를 뺀 수치만으로 절감을 주장하지 않습니다. 실제 cgroup memory.events의
OOM/oom_kill 증분과 MCP 오류를 함께 확인합니다. native 절감량이 0 또는 음수여도 그대로
보고하고, 설치 변경 효과와 코드 개선 효과를 따로 비교합니다. 세 번을 못 마쳤다면
부분 결과로 표시하며 나머지를 추정하지 않습니다.
