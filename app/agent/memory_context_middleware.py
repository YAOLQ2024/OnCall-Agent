"""Apply private memory only to the outgoing model call, after context preparation."""

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage

from app.models.memory_runtime import MEMORY_POLICY, MemoryRunContext, reference_message

class MemoryContextMiddleware(AgentMiddleware):
    @staticmethod
    def _prepare(request: ModelRequest):
        memory = request.runtime.context
        if not isinstance(memory, MemoryRunContext) or not memory.reference:
            return request
        messages = list(request.messages)
        last_user = next((index for index in range(len(messages) - 1, -1, -1)
                          if isinstance(messages[index], HumanMessage)), len(messages))
        messages.insert(last_user, reference_message(memory))
        system = request.system_message
        if system is None:
            index = next((i for i, msg in enumerate(messages) if isinstance(msg, SystemMessage)), None)
            if index is not None:
                messages[index] = MemoryContextMiddleware._add_policy(messages[index])
            else:
                system = SystemMessage(content=MEMORY_POLICY)
        else:
            system = MemoryContextMiddleware._add_policy(system)
        return request.override(messages=messages, system_message=system)

    @staticmethod
    def _add_policy(system: SystemMessage) -> SystemMessage:
        content = system.content
        if isinstance(content, str):
            content += "\n\n" + MEMORY_POLICY
        else:
            content = [*content, {"type": "text", "text": MEMORY_POLICY}]
        return system.model_copy(update={"content": content})

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))
