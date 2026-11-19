"""Public errors for Antigravity ACP and mapping to Sakura ModelError."""

from __future__ import annotations

import math
import re

from sakura_cancellation import OperationCancelled
from sakura_model import ModelError


class AgyError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int | None = None,
        retry_after: int | None = None,
        origin_turn_id: str | None = None,
        no_retry: bool = False,
        no_fallback: bool = False,
        stage: str = "",
    ) -> None:
        self.code = code
        self.raw_message = message
        self.exit_code = exit_code if type(exit_code) is int else None
        self.retry_after = (
            retry_after if type(retry_after) is int and retry_after >= 0 else None
        )
        self.origin_turn_id = (
            origin_turn_id
            if isinstance(origin_turn_id, str) and origin_turn_id
            else None
        )
        self.no_retry = bool(no_retry)
        self.no_fallback = bool(no_fallback)
        self.rpc_stage = (
            stage
            if stage
            in {
                "initialize",
                "session/new",
                "session/set_config_option",
                "session/prompt",
                "rpc",
            }
            else "idle"
        )
        super().__init__(f"AGY [{code}] {message}")


_MESSAGES = {
    "STARTUP_EXIT": (
        "CLI 已启动，但在完成 ACP 握手前退出。请检查 command 是否指向官方原生可执行文件，"
        "以及启动脚本的依赖、运行用户和隔离目录限制；这不等同于未安装或未登录。"
    ),
    "CLI_WRAPPER": (
        "CLI 启动脚本拒绝当前隔离目录或认证链接。请将 command 指向官方原生可执行文件，"
        "或更新旧启动脚本的目录限制。"
    ),
    "PROXY_AUTH": "AGY 代理认证失败，请检查代理账号或代理服务配置。",
    "PROXY": "AGY 无法通过代理连接，请检查代理协议、地址与服务状态。",
    "NETWORK": "AGY 网络连接失败，请检查当前运行环境的网络、DNS 和代理。",
    "TLS": "AGY TLS 验证失败，请检查证书链、系统时间与代理证书设置。",
    "AUTH": (
        "请用运行 Sakura 的同一系统用户执行官方 agy 登录。"
        "若普通 CLI 已登录而隔离 session/new 仍失败，请检查认证桥与官方认证后端。"
    ),
    "QUOTA": "AGY 账号额度不足，请检查官方 CLI 的额度状态。",
    "RATE_LIMIT": "AGY 请求受限，请稍后再试。",
    "TIMEOUT": "AGY 上游请求超时，请检查当前网络与 CLI 状态。",
    "UNSUPPORTED": "此 AGY 版本不支持所需 ACP 方法或参数，请检查 CLI 版本与配置。",
    "UPSTREAM": "AGY 返回错误；请在本机 CLI 检查登录、网络与模型可用性。",
}


def classified_error(
    code: str,
    *,
    stage: str = "",
    exit_code: int | None = None,
    retry_after: int | None = None,
    origin_turn_id: str | None = None,
    no_retry: bool = False,
) -> AgyError:
    """Rebuild an error from an allowlisted code, without upstream raw secrets."""
    if code not in _MESSAGES:
        code = "UPSTREAM"
    prefix = (
        f"阶段 {stage}；"
        if stage
        in {
            "initialize",
            "session/new",
            "session/set_config_option",
            "session/prompt",
            "rpc",
        }
        else ""
    )
    suffix = f" CLI 退出码：{exit_code}。" if type(exit_code) is int else ""
    return AgyError(
        code,
        prefix + _MESSAGES[code] + suffix,
        exit_code=exit_code,
        retry_after=retry_after,
        origin_turn_id=origin_turn_id,
        no_retry=no_retry,
        stage=stage,
    )


def extract_retry_after(error: object) -> int | None:
    """Extract bounded positive numeric retry_after metadata from structured errors."""
    if not isinstance(error, dict):
        return None
    candidates = [error]
    data = error.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    for source in candidates:
        for key in ("retry_after", "retryAfter", "retry_after_seconds"):
            raw = source.get(key)
            if type(raw) is int and raw > 0:
                return min(300, raw)
            if type(raw) is float and math.isfinite(raw) and raw > 0:
                return min(300, max(1, math.ceil(raw)))
    return None


