# 의존성 라이선스 고지

직접 작성한 코드·문서에만 루트 `LICENSE`의 MIT를 적용합니다. **의존성, 브라우저,
글꼴, 컨테이너에 설치되는 소프트웨어를 MIT로 재라이선스하지 않습니다.**

## DrissionPage — 기본 엔진

- 버전: `4.1.1.4` (`browser` extra와 Docker 기본 엔진).
- [원저작권자의 공식 라이선스](https://github.com/g1879/DrissionPage/blob/master/LICENSE).
- 개인 학습 및 합법적 비영리 사용 조건이 있으며 상업적 사용에는 별도 허가가
  필요합니다. 원문에는 그 밖의 사용 조건도 있습니다. 설치·사용·재배포 전에
  **선택 버전의 전체 라이선스**를 확인하세요. 이 요약은 원문을 대신하지 않습니다.
- MIT/Apache/BSD처럼 제한 없는 상업 사용을 허용하는 의존성으로 취급하면 안 됩니다.
- **프로젝트 자체는 MIT지만, DrissionPage 포함 기본 배포 전체를 ‘모든 용도에
  제한 없는 MIT 소프트웨어’라고 표시하면 안 됩니다.**
- 코드가 공개되어 누구나 설치할 수 있다는 것과 모든 용도로 쓸 권리가 있다는 것은
  다릅니다. 이 프로젝트는 의존성의 상업 사용 허가를 부여하지 않습니다.

엔진 import는 `src/cloud_browser/drission.py` 안으로 격리했습니다. 후속 엔진을
추가해도 기존 의존성의 라이선스는 바뀌지 않습니다. 현재 다른 엔진은 포함하지 않습니다.

## 다른 구성요소

각 배포물의 원래 고지·라이선스를 유지하세요. Docker는 원본 배포판/Python 패키지를
설치하며 이 저장소에 해당 프로젝트 소스를 복사하여 포함하지 않습니다.

- [공식 Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk): MIT.
- FastAPI, Uvicorn, Pydantic, Argon2, Pillow, psutil, HTTP/WebSocket 라이브러리:
  각 배포물의 고지 참조.
- [Chromium](https://www.chromium.org/chromium-os/licenses/): 다수 구성요소별 라이선스.
- [noVNC](https://github.com/novnc/noVNC): MPL 2.0 및 포함 구성요소별 고지.
- x11vnc, Xvfb, websockify, Debian 패키지·글꼴: 컨테이너의
  `/usr/share/doc/<package>/copyright` 참조. GPL 등 다른 조건이 포함됩니다.
  컨테이너 재배포 시 필요한 소스 제공 의무도 확인하세요.
- Tailscale은 운영자가 별도로 설치하는 접속 경로이며 이 저장소의 MIT 대상이 아닙니다.

`uv.lock`은 Python 의존성 버전을 기록하지만 법률 검토나 호환성 보증서는 아닙니다.
