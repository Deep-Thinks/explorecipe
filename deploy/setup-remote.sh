#!/bin/bash
# 在远端裸服务器上首次部署 explorecipe。
#
# 用法（在本地）：
#   1) 配置远端的 SSH 免密登录（推荐 ssh-copy-id）
#   2) export DOMAIN=explorecipe.example.com
#      export REMOTE=user@your.server.ip
#      ssh "$REMOTE" 'bash -s' < deploy/setup-remote.sh
#   3) 把本地 .env 拷过去：
#      scp .env "$REMOTE":/opt/explorecipe/.env
#   4) 申请 HTTPS 证书：
#      ssh "$REMOTE" "certbot --nginx -d $DOMAIN --redirect --agree-tos -m you@example.com -n"
#   5) 启动：
#      ssh "$REMOTE" "systemctl start explorecipe && systemctl status explorecipe"
#
# 注意：脚本不会写 .env，需要部署后手动拷贝本地 .env 内容到 /opt/explorecipe/.env。

set -euo pipefail

PROJECT="${PROJECT:-/opt/explorecipe}"
REPO="${REPO:-https://github.com/Deep-Thinks/explorecipe.git}"
DOMAIN="${DOMAIN:-explorecipe.example.com}"

echo "==> 检查依赖"
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }
command -v nginx >/dev/null || { echo "需要 nginx（请先 apt install nginx）"; exit 1; }

echo "==> 安装 Python 依赖"
python3 -m pip install --break-system-packages -r "$PROJECT/requirements.txt" 2>/dev/null || \
    python3 -m pip install --break-system-packages openai google-genai Pillow qrcode

echo "==> 克隆代码到 $PROJECT"
if [ -d "$PROJECT/.git" ]; then
    echo "    项目目录已存在，跳过 clone"
else
    mkdir -p "$PROJECT"
    git clone "$REPO" "$PROJECT"
fi

echo "==> 准备 logs 目录"
mkdir -p "$PROJECT/logs"

echo "==> 装 post-merge git hook"
cp "$PROJECT/deploy/post-merge" "$PROJECT/.git/hooks/post-merge"
chmod +x "$PROJECT/.git/hooks/post-merge"

echo "==> 装 systemd unit"
cp "$PROJECT/deploy/explorecipe.service" /etc/systemd/system/explorecipe.service
systemctl daemon-reload
systemctl enable explorecipe

echo "==> 装 nginx vhost（HTTP only，certbot 后续会改成 HTTPS）"
# 把模板里的 DOMAIN 占位替换为实际域名
sed "s/explorecipe.example.com/$DOMAIN/g" "$PROJECT/deploy/nginx-explorecipe.conf" \
    > /etc/nginx/sites-available/$DOMAIN
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
nginx -t

echo "==> 重载 nginx"
systemctl reload nginx

echo ""
echo "✅ 自动化完成。剩下三步手动操作："
echo ""
echo "  1) 写 .env：在 $PROJECT/.env 填入 IMAGE_API_KEY / STEPFUN_API_KEY / GEMINI_API_KEY / MINICPM_API_KEY"
echo "     从本地拷贝："
echo "       scp .env user@your.server:$PROJECT/.env"
echo ""
echo "  2) 申请 HTTPS 证书（DNS A 记录必须先做好）："
echo "       certbot --nginx -d $DOMAIN --redirect --agree-tos -m you@example.com -n"
echo ""
echo "  3) 启动服务："
echo "       systemctl start explorecipe"
echo "       systemctl status explorecipe"
echo "       journalctl -u explorecipe -f"
