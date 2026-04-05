# 竹林 AI (Bamboo AI)

> 面向中文长篇创作场景的本地化多用户 AI 小说辅助写作软件

**当前版本**: v0.2.0  
**最后更新**: 2026-04-05  
**GitHub**: https://github.com/logos-cn/zhulin-ai  
**许可证**: MIT

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)

本项目提供账号管理、书库、章节写作、AI 续写、大纲/摘要生成、人物卡与关系网提取、历史快照、全局小说助手等能力，适合个人工作站、小团队内网和自托管部署。

## 当前能力

- 多用户账号体系
  - 不开放外部注册，仅管理员在后台创建账号
  - 支持 `manage.py` 命令行初始化或重置超级管理员
- 小说创作工作台
  - 书籍、章节、正文、大纲、摘要管理
  - AI 上下文预览
  - 第一版草稿生成
  - 二次扩写 / 二次精简后再确认写入
  - AI 生成前全屏竹叶特效开关
- 世界观与关系网
  - 从软件内全书提取人物卡、关系、世界观事实
  - 从外部 `txt / docx / pdf` 导入原著并后台提取
  - 冲突确认、批量保留软件内 / 采用原著
  - 主要人物默认显示，支持切换显示次要人物
  - 支持人物卡工作台、阵营管理、关系事件时间线
- AI 配置中心
  - 兼容 OpenAI v1 风格接口
  - 支持按功能绑定不同服务商、模型、超时和优先级
  - 支持自动拉取模型列表，服务商不返回模型列表时可手填模型名
  - 支持 API Key 加密存储与普通用户出站地址限制
- 历史版本
  - AI 写入前自动留存快照
  - 支持查看正文 / 大纲 / 摘要的历史内容
- 项目运维
  - 管理后台支持 SQLite 自动备份、手动备份、备份恢复
  - 支持导出 / 导入书籍工程包，便于迁移与协作
- 全局 AI 助手
  - 全站右下角浮窗
  - 支持剧情趋势分析、最近章节点评、世界观冲突批量处理

## 技术栈

- 后端：Python 3.9+ / FastAPI
- 数据库：SQLite / SQLAlchemy ORM
- 前端：HTML / TailwindCSS / Vanilla JS
- 运行方式：Windows / Linux / macOS（Intel）
- 部署支持：本地运行 / systemd / Docker

## 目录说明

```text
.
├─ main.py                     # FastAPI 入口
├─ manage.py                   # 管理员初始化/重置命令
├─ models.py                   # SQLAlchemy ORM 模型
├─ ai_service.py               # AI 调度、Prompt 组装、生成逻辑
├─ world_extraction_service.py # 世界观后台提取任务
├─ database.py                 # 数据库与 SQLite 连接配置
├─ security.py                 # bcrypt/JWT
├─ logging_setup.py            # 日志初始化
├─ static/                     # 前端页面、脚本、样式
├─ data/                       # SQLite 数据库、导入中间文件
├─ deploy/systemd/zhulin-ai.service # systemd 服务模板
├─ Dockerfile                  # 容器镜像构建文件
└─ docs/DEPLOYMENT.md          # 详细部署说明
```

## 快速启动

### 🚀 5 分钟快速开始

```bash
# 1. 克隆代码
git clone https://github.com/logos-cn/zhulin-ai.git
cd zhulin-ai

# 2. 安装依赖
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，修改 JWT_SECRET_KEY 为随机字符串

# 4. 初始化管理员
python manage.py reset-admin --username admin --password YourPassword

# 5. 启动服务
uvicorn main:app --host 0.0.0.0 --port 199
```

访问：http://127.0.0.1:199/login

### 🐳 Docker 快速部署

```bash
docker run -d \
  --name zhulin-ai \
  -p 199:199 \
  -e JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))") \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/logos-cn/zhulin-ai:latest
```

### 📜 一键部署脚本（Linux 服务器）

```bash
curl -O https://raw.githubusercontent.com/logos-cn/zhulin-ai/main/deploy/deploy.sh
chmod +x deploy.sh
sudo ./deploy.sh
```

详见：[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#十阿里云服务器部署实战)

## 部署文档

详细部署方法见：

- **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** - 完整部署指南

内容包括：

- ✅ Windows / Linux / macOS 本地部署
- ✅ Linux 服务器 systemd 常驻部署（含一键部署脚本）
- ✅ Docker & Docker Compose 部署
- ✅ 阿里云服务器实战指南
- ✅ Nginx 反向代理配置
- ✅ 环境变量完整说明
- ✅ 数据库备份与恢复
- ✅ 监控告警与日志管理
- ✅ 安全加固建议
- ✅ 故障排查手册

## 🔒 敏感信息与私有资料

- 仓库默认不会提交 `.env`、`.env.*`、私钥文件、`*.private.json`、`*.local.json`
- 你的 AI API Key 请仅放在运行环境变量或本地 `.env` 中，不要写入代码或提交到仓库
- 本地导入的原著文本、文风素材、私有提示词文件建议放在 `data/` 或你自己的本地目录，不要纳入版本控制

## ⚠️ 生产环境注意事项

- 生产环境不要使用默认的 `JWT_SECRET_KEY`
- SQLite 适合中小规模部署（并发 < 100）；若后续并发继续增长，可迁移到 PostgreSQL
- 外部文档导入提取会消耗较多内存和算力，建议为后台任务预留资源
- 当前已为 SQLite 增加 `WAL` 与 `busy_timeout`，但超大规模并发写入仍不建议长期依赖 SQLite
- 定期备份数据库（管理后台支持自动备份）

## 🗺️ 路线说明

当前仓库以可运行的本地化创作工具为目标，优先保证：

- ✅ 自托管可部署
- ✅ 多用户可用
- ✅ 中文小说创作体验
- ✅ OpenAI 兼容接口可接入

后续可继续补充：

- [ ] Docker Compose 编排与镜像发布
- [ ] 更细的章节树拖拽管理
- [ ] 更强的 AI 助手可执行动作
- [ ] 更大规模文档提取的流式与分层存储方案
- [ ] PostgreSQL 迁移支持

---

## 📚 相关文档

- **[部署指南](docs/DEPLOYMENT.md)** - 完整部署与运维文档
- **[迭代日志](docs/ITERATIONS.md)** - 版本更新记录
- **[系统服务模板](deploy/systemd/zhulin-ai.service)** - systemd 配置示例

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件
