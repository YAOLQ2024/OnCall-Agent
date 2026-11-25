"""Single-process session operation guard, including the lifetime of SSE responses."""

import asyncio
import inspect
from functools import wraps
from weakref import WeakValueDictionary

from fastapi import HTTPException
from starlette.responses import Response
from sse_starlette.sse import EventSourceResponse

_locks: WeakValueDictionary = WeakValueDictionary()


class _SessionStreamResponse(Response):
    def __init__(self, response: EventSourceResponse, lock: asyncio.Lock):
        self.response = response
        self.lock = lock
        self.status_code = response.status_code
        self.raw_headers = response.raw_headers
        self.media_type = response.media_type
        self.background = None

    async def __call__(self, scope, receive, send):
        try:
            await self.response(scope, receive, send)
        finally:
            self.lock.release()


def guard_session(endpoint):
    """Reject concurrent chat/delete on one session; separate sessions remain independent."""
    signature = inspect.signature(endpoint)

    @wraps(endpoint)
    async def guarded(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        request = values.get("request")
        sid = values.get("session_id")
        if sid is None:
            sid = getattr(request, "id", None) or getattr(request, "session_id", None) or "default"
        key = (values["uid"], sid)
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        if lock.locked():
            raise HTTPException(status_code=409, detail="当前会话正在处理请求，请完成后重试")
        await lock.acquire()
        transferred = False
        try:
            result = await endpoint(*args, **kwargs)
            if isinstance(result, EventSourceResponse):
                transferred = True
                return _SessionStreamResponse(result, lock)
            return result
        finally:
            if not transferred:
                lock.release()

    return guarded
