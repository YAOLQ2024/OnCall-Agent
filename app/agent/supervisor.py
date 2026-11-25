"""智能 OnCall Agent 任务主管路由模块 (Supervisor / Funnel Router)。

采用业界成熟的“1+2+3 漏斗式分层架构”：
1. 第一层：自研规则快速放行 (Fast-Pass，0ms)
2. 第二层：吸收 semantic-router 思想的内存级向量路由器 (Semantic Router，~40ms)
3. 第三层：LangGraph 原生 Supervisor 大模型结构化仲裁 (LLM Arbiter，~350ms)
"""

import math
import re
import time
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, Field
from loguru import logger
from langsmith import traceable
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from app.config import config
from app.core.llm_factory import llm_factory
from app.services.vector_embedding_service import vector_embedding_service


# ==========================================
# 一、 意图枚举与数据契约
# ==========================================

class IntentType(str, Enum):
    """支持的核心业务意图"""
    KNOWLEDGE_QA = "knowledge_qa"       # ReAct 知识与工具智能体 (含知识库检索与外部 MCP 工具调用)
    AIOPS_DIAGNOSIS = "aiops_diagnosis" # 线上故障排查与告警分析 (走 Plan-Execute-Replan 工作流)
    GENERAL_CHAT = "general_chat"       # 通用闲聊与问候 (直接生成回复)


class IntentDecision(BaseModel):
    """第三层 LLM 结构化输出模型"""
    intent: Literal["knowledge_qa", "aiops_diagnosis", "general_chat"] = Field(
        description="用户意图分类：'knowledge_qa'（技术/概念/配置查询、以及天气/时间/Web等外部工具查询与通用问答）、'aiops_diagnosis'（线上告警/故障定位/服务异常排查）、'general_chat'（仅限纯打招呼、纯日常问候、表达感谢）"
    )
    reason: str = Field(description="简要分类依据，用于链路追踪与审计")


class SupervisorResult(BaseModel):
    """任务主管的最终路由决策结果"""
    intent: IntentType
    source: Literal["rule_fast_pass", "semantic_router", "llm_supervisor"]
    confidence: float
    reason: str
    latency_ms: float


# ==========================================
# 二、 第一层：工业级自研规则快速放行 (Fast-Pass)
# 参考 Rasa NLU 模式与 SRE 告警事实标准设计：
# 1. 礼貌前缀自动剥离 (Salutation Stripping)
# 2. 动宾槽位与经典运维状态词精准匹配
# 3. 演练与否定语义过滤 (Negation Filter)
# ==========================================

# 1. 句首礼貌寒暄前缀（支持多个寒暄词连用叠加，如“你好，请问”、“您好想请教下”，消除问候前缀干扰）
SALUTATION_PREFIX_PATTERN = re.compile(
    r"^(?:\s*(?:你好|您好|在吗|请问(?:一下)?|麻烦问下|想请教下|向你咨询个问题|咨询下|咨询|hi|hello|hey|dear)\s*[,，:：!！~～\s]*)+",
    re.I
)

# 2. 纯日常问候与助手能力介绍语（整句仅包含寒暄、感谢或针对助手自身身份/能力的问询，无任何具体技术业务实体）
PURE_GREETING_PATTERN = re.compile(
    r"^(?:\s*(?:你好|在吗|您好|早安|晚安|hi|hello|hey|谢谢(?:你)?|多谢(?:你)?|thx|thank\s*you|thanks|辛苦了|"
    r"你是谁|你能?(?:帮我|为我)?做(?:什么|些什么)|你能干什么|你能做啥|你有什么用|你的功能是什么|你的作用是什么|"
    r"介绍下自己|自我介绍|介绍一下你自己|你叫什么|你叫什么名字)\s*[,，:：!！?？~～\s]*)+$",
    re.I
)

# 3. 运维 CLI 命令特征（剥离前缀后行首命令，100% 属于技术或配置咨询）
COMMAND_PATTERN = re.compile(
    r"^\s*(?:kubectl|helm|docker|systemctl|journalctl|curl|ansible|terraform|git|iptables|netstat|crictl)\b",
    re.I
)

