# LangSmith 全链路追踪与父子召回验收

应用通过 LangChain 原生 tracing 和 LangSmith `traceable` 接入追踪。
自定义节点只记录真实方法调用，没有重复查询、重放结果或增加文档打印日志。
此文中的终端命令由部署者执行；本次代码交付没有启动服务或向外部服务发送数据。

## 1. 配置 LangSmith

在 LangSmith 控制台创建 API Key，在项目现有 `.env` 末尾添加以下配置，
不要用示例文件覆盖已有千问密钥。配置模板也见项目 `.env.example`。

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=替换为你的LangSmith密钥
LANGSMITH_PROJECT=OncallAgent
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_HIDE_INPUTS=false
LANGSMITH_HIDE_OUTPUTS=false
```

Endpoint 使用控制台为账号区域提供的地址。组织级或关联多个工作区的密钥，
还需按控制台要求填写 `LANGSMITH_WORKSPACE_ID`。
LangSmith Key 和千问 Key 是不同的密钥。

应用启动时将这些配置导出给 SDK；仅让 Pydantic 读取 `.env` 不会设置 SDK 环境变量。
修改配置后需要重启应用。关闭时将 `LANGSMITH_TRACING` 改为 `false`。
开启后，请求、文档、上下文和消息会发送到所配置的 LangSmith 服务。
隐藏输入或输出会影响本次验收所需的文档和工具内容可见性。

官方说明：[LangChain 追踪](https://docs.langchain.com/langsmith/trace-with-langchain)、
[检索文档追踪格式](https://docs.langchain.com/langsmith/log-retriever-trace)。

## 2. 准备环境和 Milvus

PowerShell 中切到项目根目录：

```powershell
Set-Location 'E:\DataSets\研究生三年各种文件\研二下\实习\OncallAgent'
uv sync --locked --extra dev
.\.venv\Scripts\Activate.ps1
docker compose -f vector-database.yml up -d
docker compose -f vector-database.yml ps
```

等待 etcd、minio、standalone 的健康状态就绪。保留现有 QWEN 和 MILVUS 配置，
确认千问接口可调用、Milvus 地址可连接。LangSmith 不替代这些依赖。

## 3. 启动服务

在项目根目录的已激活环境中运行：

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900
```

启动阶段在主应用事件循环内构造 LangChain Milvus 集成，因为当前版本会同时创建同步、
异步 Milvus 客户端。同步索引和检索继续在线程池中执行，复用已初始化对象；
退出时在所属事件循环内关闭异步客户端，再关闭同步客户端。
正常启动不应再出现 `Cannot create async connection: no running event loop` 告警。

另一个 PowerShell 窗口验证：

```powershell
Invoke-RestMethod 'http://127.0.0.1:9900/health' | ConvertTo-Json -Depth 8
```

主应用启动后在后台发现 MCP 工具，聊天初始化只读取可用工具快照，不等待 MCP。
默认 `MCP_AUTO_START=true`：内置本机地址自动转为 LangChain MCP 的 stdio 模式，
框架使用当前解释器启动脚本、管理会话并清理子进程，无需额外终端。
后台管理器为每个 MCP 服务保持一个长连接会话。工具发现时加载并缓存工具定义，
后续调用复用同一会话；stdio 模式下对应子进程在主应用运行期间保持启动，应用退出时统一清理。
每个会话由独立后台任务拥有，SDK 上下文在同一个任务内创建和退出，避免多个取消作用域交错关闭。

页面左侧 **MCP 工具状态** 显示 CLS、Monitor 的状态、工具名和失败原因，
可点击“重新检测工具”。失败服务每 30 秒自动重连；恢复后后续请求自动加入工具，保留会话。
已有工具调用发生连接错误或超时后，也会移除对应服务的工具、关闭旧会话并触发后台重连；
其他 MCP 和知识库不受影响。健康会话会持续复用，不会因定时检查而重复启动 stdio 子进程。
每隔 `MCP_REFRESH_INTERVAL` 使用现有会话发送心跳，空闲时断连也能触发隔离和重连。
点击“重新检测工具”会重新读取健康会话的工具列表；只对已失效会话建立新连接。
模型会被告知缺失的能力，不应把知识库建议冒充实时日志或指标。

LangSmith 中 `agent.load_mcp_tools` 是后台工具发现节点，其输入 `server_name` 标明服务。
它可能作为独立 trace 出现，不要求位于聊天 trace 下。`agent.initialize` 负责按快照构建 Agent。
某个 MCP 发现节点失败与 `agent.chat` 成功可以同时成立。

