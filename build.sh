#!/usr/bin/env bash
# 一键构建镜像
# 用法: ./build.sh [镜像名:标签] [--cn]
set -e

IMAGE="${1:-douyin-live-recorder-webui:latest}"
if [ "$IMAGE" = "--cn" ]; then IMAGE="douyin-live-recorder-webui:latest"; set -- "" "--cn"; fi

if [ "$2" = "--cn" ] || [ "$IMAGE" = "--cn" ]; then
  echo ">> 使用国内镜像源构建"
  docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t "$IMAGE" .
else
  docker build -t "$IMAGE" .
fi

echo ""
echo "构建完成: $IMAGE"
echo "启动:  docker compose up -d"
echo "访问:  http://localhost:8000"
