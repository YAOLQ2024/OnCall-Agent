"""工业级三级上下文压缩器 (Hot-Warm-Cold Context Compactor)。

融合开源界最佳实践：
1. Stage 1 (微观确定性剪枝 - 0 次 LLM 调用, <1ms)：
   - 借鉴 OpenHands (原 OpenDevin) 异常逆向贪婪捕获：锁定 ERROR/Exception/Panic/OOMKilled 等关键行及其上下文窗口。
   - 借鉴 Princeton SWE-agent 行级滑动视窗：保留首部 (Head 50 行) 与尾部 (Tail 100 行) 关键现场。
   - JSON 语法安全折叠：对 Prometheus/K8s API 返回的大型 JSON 数组做结构化折叠，严防切碎花括号导致下游解析崩溃。
   - 遵守 LangChain/OpenAI 工具消息协议不变量 (Invariant)：原位替换 ToolMessage.content，100% 保留 tool_call_id、name 与 status。
2. Stage 2 (宏观结构化工作记忆提炼 - 高水位按需单次轻量 LLM)：
   - 仅当 Stage 1 剪枝后总上下文仍突破 High Watermark (默认 48,000 Tokens) 时触发。
   - 调用轻量模型并强制关闭深度思考 (enable_thinking=False)，通过 Pydantic 提取结构化 SRE 排障工作记忆 (SREIncidentMemory)。
3. Stage 3 (LangGraph 原子轮次更迭 - 0 次 LLM 调用)：
   - 按“原子对话轮次 (Human -> AI tool_calls -> Tool -> AI)”整体淘汰旧轮次，严防孤立 ToolMessage 触发 API 400 报错。
   - 支持通过 RemoveMessage(id=...) 原子更新 LangGraph Checkpointer 状态，或通过 AgentMiddleware 动态替换模型输入视图。
"""

from __future__ import annotations

import json
import re
import asyncio
from contextvars import ContextVar
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable
from loguru import logger
from pydantic import BaseModel, Field

from app.config import config
from app.core.llm_factory import llm_factory


# =====================================================================
# 1. 快速 Token 估算器 (纯 CPU 纳秒级计算，适配中英混合日志与代码)
# =====================================================================

_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """快速估算文本 Token 数量（中英混合 SRE 日志/代码场景）。

    规则：
    - 中文字符约 0.65 Token/字
    - 英文/代码/日志字符约 0.28 Token/字符 (约 3.5 字符/Token)
    """
    if not text:
        return 0
    cjk_count = len(_CJK_PATTERN.findall(text))
    ascii_count = len(text) - cjk_count
    return max(1, int(cjk_count * 0.65 + ascii_count * 0.28))