# 4. 技术知识咨询句型与专业架构查询（支持句尾标点）
KNOWLEDGE_INQUIRY_PATTERN = re.compile(
    r"(?:(?:配置参数|底层原理|架构图|最佳实践|工作原理|实现机制|设计模式|淘汰策略|持久化机制|调度机制|高可用方案)(?:有哪些|是什么|怎么配|怎么理解)?|"
    r"(?:怎么配置|如何配置|参数说明|命令参数|语法结构|与.*的区别|和.*有什么区别)|"
    r"是什么意思|含义是什么|属于什么类型)\s*[?？。!！~～\s]*$",
    re.I
)

# 5. SRE 标准异常状态码（Kubernetes / HTTP / 系统层）
AIOPS_EXACT_STATES = re.compile(
    r"\b(?:CrashLoopBackOff|OOMKilled|ImagePullBackOff|ErrImagePull|NodeNotReady|DiskPressure|MemoryPressure|PIDPressure|"
    r"FailedScheduling|CreateContainerConfigError|Evicted|HostPortConflict|"
    r"502\s*Bad\s*Gateway|504\s*Gateway\s*Timeout|500\s*Internal\s*Server\s*Error)\b",
    re.I
)

# 6. 紧急排障动宾组合（如：订单服务报错率飙高、连接池打满、节点宕机）
AIOPS_ACTION_OBJECT = re.compile(
    r"(?:线上|生产|核心|集群|系统|服务|接口|网关|数据库|容器|Pod|节点)"
    r".{0,6}"
    r"(?:报错率飙高|频繁\s*OOM|雪崩|挂了|宕机|崩溃|卡死|打满|耗尽|突增|剧增|死锁|不可用|大面积报错|连接失败|频繁重启|急需排查|立即排障)",
    re.I
)

# 7. 演练与否定过滤（防止演练、假装报错被误判为紧急排障）
NEGATION_OR_SIMULATION = re.compile(
    r"演练|压测|测试环境|假装|不要排查|不用报警|无需排障|仅供参考|不是真的",
    re.I
)


# ==========================================
# 三、 第二层：语义向量路由样本库 (Semantic Router)
# ==========================================

ROUTE_EXEMPLARS: Dict[IntentType, List[str]] = {
    IntentType.AIOPS_DIAGNOSIS: [
        "线上订单服务接口超时率飙高，帮我排查原因",
        "payment-service 容器频繁出现 OOMKilled 重启",
        "节点 CPU 使用率突破 90%，排查线上故障",
        "网关出现大量的 504 Gateway Timeout 报错",
        "MySQL 数据库连接池打满告警处理",
        "微服务出现级联雪崩与接口大面积报错",
        "检查核心业务集群的告警信息并给出诊断报告",
    ],
    IntentType.KNOWLEDGE_QA: [
        "Kubernetes Service 的配置参数有哪些",
        "什么是 Pod 的 InitContainer 容器及其工作原理",
        "MySQL 常见的索引失效场景有哪些",
        "如何配置 Prometheus 的告警规则 alerting rules",
        "Redis 内存淘汰策略和持久化机制说明",
        "解释什么是 Java 双亲委派机制或垃圾回收算法",
        "请结合文档说明 Pod 一直处于 Pending 的定位流程",
    ],
    IntentType.GENERAL_CHAT: [
        "你好，今天过得怎么样",
        "在吗，你能帮我完成哪些工作任务",
        "你是谁，你能帮我做什么",
        "请问你是哪家公司开发的智能助手",
        "非常感谢你的耐心解答",
        "介绍一下你自己以及你的核心功能",
        "给我讲一个关于程序员的笑话吧",
    ]
}


# ==========================================
# 四、 统一主管调度器实现 (Supervisor)
# ==========================================

