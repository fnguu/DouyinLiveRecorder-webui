#!/usr/bin/env bash
# 把本地仓库推送到 GitHub，推送后 Actions 会自动构建镜像。
#
# 用法：
#   ./push.sh https://github.com/<用户名>/<仓库名>.git
#   ./push.sh git@github.com:<用户名>/<仓库名>.git
#   ./push.sh https://<TOKEN>@github.com/<用户名>/<仓库名>.git   # 私有仓库用 PAT
#
# 提示：把 token 写在 URL 里会被记录到 .git/config。用完后建议：
#   git remote set-url origin https://github.com/<用户名>/<仓库名>.git
set -e

REMOTE="${1:-}"
if [ -z "$REMOTE" ]; then
  echo "用法: ./push.sh <仓库地址>"
  echo "  例: ./push.sh https://github.com/yourname/douyin-live-recorder-webui.git"
  exit 1
fi

cd "$(dirname "$0")"

if [ ! -d .git ]; then
  echo "当前目录不是 git 仓库"
  exit 1
fi

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE"
else
  git remote add origin "$REMOTE"
fi

echo ">> 推送到 $REMOTE"
git push -u origin main

echo ""
echo "推送完成。接下来："
echo "  1. 打开仓库的 Actions 页面，等待 Build WebUI Image 跑完（约 5~10 分钟）"
echo "  2. 镜像地址: ghcr.io/<用户名>/<仓库名>:latest"
echo "  3. 私有仓库的镜像包默认私有，改为 Public 才能免登录拉取："
echo "     仓库 -> Packages -> 选中镜像包 -> Package settings -> Change visibility"
echo "  4. 拉取并启动:"
echo "     docker pull ghcr.io/<用户名>/<仓库名>:latest"
echo "     docker tag ghcr.io/<用户名>/<仓库名>:latest douyin-live-recorder-webui:latest"
echo "     docker compose up -d"
