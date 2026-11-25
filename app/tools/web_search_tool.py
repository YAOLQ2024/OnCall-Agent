"""联网搜索工具 - 基于 Tavily Search API 实现实时公网检索

为 ReAct 对话 Agent 提供公网动态信息补充能力，专用于内部知识库未收录的技术动态、
开源项目 Bug、Kubernetes CVE 漏洞、第三方库最新版本发布等场景。
"""

from typing import Any, Dict, List, Optional
import httpx
from langchain_core.tools import tool
from loguru import logger
from langsmith import traceable

from app.config import config


@traceable(name="agent.search_web.execute", run_type="tool")
def perform_web_search(
    query: str,
    max_results: int = 5,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """调用 Tavily REST API 执行公网搜索并结构化排版结果

    Args:
        query: 检索关键词
        max_results: 最多返回的条目数
        api_key: Tavily API Key，为空则读取系统配置
        timeout: 请求超时时间（秒）

    Returns:
        str: 清洗格式化后的搜索摘要及带 URL 的来源列表
    """
    effective_key = (api_key or config.tavily_api_key or "").strip()
    if not effective_key:
        logger.warning("未配置 TAVILY_API_KEY，联网搜索工具优雅降级")
        return (
            "[联网搜索未启用] 系统当前未配置 TAVILY_API_KEY，无法执行实时公网搜索。"
            "请提示用户在 .env 文件中配置 TAVILY_API_KEY（每月 1,000 次免费额度），"
            "或优先基于内部知识库已有资料进行分析解答。"
        )

    base_url = (config.tavily_api_base or "https://api.tavily.com").rstrip("/")
    search_url = f"{base_url}/search"
    effective_timeout = timeout or config.web_search_timeout
    effective_limit = min(max(1, max_results), config.web_search_max_results)

    payload = {
        "api_key": effective_key,
        "query": query,
        "search_depth": "basic",
        "include_answer": config.web_search_include_answer,
        "max_results": effective_limit,
    }

    try:
        logger.info(f"发起 Tavily 联网搜索: query='{query}', max_results={effective_limit}")
        with httpx.Client(timeout=effective_timeout) as client:
            response = client.post(
                search_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data: Dict[str, Any] = response.json()

        results: List[Dict[str, Any]] = data.get("results", [])
        ai_answer: str = data.get("answer", "") or ""

        if not results and not ai_answer:
            logger.info(f"Tavily 搜索未命中相关公网内容: '{query}'")
            return f"[联网搜索结果] 针对关键词 '{query}' 未检索到相关的公网网页内容。"

        lines: List[str] = []
        if ai_answer:
            lines.append(f"【Tavily 智能概括】: {ai_answer}\n")

        lines.append(f"找到 {len(results)} 条公网参考资料：\n")
        for idx, item in enumerate(results, 1):
            title = item.get("title", "无标题")
            url = item.get("url", "")
            content = item.get("content", "").strip()
            score = item.get("score")
            score_str = f" (相关度: {score:.2f})" if isinstance(score, (int, float)) else ""

            lines.append(f"[{idx}] {title}{score_str}")
            if url:
                lines.append(f"    来源链接: {url}")
            if content:
                lines.append(f"    核心摘要: {content}")
            lines.append("")

        output = "\n".join(lines).strip()
        logger.info(f"Tavily 搜索成功: 返回 {len(results)} 条结果")
        return output

    except httpx.TimeoutException:
        logger.error(f"Tavily 联网搜索超时 ({effective_timeout}s): query='{query}'")
        return f"[联网搜索超时] 访问外部搜索引擎超时 ({effective_timeout}秒)，可能是网络波动，请尝试精简关键词后重试。"
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        logger.error(f"Tavily API 返回 HTTP 错误: status={status}, response={e.response.text[:200]}")
        if status in (401, 403):
            return "[联网搜索鉴权失败] Tavily API Key 无效或已达每月免费调用限额，请检查 .env 中的 TAVILY_API_KEY。"
        return f"[联网搜索接口异常] Tavily 服务返回 HTTP {status} 错误，请稍后重试。"
    except Exception as e:
        logger.error(f"Tavily 联网搜索未知异常: {type(e).__name__} - {e}")
        return f"[联网搜索失败] 执行公网检索时出现异常: {str(e)}"


@tool
def search_web(query: str, max_results: int = 5) -> str:
    """通过 Tavily 搜索引擎联网检索公网实时技术文档、开源社区动态、CVE 漏洞或外部资料。

    当内部知识库没有相关信息、或用户明确要求查询公网/外部最新信息时（例如最新的技术版本更新、第三方开源库报错、行业运维资讯等），使用此工具。

    Args:
        query: 搜索关键词或短语（建议使用精准的技术实体，如 'Kubernetes 1.30 changelog' 或 'Spring Boot 3.2 升级踩坑'）
        max_results: 返回的最大结果数量（默认 5 条）

    Returns:
        str: 包含核心摘要、来源网页标题与 URL 链接的格式化文本
    """
    return perform_web_search(query=query, max_results=max_results)


def extract_web_references(content: str) -> list[dict[str, str]]:
    """从 perform_web_search 的输出文本中提取条目 (title, url)"""
    import re
    refs = []
    current_title = ""
    for line in content.splitlines():
        line = line.strip()
        m_title = re.match(r"^\[\d+\]\s*(.+?)(?:\s*\(相关度:[^)]+\))?$", line)
        if m_title:
            current_title = m_title.group(1).strip()
            continue
        if line.startswith("来源链接:") and current_title:
            url = line.replace("来源链接:", "").strip()
            if url:
                refs.append({"title": current_title, "url": url})
            current_title = ""
    return refs