def remote_error(error: object, *, retry_notice: bool = False) -> AgyError:
    retry_after = extract_retry_after(error)
    text = str(error).lower()
    eof_pattern = (
        r"\b(?:unexpected[\s_-]+)?eof\b"
        if retry_notice
        else r"\bunexpected[\s_-]+eof\b|:\s*eof\b"
    )
    patterns = (
        (
            "CLI_WRAPPER",
            r"refusing auth bridge outside agy plugin session directory|"
            r"invalid isolated acp directories|unexpected existing auth symlink|"
            r"unexpected existing isolated auth file; refusing overwrite",
        ),
        (
            "PROXY_AUTH",
            r"\b407\b|\bproxy[\s:_-]+(?:requires?[\s:_-]+)?"
            r"(?:authentication|authorization|auth|credentials?)\b|"
            r"\b(?:authentication|authorization|auth|credentials?)[\s:_-]+"
            r"(?:for[\s:_-]+|to[\s:_-]+)proxy\b|err_proxy_auth",
        ),
        (
            "PROXY",
            r"\bproxy[\s:_-]+(?:error|unavailable|refused|failed)\b|"
            r"\bproxy[\s:_-]+(?:connection|connect|tunnel)[\s:_-]+"
            r"(?:failed|failure|error|refused|reset)\b|err_proxy_connection|"
            r"(?:failed|unable).to.connect.to.(?:the.)?proxy|"
            r"\bsocks(?:5h?)?[\s:_-]+(?:handshake[\s:_-]+)?"
            r"(?:failed|failure|error|refused)\b|"
            r"\bconnect[\s:_-]+(?:tunnel[\s:_-]+)?(?:failed|failure|error)\b|"
            r"\btunnel[\s:_-]+(?:failed|failure|error)\b",
        ),
        (
            "TLS",
            r"\b(?:tls|ssl)[\s:_-]+(?:(?:handshake|connection|verification|verify)"
            r"[\s:_-]+)?(?:failed|failure|error|alert)\b|"
            r"certificate[\s:_-]+(?:(?:verify|verification|validation)[\s:_-]+)?"
            r"(?:failed|failure|error|expired|invalid|untrusted)\b|"
            r"\bcert_(?:has_expired|untrusted|authority_invalid)|"
            r"self[\s_-]signed[\s_-]cert|"
            r"unable[\s_-]to[\s_-](?:verify|get[\s_-]issuer)[\s_-]+"
            r"(?:the[\s_-]first[\s_-])?cert|err_(?:tls|ssl)_[a-z_]+",
        ),
        (
            "NETWORK",
            r"\bdns[\s:_-]+(?:resolution[\s:_-]+)?(?:failed|failure|error)\b|"
            r"enotfound|eai_again|econnrefused|econnreset|enetunreach|ehostunreach|"
            r"connection.(?:refused|reset)|network.(?:unreachable|error)|"
            r"getaddrinfo[\s:_-]+(?:failed|failure|error)|"
            r"name.or.service.not.known|failed.to.fetch|fetch.failed|"
            r"the stream was interrupted\. please continue|"
            + eof_pattern,
        ),
        (
            "AUTH",
            r"authentication.required|unauth|not.logged.in|sign.in|login|credential|\b401\b",
        ),
        (
            "QUOTA",
            r"quota[\s_-]+(?:exceeded|exhausted)|insufficient.credits|"
            r"resource.{0,32}exhausted.{0,16}\(quota\)|"
            r"(?:daily|monthly|account|subscription|billing).{0,80}(?:quota|limit|credits?).{0,40}"
            r"(?:exceeded|exhausted|reached|depleted)",
        ),
        ("RATE_LIMIT", r"rate.limit|too.many|\b429\b|resource_exhausted|capacity|\b503\b"),
        ("TIMEOUT", r"timeout|timed.out|deadline"),
        ("UNSUPPORTED", r"-32601|method.not.found|not.supported"),
    )
    for code, pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return classified_error(
                code,
                retry_after=retry_after if code in {"QUOTA", "RATE_LIMIT"} else None,
            )
    return classified_error("UPSTREAM")


def to_model_error(error: AgyError) -> ModelError:
    """Convert an AgyError to a Sakura ModelError with diagnostics."""
    if error.code == "CANCELLED":
        raise OperationCancelled() from error
    diagnostics: dict[str, object] = {
        "agyCode": error.code,
        "stage": error.rpc_stage,
    }
    if error.exit_code is not None:
        diagnostics["exitCode"] = error.exit_code
    if error.retry_after is not None:
        diagnostics["retryAfter"] = error.retry_after
    if error.origin_turn_id is not None:
        diagnostics["originTurnId"] = error.origin_turn_id

    if error.code in {"AUTH", "LOGIN_EXPIRED", "LOGIN_URL"}:
        diagnostics["httpStatus"] = 401
        diagnostics["faultDomain"] = "authentication"
        code = "MODEL_AUTHENTICATION_FAILED"
    elif error.code in {"PERMISSION", "AUTH_BRIDGE"}:
        diagnostics["httpStatus"] = 403
        diagnostics["faultDomain"] = "authentication"
        code = "MODEL_AUTHENTICATION_FAILED"
    elif error.code in {"QUOTA", "RATE_LIMIT"}:
        diagnostics["httpStatus"] = 429
        diagnostics["faultDomain"] = "rate_limit"
        code = "MODEL_RATE_LIMITED"
    elif error.code in {"TIMEOUT", "TOOL_TIMEOUT"}:
        diagnostics["faultDomain"] = "transport"
        code = "MODEL_REQUEST_TIMEOUT"
    elif error.code in {
        "NETWORK",
        "PROXY",
        "PROXY_AUTH",
        "TLS",
        "NOT_INSTALLED",
        "COMMAND_NOT_FOUND",
        "STARTUP_EXIT",
        "TRANSPORT",
        "LAUNCH",
    }:
        diagnostics["faultDomain"] = "transport"
        code = "MODEL_CONNECTION_FAILED"
    elif error.code == "INPUT_LIMIT":
        diagnostics["faultDomain"] = "context"
        code = "MODEL_CONTEXT_REJECTED"
    elif error.code in {"EMPTY", "PROTOCOL", "OUTPUT_LIMIT"}:
        diagnostics["faultDomain"] = "protocol"
        code = "MODEL_RESPONSE_INVALID"
    elif error.code in {"IMAGE", "IMAGE_BYTES", "IMAGE_PIXELS", "IMAGE_FORMAT", "IMAGE_MIME", "IMAGE_EOI", "IMAGE_CAPABILITY", "IMAGES_DISABLED", "IMAGE_COUNT", "IMAGE_URL", "IMAGE_PATH", "IMAGE_SSRF", "IMAGE_DOWNLOAD", "IMAGE_TIMEOUT"}:
        diagnostics["faultDomain"] = "provider"
        code = "MODEL_IMAGE_UNSUPPORTED" if error.code in {"IMAGE_CAPABILITY", "IMAGES_DISABLED"} else "MODEL_REQUEST_FAILED"
    else:
        diagnostics["faultDomain"] = "provider"
        code = "MODEL_REQUEST_FAILED"
    return ModelError(code, str(error), diagnostics=diagnostics)
