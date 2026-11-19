"""Retain one native ACP prompt across Sakura's tool-call / tool-result rounds."""

from __future__ import annotations

import asyncio
import copy
import json

from .errors import AgyError
from .messages import ConversationInput
from .tools import PendingCall, ToolBroker, ToolSnapshot
from .transport import Event


def _same_arguments(left_raw: str, right_dict: dict) -> bool:
    try:
        return json.loads(left_raw or "{}") == right_dict
    except (ValueError, TypeError):
        return False


def _continuation_exchange(
    messages: list[dict], baseline: list[dict], call: PendingCall, text: str
) -> list[dict]:
    """Authenticate the pending exchange independently of prefix rewrites."""
    if len(messages) < 2:
        raise AgyError("TOOL_HISTORY", "工具续轮缺少完整调用与结果消息。")
    assistant_msg = messages[-2]
    tool_msg = messages[-1]
    if assistant_msg.get("role") != "assistant" or tool_msg.get("role") != "tool":
        raise AgyError("TOOL_HISTORY", "工具续轮尾部消息角色无效。")
    if tool_msg.get("tool_call_id") != call.id:
        raise AgyError("TOOL_HISTORY", "工具续轮结果 ID 与当前挂起调用不匹配。")

    matched_call = next(
        (
            item
            for item in (assistant_msg.get("tool_calls") or [])
            if item.get("id") == call.id
            and item.get("name") == call.name
            and _same_arguments(item.get("arguments", "{}"), call.arguments)
        ),
        None,
    )
    if matched_call is None:
        raise AgyError("TOOL_HISTORY", "工具续轮缺少匹配的已审计调用。")

    known_calls = {
        item["id"]: item
        for message in baseline
        for item in (message.get("tool_calls") or [])
    }
    known_results = {
        message["tool_call_id"]: message
        for message in baseline
        if message.get("role") == "tool"
    }
    for message in messages[:-2]:
        if any(
            item != known_calls.get(item.get("id"))
            for item in (message.get("tool_calls") or [])
        ) or (
            message.get("role") == "tool"
            and message != known_results.get(message.get("tool_call_id"))
        ):
            raise AgyError("TOOL_HISTORY", "工具续轮混入了未审计或被改写的工具记录。")
    return messages[-2:]


