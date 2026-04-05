# 竹林 AI 部署说明

本文档给出本地运行、Linux 服务器常驻部署和基础运维建议。

**最新版本**: v0.2.0  
**更新时间**: 2026-04-05  
**GitHub**: https://github.com/logos-cn/zhulin-ai

## 一、环境要求

- Python 3.9+
- Git
- 可访问你所使用的 AI 服务商接口
- 建议：
  - 2 核 CPU / 4 GB 内存起步
  - 使用 SSD

依赖由 `requirements.txt` 提供：

- `fastapi`
- `uvicorn[standard]`
- `sqlalchemy`
- `bcrypt`
- `python-multipart`
- `python-docx`
- `pypdf`

## 二、环境变量

项目启动时会自动读取项目根目录 `.env` 文件，也支持 systemd `Environment=` 或容器环境变量注入。

### 2.1 生成 JWT 密钥

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2.2 完整环境变量列表

| 变量名 | 说明 | 建议值 | 必填 |
| --- | --- | --- | --- |
| `APP_NAME` | 应用名称 | `竹林 AI` | 否 |
| `APP_ENV` | 运行环境 | `production` | 是 |
| `DATABASE_URL` | 数据库连接串 | `sqlite:///./data/bamboo_ai.db` | 是 |
| `JWT_SECRET_KEY` | JWT 密钥 | 使用长随机字符串 | 是 |
| `JWT_ALGORITHM` | JWT 算法 | `HS256` | 否 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 登录有效期 | `1440` | 否 |
| `CORS_ALLOW_ORIGINS` | CORS 白名单 | `*` 或具体域名 | 否 |
| `LOG_LEVEL` | 日志级别 | `INFO` | 否 |
| `LOG_DIR` | 日志目录 | `logs` | 否 |
| `LOG_MAX_BYTES` | 单日志文件最大大小 | `10485760` (10MB) | 否 |
| `LOG_BACKUP_COUNT` | 日志文件保留数量 | `5` | 否 |
| `APP_PORT` | 启动端口 | `199` | 否 |
| `HOST` | 监听地址 | `0.0.0.0` | 否 |
| `WORLD_EXTRACTION_MAX_WORKERS` | 原著导入提取并发数上限 | `3` (2C2G 服务器) | 否 |
| `MAX_WORLD_IMPORT_BYTES` | 原著导入最大文件大小 | `67108864` (64MB) | 否 |
| `LOGIN_RATE_WINDOW_SECONDS` | 登录失败检测窗口 | `300` | 否 |
| `LOGIN_RATE_MAX_ATTEMPTS` | 登录失败最大尝试 | `8` | 否 |
| `LOGIN_LOCKOUT_SECONDS` | 登录锁定时间 | `600` | 否 |

### 2.3 .env 文件示例

```bash
# 竹林 AI 环境变量配置
APP_NAME=竹林 AI
APP_ENV=production
DATABASE_URL=sqlite:///./data/bamboo_ai.db
JWT_SECRET_KEY=VbK6U_dJFpT4siCkI1_JUAIiQEcQ0YE6g0kYhW7pBGI
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
```

## 三、Windows 本地部署

### 1. 安装依赖

```powershell
cd C:\path\to\zhulin-ai
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 设置环境变量

PowerShell 示例：

```powershell
$env:APP_ENV="production"
$env:DATABASE_URL="sqlite:///./data/bamboo_ai.db"
$env:JWT_SECRET_KEY="replace-with-a-long-random-secret"
$env:APP_PORT="199"
```

也可以在项目根目录创建 `.env` 文件，内容与上述变量一致。

### 3. 初始化管理员

```powershell
python manage.py reset-admin --username admin --password YourStrongPassword
```

### 4. 启动

```powershell
uvicorn main:app --host 0.0.0.0 --port 199
```

## 四、Linux / macOS 本地部署

### 1. 安装依赖

```bash
cd /opt/zhulin-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 设置环境变量

```bash
export APP_ENV=production
export DATABASE_URL=sqlite:///./data/bamboo_ai.db
export JWT_SECRET_KEY='replace-with-a-long-random-secret'
export APP_PORT=199
```

