"""No-generation preflight for CLI discovery, capabilities, and real model catalog."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

from .config import AgySettings
from .errors import AgyError
from .models import catalog_names, choices, find_option, options, select
from .paths import windows_long_path
from .process import discover, launch_args, make_home
from .transport import AcpClient


def capability_summary(capabilities: object) -> dict:
    source = capabilities if isinstance(capabilities, dict) else {}
    result = {}
    for group, keys in (
        ("mcpCapabilities", ("http", "sse")),
        ("promptCapabilities", ("image", "audio", "embeddedContext")),
    ):
        values = source.get(group)
        values = values if isinstance(values, dict) else {}
        result[group] = {key: values.get(key) is True for key in keys}
    return result


async def probe(
    settings: AgySettings,
    root: Path,
    *,
    model: str = "",
    effort: str = "default",
) -> dict:
    report: dict = {
        "status": "blocked",
        "stage": "discovery",
        "cliVersion": "",
        "capabilities": {},
        "models": [],
        "efforts": [],
        "toolBridgeTransport": (
            "loopback_token"
            if os.name == "nt"
            else "unix_socket"
            if os.name == "posix"
            else "unsupported"
        ),
    }
    async with asyncio.timeout(settings.startup_timeout):
        report["stage"] = "discovery"
        info = await discover(settings)
        version = re.fullmatch(
            r"(?:agy(?: version)?\s+)?v?(\d{1,4}\.\d{1,4}\.\d{1,4})",
            info.version.strip(),
            re.IGNORECASE,
        )
        report["cliVersion"] = version[1] if version else info.version
        report["stage"] = "isolation"
        root = windows_long_path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="preflight-", dir=root) as directory:
            report["stage"] = "authentication"
            home = await asyncio.to_thread(
                make_home,
                Path(directory),
                "Preflight only; no generation.",
                settings=settings,
            )
            client = AcpClient(settings)
            try:
                report["stage"] = "initialize"
                await client.start(launch_args(info, home), home / "workspace")
                caps = capability_summary(client.capabilities)
                report["capabilities"] = caps
                report["stage"] = "session/new"
                result = await client.new_session(home / "workspace")
                report["stage"] = "model_catalog"
                names = catalog_names(result)
                supports_image = bool(caps.get("promptCapabilities", {}).get("image"))
                modalities = ["text", "image"] if supports_image else ["text"]
                report["models"] = [
                    {
                        "modelId": key,
                        "label": name,
                        "inputModalities": list(modalities),
                        "supportsTools": bool(settings.enable_tools),
                    }
                    for key, name in names.items()
                ]
                config = options(result)
                if model:
                    report["stage"] = "model_selection"
                    config = await select(client, result, model, effort)
                report["efforts"] = [
                    value
                    for value in choices(find_option(config, "thought_level"))
                    if value in {"low", "medium", "high", "xhigh"}
                ]
                report["stage"] = "complete"
                report["status"] = "passed"
                return report
            finally:
                await client.close()