class ToolTurn:
    def __init__(
        self,
        runtime,
        request: ConversationInput,
        key: str,
        model: str,
        effort: str,
        snapshot: ToolSnapshot,
        *,
        profile_id: str = "",
        request_signature: str | None = None,
        claimed_half_open: tuple[str, str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.request = request
        self.key = key
        self.model = model
        self.effort = effort
        self.snapshot = snapshot
        self.profile_id = profile_id
        self.request_signature = request_signature
        self.claimed_half_open = claimed_half_open
        self.queue: asyncio.Queue[Event | BaseException] = asyncio.Queue(maxsize=128)
        self.resume = asyncio.Event()
        self.done_event = asyncio.Event()
        self.waiting: dict | None = None
        self.segment: list[str] = []
        self.finished = False
        self.task = asyncio.create_task(self._run(), name="agy:tool-turn")

    async def _put(self, item: Event | BaseException) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            raise AgyError("BACKPRESSURE", "工具轮消费者过慢。") from None

    async def _run(self) -> None:
        source = self.runtime.stream(
            self.request,
            self.key,
            self.model,
            self.effort,
            self.snapshot,
            profile_id=self.profile_id,
            request_signature=self.request_signature,
            claimed_half_open=self.claimed_half_open,
        )
        try:
            async for event in source:
                if event.kind == "text":
                    self.segment.append(event.text)
                    await self._put(event)
                elif event.kind == "tools":
                    self.waiting = event.result
                    self.resume.clear()
                    await self._put(
                        Event("tools", result=event.result, text="".join(self.segment))
                    )
                    resumed = asyncio.create_task(self.resume.wait())
                    failed = asyncio.create_task(
                        event.result["client"].failure_event.wait()
                    )
                    try:
                        async with asyncio.timeout(
                            self.runtime.settings.tool_approval_timeout
                            + self.runtime.settings.tool_execution_timeout
                        ):
                            done, _ = await asyncio.wait(
                                (resumed, failed, event.result["call"].future),
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if failed in done:
                                raise event.result["client"].failed
                            if not self.resume.is_set():
                                raise AgyError("TOOL_TIMEOUT", "工具已取消或超时；不会重放。")
                    finally:
                        resumed.cancel()
                        failed.cancel()
                        await asyncio.gather(resumed, failed, return_exceptions=True)
                    self.segment.clear()
                    self.waiting = None
                else:
                    await self._put(event)
        except TimeoutError:
            await self._error(AgyError("TOOL_TIMEOUT", "宿主工具执行未在期限内回填。"))
        except asyncio.CancelledError:
            await self._error(AgyError("CANCELLED", "工具轮已取消；已交接调用状态未知。"))
        except AgyError as exc:
            await self._error(exc)
        except Exception:
            await self._error(AgyError("TOOL_PROTOCOL", "工具轮异常。"))
        finally:
            try:
                await source.aclose()
            finally:
                self.finished = True
                if self.runtime.turns.get(self.key) is self:
                    del self.runtime.turns[self.key]
                self.done_event.set()

    async def _error(self, exc: BaseException) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(exc)

    def accept(
        self,
        request: ConversationInput,
        model: str,
        effort: str,
        snapshot: ToolSnapshot,
    ) -> None:
        if self.finished or not self.waiting:
            raise AgyError("TOOL_STALE", "没有可续接的工具请求。")
        client = self.waiting["client"]
        if (
            self.task.cancelling()
            or not client.alive
            or client.prompt_terminal
            or asyncio.get_running_loop().time() >= client.prompt_deadline
        ):
            raise AgyError("TOOL_STALE", "原 ACP prompt 已结束，拒绝迟到结果。")
        # Sakura's AssistantModelClient merges trailing runtime_context into system_prompt
        # once role=tool messages appear in round 2+, so allow system to start with round 1's system.
        system_matches = (
            request.system == self.request.system
            or (self.request.system and request.system.startswith(self.request.system))
        )
        if (
            model != self.model
            or effort != self.effort
            or snapshot.signature != self.snapshot.signature
            or not system_matches
        ):
            raise AgyError("TOOL_CONTEXT", "工具续轮上下文、模型或工具集发生变化。")
        call: PendingCall = self.waiting["call"]
        exchange = _continuation_exchange(
            request.messages, self.request.messages, call, "".join(self.segment)
        )
        broker: ToolBroker = self.waiting["broker"]
        broker.resolve(call.id, exchange[1]["content"])
        self.request.consumed_text += "".join(self.segment)
        self.request.messages.extend(copy.deepcopy(exchange))
        self.request.signatures.extend(request.signatures[-2:])
        self.resume.set()

    async def events(self):
        keep = completed = False
        try:
            while True:
                item = await self.queue.get()
                if isinstance(item, BaseException):
                    raise item
                if item.kind == "tools":
                    if self.finished:
                        raise AgyError("TOOL_STALE", "工具请求已失效。")
                    item.result["broker"].handoff(item.result["call"])
                    keep = True
                    yield item
                    return
                if item.kind == "done":
                    completed = True
                yield item
                if completed:
                    await self.task
                    return
        finally:
            if not keep:
                await self.close()
                if self.runtime.turns.get(self.key) is self:
                    del self.runtime.turns[self.key]

    async def close(self) -> None:
        if not self.task.cancelling():
            self.task.cancel()
        cleanup = asyncio.gather(self.task, return_exceptions=True)
        cancelled = None
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                if cleanup.done():
                    break
        if cancelled:
            raise cancelled


async def chat_events(
    runtime,
    request: ConversationInput,
    key: str,
    model: str,
    effort: str,
    snapshot: ToolSnapshot,
    *,
    profile_id: str = "",
    request_signature: str | None = None,
    claimed_half_open: tuple[str, str] | None = None,
):
    """Route tool continuation turns or start a fresh turn on the session key."""
    if key in runtime.entry_locks:
        raise AgyError("BUSY", "同一会话入口正在处理请求。")
    runtime.entry_locks[key] = True
    turn: ToolTurn | None = runtime.turns.get(key)
    try:
        is_tool_continuation = bool(
            request.messages and request.messages[-1].get("role") == "tool"
        )
        if turn is not None:
            if not is_tool_continuation:
                # Caller abandoned a parked tool turn and started a fresh turn.
                if turn.waiting is not None:
                    await turn.close()
                    if runtime.turns.get(key) is turn:
                        del runtime.turns[key]
                    turn = None
                else:
                    raise AgyError("BUSY", "当前会话存在进行中的工具轮，请稍后再试。")
            else:
                try:
                    turn.accept(request, model, effort, snapshot)
                except BaseException:
                    await turn.close()
                    if runtime.turns.get(key) is turn:
                        runtime.turns.pop(key, None)
                    raise
        elif is_tool_continuation:
            raise AgyError("TOOL_STALE", "工具轮已结束或不存在，无法回填工具结果。")

        if turn is None:
            if (
                len(runtime.turns)
                >= runtime.settings.max_pending + runtime.settings.max_concurrent
            ):
                raise AgyError("BUSY", "工具轮队列已满。")
            turn = ToolTurn(
                runtime,
                request,
                key,
                model,
                effort,
                snapshot,
                profile_id=profile_id,
                request_signature=request_signature,
                claimed_half_open=claimed_half_open,
            )
            runtime.turns[key] = turn
        source = turn.events()
        try:
            async for event in source:
                yield event
        finally:
            await source.aclose()
    finally:
        runtime.entry_locks.pop(key, None)
