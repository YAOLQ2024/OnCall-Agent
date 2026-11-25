"""单元测试：验证 Supervisor 1+2+3 漏斗任务路由决策链路。"""

import pytest
from unittest.mock import Mock, AsyncMock
from langchain_core.messages import HumanMessage
from app.agent.supervisor import (
    SupervisorRouter,
    IntentType,
    IntentDecision,
    SupervisorState,
    route_by_supervisor,
    supervisor_node,
    build_supervisor_graph,
)


@pytest.fixture
def mock_supervisor(monkeypatch):
    """创建一个带有 Mock 嵌入与模型的 Supervisor 实例"""
    # No real embedding or model calls may escape the unit-test fixture.
    monkeypatch.setattr("app.services.vector_embedding_service.vector_embedding_service.embed_query",
                        lambda text: [0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr("app.core.llm_factory.llm_factory.create_chat_model",
                        Mock(side_effect=AssertionError("Unexpected model call in unit test")))
    router = SupervisorRouter(score_threshold=0.70)
    # Mock 样本数据与向量（维度为 4 的简易单位向量）
    router._exemplars_routes = [
        IntentType.AIOPS_DIAGNOSIS,
        IntentType.KNOWLEDGE_QA,
        IntentType.GENERAL_CHAT,
    ]
    router._exemplars_texts = [
        "线上订单服务超时率飙高",
        "Kubernetes Service 的配置参数有哪些",
        "你好，今天过得怎么样",
    ]
    # 正交/特征向量：
    # AIOps: [1, 0, 0, 0]
    # Knowledge: [0, 1, 0, 0]
    # Chat: [0, 0, 1, 0]
    router._exemplars_vectors = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
    ]
    router._initialized = True
    return router


@pytest.mark.asyncio
async def test_layer1_rule_fast_pass_greetings(mock_supervisor):
    """测试第一层：日常极短问候规则 0ms 拦截"""
    queries = ["你好", "在吗", "hi", "hello!", "谢谢你~", "你是谁？"]
    for q in queries:
        result = await mock_supervisor.route(q)
        assert result.intent == IntentType.GENERAL_CHAT
        assert result.source == "rule_fast_pass"
        assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_layer1_rule_fast_pass_commands(mock_supervisor):
    """测试第一层：明确 CLI 命令规则 0ms 拦截"""
    queries = [
        "kubectl get pods -n kube-system",
        "helm list -A",
        "docker ps -a",
        "systemctl status nginx",
    ]
    for q in queries:
        result = await mock_supervisor.route(q)
        assert result.intent == IntentType.KNOWLEDGE_QA
        assert result.source == "rule_fast_pass"


@pytest.mark.asyncio
async def test_layer1_rule_fast_pass_alerts(mock_supervisor):
    """测试第一层：严重线上告警排障关键词 0ms 拦截"""
    queries = [
        "线上订单服务报错率飙高，赶紧看下",
        "支付系统频繁OOM，触发线上告警",
        "网关出现大面积 504 Gateway Timeout",
    ]
    for q in queries:
        result = await mock_supervisor.route(q)
        assert result.intent == IntentType.AIOPS_DIAGNOSIS
        assert result.source == "rule_fast_pass"


@pytest.mark.asyncio
async def test_layer1_rule_salutation_stripping_mixed_queries(mock_supervisor):
    """测试第一层：Rasa 模式寒暄剥离 (混合问候与真实业务问题，不被误判为闲聊)"""
    # 1. 问候 + 技术命令/文档咨询 -> 准确命中 KNOWLEDGE_QA，而不是闲聊
    qa_queries = [
        "你好，请问 kubectl get pods 怎么用？",
        "您好，想请教下 Redis 的持久化机制是什么？",
        "麻烦问下 Kubernetes Service 的配置参数有哪些？",
    ]
    for q in qa_queries:
        result = await mock_supervisor.route(q)
        assert result.intent == IntentType.KNOWLEDGE_QA
        assert result.source == "rule_fast_pass"
        assert result.confidence == 1.0

    # 2. 问候 + 紧急线上故障 -> 准确命中 AIOPS_DIAGNOSIS，而不是闲聊
    aiops_queries = [
        "你好，线上订单服务报错率飙高，赶紧看下",
        "您好，线上网关出现大面积 504 Gateway Timeout 紧急故障！",
        "hi, 核心服务频繁出现 CrashLoopBackOff 报错",
    ]
    for q in aiops_queries:
        result = await mock_supervisor.route(q)
        assert result.intent == IntentType.AIOPS_DIAGNOSIS
        assert result.source == "rule_fast_pass"
        assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_layer1_rule_simulation_negation_filter(mock_supervisor, monkeypatch):
    """测试第一层：演练与否定语义过滤 (演练场景不走紧急排障规则，放行至下游)"""
    # 模拟下游 LLM 仲裁返回
    mock_model = Mock()
    mock_structured = Mock()
    mock_structured.ainvoke = AsyncMock(
        return_value=IntentDecision(intent="knowledge_qa", reason="这是演练文档咨询，非生产事故")
    )
    mock_model.bind.return_value.with_structured_output.return_value = mock_structured
    monkeypatch.setattr("app.core.llm_factory.llm_factory.create_chat_model", lambda **kwargs: mock_model)

    # 包含 CrashLoopBackOff 但明确声明为演练
    simulation_query = "测试环境故障演练 CrashLoopBackOff，不要排查，仅记录"
    result = await mock_supervisor.route(simulation_query)
    # 不应直接命中第一层告警规则快速放行
    assert result.source != "rule_fast_pass"


@pytest.mark.asyncio
async def test_layer2_semantic_router_match(mock_supervisor, monkeypatch):
    """测试第二层：语义向量路由余弦匹配"""
    # 模拟输入 query 算出的向量与 AIOps 样本高度重合 [0.95, 0.05, 0.0, 0.0]
    monkeypatch.setattr(
        "app.services.vector_embedding_service.vector_embedding_service.embed_query",
        lambda text: [0.95, 0.05, 0.0, 0.0]
    )

    result = await mock_supervisor.route("支付接口响应极其缓慢，协助排查")
    assert result.intent == IntentType.AIOPS_DIAGNOSIS
    assert result.source == "semantic_router"
    assert result.confidence > 0.70


@pytest.mark.asyncio
async def test_layer3_llm_arbitrate_fallback(mock_supervisor, monkeypatch):
    """测试第三层：当向量相似度低时进入 LLM 结构化仲裁"""
    # 模拟输入 query 算出的向量与所有预设样本都不相似 [0.1, 0.1, 0.1, 0.1]（相似度低于 threshold）
    monkeypatch.setattr(
        "app.services.vector_embedding_service.vector_embedding_service.embed_query",
        lambda text: [0.1, 0.1, 0.1, 0.1]
    )

    # Mock LLM 结构化输出返回
    mock_model = Mock()
    mock_structured = Mock()
    mock_structured.ainvoke = AsyncMock(
        return_value=IntentDecision(intent="knowledge_qa", reason="用户询问底层实现机制")
    )
    mock_model.bind.return_value.with_structured_output.return_value = mock_structured
    monkeypatch.setattr("app.core.llm_factory.llm_factory.create_chat_model", lambda **kwargs: mock_model)

    result = await mock_supervisor.route("能否深入讲讲关于分布式事务两阶段提交的具体实现细节？")
    assert result.intent == IntentType.KNOWLEDGE_QA
    assert result.source == "llm_supervisor"
    assert "LLM 结构化仲裁" in result.reason


# ==========================================
# 四、 LangGraph Supervisor 条件边与工作流测试
# ==========================================

def test_langgraph_conditional_edge_dispatch():
    """测试 LangGraph 核心条件边函数：依据状态正确分发下一跳节点"""
    # 1. 知识库意图 -> 分流至 knowledge_qa
    state_qa: SupervisorState = {
        "messages": [HumanMessage(content="kubectl get pods")],
        "next_node": IntentType.KNOWLEDGE_QA.value,
        "route_decision": None
    }
    assert route_by_supervisor(state_qa) == IntentType.KNOWLEDGE_QA.value

    # 2. 故障诊断意图 -> 分流至 aiops_diagnosis
    state_aiops: SupervisorState = {
        "messages": [HumanMessage(content="线上订单服务报错率飙高")],
        "next_node": IntentType.AIOPS_DIAGNOSIS.value,
        "route_decision": None
    }
    assert route_by_supervisor(state_aiops) == IntentType.AIOPS_DIAGNOSIS.value

    # 3. 闲聊意图 -> 分流至 general_chat
    state_chat: SupervisorState = {
        "messages": [HumanMessage(content="你好")],
        "next_node": IntentType.GENERAL_CHAT.value,
        "route_decision": None
    }
    assert route_by_supervisor(state_chat) == IntentType.GENERAL_CHAT.value


def test_build_supervisor_graph_structure():
    """测试 LangGraph Supervisor 状态图构建与节点连接"""
    compiled_graph = build_supervisor_graph()
    assert compiled_graph is not None
    # 验证节点包含 supervisor 与三大业务节点
    node_names = set(compiled_graph.nodes.keys())
    assert "supervisor" in node_names
    assert IntentType.KNOWLEDGE_QA.value in node_names
    assert IntentType.AIOPS_DIAGNOSIS.value in node_names
    assert IntentType.GENERAL_CHAT.value in node_names
