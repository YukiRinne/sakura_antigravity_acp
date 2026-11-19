"""Recognize bounded standalone CLI notices across streaming chunks."""

from __future__ import annotations

import re
from enum import Enum

from .errors import classified_error, remote_error


class UpstreamState(str, Enum):
    UNKNOWN = "UNKNOWN"
    ACCOUNT_QUOTA_EXHAUSTED = "ACCOUNT_QUOTA_EXHAUSTED"
    RATE_LIMIT_OR_CAPACITY = "RATE_LIMIT_OR_CAPACITY"
    TRANSIENT_RETRY = "TRANSIENT_RETRY"
    AUTH_FATAL = "AUTH_FATAL"
    AGENT_FATAL = "AGENT_FATAL"


_DETAIL_FREE_PATTERN = re.compile(
    r"resource has been exhausted \(e\.g\. check quota\)", re.IGNORECASE
)
_STRUCTURED_DETAIL_PATTERN = re.compile(
    r"retryinfo|quotafailure|errorinfo|retrydelay|details[\"'\\s]*[:=\\[]",
    re.IGNORECASE,
)


def detail_free_exhausted(text: str) -> bool:
    if not text:
        return False
    if _STRUCTURED_DETAIL_PATTERN.search(text):
        return False
    return bool(_DETAIL_FREE_PATTERN.search(text))


def classify_notice(text: str) -> UpstreamState:
    text = text.lower()
    if re.search(
        r"(?:daily|monthly|account|subscription|billing)[\s\S]{0,80}"
        r"(?:quota|limit|credits?)[\s\S]{0,40}(?:exceeded|exhausted|reached|depleted)\b|"
        r"\bquota[\s_-]+(?:exceeded|exhausted)\b|\binsufficient credits\b",
        text,
    ):
        return UpstreamState.ACCOUNT_QUOTA_EXHAUSTED
    if re.search(
        r"\b401\b|\bunauthenticated\b|\binvalid (?:credentials?|token)\b|"
        r"\bauthentication required\b|\btoken (?:has )?expired\b",
        text,
    ):
        return UpstreamState.AUTH_FATAL
    if re.search(r"\b429\b|\bresource_exhausted\b|\btoo many requests\b", text):
        return UpstreamState.RATE_LIMIT_OR_CAPACITY
    if re.search(r"\b503\b|\bunavailable\b|\bno capacity\b", text):
        return UpstreamState.TRANSIENT_RETRY
    if "agent execution terminated due to error." in text:
        return UpstreamState.AGENT_FATAL
    return UpstreamState.UNKNOWN


