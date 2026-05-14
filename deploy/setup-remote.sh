#!/bin/bash
# 在远端 101.33.32.162 上首次部署 explorecipe。
# 用法（在本地执行）：
#   sshpass -p 'a1b2c3d4<>++' ssh root@101.33.32.162 'bash -s' < deploy/setup-remote.sh
# 注意：这个脚本不会写 .env，需要执行后手动拷贝本地 .env 内容到 /opt/explorecipe/.env。

set -euo pipefail

PROJECT=/opt/explorecipe
REPO=https://github.com/Deep-Thinks/explorecipe.git
DOMAIN=explorecipe.xmu-cuisine.club

echo "==> 检查依赖"
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }
python3 -c "import openai" 2>/dev/null || python3 -m pip install --break-system-packages openai

echo "==> 克隆代码"
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

echo "==> 装 nginx vhost (HTTP only, certbot 后续会改成 HTTPS)"
cp "$PROJECT/deploy/nginx-explorecipe.conf" /etc/nginx/sites-available/$DOMAIN
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
nginx -t

echo "==> 重载 nginx"
systemctl reload nginx

echo ""
echo "✅ 自动化完成。剩下三步手动操作："
echo ""
echo "  1) 写 .env：在 $PROJECT/.env 填入 IMAGE_API_KEY / IMAGE_UPSTREAM_URL / STEPFUN_API_KEY"
echo "     可以从本地 cp 过去："
echo "       sshpass -p 'a1b2c3d4<>++' scp .env root@101.33.32.162:$PROJECT/.env"
echo ""
echo "  2) 申请 HTTPS 证书（DNS A 记录必须先做好）："
echo "       certbot --nginx -d $DOMAIN --redirect --agree-tos -m dev@xmu-cuisine.club -n"
echo ""
echo "  3) 启动服务："
echo "       systemctl start explorecipe"
echo "       systemctl status explorecipe"
echo "       journalctl -u explorecipe -f"