外部 HTTP 地址保持原配置，由部署系统管理其服务进程；要保留内置本机 HTTP 连接，
设 `MCP_AUTO_START=false`，这时才需要独立部署 CLS/Monitor。
`MCP_DISCOVERY_TIMEOUT`（默认 10 秒）、`MCP_TOOL_TIMEOUT`（30 秒）限制发现与调用耗时；
`MCP_REFRESH_INTERVAL`（30 秒）控制后台重试间隔。修改后重启主应用。

## 4. 重建父子索引

在浏览器打开 `http://localhost:9900`，点击左侧 **知识库**：

1. 已有语料：在“文档目录”保留 `data/corpus/processed`，点击“重建目录索引”。
2. 本机新文档：点击“选择文档并建立索引”，选择 UTF-8 编码的 `.md` / `.txt` 文件（最大 10 MB）。
3. 等面板显示完成，再关闭面板，在聊天页面提问。

目录操作显示文件总数、成功数、失败数及失败文件详情。空目录和部分失败会单独提示。
上传操作显示生成的父文档数量；只有父子索引建立完成才提示成功。
所填目录位于运行后端的电脑上；索引按文件更新，不清空整个知识库，不移除磁盘已删除文件的索引。
执行时可关闭面板后重新打开查看结果，但请保留当前页面，刷新会丢失本页任务状态。

如果需要单独检查接口，也可以使用命令递归索引已经清洗的语料：

```powershell
$indexResult = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:9900/api/index_directory?directory_path=data%2Fcorpus%2Fprocessed'
$indexResult | ConvertTo-Json -Depth 10
```

验收：`data.total_files` 大于 0，`data.success_count` 大于 0，`data.fail_count` 为 0。
有失败时查看 `data.failed_files`。也可以把目录参数改为 `aiops-docs` 或 `uploads`。
同源重新索引会切换版本，不需要删除整个集合。

默认新集合为 `oncall_knowledge_children`。旧单层 `biz` 数据不自动转换。
父存储和来源登记在 `data/knowledge/<collection>/`，需与 Milvus 配套持久化。

LangSmith 的 `OncallAgent` 项目中应出现 `knowledge.index_directory`，其下可查看
`knowledge.index_file`、`documents.load`、`rag.index_document`、`rag.index_children`、
`embedding.documents`、`rag.store_parents` 等实际执行的节点。
Embedding 输出记录条数和维度，不上传数值向量数组；输入仍保留实际文本。

## 5. 单独验证父子召回

调用正式知识检索接口，与 Agent 使用同一召回服务，不调用聊天模型或 MCP：

```powershell
$ragBody = @{ query = 'Pod 一直 Pending，应该怎么排查？'; top_k = 4 } | ConvertTo-Json
$ragResult = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:9900/api/knowledge/search' -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($ragBody))
$ragResult | ConvertTo-Json -Depth 20
```

响应 `data.documents` 是最终父文档，含 `page_content`、`metadata` 等 Document 字段。
默认父块目标大小 1600 字符，子块 400 字符；`top_k` 限制子块命中数，
父文档按 ID 去重后数量可能更少。短父块和子块可以长度相同，不靠长度判定父子关系。

在 LangSmith 项目中打开 `knowledge.search`，查看以下节点（显示层级以 SDK 实际追踪为准）：

| 节点 | 检查输入 | 检查输出 |
| --- | --- | --- |
| `rag.child_search` | 查询、k、有效索引版本过滤条件 | `documents` 中的实际命中子块，每块有 `metadata.parent_id` |
| `rag.parent_fetch` | `keys` 是从命中子块提取并去重的父 ID | 对应父文档正文和 metadata，顺序与 keys 对应 |
| `rag.parent_retriever` | 查询 | LangChain 最终返回的父文档列表 |
| `knowledge.search` | HTTP 检索请求 | 响应中的父文档列表 |

核心验收证据：命中子块的 `parent_id` 出现在 `rag.parent_fetch` 的 `keys` 中；
回取的父正文与接口返回正文一致，且包含命中的子块内容。
原生框架不要求父文档自己的 metadata 也包含 `parent_id`。
去重范围是单次检索：同一次命中的多个子块若有相同 `parent_id`，只回取一次父文档。
原生检索器不负责多次工具调用之间的去重；本项目在聊天 Agent 的模型输入层补充中间件处理。
相同正文若以不同来源或不同父 ID 入库，也不会仅因文本相同而被原生父子检索器去重。
父存储适配器会把实际存储键填入返回父文档的 `Document.id`，它与命中子块的 `parent_id` 对应；
这是读取时的适配，已有父文档无需重建索引。
不要把原文级 `document_id` 当成父块 ID，也不要把一个 `start_index` 在两层间直接比较：
父块位置相对整篇原文，子块位置相对所属父块。