class SupervisorRouter:
    """智能任务主管：集成规则放行、向量聚类与 LLM 仲裁"""

    def __init__(self, score_threshold: float = 0.72):
        self.threshold = score_threshold
        self._exemplars_routes: List[IntentType] = []
        self._exemplars_texts: List[str] = []
        self._exemplars_vectors: List[List[float]] = []
        self._initialized = False

    def initialize(self):
        """系统启动时预计算样本向量并常驻内存（耗时仅几十毫秒）"""
        if self._initialized:
            return

        routes, texts = [], []
        for intent, samples in ROUTE_EXEMPLARS.items():
            for sample in samples:
                routes.append(intent)
                texts.append(sample)

        try:
            logger.info("正在预热 Supervisor 语义向量路由样本库 (共 {} 条样本)...", len(texts))
            vectors = vector_embedding_service.embed_documents(texts)
            self._exemplars_routes = routes
            self._exemplars_texts = texts
            self._exemplars_vectors = vectors
            self._initialized = True
            logger.info("Supervisor 语义向量路由预热就绪！")
        except Exception as e:
            logger.warning("Supervisor 语义向量预热失败（保留规则与 LLM 降级）：{}", e)

    # ----------------------------------------------------
    # 层 1：自研规则快速放行 (参考 Rasa NLU 模式与 SRE 告警事实标准，0ms)
    # ----------------------------------------------------
    def _rule_fast_pass(self, query: str) -> Optional[Tuple[IntentType, str]]:
        # 1. 严格全句匹配纯日常问候与助手功能问询（整句只有寒暄/自我介绍/功能问询，无业务实体）
        if PURE_GREETING_PATTERN.search(query):
            return IntentType.GENERAL_CHAT, "命中纯日常问候与助手身份规则"

        # 2. 剥离句首礼貌寒暄前缀（提取核心业务句子，消除问候词干扰）
        core_query = SALUTATION_PREFIX_PATTERN.sub("", query).strip()
        if not core_query:
            return IntentType.GENERAL_CHAT, "剥离寒暄礼貌用语后无实质内容"

        # 3. 剥离前缀后，检查剩余子句是否仍为纯身份/功能问询（如“你好，你是谁？你能帮我做什么？”）
        if PURE_GREETING_PATTERN.search(core_query):
            return IntentType.GENERAL_CHAT, "剥离问候前缀后为纯助手身份/功能问询规则"

        # 4. 演练与否定过滤：若明确包含演练/压测/假装报错，不触发紧急排障规则，放行至第二/三层
        is_simulation = bool(NEGATION_OR_SIMULATION.search(core_query))

        # 5. 运维 CLI 命令特征快速放行（例如 kubectl get pod, docker ps 等）
        if COMMAND_PATTERN.search(core_query):
            return IntentType.KNOWLEDGE_QA, "命中运维 CLI 命令规则"

        # 6. 技术知识咨询句型（例如：底层原理、配置参数有哪些、持久化机制是什么）
        if KNOWLEDGE_INQUIRY_PATTERN.search(core_query):
            return IntentType.KNOWLEDGE_QA, "命中专业架构与技术咨询句型规则"

        # 7. SRE 紧急状态与告警动宾组合（排除演练场景）
        if not is_simulation:
            if AIOPS_EXACT_STATES.search(core_query):
                return IntentType.AIOPS_DIAGNOSIS, "命中 SRE 核心异常状态码规则"
            if AIOPS_ACTION_OBJECT.search(core_query):
                return IntentType.AIOPS_DIAGNOSIS, "命中生产环境紧急排障动宾规则"

        return None

    # ----------------------------------------------------
    # 层 2：纯数学向量余弦相似度匹配 (借鉴 semantic-router，~30ms)
    # ----------------------------------------------------
    def _semantic_route(self, query: str) -> Optional[Tuple[IntentType, float, str]]:
        if not self._initialized or not self._exemplars_vectors:
            return None

        try:
            q_vec = vector_embedding_service.embed_query(query)
            q_norm = math.sqrt(sum(x * x for x in q_vec))
            if q_norm == 0:
                return None

            best_score = -1.0
            best_intent = IntentType.GENERAL_CHAT

            # 余弦相似度点乘计算：dot(q, e) / (norm_q * norm_e)
            for idx, e_vec in enumerate(self._exemplars_vectors):
                e_norm = math.sqrt(sum(x * x for x in e_vec))
                if e_norm == 0:
                    continue
                dot = sum(a * b for a, b in zip(q_vec, e_vec))
                cos_sim = dot / (q_norm * e_norm)

                if cos_sim > best_score:
                    best_score = cos_sim
                    best_intent = self._exemplars_routes[idx]

            # 阈值决策
            if best_score >= self.threshold:
                return best_intent, round(best_score, 4), f"语义向量最高相似度达到 {best_score:.3f}"

            return None
        except Exception as e:
            logger.warning("语义向量路由计算异常，降级至 LLM 仲裁：{}", e)
            return None

    # ----------------------------------------------------
    # 层 3：LangGraph 原生 Supervisor LLM 结构化仲裁 (~350ms)
    # ----------------------------------------------------
    async def _llm_arbitrate(self, query: str) -> Tuple[IntentType, float, str]:
        prompt = (
            "作为一个智能系统的任务主管（Supervisor），请准确分析用户提问意图，严格分类为：\n"
            "- 'knowledge_qa': 知识问答、技术文档咨询、架构概念解释、参数配置查询、以及任何需要外部工具获取实时信息（如天气查询、时间查询、网页抓取等）的查询；\n"
            "- 'aiops_diagnosis': 线上系统故障排查、服务告警处理、指标突增突降定位、服务卡死或报错定位；\n"
            "- 'general_chat': 仅限纯打招呼、纯问候（如'你好'、'在吗'、'早安'）、表达感谢（如'谢谢'）、以及针对助手自身名字/身份的闲聊（无具体事实、无数据、无工具查询诉求）。\n\n"
            f"用户提问：{query}"
        )
        try:
            model = llm_factory.create_chat_model(
                model=config.supervisor_model,
                temperature=0,
                streaming=False,
                extra_body={"enable_thinking": False},  # 关闭深度思考防截断
            ).bind(max_tokens=256)

            # 使用 Pydantic 结构化输出
            structured_model = model.with_structured_output(IntentDecision)
            decision: IntentDecision = await structured_model.ainvoke(prompt)
            return IntentType(decision.intent), 0.95, f"LLM 结构化仲裁：{decision.reason}"
        except Exception as e:
            logger.error("LLM 结构化仲裁失败，保守回退知识库问答：{}", e)
            return IntentType.KNOWLEDGE_QA, 0.50, f"仲裁异常兜底：{type(e).__name__}"

    # ----------------------------------------------------
    # 主路由对外入口
    # ----------------------------------------------------
    @traceable(name="agent.supervisor", run_type="chain")
    async def route(self, query: str) -> SupervisorResult:
        """执行 1+2+3 漏斗任务路由决策"""
        start_time = time.perf_counter()
        clean_query = query.strip()

        # 1. 第一层：规则快速放行 (0ms)
        rule_hit = self._rule_fast_pass(clean_query)
        if rule_hit:
            intent, reason = rule_hit
            latency = (time.perf_counter() - start_time) * 1000
            return SupervisorResult(
                intent=intent,
                source="rule_fast_pass",
                confidence=1.0,
                reason=reason,
                latency_ms=round(latency, 2)
            )

        # 剥离前缀后的有效业务句子（用于第二层向量计算，减少问候噪音对余弦相似度的稀释）
        core_query = SALUTATION_PREFIX_PATTERN.sub("", clean_query).strip() or clean_query

        # 2. 第二层：语义向量路由 (~40ms)
        semantic_hit = self._semantic_route(core_query)
        if semantic_hit:
            intent, score, reason = semantic_hit
            latency = (time.perf_counter() - start_time) * 1000
            return SupervisorResult(
                intent=intent,
                source="semantic_router",
                confidence=score,
                reason=reason,
                latency_ms=round(latency, 2)
            )

        # 3. 第三层：LLM 结构化兜底仲裁 (~350ms，传递完整 query 保持上下文自然度)
        intent, score, reason = await self._llm_arbitrate(clean_query)
        latency = (time.perf_counter() - start_time) * 1000
        return SupervisorResult(
            intent=intent,
            source="llm_supervisor",
            confidence=score,
            reason=reason,
            latency_ms=round(latency, 2)
        )


