# 查询改写入口

上一版未提交的 MultiQuery 单轮查询扩展已由架构审查后的实现替代。
当前采用有界历史消歧、按条件 HyDE 双路召回、父文档合并及千问 API 精排。

- 设计、数据契约和降级规则：[架构决策](retrieval-architecture.md)。
- 模型准备、配置、页面与 LangSmith 验收：[开发文档](retrieval-development.md)。

`RAG_QUERY_REWRITE_COUNT` 不再生效。无历史时跳过改写，有历史时先用规则判断是否需要改写，触发后生成一条独立 query，
不再为每次工具调用无条件扩展两条同义查询。HyDE 与精排默认关闭，配置后显式启用。

当前规则按顺序检查：指代/依赖表达触发；无依赖表达且以 CLI 命令开头时跳过；
不超过 10 字符的短查询触发；超过 25 字符且无依赖表达时跳过；其余情况保守触发。
这是启发式判断，长句不保证自包含，短句也可能完整，不能宣称准确识别所有上下文依赖。
LangSmith 的 `rag.query_rewrite` 输出中可查看 `rewrite_status=skipped:...` 的跳过原因。
