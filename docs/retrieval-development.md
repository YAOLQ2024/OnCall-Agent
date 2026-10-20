# 检索增强开发与验收

本文件依赖 [架构决策](retrieval-architecture.md)，先写文档，再实施以下改造。

## 文件与顺序

1. 新建 `app/services/retrieval_prompts.py`：历史消歧和 HyDE 提示，明确历史及假设不是事实。
2. `qwen_rerank.py` 适配千问原生 API；`rerank_service.py` 负责客户端复用、原生 CrossEncoderReranker 和单次分数观测。
3. 改造 `knowledge_retrieval_service.py`：替换未提交的 MultiQuery 扩展，LCEL 分流、并行、合并及回退。
4. `knowledge_tool.py` 注入 ToolRuntime，提取当前轮之前的历史；保持工具名及返回协议。
5. 检索 HTTP 接口支持结构化历史但不自己读写会话；普通页面问答使用原图状态。
6. 修改配置与 `.env.example`；真实 `.env` 不代改。移除本地精排依赖并更新锁文件。
7. 用离线测试覆盖决策、输入输出、隔离、失败与已有功能；真实模型及应用由用户运行。

## 配置 API（由用户执行）

项目目录 PowerShell：

```powershell
uv sync --locked
```

重排复用现有 `QWEN_API_KEY`，无需下载权重，无需本地模型依赖。
移除了上一版的 rerank extra 和本地路径/设备/max_length 配置；旧配置不再生效。
默认端点按[千问模型页面](https://www.qianwenai.com/models/qwen3.7-text-rerank)的 DashScope 协议实现，
不要改成聊天用的 `/compatible-mode/v1` 或猜测 `/rerank` 路径。

在本机 `.env` 配置：

```dotenv
RAG_QUERY_REWRITE_ENABLED=true
RAG_QUERY_REWRITE_TIMEOUT=20
RAG_HYDE_ENABLED=true
RAG_HYDE_TIMEOUT=20
RAG_RERANK_ENABLED=true
QWEN_RERANK_MODEL=qwen3.7-text-rerank
QWEN_RERANK_URL=https://maas.qianwenaiapi.com/api/v1/services/rerank/text-rerank/text-rerank
QWEN_RERANK_TIMEOUT=20
RAG_RERANK_TOP_N=3
```

不要先设评分阈值；返回的分数尺度与具体模型有关。
把 `RAG_HYDE_ENABLED=false` 可单独验证“改写＋直接召回＋精排”；
把精排也关闭可验证历史改写和既有父子召回。无需重建索引。

改写和 HyDE 的模型请求显式设置 `extra_body={"enable_thinking": False}`，
使用当前平台支持关闭思考的千问模型；该项在代码中限定于这两条轻量生成链，
无需新增 `.env` 配置。Agent 主回答的默认请求不额外添加该参数。
不要把 QwQ 等仅思考模型直接替换进这两条链；参数不兼容时会记录失败并回退。
[平台思考模式说明](https://platform.qianwenai.com/docs/developer-guides/text-generation/thinking)。

## 页面与 LangSmith

由用户启动：

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900
```

在页面新建会话，按顺序提问：

1. “请查知识库：payment-service 的 Pod 一直 Pending，应该怎么排查？”
2. “它为什么会这样？先查看哪些事件？”
3. “请查知识库解释 `ERR_PAYMENT_9021`，找不到就明确说找不到。”
4. “请查知识库：为什么集群会出现级联故障，通常如何排查？”

这些问题测试链路行为；若未索引相关文档，不能把模型通用回答算作知识库命中。
Agent 必须实际调用工具才进入本次验收。精确错误码绕过 HyDE不等于能在纯向量检索中保证精确匹配。
客户端懒初始化并复用连接池，退出应用时关闭；每次精排会产生一次真实 API 请求。

在工具内部观察：

- `rag.query_rewrite`：历史范围、原 query、结果；无历史时无生成模型子节点。
- `rag.route`：为什么跳过/启用 HyDE、改写是否退化、精排是否启用及客户端是否可构建。
- `rag.hyde_generation`：生成的假设段落；不得出现在最终工具 artifact 或作为来源。
- `rag.retrieval_routes`：direct/hyde 两路父文档；仍通过 `rag.child_search` / `rag.parent_fetch` 回取。
- `rag.merge_candidates`：去重后的候选 ID；不改原文档 metadata。
- `rag.rerank`：分数、排序前后 ID、退化原因；评分问题为独立 query，不用 HyDE 文本评分。
- `rag.rerank_api`：候选数量、请求耗时和原输入顺序的分数；失败保留异常，外层负责退化。
- 工具最终 artifact：最多 top_n 篇真实父文档。
- 最后 `rag.deduplicate_context`：继续验证同轮多次工具检索的重复正文压缩。

## 离线检查与回归

```powershell
.venv\Scripts\python.exe -m pytest -o addopts='' -q tests
```

测试不得联网下载权重，不调用实际 Qwen/Milvus，不上传测试追踪。
必须覆盖同步/异步结果一致、历史注入且不暴露给模型参数、不同请求不共享历史、
精确查询绕过、生成失败、精排 API 超时/鉴权/限流/结构错误、阈值为空、直接存储错误不被吞掉。

真实验收记录表：问题、预期文档 ID/来源、命中、排序前后、各节点耗时、父文档字符数。
分四种配置做比较，保留失败样本；不把假模型测试结果写成真实效果提升。

## 本次交付检查

2026-10-20：上一版本地方案离线回归为 `82 passed in 7.30s`；本次 API 改造另行记录结果。
本次 API 改造全量离线测试为 `93 passed in 11.81s`，包含真实 SDK 的 MockTransport 协议测试、
乱序索引映射、响应校验、HTTP 401/429/500 降级及超时不自动重试。
包括原生 Agent 的 ToolRuntime 注入、LCEL 并行与异步线程隔离、原生精排单次打分及排序观测、
各分支失败降级和既有父子/上下文去重/MCP/存储生命周期回归。
`uv lock --check` 和 `git diff --check` 通过。
本次 API 协议测试使用真实 SDK 与 MockTransport，未发外部请求。
未启动应用，未做真实模型/Milvus 联调或性能评测。

2026-10-20 思考模式加固：全量离线回归 `96 passed in 9.42s`。
新增测试核对改写/HyDE 经 LangChain 和 SDK 发出的真实 HTTP JSON：
enable_thinking=false、512 token 预算、非流式；普通 Agent 工厂请求不额外加入此参数。
未调用真实平台 API，不将这次协议测试表述为已实测降低延迟或完全消除截断。

2026-10-20 按需改写版本提交前，全量离线回归 `97 passed in 7.03s`；
新增有历史时独立命令及完整长问题跳过改写模型的验证。真实检索质量与规则误判仍需评测。