也可以写入项目根目录 `.env` 文件，由应用启动时自动加载。

### 3. 初始化管理员

```bash
python manage.py reset-admin --username admin --password YourStrongPassword
```

### 4. 启动

```bash
bash start.sh
```

或：

```bash
uvicorn main:app --host 0.0.0.0 --port 199
```

## 五、Linux 服务器常驻部署（systemd）

以下示例以 `/opt/zhulin-ai` 为部署目录，适用于 Ubuntu 20.04+、CentOS 7+ 等主流发行版。

### 5.1 拉取代码并安装依赖

```bash
# 创建部署目录
mkdir -p /opt/zhulin-ai
cd /opt/zhulin-ai

# 克隆代码（或上传代码包）
git clone https://github.com/logos-cn/zhulin-ai.git .

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 5.2 配置环境变量

```bash
# 生成 JWT 密钥
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 创建 .env 文件
cat > /opt/zhulin-ai/.env << EOF
APP_NAME=竹林 AI
APP_ENV=production
DATABASE_URL=sqlite:///./data/bamboo_ai.db
JWT_SECRET_KEY=你的 JWT 密钥
APP_PORT=199
HOST=0.0.0.0
WORLD_EXTRACTION_MAX_WORKERS=3
EOF
```

### 5.3 创建数据和日志目录

```bash
mkdir -p /opt/zhulin-ai/data /opt/zhulin-ai/logs
chown -R $USER:$USER /opt/zhulin-ai/data /opt/zhulin-ai/logs
```

### 5.4 初始化管理员

```bash
cd /opt/zhulin-ai
source .venv/bin/activate
python manage.py reset-admin --username admin --password YourStrongPassword
```

### 5.5 创建 systemd 服务

仓库已提供模板文件 `deploy/systemd/zhulin-ai.service`，可复制到 `/etc/systemd/system/zhulin-ai.service`：

```bash
sudo cp deploy/systemd/zhulin-ai.service /etc/systemd/system/
```

或手动创建：

```ini
[Unit]
Description=Zhulin AI FastAPI Service
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/zhulin-ai
Environment=APP_ENV=production
Environment=HOST=0.0.0.0
Environment=APP_PORT=199
EnvironmentFile=-/opt/zhulin-ai/.env
ExecStart=/bin/sh -c '/opt/zhulin-ai/.venv/bin/python -m uvicorn main:app --host "${HOST:-0.0.0.0}" --port "${APP_PORT:-199}"'
Restart=always
RestartSec=5
StandardOutput=append:/opt/zhulin-ai/logs/systemd.log
StandardError=append:/opt/zhulin-ai/logs/systemd.err

# 安全加固
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 5.6 启用并启动服务

```bash
# 重载 systemd 配置
sudo systemctl daemon-reload

# 启用开机自启
sudo systemctl enable zhulin-ai

# 启动服务
sudo systemctl start zhulin-ai

# 查看状态
sudo systemctl status zhulin-ai

# 查看日志
sudo journalctl -u zhulin-ai -f
```

### 5.7 常用运维命令

```bash
# 重启服务
sudo systemctl restart zhulin-ai

# 停止服务
sudo systemctl stop zhulin-ai

# 查看实时日志
sudo journalctl -u zhulin-ai -f

# 查看错误日志
sudo journalctl -u zhulin-ai -p err

# 检查端口占用
sudo netstat -tlnp | grep 199

# 健康检查
curl http://127.0.0.1:199/healthz
```

## 六、Docker 部署

### 6.1 构建镜像

项目根目录已提供 `Dockerfile`，可直接构建镜像：

```bash
cd /opt/zhulin-ai
docker build -t zhulin-ai:latest .
```

### 6.2 运行容器

```bash
docker run -d \
  --name zhulin-ai \
  -p 199:199 \
  -e APP_ENV=production \
  -e JWT_SECRET_KEY='replace-with-a-long-random-secret' \
  -e DATABASE_URL='sqlite:///./data/bamboo_ai.db' \
  -v /opt/zhulin-ai/data:/app/data \
  -v /opt/zhulin-ai/logs:/app/logs \
  zhulin-ai:latest
```