# 全局单例
supervisor_router = SupervisorRouter()


# ==========================================
# 五、 LangGraph 原生 Multi-Agent 状态图与条件边
# ==========================================

class SupervisorState(TypedDict):
    """LangGraph 多智能体协同状态"""
    messages: Annotated[Sequence[BaseMessage], add_messages]
    next_node: str                                      # Supervisor 决策的下一个工作流节点
    route_decision: Optional[Dict[str, Any]]            # 路由决策元数据（用于全链路追踪与调试）


async def supervisor_node(state: SupervisorState) -> Dict[str, Any]:
    """LangGraph 主管节点：调用 1+2+3 漏斗路由确定下一跳 Worker 节点"""
    messages = state.get("messages", [])
    if not messages:
        return {"next_node": IntentType.GENERAL_CHAT.value, "route_decision": None}

    # 提取最后一条用户输入
    last_user_query = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) or getattr(msg, "type", None) == "human":
            last_user_query = str(msg.content)
            break
    if not last_user_query:
        last_user_query = str(messages[-1].content)

    decision = await supervisor_router.route(last_user_query)
    logger.info("LangGraph Supervisor 决策：{} (来源: {}, 置信度: {})", decision.intent.value, decision.source, decision.confidence)
    return {
        "next_node": decision.intent.value,
        "route_decision": decision.model_dump(),
    }


