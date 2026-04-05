# 竹林 AI 部署说明

本文档给出本地运行、Linux 服务器常驻部署和基础运维建议。

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

关键变量如下：

| 变量名 | 说明 | 建议值 |
| --- | --- | --- |
| `APP_NAME` | 应用名称 | `竹林 AI` |
| `APP_ENV` | 运行环境 | `production` |
| `DATABASE_URL` | 数据库连接串 | `sqlite:///./data/bamboo_ai.db` |
| `JWT_SECRET_KEY` | JWT 密钥 | 使用长随机字符串 |
| `JWT_ALGORITHM` | JWT 算法 | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 登录有效期 | `1440` |
| `CORS_ALLOW_ORIGINS` | CORS 白名单 | 按需配置 |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_DIR` | 日志目录 | `logs` |
| `APP_PORT` | 启动端口 | `199` |
| `HOST` | 监听地址 | `0.0.0.0` |
| `WORLD_EXTRACTION_MAX_WORKERS` | 原著导入提取并发数上限 | `1000` |

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

## 五、Linux 服务器常驻部署

以下示例以 `/opt/zhulin-ai` 为部署目录。

### 1. 拉取代码并安装依赖

```bash
mkdir -p /opt/zhulin-ai
cd /opt/zhulin-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 初始化管理员

```bash
cd /opt/zhulin-ai
source .venv/bin/activate
python manage.py reset-admin --username admin --password YourStrongPassword
```

### 3. 创建 systemd 服务

仓库已提供模板文件 [deploy/systemd/zhulin-ai.service](/C:/Users/刘哲言/desktop/NOVA/deploy/systemd/zhulin-ai.service)，可复制到 `/etc/systemd/system/zhulin-ai.service` 后再按实际用户与目录修改。

```ini
[Unit]
Description=Zhulin AI FastAPI Service
After=network.target

[Service]
Type=simple
User=zhulin
Group=zhulin
WorkingDirectory=/opt/zhulin-ai
Environment=APP_ENV=production
Environment=HOST=0.0.0.0
Environment=APP_PORT=199
EnvironmentFile=-/opt/zhulin-ai/.env
ExecStart=/bin/sh -c '/opt/zhulin-ai/.venv/bin/python -m uvicorn main:app --host "${HOST:-0.0.0.0}" --port "${APP_PORT:-199}"'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable zhulin-ai
sudo systemctl start zhulin-ai
sudo systemctl status zhulin-ai
```

## 六、Docker 部署

项目根目录已提供 `Dockerfile`，可直接构建镜像：

```bash
cd /opt/zhulin-ai
docker build -t zhulin-ai:latest .
```

运行示例：

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

建议：

- 将 `data/` 和 `logs/` 映射为宿主机卷
- 不要把真实 API Key 写进镜像，使用 `-e` 或 `.env` 注入
- 若后续要启用多容器编排，可再追加 `docker-compose.yml`

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

## 十、已知边界

- 当前默认数据库为 SQLite，适合中小规模部署
- 超长原著导入提取会显著消耗 CPU、网络与磁盘 I/O
- 当前已提供基础 `Dockerfile`，但尚未补 `docker-compose.yml`、镜像发布流水线与容器级健康探针
