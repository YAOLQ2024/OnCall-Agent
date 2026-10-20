"""OnCall Aegnt Python 版本

基于 LangChain 的智能业务代理系统
"""

__version__ = "1.0.0"

from app.config import config
from app.core.tracing import configure_langsmith

configure_langsmith(config)

from app.utils import logger  # noqa: E402,F401