def estimate_message_tokens(message: BaseMessage) -> int:
    """估算单条 LangChain 消息的 Token 开销（含协议头、正文及 tool_calls 载荷）。"""
    tokens = 4  # 基础 role/message 协议帧开销
    content = getattr(message, "content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                tokens += estimate_tokens(str(block.get("text", "") or block))
            else:
                tokens += estimate_tokens(str(block))

    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        try:
            tokens += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
        except Exception:
            tokens += estimate_tokens(str(tool_calls))

    return tokens


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """估算整个消息序列的总 Token 数。"""
    return sum(estimate_message_tokens(m) for m in messages)


# =====================================================================
# 2. Stage 1：工业级工具输出确定性剪枝器 (OpenHands + SWE-agent 算法)
# =====================================================================

class SREToolOutputCompressor:
    """工业级 SRE 工具输出剪枝器 (0 次 LLM 调用，耗时 < 1ms)。

    核心保障：
    1. 关键异常特征行（ERROR / Exception / Panic / OOMKilled / CrashLoopBackOff 等）100% 免疫截断。
    2. 结构化 JSON 语法安全折叠，绝不切碎花括号或数组边界。
    3. 首尾现场锚定（Head 50 行 + Tail 100 行），保留命令启动参数与最终退出状态码。
    4. 极端单行超长文本（如 30 万字符 minified JSON/日志）字符级兜底防护。
    """

    ERROR_KEYWORDS = re.compile(
        r"(?i)(error|exception|failed|failure|fatal|panic|crashloopbackoff|"
        r"oomkilled|imagepullbackoff|errimagepull|timeout|timed out|"
        r"connection refused|permission denied|segfault|traceback|caused by|"
        r"status=[45]\d{2}|\b[45]\d{2}\b\s+(?:bad gateway|gateway timeout|internal server error))"
    )

    COMPACTED_MARKER = "[⚡ OnCallAgent 剪枝："

    @classmethod
    def is_already_compacted(cls, content: str) -> bool:
        """检查内容是否已经执行过剪枝，避免重复处理。"""
        return cls.COMPACTED_MARKER in content or '"_oncall_compactor"' in content

    @classmethod
    def compress(
        cls,
        content: str,
        max_chars: Optional[int] = None,
        head_lines: Optional[int] = None,
        tail_lines: Optional[int] = None,
        error_radius: Optional[int] = None,
    ) -> str:
        """对超长工具输出执行确定性结构化剪枝。"""
        if not content or not isinstance(content, str):
            return content

        max_chars = max_chars if max_chars is not None else config.compactor_tool_char_threshold
        head_lines = head_lines if head_lines is not None else config.compactor_head_lines
        tail_lines = tail_lines if tail_lines is not None else config.compactor_tail_lines
        error_radius = (
            error_radius if error_radius is not None else config.compactor_error_context_radius
        )

        # 未达剪枝门槛或已剪枝，直接原样返回
        if len(content) <= max_chars or cls.is_already_compacted(content):
            return content

        # 路径 A：尝试 JSON 结构安全折叠 (如 Prometheus 矩阵 / K8s API items 列表)
        trimmed_json = cls._try_compress_json(content, max_chars=max_chars)
        if trimmed_json is not None and len(trimmed_json) <= max_chars:
            return trimmed_json

        lines = content.splitlines()
        total_lines = len(lines)
        keep_budget = head_lines + tail_lines

        # 如果行数很少但字符极长（例如单行 30 万字符的 minified 数据）
        if total_lines <= keep_budget:
            return cls._enforce_char_ceiling(content, max_chars)

        # 路径 B：OpenHands 逆向异常贪婪提取 (锁定所有报错行及其前后上下文)
        error_indices = [
            idx for idx, line in enumerate(lines) if cls.ERROR_KEYWORDS.search(line)
        ]

        if error_indices:
            compressed_salience = cls._compress_with_error_salience(
                lines=lines,
                error_indices=error_indices,
                head_lines=min(25, head_lines),
                tail_lines=min(40, tail_lines),
                error_radius=error_radius,
                max_selected_lines=keep_budget + 80,
            )
            return cls._enforce_char_ceiling(compressed_salience, max_chars)

        # 路径 C：SWE-agent 标准行级滑动视窗 (Head + Tail)
        compressed_window = cls._compress_line_window(
            lines=lines,
            head_lines=head_lines,
            tail_lines=tail_lines,
        )
        return cls._enforce_char_ceiling(compressed_window, max_chars)

    @classmethod
    def _try_compress_json(cls, content: str, max_chars: int) -> Optional[str]:
        """对 JSON 格式输出进行语法安全的折叠，防止破坏花括号结构。"""
        stripped = content.strip()
        if not ((stripped.startswith("{") and stripped.endswith("}")) or
                (stripped.startswith("[") and stripped.endswith("]"))):
            return None

        try:
            data = json.loads(stripped)
        except Exception:
            return None

        def _fold_value(val: Any, depth: int = 0) -> Tuple[Any, bool]:
            if depth > 3:
                return val, False
            if isinstance(val, list):
                if len(val) > 6:
                    omitted = len(val) - 4
                    summary_item = {
                        "_oncall_compactor": f"{cls.COMPACTED_MARKER}已安全折叠中间 {omitted} 个同类数组元素 (总计 {len(val)} 项)]"
                    }
                    folded = val[:3] + [summary_item] + val[-1:]
                    return folded, True
                return val, False
            if isinstance(val, dict):
                modified = False
                new_dict = {}
                for k, v in val.items():
                    folded_v, child_mod = _fold_value(v, depth + 1)
                    new_dict[k] = folded_v
                    if child_mod:
                        modified = True
                return new_dict, modified
            return val, False

        folded_data, changed = _fold_value(data)
        if not changed:
            return None

        try:
            result = json.dumps(folded_data, ensure_ascii=False, indent=2)
            return result
        except Exception:
            return None

    @classmethod
    def _compress_with_error_salience(
        cls,
        lines: List[str],
        error_indices: List[int],
        head_lines: int,
        tail_lines: int,
        error_radius: int,
        max_selected_lines: int,
    ) -> str:
        """OpenHands 模式：保留首尾现场 + 所有命中异常关键字的行及其前后上下文窗口。"""
        total = len(lines)
        selected: set[int] = set()

        # 1. 始终保留首部环境头与尾部退出码现场
        selected.update(range(0, min(head_lines, total)))
        selected.update(range(max(0, total - tail_lines), total))

        # 2. 优先从靠近尾部的异常行开始逆向收集（尾部通常是 Root Cause 真实抛出点）
        for err_idx in reversed(error_indices):
            if len(selected) >= max_selected_lines:
                break
            start = max(0, err_idx - error_radius)
            end = min(total, err_idx + error_radius + 1)
            selected.update(range(start, end))

        sorted_indices = sorted(selected)
        out_lines: List[str] = []
        prev_idx = -1

        for idx in sorted_indices:
            if prev_idx != -1 and idx > prev_idx + 1:
                omitted = idx - prev_idx - 1
                out_lines.append(
                    f"... {cls.COMPACTED_MARKER}已安全折叠中间 {omitted} 行无异常日志 (总计 {total} 行)] ..."
                )
            out_lines.append(lines[idx])
            prev_idx = idx

        return "\n".join(out_lines)

    @classmethod
    def _compress_line_window(
        cls,
        lines: List[str],
        head_lines: int,
        tail_lines: int,
    ) -> str:
        """SWE-agent 模式：保留前 head_lines 行与后 tail_lines 行。"""
        total = len(lines)
        if total <= head_lines + tail_lines:
            return "\n".join(lines)

        head = lines[:head_lines]
        tail = lines[-tail_lines:]
        omitted = total - head_lines - tail_lines
        marker = (
            f"... {cls.COMPACTED_MARKER}已安全省略中间 {omitted} 行常规输出 "
            f"(总计 {total} 行，已保留首 {head_lines} 行与尾 {tail_lines} 行)] ..."
        )
        return "\n".join(head) + "\n" + marker + "\n" + "\n".join(tail)

    @classmethod
    def _enforce_char_ceiling(cls, content: str, max_chars: int) -> str:
        """极端情况兜底：若按行裁剪后字符数仍超标（如单行 30 万字符），执行安全字符窗口截断。"""
        if len(content) <= max_chars:
            return content

        head_budget = int(max_chars * 0.35)
        tail_budget = int(max_chars * 0.55)
        omitted = len(content) - head_budget - tail_budget
        return (
            content[:head_budget]
            + f"\n... {cls.COMPACTED_MARKER}单行超长文本已安全截断中间 {omitted} 字符] ...\n"
            + content[-tail_budget:]
        )


# =====================================================================
# 3. 协议安全的原子对话轮次切分器 (防止破坏 tool_call_id 配对)
# =====================================================================

def split_into_atomic_turns(
    messages: Sequence[BaseMessage],
) -> Tuple[List[BaseMessage], List[List[BaseMessage]]]:
    """将消息序列切分为“前置系统消息”与“原子对话轮次列表”。

    关键协议不变量 (OpenAI / Anthropic / Qwen API 强制要求)：
    - 每个包含 tool_calls 的 AIMessage 必须紧跟其对应的 ToolMessage(tool_call_id=...)。
    - 绝不能将 AIMessage(tool_calls) 与其 ToolMessage 拆散到不同生命周期，否则触发 HTTP 400。
    - 因此，以 HumanMessage 为每个原子轮次的起点，将该轮次内所有 AI 思考、工具调用、工具返回打包为一个不可分割的原子组。
    """
    system_messages: List[BaseMessage] = []
    turns: List[List[BaseMessage]] = []
    current_turn: List[BaseMessage] = []

    for msg in messages:
        if isinstance(msg, SystemMessage) and not turns and not current_turn:
            system_messages.append(msg)
            continue

        if isinstance(msg, HumanMessage):
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        else:
            if not current_turn:
                # 防御性处理：若历史开头出现非 Human 消息，归入首个轮次
                current_turn = [msg]
            else:
                current_turn.append(msg)

    if current_turn:
        turns.append(current_turn)

    return system_messages, turns


# =====================================================================
# 4. Stage 2 & 3：SRE 结构化工作记忆提炼与宏观状态压缩
# =====================================================================

class SREIncidentMemory(BaseModel):
    """SRE 故障排障结构化工作记忆（Cold Tier 长期记忆载体）。"""

    target_services: List[str] = Field(
        default_factory=list,
        description="当前涉及的目标服务、节点、Pod 或组件名称列表（保留精确名称）",
    )
    confirmed_symptoms: List[str] = Field(
        default_factory=list,
        description="已确认的故障现象、关键报错码、异常监控指标数值",
    )
    ruled_out_hypotheses: List[str] = Field(
        default_factory=list,
        description="已经排查验证并排除掉的故障假设（防止后续重复调用工具排查）",
    )
    executed_actions: List[str] = Field(
        default_factory=list,
        description="已执行过的关键诊断命令/工具及其核心结论摘要",
    )
    pending_next_steps: List[str] = Field(
        default_factory=list,
        description="尚未完成或下一步计划继续排查的方向",
    )

    def to_markdown(self) -> str:
        """渲染为注入 SystemMessage 的高密度结构化工作记忆。"""
        sections = ["### 🧠 [OnCallAgent 结构化历史工作记忆 (Long-Term Incident Memory)]"]
        if self.target_services:
            sections.append(f"- **故障涉及目标**: {', '.join(self.target_services)}")
        if self.confirmed_symptoms:
            sections.append("- **已确认症状与指标**:\n  - " + "\n  - ".join(self.confirmed_symptoms))
        if self.ruled_out_hypotheses:
            sections.append(
                "- **已排除的根因假设 (请勿重复排查)**:\n  - "
                + "\n  - ".join(self.ruled_out_hypotheses)
            )
        if self.executed_actions:
            sections.append("- **已执行动作与结论**:\n  - " + "\n  - ".join(self.executed_actions))
        if self.pending_next_steps:
            sections.append("- **待推进下一步**:\n  - " + "\n  - ".join(self.pending_next_steps))
        return "\n".join(sections)


COMPACTOR_SYSTEM_PROMPT = dedent("""
    你是一个专业的 SRE 故障排障工作记忆提炼器。
    请将给定的历史排障对话轮次提炼为高密度的结构化工作记忆（SREIncidentMemory）。

    严格准则：
    1. 精确保留所有服务名、Pod 名、IP、端口、错误码（如 CrashLoopBackOff, 504, OOMKilled）与关键指标数值。
    2. 明确记录哪些根因假设已经被排除，防止后续 Agent 重复调用相同工具做无用功。
    3. 严禁编造任何未在对话中出现的事实。
    4. 保持极度精炼，每条结论控制在 1 句话以内。
""").strip()


@dataclass(frozen=True)
class CompactionResult:
    """上下文压缩执行结果与可观测指标。"""

    messages: List[BaseMessage]
    original_tokens: int
    after_stage1_tokens: int
    final_tokens: int
    pruned_tool_messages: int
    macro_compacted: bool
    evicted_turn_count: int
    incident_memory: Optional[Dict[str, Any]] = None


def _trace_compaction_output(result: CompactionResult) -> Dict[str, Any]:
    return {
        "original_tokens": result.original_tokens,
        "after_stage1_tokens": result.after_stage1_tokens,
        "final_tokens": result.final_tokens,
        "pruned_tool_messages": result.pruned_tool_messages,
        "macro_compacted": result.macro_compacted,
        "evicted_turn_count": result.evicted_turn_count,
        "incident_memory": result.incident_memory,
    }


class SREContextCompactor:
    """三级上下文压缩控制器 (Hot-Warm-Cold Tier Controller)。"""

    MEMORY_TAG = "[OnCallAgent 结构化历史工作记忆"

    def __init__(
        self,
        hot_turns: Optional[int] = None,
        tool_char_threshold: Optional[int] = None,
        high_watermark_tokens: Optional[int] = None,
        target_watermark_tokens: Optional[int] = None,
    ):
        self.hot_turns = hot_turns if hot_turns is not None else config.compactor_hot_turns
        self.tool_char_threshold = (
            tool_char_threshold
            if tool_char_threshold is not None
            else config.compactor_tool_char_threshold
        )
        self.high_watermark_tokens = (
            high_watermark_tokens
            if high_watermark_tokens is not None
            else config.compactor_high_watermark_tokens
        )
        self.target_watermark_tokens = (
            target_watermark_tokens
            if target_watermark_tokens is not None
            else config.compactor_target_watermark_tokens
        )

    def prune_warm_turns(
        self,
        messages: Sequence[BaseMessage],
    ) -> Tuple[List[BaseMessage], int, int, int]:
        """执行 Stage 1 微观确定性剪枝（0 次 LLM 调用，<1ms）。

        策略：
        - 最近 `self.hot_turns` 轮对话属于热数据 (Hot Tier)，100% 原样保留，绝不截断。
        - 更早的历史轮次属于温数据 (Warm Tier)，对超长 ToolMessage 执行原位结构化剪枝。
        """
        original_tokens = estimate_messages_tokens(messages)
        sys_msgs, turns = split_into_atomic_turns(messages)

        if len(turns) <= self.hot_turns:
            return list(messages), original_tokens, original_tokens, 0

        warm_cutoff = len(turns) - self.hot_turns
        pruned_count = 0
        updated_turns: List[List[BaseMessage]] = []

        for turn_idx, turn in enumerate(turns):
            if turn_idx >= warm_cutoff:
                # 热数据保护区：100% 保留原始输出
                updated_turns.append(turn)
                continue

            # 温数据区：对大体积 ToolMessage 进行原位安全替换
            new_turn: List[BaseMessage] = []
            for msg in turn:
                if (
                    isinstance(msg, ToolMessage)
                    and isinstance(msg.content, str)
                    and len(msg.content) > self.tool_char_threshold
                ):
                    compressed_text = SREToolOutputCompressor.compress(
                        msg.content,
                        max_chars=self.tool_char_threshold,
                    )
                    if compressed_text != msg.content:
                        # 保持 id, tool_call_id, name, status, artifact 完全不变
                        msg = msg.model_copy(update={"content": compressed_text})
                        pruned_count += 1
                new_turn.append(msg)
            updated_turns.append(new_turn)

        flattened = sys_msgs + [m for t in updated_turns for m in t]
        after_tokens = estimate_messages_tokens(flattened)
        return flattened, original_tokens, after_tokens, pruned_count

    async def _distill_incident_memory(
        self,
        evicted_turns: List[List[BaseMessage]],
        existing_memory_text: str = "",
    ) -> Optional[SREIncidentMemory]:
        """调用单次轻量模型提取结构化 SRE 记忆（关闭 Thinking 模式）。"""
        serialized_lines: List[str] = []
        if existing_memory_text:
            serialized_lines.append(f"【已有历史工作记忆】:\n{existing_memory_text}\n")

        for idx, turn in enumerate(evicted_turns, 1):
            serialized_lines.append(f"--- 历史轮次 {idx} ---")
            for msg in turn:
                role = type(msg).__name__
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                # 防止单条消息过长，送入小模型前做安全窗口限制
                if len(content) > 3000:
                    content = SREToolOutputCompressor.compress(content, max_chars=3000)
                serialized_lines.append(f"[{role}]: {content}")

        transcript = "\n".join(serialized_lines)

        try:
            llm = llm_factory.create_chat_model(
                model=config.compactor_model or config.qwen_chat_model,
                temperature=0.0,
                streaming=False,
                request_timeout=config.compactor_timeout,
                extra_body={"enable_thinking": False},
            )
            prompt = ChatPromptTemplate.from_messages([
                ("system", COMPACTOR_SYSTEM_PROMPT),
                ("human", "请将以下历史排障记录提炼为结构化 SRE 工作记忆：\n\n{transcript}"),
            ])
            chain = prompt | llm.with_structured_output(SREIncidentMemory)
            memory = await chain.ainvoke({"transcript": transcript})
            if isinstance(memory, SREIncidentMemory):
                return memory
            if isinstance(memory, dict):
                return SREIncidentMemory(**memory)
        except Exception as e:
            logger.warning(f"宏观结构化记忆提炼调用轻量 LLM 失败，启用确定性规则摘要兜底: {e}")

        return self._deterministic_fallback_memory(evicted_turns)

    @staticmethod
    def _deterministic_fallback_memory(
        evicted_turns: List[List[BaseMessage]],
    ) -> SREIncidentMemory:
        """无 LLM 时的纯算法兜底摘要器（保证即使网络抖动也绝不中断主流程）。"""
        questions: List[str] = []
        actions: List[str] = []
        symptoms: List[str] = []

        for turn in evicted_turns:
            for msg in turn:
                if isinstance(msg, HumanMessage) and isinstance(msg.content, str):
                    questions.append(msg.content[:120])
                elif isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        actions.append(f"调用工具 {tc.get('name', 'unknown')}")
                elif isinstance(msg, ToolMessage) and isinstance(msg.content, str):
                    for line in msg.content.splitlines():
                        if SREToolOutputCompressor.ERROR_KEYWORDS.search(line):
                            symptoms.append(line.strip()[:150])
                            if len(symptoms) >= 5:
                                break

        return SREIncidentMemory(
            target_services=[],
            confirmed_symptoms=symptoms[:5],
            ruled_out_hypotheses=[],
            executed_actions=actions[:8] or [f"已完成 {len(evicted_turns)} 轮前置对话排查"],
            pending_next_steps=[f"历史用户核心诉求: {'; '.join(questions[:3])}"] if questions else [],
        )

    def _inject_memory_into_system(
        self,
        system_messages: List[BaseMessage],
        memory: SREIncidentMemory,
    ) -> List[BaseMessage]:
        """将提炼出的结构化记忆注入或更新至头部 SystemMessage。"""
        memory_md = memory.to_markdown()
        if not system_messages:
            return [SystemMessage(content=memory_md)]

        first_sys = system_messages[0]
        raw_content = first_sys.content if isinstance(first_sys.content, str) else str(first_sys.content)

        # 如果之前已经有结构化记忆块，替换旧记忆块
        if self.MEMORY_TAG in raw_content:
            prefix = raw_content.split("### 🧠 [OnCallAgent 结构化历史工作记忆")[0].rstrip()
            updated_content = f"{prefix}\n\n{memory_md}" if prefix else memory_md
        else:
            updated_content = f"{raw_content}\n\n{memory_md}"

        return [first_sys.model_copy(update={"content": updated_content})] + system_messages[1:]

    def _split_history(self, system_messages, turns):
        """Keep recent whole turns up to the configured target and always keep hot turns."""
        total = estimate_messages_tokens(system_messages)
        split_at = len(turns)
        for turn in reversed(turns):
            cost = estimate_messages_tokens(turn)
            if len(turns) - split_at >= self.hot_turns and total + cost > self.target_watermark_tokens:
                break
            split_at -= 1
            total += cost
        return turns[:split_at], turns[split_at:]

    @traceable(
        name="agent.context_compactor",
        run_type="chain",
        process_inputs=lambda inputs: {"message_count": len(inputs.get("messages", []))},
        process_outputs=_trace_compaction_output,
    )
    async def acompact(self, messages: Sequence[BaseMessage]) -> CompactionResult:
        """异步执行完整三级上下文压缩流水线 (Stage 1 -> 水位线判定 -> Stage 2 & 3)。"""
        if not config.compactor_enabled or not messages:
            tokens = estimate_messages_tokens(messages)
            return CompactionResult(
                messages=list(messages),
                original_tokens=tokens,
                after_stage1_tokens=tokens,
                final_tokens=tokens,
                pruned_tool_messages=0,
                macro_compacted=False,
                evicted_turn_count=0,
            )

        # Stage 1：0 成本微观确定性剪枝
        stage1_msgs, orig_tokens, stage1_tokens, pruned_tools = self.prune_warm_turns(messages)

        # 水位线检查：若未突破配置的高水位警戒线，直接 0 LLM 放行
        if stage1_tokens < self.high_watermark_tokens:
            return CompactionResult(
                messages=stage1_msgs,
                original_tokens=orig_tokens,
                after_stage1_tokens=stage1_tokens,
                final_tokens=stage1_tokens,
                pruned_tool_messages=pruned_tools,
                macro_compacted=False,
                evicted_turn_count=0,
            )

        sys_msgs, turns = split_into_atomic_turns(stage1_msgs)
        evicted_turns, kept_turns = self._split_history(sys_msgs, turns)
        evicted_count = len(evicted_turns)
        if not evicted_count:
            return CompactionResult(
                messages=stage1_msgs,
                original_tokens=orig_tokens,
                after_stage1_tokens=stage1_tokens,
                final_tokens=stage1_tokens,
                pruned_tool_messages=pruned_tools,
                macro_compacted=False,
                evicted_turn_count=0,
            )

        # 提取已有记忆文本（如有）
        existing_mem = ""
        if sys_msgs and isinstance(sys_msgs[0].content, str) and self.MEMORY_TAG in sys_msgs[0].content:
            existing_mem = sys_msgs[0].content.split("### 🧠 [OnCallAgent 结构化历史工作记忆")[-1]

        # Stage 2：单次轻量 LLM 提取结构化 SRE 工作记忆
        memory = await self._distill_incident_memory(evicted_turns, existing_memory_text=existing_mem)

        # Stage 3：原子组装新上下文
        updated_sys = self._inject_memory_into_system(sys_msgs, memory) if memory else sys_msgs
        final_messages = updated_sys + [m for t in kept_turns for m in t]
        final_tokens = estimate_messages_tokens(final_messages)

        logger.info(
            f"⚡ 上下文三级压缩完成: {orig_tokens} -> {stage1_tokens} (Stage1剪枝{pruned_tools}条) "
            f"-> {final_tokens} Tokens (归档{evicted_count}轮对话)"
        )

        return CompactionResult(
            messages=final_messages,
            original_tokens=orig_tokens,
            after_stage1_tokens=stage1_tokens,
            final_tokens=final_tokens,
            pruned_tool_messages=pruned_tools,
            macro_compacted=True,
            evicted_turn_count=evicted_count,
            incident_memory=memory.model_dump() if memory else None,
        )

    def compact_sync(self, messages: Sequence[BaseMessage]) -> CompactionResult:
        """同步执行上下文压缩（用于同步中间件入口）。"""
        if not config.compactor_enabled or not messages:
            tokens = estimate_messages_tokens(messages)
            return CompactionResult(
                messages=list(messages),
                original_tokens=tokens,
                after_stage1_tokens=tokens,
                final_tokens=tokens,
                pruned_tool_messages=0,
                macro_compacted=False,
                evicted_turn_count=0,
            )

        stage1_msgs, orig_tokens, stage1_tokens, pruned_tools = self.prune_warm_turns(messages)
        if stage1_tokens < self.high_watermark_tokens:
            return CompactionResult(
                messages=stage1_msgs,
                original_tokens=orig_tokens,
                after_stage1_tokens=stage1_tokens,
                final_tokens=stage1_tokens,
                pruned_tool_messages=pruned_tools,
                macro_compacted=False,
                evicted_turn_count=0,
            )

        sys_msgs, turns = split_into_atomic_turns(stage1_msgs)
        evicted_turns, kept_turns = self._split_history(sys_msgs, turns)
        evicted_count = len(evicted_turns)
        if not evicted_count:
            return CompactionResult(
                messages=stage1_msgs,
                original_tokens=orig_tokens,
                after_stage1_tokens=stage1_tokens,
                final_tokens=stage1_tokens,
                pruned_tool_messages=pruned_tools,
                macro_compacted=False,
                evicted_turn_count=0,
            )

        memory = self._deterministic_fallback_memory(evicted_turns)
        updated_sys = self._inject_memory_into_system(sys_msgs, memory)
        final_messages = updated_sys + [m for t in kept_turns for m in t]
        final_tokens = estimate_messages_tokens(final_messages)

        return CompactionResult(
            messages=final_messages,
            original_tokens=orig_tokens,
            after_stage1_tokens=stage1_tokens,
            final_tokens=final_tokens,
            pruned_tool_messages=pruned_tools,
            macro_compacted=True,
            evicted_turn_count=evicted_count,
            incident_memory=memory.model_dump(),
        )

    async def compact_langgraph_state(self, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """供 LangGraph StateGraph 节点直接调用的状态机原子更新方法（利用 RemoveMessage）。"""
        messages: Sequence[BaseMessage] = state.get("messages", [])
        if not messages:
            return None

        result = await self.acompact(messages)
        if not result.macro_compacted and result.pruned_tool_messages == 0:
            return None

        kept_ids = {getattr(m, "id", None) for m in result.messages if getattr(m, "id", None)}
        remove_ops = [
            RemoveMessage(id=m.id)
            for m in messages
            if getattr(m, "id", None) and m.id not in kept_ids
        ]

        return {
            "messages": [*remove_ops, *result.messages],
        }


# 全局单例压缩器与上下文通知通道 (用于向 SSE 前端推送压缩事件)
context_compactor = SREContextCompactor()
compaction_event_emitter: ContextVar[Optional[asyncio.Queue]] = ContextVar("compaction_event_emitter", default=None)


class ContextCompactorMiddleware(AgentMiddleware):
    """LangChain Agent 模型调用层上下文压缩中间件。

    与 RagContextDedupMiddleware 协同工作：
    1. 先执行 ContextCompactorMiddleware：对温数据超长工具输出剪枝，并在达到配置的高水位时压缩历史轮次；
    2. 再执行 RagContextDedupMiddleware：对本轮重复命中的知识库父文档转编号引用。
    """

    def __init__(self, compactor: Optional[SREContextCompactor] = None):
        self.compactor = compactor or context_compactor

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        result = self.compactor.compact_sync(request.messages)
        return handler(request.override(messages=result.messages))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        result = await self.compactor.acompact(request.messages)

        # 若当前会话存在流式 SSE 监听器，将压缩事件排入队列通知前端
        queue = compaction_event_emitter.get()
        if queue is not None:
            if result.macro_compacted:
                await queue.put({
                    "stage": "macro",
                    "status": "compacted",
                    "message": (
                        f"⚡ 触发宏观压缩：历史达到高水位 ({result.original_tokens} Tokens)，"
                        f"已提炼 SRE 结构化记忆并收敛至 {result.final_tokens} Tokens (归档 {result.evicted_turn_count} 轮)"
                    ),
                    "details": {
                        "original_tokens": result.original_tokens,
                        "final_tokens": result.final_tokens,
                        "evicted_turns": result.evicted_turn_count,
                        "incident_memory": result.incident_memory,
                    }
                })
            elif result.pruned_tool_messages > 0:
                saved = result.original_tokens - result.final_tokens
                await queue.put({
                    "stage": "micro",
                    "status": "pruned",
                    "message": (
                        f"⚡ 触发工具输出剪枝：已对 {result.pruned_tool_messages} 条历史超长工具输出执行确定性折叠 "
                        f"(节省 {saved} Tokens)"
                    ),
                    "details": {
                        "pruned_count": result.pruned_tool_messages,
                        "original_tokens": result.original_tokens,
                        "final_tokens": result.final_tokens,
                    }
                })

        return await handler(request.override(messages=result.messages))
