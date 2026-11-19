"""Bounded native ACP session pool, reuse arbitration, and Sakura execution adapter."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sakura_cancellation import OperationCancelled, check_cancelled

from .config import THOUGHT_LEVELS, AgySettings
from .errors import AgyError, classified_error, to_model_error
from .failure_guard import FailureGuard
from .media import prepare_sakura_media
from .messages import (
    ConversationInput,
    attach_transient_data,
    prompt_blocks,
)
from .models import catalog, select
from .notices import strip_upstream_capacity_notices
from .paths import remove_tree_robust, windows_long_path
from .process import (
    LaunchInfo,
    compose_system_prompt,
    discover,
    launch_args,
    make_home,
)
from .tool_turn import chat_events
from .tools import PendingCall, ToolBroker, ToolSnapshot
from .transport import AcpClient, Event

_UNSET = object()


def compare_history(expected: list[tuple], incoming: list[tuple]) -> str:
    if not expected:
        return "unknown"
    if expected == incoming:
        return "exact"
    if 0 < len(incoming) < len(expected) and expected[-len(incoming) :] == incoming:
        return "host_trim"
    if len(incoming) > len(expected) and incoming[: len(expected)] == expected:
        return "append_gap"
    return "rewrite"


@dataclass
class ReuseDecision:
    reuse: bool
    reasons: list[str]
    history_relation: str


def classify_reuse(
    session: Session,
    request: ConversationInput,
    model: str,
    effort: str,
    snapshot: ToolSnapshot,
    *,
    temporary: bool = False,
) -> ReuseDecision:
    incoming = request.signatures[:-1]
    history_relation = compare_history(session.host_expected, incoming)
    reasons: list[str] = []
    if temporary:
        reasons.append("temporary")
    client = session.client
    if client is None:
        if not temporary:
            reasons.append("cold_start")
    else:
        if not client.alive:
            reasons.append("process_dead")
        if session.system != request.system:
            reasons.append("system_changed")
        if session.model != model:
            reasons.append("model_changed")
        if session.effort != effort:
            reasons.append("effort_changed")
        if session.tool_signature != snapshot.signature:
            reasons.append("tools_changed")
        if history_relation != "exact":
            reasons.append(f"history_{history_relation}")
    reuse = not reasons and history_relation == "exact"
    return ReuseDecision(
        reuse=reuse, reasons=reasons, history_relation=history_relation
    )


def normalize_usage(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    usage: dict = {}
    prompt = raw.get("prompt_tokens", raw.get("input_tokens", raw.get("inputTokens")))
    completion = raw.get(
        "completion_tokens", raw.get("output_tokens", raw.get("outputTokens"))
    )
    total = raw.get("total_tokens", raw.get("totalTokens"))
    if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0:
        usage["prompt_tokens"] = prompt
    if (
        isinstance(completion, int)
        and not isinstance(completion, bool)
        and completion >= 0
    ):
        usage["completion_tokens"] = completion
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        usage["total_tokens"] = total
    elif "prompt_tokens" in usage and "completion_tokens" in usage:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    cached = raw.get("cachedReadTokens", raw.get("cached_tokens"))
    if cached is None and isinstance(raw.get("prompt_tokens_details"), dict):
        cached = raw["prompt_tokens_details"].get("cached_tokens")
    if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return usage


@dataclass
class Session:
    client: AcpClient | None = None
    broker: ToolBroker | None = None
    tool_signature: str = ""
    home: Path | None = None
    system: str = ""
    turn_counter: int = 0
    model: str = ""
    effort: str = ""
    host_expected: list[tuple] = field(default_factory=list)
    busy: bool = False
    used_at: float = field(default_factory=time.monotonic)
    lifecycle: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self) -> None:
        async with self.lifecycle:
            await self.close_unlocked()

    async def close_unlocked(self) -> None:
        try:
            try:
                if self.broker:
                    await self.broker.close()
            finally:
                self.broker = None
                if self.client:
                    await self.client.close()
        finally:
            self.client = None
            if self.home:
                await asyncio.to_thread(remove_tree_robust, self.home)
                self.home = None
            self.host_expected.clear()
            self.system = ""
            self.turn_counter = 0
            self.model = ""
            self.effort = ""
            self.tool_signature = ""


class AcpRuntime:
    def __init__(
        self,
        settings: AgySettings,
        root: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.root = windows_long_path(root / ("runtime-" + uuid.uuid4().hex))
        self.sessions: dict[str, Session] = {}
        self.key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.pool_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(settings.max_concurrent)
        self.callers = 0
        self.stopped = False
        self.info: LaunchInfo | None = None
        self.info_lock = asyncio.Lock()
        self.models_lock = asyncio.Lock()
        self.model_cache: list[str] = []
        self.model_cache_time = 0.0
        self.sweeper: asyncio.Task | None = None
        self.turns: dict = {}
        self.entry_locks: dict[str, bool] = {}
        self.failure_guard = FailureGuard(clock=clock)

        self._loop_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self.stopped:
                raise AgyError("CLOSED", "插件已停用或重新加载。")
            if self._loop is not None and self._thread and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def runner() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()

            thread = threading.Thread(
                target=runner, name="sakura-agy-loop", daemon=True
            )
            thread.start()
            ready.wait()
            self._loop = loop
            self._thread = thread
            return loop

    def run_sync(
        self, coro, *, cancel_checker: Callable[[], None] | None = None
    ):
        """Run a coroutine on this runtime's persistent event loop with cancellation."""
        loop = self._ensure_loop()
        done = threading.Event()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        future.add_done_callback(lambda _f: done.set())
        try:
            while not done.wait(0.05):
                check_cancelled(cancel_checker)
            check_cancelled(cancel_checker)
            return future.result()
        except OperationCancelled:
            loop.call_soon_threadsafe(future.cancel)
            done.wait(2.0)
            raise
        except BaseException:
            if not future.done():
                loop.call_soon_threadsafe(future.cancel)
                done.wait(2.0)
            raise

    def pool_stats(self) -> dict:
        live_processes = sum(
            1
            for s in self.sessions.values()
            if s.client is not None and s.client.alive
        )
        return {
            "activeSessions": len(self.sessions),
            "maxSessions": self.settings.max_sessions,
            "liveProcesses": live_processes,
            "activeToolTurns": len(self.turns),
            "cooldowns": self.failure_guard.snapshot(),
        }

    async def launch_info(self) -> LaunchInfo:
        async with self.info_lock:
            if self.stopped:
                raise AgyError("CLOSED", "插件已停用或重新加载。")
            if self.info is None:
                self.info = await discover(self.settings)
            return self.info

    @asynccontextmanager
    async def lease(self, key: str, temporary: bool):
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用或重新加载。")
        if self.callers >= self.settings.max_pending + self.settings.max_concurrent:
            raise AgyError("BUSY", "AGY 等待队列已满，请稍后再试。")
        self.callers += 1
        lock, refs = self.key_locks.get(key, (asyncio.Lock(), 0))
        self.key_locks[key] = (lock, refs + 1)
        locked = acquired = False
        session = None
        try:
            try:
                async with asyncio.timeout(self.settings.queue_timeout):
                    await lock.acquire()
                    locked = True
                    await self.slots.acquire()
                    acquired = True
            except TimeoutError:
                raise AgyError("BUSY", "等待 AGY 会话超时，请稍后再试。") from None
            async with self.pool_lock:
                if self.stopped:
                    raise AgyError("CLOSED", "插件已停用或重新加载。")
                if self.sweeper is None:
                    self.sweeper = asyncio.create_task(
                        self._sweep(), name="agy:idle-reaper"
                    )
                session = self.sessions.get(key)
                if session is None:
                    if len(self.sessions) >= self.settings.max_sessions:
                        idle = [(k, s) for k, s in self.sessions.items() if not s.busy]
                        if not idle:
                            raise AgyError("BUSY", "AGY 会话池已满。")
                        old_key, old = min(idle, key=lambda pair: pair[1].used_at)
                        await old.close()
                        del self.sessions[old_key]
                    session = self.sessions[key] = Session()
                session.busy = True
            yield session
        finally:
            try:
                if session is not None:
                    async with self.pool_lock:
                        session.used_at = time.monotonic()
                        session.busy = False
                        if (
                            temporary
                            or self.stopped
                            or session.client is None
                            or not session.client.alive
                        ):
                            await session.close()
                            if self.sessions.get(key) is session:
                                del self.sessions[key]
            finally:
                if acquired:
                    self.slots.release()
                if locked:
                    lock.release()
                _, refs = self.key_locks[key]
                if refs == 1:
                    del self.key_locks[key]
                else:
                    self.key_locks[key] = (lock, refs - 1)
                self.callers -= 1

    async def open(
        self, session: Session, system: str, snapshot: ToolSnapshot | None = None
    ) -> dict:
        async with session.lifecycle:
            return await self._open_unlocked(session, system, snapshot)

    async def _open_unlocked(
        self, session: Session, system: str, snapshot: ToolSnapshot | None = None
    ) -> dict:
        await session.close_unlocked()
        info = await self.launch_info()
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用。")
        tools_mode = bool(snapshot and snapshot.tools)
        session.home = await asyncio.to_thread(
            make_home,
            self.root,
            system,
            tools_mode=tools_mode,
            settings=self.settings,
        )
        session.client = AcpClient(self.settings)
        try:
            await session.client.start(
                launch_args(
                    info,
                    session.home,
                    use_default_agent=tools_mode,
                    customizations_mode=self.settings.agy_customizations_mode,
                ),
                session.home / "workspace",
            )
            servers = []
            if tools_mode and snapshot is not None:
                session.broker = ToolBroker(snapshot, self.settings)
                servers = [await session.broker.start()]
                session.client.tool_broker = session.broker
            injected_system = (
                compose_system_prompt(system, mcp_tools=tools_mode)
                if system.strip()
                else ""
            )
            result = await session.client.new_session(
                session.home / "workspace",
                servers,
                system_prompt=injected_system,
            )
            if session.broker:
                session.broker.session_id = session.client.session_id
            session.tool_signature = snapshot.signature if tools_mode and snapshot else ""
            if self.stopped:
                raise AgyError("CLOSED", "插件已停用。")
            return result
        except BaseException:
            await session.close_unlocked()
            raise

    async def stream(
        self,
        request: ConversationInput,
        key: str | None,
        model: str,
        effort: str,
        snapshot: ToolSnapshot,
        *,
        profile_id: str = "",
        request_signature: str | None = None,
        claimed_half_open: tuple[str, str] | None = None,
    ) -> AsyncIterator[Event]:
        temporary = key is None
        async with self.lease(key or uuid.uuid4().hex, temporary) as session:
            completed = False
            failure_exc: BaseException | None = None
            cancelled = False
            request_sent = False
            turn_id = uuid.uuid4().hex[:12]
            try:
                if profile_id:
                    check = await self.failure_guard.check_empty_only(
                        (profile_id, model),
                        request_signature,
                        session_key=key,
                        empty_ttl=self.settings.empty_dedupe_ttl,
                        continuation=False,
                    )
                    if not check.allowed:
                        self.failure_guard.release_probe(claimed_half_open)
                        claimed_half_open = None
                        raise AgyError(
                            "EMPTY",
                            f"AGY 短期抑制重复空回复（请约 {check.retry_after} 秒后再试）。",
                            retry_after=check.retry_after,
                            origin_turn_id=check.origin_turn_id,
                            no_retry=True,
                        )

                decision = classify_reuse(
                    session,
                    request,
                    model,
                    effort,
                    snapshot,
                    temporary=temporary,
                )
                if decision.reuse:
                    turn_index = session.turn_counter + 1
                    prompt, _omitted = prompt_blocks(
                        request.messages[-1:], self.settings.history_bytes
                    )
                else:
                    turn_index = 1
                    prompt, _omitted = prompt_blocks(
                        request.messages, self.settings.history_bytes
                    )
                    result = await self.open(session, request.system, snapshot)
                    assert session.client
                    await select(session.client, result, model, effort)
                    session.system = request.system
                    session.model = model
                    session.effort = effort

                prompt = attach_transient_data(prompt, request.transient_context)
                assert session.client
                if any(
                    p.get("type") == "image" for p in prompt
                ) and not session.client.capabilities.get("promptCapabilities", {}).get(
                    "image"
                ):
                    raise AgyError("IMAGE_CAPABILITY", "当前 CLI/model 没有声明图片输入能力。")
                if session.broker:
                    session.broker.count = 0
                    session.broker.result_bytes = 0
                    session.broker.notifications.clear()
                    session.broker.error = None

                text_parts: list[str] = []
                session.client.prompt_sent = False
                request_sent = True
                turn = tool_events(session.client, prompt, session.broker)
                try:
                    async for event in turn:
                        if event.kind == "text":
                            text_parts.append(event.text)
                        if event.kind == "done":
                            result = event.result or {}
                            reason = result.get("stopReason")
                            if reason == "cancelled":
                                raise AgyError("CANCELLED", "AGY 取消了本轮请求。")
                            if reason not in {"end_turn", "max_tokens"}:
                                raise AgyError(
                                    "STOP",
                                    "AGY 未正常结束本轮，请检查 CLI 状态。",
                                )
                            raw_answer = "".join(text_parts)[len(request.consumed_text) :]
                            answer, stripped_notices = strip_upstream_capacity_notices(
                                raw_answer
                            )
                            if not answer.strip():
                                if result.get("agy_retry_error"):
                                    raise classified_error(result["agy_retry_error"])
                                if stripped_notices:
                                    raise AgyError(
                                        "RATE_LIMIT",
                                        "AGY 上游容量不可用，请稍后重试。",
                                    )
                                if reason == "end_turn" and getattr(
                                    session.client, "permission_errors", None
                                ):
                                    raise AgyError(
                                        "TOOL_PERMISSION",
                                        "AGY 工具权限请求被安全策略拒绝，CLI 结束了本轮且未返回正文。",
                                    )
                                raise AgyError("EMPTY", "AGY 没有返回正文。")
                            assistant_sig = (
                                "assistant",
                                answer,
                                (),
                                "",
                            )
                            session.host_expected = [*request.signatures, assistant_sig]
                            session.turn_counter = turn_index
                            if profile_id:
                                self.failure_guard.record_success((profile_id, model))
                            claimed_half_open = None
                            completed = True
                            yield Event(
                                "done",
                                text=answer,
                                result={
                                    **result,
                                    "usage": normalize_usage(
                                        result.get("usage")
                                        or getattr(session.client, "latest_usage", None)
                                    ),
                                },
                            )
                            return
                        yield event
                finally:
                    await turn.aclose()
            except (asyncio.CancelledError, GeneratorExit) as exc:
                cancelled = True
                failure_exc = exc
                raise
            except BaseException as exc:
                failure_exc = exc
                raise
            finally:
                short_circuited = (
                    isinstance(failure_exc, AgyError)
                    and failure_exc.no_retry
                    and not request_sent
                )
                if short_circuited:
                    self.failure_guard.release_probe(claimed_half_open)
                elif not completed:
                    error_code = (
                        failure_exc.code
                        if isinstance(failure_exc, AgyError)
                        else "CANCELLED"
                        if cancelled
                        else "UPSTREAM"
                    )
                    if profile_id and not cancelled:
                        self.failure_guard.record_failure(
                            (profile_id, model),
                            request_signature,
                            error_code,
                            origin_turn_id=turn_id,
                            retry_after=getattr(failure_exc, "retry_after", None),
                            session_key=key,
                            empty_ttl=self.settings.empty_dedupe_ttl,
                            quota_ttl=self.settings.quota_cooldown,
                            continuation=False,
                        )
                    else:
                        self.failure_guard.release_probe(claimed_half_open)
                    await session.close()
                elif (
                    session.client is not None
                    and session.client.context_usage.suspected_compaction
                ):
                    await session.close()

    async def execute_turn(
        self,
        request: dict,
        *,
        profile_id: str,
        model: str,
        session_key: str | None,
        progress: Callable[[dict], None],
        operation: str = "generate",
    ) -> dict:
        if operation == "test_connection":
            raw_messages = [{"role": "user", "content": "Reply with only OK."}]
            tools_raw: list[dict] = []
            tool_choice = "none"
            response_format = None
            stream = False
            session_key = None
        else:
            raw_messages = list(request.get("messages") or [])
            tools_raw = list(request.get("tools") or [])
            tool_choice = request.get("toolChoice", "auto")
            response_format = request.get("responseFormat")
            stream = bool(request.get("stream", False))

        normalized_messages = await prepare_sakura_media(raw_messages, self.settings)
        use_tools = bool(self.settings.enable_tools and tools_raw and tool_choice != "none")
        snapshot = ToolSnapshot(tools_raw) if use_tools else ToolSnapshot()
        conv_input = ConversationInput.build(
            normalized_messages,
            response_format=response_format,
            has_tools=bool(snapshot.tools),
        )

        params = request.get("parameters") or {}
        extra_body = params.get("extra_body") or {}
        effort = str(
            extra_body.get("thought_level")
            or params.get("thought_level")
            or self.settings.thought_level
            or "default"
        ).lower()
        if effort not in THOUGHT_LEVELS:
            effort = self.settings.thought_level

        is_tool_continuation = bool(
            conv_input.messages and conv_input.messages[-1].get("role") == "tool"
        )

        scope = (profile_id, model)
        guard_check = await self.failure_guard.check(
            scope,
            conv_input.request_signature,
            session_key=session_key,
            empty_ttl=self.settings.empty_dedupe_ttl,
            quota_ttl=self.settings.quota_cooldown,
            continuation=is_tool_continuation,
        )
        if not guard_check.allowed:
            raise AgyError(
                guard_check.code or "RATE_LIMIT",
                f"AGY 模型冷却中（请约 {guard_check.retry_after} 秒后再试）。",
                retry_after=guard_check.retry_after,
                origin_turn_id=guard_check.origin_turn_id,
                no_retry=True,
            )

        if session_key is not None and (snapshot.tools or is_tool_continuation):
            source = chat_events(
                self,
                conv_input,
                session_key,
                model,
                effort,
                snapshot,
                profile_id=profile_id,
                request_signature=conv_input.request_signature,
                claimed_half_open=guard_check.half_open_scope,
            )
        else:
            source = self.stream(
                conv_input,
                session_key,
                model,
                effort,
                snapshot,
                profile_id=profile_id,
                request_signature=conv_input.request_signature,
                claimed_half_open=guard_check.half_open_scope,
            )

        thoughts: list[str] = []
        usage: dict = {}
        pending = ""
        last_emit = time.monotonic()

        def flush_pending(force: bool = False) -> None:
            nonlocal pending, last_emit
            if not stream or not pending:
                return
            while len(pending) >= 2048:
                progress({"type": "text_delta", "text": pending[:2048]})
                pending = pending[2048:]
                last_emit = time.monotonic()
            if pending and (force or time.monotonic() - last_emit >= 0.05):
                progress({"type": "text_delta", "text": pending})
                pending = ""
                last_emit = time.monotonic()

        try:
            async for event in source:
                if event.kind == "thought":
                    thoughts.append(event.text)
                elif event.kind == "usage" and isinstance(event.result, dict):
                    usage = normalize_usage(event.result.get("usage")) or usage
                elif event.kind == "text":
                    if stream:
                        pending += event.text
                        flush_pending(False)
                elif event.kind == "tools":
                    flush_pending(True)
                    call: PendingCall = event.result["call"]
                    provider_data = {}
                    if thoughts:
                        provider_data["thought"] = "".join(thoughts)
                    return {
                        "message": {
                            "role": "assistant",
                            "content": event.text,
                            "toolCalls": [
                                {
                                    "id": call.id,
                                    "name": call.name,
                                    "arguments": json.dumps(
                                        call.arguments, ensure_ascii=False
                                    ),
                                }
                            ],
                            **({"providerData": provider_data} if provider_data else {}),
                        },
                        "usage": usage,
                        "finishReason": "tool_calls",
                    }
                elif event.kind == "done":
                    flush_pending(True)
                    result = event.result or {}
                    final_usage = normalize_usage(result.get("usage")) or usage
                    stop_reason = result.get("stopReason")
                    finish = "length" if stop_reason == "max_tokens" else "stop"
                    provider_data = {}
                    if thoughts:
                        provider_data["thought"] = "".join(thoughts)
                    return {
                        "message": {
                            "role": "assistant",
                            "content": event.text,
                            "toolCalls": [],
                            **({"providerData": provider_data} if provider_data else {}),
                        },
                        "usage": final_usage,
                        "finishReason": finish,
                    }
            raise AgyError("EMPTY", "AGY 未返回结果。")
        finally:
            await source.aclose()

    def execute_sync(
        self,
        request: dict,
        *,
        profile_id: str,
        model: str,
        session_key: str | None,
        cancel_checker: Callable[[], None] | None,
        progress: Callable[[dict], None],
        operation: str = "generate",
    ) -> dict:
        try:
            return self.run_sync(
                self.execute_turn(
                    request,
                    profile_id=profile_id,
                    model=model,
                    session_key=session_key,
                    progress=progress,
                    operation=operation,
                ),
                cancel_checker=cancel_checker,
            )
        except AgyError as error:
            raise to_model_error(error) from error

    async def _sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(30, self.settings.idle_ttl))
                async with self.pool_lock:
                    now = time.monotonic()
                    for key, session in list(self.sessions.items()):
                        if (
                            not session.busy
                            and now - session.used_at >= self.settings.idle_ttl
                        ):
                            await session.close()
                            del self.sessions[key]
        except asyncio.CancelledError:
            return

    async def aclose(self) -> None:
        self.stopped = True
        await asyncio.gather(*(turn.close() for turn in list(self.turns.values())))
        self.turns.clear()
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        async with self.pool_lock:
            await asyncio.gather(
                *(session.close() for session in self.sessions.values())
            )
            self.sessions.clear()
        try:
            self.root.rmdir()
        except OSError:
            pass

    def close(self) -> None:
        with self._loop_lock:
            self.stopped = True
            loop = self._loop
            thread = self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self.aclose(), loop)
            fut.result(timeout=5.0)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        thread.join(timeout=2.0)


async def tool_events(
    client: AcpClient, prompt: list[dict], broker: ToolBroker | None
) -> AsyncIterator[Event]:
    """Multiplex MCP pending calls without blocking ACP's stdout reader."""
    source = client.generate(prompt)
    next_event = next_call = None
    try:
        next_event = asyncio.create_task(anext(source))
        if broker is not None:
            next_call = asyncio.create_task(broker.ready.get())
        while True:
            tasks = [t for t in (next_event, next_call) if t]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if next_event.done():
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                yield event
                next_event = asyncio.create_task(anext(source))
            elif next_call is not None and next_call.done():
                call = next_call.result()
                if call.id in broker.pending and not call.future.done():
                    yield Event(
                        "tools",
                        result={"call": call, "broker": broker, "client": client},
                    )
                next_call = asyncio.create_task(broker.ready.get())
    finally:
        tasks = [t for t in (next_event, next_call) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await source.aclose()