若没有登记好的索引，检索会返回空列表，trace 中也不会出现实际的子查询。
若 `rag.child_search` 为空，不能据此声称已经验证父文档回取。

## 6. 验证 Agent 调 RAG 工具及模型输入

主服务启动且知识库索引成功后，在页面点击“新建对话”，发送（不要求 MCP 可用）：

> 请先调用 retrieve_knowledge 检索知识库，再根据检索证据回答：Pod 一直 Pending，应该怎么排查？请注明来源。

页面的“快速”模式对应 `agent.chat`，切到“流式”后对应 `agent.chat_stream`。
也可以单独通过接口验证，新会话示例：

```powershell
$chatBody = @{ Id = 'trace-parent-child-001'; Question = '请先调用 retrieve_knowledge 检索知识库，再根据检索证据回答：Pod 一直 Pending，应该怎么排查？请注明来源。' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:9900/api/chat' -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($chatBody)) | ConvertTo-Json -Depth 20
```

在 LangSmith 中打开 `agent.chat`，可用 `session_id` 或 `thread_id`
值 `trace-parent-child-001` 定位。流式链路名为 `agent.chat_stream`，AIOps 为 `agent.aiops`。
重复验证时换一个新的 Id，避免既有会话上下文影响模型是否再次调用工具。

依次检查模型生成的工具调用、`retrieve_knowledge`、`rag.retrieve`、
子块命中及父回取节点、`rag.format_context`，最后查看工具调用之后的下一次模型输入。
模型自主选择工具；如果没有 `retrieve_knowledge` 节点，说明这一次没有调用 RAG，
不能仅凭回答内容判断它调用过。直接检索接口用于隔离这类 Agent 决策因素。

工具函数的返回类型是 `tuple[str, list[Document]]`，即 `(context, docs)`。
`@tool(response_format="content_and_artifact")` 在 Agent 工具调用中将其包装为 ToolMessage：

- `content`：`rag.format_context` 生成的字符串，包含参考资料编号、标题、来源和父文档正文。
- `artifact`：父文档 `Document` 列表，保留结构化正文和 metadata。
- `tool_call_id`：与模型发出的那次工具调用对应。

在工具节点输出中观察这些字段。LangSmith 原始视图可能带 LangChain 序列化外壳，
关注字段逻辑，而不是要求顶层 JSON 长得完全一样。

然后打开后续模型节点的 Inputs/Messages，找到对应 ToolMessage 的 `content`。
它才是该项目向聊天模型提供的检索上下文。Trace 记录的 LangChain 消息对象可能仍显示
`artifact`；当前 ChatOpenAI 适配器构造实际 API 请求时，只发送工具消息的
`role`、`content`、`tool_call_id`，不会发送 artifact 文档对象。

LangSmith 可以观察已接入的 Agent、工具、模型和 RAG 调用；它不是逐行调试器。
未接入追踪的远程 MCP 服务内部代码、数据库执行计划、模型内部推理不会自动展示。
自定义模型名的价格与 token 统计取决于服务商返回和 LangSmith 配置，不作为本轮验收条件。

### 同轮多次检索的上下文去重

快速与流式聊天均接入 `RagContextDedupMiddleware`，使用 LangChain 官方
`wrap_model_call` / `awrap_model_call` 扩展点，在真正调用模型前执行：

1. 按用户消息分轮处理，按消息顺序保留父文档首次出现的正文和来源。
2. 用 `Document.id`（父存储键）与正文哈希判断重复；同一 ID 正文发生变化时保留新内容。
3. 后续重复出现改为 `重复命中：【参考资料 N】`，有新增文档时只追加新增正文。
4. 资料编号在本轮内统一递增；每条工具消息及其 `tool_call_id` 都保留。

示例：第一次返回 A、B，第二次返回 B、C，模型看到第一次的 A、B 正文，
第二次只看到 C 正文及指向 B 的引用。并行调用的结果在模型调用前统一整理，不使用全局去重集合。
历史各轮也独立整理，但不会跨用户轮次抑制文档；新问题仍可重新使用同一资料。
旧消息缺少 ID 时，只合并正文及 metadata 完全相同的文档。不同 ID/不同来源的相同正文保留各自证据。

原始工具 `content`、完整 `artifact` 和检查点不变，检索结果也没有少查或重放。
因此工具节点输出仍可能看到重复，这是完整取证记录。查看去重效果应检查：

