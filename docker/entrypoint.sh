#!/usr/bin/env bash
# 容器入口：初始化挂载目录 + 拉起 supervisord（录制进程 + WebUI）
set -e

APP_DIR=/app
CONFIG_DIR="$APP_DIR/config"
DEFAULT_DIR="$APP_DIR/config_default"

mkdir -p "$CONFIG_DIR" "$APP_DIR/logs" "$APP_DIR/downloads" "$APP_DIR/backup_config" /run/dlr

# config 卷首次挂载时是空目录：用镜像内置模板补齐，避免录制器启动即报错
for f in config.ini URL_config.ini; do
    if [ ! -f "$CONFIG_DIR/$f" ] && [ -f "$DEFAULT_DIR/$f" ]; then
        cp "$DEFAULT_DIR/$f" "$CONFIG_DIR/$f"
        echo "[init] 已从模板生成 $CONFIG_DIR/$f"
    fi
done
[ -f "$CONFIG_DIR/URL_config.ini" ] || : > "$CONFIG_DIR/URL_config.ini"

# WebUI 的状态文件（记录每个主播的通知开关等）也放在 config 卷里，随容器一起持久化
touch "$CONFIG_DIR/webui_state.json" 2>/dev/null || true
[ -s "$CONFIG_DIR/webui_state.json" ] || echo '{"version":1,"anchors":[],"deleting":[]}' > "$CONFIG_DIR/webui_state.json"

export WEBUI_PORT="${WEBUI_PORT:-8000}"
echo "[init] WebUI -> http://0.0.0.0:${WEBUI_PORT}  录制目录 -> ${APP_DIR}/downloads"

exec supervisord -c "$APP_DIR/docker/supervisord.conf" -n
