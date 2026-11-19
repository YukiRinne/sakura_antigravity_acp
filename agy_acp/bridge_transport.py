"""Private local MCP transport; pure stdlib for standalone stdio relay."""

from __future__ import annotations

import asyncio
import json
import secrets

VERSION = 1
FRAME_LIMIT = 1048576
TOKEN_ENV = "SAKURA_MCP_BRIDGE_TOKEN"
HANDSHAKE_TIMEOUT = 5


def encode(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _reject_constant(_value: str):
    raise ValueError("Invalid JSON number")


async def read_frame(reader, limit: int = FRAME_LIMIT) -> dict:
    line = await reader.readline()
    if not line.endswith(b"\n") or len(line) > limit:
        raise ValueError("Invalid bridge frame")
    value = json.loads(line, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Invalid bridge object")
    return value


class BridgeTransport:
    """Private IPC transport: Unix domain socket (POSIX) or token-authenticated loopback TCP (Windows)."""

    def __init__(self, tcp: bool = False) -> None:
        self.tcp = tcp
        self.token = secrets.token_hex(32) if tcp else ""

    async def receive(self, reader, writer) -> dict:
        if not self.tcp:
            return await read_frame(reader)
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            envelope = await read_frame(reader)
        token = envelope.get("token")
        request = envelope.get("request")
        if (
            type(envelope.get("version")) is not int
            or envelope["version"] != VERSION
            or not isinstance(token, str)
            or len(token) != 64
            or not token.isascii()
            or not secrets.compare_digest(token, self.token)
            or not isinstance(request, dict)
        ):
            raise ValueError("Bridge authentication failed")
        return request


async def connect(endpoint: str, request: dict, token: str):
    """Connect only to literal 127.0.0.1 loopback or an explicit private Unix socket."""
    if not endpoint.startswith("tcp:"):
        reader, writer = await asyncio.open_unix_connection(
            endpoint, limit=16 * FRAME_LIMIT
        )
        try:
            data = encode(request) + b"\n"
            if len(data) > FRAME_LIMIT:
                raise ValueError("Bridge frame too large")
            writer.write(data)
            await writer.drain()
            return reader, writer
        except BaseException:
            writer.close()
            raise
    port = int(endpoint[4:])
    if not 1 <= port <= 65535 or len(token) != 64 or not token.isascii():
        raise ValueError("Invalid bridge endpoint")
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, limit=16 * FRAME_LIMIT
    )
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            data = encode({"version": VERSION, "token": token, "request": request}) + b"\n"
            if len(data) > FRAME_LIMIT:
                raise ValueError("Bridge frame too large")
            writer.write(data)
            await writer.drain()
        return reader, writer
    except BaseException:
        writer.close()
        raise