def route_by_supervisor(state: SupervisorState) -> str:
    """LangGraph 核心条件边 (Conditional Edge Router Function)
    
    根据 Supervisor 节点的决策状态，将执行流有条件地导向对应的下游 Worker 节点。
    """
    next_target = state.get("next_node", IntentType.GENERAL_CHAT.value)
    logger.debug("Supervisor 条件边导向节点 -> {}", next_target)
    return next_target


async def knowledge_qa_node(state: SupervisorState) -> Dict[str, Any]:
    """ReAct 知识与工具智能体 Worker 节点"""
    from app.services.rag_agent_service import rag_agent_service
    messages = state["messages"]
    last_query = str(messages[-1].content)
    answer = await rag_agent_service.query(last_query)
    return {"messages": [AIMessage(content=answer)]}


async def aiops_diagnosis_node(state: SupervisorState) -> Dict[str, Any]:
    """AIOps 线上故障排查 Worker 节点 (基于 Plan-Execute-Replan 工作流)"""
    from app.services.aiops_service import AIOpsService
    messages = state["messages"]
    last_query = str(messages[-1].content)
    aiops = AIOpsService()
    final_response = "排障工作流启动..."
    async for event in aiops.execute(last_query):
        if event.get("event") == "workflow_completed":
            final_response = event.get("data", {}).get("response", final_response)
    return {"messages": [AIMessage(content=final_response)]}


async def general_chat_node(state: SupervisorState) -> Dict[str, Any]:
    """日常问候与通用对话 Worker 节点"""
    messages = state["messages"]
    last_query = str(messages[-1].content)
    model = llm_factory.create_chat_model(
        model=config.supervisor_model,
        temperature=0.7,
        streaming=False,
    )
    resp = await model.ainvoke([
        SystemMessage(content="你是智能 OnCall 运维系统的助手。请礼貌、简洁地回应用户的日常问候或针对系统能力的咨询。"),
        HumanMessage(content=last_query)
    ])
    return {"messages": [resp]}


def build_supervisor_graph(checkpointer: Optional[Any] = None):
    """构建并编译基于 LangGraph 条件边的 Multi-Agent Supervisor 协同图"""
    workflow = StateGraph(SupervisorState)

    # 1. 注册主管节点与各专业 Worker 节点
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node(IntentType.KNOWLEDGE_QA.value, knowledge_qa_node)
    workflow.add_node(IntentType.AIOPS_DIAGNOSIS.value, aiops_diagnosis_node)
    workflow.add_node(IntentType.GENERAL_CHAT.value, general_chat_node)

    # 2. 设置状态图入口
    workflow.set_entry_point("supervisor")

    # 3. 核心：注册 Supervisor 条件边 (Conditional Edges)
    workflow.add_conditional_edges(
        "supervisor",
        route_by_supervisor,
        {
            IntentType.KNOWLEDGE_QA.value: IntentType.KNOWLEDGE_QA.value,
            IntentType.AIOPS_DIAGNOSIS.value: IntentType.AIOPS_DIAGNOSIS.value,
            IntentType.GENERAL_CHAT.value: IntentType.GENERAL_CHAT.value,
        }
    )

    # 4. 各 Worker 节点执行完毕后流向 END
    workflow.add_edge(IntentType.KNOWLEDGE_QA.value, END)
    workflow.add_edge(IntentType.AIOPS_DIAGNOSIS.value, END)
    workflow.add_edge(IntentType.GENERAL_CHAT.value, END)

    return workflow.compile(checkpointer=checkpointer)
