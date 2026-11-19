"""多连接配置管理、ACP 登录/自检后台任务与 Runtime 生命周期管理。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import uuid
import webbrowser

from sakura_cancellation import OperationCancelled
from sakura_model import ModelError

if __package__:
    from .agy_acp import AcpRuntime, AgyError, AgySettings, LoginFlow, run_probe as probe
    from .agy_acp.errors import to_model_error
    from .agy_acp.paths import cleanup_stale_dirs
else:
    from agy_acp import AcpRuntime, AgyError, AgySettings, LoginFlow, run_probe as probe
    from agy_acp.errors import to_model_error
    from agy_acp.paths import cleanup_stale_dirs

SERVICE_KEY = "sakura.model.antigravity"

_CONFIG_DEFAULTS = {
    "command": "",
    "thought_level": "default",
    "proxy_mode": "inherit",
    "proxy_url": "",
    "enable_tools": True,
    "enable_images": True,
    "auth_bridge": True,
    "request_timeout": 120,
}


@dataclass
class AccountOperation:
    result: dict
    cancelled: threading.Event = field(default_factory=threading.Event)
    worker: threading.Thread | None = None
    login_flow: LoginFlow | None = None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None


def _normalize_profile(raw: dict) -> dict:
    profile = dict(raw)
    for key, default in _CONFIG_DEFAULTS.items():
        profile.setdefault(key, default)
    profile.setdefault("models", [])
    profile.setdefault("loggedIn", False)
    profile.setdefault("cliVersion", "")
    profile.setdefault("capabilities", {})
    return profile


class AntigravityProfiles:
    def __init__(self, context) -> None:
        self.context = context
        self.lock = threading.RLock()
        saved = context.config.get()
        if "profiles" not in saved or not isinstance(saved.get("profiles"), list):
            if context.config.replace({**saved, "profiles": []}) == "error":
                raise ModelError("CONFIG_APPLY_FAILED", "Antigravity 连接配置初始化失败。")
            saved = context.config.get()
        self.active = {"profiles": [_normalize_profile(p) for p in saved.get("profiles", [])]}
        self.runtimes: dict[str, AcpRuntime] = {}
        self.operations: dict[str, AccountOperation] = {}
        self.service = None
        self.closed = False
        self.logger = context.get("sakura.host.logging")
        context.config.on_change(self.apply_config)
        cleanup_stale_dirs(Path(self.context.data_path("runtimes")))

    def apply_config(self, values: dict) -> str:
        with self.lock:
            raw_profiles = values.get("profiles", []) if isinstance(values, dict) else []
            self.active = {"profiles": [_normalize_profile(p) for p in raw_profiles]}
        return "applied"

    def set_service(self, service) -> None:
        self.service = service
        self.context.effect(self.close)

    def profile(self, profile_id: str) -> dict:
        item = next(
            (p for p in self.active["profiles"] if p["profileId"] == profile_id),
            None,
        )
        if item is None:
            raise ModelError("MODEL_REFERENCE_INVALID", "此 Antigravity 模型服务已不存在，请重新选择。")
        return item

    def _settings_for(self, profile: dict) -> AgySettings:
        try:
            return AgySettings.from_dict(profile)
        except AgyError as error:
            raise to_model_error(error) from error

    def runtime_for(self, profile_id: str) -> AcpRuntime:
        with self.lock:
            if self.closed:
                raise OperationCancelled()
            profile = self.profile(profile_id)
            runtime = self.runtimes.get(profile_id)
            if runtime is None or runtime.stopped:
                settings = self._settings_for(profile)
                runtime = AcpRuntime(
                    settings, Path(self.context.data_path(f"runtimes/{profile_id}"))
                )
                self.runtimes[profile_id] = runtime
            return runtime

    def _drop_runtime(self, profile_id: str) -> None:
        with self.lock:
            runtime = self.runtimes.pop(profile_id, None)
        if runtime is not None:
            runtime.close()

    def close_scope(self, plugin_id: str, scope_id: str) -> None:
        prefix = f"{plugin_id}:{scope_id}:"
        with self.lock:
            runtimes = list(self.runtimes.values())
        for runtime in runtimes:
            loop = runtime._loop
            if loop is None or loop.is_closed():
                continue

            async def _close_matching() -> None:
                for key, turn in list(runtime.turns.items()):
                    if key.startswith(prefix):
                        await turn.close()
                        runtime.turns.pop(key, None)
                async with runtime.pool_lock:
                    for key, session in list(runtime.sessions.items()):
                        if key.startswith(prefix):
                            await session.close()
                            runtime.sessions.pop(key, None)

            try:
                asyncio.run_coroutine_threadsafe(_close_matching(), loop).result(timeout=2.0)
            except Exception:
                pass

    def catalog(self) -> list[dict]:
        with self.lock:
            return [
                {
                    "profileId": p["profileId"],
                    "label": p["label"],
                    "models": deepcopy(p["models"]),
                }
                for p in self.active["profiles"]
            ]

    def describe(self, profile_id: str, model_id: str) -> dict:
        with self.lock:
            profile = self.profile(profile_id)
            model = next(
                (m for m in profile["models"] if m["modelId"] == model_id),
                None,
            )
            if model is None:
                raise ModelError("MODEL_REFERENCE_INVALID", "此模型不在当前连接目录中，请重新选择模型。")
            window = model.get("contextWindowTokens")
            supports_image = bool(
                (profile.get("capabilities") or {})
                .get("promptCapabilities", {})
                .get("image", True)
            )
            modalities = model.get("inputModalities")
            if modalities is None:
                modalities = ["text", "image"] if (supports_image and profile.get("enable_images", True)) else ["text"]
            supports_tools = model.get("supportsTools")
            if supports_tools is None:
                supports_tools = bool(profile.get("enable_tools", True))
            return {
                "contextWindowTokens": window or 32768,
                "contextWindowSource": "provider" if window else "fallback",
                "inputModalities": list(modalities),
                "supportsTools": bool(supports_tools),
            }

    def resolve(self, profile_id: str, model_id: str) -> dict:
        with self.lock:
            self.describe(profile_id, model_id)
            profile = self.profile(profile_id)
            caller_id = getattr(self.context, "caller_id", None) or "direct"
            caller_scope = getattr(self.context, "caller_scope", None) or "default"
            return {
                "profileId": profile_id,
                "model": model_id,
                "session_key": f"{caller_id}:{caller_scope}:{profile_id}",
                "proxy_url": profile.get("proxy_url", ""),
                "timeout_seconds": profile.get("request_timeout", 120),
            }

    def resolve_probe(self, profile_id: str, values: dict, require_model: bool = False) -> dict:
        with self.lock:
            profile = self.profile(profile_id)
            model = values["modelId"] if require_model else values.get("modelId", "")
            return {
                "profileId": profile_id,
                "model": model,
                "session_key": None,
                "proxy_url": profile.get("proxy_url", ""),
                "timeout_seconds": values.get("timeout_seconds", profile.get("request_timeout", 120)),
            }

    def execute(self, settings: dict, request: dict, *, cancel_checker, progress, operation: str = "generate") -> dict:
        profile_id = settings["profileId"]
        runtime = self.runtime_for(profile_id)
        return runtime.execute_sync(
            request,
            profile_id=profile_id,
            model=settings["model"],
            session_key=settings.get("session_key"),
            cancel_checker=cancel_checker,
            progress=progress,
            operation=operation,
        )

    def _update_profiles(self, profiles: list[dict]) -> None:
        if self.context.config.update({"profiles": profiles}) == "error":
            raise ModelError("CONFIG_APPLY_FAILED", "Antigravity 配置未能应用，请重试。")

    def load(self) -> dict:
        return {"operationRequest": {}, **self.status({})["values"]}

    def add(self, values: dict) -> dict:
        with self.lock:
            profiles = deepcopy(self.active["profiles"])
            labels = {p["label"] for p in profiles}
            label, number = "Google Antigravity", 1
            while label in labels:
                number += 1
                label = f"Google Antigravity {number}"
            created_at = values.get("operationRequest", {}).get("createdAt", int(time.time() * 1000))
            profiles.append(
                _normalize_profile(
                    {
                        "profileId": uuid.uuid4().hex,
                        "label": label,
                        "createdAt": created_at,
                    }
                )
            )
            self._update_profiles(profiles)
            return self.status({})

    def rename(self, values: dict) -> dict:
        request = values["operationRequest"]
        label = str(request.get("label", "")).strip()
        if not label:
            raise ModelError("FIELD_REQUIRED", "请填写模型服务名称。")
        with self.lock:
            self.profile(request["profileId"])
            self._update_profiles(
                [
                    dict(p, label=label) if p["profileId"] == request["profileId"] else p
                    for p in self.active["profiles"]
                ]
            )
            return self.status({})

    def configure(self, values: dict) -> dict:
        request = values["operationRequest"]
        profile_id = request["profileId"]
        with self.lock:
            current = self.profile(profile_id)
            candidate = dict(current)
            for key in _CONFIG_DEFAULTS:
                if key in request:
                    candidate[key] = request[key]
            # Validate via AgySettings before persisting.
            self._settings_for(candidate)
            self._update_profiles(
                [
                    candidate if p["profileId"] == profile_id else p
                    for p in self.active["profiles"]
                ]
            )
        self._drop_runtime(profile_id)
        return self.status({})

    def remove(self, values: dict) -> dict:
        profile_id = values["operationRequest"]["profileId"]
        with self.lock:
            self.profile(profile_id)
            self._update_profiles(
                [p for p in self.active["profiles"] if p["profileId"] != profile_id]
            )
            operation = self.operations.pop(profile_id, None)
            if operation is not None:
                self._cancel_operation(operation)
        self._drop_runtime(profile_id)
        if operation is not None and operation.worker:
            operation.worker.join(2)
        return self.status({})

    def status(self, _values: dict) -> dict:
        with self.lock:
            profiles = []
            for profile in self.active["profiles"]:
                profile_id = profile["profileId"]
                operation = self.operations.get(profile_id)
                runtime = self.runtimes.get(profile_id)
                profiles.append(
                    {
                        **deepcopy(profile),
                        "poolStats": runtime.pool_stats() if runtime else None,
                        "operationResult": deepcopy(operation.result) if operation else {},
                    }
                )
            return {"values": {"profiles": profiles}}

    def _cancel_operation(self, operation: AccountOperation) -> None:
        operation.cancelled.set()
        loop, task, flow = operation.loop, operation.task, operation.login_flow
        if loop is not None and not loop.is_closed():
            if flow is not None:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(flow.close()))
            if task is not None:
                loop.call_soon_threadsafe(task.cancel)

    def start(self, values: dict) -> dict:
        request = values["operationRequest"]
        kind = request["operation"]
        identity = request["requestId"]
        profile_id = request["profileId"]
        with self.lock:
            if self.closed:
                raise OperationCancelled()
            profile_snapshot = deepcopy(self.profile(profile_id))
            settings_obj = self._settings_for(profile_snapshot)
            previous = self.operations.get(profile_id)
            if previous is not None:
                self._cancel_operation(previous)
            operation = AccountOperation(
                {"requestId": identity, "state": "running", "operation": kind}
            )
            self.operations[profile_id] = operation

            def check() -> None:
                if operation.cancelled.is_set() or self.closed:
                    raise OperationCancelled()

            def publish(patch: dict) -> None:
                with self.lock:
                    if operation.cancelled.is_set():
                        return
                    operation.result.update(patch)

            def save_probe_result(report: dict) -> None:
                models = report.get("models") or []
                with self.lock:
                    check()
                    updated = [
                        dict(
                            p,
                            loggedIn=True,
                            cliVersion=report.get("cliVersion", ""),
                            capabilities=report.get("capabilities", {}),
                            models=models,
                        )
                        if p["profileId"] == profile_id
                        else p
                        for p in self.active["profiles"]
                    ]
                    self._update_profiles(updated)

            def run() -> None:
                started = time.monotonic()
                fields = {"operation_id": identity, "profile_id": profile_id, "stage": kind}
                if kind == "test_connection":
                    fields["model_id"] = request["modelId"]
                self.logger.info("Antigravity 操作开始", fields=fields)
                failure = None
                probe_root = Path(self.context.data_path("probes"))
                try:
                    if kind == "test_connection":
                        probe_settings = self.resolve_probe(
                            profile_id, request, require_model=True
                        )
                        response = self.service.execute(
                            probe_settings,
                            {},
                            cancel_checker=check,
                            progress=lambda _e: None,
                            operation="test_connection",
                        )
                        result = {
                            "state": "completed",
                            "message": "模型请求完成。",
                            "modelId": probe_settings["model"],
                            "reply": response["message"]["content"],
                        }
                    elif kind == "login":
                        async def do_login() -> dict:
                            operation.loop = asyncio.get_running_loop()

                            async def verify() -> dict:
                                return await probe(settings_obj, probe_root)

                            flow = LoginFlow(
                                settings_obj,
                                probe_root,
                                verify,
                                on_update=lambda patch: publish(
                                    {
                                        "loginUrl": patch.get("loginUrl", ""),
                                        "loginState": patch.get("state", ""),
                                    }
                                ),
                            )
                            operation.login_flow = flow
                            task = asyncio.create_task(flow.run())
                            operation.task = task
                            return await task

                        report = asyncio.run(do_login())
                        save_probe_result(report)
                        models = report.get("models") or []
                        result = {
                            "state": "completed",
                            "message": f"登录成功，已同步 {len(models)} 个模型。",
                            "loginUrl": "",
                        }
                    else:
                        # sync_models / probe
                        async def do_probe() -> dict:
                            operation.loop = asyncio.get_running_loop()
                            task = asyncio.create_task(probe(settings_obj, probe_root))
                            operation.task = task
                            return await task

                        report = asyncio.run(do_probe())
                        save_probe_result(report)
                        models = report.get("models") or []
                        result = {
                            "state": "completed",
                            "message": f"已同步 {len(models)} 个模型。"
                            if models
                            else "未检测到可用模型。",
                        }
                except (OperationCancelled, asyncio.CancelledError):
                    result = {"state": "cancelled", "message": "已取消。", "loginUrl": ""}
                except Exception as error:
                    if isinstance(error, AgyError):
                        error = to_model_error(error)
                    secrets = (settings_obj.proxy_url,) if settings_obj.proxy_url else ()
                    diagnostics = self.context.exception_diagnostics(
                        error, secrets=secrets
                    )
                    failure = diagnostics
                    result = {
                        "state": "failed",
                        "code": getattr(error, "code", "ANTIGRAVITY_OPERATION_FAILED"),
                        "message": diagnostics["diagnostic"],
                        "diagnostics": diagnostics,
                        "loginUrl": "",
                    }
                with self.lock:
                    if operation.cancelled.is_set():
                        result = {"state": "cancelled", "message": "已取消。", "loginUrl": ""}
                    result["elapsedMs"] = round((time.monotonic() - started) * 1000)
                    operation.result = {"requestId": identity, "operation": kind, **result}
                fields = {**fields, "state": result["state"], "elapsed_ms": result["elapsedMs"]}
                if failure and result["state"] != "cancelled":
                    self.logger.warning("Antigravity 操作失败", fields={**fields, **failure})
                else:
                    self.logger.info("Antigravity 操作结束", fields=fields)

            operation.worker = threading.Thread(
                target=run, name="antigravity-settings", daemon=True
            )
            operation.worker.start()
        return self.status({})

    def submit_code(self, values: dict) -> dict:
        request = values.get("operationRequest") or {}
        profile_id = request.get("profileId")
        request_id = request.get("requestId")
        code = request.get("code", "")
        with self.lock:
            operation = self.operations.get(profile_id)
            if (
                operation is None
                or operation.result.get("requestId") != request_id
                or operation.login_flow is None
            ):
                raise ModelError("LOGIN_EXPIRED", "当前没有等待授权码的登录任务。")
            flow = operation.login_flow
        try:
            flow.submit_threadsafe(code)
        except AgyError as error:
            raise to_model_error(error) from error
        return self.status({})

    def cancel(self, values: dict) -> dict:
        request = values.get("operationRequest", {})
        with self.lock:
            operation = self.operations.get(request.get("profileId"))
            if operation and request.get("requestId") == operation.result.get("requestId"):
                self._cancel_operation(operation)
        return {}

    def open_login(self, values: dict) -> dict:
        request = values.get("operationRequest") or {}
        profile_id = request.get("profileId")
        with self.lock:
            operation = self.operations.get(profile_id)
            url = (operation.result.get("loginUrl") if operation else "") or ""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "accounts.google.com":
            raise ModelError("LOGIN_URL_INVALID", "当前没有有效的 Google 授权链接。")
        if not webbrowser.open(url):
            raise ModelError("ANTIGRAVITY_BROWSER_OPEN_FAILED", f"无法打开浏览器，请手动访问 {url}。")
        return {}

    def logout(self, values: dict) -> dict:
        profile_id = values["operationRequest"]["profileId"]
        with self.lock:
            self.profile(profile_id)
            operation = self.operations.pop(profile_id, None)
            if operation is not None:
                self._cancel_operation(operation)
            self._update_profiles(
                [
                    dict(p, loggedIn=False, cliVersion="") if p["profileId"] == profile_id else p
                    for p in self.active["profiles"]
                ]
            )
        self._drop_runtime(profile_id)
        self.logger.info("Antigravity 连接已重置登录状态", fields={"profile_id": profile_id})
        return self.status({})

    def close(self) -> None:
        with self.lock:
            self.closed = True
            operations = list(self.operations.values())
            for operation in operations:
                self._cancel_operation(operation)
            runtimes = list(self.runtimes.values())
            self.runtimes.clear()
        for operation in operations:
            if operation.worker and operation.worker is not threading.current_thread():
                operation.worker.join(2)
        for runtime in runtimes:
            runtime.close()

    def register_settings(self) -> None:
        settings = self.context.get("sakura.host.settings")
        settings.register(
            {
                "sectionId": "account",
                "title": "Google Antigravity",
                "order": 30,
                "presentation": {
                    "component": "module",
                    "standalone": True,
                    "source": Path(__file__)
                    .with_name("frontend")
                    .joinpath("account.js")
                    .read_text(encoding="utf-8"),
                },
                "fields": [
                    {
                        "key": "profiles",
                        "label": "连接",
                        "type": "data",
                        "default": [],
                        "readonly": True,
                    },
                    {
                        "key": "operationRequest",
                        "label": "操作",
                        "type": "data",
                        "default": {},
                    },
                ],
                "actions": [
                    {"actionId": key, "label": label}
                    for key, label in (
                        ("add", "添加模型服务"),
                        ("rename", "修改名称"),
                        ("configure", "保存连接设置"),
                        ("remove", "删除模型服务"),
                        ("start", "开始"),
                        ("submitCode", "提交授权码"),
                        ("status", "查看状态"),
                        ("cancel", "取消"),
                        ("openLogin", "打开登录页面"),
                        ("logout", "退出登录"),
                    )
                ],
            },
            load=self.load,
            save=lambda _values: {"applicationState": "applied"},
            actions={
                "add": self.add,
                "rename": self.rename,
                "configure": self.configure,
                "remove": self.remove,
                "start": self.start,
                "submitCode": self.submit_code,
                "status": self.status,
                "cancel": self.cancel,
                "openLogin": self.open_login,
                "logout": self.logout,
            },
        )
        settings.place("account", page_id="host:providers", order=30)
