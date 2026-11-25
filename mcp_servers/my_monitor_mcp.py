# my_monitor_mcp.py
from typing import Optional
from fastmcp import FastMCP

# 1. 初始化一个名为 "LocalOps" 的 MCP 服务
mcp = FastMCP("LocalOps")

# 2. 用 @mcp.tool() 装饰器声明一个工具
# FastMCP 会自动解析函数签名、类型注解和文档注释，自动生成符合 JSON-RPC 规范的 JSON Schema！
@mcp.tool()
def query_cpu_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m",
    alert_threshold: float = 80.0
) -> dict:
    """查询指定微服务的 CPU 使用率指标。
    
    Args:
        service_name: 微服务名称，例如 'payment-service' 或 'data-sync'
        start_time: 开始时间（可选，格式 YYYY-MM-DD HH:MM:SS）
        end_time: 结束时间（可选，格式 YYYY-MM-DD HH:MM:SS）
        interval: 采样周期，支持 '1m', '5m'
        alert_threshold: 告警阈值，默认 80.0
    """
    # 模拟真实监控返回
    current_cpu = 88.5
    return {
        "service_name": service_name,
        "start_time": start_time,
        "end_time": end_time,
        "interval": interval,
        "current_cpu": current_cpu,
        "is_alert": current_cpu > alert_threshold,
        "message": f"警告: {service_name} CPU 当前为 {current_cpu}%，超过阈值 {alert_threshold}%！"
    }

@mcp.tool()
def restart_pod(pod_name: str, namespace: str = "default") -> str:
    """重启异常的 Kubernetes Pod 实例"""
    return f"指令已下发: Pod {namespace}/{pod_name} 正在滚动重启..."

if __name__ == "__main__":
    # 以 stdio 模式启动（供 Claude Desktop / VS Code / Agent 子进程调用）
    mcp.run(transport="stdio")