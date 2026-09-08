FROM python:3.13-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY LICENSE THIRD_PARTY_NOTICES.md /app/
COPY deploy/seccomp /app/deploy/seccomp
COPY docs /app/docs
COPY uv.lock /app/uv.lock
COPY src /app/src
COPY scripts /app/scripts
RUN pip install --no-cache-dir uv==0.12.9 && uv sync --frozen --no-dev --no-editable

FROM base AS egress
USER 65534:65534
ENTRYPOINT ["python", "-m", "cloud_browser.egress"]

FROM base AS ingress
USER 65534:65534
ENTRYPOINT ["python", "-m", "cloud_browser.ingress"]

FROM base AS browser
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-sandbox xvfb xauth x11vnc novnc websockify \
    fonts-noto-cjk fonts-liberation iptables iproute2 sudo util-linux tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 browser && useradd --uid 1001 --gid 1001 --create-home browser \
    && useradd --uid 1000 --create-home --groups browser app \
    && uv sync --frozen --no-dev --no-editable --extra browser
COPY deploy/chromium-launcher /usr/local/bin/chromium-launcher
COPY deploy/entrypoint.sh /usr/local/bin/cloud-browser-entrypoint
COPY deploy/browser-stop /usr/local/bin/cloud-browser-stop
COPY deploy/browser.sudoers /etc/sudoers.d/cloud-browser
RUN chmod 755 /usr/local/bin/chromium-launcher /usr/local/bin/cloud-browser-entrypoint /usr/local/bin/cloud-browser-stop \
    && chmod 440 /etc/sudoers.d/cloud-browser && visudo -cf /etc/sudoers.d/cloud-browser
ENV DISPLAY=:99 CB_CHROMIUM_PATH=/usr/local/bin/chromium-launcher CB_DATA_DIR=/data \
    CB_BIND_HOST=0.0.0.0 CB_BROWSER_PROXY=http://egress:3128 CB_HEADLESS=false
ENTRYPOINT ["tini", "--", "/usr/local/bin/cloud-browser-entrypoint"]
