"""单元测试：工业级三级上下文压缩器 (SREToolOutputCompressor & SREContextCompactor)。"""

import json
import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agent.compactor import (
    SREContextCompactor,
    SREIncidentMemory,
    SREToolOutputCompressor,
    estimate_messages_tokens,
    split_into_atomic_turns,
)


def test_json_structural_safe_compression():
    """验证大型 Prometheus/K8s JSON 列表折叠后仍为 100% 合法 JSON，绝不切碎花括号。"""
    large_items = [
        {
            "pod": f"payment-service-pod-{i}",
            "namespace": "production",
            "status": "Running",
            "metrics": "x" * 500,
        }
        for i in range(120)
    ]
    payload = {
        "apiVersion": "v1",
        "kind": "PodList",
        "items": large_items,
    }
    raw_json = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(raw_json) > 45_000

    compressed = SREToolOutputCompressor.compress(raw_json, max_chars=45_000)
    assert len(compressed) < 10_000

    # 必须能被 json.loads 正常解析，不能出现半截 JSON 语法错误
    parsed = json.loads(compressed)
    assert parsed["apiVersion"] == "v1"
    assert parsed["kind"] == "PodList"
    assert len(parsed["items"]) == 5  # 首3 + 折叠摘要1 + 尾1
    assert parsed["items"][0]["pod"] == "payment-service-pod-0"
    assert parsed["items"][-1]["pod"] == "payment-service-pod-119"
    assert "_oncall_compactor" in parsed["items"][3]


def test_openhands_error_salience_preservation():
    """验证在千行常规日志中间埋藏的报错行（ERROR / OOMKilled）及其上下文 100% 免疫截断。"""
    lines = [f"2026-10-24 12:00:00 INFO [worker-{i}] Processing normal heartbeat packet..." + ("a" * 60) for i in range(800)]
    # 在第 400 行埋入真实根因报错
    lines[399] = "2026-10-24 12:05:01 DEBUG [worker-399] Allocating direct buffer 2048MB"
    lines[400] = "2026-10-24 12:05:02 FATAL [worker-400] java.lang.OutOfMemoryError: Direct buffer memory (OOMKilled)"
    lines[401] = "2026-10-24 12:05:02 INFO  [worker-401] Dumping heap trace to /var/log/heap.bin"

    raw_log = "\n".join(lines)
    assert len(raw_log) > 45_000

    compressed = SREToolOutputCompressor.compress(raw_log, max_chars=45_000, error_radius=2)
    assert len(compressed) < len(raw_log)

    # 关键报错行与上下文必须一字不差保留
    assert "java.lang.OutOfMemoryError: Direct buffer memory (OOMKilled)" in compressed
    assert "Allocating direct buffer 2048MB" in compressed
    assert "Dumping heap trace to /var/log/heap.bin" in compressed
    # 首尾行也必须保留
    assert "worker-0" in compressed
    assert "worker-799" in compressed


def test_swe_agent_line_window_and_single_line_300k_protection():
    """验证普通长日志的 SWE-agent 首50尾100行窗口，以及单行 30 万字符极端情况防爆。"""
    # 1. 普通无报错长日志
    normal_lines = [f"line-{i}: normal routine check passed " + ("z" * 60) for i in range(1000)]
    raw_normal = "\n".join(normal_lines)
    assert len(raw_normal) > 45_000

    compressed_normal = SREToolOutputCompressor.compress(
        raw_normal, max_chars=45_000, head_lines=50, tail_lines=100
    )
    assert "line-0:" in compressed_normal
    assert "line-49:" in compressed_normal
    assert "line-50:" not in compressed_normal
    assert "line-900:" in compressed_normal
    assert "line-999:" in compressed_normal

    # 2. 单行 300,000 字符极端代码搜索返回
    single_huge_line = "BEGIN_MARKER_" + ("X" * 300_000) + "_END_MARKER"
    compressed_huge = SREToolOutputCompressor.compress(single_huge_line, max_chars=45_000)
    assert len(compressed_huge) <= 45_000
    assert compressed_huge.startswith("BEGIN_MARKER_")
    assert compressed_huge.endswith("_END_MARKER")


