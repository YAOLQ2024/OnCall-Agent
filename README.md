# OnCall Aegnt

> 企业级智能对话和运维助手，支持 RAG 知识库问答和 AIOps 智能诊断

检索增强按有界历史改写、条件 HyDE、父子召回、千问 API 精排分层实现。
设计取舍见 [架构决策](docs/retrieval-architecture.md)，安装、配置及页面验收见
[开发与验收](docs/retrieval-development.md)。精排调用 `qwen3.7-text-rerank`，复用千问密钥，无需本地权重。

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)
[![LangChain](https://img.shields.io/badge/LangChain-latest-orange.svg)](https://www.langchain.com/)

## ✨ 核心特性

- 🤖 **智能对话** - LangChain 多轮对话 + 流式输出
- 📚 **RAG 问答** - LangChain 父子召回，支持文档上传、父文档持久化和文件索引更新
- 🔧 **AIOps 诊断** - Plan-Execute-Replan 自动故障诊断和根因分析
- 🌐 **Web 界面** - 现代化 UI，支持多种对话模式：快速问答/流式对话
- 🔌 **MCP 集成** - 日志查询和监控数据工具接入

## 🛠️ 技术栈

- **框架**: FastAPI + LangChain + LangGraph
- **LLM**: 千问 AI 平台（OpenAI 兼容接口）
- **向量库**: Milvus
- **工具协议**: MCP (Model Context Protocol)

## 🚀 快速开始

### 环境要求
- Python 3.10+
- 千问 AI 平台 API Key（在千问 AI 平台控制台创建）

### 安装和启动

#### Linux/macOS 环境

```bash
# 1. 克隆项目
git clone <repository_url>
cd super_biz_agent_py

# 2. 安装依赖（推荐使用 uv）
# 方式 1: 使用 uv（推荐，更快）
pip install uv
uv venv
source .venv/bin/activate
uv pip install -e .

# 方式 2: 使用 pip
pip install -e .

# 3. 编辑配置文件
# 首次使用需要编辑 .env 文件，填入你的 QWEN_API_KEY
vim .env  # 或使用其他编辑器

# 4. 一键初始化（启动 Docker + 服务 + 上传文档）
make init

# 5. 一键启动
make start
```

#### Windows 环境（PowerShell/CMD）

如果Windows 不支持 `make` 命令，可以手动执行以下步骤以启动服务：

```powershell
# 1. 克隆项目
git clone <repository_url>
cd super_biz_agent_py

# 2. 创建虚拟环境并安装依赖
# 方式 1: 使用 uv（推荐，更快）
pip install uv
# 创建虚拟环境
uv venv
# 激活虚拟环境
.venv\Scripts\activate
# 安装所有依赖
uv pip install -e .

# 方式 2: 使用 pip
python -m venv .venv
.venv\Scripts\activate
pip install -e .

# 3. 编辑配置文件
# 使用记事本或其他编辑器打开 .env 文件，填入你的 QWEN_API_KEY
notepad .env

# 4. 启动 Docker Desktop
# 确保 Docker Desktop 已安装并正在运行

# 5. 启动 Milvus 向量数据库（Docker Compose）
docker compose -f vector-database.yml up -d

# 6. 等待 Milvus 启动完成（约 5-10 秒）
timeout /t 10

# 7. 启动主服务；内置 MCP 在后台通过框架 stdio 自动管理，无需额外终端
# 注意：日志会自动输出到 logs\app_YYYY-MM-DD.log
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900

# 8. 浏览器打开 http://localhost:9900，点击左侧“知识库”上传文档或重建目录索引
```

**Windows 一键启动脚本**（推荐）

使用启动脚本：

```powershell
# 启动所有服务
.\start-windows.bat

# 停止所有服务
.\stop-windows.bat
```

### 访问服务
- **Web 界面**: http://localhost:9900
- **API 文档**: http://localhost:9900/docs

页面左侧的 **知识库** 支持以下操作，无需通过 API 文档提交：

- **上传文档**：选择 UTF-8 编码的 `.md` 或 `.txt` 文件，单个不超过 10 MB。
  上传后自动加载、父子切分、向量化并保存，面板显示文件名和父文档数量。同名文件更新已有内容。
- **重建目录索引**：默认 `data/corpus/processed`，也可填写 `uploads`、`aiops-docs`
  或其他服务端目录；相对路径按从项目根目录启动服务解析。递归索引后显示总数、成功数、失败数、耗时和失败原因。
  该操作更新目录中现存的文件，不清空整个知识库，也不移除已从磁盘删除的文件索引。

任务执行期间禁止在本页重复提交，关闭面板不会取消任务，重新打开可查看结果；刷新或关闭浏览器
会丢失本页结果，不能据此判断后台操作是否结束。索引成功后直接在聊天页面提问。
输入框“更多选项 → 上传文件”也可打开同一上传流程。

左侧 **MCP 工具状态** 显示 CLS、Monitor 的可用状态、已发现的工具名和失败原因，
并提供“重新检测工具”。工具发现完全在后台执行；某个 MCP 不可用时只移除该服务的工具，
知识库问答继续工作，恢复后后续请求自动使用恢复的工具，并保留原会话。

`MCP_AUTO_START=true` 为默认值：内置本机 HTTP 地址会转为 LangChain MCP 的 stdio 配置，
使用当前 Python 解释器自动启动本地脚本。主应用启动时建立每个服务的 MCP 会话并发现工具，
后续工具调用复用会话；应用关闭时统一关闭会话并清理 stdio 子进程。故障服务会在后台重连，
健康服务不会因定时检查而重复启动子进程；这不是常驻在 8003/8004 端口的 HTTP 服务。
远程 HTTP MCP 地址保持原样。要连接独立部署的本机 HTTP 服务，设置 `MCP_AUTO_START=false`。

## 📡 API 接口

### 核心接口

| 功能 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 普通对话 | POST | `/api/chat` | 一次性返回 |
| 流式对话 | POST | `/api/chat_stream` | SSE 流式输出 |
| AIOps 诊断 | POST | `/api/aiops` | 自动故障诊断（流式） |
| 文件上传 | POST | `/api/upload` | 上传并索引文档 |
| 目录索引 | POST | `/api/index_directory` | 重建目录父子索引 |
| 工具状态 | GET | `/api/tools/status` | 读取缓存的 MCP 工具状态 |
| 工具检测 | POST | `/api/tools/refresh` | 提交后台重新检测，立即返回 202 |
| 知识检索 | POST | `/api/knowledge/search` | 子文档匹配并返回父文档 |
| 健康检查 | GET | `/health` | 服务状态检查 |

### 使用示例

```bash
# 普通对话
curl -X POST "http://localhost:9900/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"Id":"session-123","Question":"你好"}'

# 流式对话
curl -X POST "http://localhost:9900/api/chat_stream" \
  -H "Content-Type: application/json" \
  -d '{"Id":"session-123","Question":"你好"}' \
  --no-buffer

# AIOps 诊断
curl -X POST "http://localhost:9900/api/aiops" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"session-123"}' \
  --no-buffer
```

## 📁 项目结构

```
super_biz_agent_py/
├── app/                                    # 应用核心
│   ├── __init__.py                         # 包初始化（自动加载日志配置）
│   ├── main.py                             # FastAPI 应用入口
│   ├── config.py                           # 配置管理（环境变量、MCP 服务器配置）
│   ├── api/                                # API 路由层
│   │   ├── __init__.py
│   │   ├── chat.py                         # 对话接口（RAG 聊天）
│   │   ├── aiops.py                        # AIOps 接口（故障诊断）
│   │   ├── file.py                         # 文件管理（文档上传）
│   │   └── health.py                       # 健康检查（服务状态）
│   ├── services/                           # 业务服务层
│   │   ├── __init__.py
│   │   ├── rag_agent_service.py            # RAG Agent（LangGraph 状态图）
│   │   ├── aiops_service.py                # AIOps 服务（计划-执行-重规划）
│   │   ├── vector_store_manager.py         # 向量存储管理器
│   │   ├── vector_embedding_service.py     # 向量embedding服务
│   │   ├── vector_index_service.py         # 向量索引服务
│   │   ├── document_loader_service.py      # 文档加载和来源 metadata
│   │   └── parent_document_service.py      # 原生父子召回与文件索引版本管理
│   ├── agent/                              # Agent 模块
│   │   ├── __init__.py
│   │   ├── mcp_client.py                   # MCP 客户端（工具调用）
│   │   └── aiops/                          # AIOps 核心逻辑
│   │       ├── __init__.py
│   │       ├── planner.py                  # 计划制定器
│   │       ├── executor.py                 # 步骤执行器
│   │       ├── replanner.py                # 重规划器
│   │       ├── state.py                    # 状态定义
│   │       └── utils.py                    # 工具函数
│   ├── models/                             # 数据模型层
│   │   ├── __init__.py
│   │   ├── aiops.py                        # AIOps 模型
│   │   ├── document.py                     # 文档模型
│   │   ├── request.py                      # 请求模型
│   │   └── response.py                     # 响应模型
│   ├── tools/                              # Agent 工具集
│   │   ├── __init__.py
│   │   ├── knowledge_tool.py               # 知识库查询工具
│   │   └── time_tool.py                    # 时间工具
│   ├── core/                               # 核心组件
│   │   ├── __init__.py
│   │   ├── llm_factory.py                  # LLM 工厂（模型管理）
│   │   └── milvus_client.py                # Milvus 客户端
│   └── utils/                              # 工具类
│       ├── __init__.py
│       └── logger.py                       # 日志配置（Loguru）
├── static/                                 # Web 前端（纯静态）
│   ├── index.html                          # 主页面
│   ├── app.js                              # 前端逻辑
│   └── styles.css                          # 样式表
├── mcp_servers/                            # MCP 服务器
│   ├── cls_server.py                       # CLS 日志查询服务
│   ├── monitor_server.py                   # 监控数据服务
│   └── README.md                           # MCP 服务说明
├── aiops-docs/                             # 运维知识库（Markdown 文档）
├── logs/                                   # 日志目录（Loguru 自动创建）
│   └── app_YYYY-MM-DD.log                  # 按天轮转的日志文件
├── uploads/                                # 上传文件临时目录
├── volumes/                                # Milvus 数据持久化目录
├── .env                                    # 环境变量配置（需手动创建）
├── Makefile                                # 项目管理命令（Linux/macOS）
├── start-windows.bat                       # Windows 启动脚本
├── stop-windows.bat                        # Windows 停止脚本
├── vector-database.yml                     # Milvus Docker Compose 配置
├── pyproject.toml                          # 项目配置（依赖、元数据）
├── uv.lock                                 # uv 依赖锁定文件
├── pyrightconfig.json                      # Pyright 类型检查配置
└── README.md                               # 项目说明
```

## ⚙️ 配置说明

LangSmith 配置、启动顺序、索引重建和父子召回验收命令见
[全链路追踪与验收](docs/observability.md)。配置模板见 [.env.example](.env.example)。

通过 `.env` 文件配置：

```bash
# 千问 AI 平台配置（必填）
QWEN_API_KEY=your-api-key
QWEN_API_BASE=https://maas.qianwenaiapi.com/compatible-mode/v1
QWEN_CHAT_MODEL=qwen3.8-max
QWEN_EMBEDDING_MODEL=qwen3.7-text-embedding-flash
QWEN_EMBEDDING_DIMENSIONS=1024

# Milvus 配置
MILVUS_HOST=localhost
MILVUS_PORT=19530

# RAG 配置
RAG_TOP_K=3
RAG_COLLECTION_NAME=oncall_knowledge_children
RAG_STORAGE_PATH=data/knowledge
RAG_PARENT_CHUNK_SIZE=1600
RAG_CHILD_CHUNK_SIZE=400
RAG_CHILD_CHUNK_OVERLAP=80
RAG_STORAGE_TIMEOUT=120

# LangSmith（启用时填写独立的 LangSmith 密钥）
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=OncallAgent
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

上传和目录索引由 `ParentDocumentRetriever.add_documents()` 完成父子切分、子向量入库及
父文档存储。`retrieve_knowledge` 使用该 Retriever 回取父文档，统一供对话 Agent 和
AIOps 使用；不再走单层向量检索。详细的数据更新机制及迁移步骤见
[知识库说明](data/corpus/README.md)。旧单层 `biz` 集合不会自动转换或删除，需要从原文重建新索引。

快速与流式聊天使用 LangChain 模型调用中间件，对同轮多次检索返回的父文档整理上下文：
首次出现保留正文，后续重复命中改为资料编号引用，新资料继续追加。按用户消息划分轮次，
不同问题不共享去重集合；原始工具结果、artifact 和检查点仍保留完整内容。
LangSmith 的 `rag.deduplicate_context` 显示本轮去重统计，最内层聊天模型输入显示实际上下文。
该处理兼容已有索引，无需重新向量化，详见[追踪与验证说明](docs/observability.md)。

## 🎯 AIOps 智能运维

基于 **Plan-Execute-Replan** 模式实现自动故障诊断。

### 核心特性
- ✅ 自动制定诊断计划（Planner）
- ✅ 智能工具调用（Executor）
- ✅ 动态调整步骤（Replanner）
- ✅ 流式输出诊断过程
- ✅ 生成结构化报告

### 快速测试

```bash
# 服务已通过 make init 自动启动
# 如需重启服务：make restart

# 访问 Web 界面，点击"智能运维与诊断工具"
# 或使用 API
curl -X POST "http://localhost:9900/api/aiops" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test"}' \
  --no-buffer
```

### 诊断流程
```
1. Planner 制定计划 → 生成 4-6 个诊断步骤
2. Executor 执行步骤 → 调用 MCP 工具（日志查询、监控数据）
3. Replanner 评估结果 → 决定继续/调整/生成报告
4. 输出诊断报告 → 根因分析 + 运维建议
```

## 📝 开发指南

### 常用命令

```bash
# 项目管理
make init              # 一键初始化（Docker + 服务 + 文档）
make start             # 启动所有服务
make stop              # 停止所有服务
make restart           # 重启所有服务

# 依赖管理
make install-dev       # 安装开发依赖
make sync              # 同步依赖

# Docker 管理
make up                # 启动 Docker 容器
make down              # 停止 Docker 容器

# 代码质量
make format            # 格式化代码
make lint              # 代码检查
```


## 🐛 常见问题

### Windows 环境问题

#### 1. `make` 命令不可用
Windows 不支持 `make` 命令，请使用提供的批处理脚本：
```powershell
# 启动服务
.\start-windows.bat

# 停止服务
.\stop-windows.bat
```

#### 2. PowerShell 执行策略限制
如果遇到 "无法加载文件，因为在此系统上禁止运行脚本" 错误：
```powershell
# 临时允许脚本执行（管理员权限）
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process

# 或者使用 CMD 而不是 PowerShell
cmd
.\start-windows.bat
```

#### 3. 端口被占用（Windows）
```powershell
# 查看占用端口的进程
netstat -ano | findstr :9900

# 结束进程（替换 PID 为实际进程 ID）
taskkill /F /PID <PID>
```

### 通用问题

### API Key 错误
```bash
# 检查环境变量
cat .env | grep QWEN_API_KEY    # Linux/macOS
type .env | findstr QWEN_API_KEY  # Windows
```

### Milvus 连接失败
```bash
# 确保本机有 Docker 服务并且已经启动（可以使用 Docker Desktop）

# 检查 Milvus 状态
docker ps | grep milvus

# 重启 Milvus（使用 docker compose）
docker compose -f vector-database.yml restart

# 或者重启单个服务
docker compose -f vector-database.yml restart standalone
```

### 服务无法启动

**Linux/macOS:**
```bash
# 查看服务日志
tail -f logs/app_$(date +%Y-%m-%d).log  # FastAPI 主服务（Loguru 日志）
tail -f mcp_cls.log                      # CLS MCP 服务
tail -f mcp_monitor.log                  # Monitor MCP 服务

# 检查端口占用
lsof -i :9900  # FastAPI
lsof -i :8003  # CLS MCP
lsof -i :8004  # Monitor MCP
```

**Windows:**
```powershell
# 查看服务日志（获取今天的日期）
$today = Get-Date -Format "yyyy-MM-dd"
type logs\app_$today.log  # FastAPI 主服务（Loguru 日志）
type mcp_cls.log          # CLS MCP 服务
type mcp_monitor.log      # Monitor MCP 服务

# 或者查看最新的日志文件
Get-ChildItem logs\*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content -Tail 50

# 检查端口占用
netstat -ano | findstr :9900  # FastAPI
netstat -ano | findstr :8003  # CLS MCP
netstat -ano | findstr :8004  # Monitor MCP
```

## 📚 参考资源

- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [LangChain 文档](https://python.langchain.com/)
- [LangGraph Plan-Execute](https://langchain-ai.github.io/langgraph/tutorials/plan-and-execute/)
- [千问 AI 平台文档](https://platform.qianwenai.com/docs/)
- [MCP 协议](https://modelcontextprotocol.io/)

## 📄 许可证
作者：[YAOLQ2024](https://github.com/YAOLQ2024/OnCall-Agent)

MIT License