class RetryNoticeGuard:
    """Discard complete CLI retry and stream-interrupt notices before streaming."""

    heads = ("⚠ API error (attempt ", "⚠️ API error (attempt ")
    interrupt_heads = (
        "⚠ The stream was interrupted. Please continue",
        "⚠️ The stream was interrupted. Please continue",
    )
    header = re.compile(r"^⚠\ufe0f? API error \(attempt [0-9]+\):")
    interrupt_header = re.compile(
        r"^⚠\ufe0f? The stream was interrupted\. Please continue"
    )
    continuation_prefixes = ('"http://', '"https://')
    continuation_header = re.compile(r'^"https?://')
    max_continuation_lines = 2
    max_diagnostic_bytes = 4096

    def __init__(self) -> None:
        self.pending = ""
        self.leading_blanks = ""
        self.emitted = False
        self.line_start = True
        self.dropping = False
        self.continuing = False
        self._can_continue = False
        self.continuation_lines = 0
        self.diagnostic = ""
        self._notice_diagnostic = ""
        self.error_code = ""
        self.state = UpstreamState.UNKNOWN
        self.fast_fail_code = ""
        self.rate_limit_notices = 0
        self._rate_counted = False
        self._attempt = ""
        self._last_rate_attempt = None
        self.detail_free = False
        self.detail_free_seen = False

    def _possible(self, text: str) -> bool:
        for head in self.heads:
            if head.startswith(text):
                return True
            if text.startswith(head):
                tail = text[len(head) :]
                if re.fullmatch(r"[0-9]+\)?", tail):
                    return True
        return any(head.startswith(text) for head in self.interrupt_heads)

    def _possible_continuation(self, text: str) -> bool:
        return bool(text) and any(
            prefix.startswith(text) for prefix in self.continuation_prefixes
        )

    def _append_diagnostic(self, text: str, *, separator: str = "") -> None:
        if separator and len(self._notice_diagnostic) < self.max_diagnostic_bytes:
            self._notice_diagnostic += separator
        notice_room = max(0, self.max_diagnostic_bytes - len(self._notice_diagnostic))
        notice_added = text[:notice_room]
        line_room = max(0, self.max_diagnostic_bytes - len(self.diagnostic))
        line_added = text[: min(notice_room, line_room)]
        if line_added:
            self.diagnostic += line_added
        if notice_added or not self.error_code:
            self._notice_diagnostic += notice_added
            self.error_code = remote_error(
                self._notice_diagnostic, retry_notice=True
            ).code
            self.state = classify_notice(self._notice_diagnostic)
            self.detail_free = (
                self.state == UpstreamState.RATE_LIMIT_OR_CAPACITY
                and detail_free_exhausted(self._notice_diagnostic)
            )
            if self.detail_free:
                self.detail_free_seen = True
            if self.state == UpstreamState.ACCOUNT_QUOTA_EXHAUSTED:
                self.fast_fail_code = "QUOTA"
            elif self.state == UpstreamState.AUTH_FATAL:
                self.fast_fail_code = "AUTH"
            elif self.state == UpstreamState.RATE_LIMIT_OR_CAPACITY:
                if not self._rate_counted and self._attempt != self._last_rate_attempt:
                    self.rate_limit_notices += 1
                    self._rate_counted = True
                    self._last_rate_attempt = self._attempt
                if self.rate_limit_notices >= 2:
                    self.fast_fail_code = "RATE_LIMIT"

    def _start_notice(self, text: str, *, can_continue: bool) -> None:
        self.dropping = True
        self.continuing = False
        self._can_continue = can_continue
        self.continuation_lines = 0
        self.diagnostic = ""
        self._notice_diagnostic = ""
        self._rate_counted = False
        matched = re.search(r"\(attempt ([0-9]+)\):", text)
        self._attempt = matched.group(1) if matched else ""
        self._append_diagnostic(text)

    def _drop_line(self, newline: str) -> None:
        if newline:
            self._finish_dropped_line()
        self.line_start = bool(newline)

    def _finish_dropped_line(self) -> None:
        self.dropping = False
        if self.continuing:
            if self.continuation_lines >= self.max_continuation_lines:
                self.continuing = False
        elif self._can_continue:
            self.continuing = True
            self._can_continue = False

    def _end_continuation(self) -> None:
        self.continuing = False
        self.continuation_lines = 0

    def _emit(self, output: list[str], chunk: str) -> None:
        if not chunk:
            return
        if self.leading_blanks:
            output.append(self.leading_blanks)
            self.leading_blanks = ""
        output.append(chunk)
        self.emitted = True

    def feed(self, text: str, *, final: bool = False) -> str:
        text = self.pending + text
        self.pending = ""
        output: list[str] = []
        while text:
            line, newline, rest = text.partition("\n")
            if self.dropping:
                self._append_diagnostic(line.rstrip("\r"))
                if newline:
                    self._finish_dropped_line()
                text = rest
                continue
            if self.line_start and self.continuing:
                trimmed = line.rstrip("\r")
                if self.continuation_header.match(trimmed):
                    self.continuation_lines += 1
                    self._append_diagnostic(trimmed, separator="\n")
                    self._drop_line(newline)
                    text = rest
                    continue
                if (
                    not newline
                    and not final
                    and len(line) <= self.max_diagnostic_bytes
                    and self._possible_continuation(line)
                ):
                    self.pending = line
                    break
                self._end_continuation()
            if self.line_start:
                if (
                    not self.emitted
                    and line in ("", "\r")
                    and newline
                    and len(self.leading_blanks) + len(line) + len(newline)
                    <= self.max_diagnostic_bytes
                ):
                    self.leading_blanks += line + newline
                    self.line_start = True
                    text = rest
                    continue
                candidate = line.lstrip()
                can_continue = bool(self.header.match(candidate))
                if can_continue or self.interrupt_header.match(candidate):
                    self.leading_blanks = ""
                    self._start_notice(
                        candidate.rstrip("\r"), can_continue=can_continue
                    )
                    self._drop_line(newline)
                    text = rest
                    continue
                if (
                    not newline
                    and not final
                    and len(line) <= self.max_diagnostic_bytes
                    and self._possible(candidate)
                ):
                    self.pending = line
                    break
            self._emit(output, line + newline)
            self.line_start = bool(newline)
            text = rest
        if final:
            if self.leading_blanks:
                output.append(self.leading_blanks)
                self.leading_blanks = ""
                self.emitted = True
            self._end_continuation()
            self._can_continue = False
            self.dropping = False
            self.diagnostic = ""
        return "".join(output)


