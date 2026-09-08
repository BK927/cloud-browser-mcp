# Debian 13 네이티브 설치

Docker 없이 같은 Python 패키지·DrissionPage 어댑터·MCP 계약을 실행하는 추가 경로입니다.
지원 대상은 Debian 13 arm64/amd64이며 Windows/macOS는 [Docker](DEPLOYMENT.md)를 사용합니다.
Pi 영구 전환은 자동으로 수행하지 않습니다. 실제 Pi 비교 결과는 별도 검증 관문입니다.

## 처음 설치

엔진의 [별도 라이선스](../THIRD_PARTY_NOTICES.md)를 먼저 확인하세요. 신뢰한 정확한 커밋을
checkout하고 `.env.example`을 별도 운영자 파일로 복사해 주소·callback·암호 해시를 설정합니다.
암호는 `cloud-browser hash-password`의 숨김 터미널 입력으로 만들며 채팅·명령 인자로 보내지 않습니다.
기존 Docker의 OAuth 주소·client ID·callback·정책을 유지할 수 있지만 DB/프로필을 자동 복사하지는 않습니다.

```sh
sudo python3 scripts/native_install.py install \
  --source . --env-file /absolute/private/browser.env \
  --memory-mib 1024 --public-port 18000 --control-port 18001
sudo python3 scripts/native_install.py status
```

기본 데이터는 새 `/var/lib/cloud-browser-native`입니다. 다른 경로는 전용 절대 경로
`--data-dir /var/lib/cloud-browser-comparison`처럼 지정합니다. 내용이 있는 미소유 경로,
심볼릭 링크, 홈/저장소 루트는 가져오거나 덮어쓰지 않습니다. `.env`는 최초에만
`/etc/cloud-browser/browser.env`로 복사하며 업데이트 때 덮어쓰지 않습니다.

설치기를 `umask 077`에서 실행해도 됩니다. 서비스 계정이 읽는 코드·가상환경 및
`/etc/cloud-browser` 디렉터리는 root 소유의 공개 읽기/탐색 권한으로 만들지만,
`browser.env`는 계속 `root:cb-api 0640`, 브라우저 HOME은 `0700`으로 유지합니다.
의존성 생성 동안만 공개 코드용 umask를 사용하고 원래 값을 복원합니다. 과거의 제한된
wheel 캐시 권한을 재사용하지 않으며, 서비스 UID별 실제 import를 서비스 중단 전에 검사합니다.

구버전 설치가 `200/CHDIR` 또는 `203/EXEC`로 실패했다면 수정된 checkout으로 update하세요.
설치기는 소유권을 확인한 공용 상위 디렉터리를 복구하고 새 해시 경로에 가상환경을 만듭니다.
이전 실패 release·설정·데이터를 재귀적으로 chmod하거나 삭제하지 않습니다. 같은 release를
재사용할 때도 접근 검사를 통과해야 하며, 임의로 바뀐 권한을 숨긴 채 시작하지 않습니다.

설치만 하면 리스너는 시작하지 않습니다. 포트 충돌·자원 여유·현재 작업 종료를 확인한 뒤:

```sh
sudo python3 scripts/native_install.py update --source . --start
```

`--start`는 이 프로젝트의 네이티브 unit만 재시작합니다. Docker·Tailscale·다른 MCP에는
시작/중단/삭제 명령을 내리지 않습니다. 기존 Docker의 8000/8001과 비교용 18000/18001을
구분하세요. 동시에 두 브라우저를 돌려 Pi의 같은 예산 비교를 망치지 않도록 실제 측정 순서는
운영자가 조정합니다. 외부 Funnel/Serve 경로 변경은 설치기 범위 밖입니다.

## 격리와 상주 비용

| 역할 | Linux 경계 | 기본 예산 |
|---|---|---|
| API·worker·Chromium·화면 | `cloud-browser.service`, 전용 netns, cb-api/cb-browser UID | 1024MiB RAM + 1024MiB swap 상한 |
| 검증 egress | cb-egress, host-side veth 주소의 3128만 수신 | 128MiB |
| 고정 ingress | cb-ingress, host 127.0.0.1의 두 포트만 수신 | 64MiB |
| netns 설정 | root oneshot, 설정 후 종료 | 상주 Python 없음 |

세션이 없으면 Chromium/Xvfb가 없습니다. 로그인/handoff 때만 x11vnc·WebSocket 중계를
띄우고 완료/종료 때 회수합니다. 자동/수동 제어가 같은 Chromium과 보호된 Xauthority를
공유합니다. 초기 viewport는 1024×768이며 가상 화면 상한은 운영자가 조절합니다.
Chromium의 HOME은 전용 데이터 아래 `browser-home`이며 프로필 하위 파일은 전용
browser 그룹으로만 공유합니다. API의 0007 umask는 이 두 UID의 프로필 접근을 위한 값이고,
자격증명 env·Xauthority·비공개 업로드에는 별도 제한 권한을 적용합니다.

기본 연결은 사용하지 않는 `10.203.87.0/30`입니다. 충돌하면 `--cidr`로 다른 사설 /30을
선택합니다. 브라우저 namespace에는 default route가 없고 browser UID는 검증 proxy 외
TCP/IP 목적지에 연결할 수 없습니다. IPv6도 차단합니다. CDP/VNC는 원본 포트를 공개하지 않습니다.
proxy는 매 연결 DNS/IP 검증으로 내부망·loopback·인증정보 URL 접근을 거부합니다.