- `rag.deduplicate_context`：`input_parent_occurrences`、`unique_parents`、`duplicate_occurrences`
  和首次出现的 `parent_id`、`first_tool_call_id`。这些统计对应最新一条用户消息之后的当前轮次。
- 最内层聊天模型调用的 Inputs/Messages：检查工具消息的 **content** 是否只保留一次父文档正文，
  后续重复结果是否改为资料编号引用。Agent 图节点的原始状态仍保存完整结果。
- 模型输入原始视图中的 `artifact` 仍可能重复，但当前适配器不把它发给模型。

去重减少重复正文占用的模型输入，不减少 Agent 发出的查询次数，也不节省这些查询的嵌入和检索调用。
该策略不是相似内容压缩、摘要或跨轮记忆清理。
扩展点说明：[LangChain 自定义中间件](https://docs.langchain.com/oss/python/langchain/middleware/custom)。

页面验收时，重启主服务后点击“新建对话”，发送：

> 为验证检索上下文去重，请在这一轮回答中调用 retrieve_knowledge 两次，两次 query 都严格使用“Pod Pending 排查”；再调用一次，query 使用“Pod Pending CPU 内存不足 hostPort”。完成检索后汇总排查建议并注明来源。

模型自主决定工具调用，先确认 trace 中实际出现至少两次 `retrieve_knowledge`，
且返回结果存在相同 ID 和正文；只有一次调用或没有重复命中时，本次请求不能验证跨调用去重。
查看最后一次 `rag.deduplicate_context`，而不是首次模型调用前文档数仍为 0 的节点。
本轮应满足 `input_parent_occurrences = unique_parents + duplicate_occurrences`；
确有重复命中时 `duplicate_occurrences` 应大于 0。
原始各工具消息的 artifact 中同一个 `Document.id` 可以多次出现；
最终聊天模型输入的工具消息 content 中，该父文档全文在当前轮次只出现一次，其他位置为资料编号引用。

## 7. 回归与排查

验证 MCP 降级不必停止已有服务。可以临时把 `.env` 中的 MCP 配置改为下列不可达地址，
重启主应用后查看页面状态，并继续发送上面的 Pod 问题：

```dotenv
MCP_AUTO_START=false
MCP_CLS_URL=http://127.0.0.1:65533/mcp
MCP_MONITOR_URL=http://127.0.0.1:65534/mcp
```

前提是这两个端口在本机确实没有服务。预期：页面显示两个 MCP 不可用，
快速/流式聊天仍能调用 `retrieve_knowledge`，LangSmith 能看到父子召回链路。
这验证的是 MCP 隔离，仍需 Milvus、索引和聊天模型正常。
验证后恢复原 MCP URL 和 `MCP_AUTO_START=true`，重启主应用，工具应自动重新发现。
同一个已运行主应用中的临时连接故障恢复，可通过“重新检测工具”或后台重试恢复，
不需要清空会话；修改配置文件本身仍需要重启。

离线测试默认禁用远程 trace；追踪用例使用 SDK local 模式验证节点结构，不连接 LangSmith：

```powershell
python -m pytest tests/test_parent_document_service.py tests/test_tracing.py tests/test_milvus_timeout.py tests/test_mcp_initialization.py tests/test_rag_context_middleware.py tests/test_vector_store_lifecycle.py
```

追踪页面无记录时依次检查：是否在正确项目和时间范围、服务是否在配置修改后启动、
endpoint 是否匹配账号区域、密钥和 workspace 是否匹配、应用能否访问 LangSmith API。
观察文档为空还需确认没有启用隐藏输入/输出。
正常关闭服务会 flush SDK 缓冲区；强制结束进程可能丢失尚未提交的追踪。

2026-10-20 离线运行记录：使用项目 Windows Python 3.12.15 执行以下命令，结果为 28 passed：

```powershell
python -m pytest -o addopts='' -v tests/test_rag_context_middleware.py tests/test_tracing.py tests/test_parent_document_service.py
```

其中原生 Agent 集成用例模拟两轮共 5 次工具调用，原始父文档累计出现 12 次、
实际模型输入保留 4 个不同父文档的正文各一次，原始工具结果与检查点完整保留。
测试使用假模型和内存/临时文件存储，远程 tracing 禁用；没有连接真实 Milvus、
调用真实聊天/嵌入接口或向 LangSmith 上传记录。该结果验证代码逻辑与 Agent 接线，
真实页面链路仍按上述 trace 对照步骤验收。
运行时未安装覆盖率插件，命令用 `-o addopts=''` 覆盖项目默认覆盖率参数。
