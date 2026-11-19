"""Bounded official agy CLI interactive login flow."""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgySettings
from .errors import AgyError
from .paths import windows_long_path
from .process import build_child_env, capture, discover, spawn, stop_process


class LoginFlow:
    def __init__(
        self,
        settings: AgySettings,
        root: Path,
        verify: Callable[[], object],
        on_update: Callable[[dict], None] | None = None,
    ) -> None:
        self.settings = settings
        self.root = windows_long_path(root)
        self.verify = verify
        self.on_update = on_update
        self.proc: asyncio.subprocess.Process | None = None
        self.url = ""
        self.state = "idle"
        self.submitted = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def _publish(self) -> None:
        if self.on_update is not None:
            self.on_update({"state": self.state, "loginUrl": self.url})

    async def submit(self, code: str) -> None:
        if self.state != "waiting" or self.submitted or not self.proc or not self.proc.stdin:
            raise AgyError("BUSY", "当前登录暂不接受授权码。")
        if (
            not isinstance(code, str)
            or not 1 <= len(code.strip()) <= 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in code)
        ):
            raise AgyError("CONFIG", "请输入浏览器显示的单行授权码（最多 4096 字符）。")
        self.submitted = True
        self.state = "verifying"
        self.url = ""
        self._publish()
        try:
            self.proc.stdin.write((code.strip() + "\n").encode("utf-8"))
            await asyncio.wait_for(self.proc.stdin.drain(), 5)
        except (OSError, TimeoutError):
            await self.close()
            raise AgyError("AUTH", "授权码未能交给 agy CLI，请重新登录。") from None

    def submit_threadsafe(self, code: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise AgyError("BUSY", "登录任务未处于可提交状态。")
        fut = asyncio.run_coroutine_threadsafe(self.submit(code), loop)
        fut.result(timeout=10)

    async def close(self) -> None:
        if self.proc is not None:
            try:
                await stop_process(self.proc)
            finally:
                self.proc = None
        self.url = ""
        if self.state in {"starting", "waiting", "verifying"}:
            self.state = "cancelled"
            self._publish()

    async def _drain(self, stream, ready: asyncio.Event) -> None:
        window = ""
        while chunk := await stream.read(2048):
            window = (window + chunk.decode("utf-8", "replace"))[-16384:]
            self._find_url(window, ready)
        self._find_url(window + "\n", ready)

    def _find_url(self, text: str, ready: asyncio.Event) -> None:
        if ready.is_set():
            return
        for match in re.finditer(
            r"""https://accounts\.google\.com/[^\s"'<>\x1b]{1,8192}(?=[\s"'<>\x1b])""",
            text,
        ):
            url = match[0]
            parsed = urlsplit(url)
            if parsed.netloc == "accounts.google.com" and not any(
                ord(c) < 32 for c in url
            ):
                self.url = url
                self.state = "waiting"
                self._publish()
                ready.set()
                return

    async def run(self) -> object:
        self._loop = asyncio.get_running_loop()
        self.state = "starting"
        self.url = ""
        self.submitted = False
        self._publish()
        readers: list[asyncio.Task] = []
        waiters: list[asyncio.Task] = []
        try:
            async with asyncio.timeout(300):
                info = await discover(self.settings)
                env = build_child_env(self.settings)
                if info.supports_login is None:
                    help_text = await capture(
                        [info.binary, "acp", "--help"],
                        self.settings.startup_timeout,
                        env=env,
                    )
                    supports_login = "--login" in help_text
                else:
                    supports_login = info.supports_login
                if not supports_login:
                    raise AgyError(
                        "UNSUPPORTED", "此 agy CLI 不支持面板登录，请在终端运行 agy 完成登录。"
                    )
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix="login-", dir=self.root) as directory:
                    try:
                        self.proc = await spawn(
                            [info.binary, "acp", "--login"], Path(directory), env=env
                        )
                        ready = asyncio.Event()
                        readers = [
                            asyncio.create_task(self._drain(stream, ready))
                            for stream in (self.proc.stdout, self.proc.stderr)
                        ]
                        exited = asyncio.create_task(self.proc.wait())
                        link = asyncio.create_task(ready.wait())
                        waiters = [exited, link]
                        done, _ = await asyncio.wait(
                            waiters, timeout=45, return_when=asyncio.FIRST_COMPLETED
                        )
                        if not done:
                            raise AgyError(
                                "LOGIN_URL",
                                "CLI 未返回授权链接，请确认本机可直接运行 agy acp --login。",
                            )
                        code = await exited
                        self.state, self.url = "verifying", ""
                        self._publish()
                        if code != 0:
                            raise AgyError("AUTH", "官方登录未成功，请重新登录。")
                        report = await self.verify()
                        self.state = "complete"
                        self._publish()
                        return report
                    finally:
                        if self.proc is not None:
                            try:
                                await stop_process(self.proc)
                            finally:
                                self.proc = None
        except TimeoutError:
            self.state = "failed"
            self._publish()
            raise AgyError("TIMEOUT", "Antigravity 登录超时，请重试。") from None
        except asyncio.CancelledError:
            self.state = "cancelled"
            self._publish()
            raise
        finally:
            for task in readers + waiters:
                task.cancel()
            await asyncio.gather(*readers, *waiters, return_exceptions=True)
            self.proc = None
            self.url = ""
