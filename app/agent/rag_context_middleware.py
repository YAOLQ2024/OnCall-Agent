"""在模型输入层按本轮问答去重父文档，完整工具结果继续保留在消息状态中。"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.documents import Document
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langsmith import traceable

from app.tools.knowledge_tool import format_docs


@dataclass(frozen=True)
class PreparedRagContext:
    messages: list[BaseMessage]
    input_parent_occurrences: int
    unique_parents: int
    duplicate_occurrences: int
    changed_tool_messages: int
    references: list[dict]


def _trace_output(result: PreparedRagContext | None) -> dict:
    if result is None:
        return {}
    return {
        "statistics_scope": "latest_user_turn",
        "input_parent_occurrences": result.input_parent_occurrences,
        "unique_parents": result.unique_parents,
        "duplicate_occurrences": result.duplicate_occurrences,
        "changed_tool_messages": result.changed_tool_messages,
        "references": result.references,
    }


def _parent_key(document: Document) -> tuple[str, str, str]:
    content_hash = hashlib.sha256(document.page_content.encode("utf-8")).hexdigest()
    parent_id = document.id or document.metadata.get("parent_id")
    if parent_id:
        # 同 ID 的正文若已发生变化，也保留新内容。
        return "parent", str(parent_id), content_hash
    # 兼容缺少 ID 的历史消息：只合并正文和 metadata 完全相同的文档。
    try:
        metadata = json.dumps(document.metadata, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        # 无法规范序列化时保守地保留该文档，不能因去重影响正常回答。
        return "opaque", str(id(document)), content_hash
    return "document", hashlib.sha256(metadata.encode("utf-8")).hexdigest(), content_hash


@traceable(
    name="rag.deduplicate_context", run_type="chain",
    process_inputs=lambda inputs: {"message_count": len(inputs.get("messages", []))},
    process_outputs=_trace_output,
)
def prepare_rag_context(messages: Sequence[BaseMessage]) -> PreparedRagContext:
    """各轮独立生成视图，统计最新用户轮次；不修改检查点或检索 artifact。"""
    prepared = list(messages)
    turn_start = next(
        (index + 1 for index in range(len(messages) - 1, -1, -1)
         if isinstance(messages[index], HumanMessage)),
        0,
    )
    seen: dict[tuple[str, str, str], int] = {}
    references = []
    occurrences = duplicates = changed = 0
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            # 历史轮次也独立整理，避免下一轮把历史重复正文重新发给模型。
            seen.clear()
            continue
        current_turn = index >= turn_start
        if (
            not isinstance(message, ToolMessage) or message.name != "retrieve_knowledge"
            or message.status == "error" or not message.artifact
        ):
            continue
        documents = message.artifact
        if not isinstance(documents, (list, tuple)) or not all(
            isinstance(document, Document) for document in documents
        ):
            # 无法可靠解释结构时保留原文，避免把未知结果误删。
            continue
        new_documents, numbers, repeated = [], [], []
        for document in documents:
            if current_turn:
                occurrences += 1
            key = _parent_key(document)
            if key in seen:
                if current_turn:
                    duplicates += 1
                if seen[key] not in repeated:
                    repeated.append(seen[key])
                continue
            number = len(seen) + 1
            seen[key] = number
            new_documents.append(document)
            numbers.append(number)
            if current_turn:
                references.append({
                    "reference_number": number,
                    "parent_id": document.id or document.metadata.get("parent_id"),
                    "first_tool_call_id": message.tool_call_id,
                })
        parts = []
        if new_documents:
            parts.append(format_docs(new_documents, reference_numbers=numbers))
        else:
            parts.append("本次检索无新增资料。")
        if repeated:
            labels = "、".join(f"【参考资料 {number}】" for number in repeated)
            parts.append(f"重复命中：{labels}。正文与来源已在本轮前文提供，请直接使用。")
        content = "\n\n".join(parts)
        if content != message.content:
            # 保留 id、tool_call_id、name、status 和完整 artifact，满足工具消息协议。
            prepared[index] = message.model_copy(update={"content": content})
            if current_turn:
                changed += 1
    return PreparedRagContext(prepared, occurrences, len(seen), duplicates, changed, references)


class RagContextDedupMiddleware(AgentMiddleware):
    """使用 LangChain 官方模型调用扩展点，仅替换本次请求的消息视图。"""

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        context = prepare_rag_context(request.messages)
        return handler(request.override(messages=context.messages))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        context = prepare_rag_context(request.messages)
        return await handler(request.override(messages=context.messages))
