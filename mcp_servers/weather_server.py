"""开源实时天气查询 MCP Server (基于 Open-Meteo 免 API Key 免费公共气象接口)
无需任何账号或 Key，开箱即用，支持全球各大城市。
"""

import json
from urllib.request import urlopen, Request
from urllib.parse import quote
from fastmcp import FastMCP

mcp = FastMCP("WeatherOps")

# 预置常用城市坐标，避免依赖外部逆地理编码
CITY_COORDINATES = {
    "北京": (39.9042, 116.4074),
    "beijing": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "shanghai": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "guangzhou": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "shenzhen": (22.5431, 114.0579),
    "杭州": (30.2741, 120.1551),
    "hangzhou": (30.2741, 120.1551),
    "成都": (30.5728, 104.0668),
    "chengdu": (30.5728, 104.0668),
    "武汉": (30.5928, 114.3055),
    "wuhan": (30.5928, 114.3055),
}


def _get_coords(city: str) -> tuple[float, float]:
    city_lower = city.strip().lower()
    if city in CITY_COORDINATES:
        return CITY_COORDINATES[city]
    if city_lower in CITY_COORDINATES:
        return CITY_COORDINATES[city_lower]
    # 动态通过 Open-Meteo Geocoding API 解析未知城市
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(city)}&count=1&language=zh&format=json"
        req = Request(geo_url, headers={"User-Agent": "OncallAgent-Weather/1.0"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "results" in data and len(data["results"]) > 0:
                loc = data["results"][0]
                return loc["latitude"], loc["longitude"]
    except Exception:
        pass
    # 默认兜底北京
    return (39.9042, 116.4074)


@mcp.tool()
def get_current_weather(city: str) -> dict:
    """查询指定城市的当前实时天气情况（温度、湿度、风速、天气状况）。

    Args:
        city: 城市名称，如 '北京', '上海', '深圳', 'Hangzhou' 等
    """
    lat, lon = _get_coords(city)
    api_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,"
        f"apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m&timezone=auto"
    )

    try:
        req = Request(api_url, headers={"User-Agent": "OncallAgent-Weather/1.0"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            curr = data.get("current", {})

            # 简易 WMO 天气代码映射
            code = curr.get("weather_code", 0)
            condition = "晴朗" if code == 0 else "多云/阴天" if code in [1, 2, 3] else "雨天" if code in [51, 53, 61, 63, 65] else "特殊天气"

            return {
                "city": city,
                "coordinates": {"latitude": lat, "longitude": lon},
                "temperature": f"{curr.get('temperature_2m')}°C",
                "apparent_temperature": f"{curr.get('apparent_temperature')}°C",
                "humidity": f"{curr.get('relative_humidity_2m')}%",
                "wind_speed": f"{curr.get('wind_speed_10m')} km/h",
                "condition": condition,
                "observation_time": curr.get("time"),
            }
    except Exception as e:
        return {
            "city": city,
            "error": f"获取天气失败: {str(e)}",
            "fallback": "暂时无法连接 Open-Meteo 公共接口，请检查外网连接。"
        }


if __name__ == "__main__":
    mcp.run(transport="stdio")
