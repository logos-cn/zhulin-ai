#!/bin/bash
set -e

# 竹林 AI 一键部署脚本
# 适用于 Ubuntu 20.04+ / Debian 10+ / CentOS 7+

DEPLOY_DIR="/opt/zhulin-ai"
APP_USER="www-data"

echo "🚀 开始部署竹林 AI..."
echo ""

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then
  echo "❌ 请使用 sudo 运行此脚本"
  echo "用法：sudo ./deploy.sh"
  exit 1
fi

# 检测操作系统
if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS=$NAME
else
  echo "⚠️  无法检测操作系统版本"
  OS="Unknown"
fi

echo "📋 检测到操作系统：$OS"
echo ""

# 安装系统依赖
echo "📦 安装系统依赖..."
if command -v apt &> /dev/null; then
  apt update
  apt install -y python3 python3-pip python3-venv git curl
elif command -v yum &> /dev/null; then
  yum install -y python3 python3-pip git curl
elif command -v dnf &> /dev/null; then
  dnf install -y python3 python3-pip git curl
else
  echo "❌ 不支持的包管理器"
  exit 1
fi
echo ""

# 创建部署目录
echo "📁 创建部署目录..."
mkdir -p $DEPLOY_DIR
cd $DEPLOY_DIR

# 克隆代码
echo "📥 克隆代码..."
if [ -d ".git" ]; then
  echo "⚠️  代码已存在，执行 git pull..."
  git pull
else
  git clone https://github.com/logos-cn/zhulin-ai.git .
fi
echo ""

# 创建虚拟环境
echo "🐍 创建 Python 虚拟环境..."
python3 -m venv .venv
source .venv/bin/activate

# 安装 Python 依赖
echo "📚 安装 Python 依赖..."
pip install --upgrade pip
pip install -r requirements.txt
echo ""

# 生成 JWT 密钥
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# 创建 .env 文件
echo "⚙️  创建环境变量配置..."
cat > .env << EOF
# 竹林 AI 环境变量配置
APP_NAME=竹林 AI
APP_ENV=production
DATABASE_URL=sqlite:///./data/bamboo_ai.db
JWT_SECRET_KEY=$JWT_SECRET
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
CORS_ALLOW_ORIGINS=*
LOG_LEVEL=INFO
LOG_DIR=logs
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
APP_PORT=199
HOST=0.0.0.0
WORLD_EXTRACTION_MAX_WORKERS=3
MAX_WORLD_IMPORT_BYTES=67108864
LOGIN_RATE_WINDOW_SECONDS=300
LOGIN_RATE_MAX_ATTEMPTS=8
LOGIN_LOCKOUT_SECONDS=600
EOF
echo ""

# 创建数据和日志目录
echo "📂 创建数据和日志目录..."
mkdir -p data logs
chown -R $APP_USER:$APP_USER data logs
chown -R $APP_USER:$APP_USER .env
echo ""

# 初始化管理员
echo "👤 初始化管理员账户..."
read -p "设置管理员密码（留空使用默认密码 Admin@123456）: " -s ADMIN_PASSWORD
echo ""
if [ -z "$ADMIN_PASSWORD" ]; then
  ADMIN_PASSWORD="Admin@123456"
fi

python manage.py reset-admin --username admin --password $ADMIN_PASSWORD
echo ""

# 配置 systemd 服务
echo "🔧 配置 systemd 服务..."
if [ -f deploy/systemd/zhulin-ai.service ]; then
  cp deploy/systemd/zhulin-ai.service /etc/systemd/system/
  
  # 修改服务文件中的用户
  if [ "$OS" = "Ubuntu" ] || [ "$OS" = "Debian GNU/Linux" ]; then
    sed -i "s/User=zhulin/User=$APP_USER/g" /etc/systemd/system/zhulin-ai.service
    sed -i "s/Group=zhulin/Group=$APP_USER/g" /etc/systemd/system/zhulin-ai.service
  fi
  
  systemctl daemon-reload
  systemctl enable zhulin-ai
  systemctl start zhulin-ai
  echo ""
  
  # 检查服务状态
  if systemctl is-active --quiet zhulin-ai; then
    echo "✅ 服务启动成功！"
  else
    echo "⚠️  服务启动失败，请检查日志：journalctl -u zhulin-ai -f"
  fi
else
  echo "⚠️  systemd 服务文件不存在，跳过..."
fi
echo ""

# 获取服务器 IP
if command -v curl &> /dev/null; then
  SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "127.0.0.1")
else
  SERVER_IP="127.0.0.1"
fi

# 显示部署信息
echo "=========================================="
echo "✅ 竹林 AI 部署完成！"
echo "=========================================="
echo ""
echo "📍 访问地址：http://$SERVER_IP:199"
echo "👤 管理员账户：admin"
echo "🔑 管理员密码：$ADMIN_PASSWORD"
echo ""
echo "📝 重要文件位置："
echo "   - 代码目录：$DEPLOY_DIR"
echo "   - 数据库：$DEPLOY_DIR/data/bamboo_ai.db"
echo "   - 日志目录：$DEPLOY_DIR/logs/"
echo "   - 环境配置：$DEPLOY_DIR/.env"
echo ""
echo "🔧 常用命令："
echo "   - 查看状态：sudo systemctl status zhulin-ai"
echo "   - 重启服务：sudo systemctl restart zhulin-ai"
echo "   - 查看日志：sudo journalctl -u zhulin-ai -f"
echo "   - 停止服务：sudo systemctl stop zhulin-ai"
echo ""
echo "⚠️  请妥善保管："
echo "   - .env 文件中的 JWT_SECRET_KEY"
echo "   - 管理员密码"
echo ""
echo "📚 详细文档：https://github.com/logos-cn/zhulin-ai/blob/main/docs/DEPLOYMENT.md"
echo "=========================================="