NetworkManager가 실행 중이면, 설치한 network unit은 **이번 시작에서 생성한 전용 veth
두 개만** `nmcli device set … managed no`로 넘겨받은 뒤 주소를 설정합니다. 장치 등록을
제한 시간 동안 기다리고 unmanaged 상태를 확인합니다. NM 전체 reload/restart, 연결 프로필
수정, 영구 unmanaged 설정 또는 Ethernet/Wi-Fi·Docker·Tailscale 변경은 하지 않습니다.
이는 Debian이 제공하는 [장치별 런타임 설정](https://manpages.debian.org/trixie/network-manager/nmcli.1.en.html#DEVICE_MANAGEMENT_COMMANDS)을 사용합니다.

network 완료, egress `ExecStartPre`, API 시작에서 관련 veth의 종류·UP 상태·정확한 IPv4/30을
검사합니다. `NATIVE_MANAGER_UNVERIFIED`, `NATIVE_LINK_CHANGED`, `NATIVE_LINK_NOT_READY`는
검증 실패이며 우회 실행하지 않습니다. NM이 없으면 새로 설치/시작하지 않습니다. 실행 중에
관리자가 NM을 재설정하거나 장치 소유권을 바꾸는 경우 자동 복구를 보장하지 않으므로 기존
작업을 안전하게 종료하고 전용 network unit을 다시 시작해 확인하세요.

운영 API는 root 소유 attestation·실제 namespace·실제 cgroup `memory.max`·proxy 경로를
확인하지 못하면 리스너를 열지 않습니다. Docker와 같은 데이터 inode를 여는 다른 인스턴스는
OS 잠금에서 거절됩니다. 구버전 Docker는 이 잠금이 없으므로 구버전 데이터 볼륨을 공유하지 마세요.
Chromium sandbox를 끄는 옵션이나 `privileged` 실행은 네이티브 설치에 없습니다.
고정 sudo wrapper와 setuid sandbox 때문에 API unit은 `NoNewPrivileges=false`이며,
root 권한이 필요한 명령은 전용 브라우저 실행/정리 두 경로로 제한합니다.

## 업데이트·상태·복구

```sh
sudo python3 scripts/native_install.py update --source /absolute/verified/checkout --start
sudo python3 scripts/native_install.py status
sudo systemctl status cloud-browser.service cloud-browser-egress.service cloud-browser-ingress.service
```

소스는 해시별 `/opt/cloud-browser/releases/`에 설치하며 의존성은 `uv.lock`으로 고정합니다.
`/etc/cloud-browser/install.json`은 현재/이전 release와 관리 파일 해시를 기록합니다.
수정된 unit/wrapper를 발견하면 보존 검토를 요구합니다. drop-in은 지우지 않으며,
실행 시 격리·예산 검사가 잘못된 override를 거부합니다.
`runtime.env`는 설치기가 관리하는 비밀값 없는 실행 경계입니다. 이 파일이 운영자 env보다
나중에 적용되어 Docker용 data/bind/proxy 값이 네이티브 경계를 바꾸지 못하게 합니다.

시작 실패 시 기존 소스·설정·데이터를 자동 삭제하지 않습니다. health 실패는 성공으로
보고하지 않습니다. 이전 checkout으로 같은 `update --start`를 실행해 명시적으로 롤백할 수
있지만, DB 형식 변경이 있는 향후 버전은 먼저 호환성을 검토해야 합니다. 인증 중 재시작은
세션을 만료시키므로 사용자가 직접 제어를 마칠 때까지 업데이트하지 마세요.

namespace 설정 중 실패하여 attestation이 없거나 규칙이 바뀌면 자동 정리도 거절합니다.
관리자가 `ip netns`, 정확한 cb-* interface와 `CB_NATIVE_*` 체인을 점검해야 합니다.
호스트 방화벽 전체 flush, Docker 네트워크 제거, sandbox 해제로 복구하지 마세요.
주소가 사라졌더라도 보호된 attestation·namespace identity·방화벽이 그대로이면 전용
`network-down`은 허용합니다. 실행 준비 상태와 정리 소유권 검사를 분리한 것이며,
미소유/변조된 네트워크를 제거할 수 있게 하는 예외는 아닙니다.

## 삭제

```sh
sudo python3 scripts/native_install.py uninstall
```

네이티브 unit과 전용 sudo 권한만 제거합니다. 설정·release·DB·프로필은 보존합니다.
정말 이 네이티브 설치의 데이터까지 삭제하려는 경우에만 `uninstall --purge-data`를
사용하세요. 검증한 전용 경로를 잠근 뒤 영구 삭제하며 되돌리려면 사전 백업이 필요합니다.
계정·Debian 패키지·Docker·다른 MCP는 제거하지 않습니다.

## 검증 범위

Linux CI는 `umask 077`의 신규 설치·동일 release 업데이트와 자격증명 접근 차단,
실제 unit/netns/UID 차단, MCP 이미지, on-demand 화면/수동 제어, worker 강제
종료 후 회수를 검사합니다. Ubuntu CI의 Google Chrome 검증은 Debian 설치나 Pi 실측을
대체하지 않습니다. [동일 예산 비교 절차](PERFORMANCE_COMPARISON.md)를 따르고 절감이
없거나 실패한 경우도 그대로 기록하세요.

실제 NM 회귀는 인터넷과 호스트 mount가 없는 별도 `network-manager-test` CI 컨테이너에서
검사합니다. 그 컨테이너의 추가 namespace 권한/보안 프로필은 중첩 netns 시험 전용이며
일반 Docker·네이티브 배포에 적용하지 않습니다. Chromium sandbox 검사도 별도로 유지합니다.
