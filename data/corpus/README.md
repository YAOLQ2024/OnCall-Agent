# 知识库父子召回

LangSmith 接入、服务启动、重建索引和逐项验证命令见
[全链路追踪与验收](../../docs/observability.md)。

父子召回已经接入应用服务，使用既有 HTTP 接口，不提供独立命令行入口。

## 调用链

上传或目录索引：`app/api/file.py` → `VectorIndexService` → `DocumentLoaderService`
→ `ParentDocumentService.index_document()` → LangChain `ParentDocumentRetriever.add_documents()`。

对话和 AIOps：`retrieve_knowledge` → `ParentDocumentService.retrieve()`
→ 原生 Retriever 的子向量匹配、父 ID 去重和父文档回取 → Agent 上下文。

`vector_store_manager.py` 管理子向量存储；`parent_document_service.py` 管理
框架配置、父存储、文件索引版本和旧版本清理。切分和父子回取使用 LangChain 原生组件。

## 切分与 metadata

两层均使用 LangChain `MarkdownTextSplitter`。默认父块目标大小 1600 字符、不重叠；
子块 400 字符、重叠目标 80 字符。框架优先尝试 Markdown 标题等分隔符，
再递归细分；不保证代码围栏完整，也不自动添加 `h1`～`h4` 章节字段。
大小按字符计算，不是 token 数，也不是已评测的最优参数。

上传的 Markdown/文本由框架 `TextLoader` 读取，再补充实际路径、文件名、
文档标题、原文 ID 和摘要。标题优先从 Markdown 一级标题提取，没有时使用文件名。
`data/corpus/processed` 中的已登记文档还会读取 `sources.toml` 的来源 URL 和语言。
来源 URL 是采集记录，不由切分器猜测。

父子文档均继承原文 metadata，Retriever 为子块添加 `parent_id`。
父 ID 作为父存储的键，使用框架公开 `ids` 参数传入，以便同源更新时清理旧父文档；
框架仍负责父子切分、关联和写入。父文档本身不要求具有 `parent_id` metadata。
父块 `start_index` 相对原文，子块 `start_index` 相对父块。
继承的 `content_sha256` 表示整篇原文版本，不表示分片摘要。

## 配置与数据

沿用项目当前的 `QWEN_API_KEY`、`QWEN_API_BASE`、`QWEN_EMBEDDING_MODEL`、
`QWEN_EMBEDDING_DIMENSIONS`、`MILVUS_HOST` 和 `MILVUS_PORT`。
父子召回配置均有默认值，可在 `.env` 中覆盖：

```dotenv
RAG_COLLECTION_NAME=oncall_knowledge_children
RAG_STORAGE_PATH=data/knowledge
RAG_PARENT_CHUNK_SIZE=1600
RAG_CHILD_CHUNK_SIZE=400
RAG_CHILD_CHUNK_OVERLAP=80
RAG_TOP_K=3
RAG_STORAGE_TIMEOUT=120
```

子向量保存在 `RAG_COLLECTION_NAME`；本地目录为
`RAG_STORAGE_PATH/<collection>/`，包含父文档 `parents/` 和来源版本登记 `sources.sqlite`。
相对存储路径按项目根目录解析。备份和部署时，Milvus、父文件和登记数据库必须配套保留。
当前方案适用于共享这些本地文件的同一主机进程，不支持多个节点各自保留独立父存储。

文件更新先写新版本，再提交可见版本。查询只包含登记为当前、且配置指纹匹配的版本。
切换等待旧读取事务完成，之后删除旧子向量和旧父文件。清理失败保留队列，
在下次更新或应用启动时重试，旧版本不重新进入检索。
写入中抛错时保留旧版本，并清理未发布的新版本。
Milvus 和文件存储不属于同一个分布式事务；进程被强制终止时可能留下不可见的孤立数据，
本实现没有定时孤立数据扫描器。

更新 embedding 模型或切分参数后，旧配置的记录不会参与新配置检索，需要重新索引。
向量维度或集合 schema 不兼容时明确报错，不自动删除或重建集合。

## 上线迁移与验收

1. 使用项目环境启动 Milvus 和现有 FastAPI 服务：

   ```powershell
   python -m uvicorn app.main:app --host 0.0.0.0 --port 9900
   ```

2. 在 `/docs` 使用现有 `/api/upload` 上传文档，或使用 `/api/index_directory`
   重建目录。`directory_path` 是查询参数。例如重建已清洗语料：

   ```powershell
   Invoke-RestMethod -Method Post -Uri "http://localhost:9900/api/index_directory?directory_path=data%2Fcorpus%2Fprocessed"
   ```

   也可把目录设置为 `aiops-docs` 或 `uploads`。目录索引递归处理 `.md`、`.txt`。
   旧单层 `biz` 和此前独立入口生成的数据不会自动迁移或删除。

3. 上传成功响应包含 `data.index.parent_count` 和索引版本；索引失败返回 HTTP 500，
   不再把索引失败作为成功返回。失败时文件可能已保存，但旧可见索引仍保留。
4. 用现有对话和 AIOps 接口触发 `retrieve_knowledge`，检查引用来源和完整父上下文。
   `RAG_TOP_K` 是子文档命中数，父文档去重后返回数量可以更少。
5. 覆盖上传同名文件，确认后续查询只使用新版本；重启后父文档仍应可以回取。

已有回归用例覆盖框架父子关联、去重、同源替换、写入失败、清理重试、重启、
索引和 Agent 工具路由、上传失败响应。测试用向量存储替身隔离外部服务，
不验证真实 embedding 质量或 Milvus 行为。由使用者在项目环境执行：

```powershell
uv sync --locked --extra dev
python -m pytest tests/test_parent_document_service.py
```

本次仅完成代码编写和静态审阅，尚未执行这些测试或真实服务验收。
