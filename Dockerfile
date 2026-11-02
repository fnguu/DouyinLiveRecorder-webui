# DouyinLiveRecorder + WebUI
# 构建:  docker build -t douyin-live-recorder-webui:latest .
# 国内加速: docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t douyin-live-recorder-webui:latest .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    DLR_NO_CLEAR=1 \
    DLR_NONINTERACTIVE=1 \
    WEBUI_PORT=8000

WORKDIR /app

# ffmpeg 负责录制；nodejs 供部分平台（抖音/TikTok）的签名脚本执行
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg supervisor tzdata curl ca-certificates gnupg; \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo "$TZ" > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-webui.txt ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt -r requirements-webui.txt

COPY . .

RUN mkdir -p /app/config /app/logs /app/downloads /app/backup_config /run/dlr \
 && cp -a /app/config /app/config_default \
 && chmod +x /app/docker/entrypoint.sh

# 构建信息：WebUI 的「系统状态」页会读取
ARG BUILD_TIME=""
ARG GIT_SHA=""
RUN printf '{"build_time":"%s","git_sha":"%s"}' "${BUILD_TIME}" "${GIT_SHA}" > /app/build_info.json

EXPOSE 8000
VOLUME ["/app/config", "/app/downloads", "/app/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WEBUI_PORT}/api/health" || exit 1

ENTRYPOINT ["/bin/bash", "/app/docker/entrypoint.sh"]