class TerminalNoticeGuard:
    messages = {
        "there was a network issue connecting to the server, please try again": "NETWORK",
        "our servers are experiencing high traffic right now, please try again in a minute": "RATE_LIMIT",
    }

    def __init__(self) -> None:
        self.pending = ""
        self.line_start = True

    def _verdict(self, raw: str, final: bool) -> str:
        if len(raw) > 4096:
            return "emit"
        text = re.sub(r"\s+", " ", raw.replace("\ufe0f", "")).strip().lower()
        marked = text.startswith("⚠")
        if marked:
            text = text[1:].lstrip()
            if "error:".startswith(text) and not final:
                return "hold"
            if text.startswith("error:"):
                text = text[6:].lstrip()
        for head, code in self.messages.items():
            if text in {head, head + "."}:
                if final or (marked and text.endswith(".")):
                    raise classified_error(code)
                return "hold"
            if not final and (head + ".").startswith(text):
                return "hold"
        return "emit"

    def feed(self, text: str, *, final: bool = False) -> str:
        output: list[str] = []
        for piece in text.splitlines(keepends=True):
            candidate = self.pending + piece
            self.pending = ""
            if self.line_start and self._verdict(candidate, False) == "hold":
                self.pending = candidate
            else:
                output.append(candidate)
                self.line_start = candidate.endswith(("\n", "\r"))
        if final and self.pending:
            self._verdict(self.pending, True)
            output.append(self.pending)
            self.line_start = self.pending.endswith(("\n", "\r"))
            self.pending = ""
        return "".join(output)


_UPSTREAM_CAPACITY_NOTICE_RE = re.compile(
    r"^⚠\ufe0f?\s+API error \(attempt \d+\): UNAVAILABLE \(code 503\): "
    r"No capacity available for model \S+ on the server$"
)


def strip_upstream_capacity_notices(text: str) -> tuple[str, int]:
    """Remove CLI 503 capacity retry lines and extra blank lines around them."""
    if not text:
        return text, 0
    kept: list[str] = []
    pending_blanks: list[str] = []
    removed = 0
    after_notice = False
    had_blank_before_notice = False

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _UPSTREAM_CAPACITY_NOTICE_RE.match(stripped):
            removed += 1
            if pending_blanks:
                had_blank_before_notice = True
            pending_blanks.clear()
            after_notice = True
        elif stripped == "":
            pending_blanks.append(line)
        else:
            if kept:
                if not after_notice:
                    kept.extend(pending_blanks)
                elif pending_blanks or had_blank_before_notice:
                    kept.append(pending_blanks[0] if pending_blanks else "\n")
            pending_blanks.clear()
            after_notice = False
            had_blank_before_notice = False
            kept.append(line)

    if removed == 0:
        return text, 0
    if not after_notice:
        kept.extend(pending_blanks)
    elif kept:
        kept[-1] = kept[-1].rstrip("\r\n")
    return "".join(kept), removed