def test_hot_tier_immunity_and_warm_tier_protocol_invariant():
    """验证最近 2 轮热数据 100% 豁免不切，旧轮次原位剪枝且保留 tool_call_id 协议不变量。"""
    compactor = SREContextCompactor(hot_turns=2, tool_char_threshold=10_000)

    huge_old_output = "\n".join([f"old-log-{i}: " + ("A" * 50) for i in range(400)])
    huge_hot_output = "\n".join([f"hot-log-{i}: " + ("B" * 50) for i in range(400)])
    assert len(huge_old_output) > 10_000
    assert len(huge_hot_output) > 10_000

    messages = [
        SystemMessage(content="系统提示词"),
        # Turn 1 (Warm Tier - 历史旧轮次，应当被剪枝)
        HumanMessage(content="第一轮：查一下网关日志"),
        AIMessage(content="", tool_calls=[{"name": "query_cls_logs", "args": {}, "id": "call_old_1"}]),
        ToolMessage(content=huge_old_output, tool_call_id="call_old_1", name="query_cls_logs"),
        AIMessage(content="第一轮分析完毕"),
        # Turn 2 (Hot Tier - 倒数第 2 轮，豁免保护)
        HumanMessage(content="第二轮：查一下监控"),
        AIMessage(content="监控正常"),
        # Turn 3 (Hot Tier - 最新第 1 轮，豁免保护，含超大日志)
        HumanMessage(content="第三轮：拉取最新 Pod 全量输出"),
        AIMessage(content="", tool_calls=[{"name": "kubectl_logs", "args": {}, "id": "call_hot_3"}]),
        ToolMessage(content=huge_hot_output, tool_call_id="call_hot_3", name="kubectl_logs"),
        AIMessage(content="正在分析最新日志"),
    ]

    pruned_msgs, orig_tok, stage1_tok, pruned_count = compactor.prune_warm_turns(messages)

    assert pruned_count == 1
    assert stage1_tok < orig_tok

    # 检查 Turn 1 的 ToolMessage 已原位剪枝，且 tool_call_id 协议属性 100% 完好
    old_tool_msg = pruned_msgs[3]
    assert isinstance(old_tool_msg, ToolMessage)
    assert old_tool_msg.tool_call_id == "call_old_1"
    assert old_tool_msg.name == "query_cls_logs"
    assert len(old_tool_msg.content) < len(huge_old_output)
    assert SREToolOutputCompressor.COMPACTED_MARKER in old_tool_msg.content

    # 检查 Turn 3 (Hot Tier) 的超大 ToolMessage 100% 原封不动！
    hot_tool_msg = pruned_msgs[9]
    assert isinstance(hot_tool_msg, ToolMessage)
    assert hot_tool_msg.content == huge_hot_output


@pytest.mark.asyncio
async def test_macro_compaction_and_atomic_turn_integrity(monkeypatch):
    """验证突破高水位线时触发宏观提炼，且绝不拆散 AIMessage(tool_calls) 与 ToolMessage。"""
    compactor = SREContextCompactor(
        hot_turns=1,
        tool_char_threshold=5_000,
        high_watermark_tokens=1_000,
        target_watermark_tokens=400,
    )

    async def fake_distill(evicted_turns, existing_memory_text=""):
        return SREIncidentMemory(
            target_services=["order-service", "redis-cluster"],
            confirmed_symptoms=["HTTP 504 Gateway Timeout", "Redis 连接池耗尽"],
            ruled_out_hypotheses=["已排除 CPU 满载问题 (CPU 仅 15%)"],
            executed_actions=["查询了 order-service 监控与错误日志"],
            pending_next_steps=["检查 Redis 慢查询日志"],
        )

    monkeypatch.setattr(compactor, "_distill_incident_memory", fake_distill)

    messages = [
        SystemMessage(content="你是 SRE 排障专家"),
        # Turn 1 (将被归档至 Cold Tier)
        HumanMessage(content="线上 order-service 大面积 504，帮我排查" + ("！" * 600)),
        AIMessage(content="", tool_calls=[{"name": "check_cpu", "args": {}, "id": "tc_1"}]),
        ToolMessage(content="CPU usage is 15%, normal." + (" " * 800), tool_call_id="tc_1", name="check_cpu"),
        AIMessage(content="CPU 正常，排除 CPU 瓶颈。" + ("。" * 600)),
        # Turn 2 (将被归档至 Cold Tier)
        HumanMessage(content="那看看 Redis 连接池状态" + ("？" * 600)),
        AIMessage(content="", tool_calls=[{"name": "check_redis", "args": {}, "id": "tc_2"}]),
        ToolMessage(content="ERROR: Redis pool exhausted, timeout waiting for idle object", tool_call_id="tc_2", name="check_redis"),
        AIMessage(content="确认是 Redis 连接池耗尽。" + ("！" * 600)),
        # Turn 3 (Hot Tier，必须完整保留)
        HumanMessage(content="现在怎么快速恢复？"),
        AIMessage(content="", tool_calls=[{"name": "get_runbook", "args": {}, "id": "tc_3"}]),
        ToolMessage(content="Runbook: 扩容连接池并重启实例", tool_call_id="tc_3", name="get_runbook"),
        AIMessage(content="建议立即扩容并清理慢查询。"),
    ]

    result = await compactor.acompact(messages)

    assert result.macro_compacted is True
    assert result.evicted_turn_count == 2
    assert result.final_tokens < result.original_tokens

    # 验证 SystemMessage 已注入结构化 SRE 工作记忆
    sys_msg = result.messages[0]
    assert isinstance(sys_msg, SystemMessage)
    assert "order-service" in sys_msg.content
    assert "已排除 CPU 满载问题" in sys_msg.content

    # 验证剩余消息严格从 Turn 3 的 HumanMessage 开始，没有任何孤立的 ToolMessage
    remaining = result.messages[1:]
    assert isinstance(remaining[0], HumanMessage)
    assert remaining[0].content == "现在怎么快速恢复？"
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "tc_3" for m in remaining)
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id in ("tc_1", "tc_2") for m in remaining)


def test_sync_compactor_honors_target_watermark_beyond_hot_turns():
    compactor = SREContextCompactor(
        hot_turns=1,
        high_watermark_tokens=500,
        target_watermark_tokens=250,
    )
    messages = [HumanMessage(content=f"{index} " + "x" * 400) for index in range(5)]
    result = compactor.compact_sync(messages)
    assert result.macro_compacted
    assert result.evicted_turn_count == 3
    assert result.messages[-2:] == messages[-2:]
