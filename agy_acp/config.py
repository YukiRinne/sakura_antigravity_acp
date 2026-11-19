"""Per-profile settings validation for Google Antigravity ACP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import AgyError

THOUGHT_LEVELS = ("default", "low", "medium", "high", "xhigh")
PROXY_MODES = ("inherit", "direct", "custom")
REUSE_MODES = ("strict", "structured")
CUSTOMIZATIONS_MODES = ("inherit", "minimal")


def _bounded_int(value: object, default: int, *, minimum: int, maximum: int, field: str) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgyError("CONFIG", f"配置项 {field} 必须是整数。")
    if not minimum <= value <= maximum:
        raise AgyError("CONFIG", f"配置项 {field} 须在 {minimum} 到 {maximum} 之间。")
    return value


def _validate_proxy_url(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AgyError("CONFIG", "自定义代理地址格式无效。")
    text = value.strip()
    if not text:
        return ""
    if any(ord(ch) < 33 for ch in text):
        raise AgyError("CONFIG", "自定义代理地址不能包含控制字符或空格。")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise AgyError("CONFIG", "代理地址仅支持 http、https、socks5 或 socks5h 协议。")
    return text


@dataclass(frozen=True)
class AgySettings:
    command: str = ""
    thought_level: str = "default"
    proxy_mode: str = "inherit"
    proxy_url: str = ""
    auth_bridge: bool = True
    enable_tools: bool = True
    enable_images: bool = True
    session_reuse_mode: str = "strict"
    agy_customizations_mode: str = "inherit"
    startup_timeout: int = 30
    request_timeout: int = 120
    idle_ttl: int = 600
    max_concurrent: int = 2
    max_sessions: int = 4
    max_pending: int = 16
    queue_timeout: int = 30
    history_bytes: int = 160_000
    max_output_bytes: int = 4_194_304
    image_max_count: int = 8
    image_max_bytes: int = 20 * 1024 * 1024
    image_max_pixels: int = 16_000_000
    image_timeout: int = 15
    media_roots: tuple[str, ...] = ()
    tool_max_calls: int = 16
    tool_max_concurrent: int = 1
    tool_approval_timeout: int = 60
    tool_execution_timeout: int = 180
    tool_result_bytes: int = 262_144
    quota_cooldown: int = 60
    empty_dedupe_ttl: int = 30
    context_compaction_min_peak: int = 64_000
    context_compaction_drop_percent: int = 50

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> AgySettings:
        data = dict(raw or {})
        command = str(data.get("command") or "").strip()
        if any(ord(ch) < 32 for ch in command):
            raise AgyError("CONFIG", "CLI 路径包含无效控制字符。")
        thought_level = str(data.get("thought_level") or "default").strip().lower()
        if thought_level not in THOUGHT_LEVELS:
            raise AgyError("CONFIG", f"思考档位仅支持：{', '.join(THOUGHT_LEVELS)}。")
        proxy_mode = str(data.get("proxy_mode") or "inherit").strip().lower()
        if proxy_mode not in PROXY_MODES:
            raise AgyError("CONFIG", f"代理模式仅支持：{', '.join(PROXY_MODES)}。")
        proxy_url = _validate_proxy_url(data.get("proxy_url"))
        if proxy_mode == "custom" and not proxy_url:
            raise AgyError("CONFIG", "选择自定义代理模式时请填写代理地址。")
        reuse_mode = str(data.get("session_reuse_mode") or "strict").strip().lower()
        if reuse_mode not in REUSE_MODES:
            raise AgyError("CONFIG", "会话复用模式无效。")
        customizations_mode = str(data.get("agy_customizations_mode") or "inherit").strip().lower()
        if customizations_mode not in CUSTOMIZATIONS_MODES:
            raise AgyError("CONFIG", "组件加载模式无效。")
        raw_roots = data.get("media_roots") or ()
        if not isinstance(raw_roots, (list, tuple)) or any(not isinstance(item, str) for item in raw_roots):
            raise AgyError("CONFIG", "media_roots 必须是路径字符串列表。")
        return cls(
            command=command,
            thought_level=thought_level,
            proxy_mode=proxy_mode,
            proxy_url=proxy_url,
            auth_bridge=bool(data.get("auth_bridge", True)),
            enable_tools=bool(data.get("enable_tools", True)),
            enable_images=bool(data.get("enable_images", True)),
            session_reuse_mode=reuse_mode,
            agy_customizations_mode=customizations_mode,
            startup_timeout=_bounded_int(data.get("startup_timeout"), 30, minimum=5, maximum=300, field="startup_timeout"),
            request_timeout=_bounded_int(data.get("request_timeout", data.get("timeout_seconds")), 120, minimum=5, maximum=1800, field="request_timeout"),
            idle_ttl=_bounded_int(data.get("idle_ttl"), 600, minimum=30, maximum=86400, field="idle_ttl"),
            max_concurrent=_bounded_int(data.get("max_concurrent"), 2, minimum=1, maximum=8, field="max_concurrent"),
            max_sessions=_bounded_int(data.get("max_sessions"), 4, minimum=1, maximum=32, field="max_sessions"),
            max_pending=_bounded_int(data.get("max_pending"), 16, minimum=1, maximum=128, field="max_pending"),
            queue_timeout=_bounded_int(data.get("queue_timeout"), 30, minimum=1, maximum=300, field="queue_timeout"),
            history_bytes=_bounded_int(data.get("history_bytes"), 160_000, minimum=4096, maximum=2_000_000, field="history_bytes"),
            max_output_bytes=_bounded_int(data.get("max_output_bytes"), 4_194_304, minimum=65_536, maximum=16_777_216, field="max_output_bytes"),
            image_max_count=_bounded_int(data.get("image_max_count"), 8, minimum=1, maximum=16, field="image_max_count"),
            tool_max_calls=_bounded_int(data.get("tool_max_calls"), 16, minimum=1, maximum=64, field="tool_max_calls"),
            tool_max_concurrent=_bounded_int(data.get("tool_max_concurrent"), 1, minimum=1, maximum=8, field="tool_max_concurrent"),
            tool_approval_timeout=_bounded_int(data.get("tool_approval_timeout"), 60, minimum=5, maximum=600, field="tool_approval_timeout"),
            tool_execution_timeout=_bounded_int(data.get("tool_execution_timeout"), 180, minimum=5, maximum=1800, field="tool_execution_timeout"),
            quota_cooldown=_bounded_int(data.get("quota_cooldown"), 60, minimum=0, maximum=300, field="quota_cooldown"),
            empty_dedupe_ttl=_bounded_int(data.get("empty_dedupe_ttl"), 30, minimum=0, maximum=300, field="empty_dedupe_ttl"),
            media_roots=tuple(raw_roots),
        )