### 6.3 Docker Compose 部署（推荐）

创建 `docker-compose.yml`：

```yaml
version: '3.8'

services:
  zhulin-ai:
    image: zhulin-ai:latest
    container_name: zhulin-ai
    restart: always
    ports:
      - "199:199"
    environment:
      - APP_ENV=production
      - JWT_SECRET_KEY=${JWT_SECRET_KEY}
      - DATABASE_URL=sqlite:///./data/bamboo_ai.db
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./.env:/app/.env:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:199/healthz"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

启动：

```bash
docker-compose up -d
```

### 6.4 建议

- 将 `data/` 和 `logs/` 映射为宿主机卷
- 不要把真实 API Key 写进镜像，使用 `-e` 或 `.env` 注入
- 启用 healthcheck 便于监控

## 七、Nginx 反向代理示例

如果你想通过域名访问，可使用如下 Nginx 配置：

```nginx
server {
    listen 80;
    server_name your-domain.example.com;

    client_max_body_size 200m;

    location / {
        proxy_pass http://127.0.0.1:199;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 八、日志与数据

默认目录：

- 数据库：`./data/bamboo_ai.db`
- 导入缓存：`./data/world_imports/`
- 日志：`./logs/`

建议：

- 定期备份 `data/`
- 在升级前先备份数据库文件
- 日志目录交给 logrotate 或系统清理策略

## 九、升级建议

升级步骤建议：

1. 备份 `data/` 与 `logs/`
2. 停止服务
3. 拉取新代码
4. 安装依赖
5. 启动服务
6. 检查 `/healthz`

命令示例：

```bash
sudo systemctl stop zhulin-ai
cd /opt/zhulin-ai
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl start zhulin-ai
curl http://127.0.0.1:199/healthz
```

## 十、阿里云服务器部署实战

### 10.1 服务器配置建议

| 配置 | 推荐 | 说明 |
|------|------|------|
| CPU | 2 核 + | 世界观提取需要较多 CPU |
| 内存 | 4GB+ | Python + SQLite + 后台任务 |
| 系统 | Ubuntu 22.04 LTS | 推荐使用 LTS 版本 |
| 磁盘 | 40GB+ SSD | 数据库 + 日志 + 导入文件 |

### 10.2 安全组配置

在阿里云控制台开放以下端口：

| 端口 | 用途 | 授权对象 |
|------|------|----------|
| 22 | SSH | 你的 IP |
| 199 | 应用端口 | 0.0.0.0/0 或特定 IP |
| 80/443 | HTTP/HTTPS（可选） | 0.0.0.0/0 |

### 10.3 一键部署脚本

创建 `deploy.sh`：

```bash
#!/bin/bash
set -e

DEPLOY_DIR="/opt/zhulin-ai"
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

echo "🚀 开始部署竹林 AI..."

# 创建目录
mkdir -p $DEPLOY_DIR
cd $DEPLOY_DIR

# 克隆代码
git clone https://github.com/logos-cn/zhulin-ai.git .

# 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 创建 .env
cat > .env << EOF
APP_NAME=竹林 AI
APP_ENV=production
DATABASE_URL=sqlite:///./data/bamboo_ai.db
JWT_SECRET_KEY=$JWT_SECRET
APP_PORT=199
HOST=0.0.0.0
WORLD_EXTRACTION_MAX_WORKERS=3
EOF

# 创建目录
mkdir -p data logs

# 初始化管理员
read -p "设置管理员密码：" -s ADMIN_PASSWORD
echo ""
python manage.py reset-admin --username admin --password $ADMIN_PASSWORD

# 配置 systemd
sudo cp deploy/systemd/zhulin-ai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable zhulin-ai
sudo systemctl start zhulin-ai

echo "✅ 部署完成！"
echo "📍 访问地址：http://$(curl -s ifconfig.me):199"
echo "👤 管理员：admin"
echo "🔑 请妥善保管 .env 文件中的 JWT_SECRET_KEY"
```

执行：

```bash
curl -O https://raw.githubusercontent.com/logos-cn/zhulin-ai/main/deploy/deploy.sh
chmod +x deploy.sh
sudo ./deploy.sh
```

## 十一、数据库备份配置

### 11.1 自动备份（管理后台）

1. 登录管理员后台
2. 进入「后台选项」→「数据库备份」
3. 配置：
   - **备份间隔**: 24 小时
   - **保留天数**: 7 天
   - 点击「保存设置」

### 11.2 手动备份

```bash
# systemd 部署
sudo systemctl stop zhulin-ai
cp /opt/zhulin-ai/data/bamboo_ai.db /opt/zhulin-ai/data/bamboo_ai.db.backup.$(date +%Y%m%d)
sudo systemctl start zhulin-ai

# Docker 部署
docker cp zhulin-ai:/app/data/bamboo_ai.db ./bamboo_ai.db.backup.$(date +%Y%m%d)
```

### 11.3 恢复数据库

```bash
# 停止服务
sudo systemctl stop zhulin-ai

# 恢复备份
cp /opt/zhulin-ai/data/bamboo_ai.db.backup.20260405 /opt/zhulin-ai/data/bamboo_ai.db

# 启动服务
sudo systemctl start zhulin-ai
```

## 十二、监控与告警

### 12.1 健康检查端点

```bash
# 基础健康检查
curl http://127.0.0.1:199/healthz

# 详细状态（需登录）
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:199/api/v1/admin/status
```

### 12.2 日志监控

```bash
# 查看错误日志
tail -f /opt/zhulin-ai/logs/*.log | grep ERROR

# 查看 AI 调用日志
tail -f /opt/zhulin-ai/logs/ai_service.log

# 查看世界观提取日志
tail -f /opt/zhulin-ai/logs/world_extraction.log
```

### 12.3 资源监控

```bash
# 查看进程状态
systemctl status zhulin-ai

# 查看内存占用
ps aux | grep uvicorn

# 查看磁盘使用
df -h /opt/zhulin-ai/data
```

## 十三、安全加固建议

### 13.1 生产环境必做

- ✅ 使用强 JWT_SECRET_KEY（至少 32 字节随机）
- ✅ 不要使用默认端口 199（改为非常用端口）
- ✅ 配置 Nginx 反向代理 + HTTPS
- ✅ 限制 CORS 白名单为具体域名
- ✅ 定期备份数据库
- ✅ 限制登录失败次数（已默认开启）

### 13.2 可选加固

- 配置 Fail2Ban 防止暴力破解
- 使用 Cloudflare 等 CDN 隐藏真实 IP
- 配置数据库加密（SQLite + SQLCipher）
- 启用服务器防火墙（UFW/iptables）

```bash
# UFW 示例
sudo ufw allow 22/tcp
sudo ufw allow 199/tcp
sudo ufw enable
```

## 十四、故障排查

### 14.1 服务无法启动

```bash
# 查看详细错误
sudo journalctl -u zhulin-ai -n 50

# 检查端口占用
sudo lsof -i :199

# 检查 .env 文件
cat /opt/zhulin-ai/.env

# 手动启动测试
cd /opt/zhulin-ai
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 199
```

### 14.2 数据库锁定

```bash
# 查看锁定进程
lsof /opt/zhulin-ai/data/bamboo_ai.db

# 重启服务释放锁
sudo systemctl restart zhulin-ai

# 检查 WAL 文件
ls -lh /opt/zhulin-ai/data/*.db-wal
```

### 14.3 AI 调用失败

1. 检查 AI 配置是否正确
2. 查看 `logs/ai_service.log`
3. 测试 API 连通性：
   ```bash
   curl -X POST https://your-ai-provider.com/v1/chat/completions \
     -H "Authorization: Bearer YOUR_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"test"}]}'
   ```

## 十五、已知边界

- 当前默认数据库为 SQLite，适合中小规模部署（并发 < 100）
- 超长原著导入提取会显著消耗 CPU、网络与磁盘 I/O
- 大规模并发场景建议迁移到 PostgreSQL
- 当前已提供基础 `Dockerfile` 和 `docker-compose.yml` 示例
